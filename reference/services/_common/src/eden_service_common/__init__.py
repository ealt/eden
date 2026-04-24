"""Shared scaffolding for reference EDEN services."""

from __future__ import annotations

from .cli import add_common_arguments, parse_log_level
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

__all__ = [
    "ScriptedEvaluateFn",
    "ScriptedImplementFn",
    "ScriptedPlanFn",
    "StopFlag",
    "add_common_arguments",
    "configure_logging",
    "get_logger",
    "install_stop_handlers",
    "make_evaluate_fn",
    "make_implement_fn",
    "make_plan_fn",
    "parse_log_level",
    "seed_bare_repo",
    "wait_for_task_store",
]
