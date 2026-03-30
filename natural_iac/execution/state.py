"""
natural-iac state — our source of truth, separate from tfstate.

Stores what resources exist, their cloud IDs, and enough context to
detect drift against the originating contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class ResourceState(BaseModel):
    """State for a single cloud resource."""

    id: str                       # our canonical id: "{type}.{logical_name}"
    type: str
    logical_name: str
    component: str                # originating contract component
    provider_id: str | None = None  # cloud-assigned ID (ARN, resource ID, etc.)
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_refreshed: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class State(BaseModel):
    """Full deployment state for one contract."""

    contract_id: str
    contract_name: str
    provider: str
    region: str
    resources: dict[str, ResourceState] = Field(default_factory=dict)
    last_applied: datetime | None = None

    # -----------------------------------------------------------------------

    def upsert(self, resource: ResourceState) -> None:
        self.resources[resource.id] = resource

    def remove(self, resource_id: str) -> None:
        self.resources.pop(resource_id, None)

    def get(self, resource_id: str) -> ResourceState | None:
        return self.resources.get(resource_id)

    # -----------------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------------

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "State":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def empty(cls, contract_id: str, contract_name: str, provider: str, region: str) -> "State":
        return cls(
            contract_id=contract_id,
            contract_name=contract_name,
            provider=provider,
            region=region,
        )
