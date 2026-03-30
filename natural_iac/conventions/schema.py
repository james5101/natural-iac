"""
Conventions schema -- org-level standards applied across all plans.

Loaded from .niac/conventions.yaml at plan/validate time. Controls:
  - naming:  pattern templates for Terraform logical_name
  - tags:    required tags (validator errors) and org-wide defaults
  - modules: Terraform module overrides for specific resource types
  - defaults: org-wide property overrides (conventions win over contract)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_CONVENTIONS_PATH = Path(".niac/conventions.yaml")


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


class NamingConfig(BaseModel):
    """Pattern-based naming for Terraform logical names.

    pattern uses {placeholders} resolved from variables + two built-ins:
      {component}  -- contract component name
      {type_short} -- looked up from type_short_map, falls back to resource type
    """
    pattern: str = "{component}-{type_short}"
    variables: dict[str, str] = Field(default_factory=dict)
    type_short_map: dict[str, str] = Field(default_factory=dict)

    def apply(self, resource_type: str, component: str) -> str:
        """Return the logical name for a resource given its type and component."""
        type_short = self.type_short_map.get(resource_type, resource_type)
        ctx = {**self.variables, "component": component, "type_short": type_short}
        try:
            return self.pattern.format(**ctx)
        except KeyError:
            # Pattern references a variable not in ctx -- fall back gracefully
            return f"{component}-{type_short}"


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class TagConfig(BaseModel):
    required: list[str] = Field(default_factory=list)
    defaults: dict[str, str] = Field(default_factory=dict)

    def resolve_defaults(self, variables: dict[str, str]) -> dict[str, str]:
        """Return defaults with ${var} placeholders resolved from naming variables."""
        resolved: dict[str, str] = {}
        for k, v in self.defaults.items():
            for var_name, var_val in variables.items():
                v = v.replace(f"${{{var_name}}}", var_val)
            resolved[k] = v
        return resolved


# ---------------------------------------------------------------------------
# Module overrides
# ---------------------------------------------------------------------------


class ModuleOverride(BaseModel):
    """Replace a specific Terraform resource type with an internal module.

    When the renderer encounters a resource whose type matches ``match``,
    it emits a ``module`` block instead of a ``resource`` block.

    input_map:   {our_property_name: module_variable_name}
    passthrough: property names passed through with their original name
    Unmapped, non-passthrough properties are dropped with a warning.
    """
    match: str  # Terraform resource type, e.g. "aws_db_instance"
    source: str  # module source URL
    name_template: str = "{component}"  # {component} and {type_short} available
    input_map: dict[str, str] = Field(default_factory=dict)
    passthrough: list[str] = Field(default_factory=list)

    def resolve_name(self, component: str, resource_type: str, type_short_map: dict[str, str]) -> str:
        type_short = type_short_map.get(resource_type, resource_type)
        try:
            return self.name_template.format(component=component, type_short=type_short)
        except KeyError:
            return component


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class DefaultsConfig(BaseModel):
    """Org-wide property overrides keyed by Terraform resource type.

    Conventions win -- these values are applied after the planner output
    and cannot be overridden by the contract.
    """
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DefaultsConfig":
        return cls(overrides=raw)

    def apply(self, resource_type: str, properties: dict[str, Any]) -> dict[str, Any]:
        """Return properties with org defaults merged in (org wins on conflict)."""
        org = self.overrides.get(resource_type, {})
        if not org:
            return properties
        return {**properties, **org}


# ---------------------------------------------------------------------------
# Top-level profile
# ---------------------------------------------------------------------------


class ConventionProfile(BaseModel):
    version: str = "v1"
    naming: NamingConfig = Field(default_factory=NamingConfig)
    tags: TagConfig = Field(default_factory=TagConfig)
    modules: list[ModuleOverride] = Field(default_factory=list)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)

    @classmethod
    def load(cls, path: Path | str | None = None) -> "ConventionProfile | None":
        """Load conventions from .niac/conventions.yaml (or explicit path).

        Returns None if the file does not exist -- callers treat None as
        "no conventions configured".
        """
        import yaml  # lazy import -- not required if conventions unused

        target = Path(path) if path else _CONVENTIONS_PATH
        if not target.exists():
            return None

        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}

        # defaults is a plain dict in YAML; normalise to DefaultsConfig
        raw_defaults = raw.pop("defaults", {})
        profile = cls.model_validate(raw)
        profile.defaults = DefaultsConfig.from_dict(raw_defaults)
        return profile

    def module_for(self, resource_type: str) -> "ModuleOverride | None":
        """Return the ModuleOverride for resource_type, or None."""
        for m in self.modules:
            if m.match == resource_type:
                return m
        return None
