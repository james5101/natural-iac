"""
Standalone demo -- runs the full pipeline without CLI machinery.

Usage:
  python demo.py                                    # uses DEFAULT_INTENT below
  python demo.py --intent "I need a web API..."     # override intent on the command line
  python demo.py --intent "..." --output myapp      # set output filename stem

Prints all output to stdout with no Rich formatting so nothing gets swallowed.
"""

import argparse
import asyncio
import os
import sys
import traceback
from pathlib import Path

DEFAULT_INTENT = (
    "I need a private ec2 webserver that is on my internal network of 10.0.1.0/24,"
    "Install nginx via user-data, add 1 data disk of 50gb. Make sure disks are encrypted,"
    "It should be running ubuntu and it should be a small instance."
)

# ---- parse args -----------------------------------------------------------
_parser = argparse.ArgumentParser(description="natural-iac demo pipeline")
_parser.add_argument("--intent", "-i", default=None, help="Override the intent string")
_parser.add_argument("--output", "-o", default="demo", help="Output file stem (default: demo)")
_args = _parser.parse_args()

INTENT = _args.intent or DEFAULT_INTENT
OUTPUT_CONTRACT = Path(f"{_args.output}.contract.yaml")
OUTPUT_HCL_DIR = Path(f"{_args.output}-terraform-out")


def banner(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def check_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("ERROR: ANTHROPIC_API_KEY is not set.")
        print("  Set it with:  $env:ANTHROPIC_API_KEY = 'sk-ant-...'")
        sys.exit(1)
    print(f"API key: {key[:15]}...{key[-4:]}")
    return key


async def run(key: str) -> None:
    import anthropic
    from natural_iac.contract import contract_to_yaml, validate_contract
    from natural_iac.conventions import ConventionProfile
    from natural_iac.execution.terraform.renderer import render_to_dir
    from natural_iac.intent import IntentAgent
    from natural_iac.planner import PlannerAgent

    conventions = ConventionProfile.load()
    if conventions:
        print(f"Loaded conventions from .niac/conventions.yaml")

    client = anthropic.AsyncAnthropic(api_key=key)

    # ------------------------------------------------------------------
    # Step 1: Intent -> Contract
    # ------------------------------------------------------------------
    banner("STEP 1: Intent Agent")
    print(f"Intent: {INTENT}\n")

    intent_agent = IntentAgent(client=client, max_clarifications=2)

    async def clarification_callback(question: str) -> str:
        print(f"\n[QUESTION] {question}")
        return input("  Your answer: ")

    print("Calling Claude (intent agent)...")
    intent_result = await intent_agent.parse(INTENT, clarification_callback=clarification_callback, conventions=conventions)

    print(f"\nContract name : {intent_result.contract.name}")
    print(f"Components    : {[c.name for c in intent_result.contract.components]}")
    print(f"Questions asked: {intent_result.trace.questions_asked}")

    validation = intent_result.validation
    errors   = len(validation.errors)
    warnings = len(validation.warnings)
    print(f"Validation    : {errors} errors, {warnings} warnings")
    for v in validation.violations:
        print(f"  [{v.severity.value.upper()}] {v.rule}: {v.message}")

    yaml_str = contract_to_yaml(intent_result.contract)
    OUTPUT_CONTRACT.write_text(yaml_str, encoding="utf-8")
    print(f"\nContract saved -> {OUTPUT_CONTRACT}")

    # ------------------------------------------------------------------
    # Step 2: Contract -> Execution Plan
    # ------------------------------------------------------------------
    banner("STEP 2: Planner Agent")
    print("Calling Claude (planner agent)...")

    planner = PlannerAgent(client=client)
    plan, trace = await planner.plan(intent_result.contract, conventions=conventions)

    # Debug: show raw tool call output
    for turn in trace.turns:
        if turn.get("role") == "assistant":
            for block in (turn.get("content") or []):
                if hasattr(block, "type") and block.type == "tool_use":
                    import json
                    raw = block.input
                    print(f"\n[DEBUG] emit_plan tool input keys: {list(raw.keys())}")
                    resources_raw = raw.get("resources", "MISSING")
                    print(f"[DEBUG] resources type: {type(resources_raw).__name__}, len: {len(resources_raw) if isinstance(resources_raw, list) else 'n/a'}")
                    if isinstance(resources_raw, list) and resources_raw:
                        print(f"[DEBUG] first resource: {json.dumps(resources_raw[0], indent=2)}")

    print(f"\nRegion    : {plan.region}")
    print(f"Resources : {len(plan.resources)}")
    by_component: dict[str, list] = {}
    for r in plan.resources:
        by_component.setdefault(r.component, []).append(r.type)
    for comp, types in sorted(by_component.items()):
        print(f"  {comp:20s}  {', '.join(types)}")

    if plan.cost_estimate:
        est = plan.cost_estimate
        print(f"\nEstimated cost: ${est.total_monthly_usd:.0f}/mo  (confidence: {est.confidence.value})")
        for item in est.breakdown[:6]:
            print(f"  {item.resource_id:40s}  ${item.monthly_usd:.0f}/mo")

    # ------------------------------------------------------------------
    # Step 3: Render HCL
    # ------------------------------------------------------------------
    banner("STEP 3: Terraform HCL Renderer")
    written = render_to_dir(plan, OUTPUT_HCL_DIR, conventions=conventions)
    print(f"Written to {OUTPUT_HCL_DIR}/")
    for p in written:
        print(f"  {p.name}  ({p.stat().st_size} bytes)")

    print()
    print("--- providers.tf ---")
    print((OUTPUT_HCL_DIR / "providers.tf").read_text())

    print()
    print("--- main.tf (first 80 lines) ---")
    main_lines = (OUTPUT_HCL_DIR / "main.tf").read_text().splitlines()
    print("\n".join(main_lines[:80]))
    if len(main_lines) > 80:
        print(f"  ... ({len(main_lines) - 80} more lines)")

    banner("DONE")
    print(f"Contract : {OUTPUT_CONTRACT}")
    print(f"HCL      : {OUTPUT_HCL_DIR}/")
    print()
    print("To apply with Terraform:")
    print(f"  cd {OUTPUT_HCL_DIR}")
    print("  terraform init")
    print("  terraform apply")


if __name__ == "__main__":
    print("natural-iac demo")
    print(f"Python {sys.version.split()[0]}")

    key = check_key()

    try:
        asyncio.run(run(key))
    except Exception:
        print("\nERROR during demo:")
        traceback.print_exc()
        sys.exit(1)
