"""
Module variable reader -- fetches and parses a Terraform module's variables.tf
so the planner knows what inputs the module actually accepts.

Supports:
  - Public GitHub repos via raw.githubusercontent.com
  - Local filesystem paths (for private/internal modules)

Falls back gracefully (returns empty list) if fetching fails for any reason
(no internet, private repo, unexpected URL format). The pipeline continues
using input_map/passthrough from conventions.yaml as a manual fallback.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# Simple in-process cache: source URL -> list[ModuleVariable]
# Avoids re-fetching the same module multiple times in one session.
_CACHE: dict[str, list["ModuleVariable"]] = {}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ModuleVariable:
    name: str
    type: str = "any"
    description: str = ""
    required: bool = True  # True = no default declared


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_module_variables(source: str) -> list[ModuleVariable]:
    """Return the list of input variables declared in a Terraform module.

    Tries GitHub raw content first, then local path. Returns [] on any
    failure so callers can proceed with manual input_map fallback.
    """
    if source in _CACHE:
        return _CACHE[source]

    variables: list[ModuleVariable] = []

    raw_url = _github_raw_variables_url(source)
    if raw_url:
        content = _fetch_url(raw_url)
        if content:
            variables = _parse_variables_tf(content)

    if not variables:
        local_vars_tf = _local_variables_tf_path(source)
        if local_vars_tf and local_vars_tf.exists():
            variables = _parse_variables_tf(local_vars_tf.read_text(encoding="utf-8"))

    _CACHE[source] = variables
    return variables


def clear_cache() -> None:
    """Clear the in-process variable cache (useful in tests)."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# GitHub URL resolution
# ---------------------------------------------------------------------------


def _github_raw_variables_url(source: str) -> str | None:
    """Convert a module source string to a raw GitHub variables.tf URL.

    Handles the common formats produced by the LLM and by humans:
      git::https://github.com/owner/repo.git//subdir?ref=v1.0
      git::github.com/owner/repo//subdir?ref=v1.0
      github.com/owner/repo
      git::github.com/owner/repo/tree/v7.2.0   (LLM sometimes generates this)
    """
    s = source

    # Strip protocol prefixes
    for prefix in ("git::", "https://", "http://"):
        s = s.removeprefix(prefix)

    if not s.startswith("github.com/"):
        return None

    # Extract ?ref= query param
    ref = "main"
    if "?ref=" in s:
        s, ref = s.split("?ref=", 1)
        ref = ref.split("&")[0]  # drop any extra query params

    # Extract /tree/{ref} pattern (LLM sometimes generates GitHub browser URLs)
    tree_match = re.search(r"/tree/([^/]+)", s)
    if tree_match:
        ref = tree_match.group(1)
        s = s[:tree_match.start()]

    # Extract subdir (//subdir syntax)
    subdir = ""
    if "//" in s:
        s, subdir = s.split("//", 1)
        subdir = subdir.strip("/")

    # Strip .git suffix
    s = re.sub(r"\.git$", "", s)

    # Match github.com/owner/repo
    m = re.match(r"github\.com/([^/]+)/([^/\s]+)$", s)
    if not m:
        return None

    owner, repo = m.group(1), m.group(2)
    path = f"{subdir}/variables.tf" if subdir else "variables.tf"
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"


def _local_variables_tf_path(source: str) -> Path | None:
    """Return a local variables.tf path if the source looks like a local path."""
    # Local paths start with ./ ../ or / (absolute)
    if source.startswith(("./", "../", "/")):
        subdir = ""
        if "//" in source:
            base, subdir = source.split("//", 1)
        else:
            base = source
        target = Path(base) / subdir / "variables.tf" if subdir else Path(base) / "variables.tf"
        return target
    return None


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------


def _fetch_url(url: str, timeout: int = 5) -> str | None:
    """Fetch URL content as text. Returns None on any error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# HCL variable block parser
# ---------------------------------------------------------------------------


def _parse_variables_tf(content: str) -> list[ModuleVariable]:
    """Parse HCL variable blocks from a variables.tf file.

    Regex-based rather than a full HCL parser -- handles all common
    variable block patterns without introducing a dependency.
    """
    variables: list[ModuleVariable] = []

    # Match variable "name" { ... } blocks, including nested braces
    pattern = re.compile(
        r'variable\s+"([^"]+)"\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}',
        re.DOTALL,
    )

    for match in pattern.finditer(content):
        name = match.group(1)
        body = match.group(2)

        # description = "..."  (single or multiline with <<-EOT is ignored)
        desc_match = re.search(r'description\s*=\s*"([^"]*)"', body)
        description = desc_match.group(1) if desc_match else ""

        # type = string | number | bool | list(...) | map(...) | any | object({...})
        type_match = re.search(r"type\s*=\s*(\S+)", body)
        type_ = type_match.group(1) if type_match else "any"

        # required = no "default" key in block
        has_default = bool(re.search(r"\bdefault\b", body))

        variables.append(ModuleVariable(
            name=name,
            type=type_,
            description=description,
            required=not has_default,
        ))

    return variables


# ---------------------------------------------------------------------------
# Formatting helpers (used by planner prompt builder)
# ---------------------------------------------------------------------------


def format_variables_for_prompt(variables: list[ModuleVariable]) -> str:
    """Render a variable list as a compact prompt section."""
    if not variables:
        return ""

    required = [v for v in variables if v.required]
    optional = [v for v in variables if not v.required]

    lines: list[str] = []

    if required:
        lines.append("  Required (must emit):")
        for v in required:
            desc = f"  # {v.description}" if v.description else ""
            lines.append(f"    {v.name} ({v.type}){desc}")

    if optional:
        lines.append("  Optional:")
        for v in optional:
            desc = f"  # {v.description}" if v.description else ""
            lines.append(f"    {v.name} ({v.type}){desc}")

    return "\n".join(lines)
