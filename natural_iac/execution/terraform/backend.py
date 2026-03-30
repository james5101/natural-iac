"""
Terraform execution backend.

Uses Terraform as an execution primitive — it handles resource ordering,
parallelism, and apply/destroy semantics. State is read back from tfstate
after each operation and merged into our own State model.

The contract and planner layers are unaware this backend exists.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...planner.schema import ExecutionPlan
from ..backends import ApplyResult, ExecutionBackend
from ..state import ResourceState, State
from .renderer import render_to_dir


class TerraformBackend(ExecutionBackend):
    """Execution backend that shells out to the Terraform CLI.

    Parameters
    ----------
    work_dir:
        Directory where HCL files and tfstate are written. If None a
        temporary directory is created and cleaned up after each operation.
        For persistent state, pass a stable path (e.g. ``.niac/terraform``).
    terraform_bin:
        Path to the ``terraform`` binary. Defaults to ``terraform`` on $PATH.
    """

    def __init__(
        self,
        work_dir: str | Path | None = None,
        terraform_bin: str = "terraform",
    ) -> None:
        self.work_dir = Path(work_dir) if work_dir else None
        self.terraform_bin = terraform_bin

    # -----------------------------------------------------------------------
    # ExecutionBackend interface
    # -----------------------------------------------------------------------

    async def apply(
        self,
        plan: ExecutionPlan,
        state: State,
        *,
        auto_approve: bool = False,
    ) -> tuple[ApplyResult, State]:
        work_dir, cleanup = self._resolve_work_dir()
        try:
            render_to_dir(plan, work_dir)
            await self._terraform_init(work_dir)
            result = await self._terraform_apply(work_dir, auto_approve=auto_approve)
            if result.success:
                tfstate = _load_tfstate(work_dir)
                state = _merge_tfstate(tfstate, state, plan)
                state.last_applied = datetime.now(timezone.utc)
            return result, state
        finally:
            if cleanup:
                shutil.rmtree(work_dir, ignore_errors=True)

    async def destroy(
        self,
        plan: ExecutionPlan,
        state: State,
        *,
        auto_approve: bool = False,
    ) -> tuple[ApplyResult, State]:
        work_dir, cleanup = self._resolve_work_dir()
        try:
            render_to_dir(plan, work_dir)
            await self._terraform_init(work_dir)
            result = await self._terraform_destroy(work_dir, auto_approve=auto_approve)
            if result.success:
                for resource_id in result.destroyed:
                    state.remove(resource_id)
            return result, state
        finally:
            if cleanup:
                shutil.rmtree(work_dir, ignore_errors=True)

    async def refresh(self, plan: ExecutionPlan, state: State) -> State:
        work_dir, cleanup = self._resolve_work_dir()
        try:
            render_to_dir(plan, work_dir)
            await self._terraform_init(work_dir)
            await self._run(["refresh"], work_dir)
            tfstate = _load_tfstate(work_dir)
            return _merge_tfstate(tfstate, state, plan)
        finally:
            if cleanup:
                shutil.rmtree(work_dir, ignore_errors=True)

    # -----------------------------------------------------------------------
    # Terraform CLI wrappers
    # -----------------------------------------------------------------------

    async def _terraform_init(self, work_dir: Path) -> None:
        stdout, stderr, code = await self._run(
            ["init", "-no-color", "-input=false"],
            work_dir,
        )
        if code != 0:
            raise TerraformError(f"terraform init failed:\n{stderr}")

    async def _terraform_apply(
        self, work_dir: Path, *, auto_approve: bool
    ) -> ApplyResult:
        args = ["apply", "-no-color", "-input=false"]
        if auto_approve:
            args.append("-auto-approve")

        stdout, stderr, code = await self._run(args, work_dir)

        if code != 0:
            return ApplyResult(
                success=False,
                error=stderr or stdout,
                stdout=stdout,
                stderr=stderr,
            )

        created, updated, destroyed = _parse_apply_summary(stdout)
        return ApplyResult(
            success=True,
            created=created,
            updated=updated,
            destroyed=destroyed,
            stdout=stdout,
            stderr=stderr,
        )

    async def _terraform_destroy(
        self, work_dir: Path, *, auto_approve: bool
    ) -> ApplyResult:
        args = ["destroy", "-no-color", "-input=false"]
        if auto_approve:
            args.append("-auto-approve")

        stdout, stderr, code = await self._run(args, work_dir)

        if code != 0:
            return ApplyResult(
                success=False,
                error=stderr or stdout,
                stdout=stdout,
                stderr=stderr,
            )

        _, _, destroyed = _parse_apply_summary(stdout)
        return ApplyResult(
            success=True,
            destroyed=destroyed,
            stdout=stdout,
            stderr=stderr,
        )

    async def _run(
        self, args: list[str], work_dir: Path
    ) -> tuple[str, str, int]:
        """Run a terraform subcommand, return (stdout, stderr, returncode)."""
        proc = await asyncio.create_subprocess_exec(
            self.terraform_bin,
            *args,
            cwd=str(work_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        return (
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
            proc.returncode,
        )

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _resolve_work_dir(self) -> tuple[Path, bool]:
        """Return (work_dir, should_cleanup)."""
        if self.work_dir is not None:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            return self.work_dir, False
        tmp = Path(tempfile.mkdtemp(prefix="niac-terraform-"))
        return tmp, True


# ---------------------------------------------------------------------------
# tfstate parsing
# ---------------------------------------------------------------------------


def _load_tfstate(work_dir: Path) -> dict[str, Any]:
    tfstate_path = work_dir / "terraform.tfstate"
    if not tfstate_path.exists():
        return {}
    return json.loads(tfstate_path.read_text(encoding="utf-8"))


def _merge_tfstate(
    tfstate: dict[str, Any],
    state: State,
    plan: ExecutionPlan,
) -> State:
    """Extract resource instances from tfstate and upsert into our State."""
    resources_in_tfstate = (
        tfstate.get("resources", []) if tfstate else []
    )

    for tf_resource in resources_in_tfstate:
        rtype = tf_resource.get("type", "")
        lname = tf_resource.get("name", "")
        resource_id = f"{rtype}.{lname}"

        # Pull the first instance's attributes
        instances = tf_resource.get("instances", [])
        if not instances:
            continue
        attrs = instances[0].get("attributes", {})

        # Find the matching plan resource to get component name
        plan_resource = plan.resource_map.get(resource_id)
        component = plan_resource.component if plan_resource else "unknown"

        state.upsert(ResourceState(
            id=resource_id,
            type=rtype,
            logical_name=lname,
            component=component,
            provider_id=attrs.get("id") or attrs.get("arn"),
            attributes=attrs,
            last_refreshed=datetime.now(timezone.utc),
        ))

    return state


def _parse_apply_summary(output: str) -> tuple[list[str], list[str], list[str]]:
    """Extract created/updated/destroyed resource names from terraform output.

    Terraform prints lines like:
      aws_vpc.main: Creating...
      aws_vpc.main: Creation complete after 2s [id=vpc-0abc]
      aws_db_instance.db: Modifying... [id=mydb]
      aws_db_instance.db: Still destroying... [id=mydb]
    """
    created: list[str] = []
    updated: list[str] = []
    destroyed: list[str] = []

    for line in output.splitlines():
        # "resource_type.name: Creation complete"
        if ": Creation complete" in line:
            name = line.split(":")[0].strip()
            if name:
                created.append(name)
        elif ": Modifications complete" in line or ": Still modifying" in line:
            name = line.split(":")[0].strip()
            if name and name not in updated:
                updated.append(name)
        elif ": Destruction complete" in line:
            name = line.split(":")[0].strip()
            if name:
                destroyed.append(name)

    return created, updated, destroyed


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TerraformError(RuntimeError):
    """Raised when a Terraform CLI command fails."""
