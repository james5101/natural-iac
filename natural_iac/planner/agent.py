"""
Planner agent — contract → execution plan.

Takes a validated InfraContract and uses Claude to produce a concrete
AWS resource graph. No clarification loop: the contract is the spec.
All turns are recorded for traceability.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anthropic

from ..contract.schema import InfraContract
from .cost import estimate_cost
from .prompts import SYSTEM_PROMPT
from .schema import (
    ChangeAction,
    ExecutionPlan,
    Provider,
    Resource,
    ResourceChange,
    ResourceKind,
)

if TYPE_CHECKING:
    from ..conventions.schema import ConventionProfile

DEFAULT_MODEL = "claude-opus-4-6"
MAX_TOKENS = 16000
DEFAULT_REGION = "us-east-1"


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


@dataclass
class PlannerTrace:
    contract_id: str
    turns: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


def _emit_plan_tool() -> dict:
    return {
        "name": "emit_plan",
        "description": (
            "Emit the complete resource graph for this contract. "
            "Include every AWS resource needed to deploy all contract components. "
            "Use Terraform resource type names and property conventions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "AWS region for the deployment, e.g. us-east-1",
                },
                "resources": {
                    "type": "array",
                    "description": "Complete list of AWS resources in the plan.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "description": "Terraform resource type, e.g. aws_ecs_service",
                            },
                            "logical_name": {
                                "type": "string",
                                "description": "Terraform resource logical name, e.g. api",
                            },
                            "component": {
                                "type": "string",
                                "description": "Contract component name this resource implements, or 'shared' for shared infra.",
                            },
                            "kind": {
                                "type": "string",
                                "enum": ["resource", "data"],
                                "description": (
                                    "Use 'data' for existing resources from the contract's "
                                    "existing_resources list (Terraform data sources). "
                                    "Use 'resource' (default) for all managed resources being created."
                                ),
                            },
                            "properties": {
                                "type": "object",
                                "description": "Terraform resource arguments (snake_case keys).",
                                "additionalProperties": True,
                            },
                            "depends_on_logical": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of '{type}.{logical_name}' ids this resource depends on.",
                            },
                            "tags": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                        },
                        "required": ["type", "logical_name", "component", "properties"],
                    },
                },
            },
            "required": ["region", "resources"],
        },
    }


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class PlannerAgent:
    """Translates a validated InfraContract into an ExecutionPlan.

    Parameters
    ----------
    client:
        An ``anthropic.AsyncAnthropic`` instance.
    model:
        Claude model to use.
    """

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self.client = client
        self.model = model

    async def plan(
        self,
        contract: InfraContract,
        provider: Provider = Provider.AWS,
        conventions: "ConventionProfile | None" = None,
    ) -> tuple[ExecutionPlan, PlannerTrace]:
        """Produce an ExecutionPlan for the given contract.

        Returns
        -------
        plan:
            The resolved execution plan with resources, changes, and cost estimate.
        trace:
            Full conversation record for observability.
        """
        trace = PlannerTrace(contract_id=contract.id)

        # Fetch module variables before building the prompt so the LLM knows
        # what inputs each module override actually accepts.
        module_vars: "dict[str, list]" = {}
        if conventions is not None and conventions.modules:
            module_vars = conventions.load_module_variables()

        user_message = _build_user_message(contract, conventions, module_vars)
        messages: list[dict] = [{"role": "user", "content": user_message}]
        trace.turns.append({"role": "user", "content": user_message})

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=[_emit_plan_tool()],
            tool_choice={"type": "any"},  # must call the tool
            messages=messages,
        )

        trace.turns.append({"role": "assistant", "content": response.content})

        plan_data = _extract_plan_tool_call(response)
        plan = _build_plan(plan_data, contract, provider, conventions)
        plan.cost_estimate = estimate_cost(plan)

        return plan, trace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_user_message(
    contract: InfraContract,
    conventions: "ConventionProfile | None" = None,
    module_vars: "dict[str, list] | None" = None,
) -> str:
    """Render the contract (and optional conventions) as a clear planning request."""
    from ..contract.serializer import contract_to_yaml

    lines = [
        "Please produce an AWS execution plan for the following infrastructure contract.",
        "",
        "```yaml",
        contract_to_yaml(contract).strip(),
        "```",
        "",
        "Apply all security constraints from the contract. Emit the complete resource graph.",
    ]

    if conventions is not None:
        lines += ["", _build_conventions_section(conventions, module_vars or {}, contract.metadata)]

    return "\n".join(lines)


def _build_conventions_section(
    conventions: "ConventionProfile",
    module_vars: "dict[str, list]",
    contract_metadata: "dict[str, Any] | None" = None,
) -> str:
    """Render the conventions as an instruction block appended to the user message."""
    from ..conventions.module_reader import format_variables_for_prompt

    parts = ["## Org conventions -- follow these exactly", ""]

    naming = conventions.naming
    if naming.pattern != "{component}-{type_short}" or naming.variables or naming.type_short_map:
        parts.append(f"### Naming pattern: `{naming.pattern}`")
        if naming.variables:
            parts.append("Variables: " + ", ".join(f"{k}={v}" for k, v in naming.variables.items()))
        if naming.type_short_map:
            parts.append("Type short names:")
            for rtype, short in naming.type_short_map.items():
                parts.append(f"  {rtype} -> {short}")
        parts.append(
            "Apply this pattern to the logical_name of EVERY resource. "
            "Use the component name from the contract for {component}."
        )
        parts.append("")

    # Resolve tag defaults: naming variable defaults + flat contract metadata values
    meta_vars = {**naming.variables}
    if contract_metadata:
        meta_vars.update({k: str(v) for k, v in contract_metadata.items() if isinstance(v, str)})
    tag_defaults = conventions.tags.resolve_defaults(meta_vars)
    if tag_defaults:
        parts.append("### Required tags on all resources:")
        for k, v in tag_defaults.items():
            parts.append(f"  {k} = \"{v}\"")
        parts.append("Include these in the tags object of every resource.")
        parts.append("")

    if conventions.modules:
        parts.append("### Module overrides")
        parts.append(
            "When planning these resource types, emit the module's variable names "
            "as property keys (not the raw Terraform resource property names). "
            "The renderer will emit a module block using exactly the names you provide."
        )
        parts.append("")
        for mod in conventions.modules:
            parts.append(f"#### {mod.match}")
            parts.append(f"  source: {mod.source}")
            parts.append(f"  module name template: {mod.name_template}")

            vars_ = module_vars.get(mod.match, [])
            if vars_:
                parts.append(f"  Variables (fetched from module source -- use these exact names):")
                parts.append(format_variables_for_prompt(vars_))
                parts.append(
                    "  IMPORTANT: Emit property names that match these variable names exactly. "
                    "Only emit variables that are relevant to the contract component. "
                    "Omit optional variables unless you have a specific value for them."
                )
            elif mod.input_map:
                parts.append("  Property name mapping (emit the LEFT side; renderer translates to RIGHT):")
                for our_name, module_var in mod.input_map.items():
                    parts.append(f"    {our_name} -> {module_var}")
                if mod.passthrough:
                    parts.append(f"  Pass through unchanged: {', '.join(mod.passthrough)}")
            parts.append("")

    if conventions.defaults.overrides:
        parts.append("### Property defaults (applied after your output -- you may omit these):")
        for rtype, props in conventions.defaults.overrides.items():
            parts.append(f"  {rtype}: {props}")
        parts.append("")

    return "\n".join(parts)


def _extract_plan_tool_call(response: anthropic.types.Message) -> dict[str, Any]:
    """Pull the emit_plan tool call payload out of a response."""
    for block in response.content:
        if block.type == "tool_use" and block.name == "emit_plan":
            return block.input
    raise RuntimeError(
        "Planner did not emit a plan. Response stop_reason: "
        f"{response.stop_reason}. Content blocks: "
        f"{[b.type for b in response.content]}"
    )


def _infer_region(contract: InfraContract) -> str:
    allowed = contract.constraints.security.allowed_regions
    return allowed[0] if allowed else DEFAULT_REGION


def _build_plan(
    data: dict[str, Any],
    contract: InfraContract,
    provider: Provider,
    conventions: "ConventionProfile | None" = None,
) -> ExecutionPlan:
    """Assemble an ExecutionPlan from the raw tool call payload."""
    region = data.get("region") or _infer_region(contract)
    raw_resources: list[dict] = data.get("resources", [])

    # Pre-compute convention tag defaults once (avoids repeated resolution).
    # Merge naming variable defaults with flat values from contract.metadata so
    # that user-captured values (e.g. env=dev stored by the intent agent) resolve
    # ${var} placeholders in tag defaults.  Contract metadata wins on conflict.
    convention_tag_defaults: dict[str, str] = {}
    if conventions is not None:
        naming_vars = {**conventions.naming.variables}
        # Only pull flat string values from metadata — skip nested dicts (e.g. tags)
        naming_vars.update(
            {k: str(v) for k, v in contract.metadata.items() if isinstance(v, str)}
        )
        convention_tag_defaults = conventions.tags.resolve_defaults(naming_vars)

    resources: list[Resource] = []
    for raw in raw_resources:
        rtype = raw["type"]
        lname = raw["logical_name"]
        kind = ResourceKind(raw.get("kind", "resource"))

        component_name = raw.get("component", "shared")

        # Post-process logical_name using naming convention (safety net over LLM output).
        # Only fires for resource types explicitly listed in type_short_map — this
        # prevents smashing the LLM's differentiated names (e.g. execution-role vs
        # task-role) when multiple resources of the same type serve one component.
        naming = conventions.naming if conventions is not None else None
        if (
            naming is not None
            and rtype in naming.type_short_map
            and kind == ResourceKind.RESOURCE
            and component_name != "shared"
        ):
            lname = naming.apply(rtype, component_name)

        rid = Resource.make_id(rtype, lname)

        # Resolve depends_on from logical ids to canonical ids
        depends_on_logical: list[str] = raw.get("depends_on_logical", [])
        depends_on = [d for d in depends_on_logical if d]

        # Data sources are read-only lookups -- no managed_by tags
        if kind == ResourceKind.DATA:
            tags: dict[str, str] = {}
        else:
            base_tags: dict[str, str] = {**convention_tag_defaults}
            base_tags.update({
                "managed_by": "natural-iac",
                "contract": contract.name,
                "component": component_name,
            })
            component = next(
                (c for c in contract.components if c.name == component_name), None
            )
            if component:
                base_tags.update(component.tags)
            base_tags.update(raw.get("tags") or {})
            tags = base_tags

        # Apply org-wide property defaults (conventions win over LLM output)
        properties: dict[str, Any] = raw.get("properties", {})
        if conventions is not None and kind == ResourceKind.RESOURCE:
            properties = conventions.defaults.apply(rtype, properties)

        resources.append(Resource(
            id=rid,
            type=rtype,
            logical_name=lname,
            component=component_name,
            kind=kind,
            properties=properties,
            depends_on=depends_on,
            tags=tags,
        ))

    # For v1: no state, so every managed resource is a CREATE.
    # Data sources are not changes — they already exist.
    changes = [
        ResourceChange(resource_id=r.id, action=ChangeAction.CREATE)
        for r in resources
        if r.kind == ResourceKind.RESOURCE
    ]

    return ExecutionPlan(
        id=str(uuid.uuid4()),
        contract_id=contract.id,
        contract_name=contract.name,
        provider=provider,
        region=region,
        resources=resources,
        changes=changes,
    )
