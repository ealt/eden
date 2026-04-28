"""Shared scaffolding for reference EDEN services."""

from __future__ import annotations

from .cli import add_common_arguments, parse_log_level
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
    "ScriptedEvaluateFn",
    "ScriptedImplementFn",
    "ScriptedPlanFn",
    "StopFlag",
    "Subprocess",
    "TaskWorktree",
    "add_common_arguments",
    "configure_logging",
    "get_logger",
    "install_stop_handlers",
    "load_experiment_config",
    "make_evaluate_fn",
    "make_implement_fn",
    "make_plan_fn",
    "parse_env_file",
    "parse_json_line",
    "parse_log_level",
    "require_command",
    "seed_bare_repo",
    "spawn",
    "sweep_host_worktrees",
    "wait_for_task_store",
]
