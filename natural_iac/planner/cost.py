"""
Simple lookup-based cost estimator.

Produces LOW-confidence estimates from a table of rough monthly costs per
resource type and size. Infracost integration is a future enhancement —
this gets something useful in the plan output immediately.

All figures are USD/month, us-east-1 on-demand pricing, rounded to nearest $5.
"""

from __future__ import annotations

from .schema import CostConfidence, CostEstimate, CostLineItem, ExecutionPlan, Resource

# (resource_type, size_hint) → monthly USD
# size_hint is inferred from the resource's "instance_class" / "instance_type"
# property when present, otherwise defaults to the "small" column.
_COST_TABLE: dict[str, dict[str, float]] = {
    # ECS Fargate (per service, 0.25 vCPU / 0.5 GB baseline)
    "aws_ecs_service": {
        "micro": 5,
        "small": 15,
        "medium": 40,
        "large": 100,
        "xlarge": 250,
    },
    # RDS (db.t4g.* family)
    "aws_db_instance": {
        "micro": 15,
        "small": 30,
        "medium": 80,
        "large": 200,
        "xlarge": 500,
    },
    # ElastiCache (cache.t4g.*)
    "aws_elasticache_replication_group": {
        "micro": 15,
        "small": 25,
        "medium": 60,
        "large": 150,
        "xlarge": 400,
    },
    "aws_elasticache_cluster": {
        "micro": 10,
        "small": 20,
        "medium": 50,
        "large": 130,
        "xlarge": 350,
    },
    # SQS — effectively free at small scale
    "aws_sqs_queue": {
        "micro": 1,
        "small": 1,
        "medium": 5,
        "large": 20,
        "xlarge": 50,
    },
    # ALB
    "aws_lb": {"micro": 20, "small": 20, "medium": 25, "large": 30, "xlarge": 40},
    "aws_alb": {"micro": 20, "small": 20, "medium": 25, "large": 30, "xlarge": 40},
    # S3 — dominated by usage, not base cost
    "aws_s3_bucket": {"micro": 1, "small": 5, "medium": 20, "large": 80, "xlarge": 200},
    # CloudFront — estimate based on transfer
    "aws_cloudfront_distribution": {
        "micro": 5,
        "small": 20,
        "medium": 60,
        "large": 150,
        "xlarge": 400,
    },
    # Secrets Manager ($0.40/secret/month + API calls)
    "aws_secretsmanager_secret": {
        "micro": 1,
        "small": 1,
        "medium": 5,
        "large": 10,
        "xlarge": 20,
    },
    # MSK (Kafka) — expensive
    "aws_msk_cluster": {
        "micro": 200,
        "small": 300,
        "medium": 600,
        "large": 1200,
        "xlarge": 3000,
    },
    # ECS task definition — no direct cost
    "aws_ecs_task_definition": {"micro": 0, "small": 0, "medium": 0, "large": 0, "xlarge": 0},
    # Networking — negligible for estimates
    "aws_vpc": {"micro": 0, "small": 0, "medium": 0, "large": 0, "xlarge": 0},
    "aws_subnet": {"micro": 0, "small": 0, "medium": 0, "large": 0, "xlarge": 0},
    "aws_security_group": {"micro": 0, "small": 0, "medium": 0, "large": 0, "xlarge": 0},
    "aws_db_subnet_group": {"micro": 0, "small": 0, "medium": 0, "large": 0, "xlarge": 0},
    "aws_elasticache_subnet_group": {"micro": 0, "small": 0, "medium": 0, "large": 0, "xlarge": 0},
    "aws_ecs_cluster": {"micro": 0, "small": 0, "medium": 0, "large": 0, "xlarge": 0},
    "aws_iam_role": {"micro": 0, "small": 0, "medium": 0, "large": 0, "xlarge": 0},
    "aws_iam_role_policy_attachment": {"micro": 0, "small": 0, "medium": 0, "large": 0, "xlarge": 0},
    "aws_cloudwatch_log_group": {"micro": 1, "small": 2, "medium": 5, "large": 15, "xlarge": 40},
    "aws_lb_listener": {"micro": 0, "small": 0, "medium": 0, "large": 0, "xlarge": 0},
    "aws_lb_target_group": {"micro": 0, "small": 0, "medium": 0, "large": 0, "xlarge": 0},
}

_SIZE_KEYWORDS: dict[str, str] = {
    # RDS instance classes
    "t3.micro": "micro", "t4g.micro": "micro",
    "t3.small": "small", "t4g.small": "small",
    "t3.medium": "medium", "t4g.medium": "medium",
    "m5.large": "large", "m6g.large": "large",
    "m5.xlarge": "xlarge", "m6g.xlarge": "xlarge",
    # Cache node types
    "cache.t3.micro": "micro", "cache.t4g.micro": "micro",
    "cache.t3.small": "small", "cache.t4g.small": "small",
    "cache.t3.medium": "medium", "cache.t4g.medium": "medium",
    "cache.m5.large": "large", "cache.m6g.large": "large",
}


def _infer_size(resource: Resource) -> str:
    """Infer a size bucket from resource properties."""
    props = resource.properties
    for key in ("instance_class", "instance_type", "node_type"):
        val = props.get(key, "")
        if val in _SIZE_KEYWORDS:
            return _SIZE_KEYWORDS[val]
        # fallback: look for the word in the value
        for keyword in ("micro", "small", "medium", "large", "xlarge"):
            if keyword in str(val).lower():
                return keyword
    return "small"


def estimate_cost(plan: ExecutionPlan) -> CostEstimate:
    """Produce a rough monthly cost estimate from the resource graph.

    Confidence is always LOW — this is a sanity-check estimate, not a bill.
    """
    line_items: list[CostLineItem] = []
    total = 0.0

    for resource in plan.resources:
        costs = _COST_TABLE.get(resource.type)
        if costs is None:
            line_items.append(CostLineItem(
                resource_id=resource.id,
                resource_type=resource.type,
                monthly_usd=0.0,
                notes="unknown resource type — not costed",
            ))
            continue

        size = _infer_size(resource)
        amount = costs.get(size, costs.get("small", 0.0))
        total += amount

        if amount > 0:
            line_items.append(CostLineItem(
                resource_id=resource.id,
                resource_type=resource.type,
                monthly_usd=amount,
                notes=f"size={size}",
            ))

    # Sort by cost descending for readability
    line_items.sort(key=lambda x: x.monthly_usd, reverse=True)

    return CostEstimate(
        total_monthly_usd=round(total, 2),
        breakdown=line_items,
        confidence=CostConfidence.LOW,
    )
