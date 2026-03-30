# natural-iac

**Describe your infrastructure in plain English. Get production-ready Terraform.**

natural-iac replaces the write-HCL-by-hand workflow with a three-layer model powered by Claude:

```
"I need a SaaS API with Postgres, Redis, and an async job queue"
                          |
                    Intent Agent
                          |
              demo.contract.yaml  <-- committed to git
                          |
                    Planner Agent
                          |
              terraform-out/main.tf  <-- ready to apply
```

---

## Why

Writing Terraform forces you to think in cloud primitives (VPCs, security group rules, IAM trust policies) before you've expressed what you actually need. The gap between "I need a private database" and the 40 lines of HCL it requires is where mistakes, security misconfigurations, and cargo-culted boilerplate live.

natural-iac separates the **what** from the **how**:

- **Contract** -- what you need and the invariants that must hold, committed to git, provider-agnostic
- **Plan** -- how your cloud provider implements it, derived fresh each time
- **Execution** -- Terraform (or direct SDK) applies the plan

This separation has practical benefits:

| Problem with raw Terraform | natural-iac approach |
|---|---|
| Security misconfig requires reading HCL carefully | Contract enforces constraints (no public datastores, encryption at rest) before any resource is planned |
| "Why does this VPC exist?" requires reading old PRs | Contract describes intent: `role: primary_datastore`, `availability: high` |
| Drift detection is resource-level ("sg-1234 changed") | Drift detection is contract-level ("your database is now publicly accessible, violating your security contract") |
| Re-using patterns requires copying HCL | Roles (`web_api`, `job_queue`) map to correct resource graphs automatically |
| Provider upgrades break HCL | Contract is provider-agnostic; execution backend absorbs provider changes |

---

## Quick start

```bash
git clone https://github.com/you/natural-iac
cd natural-iac
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=sk-ant-...
```

### Run the demo pipeline

```bash
# Uses the intent hardcoded in demo.py, or pass your own:
python demo.py --intent "I need a web API with Postgres and Redis" --output myapp

# Output:
#   myapp.contract.yaml       <- commit this
#   myapp-terraform-out/      <- review and apply
```

### Or use the CLI (VS Code integrated terminal recommended on Windows)

```bash
# Step 1: parse intent into a contract
niac parse "I need a web API with Postgres and Redis" -o myapp.contract.yaml

# Step 2: validate the contract
niac validate myapp.contract.yaml

# Step 3: generate Terraform
niac plan myapp.contract.yaml --render-dir ./terraform-out

# Step 4: apply
cd terraform-out
terraform init && terraform apply
```

---

## The contract

The contract is the source of truth. It lives in git. It is what gets reviewed, not Terraform state.

```yaml
schema_version: v1
name: saas-api
human_reviewed: false   # CI gates on this before apply

components:
  - name: api
    role: web_api
    requirements:
      availability: high
      size_hint: small
      publicly_accessible: true

  - name: db
    role: primary_datastore
    requirements:
      availability: high
      publicly_accessible: false   # enforced by validator
      backup_retention_days: 14

constraints:
  security:
    no_public_datastores: true
    encryption_at_rest: true
  cost:
    max_monthly_usd: 500.0

invariants:
  - name: no-public-db
    description: The database must never be publicly accessible
    rule: no primary_datastore is publicly_accessible
    severity: error
```

From this contract, the planner generates a complete AWS resource graph: VPC, subnets, security groups with least-privilege rules, RDS with multi-AZ and encryption, ECS Fargate with ALB, CloudWatch log groups, IAM roles -- all correctly wired together.

### Component roles (provider-agnostic)

| Role | Maps to (AWS) |
|---|---|
| `web_api` | ECS Fargate + ALB, or EC2 if OS/user-data specified |
| `worker` | ECS Fargate (no ALB) |
| `primary_datastore` | RDS (Postgres default) |
| `cache` | ElastiCache Redis |
| `job_queue` | SQS |
| `message_broker` | MSK (Kafka) |
| `object_storage` | S3 |
| `cdn` | CloudFront |
| `secret_store` | Secrets Manager |
| `network` | VPC + subnets + route tables |

### Availability tiers

