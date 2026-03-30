"""Execution layer tests.

Renderer tests are pure unit tests — no subprocesses.
Backend tests mock asyncio.create_subprocess_exec.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from natural_iac.execution.state import ResourceState, State
from natural_iac.execution.terraform.backend import (
    TerraformBackend,
    _merge_tfstate,
    _parse_apply_summary,
)
from natural_iac.execution.terraform.renderer import (
    _hcl_value,
    render_plan,
    render_to_dir,
)
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


def make_resource(
    type_: str,
    logical_name: str,
    component: str = "api",
    depends_on: list[str] | None = None,
    tags: dict | None = None,
    **props,
) -> Resource:
    return Resource(
        id=Resource.make_id(type_, logical_name),
        type=type_,
        logical_name=logical_name,
        component=component,
        properties=props,
        depends_on=depends_on or [],
        tags=tags or {},
    )


def make_plan(resources: list[Resource], region: str = "us-east-1") -> ExecutionPlan:
    return ExecutionPlan(
        contract_id="contract-123",
        contract_name="test-app",
        provider=Provider.AWS,
        region=region,
        resources=resources,
        changes=[ResourceChange(resource_id=r.id, action=ChangeAction.CREATE) for r in resources],
    )


def make_state(contract_id: str = "contract-123") -> State:
    return State.empty(
        contract_id=contract_id,
        contract_name="test-app",
        provider="aws",
        region="us-east-1",
    )


# ---------------------------------------------------------------------------
# HCL value serialisation
# ---------------------------------------------------------------------------


class TestHclValue:
    def test_none(self):
        assert _hcl_value(None) == "null"

    def test_bool_true(self):
        assert _hcl_value(True) == "true"

    def test_bool_false(self):
        assert _hcl_value(False) == "false"

    def test_integer(self):
        assert _hcl_value(42) == "42"

    def test_float(self):
        assert _hcl_value(3.14) == "3.14"

    def test_plain_string_quoted(self):
        assert _hcl_value("hello") == '"hello"'

    def test_string_with_quotes_escaped(self):
        assert _hcl_value('say "hi"') == '"say \\"hi\\""'

    def test_tf_reference_unquoted(self):
        assert _hcl_value("${aws_vpc.main.id}") == "aws_vpc.main.id"

    def test_tf_reference_partial_no_match(self):
        # Only strips if the whole string is ${...}
        assert _hcl_value("prefix-${aws_vpc.main.id}") == '"prefix-${aws_vpc.main.id}"'

    def test_empty_list(self):
        assert _hcl_value([]) == "[]"

    def test_scalar_list_oneliner(self):
        result = _hcl_value(["a", "b", "c"])
        assert result == '["a", "b", "c"]'

    def test_empty_map(self):
        assert _hcl_value({}) == "{}"

    def test_nested_map(self):
        result = _hcl_value({"key": "value"})
        assert "key" in result
        assert '"value"' in result

    def test_bool_in_list(self):
        result = _hcl_value([True, False])
        assert "true" in result
        assert "false" in result


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class TestRenderer:
    def test_renders_two_files(self):
        plan = make_plan([make_resource("aws_vpc", "main", component="shared")])
        files = render_plan(plan)
        assert "providers.tf" in files
        assert "main.tf" in files

    def test_providers_tf_contains_region(self):
        plan = make_plan([], region="eu-west-1")
        hcl = render_plan(plan)["providers.tf"]
        assert 'region = "eu-west-1"' in hcl

    def test_providers_tf_pins_aws_provider(self):
        plan = make_plan([])
        hcl = render_plan(plan)["providers.tf"]
        assert "hashicorp/aws" in hcl
        assert "~> 5.0" in hcl

    def test_resource_block_present(self):
        plan = make_plan([make_resource("aws_vpc", "main", component="shared")])
        hcl = render_plan(plan)["main.tf"]
        assert 'resource "aws_vpc" "main"' in hcl

    def test_string_property_quoted(self):
        plan = make_plan([make_resource("aws_vpc", "main", cidr_block="10.0.0.0/16")])
        hcl = render_plan(plan)["main.tf"]
        assert 'cidr_block = "10.0.0.0/16"' in hcl

    def test_bool_property_unquoted(self):
        plan = make_plan([
            make_resource("aws_db_instance", "db", component="db",
                          storage_encrypted=True, publicly_accessible=False)
        ])
        hcl = render_plan(plan)["main.tf"]
        assert "storage_encrypted = true" in hcl
        assert "publicly_accessible = false" in hcl

    def test_int_property_unquoted(self):
        plan = make_plan([
            make_resource("aws_ecs_service", "api", desired_count=2)
        ])
        hcl = render_plan(plan)["main.tf"]
        assert "desired_count = 2" in hcl

    def test_tf_reference_in_property_unquoted(self):
        plan = make_plan([
            make_resource("aws_ecs_service", "api",
                          cluster="${aws_ecs_cluster.main.id}")
        ])
        hcl = render_plan(plan)["main.tf"]
        assert "cluster = aws_ecs_cluster.main.id" in hcl
        # Must NOT appear quoted
        assert 'cluster = "aws_ecs_cluster.main.id"' not in hcl

    def test_tags_rendered(self):
        plan = make_plan([
            make_resource("aws_vpc", "main", tags={"managed_by": "natural-iac", "env": "prod"})
        ])
        hcl = render_plan(plan)["main.tf"]
        assert "tags = {" in hcl
        assert 'managed_by = "natural-iac"' in hcl
        assert 'env = "prod"' in hcl

    def test_tags_in_properties_merged_with_resource_tags(self):
        """tags in Resource.properties and Resource.tags should be merged; Resource.tags wins."""
        resource = Resource(
            id="aws_vpc.main",
            type="aws_vpc",
            logical_name="main",
            component="shared",
            properties={"tags": {"env": "staging", "owner": "team-a"}},
            tags={"env": "prod", "managed_by": "natural-iac"},  # wins on 'env'
        )
        plan = make_plan([resource])
        hcl = render_plan(plan)["main.tf"]
        assert 'env = "prod"' in hcl          # Resource.tags wins
        assert 'owner = "team-a"' in hcl       # from properties.tags
        assert 'managed_by = "natural-iac"' in hcl
        # Must not appear twice
        assert hcl.count('env = "prod"') == 1

    def test_depends_on_rendered_as_references(self):
        resource = make_resource(
            "aws_ecs_service", "api",
            depends_on=["aws_ecs_cluster.main", "aws_iam_role.task_exec"],
        )
        plan = make_plan([resource])
        hcl = render_plan(plan)["main.tf"]
        assert "depends_on = [aws_ecs_cluster.main, aws_iam_role.task_exec]" in hcl
        # Must NOT be quoted
        assert 'depends_on = ["aws_ecs_cluster.main"' not in hcl

    def test_no_depends_on_block_when_empty(self):
        plan = make_plan([make_resource("aws_vpc", "main")])
        hcl = render_plan(plan)["main.tf"]
        assert "depends_on" not in hcl

    def test_multiple_resources_all_present(self):
        plan = make_plan([
            make_resource("aws_vpc", "main", component="shared"),
            make_resource("aws_ecs_cluster", "main", component="shared"),
            make_resource("aws_ecs_service", "api"),
        ])
        hcl = render_plan(plan)["main.tf"]
        assert 'resource "aws_vpc" "main"' in hcl
        assert 'resource "aws_ecs_cluster" "main"' in hcl
        assert 'resource "aws_ecs_service" "api"' in hcl

    def test_render_to_dir_writes_files(self, tmp_path):
        plan = make_plan([make_resource("aws_vpc", "main")])
        written = render_to_dir(plan, tmp_path)
        assert len(written) == 2
        assert (tmp_path / "providers.tf").exists()
        assert (tmp_path / "main.tf").exists()

    def test_render_to_dir_creates_directory(self, tmp_path):
        plan = make_plan([make_resource("aws_vpc", "main")])
        target = tmp_path / "subdir" / "nested"
        render_to_dir(plan, target)
        assert target.exists()

    def test_contract_name_in_comment(self):
        plan = make_plan([])
        hcl = render_plan(plan)["main.tf"]
        assert "test-app" in hcl

    def test_nested_map_property(self):
        plan = make_plan([
            make_resource("aws_ecs_task_definition", "api",
                          environment={"KEY": "VALUE", "DEBUG": "false"})
        ])
        hcl = render_plan(plan)["main.tf"]
        assert "environment = {" in hcl
        assert '"KEY"' in hcl or "KEY" in hcl

    def test_data_source_renders_data_block(self):
        data_res = Resource(
            id="aws_vpc.main-vpc",
            type="aws_vpc",
            logical_name="main-vpc",
            component="shared",
            kind=ResourceKind.DATA,
            properties={"id": "vpc-0abc1234"},
        )
        plan = make_plan([data_res])
        hcl = render_plan(plan)["main.tf"]
        assert 'data "aws_vpc" "main-vpc"' in hcl
        assert 'resource "aws_vpc"' not in hcl
        assert 'id = "vpc-0abc1234"' in hcl

    def test_data_source_has_no_tags_block(self):
        data_res = Resource(
            id="aws_vpc.main-vpc",
            type="aws_vpc",
            logical_name="main-vpc",
            component="shared",
            kind=ResourceKind.DATA,
            properties={"id": "vpc-0abc1234"},
            tags={"managed_by": "natural-iac"},  # should be ignored for data sources
        )
        plan = make_plan([data_res])
        hcl = render_plan(plan)["main.tf"]
        assert "tags" not in hcl

    def test_data_sources_rendered_before_managed_resources(self):
        data_res = Resource(
            id="aws_vpc.main-vpc",
            type="aws_vpc",
            logical_name="main-vpc",
            component="shared",
            kind=ResourceKind.DATA,
            properties={"id": "vpc-0abc1234"},
        )
        managed_res = make_resource(
            "aws_instance", "webserver",
            vpc_security_group_ids="${data.aws_vpc.main-vpc.id}",
        )
        plan = make_plan([managed_res, data_res])  # deliberately reversed order
        hcl = render_plan(plan)["main.tf"]
        data_pos = hcl.index('data "aws_vpc"')
        resource_pos = hcl.index('resource "aws_instance"')
        assert data_pos < resource_pos

    def test_data_source_reference_renders_as_tf_expression(self):
        managed_res = make_resource(
            "aws_instance", "webserver",
            subnet_id="${data.aws_subnet.app_subnet.id}",
        )
        plan = make_plan([managed_res])
        hcl = render_plan(plan)["main.tf"]
        assert "subnet_id = data.aws_subnet.app_subnet.id" in hcl
        assert 'subnet_id = "data.aws_subnet' not in hcl


# ---------------------------------------------------------------------------
# tfstate parsing
# ---------------------------------------------------------------------------


class TestTfstateParsing:
    def _make_tfstate(self, resources: list[dict]) -> dict:
        return {"version": 4, "resources": resources}

    def _make_tf_resource(
        self, type_: str, name: str, attrs: dict
    ) -> dict:
        return {
            "type": type_,
            "name": name,
            "mode": "managed",
            "instances": [{"attributes": attrs}],
        }

    def test_basic_resource_extracted(self):
        tfstate = self._make_tfstate([
            self._make_tf_resource("aws_vpc", "main", {"id": "vpc-0abc", "cidr_block": "10.0.0.0/16"}),
        ])
        plan = make_plan([make_resource("aws_vpc", "main", component="shared")])
        state = make_state()

        result = _merge_tfstate(tfstate, state, plan)

        assert "aws_vpc.main" in result.resources
        rs = result.resources["aws_vpc.main"]
        assert rs.provider_id == "vpc-0abc"
        assert rs.attributes["cidr_block"] == "10.0.0.0/16"

    def test_arn_used_as_provider_id_when_no_id(self):
        tfstate = self._make_tfstate([
            self._make_tf_resource("aws_iam_role", "task_exec", {"arn": "arn:aws:iam::123:role/exec"}),
        ])
        plan = make_plan([make_resource("aws_iam_role", "task_exec")])
        state = make_state()

        result = _merge_tfstate(tfstate, state, plan)

        assert result.resources["aws_iam_role.task_exec"].provider_id == "arn:aws:iam::123:role/exec"

    def test_empty_tfstate_leaves_state_unchanged(self):
        state = make_state()
        plan = make_plan([])
        result = _merge_tfstate({}, state, plan)
        assert result.resources == {}

    def test_multiple_resources_merged(self):
        tfstate = self._make_tfstate([
            self._make_tf_resource("aws_vpc", "main", {"id": "vpc-1"}),
            self._make_tf_resource("aws_ecs_cluster", "main", {"id": "cluster-1"}),
        ])
        plan = make_plan([
            make_resource("aws_vpc", "main", component="shared"),
            make_resource("aws_ecs_cluster", "main", component="shared"),
        ])
        state = make_state()

        result = _merge_tfstate(tfstate, state, plan)

        assert len(result.resources) == 2

    def test_component_set_from_plan(self):
        tfstate = self._make_tfstate([
            self._make_tf_resource("aws_db_instance", "db", {"id": "mydb"}),
        ])
        plan = make_plan([make_resource("aws_db_instance", "db", component="db")])
        state = make_state()

        result = _merge_tfstate(tfstate, state, plan)

        assert result.resources["aws_db_instance.db"].component == "db"


# ---------------------------------------------------------------------------
# Apply output parsing
# ---------------------------------------------------------------------------


class TestApplySummaryParsing:
    def test_creation_complete(self):
        output = textwrap.dedent("""\
            aws_vpc.main: Creating...
            aws_vpc.main: Creation complete after 2s [id=vpc-0abc]
            aws_ecs_cluster.main: Creating...
            aws_ecs_cluster.main: Creation complete after 1s [id=cluster-1]
        """)
        created, updated, destroyed = _parse_apply_summary(output)
        assert "aws_vpc.main" in created
        assert "aws_ecs_cluster.main" in created
        assert updated == []
        assert destroyed == []

    def test_destruction_complete(self):
        output = "aws_vpc.main: Destruction complete after 1s\n"
        created, updated, destroyed = _parse_apply_summary(output)
        assert "aws_vpc.main" in destroyed
        assert created == []

    def test_empty_output(self):
        created, updated, destroyed = _parse_apply_summary("")
        assert created == updated == destroyed == []


# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------


class TestState:
    def test_upsert_and_get(self):
        state = make_state()
        rs = ResourceState(
            id="aws_vpc.main",
            type="aws_vpc",
            logical_name="main",
            component="shared",
            provider_id="vpc-0abc",
        )
        state.upsert(rs)
        assert state.get("aws_vpc.main") is not None
        assert state.get("aws_vpc.main").provider_id == "vpc-0abc"

    def test_remove(self):
        state = make_state()
        rs = ResourceState(id="aws_vpc.main", type="aws_vpc", logical_name="main", component="shared")
        state.upsert(rs)
        state.remove("aws_vpc.main")
        assert state.get("aws_vpc.main") is None

    def test_remove_nonexistent_is_noop(self):
        state = make_state()
        state.remove("does_not_exist.foo")  # should not raise

    def test_save_and_load(self, tmp_path):
        state = make_state()
        state.upsert(ResourceState(
            id="aws_vpc.main", type="aws_vpc", logical_name="main",
            component="shared", provider_id="vpc-0abc",
        ))
        path = tmp_path / "state.json"
        state.save(path)

        loaded = State.load(path)
        assert loaded.contract_id == "contract-123"
        assert loaded.resources["aws_vpc.main"].provider_id == "vpc-0abc"

    def test_save_creates_parent_dirs(self, tmp_path):
        state = make_state()
        path = tmp_path / "deep" / "nested" / "state.json"
        state.save(path)
        assert path.exists()


# ---------------------------------------------------------------------------
# TerraformBackend (mocked subprocess)
# ---------------------------------------------------------------------------


class TestTerraformBackend:
    def _make_proc(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(
            stdout.encode("utf-8"),
            stderr.encode("utf-8"),
        ))
        return proc

    def _apply_output(self, *resource_names: str) -> str:
        lines = []
        for name in resource_names:
            lines.append(f"{name}: Creating...")
            lines.append(f"{name}: Creation complete after 1s [id=some-id]")
        lines.append("")
        lines.append("Apply complete! Resources: 2 added, 0 changed, 0 destroyed.")
        return "\n".join(lines)

    @pytest.mark.asyncio
    async def test_apply_runs_init_then_apply(self, tmp_path):
        plan = make_plan([make_resource("aws_vpc", "main", component="shared")])
        state = make_state()
        backend = TerraformBackend(work_dir=tmp_path)

        calls = []

        async def fake_exec(binary, *args, **kwargs):
            calls.append(list(args))
            stdout = ""
            if args[0] == "apply":
                stdout = self._apply_output("aws_vpc.main")
            proc = self._make_proc(stdout=stdout)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result, new_state = await backend.apply(plan, state, auto_approve=True)

        command_names = [c[0] for c in calls]
        assert "init" in command_names
        assert "apply" in command_names
        # init must come before apply
        assert command_names.index("init") < command_names.index("apply")

    @pytest.mark.asyncio
    async def test_apply_writes_hcl_files(self, tmp_path):
        plan = make_plan([make_resource("aws_vpc", "main", component="shared")])
        state = make_state()
        backend = TerraformBackend(work_dir=tmp_path)

        async def fake_exec(binary, *args, **kwargs):
            stdout = ""
            if args[0] == "apply":
                stdout = self._apply_output("aws_vpc.main")
            return self._make_proc(stdout=stdout)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await backend.apply(plan, state, auto_approve=True)

        assert (tmp_path / "providers.tf").exists()
        assert (tmp_path / "main.tf").exists()

    @pytest.mark.asyncio
    async def test_apply_auto_approve_flag_passed(self, tmp_path):
        plan = make_plan([])
        state = make_state()
        backend = TerraformBackend(work_dir=tmp_path)

        captured_args = {}

        async def fake_exec(binary, *args, **kwargs):
            if args[0] == "apply":
                captured_args["apply_args"] = list(args)
            return self._make_proc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await backend.apply(plan, state, auto_approve=True)

        assert "-auto-approve" in captured_args.get("apply_args", [])

    @pytest.mark.asyncio
    async def test_apply_failure_returns_unsuccessful_result(self, tmp_path):
        plan = make_plan([make_resource("aws_vpc", "main")])
        state = make_state()
        backend = TerraformBackend(work_dir=tmp_path)

        async def fake_exec(binary, *args, **kwargs):
            if args[0] == "init":
                return self._make_proc()
            return self._make_proc(stderr="Error: something went wrong", returncode=1)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result, _ = await backend.apply(plan, state, auto_approve=True)

        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_init_failure_raises(self, tmp_path):
        plan = make_plan([])
        state = make_state()
        backend = TerraformBackend(work_dir=tmp_path)

        async def fake_exec(binary, *args, **kwargs):
            return self._make_proc(stderr="Error: no network", returncode=1)

        from natural_iac.execution.terraform.backend import TerraformError
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with pytest.raises(TerraformError, match="terraform init failed"):
                await backend.apply(plan, state, auto_approve=True)
