"""Contract schema, serialization, and validation tests."""

import pytest
from pydantic import ValidationError

from natural_iac.contract import (
    Availability,
    Component,
    ComponentRequirements,
    ComponentRole,
    Constraints,
    InfraContract,
    Invariant,
    InvariantSeverity,
    SecurityConstraint,
    SizeHint,
    contract_from_yaml,
    contract_to_yaml,
    validate_contract,
)
from natural_iac.contract.validator import Severity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_contract(**overrides) -> InfraContract:
    defaults = dict(
        name="test-app",
        components=[
            Component(name="api", role=ComponentRole.WEB_API),
            Component(name="db", role=ComponentRole.PRIMARY_DATASTORE),
        ],
    )
    defaults.update(overrides)
    return InfraContract(**defaults)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_valid_contract(self):
        c = make_contract()
        assert c.name == "test-app"
        assert len(c.components) == 2

    def test_duplicate_component_names_rejected(self):
        with pytest.raises(ValidationError, match="Duplicate component names"):
            InfraContract(
                name="bad",
                components=[
                    Component(name="api", role=ComponentRole.WEB_API),
                    Component(name="api", role=ComponentRole.WORKER),
                ],
            )

    def test_depends_on_unknown_component_rejected(self):
        with pytest.raises(ValidationError, match="depends_on unknown components"):
            InfraContract(
                name="bad",
                components=[
                    Component(name="api", role=ComponentRole.WEB_API, depends_on=["ghost"]),
                ],
            )

    def test_self_dependency_rejected(self):
        with pytest.raises(ValidationError, match="cannot depend on itself"):
            Component(name="api", role=ComponentRole.WEB_API, depends_on=["api"])

    def test_component_name_pattern(self):
        with pytest.raises(ValidationError):
            Component(name="My API", role=ComponentRole.WEB_API)

    def test_duplicate_invariant_names_rejected(self):
        with pytest.raises(ValidationError, match="Duplicate invariant names"):
            make_contract(invariants=[
                Invariant(name="rule-a", description="first", rule="x"),
                Invariant(name="rule-a", description="second", rule="y"),
            ])

    def test_empty_components_rejected(self):
        with pytest.raises(ValidationError):
            InfraContract(name="empty", components=[])


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_yaml_round_trip(self):
        original = make_contract()
        yaml_str = contract_to_yaml(original)
        restored = contract_from_yaml(yaml_str)
        assert restored.name == original.name
        assert restored.id == original.id
        assert len(restored.components) == len(original.components)
        assert restored.components[0].role == original.components[0].role

    def test_yaml_uses_string_values_not_python_objects(self):
        yaml_str = contract_to_yaml(make_contract())
        assert "web_api" in yaml_str
        assert "!!python" not in yaml_str

    def test_invalid_yaml_raises(self):
        with pytest.raises(Exception):
            contract_from_yaml("not: a: valid: contract")


# ---------------------------------------------------------------------------
# Constraint validation
# ---------------------------------------------------------------------------

class TestConstraintValidation:
    def test_clean_contract_has_only_human_reviewed_warning(self):
        c = make_contract(human_reviewed=False)
        result = validate_contract(c)
        assert result.passed
        assert len(result.errors) == 0
        rules = {v.rule for v in result.warnings}
        assert "governance.human_reviewed" in rules

    def test_human_reviewed_suppresses_warning(self):
        c = make_contract(human_reviewed=True)
        result = validate_contract(c)
        rules = {v.rule for v in result.warnings}
        assert "governance.human_reviewed" not in rules

    def test_public_datastore_is_error(self):
        c = InfraContract(
            name="bad-app",
            components=[
                Component(
                    name="db",
                    role=ComponentRole.PRIMARY_DATASTORE,
                    requirements=ComponentRequirements(publicly_accessible=True),
                ),
            ],
        )
        result = validate_contract(c)
        assert not result.passed
        assert any(v.rule == "security.no_public_datastores" for v in result.errors)

    def test_public_datastore_allowed_when_constraint_disabled(self):
        c = InfraContract(
            name="ok-app",
            components=[
                Component(
                    name="db",
                    role=ComponentRole.PRIMARY_DATASTORE,
                    requirements=ComponentRequirements(publicly_accessible=True),
                ),
            ],
            constraints=Constraints(
                security=SecurityConstraint(no_public_datastores=False)
            ),
        )
        result = validate_contract(c)
        assert not any(v.rule == "security.no_public_datastores" for v in result.errors)

    def test_availability_mismatch_warning(self):
        c = InfraContract(
            name="mixed-avail",
            components=[
                Component(
                    name="api",
                    role=ComponentRole.WEB_API,
                    requirements=ComponentRequirements(availability=Availability.HIGH),
                    depends_on=["db"],
                ),
                Component(
                    name="db",
                    role=ComponentRole.PRIMARY_DATASTORE,
                    requirements=ComponentRequirements(availability=Availability.STANDARD),
                ),
            ],
        )
        result = validate_contract(c)
        assert any(v.rule == "availability.dependency_tier" for v in result.warnings)

    def test_cost_alert_threshold_warning(self):
        from natural_iac.contract import CostConstraint
        c = InfraContract(
            name="cost-app",
            components=[Component(name="api", role=ComponentRole.WEB_API)],
            constraints=Constraints(
                cost=CostConstraint(max_monthly_usd=100.0, alert_threshold_usd=150.0)
            ),
        )
        result = validate_contract(c)
        assert any(v.rule == "cost.alert_threshold" for v in result.warnings)


# ---------------------------------------------------------------------------
# Invariant evaluation
# ---------------------------------------------------------------------------

class TestInvariants:
    def test_invariant_passes_for_private_datastore(self):
        c = InfraContract(
            name="secure-app",
            components=[
                Component(
                    name="db",
                    role=ComponentRole.PRIMARY_DATASTORE,
                    requirements=ComponentRequirements(publicly_accessible=False),
                ),
            ],
            invariants=[
                Invariant(
                    name="no-public-db",
                    description="DB must be private",
                    rule="no primary_datastore is publicly_accessible",
                    severity=InvariantSeverity.ERROR,
                )
            ],
        )
        result = validate_contract(c)
        assert not any(v.rule == "invariant.no-public-db" for v in result.violations)

    def test_invariant_fails_for_public_datastore_when_constraint_off(self):
        c = InfraContract(
            name="insecure-app",
            components=[
                Component(
                    name="db",
                    role=ComponentRole.PRIMARY_DATASTORE,
                    requirements=ComponentRequirements(publicly_accessible=True),
                ),
            ],
            constraints=Constraints(
                security=SecurityConstraint(no_public_datastores=False)
            ),
            invariants=[
                Invariant(
                    name="no-public-db",
                    description="DB must be private",
                    rule="no primary_datastore is publicly_accessible",
                    severity=InvariantSeverity.ERROR,
                )
            ],
        )
        result = validate_contract(c)
        assert any(v.rule == "invariant.no-public-db" for v in result.errors)

    def test_unknown_invariant_rule_is_skipped(self):
        c = make_contract(
            invariants=[
                Invariant(
                    name="custom-rule",
                    description="some future rule",
                    rule="datastores must be in private subnets",
                )
            ]
        )
        result = validate_contract(c)
        assert not any(v.rule == "invariant.custom-rule" for v in result.violations)