| Tier | Meaning |
|---|---|
| `development` | Single instance, can restart |
| `standard` | Reasonable uptime, single-AZ |
| `high` | Multi-AZ, no single point of failure |
| `critical` | Multi-region |

### Escape hatch

Provider-specific configuration that doesn't belong in the abstract contract goes in `raw_override`:

```yaml
- name: webserver
  role: web_api
  requirements:
    extra:
      os: ubuntu
      user_data: "#!/bin/bash\napt-get install -y nginx"
      data_disks:
        - size_gb: 50
          encrypted: true
  raw_override:
    provider: aws
    content:
      instance_type_family: t3
      ami_filter: ubuntu
```

The intent agent populates `raw_override` automatically when you mention provider-specific requirements. The planner detects these signals and generates `aws_instance` + `aws_ebs_volume` instead of ECS.

---

## What gets generated

From a 5-component contract (API, database, cache, queue, worker), the planner generates ~38 AWS resources:

- VPC with public/private subnets, route tables, NAT gateway
- Security groups with least-privilege rules (app SG -> DB SG on port 5432, not 0.0.0.0/0)
- RDS with `storage_encrypted=true`, `multi_az=true`, `deletion_protection=true`, `manage_master_user_password=true`
- ElastiCache with `at_rest_encryption_enabled`, `transit_encryption_enabled`, `automatic_failover_enabled`
- SQS with KMS encryption
- ECS Fargate with task definitions, execution roles, CloudWatch log groups
- IAM roles with least-privilege policies
- All `depends_on` edges correctly set

Estimated cost is printed alongside the plan (LOW confidence, for sanity-checking not billing).

---

## Architecture

```
natural_iac/
  intent/       IntentAgent -- NL -> InfraContract (Claude tool use, clarification loop)
  contract/     Pydantic schema, YAML serializer, constraint validator
  planner/      PlannerAgent -- InfraContract -> ExecutionPlan (Claude tool use)
                cost.py -- lookup-based cost estimator
  execution/    ExecutionBackend ABC
                terraform/  -- HCL renderer + terraform CLI wrapper + tfstate reader
```

### Security model

The contract validator enforces constraints before any resource is planned:

- `no_public_datastores` -- ERROR if any datastore/cache has `publicly_accessible: true`
- `encryption_at_rest` -- ERROR if raw_override explicitly disables encryption
- `availability.dependency_tier` -- WARN if a high-availability service depends on a standard-availability service
- `human_reviewed` -- WARN until a human sets the flag
- Custom `invariants` with a simple rule DSL

CI should gate on `human_reviewed: true` and zero errors before allowing `terraform apply`.

---

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v   # 96 tests, ~1s
```

### Test without hitting the API

All Claude calls are mocked in tests. The full pipeline (intent -> contract -> plan -> HCL) is covered without needing an API key.

```bash
python -m pytest tests/test_contract.py    # schema, validation, YAML
python -m pytest tests/test_intent_agent.py  # intent parsing, clarification loop
python -m pytest tests/test_planner.py    # resource mapping, cost estimation
python -m pytest tests/test_execution.py  # HCL rendering, state, terraform wrapper
```

---

## Current limitations

- **HCL block syntax**: Terraform AWS provider v5 requires block syntax (`ingress { }`) for some nested resource arguments. The renderer currently uses attribute syntax (`ingress = [...]`) for all nested values. Generated HCL requires minor manual edits before `terraform validate` passes on those resources.
- **No apply command yet**: `niac plan` generates HCL but there is no `niac apply` -- run Terraform directly from the rendered output directory.
- **AWS only**: The planner prompt targets AWS/Terraform. GCP and Azure backends are not yet implemented.
- **No drift detection yet**: State backend exists but drift detection against the contract is not yet implemented.

---

## Stack

- Python 3.12
- [Anthropic SDK](https://github.com/anthropics/anthropic-sdk-python) -- Claude for intent parsing and planning
- [Pydantic v2](https://docs.pydantic.dev/) -- contract schema validation
- [Terraform](https://www.terraform.io/) -- execution backend
- [Typer](https://typer.tiangolo.com/) + [Rich](https://rich.readthedocs.io/) -- CLI
