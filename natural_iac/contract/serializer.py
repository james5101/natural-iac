"""YAML serialization for InfraContract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schema import InfraContract


def contract_to_dict(contract: InfraContract) -> dict[str, Any]:
    """Serialize contract to a plain dict suitable for YAML output."""
    # mode="json" ensures enums serialize as their string values, not Python objects
    return contract.model_dump(mode="json", exclude_none=True)


def contract_to_yaml(contract: InfraContract) -> str:
    """Render contract as a YAML string."""
    data = contract_to_dict(contract)
    return yaml.dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def contract_from_dict(data: dict[str, Any]) -> InfraContract:
    """Deserialize a contract from a plain dict (e.g. parsed YAML)."""
    return InfraContract.model_validate(data)


def contract_from_yaml(text: str) -> InfraContract:
    """Parse a YAML string into an InfraContract, validating the schema."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("Contract YAML must be a mapping at the top level")
    return contract_from_dict(data)


def load_contract(path: str | Path) -> InfraContract:
    """Load and validate a contract from a YAML file on disk."""
    path = Path(path)
    return contract_from_yaml(path.read_text(encoding="utf-8"))


def save_contract(contract: InfraContract, path: str | Path) -> None:
    """Write a contract to a YAML file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contract_to_yaml(contract), encoding="utf-8")
