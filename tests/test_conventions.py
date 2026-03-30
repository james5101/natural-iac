"""Conventions tests -- schema, naming, tags, module rendering, validator integration."""

from __future__ import annotations

import textwrap
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from natural_iac.conventions import ConventionProfile, ModuleOverride, ModuleVariable, NamingConfig, TagConfig
from natural_iac.conventions.module_reader import (
    _github_raw_variables_url,
    _parse_variables_tf,
    clear_cache,
    fetch_module_variables,
    format_variables_for_prompt,
)
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


# ---------------------------------------------------------------------------
# Module reader: URL parsing
# ---------------------------------------------------------------------------


class TestGithubRawVariablesUrl:
    def test_git_prefix_with_ref(self):
        url, host = _github_raw_variables_url(
            "git::https://github.com/acme/tf-modules.git//rds?ref=v3.1.0"
        )
        assert url == "https://raw.githubusercontent.com/acme/tf-modules/v3.1.0/rds/variables.tf"
        assert host == "github.com"

    def test_git_prefix_no_https(self):
        url, host = _github_raw_variables_url(
            "git::github.com/acme/tf-modules//rds?ref=v1.0"
        )
        assert url == "https://raw.githubusercontent.com/acme/tf-modules/v1.0/rds/variables.tf"

    def test_no_subdir(self):
        url, _ = _github_raw_variables_url("git::github.com/acme/tf-rds?ref=v2.0")
        assert url == "https://raw.githubusercontent.com/acme/tf-rds/v2.0/variables.tf"

    def test_llm_tree_url_format(self):
        url, _ = _github_raw_variables_url(
            "git::github.com/terraform-aws-modules/terraform-aws-rds/tree/v7.2.0"
        )
        assert url == "https://raw.githubusercontent.com/terraform-aws-modules/terraform-aws-rds/v7.2.0/variables.tf"

    def test_non_github_returns_none(self):
        url, host = _github_raw_variables_url("registry.terraform.io/hashicorp/consul/aws")
        assert url is None and host is None
        url, host = _github_raw_variables_url("./local/module")
        assert url is None and host is None

    def test_plain_github_no_ref_defaults_main(self):
        url, _ = _github_raw_variables_url("github.com/acme/tf-modules")
        assert url == "https://raw.githubusercontent.com/acme/tf-modules/main/variables.tf"

    # --- GitHub Enterprise ---

    def test_github_enterprise_hostname(self):
        url, host = _github_raw_variables_url(
            "git::github.mycompany.com/infra/tf-modules//rds?ref=v3.0"
        )
        assert url == "https://github.mycompany.com/infra/tf-modules/raw/v3.0/rds/variables.tf"
        assert host == "github.mycompany.com"

    def test_github_enterprise_no_subdir(self):
        url, host = _github_raw_variables_url(
            "git::github.acme.io/infra/tf-rds?ref=v1.5"
        )
        assert url == "https://github.acme.io/infra/tf-rds/raw/v1.5/variables.tf"
        assert host == "github.acme.io"


# ---------------------------------------------------------------------------
# Module reader: HCL parsing
# ---------------------------------------------------------------------------


SAMPLE_VARIABLES_TF = """\
variable "identifier" {
  description = "The name of the RDS instance"
  type        = string
}

variable "engine" {
  description = "The database engine"
  type        = string
  default     = "postgres"
}

variable "instance_class" {
  description = "The instance type of the RDS instance"
  type        = string
}

variable "db_name" {
  description = "The DB name. Note that this is not applicable for Oracle"
  type        = string
  default     = null
}

variable "allocated_storage" {
  description = "The allocated storage in gigabytes"
  type        = number
}

variable "storage_encrypted" {
  description = "Specifies whether the DB instance is encrypted"
  type        = bool
  default     = true
}
"""


