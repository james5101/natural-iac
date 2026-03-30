"""
Contract schema — the load-bearing layer of natural-iac.

A contract is provider-agnostic: it describes *what* you need and the
invariants that must hold, not *how* a specific cloud implements it.
Committed to git; intent is ephemeral; execution is derived.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------

class SchemaVersion(str, Enum):
    V1 = "v1"


# ---------------------------------------------------------------------------
# Component roles — provider-agnostic abstractions
# ---------------------------------------------------------------------------

class ComponentRole(str, Enum):
    WEB_API = "web_api"
    WORKER = "worker"
    SCHEDULER = "scheduler"
    PRIMARY_DATASTORE = "primary_datastore"
    CACHE = "cache"
    JOB_QUEUE = "job_queue"
    MESSAGE_BROKER = "message_broker"
    OBJECT_STORAGE = "object_storage"
    CDN = "cdn"
    LOAD_BALANCER = "load_balancer"
    SECRET_STORE = "secret_store"
    NETWORK = "network"


# ---------------------------------------------------------------------------
# Component requirements
# ---------------------------------------------------------------------------

class Availability(str, Enum):
    """Availability tier — execution backends map this to concrete HA config."""
    DEVELOPMENT = "development"  # single instance, can go down
    STANDARD = "standard"        # reasonable uptime, single-AZ ok
    HIGH = "high"                # multi-AZ, no single point of failure
    CRITICAL = "critical"        # multi-region active-active


class SizeHint(str, Enum):
    """Rough sizing hint — backends translate to instance types / capacity units."""
    MICRO = "micro"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    XLARGE = "xlarge"


class ComponentRequirements(BaseModel):
    availability: Availability = Availability.STANDARD
    size_hint: SizeHint = SizeHint.SMALL
    publicly_accessible: bool = False
    multi_region: bool = False
    read_replicas: int = Field(default=0, ge=0)
    # Datastore-specific
    backup_retention_days: int = Field(default=7, ge=0)
    # Freeform role-specific settings that don't belong in the core schema
    extra: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Raw override — escape hatch, always supported
# ---------------------------------------------------------------------------

class RawOverride(BaseModel):
    """Pass-through block to execution backend.

    Use sparingly — the whole point of the contract is provider-agnostic
    intent. But escape hatches are first-class citizens here.
    """
    provider: str  # e.g. "aws", "gcp", "terraform"
    content: dict[str, Any]


# ---------------------------------------------------------------------------
# Component
# ---------------------------------------------------------------------------

class Component(BaseModel):
    name: str = Field(..., pattern=r"^[a-z][a-z0-9_-]*$")
    role: ComponentRole
    description: str = ""
    requirements: ComponentRequirements = Field(default_factory=ComponentRequirements)
    depends_on: list[str] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)
    raw_override: RawOverride | None = None

    @model_validator(mode="after")
    def validate_depends_on_not_self(self) -> "Component":
        if self.name in self.depends_on:
            raise ValueError(f"Component '{self.name}' cannot depend on itself")
        return self


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

class CostConstraint(BaseModel):
    max_monthly_usd: float | None = Field(default=None, gt=0)
    alert_threshold_usd: float | None = Field(default=None, gt=0)


class SecurityConstraint(BaseModel):
    no_public_datastores: bool = True
    encryption_at_rest: bool = True
    encryption_in_transit: bool = True
    # Empty list = no regional restriction
    allowed_regions: list[str] = Field(default_factory=list)
    # Explicit deny list takes precedence over allowed_regions
    denied_regions: list[str] = Field(default_factory=list)


class ComplianceConstraint(BaseModel):
    # e.g. ["SOC2", "HIPAA", "PCI-DSS"]
    frameworks: list[str] = Field(default_factory=list)
    # ISO 3166-1 alpha-2 country codes for data residency requirements
    data_residency: list[str] = Field(default_factory=list)


class Constraints(BaseModel):
    cost: CostConstraint = Field(default_factory=CostConstraint)
    security: SecurityConstraint = Field(default_factory=SecurityConstraint)
    compliance: ComplianceConstraint = Field(default_factory=ComplianceConstraint)


# ---------------------------------------------------------------------------
# Invariants — named assertions over the resolved resource graph
# ---------------------------------------------------------------------------

class InvariantSeverity(str, Enum):
    WARN = "warn"    # violation is reported but doesn't block apply
    ERROR = "error"  # violation blocks apply


class Invariant(BaseModel):
    """A named, human-readable assertion that the planner must verify.

    Rules are evaluated by the constraint validator against the resolved
    component graph, not raw cloud resources.
    """
    name: str = Field(..., pattern=r"^[a-z][a-z0-9_-]*$")
    description: str
    # Structured rule expression — validated by the constraint validator.
    # Currently supports a small DSL; raw strings are treated as documentation.
    rule: str
    severity: InvariantSeverity = InvariantSeverity.ERROR


# ---------------------------------------------------------------------------
# Top-level contract
# ---------------------------------------------------------------------------

class InfraContract(BaseModel):
    schema_version: SchemaVersion = SchemaVersion.V1
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = Field(..., pattern=r"^[a-z][a-z0-9_-]*$")
    description: str = ""
    # CI gates on this flag before allow apply
    human_reviewed: bool = False
    components: list[Component] = Field(..., min_length=1)
    constraints: Constraints = Field(default_factory=Constraints)
    invariants: list[Invariant] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_component_names_unique(self) -> "InfraContract":
        names = [c.name for c in self.components]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"Duplicate component names: {duplicates}")
        return self

    @model_validator(mode="after")
    def validate_depends_on_exist(self) -> "InfraContract":
        names = {c.name for c in self.components}
        for component in self.components:
            unknown = set(component.depends_on) - names
            if unknown:
                raise ValueError(
                    f"Component '{component.name}' depends_on unknown components: {unknown}"
                )
        return self

    @model_validator(mode="after")
    def validate_invariant_names_unique(self) -> "InfraContract":
        names = [i.name for i in self.invariants]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"Duplicate invariant names: {duplicates}")
        return self
