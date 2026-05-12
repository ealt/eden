"""Shared scaffolding for reference EDEN services."""

from __future__ import annotations

from .auth import (
    DEFAULT_CREDENTIALS_DIR,
    WorkerCredential,
    bootstrap_worker_credential,
    credential_path,
    resolve_worker_bearer,
)
from .cli import (
    add_common_arguments,
    add_exec_arguments,
    parse_log_level,
    resolve_admin_token,
    resolve_credentials_dir,
    resolve_exec_args,
)
from .container_exec import (
    BindMount,
    VolumeMount,
    cleanup_cidfile,
    kill_via_cidfile,
    make_cidfile_callbacks,
    make_cidfile_path,
    parse_bind_spec,
    parse_volume_spec,
    reap_orphaned_containers,
    wrap_command,
)
from .experiment_config import load_experiment_config, require_command
from .logging import configure_logging, get_logger
from .readiness import wait_for_task_store
from .repo import seed_bare_repo
from .scripted import (
    ScriptedEvaluateFn,
    ScriptedImplementFn,
    ScriptedPlanFn,
    make_evaluate_fn,
    make_implement_fn,
    make_plan_fn,
)
from .signals import StopFlag, install_stop_handlers
from .subprocess_runner import (
    Subprocess,
    parse_env_file,
    parse_json_line,
    spawn,
)
from .worktrees import TaskWorktree, sweep_host_worktrees

__all__ = [
    "BindMount",
    "DEFAULT_CREDENTIALS_DIR",
    "ScriptedEvaluateFn",
    "ScriptedImplementFn",
    "ScriptedPlanFn",
    "StopFlag",
    "Subprocess",
    "TaskWorktree",
    "VolumeMount",
    "WorkerCredential",
    "add_common_arguments",
    "add_exec_arguments",
    "bootstrap_worker_credential",
    "cleanup_cidfile",
    "credential_path",
    "configure_logging",
    "get_logger",
    "install_stop_handlers",
    "kill_via_cidfile",
    "load_experiment_config",
    "make_cidfile_callbacks",
    "make_cidfile_path",
    "make_evaluate_fn",
    "make_implement_fn",
    "make_plan_fn",
    "parse_bind_spec",
    "parse_env_file",
    "parse_json_line",
    "parse_log_level",
    "parse_volume_spec",
    "reap_orphaned_containers",
    "require_command",
    "resolve_admin_token",
    "resolve_credentials_dir",
    "resolve_exec_args",
    "resolve_worker_bearer",
    "seed_bare_repo",
    "spawn",
    "sweep_host_worktrees",
    "wait_for_task_store",
    "wrap_command",
]
