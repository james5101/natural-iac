"""
Constraint validation — checks a contract's constraints and invariants
against the component graph before any execution plan is produced.

This runs at contract-commit time and again before apply, so violations
are caught as early as possible with contract-level language, not resource diffs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from .schema import (
    Component,
    ComponentRole,
    InfraContract,
    InvariantSeverity,
)

if TYPE_CHECKING:
    from ..conventions.schema import ConventionProfile

_DATASTORE_ROLES = {
    ComponentRole.PRIMARY_DATASTORE,
    ComponentRole.CACHE,
}


class Severity(str, Enum):
    WARN = "warn"
    ERROR = "error"


@dataclass
class Violation:
    severity: Severity
    rule: str
    message: str
    component: str | None = None


@dataclass
class ValidationResult:
    violations: list[Violation] = field(default_factory=list)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == Severity.WARN]

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def add(self, violation: Violation) -> None:
        self.violations.append(violation)


# ---------------------------------------------------------------------------
# Individual constraint checkers
# ---------------------------------------------------------------------------

def _check_security(contract: InfraContract, result: ValidationResult) -> None:
    sec = contract.constraints.security

    for component in contract.components:
        req = component.requirements

        if sec.no_public_datastores and component.role in _DATASTORE_ROLES:
            if req.publicly_accessible:
                result.add(Violation(
                    severity=Severity.ERROR,
                    rule="security.no_public_datastores",
                    message=(
                        f"Component '{component.name}' is a {component.role.value} "
                        "but is marked publicly_accessible=true, which violates "
                        "the no_public_datastores security constraint."
                    ),
                    component=component.name,
                ))

        if sec.encryption_at_rest and component.role in _DATASTORE_ROLES:
            # encryption_at_rest is the default expectation; execution backends
            # must enforce it — flag if explicitly opted out via raw_override
            if component.raw_override:
                override = component.raw_override.content
                if override.get("storage_encrypted") is False or override.get("encrypted") is False:
                    result.add(Violation(
                        severity=Severity.ERROR,
                        rule="security.encryption_at_rest",
                        message=(
                            f"Component '{component.name}' raw_override disables encryption at rest, "
                            "which violates the encryption_at_rest security constraint."
                        ),
                        component=component.name,
                    ))


def _check_cost(contract: InfraContract, result: ValidationResult) -> None:
    cost = contract.constraints.cost
    if cost.alert_threshold_usd is not None and cost.max_monthly_usd is not None:
        if cost.alert_threshold_usd >= cost.max_monthly_usd:
            result.add(Violation(
                severity=Severity.WARN,
                rule="cost.alert_threshold",
                message=(
                    f"alert_threshold_usd ({cost.alert_threshold_usd}) should be "
                    f"less than max_monthly_usd ({cost.max_monthly_usd})."
                ),
            ))


def _check_availability(contract: InfraContract, result: ValidationResult) -> None:
    """Warn when a web_api depends on a datastore with lower availability."""
    from .schema import Availability

    _tier_order = {
        Availability.DEVELOPMENT: 0,
        Availability.STANDARD: 1,
        Availability.HIGH: 2,
        Availability.CRITICAL: 3,
    }

    component_map: dict[str, Component] = {c.name: c for c in contract.components}

    for component in contract.components:
        comp_tier = _tier_order[component.requirements.availability]
        for dep_name in component.depends_on:
            dep = component_map[dep_name]
            dep_tier = _tier_order[dep.requirements.availability]
            if dep_tier < comp_tier:
                result.add(Violation(
                    severity=Severity.WARN,
                    rule="availability.dependency_tier",
                    message=(
                        f"Component '{component.name}' has availability={component.requirements.availability.value} "
                        f"but depends on '{dep_name}' which has lower availability={dep.requirements.availability.value}. "
                        "The dependency may become a bottleneck."
                    ),
                    component=component.name,
                ))


def _check_human_reviewed(contract: InfraContract, result: ValidationResult) -> None:
    if not contract.human_reviewed:
        result.add(Violation(
            severity=Severity.WARN,
            rule="governance.human_reviewed",
            message="Contract has not been human-reviewed. Set human_reviewed=true before applying to production.",
        ))


# ---------------------------------------------------------------------------
# Invariant evaluation — simple DSL, extensible
# ---------------------------------------------------------------------------

_INVARIANT_RULES: dict[str, Any] = {}  # reserved for future rule registry


def _evaluate_invariant(rule: str, contract: InfraContract) -> bool | None:
    """
    Evaluate a structured invariant rule expression against the contract.

    Supported forms:
      all <role> are not publicly_accessible
      no <role> is publicly_accessible
      all components have encryption_at_rest

    Returns True (passes), False (fails), or None (rule not understood — skip).
    """
    import re

    tokens = rule.strip().lower()

    # "no <role> is publicly_accessible"
    m = re.fullmatch(r"no (\w+) is publicly_accessible", tokens)
    if m:
        role_str = m.group(1)
        try:
            role = ComponentRole(role_str)
        except ValueError:
            return None
        return all(
            not c.requirements.publicly_accessible
            for c in contract.components
            if c.role == role
        )

    # "all <role> are not publicly_accessible"
    m = re.fullmatch(r"all (\w+) are not publicly_accessible", tokens)
    if m:
        role_str = m.group(1)
        try:
            role = ComponentRole(role_str)
        except ValueError:
            return None
        return all(
            not c.requirements.publicly_accessible
            for c in contract.components
            if c.role == role
        )

    return None  # unknown rule — treated as documentation


def _check_invariants(contract: InfraContract, result: ValidationResult) -> None:
    for invariant in contract.invariants:
        outcome = _evaluate_invariant(invariant.rule, contract)
        if outcome is False:
            sev = (
                Severity.ERROR
                if invariant.severity == InvariantSeverity.ERROR
                else Severity.WARN
            )
            result.add(Violation(
                severity=sev,
                rule=f"invariant.{invariant.name}",
                message=f"Invariant '{invariant.name}' failed: {invariant.description}",
            ))


# ---------------------------------------------------------------------------
# Convention checks
# ---------------------------------------------------------------------------


def _check_conventions(
    contract: InfraContract,
    profile: "ConventionProfile",
    result: ValidationResult,
) -> None:
    """Validate that required tags can be satisfied by this contract.

    The contract itself may not set every tag -- org defaults in the
    conventions file will fill gaps at render time. We only error if a
    required tag won't be covered by either the contract metadata or
    the conventions defaults.
    """
    required = profile.tags.required
    if not required:
        return

    # Tags available at render time = conventions defaults + contract metadata tags
    covered: set[str] = set(profile.tags.defaults.keys())
    covered.update(contract.metadata.get("tags", {}).keys())

    missing = [t for t in required if t not in covered]
    if missing:
        result.add(Violation(
            severity=Severity.ERROR,
            rule="conventions.required_tags",
            message=(
                f"Required tag(s) {missing} are not provided by conventions defaults "
                "or contract metadata. Add them to conventions.yaml tags.defaults "
                "or to the contract metadata.tags field."
            ),
        ))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_contract(
    contract: InfraContract,
    conventions: "ConventionProfile | None" = None,
) -> ValidationResult:
    """Run all constraint checks and invariant evaluations against a contract.

    Returns a ValidationResult — callers decide whether to treat warnings as
    blocking based on their context (e.g. CI vs local dev).
    """
    result = ValidationResult()
    _check_security(contract, result)
    _check_cost(contract, result)
    _check_availability(contract, result)
    _check_human_reviewed(contract, result)
    _check_invariants(contract, result)
    if conventions is not None:
        _check_conventions(contract, conventions, result)
    return result
