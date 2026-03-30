"""Abstract execution backend interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..planner.schema import ExecutionPlan
from .state import State


@dataclass
class ApplyResult:
    success: bool
    created: list[str] = field(default_factory=list)   # resource ids
    updated: list[str] = field(default_factory=list)
    destroyed: list[str] = field(default_factory=list)
    error: str | None = None
    stdout: str = ""
    stderr: str = ""

    @property
    def total_changes(self) -> int:
        return len(self.created) + len(self.updated) + len(self.destroyed)


class ExecutionBackend(ABC):
    """Pluggable execution backend.

    The backend receives a plan and makes cloud reality match it.
    It is responsible for ordering, idempotency, and returning updated state.
    The contract and plan layers never know which backend is in use.
    """

    @abstractmethod
    async def apply(
        self,
        plan: ExecutionPlan,
        state: State,
        *,
        auto_approve: bool = False,
    ) -> tuple[ApplyResult, State]:
        """Apply a plan, returning the result and updated state."""

    @abstractmethod
    async def destroy(
        self,
        plan: ExecutionPlan,
        state: State,
        *,
        auto_approve: bool = False,
    ) -> tuple[ApplyResult, State]:
        """Destroy all resources tracked in the plan."""

    @abstractmethod
    async def refresh(self, plan: ExecutionPlan, state: State) -> State:
        """Re-read actual cloud state and return an updated State object."""