class TestParseVariablesTf:
    def test_parses_all_variables(self):
        variables = _parse_variables_tf(SAMPLE_VARIABLES_TF)
        names = {v.name for v in variables}
        assert names == {
            "identifier", "engine", "instance_class", "db_name",
            "allocated_storage", "storage_encrypted",
        }

    def test_required_vs_optional(self):
        variables = _parse_variables_tf(SAMPLE_VARIABLES_TF)
        by_name = {v.name: v for v in variables}

        assert by_name["identifier"].required is True
        assert by_name["instance_class"].required is True
        assert by_name["allocated_storage"].required is True

        assert by_name["engine"].required is False
        assert by_name["db_name"].required is False
        assert by_name["storage_encrypted"].required is False

    def test_types_extracted(self):
        variables = _parse_variables_tf(SAMPLE_VARIABLES_TF)
        by_name = {v.name: v for v in variables}
        assert by_name["identifier"].type == "string"
        assert by_name["allocated_storage"].type == "number"
        assert by_name["storage_encrypted"].type == "bool"

    def test_descriptions_extracted(self):
        variables = _parse_variables_tf(SAMPLE_VARIABLES_TF)
        by_name = {v.name: v for v in variables}
        assert "RDS instance" in by_name["identifier"].description
        assert "database engine" in by_name["engine"].description

    def test_empty_file_returns_empty_list(self):
        assert _parse_variables_tf("") == []


# ---------------------------------------------------------------------------
# Module reader: fetch with mock
# ---------------------------------------------------------------------------


