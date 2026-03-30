"""
Planner agent — contract → execution plan.

Takes a validated InfraContract and uses Claude to produce a concrete
AWS resource graph. No clarification loop: the contract is the spec.
All turns are recorded for traceability.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

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
)

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
        user_message = _build_user_message(contract)
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
        plan = _build_plan(plan_data, contract, provider)
        plan.cost_estimate = estimate_cost(plan)

        return plan, trace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_user_message(contract: InfraContract) -> str:
    """Render the contract as a clear planning request."""
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
    return "\n".join(lines)


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
) -> ExecutionPlan:
    """Assemble an ExecutionPlan from the raw tool call payload."""
    region = data.get("region") or _infer_region(contract)
    raw_resources: list[dict] = data.get("resources", [])

    resources: list[Resource] = []
    for raw in raw_resources:
        rtype = raw["type"]
        lname = raw["logical_name"]
        rid = Resource.make_id(rtype, lname)

        # Resolve depends_on from logical ids to canonical ids
        depends_on_logical: list[str] = raw.get("depends_on_logical", [])
        # IDs are already in "{type}.{logical_name}" form — pass through
        depends_on = [d for d in depends_on_logical if d]

        # Merge contract-level tags + component tags + managed-by tag
        component_name = raw.get("component", "shared")
        base_tags = {
            "managed_by": "natural-iac",
            "contract": contract.name,
            "component": component_name,
        }
        component = next(
            (c for c in contract.components if c.name == component_name), None
        )
        if component:
            base_tags.update(component.tags)
        base_tags.update(raw.get("tags") or {})

        resources.append(Resource(
            id=rid,
            type=rtype,
            logical_name=lname,
            component=component_name,
            properties=raw.get("properties", {}),
            depends_on=depends_on,
            tags=base_tags,
        ))

    # For v1: no state, so every resource is a CREATE
    changes = [
        ResourceChange(resource_id=r.id, action=ChangeAction.CREATE)
        for r in resources
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
