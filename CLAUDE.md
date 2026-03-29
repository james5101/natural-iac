# natural-iac

Intent-based infrastructure as code — natural language → structured contract → provider execution.

## Project Vision

Replace HCL/Terraform verbosity with a three-layer model:

1. **Intent** (natural language) — what you need and why
2. **Contract** (structured YAML) — machine-verifiable requirements, constraints, invariants — this is what gets committed to git
3. **Execution Plan** (provider resources) — resolved from contract + current state, renderable as Terraform or direct SDK calls

The contract is the source of truth. Intent is ephemeral. Execution is derived.

## Architecture

```
Intent Agent      →  parses NL, asks clarifying questions, emits contract
Constraint Validator  →  checks org policies, security posture, cost limits
Planner Agent     →  contract + state → resource graph + cost estimate
Execution Engine  →  pluggable backends (Terraform, Pulumi, direct AWS SDK)
State Backend     →  stores contract state + resource state, enables drift detection at intent level
```

## Stack

- Python 3.12
- Claude API (Anthropic SDK) for intent parsing and planning agents
- Pydantic for contract schema validation
- Infracost API for cost estimation
- Terraform as initial execution backend (escape hatch to raw HCL always supported)

## Key Design Decisions

- **Contract YAML is provider-agnostic** — roles like `web_api`, `primary_datastore`, `job_queue` not AWS/GCP specifics
- **LLM non-determinism is solved at the contract layer** — re-parsing intent may differ; committed contract does not
- **Escape hatch is first-class** — `raw_override` blocks in contracts pass through to execution verbatim
- **Drift detection is contract-aware** — "publicly accessible RDS violates your security contract" not just state diff
- **human_reviewed flag in contract** — CI gates on this before apply

## Project Structure

```
natural_iac/
  intent/       intent parsing agent, clarification loop
  contract/     Pydantic schema, validator, YAML serialization
  planner/      maps contract → resource graph, cost estimation
  execution/    pluggable backends, Terraform renderer
examples/       sample .intent files and resolved contracts
```

## Development Notes

- Keep provider-specific logic isolated in `execution/` backends
- Contract schema changes are breaking — version the schema
- All agent calls should be traceable (log intent → contract → plan chain)