class TestFetchModuleVariables:
    def setup_method(self):
        clear_cache()

    def test_returns_empty_on_non_github_source(self):
        variables = fetch_module_variables("./local/module")
        assert variables == []

    def test_uses_cache_on_second_call(self, monkeypatch):
        call_count = 0

        def mock_fetch(url, token=None, timeout=5):
            nonlocal call_count
            call_count += 1
            return SAMPLE_VARIABLES_TF

        monkeypatch.setattr(
            "natural_iac.conventions.module_reader._fetch_url", mock_fetch
        )

        source = "git::github.com/acme/tf-rds?ref=v1.0"
        fetch_module_variables(source)
        fetch_module_variables(source)

        assert call_count == 1  # second call hit cache

    def test_returns_parsed_variables_on_success(self, monkeypatch):
        monkeypatch.setattr(
            "natural_iac.conventions.module_reader._fetch_url",
            lambda url, token=None, timeout=5: SAMPLE_VARIABLES_TF,
        )

        variables = fetch_module_variables("git::github.com/acme/tf-rds?ref=v1.0")
        assert len(variables) == 6
        names = {v.name for v in variables}
        assert "identifier" in names
        assert "db_name" in names

    def test_returns_empty_on_fetch_failure(self, monkeypatch):
        monkeypatch.setattr(
            "natural_iac.conventions.module_reader._fetch_url",
            lambda url, token=None, timeout=5: None,
        )
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            variables = fetch_module_variables("git::github.com/acme/tf-rds?ref=v1.0")
        assert variables == []

    def test_token_passed_to_fetch(self, monkeypatch):
        captured: dict = {}

        def mock_fetch(url, token=None, timeout=5):
            captured["token"] = token
            return SAMPLE_VARIABLES_TF

        monkeypatch.setenv("GITHUB_TOKEN", "ghp_testtoken123")
        monkeypatch.setattr("natural_iac.conventions.module_reader._fetch_url", mock_fetch)

        fetch_module_variables("git::github.com/acme/tf-rds?ref=v1.0")
        assert captured["token"] == "ghp_testtoken123"

    def test_gh_token_alias_accepted(self, monkeypatch):
        captured: dict = {}

        def mock_fetch(url, token=None, timeout=5):
            captured["token"] = token
            return SAMPLE_VARIABLES_TF

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "ghp_alias456")
        monkeypatch.setattr("natural_iac.conventions.module_reader._fetch_url", mock_fetch)

        fetch_module_variables("git::github.com/acme/tf-rds?ref=v1.0")
        assert captured["token"] == "ghp_alias456"

    def test_no_token_when_env_not_set(self, monkeypatch):
        captured: dict = {}

        def mock_fetch(url, token=None, timeout=5):
            captured["token"] = token
            return SAMPLE_VARIABLES_TF

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setattr("natural_iac.conventions.module_reader._fetch_url", mock_fetch)

        fetch_module_variables("git::github.com/acme/public-module?ref=v1.0")
        assert captured["token"] is None

    def test_local_path_takes_priority_over_network(self, tmp_path, monkeypatch):
        # Write a local variables.tf
        (tmp_path / "variables.tf").write_text(
            'variable "local_var" { type = string }', encoding="utf-8"
        )
        fetch_count = 0

        def mock_fetch(url, token=None, timeout=5):
            nonlocal fetch_count
            fetch_count += 1
            return SAMPLE_VARIABLES_TF  # would return different content

        monkeypatch.setattr("natural_iac.conventions.module_reader._fetch_url", mock_fetch)

        variables = fetch_module_variables(
            "git::github.com/acme/tf-rds?ref=v1.0",
            local_path=str(tmp_path),
        )

        assert fetch_count == 0  # network never called
        assert len(variables) == 1
        assert variables[0].name == "local_var"

    def test_fetch_failure_emits_actionable_warning(self, monkeypatch):
        monkeypatch.setattr(
            "natural_iac.conventions.module_reader._fetch_url",
            lambda url, token=None, timeout=5: None,
        )
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            fetch_module_variables("git::github.com/acme/private-module?ref=v1.0")

        assert len(caught) == 1
        msg = str(caught[0].message)
        assert "GITHUB_TOKEN" in msg
        assert "local_path" in msg

    def test_fetch_failure_with_token_gives_scope_hint(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_expired")
        monkeypatch.setattr(
            "natural_iac.conventions.module_reader._fetch_url",
            lambda url, token=None, timeout=5: None,
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            fetch_module_variables("git::github.com/acme/private-module?ref=v1.0")

        msg = str(caught[0].message)
        assert "scope" in msg.lower() or "token" in msg.lower()


# ---------------------------------------------------------------------------
# Renderer: module block with fetched variables
# ---------------------------------------------------------------------------


class TestModuleRenderingWithVariables:
    def _profile_and_vars(self):
        profile = ConventionProfile(
            naming=NamingConfig(type_short_map={"aws_db_instance": "rds"}),
            modules=[
                ModuleOverride(
                    match="aws_db_instance",
                    source="git::github.com/acme/tf-modules//rds?ref=v1.0",
                    name_template="rds_{component}",
                )
            ],
        )
        # Simulate fetched variables -- LLM has already used these names
        vars_ = [
            ModuleVariable("identifier", "string", "RDS instance name", required=True),
            ModuleVariable("instance_class", "string", "Instance type", required=True),
            ModuleVariable("db_name", "string", "Database name", required=False),
            ModuleVariable("storage_encrypted", "bool", "Enable encryption", required=False),
        ]
        return profile, vars_

    def test_known_variable_names_pass_through_directly(self):
        profile, vars_ = self._profile_and_vars()
        # LLM emitted the module's actual variable names
        resource = make_resource(
            "aws_db_instance", "db", "db",
            identifier="prod-db",
            instance_class="db.t4g.small",
            db_name="myapp",
            storage_encrypted=True,
        )
        plan = make_plan([resource])

        from natural_iac.execution.terraform.renderer import _render_module
        override = profile.modules[0]
        hcl = _render_module(resource, override, profile, vars_)

        assert 'identifier = "prod-db"' in hcl
        assert 'db_name = "myapp"' in hcl
        assert "instance_class" in hcl
        assert "storage_encrypted = true" in hcl

    def test_unknown_property_dropped_with_warning(self):
        profile, vars_ = self._profile_and_vars()
        resource = make_resource(
            "aws_db_instance", "db", "db",
            identifier="prod-db",
            unknown_prop="value",   # not in module variables
        )

        from natural_iac.execution.terraform.renderer import _render_module
        override = profile.modules[0]

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            hcl = _render_module(resource, override, profile, vars_)

        assert any("unknown_prop" in str(w.message) for w in caught)
        assert "unknown_prop" not in hcl

    def test_explicit_input_map_takes_priority_over_variable_names(self):
        profile = ConventionProfile(
            modules=[
                ModuleOverride(
                    match="aws_db_instance",
                    source="git::github.com/acme/tf-modules//rds?ref=v1.0",
                    name_template="rds_{component}",
                    input_map={"instance_class": "db_instance_class"},  # explicit override
                )
            ],
        )
        vars_ = [
            ModuleVariable("db_instance_class", "string", required=True),
            ModuleVariable("instance_class", "string", required=False),
        ]
        resource = make_resource(
            "aws_db_instance", "db", "db",
            instance_class="db.t4g.small",
        )

        from natural_iac.execution.terraform.renderer import _render_module
        override = profile.modules[0]
        hcl = _render_module(resource, override, profile, vars_)

        # Explicit input_map translated instance_class -> db_instance_class
        assert "db_instance_class" in hcl
        assert "  instance_class =" not in hcl
