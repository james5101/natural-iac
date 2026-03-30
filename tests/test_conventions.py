"""Conventions tests -- schema, naming, tags, module rendering, validator integration."""

from __future__ import annotations

import textwrap
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from natural_iac.conventions import ConventionProfile, ModuleOverride, NamingConfig, TagConfig
from natural_iac.conventions.schema import DefaultsConfig
from natural_iac.contract import (
    Component,
    ComponentRole,
    InfraContract,
    validate_contract,
)
from natural_iac.execution.terraform.renderer import render_plan
from natural_iac.planner.schema import (
    ChangeAction,
    ExecutionPlan,
    Provider,
    Resource,
    ResourceChange,
    ResourceKind,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_resource(type_: str, logical_name: str, component: str = "api", **props) -> Resource:
    return Resource(
        id=Resource.make_id(type_, logical_name),
        type=type_,
        logical_name=logical_name,
        component=component,
        properties=dict(props),
    )


def make_plan(resources: list[Resource]) -> ExecutionPlan:
    return ExecutionPlan(
        contract_id="c-123",
        contract_name="test-app",
        provider=Provider.AWS,
        region="us-east-1",
        resources=resources,
        changes=[
            ResourceChange(resource_id=r.id, action=ChangeAction.CREATE)
            for r in resources
            if r.kind == ResourceKind.RESOURCE
        ],
    )


def make_contract(**overrides) -> InfraContract:
    defaults = dict(
        name="test-app",
        components=[Component(name="api", role=ComponentRole.WEB_API)],
    )
    defaults.update(overrides)
    return InfraContract(**defaults)


def minimal_profile(**overrides) -> ConventionProfile:
    return ConventionProfile(**overrides)


# ---------------------------------------------------------------------------
# NamingConfig
# ---------------------------------------------------------------------------


class TestNamingConfig:
    def test_default_pattern(self):
        naming = NamingConfig()
        result = naming.apply("aws_instance", "webserver")
        assert result == "webserver-aws_instance"

    def test_pattern_with_variables(self):
        naming = NamingConfig(
            pattern="{env}-{component}-{type_short}",
            variables={"env": "prod"},
            type_short_map={"aws_instance": "ec2"},
        )
        assert naming.apply("aws_instance", "webserver") == "prod-webserver-ec2"

    def test_type_short_map_fallback(self):
        naming = NamingConfig(
            pattern="{component}-{type_short}",
            type_short_map={"aws_vpc": "vpc"},
        )
        # aws_instance not in map -- falls back to full type
        assert naming.apply("aws_instance", "api") == "api-aws_instance"

    def test_missing_variable_falls_back_gracefully(self):
        naming = NamingConfig(
            pattern="{env}-{component}-{type_short}",
            variables={},  # env missing
            type_short_map={"aws_instance": "ec2"},
        )
        result = naming.apply("aws_instance", "api")
        assert result == "api-ec2"  # falls back to component-type_short


# ---------------------------------------------------------------------------
# TagConfig
# ---------------------------------------------------------------------------


class TestTagConfig:
    def test_resolve_defaults_no_variables(self):
        tags = TagConfig(defaults={"ManagedBy": "natural-iac", "Env": "prod"})
        resolved = tags.resolve_defaults({})
        assert resolved == {"ManagedBy": "natural-iac", "Env": "prod"}

    def test_resolve_defaults_with_variable_substitution(self):
        tags = TagConfig(defaults={"Environment": "${env}", "Owner": "platform"})
        resolved = tags.resolve_defaults({"env": "staging"})
        assert resolved["Environment"] == "staging"
        assert resolved["Owner"] == "platform"

    def test_unresolved_placeholder_stays_as_is(self):
        tags = TagConfig(defaults={"CostCenter": "${cost_center}"})
        resolved = tags.resolve_defaults({})  # no variable provided
        assert resolved["CostCenter"] == "${cost_center}"


# ---------------------------------------------------------------------------
# DefaultsConfig
# ---------------------------------------------------------------------------


class TestDefaultsConfig:
    def test_apply_injects_org_defaults(self):
        defaults = DefaultsConfig.from_dict({
            "aws_db_instance": {"deletion_protection": True, "storage_encrypted": True}
        })
        props = {"instance_class": "db.t4g.small"}
        result = defaults.apply("aws_db_instance", props)
        assert result["deletion_protection"] is True
        assert result["storage_encrypted"] is True
        assert result["instance_class"] == "db.t4g.small"

    def test_conventions_win_over_existing_property(self):
        defaults = DefaultsConfig.from_dict({
            "aws_db_instance": {"deletion_protection": True}
        })
        # LLM set it to False -- conventions override
        props = {"deletion_protection": False}
        result = defaults.apply("aws_db_instance", props)
        assert result["deletion_protection"] is True

    def test_no_match_returns_properties_unchanged(self):
        defaults = DefaultsConfig.from_dict({"aws_db_instance": {"deletion_protection": True}})
        props = {"instance_type": "t3.small"}
        assert defaults.apply("aws_instance", props) == props


# ---------------------------------------------------------------------------
# ConventionProfile.load()
# ---------------------------------------------------------------------------


class TestConventionProfileLoad:
    def test_load_returns_none_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert ConventionProfile.load() is None

    def test_load_parses_full_yaml(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        niac_dir = tmp_path / ".niac"
        niac_dir.mkdir()
        (niac_dir / "conventions.yaml").write_text(textwrap.dedent("""\
            version: v1
            naming:
              pattern: "{env}-{component}-{type_short}"
              variables:
                env: prod
              type_short_map:
                aws_instance: ec2
            tags:
              required:
                - CostCenter
              defaults:
                ManagedBy: natural-iac
                Environment: "${env}"
            modules:
              - match: aws_db_instance
                source: "git::github.com/acme/tf-modules//rds?ref=v1.0"
                name_template: "rds_{component}"
                input_map:
                  instance_class: db_instance_class
                passthrough:
                  - deletion_protection
            defaults:
              aws_instance:
                ebs_optimized: true
        """), encoding="utf-8")

        profile = ConventionProfile.load()
        assert profile is not None
        assert profile.naming.pattern == "{env}-{component}-{type_short}"
        assert profile.naming.variables["env"] == "prod"
        assert profile.naming.type_short_map["aws_instance"] == "ec2"
        assert profile.tags.required == ["CostCenter"]
        assert profile.tags.defaults["ManagedBy"] == "natural-iac"
        assert len(profile.modules) == 1
        assert profile.modules[0].match == "aws_db_instance"
        assert profile.defaults.overrides["aws_instance"]["ebs_optimized"] is True

    def test_module_for_returns_none_for_unknown_type(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        profile = ConventionProfile()
        assert profile.module_for("aws_instance") is None

    def test_module_for_returns_correct_override(self):
        profile = ConventionProfile(
            modules=[
                ModuleOverride(match="aws_db_instance", source="git::example.com//rds"),
            ]
        )
        result = profile.module_for("aws_db_instance")
        assert result is not None
        assert result.source == "git::example.com//rds"


# ---------------------------------------------------------------------------
# Renderer: module blocks
# ---------------------------------------------------------------------------


class TestModuleRendering:
    def _profile_with_rds_module(self) -> ConventionProfile:
        return ConventionProfile(
            naming=NamingConfig(type_short_map={"aws_db_instance": "rds"}),
            modules=[
                ModuleOverride(
                    match="aws_db_instance",
                    source="git::github.com/acme/tf-modules//rds?ref=v3.0",
                    name_template="rds_{component}",
                    input_map={
                        "instance_class": "db_instance_class",
                        "storage_encrypted": "encryption_enabled",
                    },
                    passthrough=["deletion_protection", "engine_version"],
                )
            ],
        )

    def test_module_block_emitted_instead_of_resource(self):
        resource = make_resource(
            "aws_db_instance", "db", "db",
            instance_class="db.t4g.small",
            storage_encrypted=True,
        )
        plan = make_plan([resource])
        hcl = render_plan(plan, self._profile_with_rds_module())["main.tf"]

        assert 'module "rds_db"' in hcl
        assert 'source = "git::github.com/acme/tf-modules//rds?ref=v3.0"' in hcl
        assert 'resource "aws_db_instance"' not in hcl

    def test_input_map_translates_property_names(self):
        resource = make_resource(
            "aws_db_instance", "db", "db",
            instance_class="db.t4g.small",
            storage_encrypted=True,
        )
        plan = make_plan([resource])
        hcl = render_plan(plan, self._profile_with_rds_module())["main.tf"]

        assert "db_instance_class" in hcl
        assert "encryption_enabled" in hcl
        # Original property names must not appear as standalone assignments
        assert "  instance_class =" not in hcl
        assert "  storage_encrypted =" not in hcl

    def test_passthrough_properties_use_original_name(self):
        resource = make_resource(
            "aws_db_instance", "db", "db",
            deletion_protection=True,
            engine_version="15.4",
        )
        plan = make_plan([resource])
        hcl = render_plan(plan, self._profile_with_rds_module())["main.tf"]

        assert "deletion_protection = true" in hcl
        assert 'engine_version = "15.4"' in hcl

    def test_unmapped_properties_dropped_with_warning(self):
        resource = make_resource(
            "aws_db_instance", "db", "db",
            instance_class="db.t4g.small",
            monitoring_interval=60,      # not in input_map or passthrough
            apply_immediately=False,     # not in input_map or passthrough
        )
        plan = make_plan([resource])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            hcl = render_plan(plan, self._profile_with_rds_module())["main.tf"]

        assert any("monitoring_interval" in str(w.message) for w in caught)
        assert any("apply_immediately" in str(w.message) for w in caught)
        assert "monitoring_interval" not in hcl
        assert "apply_immediately" not in hcl

    def test_unmatched_resource_type_uses_resource_block(self):
        resource = make_resource("aws_vpc", "main", "shared", cidr_block="10.0.0.0/16")
        plan = make_plan([resource])
        hcl = render_plan(plan, self._profile_with_rds_module())["main.tf"]

        assert 'resource "aws_vpc" "main"' in hcl
        assert "module" not in hcl

    def test_module_tags_rendered(self):
        resource = Resource(
            id="aws_db_instance.db",
            type="aws_db_instance",
            logical_name="db",
            component="db",
            properties={"instance_class": "db.t4g.small"},
            tags={"managed_by": "natural-iac", "env": "prod"},
        )
        plan = make_plan([resource])
        hcl = render_plan(plan, self._profile_with_rds_module())["main.tf"]

        assert "tags = {" in hcl
        assert 'managed_by = "natural-iac"' in hcl


# ---------------------------------------------------------------------------
# Validator: required tags
# ---------------------------------------------------------------------------


class TestConventionValidation:
    def test_required_tag_covered_by_defaults_passes(self):
        profile = ConventionProfile(
            tags=TagConfig(
                required=["CostCenter"],
                defaults={"CostCenter": "platform-eng"},
            )
        )
        contract = make_contract()
        result = validate_contract(contract, conventions=profile)
        assert not any(v.rule == "conventions.required_tags" for v in result.violations)

    def test_required_tag_not_covered_is_error(self):
        profile = ConventionProfile(
            tags=TagConfig(
                required=["CostCenter"],
                defaults={"ManagedBy": "natural-iac"},  # CostCenter missing
            )
        )
        contract = make_contract()
        result = validate_contract(contract, conventions=profile)
        assert any(v.rule == "conventions.required_tags" for v in result.errors)

    def test_required_tag_covered_by_contract_metadata_passes(self):
        profile = ConventionProfile(
            tags=TagConfig(required=["CostCenter"])
        )
        contract = make_contract(metadata={"tags": {"CostCenter": "data-team"}})
        result = validate_contract(contract, conventions=profile)
        assert not any(v.rule == "conventions.required_tags" for v in result.violations)

    def test_no_conventions_skips_tag_check(self):
        contract = make_contract()
        result = validate_contract(contract, conventions=None)
        assert not any(v.rule == "conventions.required_tags" for v in result.violations)

    def test_multiple_missing_required_tags_single_violation(self):
        profile = ConventionProfile(
            tags=TagConfig(required=["CostCenter", "Owner", "Team"])
        )
        contract = make_contract()
        result = validate_contract(contract, conventions=profile)
        errors = [v for v in result.errors if v.rule == "conventions.required_tags"]
        assert len(errors) == 1  # one violation listing all missing tags


# ---------------------------------------------------------------------------
# Planner: naming + defaults applied
# ---------------------------------------------------------------------------


class TestPlannerConventions:
    @pytest.mark.asyncio
    async def test_logical_name_post_processed(self):
        from unittest.mock import AsyncMock, MagicMock
        from natural_iac.planner import PlannerAgent

        profile = ConventionProfile(
            naming=NamingConfig(
                pattern="{env}-{component}-{type_short}",
                variables={"env": "prod"},
                type_short_map={"aws_instance": "ec2"},
            )
        )

        mock_client = MagicMock()
        mock_client.messages = MagicMock()

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "emit_plan"
        tool_block.id = "tu_abc"
        tool_block.input = {
            "region": "us-east-1",
            "resources": [{
                "type": "aws_instance",
                "logical_name": "webserver",  # LLM chose its own name
                "component": "api",
                "properties": {"instance_type": "t3.small"},
            }],
        }
        resp = MagicMock()
        resp.content = [tool_block]
        resp.stop_reason = "tool_use"
        mock_client.messages.create = AsyncMock(return_value=resp)

        agent = PlannerAgent(client=mock_client)
        contract = make_contract()
        plan, _ = await agent.plan(contract, conventions=profile)

        assert len(plan.resources) == 1
        assert plan.resources[0].logical_name == "prod-api-ec2"

    @pytest.mark.asyncio
    async def test_org_defaults_applied_to_properties(self):
        from natural_iac.planner import PlannerAgent

        profile = ConventionProfile(
            defaults=DefaultsConfig.from_dict({
                "aws_db_instance": {"deletion_protection": True}
            })
        )

        mock_client = MagicMock()
        mock_client.messages = MagicMock()

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "emit_plan"
        tool_block.id = "tu_abc"
        tool_block.input = {
            "region": "us-east-1",
            "resources": [{
                "type": "aws_db_instance",
                "logical_name": "db",
                "component": "db",
                "properties": {"deletion_protection": False},  # LLM set false
            }],
        }
        resp = MagicMock()
        resp.content = [tool_block]
        resp.stop_reason = "tool_use"
        mock_client.messages.create = AsyncMock(return_value=resp)

        agent = PlannerAgent(client=mock_client)
        contract = make_contract(
            components=[
                Component(name="api", role=ComponentRole.WEB_API),
                Component(name="db", role=ComponentRole.PRIMARY_DATASTORE),
            ]
        )
        plan, _ = await agent.plan(contract, conventions=profile)

        db = plan.resource_map["aws_db_instance.db"]
        assert db.properties["deletion_protection"] is True  # conventions won

    @pytest.mark.asyncio
    async def test_convention_tags_injected_into_resources(self):
        from natural_iac.planner import PlannerAgent

        profile = ConventionProfile(
            tags=TagConfig(defaults={"CostCenter": "platform-eng", "Owner": "infra-team"})
        )

        mock_client = MagicMock()
        mock_client.messages = MagicMock()

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "emit_plan"
        tool_block.id = "tu_abc"
        tool_block.input = {
            "region": "us-east-1",
            "resources": [{
                "type": "aws_instance",
                "logical_name": "api",
                "component": "api",
                "properties": {},
            }],
        }
        resp = MagicMock()
        resp.content = [tool_block]
        resp.stop_reason = "tool_use"
        mock_client.messages.create = AsyncMock(return_value=resp)

        agent = PlannerAgent(client=mock_client)
        plan, _ = await agent.plan(make_contract(), conventions=profile)

        resource = plan.resources[0]
        assert resource.tags["CostCenter"] == "platform-eng"
        assert resource.tags["Owner"] == "infra-team"
        assert resource.tags["managed_by"] == "natural-iac"  # still present
