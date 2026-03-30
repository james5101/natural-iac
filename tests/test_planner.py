"""Planner tests — Claude calls are mocked."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from natural_iac.contract import (
    Availability,
    Component,
    ComponentRequirements,
    ComponentRole,
    Constraints,
    InfraContract,
    SecurityConstraint,
)
from natural_iac.planner import (
    ChangeAction,
    CostConfidence,
    ExecutionPlan,
    PlannerAgent,
    Provider,
    Resource,
    estimate_cost,
)
from natural_iac.planner.schema import CostLineItem


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def make_contract(**overrides) -> InfraContract:
    defaults = dict(
        name="test-app",
        components=[
            Component(name="api", role=ComponentRole.WEB_API),
            Component(name="db", role=ComponentRole.PRIMARY_DATASTORE),
        ],
        constraints=Constraints(
            security=SecurityConstraint(allowed_regions=["us-east-1"])
        ),
    )
    defaults.update(overrides)
    return InfraContract(**defaults)


def make_resource(type_: str, logical_name: str, component: str = "api", **props) -> Resource:
    return Resource(
        id=Resource.make_id(type_, logical_name),
        type=type_,
        logical_name=logical_name,
        component=component,
        properties=props,
    )


def _tool_use_block(name: str, input_data: dict, block_id: str | None = None):
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.id = block_id or f"tu_{uuid.uuid4().hex[:8]}"
    block.input = input_data
    return block


def _api_response(*blocks, stop_reason: str = "tool_use"):
    resp = MagicMock()
    resp.content = list(blocks)
    resp.stop_reason = stop_reason
    return resp


def _minimal_plan_payload(region: str = "us-east-1") -> dict:
    return {
        "region": region,
        "resources": [
            {
                "type": "aws_vpc",
                "logical_name": "main",
                "component": "shared",
                "properties": {"cidr_block": "10.0.0.0/16"},
            },
            {
                "type": "aws_ecs_cluster",
                "logical_name": "main",
                "component": "shared",
                "properties": {"name": "test-app"},
            },
            {
                "type": "aws_ecs_service",
                "logical_name": "api",
                "component": "api",
                "properties": {"desired_count": 1},
                "depends_on_logical": ["aws_ecs_cluster.main"],
            },
            {
                "type": "aws_db_instance",
                "logical_name": "db",
                "component": "db",
                "properties": {
                    "instance_class": "db.t4g.small",
                    "storage_encrypted": True,
                    "publicly_accessible": False,
                },
                "depends_on_logical": [],
            },
        ],
    }


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock()
    return client


@pytest.fixture
def agent(mock_client):
    return PlannerAgent(client=mock_client, model="claude-test")


# ---------------------------------------------------------------------------
# PlannerAgent
# ---------------------------------------------------------------------------


class TestPlannerAgent:
    @pytest.mark.asyncio
    async def test_returns_execution_plan(self, agent, mock_client):
        mock_client.messages.create.return_value = _api_response(
            _tool_use_block("emit_plan", _minimal_plan_payload())
        )

        plan, trace = await agent.plan(make_contract())

        assert isinstance(plan, ExecutionPlan)
        assert plan.contract_name == "test-app"

    @pytest.mark.asyncio
    async def test_resources_parsed(self, agent, mock_client):
        mock_client.messages.create.return_value = _api_response(
            _tool_use_block("emit_plan", _minimal_plan_payload())
        )

        plan, _ = await agent.plan(make_contract())

        assert len(plan.resources) == 4
        ids = {r.id for r in plan.resources}
        assert "aws_ecs_service.api" in ids
        assert "aws_db_instance.db" in ids

    @pytest.mark.asyncio
    async def test_all_changes_are_create(self, agent, mock_client):
        mock_client.messages.create.return_value = _api_response(
            _tool_use_block("emit_plan", _minimal_plan_payload())
        )

        plan, _ = await agent.plan(make_contract())

        assert all(c.action == ChangeAction.CREATE for c in plan.changes)
        assert len(plan.changes) == len(plan.resources)

    @pytest.mark.asyncio
    async def test_region_from_contract_allowed_regions(self, agent, mock_client):
        mock_client.messages.create.return_value = _api_response(
            _tool_use_block("emit_plan", _minimal_plan_payload(region="eu-west-1"))
        )
        contract = make_contract(
            constraints=Constraints(
                security=SecurityConstraint(allowed_regions=["eu-west-1"])
            )
        )

        plan, _ = await agent.plan(contract)

        assert plan.region == "eu-west-1"

    @pytest.mark.asyncio
    async def test_region_defaults_to_us_east_1(self, agent, mock_client):
        payload = _minimal_plan_payload()
        payload["region"] = "us-east-1"
        mock_client.messages.create.return_value = _api_response(
            _tool_use_block("emit_plan", payload)
        )
        contract = make_contract(
            constraints=Constraints(
                security=SecurityConstraint(allowed_regions=[])
            )
        )

        plan, _ = await agent.plan(contract)

        assert plan.region == "us-east-1"

    @pytest.mark.asyncio
    async def test_tags_merged_with_contract_tags(self, agent, mock_client):
        payload = _minimal_plan_payload()
        # give the api component a tag
        payload["resources"][2]["tags"] = {"env": "prod"}
        mock_client.messages.create.return_value = _api_response(
            _tool_use_block("emit_plan", payload)
        )
        contract = make_contract(
            components=[
                Component(
                    name="api",
                    role=ComponentRole.WEB_API,
                    tags={"team": "platform"},
                ),
                Component(name="db", role=ComponentRole.PRIMARY_DATASTORE),
            ]
        )

        plan, _ = await agent.plan(contract)

        api_resource = plan.resource_map["aws_ecs_service.api"]
        assert api_resource.tags["managed_by"] == "natural-iac"
        assert api_resource.tags["contract"] == "test-app"
        assert api_resource.tags["team"] == "platform"
        assert api_resource.tags["env"] == "prod"

    @pytest.mark.asyncio
    async def test_cost_estimate_attached(self, agent, mock_client):
        mock_client.messages.create.return_value = _api_response(
            _tool_use_block("emit_plan", _minimal_plan_payload())
        )

        plan, _ = await agent.plan(make_contract())

        assert plan.cost_estimate is not None
        assert plan.cost_estimate.total_monthly_usd > 0

    @pytest.mark.asyncio
    async def test_trace_records_turns(self, agent, mock_client):
        mock_client.messages.create.return_value = _api_response(
            _tool_use_block("emit_plan", _minimal_plan_payload())
        )

        _, trace = await agent.plan(make_contract())

        assert len(trace.turns) == 2  # user + assistant
        assert trace.turns[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_no_tool_call_raises(self, agent, mock_client):
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Here is your plan."
        mock_client.messages.create.return_value = _api_response(
            text_block, stop_reason="end_turn"
        )

        with pytest.raises(RuntimeError, match="Planner did not emit a plan"):
            await agent.plan(make_contract())

    @pytest.mark.asyncio
    async def test_depends_on_preserved(self, agent, mock_client):
        mock_client.messages.create.return_value = _api_response(
            _tool_use_block("emit_plan", _minimal_plan_payload())
        )

        plan, _ = await agent.plan(make_contract())

        ecs_service = plan.resource_map["aws_ecs_service.api"]
        assert "aws_ecs_cluster.main" in ecs_service.depends_on


# ---------------------------------------------------------------------------
# Cost estimator
# ---------------------------------------------------------------------------


class TestCostEstimator:
    def _make_plan(self, resources: list[Resource]) -> ExecutionPlan:
        contract = make_contract()
        return ExecutionPlan(
            contract_id=contract.id,
            contract_name=contract.name,
            provider=Provider.AWS,
            region="us-east-1",
            resources=resources,
            changes=[],
        )

    def test_known_resources_costed(self):
        plan = self._make_plan([
            make_resource("aws_ecs_service", "api"),
            make_resource("aws_db_instance", "db", "db", instance_class="db.t4g.small"),
        ])

        result = estimate_cost(plan)

        assert result.total_monthly_usd > 0
        costed = {item.resource_id for item in result.breakdown}
        assert "aws_ecs_service.api" in costed
        assert "aws_db_instance.db" in costed

    def test_zero_cost_resources_excluded_from_breakdown(self):
        plan = self._make_plan([
            make_resource("aws_vpc", "main", "shared"),
            make_resource("aws_security_group", "api", "api"),
            make_resource("aws_ecs_service", "api"),
        ])

        result = estimate_cost(plan)

        # VPC and SG have $0 cost — should not appear in breakdown
        ids = {item.resource_id for item in result.breakdown}
        assert "aws_vpc.main" not in ids
        assert "aws_security_group.api" not in ids

    def test_unknown_resource_type_gets_zero_cost_note(self):
        plan = self._make_plan([
            make_resource("aws_some_new_service", "thing", "api"),
        ])

        result = estimate_cost(plan)

        assert result.total_monthly_usd == 0.0
        assert any("unknown resource type" in item.notes for item in result.breakdown)

    def test_confidence_is_always_low(self):
        plan = self._make_plan([make_resource("aws_ecs_service", "api")])
        result = estimate_cost(plan)
        assert result.confidence == CostConfidence.LOW

    def test_breakdown_sorted_by_cost_descending(self):
        plan = self._make_plan([
            make_resource("aws_ecs_service", "api"),  # ~$15
            make_resource("aws_db_instance", "db", "db", instance_class="db.m6g.large"),  # ~$200
            make_resource("aws_sqs_queue", "jobs", "jobs"),  # ~$1
        ])

        result = estimate_cost(plan)
        costs = [item.monthly_usd for item in result.breakdown]

        assert costs == sorted(costs, reverse=True)

    def test_size_hint_inferred_from_instance_class(self):
        plan = self._make_plan([
            make_resource("aws_db_instance", "db", "db", instance_class="db.m6g.large"),
        ])
        result = estimate_cost(plan)
        db_item = next(i for i in result.breakdown if i.resource_id == "aws_db_instance.db")
        assert db_item.monthly_usd == 200  # large tier

    def test_total_is_sum_of_breakdown(self):
        plan = self._make_plan([
            make_resource("aws_ecs_service", "api"),
            make_resource("aws_db_instance", "db", "db"),
            make_resource("aws_elasticache_replication_group", "cache", "cache"),
        ])
        result = estimate_cost(plan)
        expected = sum(item.monthly_usd for item in result.breakdown)
        assert abs(result.total_monthly_usd - expected) < 0.01
