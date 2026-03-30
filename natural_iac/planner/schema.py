"""Execution plan schema — the planner's output."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Provider(str, Enum):
    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"


class ChangeAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DESTROY = "destroy"
    NO_OP = "no_op"


# ---------------------------------------------------------------------------
# Resource — a single concrete cloud resource
# ---------------------------------------------------------------------------


class Resource(BaseModel):
    """A concrete cloud resource in the execution plan.

    ``type`` and ``properties`` use Terraform resource schema conventions so
    the execution backend can render HCL directly without translation.
    """

    id: str  # "{type}.{logical_name}", e.g. "aws_ecs_service.api"
    type: str  # Terraform resource type, e.g. "aws_ecs_service"
    logical_name: str  # Terraform resource name, e.g. "api"
    component: str  # source contract component name
    properties: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)  # resource ids
    tags: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def make_id(cls, type_: str, logical_name: str) -> str:
        return f"{type_}.{logical_name}"


# ---------------------------------------------------------------------------
# Cost estimate
# ---------------------------------------------------------------------------


class CostLineItem(BaseModel):
    resource_id: str
    resource_type: str
    monthly_usd: float
    notes: str = ""


class CostConfidence(str, Enum):
    LOW = "low"      # rough order-of-magnitude
    MEDIUM = "medium"  # based on known resource sizes
    HIGH = "high"    # infracost or exact pricing API


class CostEstimate(BaseModel):
    total_monthly_usd: float
    breakdown: list[CostLineItem]
    confidence: CostConfidence = CostConfidence.LOW
    as_of: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Change tracking
# ---------------------------------------------------------------------------


class ResourceChange(BaseModel):
    resource_id: str
    action: ChangeAction


# ---------------------------------------------------------------------------
# Execution plan — top-level planner output
# ---------------------------------------------------------------------------


class ExecutionPlan(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    contract_id: str
    contract_name: str
    provider: Provider
    region: str
    resources: list[Resource]
    changes: list[ResourceChange]
    cost_estimate: CostEstimate | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def creates(self) -> list[ResourceChange]:
        return [c for c in self.changes if c.action == ChangeAction.CREATE]

    @property
    def destroys(self) -> list[ResourceChange]:
        return [c for c in self.changes if c.action == ChangeAction.DESTROY]

    @property
    def resource_map(self) -> dict[str, Resource]:
        return {r.id: r for r in self.resources}
