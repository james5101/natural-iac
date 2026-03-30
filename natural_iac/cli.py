"""
niac CLI - natural language -> infrastructure contract -> Terraform HCL.

  niac parse "I need a web API with Postgres and a Redis cache" -o app.contract.yaml
  niac validate app.contract.yaml
  niac plan app.contract.yaml
  niac plan app.contract.yaml --render-dir ./terraform-out
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import anthropic
import typer
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .contract import contract_to_yaml, load_contract, validate_contract
from .contract.validator import Severity
from .execution.terraform.renderer import render_plan, render_to_dir
from .intent import IntentAgent
from .planner import PlannerAgent
from .planner.schema import CostEstimate

app = typer.Typer(
    name="niac",
    help="Intent-based infrastructure as code.",
    no_args_is_help=True,
)
# Explicitly bind to sys.stdout/stderr so Rich writes through Python's
# stream rather than the Windows Console API handle, which PowerShell
# doesn't capture.
console = Console(file=sys.stdout, force_terminal=True)
err_console = Console(file=sys.stderr, force_terminal=True)

DEFAULT_MODEL = "claude-opus-4-6"


def _require_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        err_console.print(
            "[red]Error:[/red] ANTHROPIC_API_KEY is not set.\n"
            "  Export it before running: [dim]export ANTHROPIC_API_KEY=sk-ant-...[/dim]"
        )
        raise typer.Exit(1)
    return key


# ---------------------------------------------------------------------------
# niac parse
# ---------------------------------------------------------------------------


@app.command()
def parse(
    intent: str = typer.Argument(..., help="Natural language description of what you need."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write contract YAML to this file."),
    no_interactive: bool = typer.Option(False, "--no-interactive", help="Skip clarifying questions."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", "-m", help="Claude model to use."),
) -> None:
    """Parse natural language intent into an infrastructure contract."""
    api_key = _require_api_key()
    client = anthropic.AsyncAnthropic(api_key=api_key)
    agent = IntentAgent(client=client, model=model)

    console.print(f"\n[bold]Intent:[/bold] {intent}\n")

    async def run():
        async def clarification_callback(question: str) -> str:
            console.print(f"[bold yellow]?[/bold yellow] {question}")
            return typer.prompt("  Answer")

        callback = None if no_interactive else clarification_callback

        console.print("[dim]-> Parsing intent...[/dim]")
        return await agent.parse(intent, clarification_callback=callback)

    try:
        result = asyncio.run(run())
    except Exception as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)

    _print_validation(result.validation)

    yaml_str = contract_to_yaml(result.contract)
    console.print()
    console.print(Panel(
        Syntax(yaml_str, "yaml", theme="monokai"),
        title=f"[bold green]Contract: {result.contract.name}[/bold green]",
        border_style="green",
    ))

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(yaml_str, encoding="utf-8")
        console.print(f"[dim]Saved -> {output}[/dim]")
    else:
        console.print(
            "\n[dim]Tip: save with [/dim][bold]-o app.contract.yaml[/bold]"
            "[dim] then run [/dim][bold]niac plan app.contract.yaml[/bold]"
        )


# ---------------------------------------------------------------------------
# niac validate
# ---------------------------------------------------------------------------


@app.command()
def validate(
    contract_file: Path = typer.Argument(..., help="Path to a contract YAML file."),
    strict: bool = typer.Option(False, "--strict", help="Treat warnings as errors."),
) -> None:
    """Validate a contract file against its constraints and invariants."""
    if not contract_file.exists():
        err_console.print(f"[red]Error:[/red] File not found: {contract_file}")
        raise typer.Exit(1)

    try:
        contract = load_contract(contract_file)
    except Exception as e:
        err_console.print(f"[red]Schema error:[/red] {e}")
        raise typer.Exit(1)

    console.print(f"\n[bold]Contract:[/bold] {contract.name}  [dim]({len(contract.components)} components)[/dim]\n")
    result = validate_contract(contract)
    _print_validation(result)

    if not result.passed or (strict and result.warnings):
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# niac plan
# ---------------------------------------------------------------------------


@app.command()
def plan(
    contract_file: Path = typer.Argument(..., help="Path to a validated contract YAML file."),
    render_dir: Path | None = typer.Option(None, "--render-dir", "-r", help="Write HCL files to this directory."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", "-m", help="Claude model to use."),
) -> None:
    """Resolve a contract to an execution plan and render Terraform HCL."""
    if not contract_file.exists():
        err_console.print(f"[red]Error:[/red] File not found: {contract_file}")
        raise typer.Exit(1)

    try:
        contract = load_contract(contract_file)
    except Exception as e:
        err_console.print(f"[red]Schema error:[/red] {e}")
        raise typer.Exit(1)

    console.print(f"\n[bold]Contract:[/bold] {contract.name}  [dim]({len(contract.components)} components)[/dim]")

    # Validate first - block on errors
    validation = validate_contract(contract)
    _print_validation(validation)
    if not validation.passed:
        raise typer.Exit(1)

    api_key = _require_api_key()
    client = anthropic.AsyncAnthropic(api_key=api_key)
    agent = PlannerAgent(client=client, model=model)

    async def run():
        console.print("\n[dim]-> Running planner...[/dim]")
        return await agent.plan(contract)

    try:
        execution_plan, _trace = asyncio.run(run())
    except Exception as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)

    # Cost estimate
    console.print()
    _print_cost(execution_plan.cost_estimate)

    # Change summary
    console.print()
    _print_changes(execution_plan)

    # HCL output
    hcl_files = render_plan(execution_plan)
    console.print()
    for filename, content in hcl_files.items():
        console.print(Panel(
            Syntax(content, "hcl", theme="monokai", line_numbers=True),
            title=f"[bold]{filename}[/bold]",
            border_style="blue",
        ))

    if render_dir:
        written = render_to_dir(execution_plan, render_dir)
        console.print(f"\n[dim]HCL written to {render_dir}/[/dim]")
        for p in written:
            console.print(f"  [dim]{p.name}[/dim]")
        console.print(
            "\n[dim]To apply:[/dim]\n"
            f"  [bold]cd {render_dir} && terraform init && terraform apply[/bold]"
        )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _print_validation(result) -> None:
    if not result.violations:
        console.print("[bold green]OK[/bold green] No violations.")
        return

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("", width=6)
    table.add_column("Rule", style="dim", width=36)
    table.add_column("Message")

    for v in result.violations:
        color = "red" if v.severity == Severity.ERROR else "yellow"
        icon = "ERR" if v.severity == Severity.ERROR else "WARN"
        table.add_row(f"[{color}]{icon}[/{color}]", v.rule, v.message)

    console.print(table)

    errors, warnings = len(result.errors), len(result.warnings)
    parts = []
    if errors:
        parts.append(f"[red]{errors} error{'s' if errors != 1 else ''}[/red]")
    if warnings:
        parts.append(f"[yellow]{warnings} warning{'s' if warnings != 1 else ''}[/yellow]")
    if parts:
        console.print("  " + ", ".join(parts))


def _print_cost(estimate: CostEstimate | None) -> None:
    if estimate is None:
        return

    total = estimate.total_monthly_usd
    confidence_color = {"low": "yellow", "medium": "cyan", "high": "green"}.get(
        estimate.confidence.value, "white"
    )

    console.print(
        f"[bold]Estimated cost:[/bold] "
        f"[bold cyan]${total:.0f}/mo[/bold cyan]  "
        f"[dim](confidence: [{confidence_color}]{estimate.confidence.value}[/{confidence_color}])[/dim]"
    )

    # Top cost drivers (non-zero, up to 5)
    top = [i for i in estimate.breakdown if i.monthly_usd > 0][:5]
    if top:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Resource", style="dim")
        table.add_column("Cost", justify="right")
        for item in top:
            table.add_row(item.resource_id, f"${item.monthly_usd:.0f}/mo")
        console.print(table)


def _print_changes(execution_plan) -> None:
    creates = len(execution_plan.creates)
    total = len(execution_plan.resources)
    console.print(
        f"[bold]Resources:[/bold] [green]+{creates} to create[/green]  "
        f"[dim]({total} total)[/dim]"
    )

    # Group by component
    by_component: dict[str, list] = {}
    for r in execution_plan.resources:
        by_component.setdefault(r.component, []).append(r)

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Component", style="bold")
    table.add_column("Resources", style="dim")
    for component, resources in sorted(by_component.items()):
        types = ", ".join(r.type for r in resources)
        table.add_row(component, types)
    console.print(table)
