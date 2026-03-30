"""
Module variable reader -- fetches and parses a Terraform module's variables.tf
so the planner knows what inputs the module actually accepts.

Supports:
  - Public GitHub repos via raw.githubusercontent.com
  - Private GitHub repos -- set GITHUB_TOKEN or GH_TOKEN env var
  - GitHub Enterprise Server -- detected from non-github.com hostnames
  - Local filesystem paths -- set local_path on the ModuleOverride in conventions.yaml

Falls back gracefully (returns empty list + warning) if fetching fails for any
reason. The pipeline continues using input_map/passthrough from conventions.yaml.

Private repo setup:
  export GITHUB_TOKEN=ghp_...      # GitHub.com personal access token
  export GH_TOKEN=ghp_...          # alias, also accepted

GitHub Enterprise:
  Source URL: git::github.mycompany.com/owner/repo?ref=v1.0
  The hostname is detected automatically from the URL.
  Set GITHUB_TOKEN with a token that has access to the enterprise instance.

Local path (no network needed):
  In .niac/conventions.yaml:
    modules:
      - match: aws_db_instance
        source: git::github.mycompany.com/infra/tf-modules//rds?ref=v3.0
        local_path: ./vendor/tf-modules/rds   # takes priority if it exists
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass
from pathlib import Path

# Simple in-process cache: cache_key -> list[ModuleVariable]
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


def fetch_module_variables(
    source: str,
    local_path: str | None = None,
) -> list[ModuleVariable]:
    """Return the list of input variables declared in a Terraform module.

    Resolution order:
      1. local_path (explicit override in conventions.yaml) -- fastest, works offline
      2. GitHub raw content API (with auth token if GITHUB_TOKEN/GH_TOKEN is set)
      3. Source-string local path (./relative or /absolute paths in source field)

    Returns [] on any failure and emits a warning so operators know to act.
    """
    cache_key = f"{source}::{local_path or ''}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    variables: list[ModuleVariable] = []

    # 1. Explicit local_path takes priority
    if local_path:
        lp = Path(local_path) / "variables.tf"
        if lp.exists():
            variables = _parse_variables_tf(lp.read_text(encoding="utf-8"))

    # 2. GitHub (public or private via token)
    if not variables:
        raw_url, host = _github_raw_variables_url(source)
        if raw_url:
            token = _github_token()
            content = _fetch_url(raw_url, token=token)
            if content:
                variables = _parse_variables_tf(content)
            elif not variables:
                _warn_fetch_failed(source, host, has_token=bool(token), local_path=local_path)

    # 3. Source is itself a local path
    if not variables:
        local_vars_tf = _local_variables_tf_path(source)
        if local_vars_tf and local_vars_tf.exists():
            variables = _parse_variables_tf(local_vars_tf.read_text(encoding="utf-8"))

    _CACHE[cache_key] = variables
    return variables


def clear_cache() -> None:
    """Clear the in-process variable cache (useful in tests)."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# GitHub URL resolution
# ---------------------------------------------------------------------------


def _github_raw_variables_url(source: str) -> tuple[str | None, str | None]:
    """Convert a module source string to a raw GitHub variables.tf URL.

    Returns (url, hostname) where hostname is the GitHub host (github.com or
    a GitHub Enterprise hostname). Returns (None, None) if not a GitHub source.

    Handles formats produced by the LLM and by humans:
      git::https://github.com/owner/repo.git//subdir?ref=v1.0
      git::github.com/owner/repo//subdir?ref=v1.0
      git::github.mycompany.com/owner/repo?ref=v1.0    (GitHub Enterprise)
      github.com/owner/repo
      git::github.com/owner/repo/tree/v7.2.0            (LLM browser URL)
    """
    s = source

    # Strip protocol prefixes
    for prefix in ("git::", "https://", "http://"):
        s = s.removeprefix(prefix)

    # Must contain a github-like hostname
    host_match = re.match(r"(github(?:\.[^/]+)+)/", s)
    if not host_match:
        return None, None

    host = host_match.group(1)  # e.g. "github.com" or "github.mycompany.com"

    # Extract ?ref= query param
    ref = "main"
    if "?ref=" in s:
        s, ref = s.split("?ref=", 1)
        ref = ref.split("&")[0]

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

    # Match {host}/owner/repo
    m = re.match(rf"{re.escape(host)}/([^/]+)/([^/\s]+)$", s)
    if not m:
        return None, None

    owner, repo = m.group(1), m.group(2)
    path = f"{subdir}/variables.tf" if subdir else "variables.tf"

    if host == "github.com":
        # Public GitHub uses raw.githubusercontent.com
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    else:
        # GitHub Enterprise Server: raw content at {host}/{owner}/{repo}/raw/{ref}/{path}
        url = f"https://{host}/{owner}/{repo}/raw/{ref}/{path}"

    return url, host


def _local_variables_tf_path(source: str) -> Path | None:
    """Return a local variables.tf path if the source looks like a local path."""
    if source.startswith(("./", "../", "/")):
        if "//" in source:
            base, subdir = source.split("//", 1)
        else:
            base, subdir = source, ""
        return Path(base) / subdir / "variables.tf" if subdir else Path(base) / "variables.tf"
    return None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _github_token() -> str | None:
    """Return a GitHub token from the environment, or None."""
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------


def _fetch_url(url: str, token: str | None = None, timeout: int = 5) -> str | None:
    """Fetch URL content as text. Returns None on any error.

    Adds Authorization header when a token is provided (required for
    private GitHub repos and GitHub Enterprise instances).
    """
    try:
        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Failure warning
# ---------------------------------------------------------------------------


def _warn_fetch_failed(
    source: str,
    host: str | None,
    has_token: bool,
    local_path: str | None,
) -> None:
    """Emit an actionable warning when module variable fetching fails."""
    lines = [
        f"Could not fetch module variables from: {source}",
        "Falling back to input_map/passthrough from conventions.yaml.",
    ]

    if not has_token:
        lines.append(
            "If this is a private repo, set GITHUB_TOKEN (or GH_TOKEN) "
            "to a token with read access."
        )
    else:
        lines.append(
            "A GITHUB_TOKEN is set but the request still failed. "
            "Check that the token has 'repo' (private) or 'contents:read' scope "
            f"and access to {host or 'the module host'}."
        )

    if not local_path:
        lines.append(
            "Alternatively, add local_path to the module entry in .niac/conventions.yaml "
            "pointing to a local checkout of the module."
        )

    warnings.warn("\n".join(lines), stacklevel=3)


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

        desc_match = re.search(r'description\s*=\s*"([^"]*)"', body)
        description = desc_match.group(1) if desc_match else ""

        type_match = re.search(r"type\s*=\s*(\S+)", body)
        type_ = type_match.group(1) if type_match else "any"

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
