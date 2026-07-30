#!/usr/bin/env python3
"""Per-experiment cost report (issue #343 milestone 3).

Reads the reference cost ledger over the wire and reduces it to a
per-role + per-variant rollup, joined against each variant's status and
evaluation payload so **DCI-per-dollar** (or any other
metric-per-dollar) is a one-line consumer computation rather than a
two-source join the caller has to write.

    python3 -m eden_service_common.cost_report
        --task-store-url http://localhost:8080
        --experiment-id exp_… > cost.json

JSON is the default because the point of the report is feeding analysis;
``--format table`` is the human read. Auth comes from the environment
(``EDEN_ADMIN_TOKEN``, or a full ``EDEN_BEARER`` for a worker identity)
— never from argv, where it would land in shell history and every ``ps``
listing on the box.

The rollup is a read-time reduction (:func:`eden_storage.summarize`), so
it cannot disagree with the ledger it reads. Entries with no
``variant_id`` — ideation spend, which precedes any variant — land in
``totals`` and ``by_role`` but in no ``by_variant`` bucket; ``by_role``
is the complete partition. `by_variant` rows for a variant the store no
longer has (or has not yet completed) carry ``status: null`` and
``evaluation: null`` rather than being dropped: the spend happened.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from eden_storage import summarize
from eden_storage.errors import NotFound
from eden_wire import StoreClient


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the report CLI's flags."""
    parser = argparse.ArgumentParser(
        description="Per-experiment cost rollup from the reference cost ledger.",
        epilog=(
            "Auth is read from EDEN_BEARER (a full '<principal>:<secret>' "
            "bearer) or EDEN_ADMIN_TOKEN (used as 'admin:<token>'). "
            "Neither is accepted on the command line."
        ),
    )
    parser.add_argument(
        "--task-store-url",
        required=True,
        help="Base URL of the task-store-server, e.g. http://localhost:8080",
    )
    parser.add_argument(
        "--experiment-id", required=True, help="Experiment to report on."
    )
    parser.add_argument(
        "--role",
        choices=("ideator", "executor", "evaluator"),
        help="Restrict to one role's spend (default: every role).",
    )
    parser.add_argument(
        "--variant-id", help="Restrict to one variant's spend."
    )
    parser.add_argument(
        "--format",
        choices=("json", "table"),
        default="json",
        help="Output format (default: json).",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="HTTP timeout in seconds."
    )
    return parser.parse_args(argv)


def resolve_bearer() -> str | None:
    """Return the §13.1 bearer from the environment, or ``None``.

    ``None`` is a valid posture: a task-store-server started without
    ``--admin-token`` runs unauthenticated (the in-process / test
    deployment), and sending a bearer at it is harmless but pointless.
    """
    bearer = os.environ.get("EDEN_BEARER")
    if bearer:
        return bearer
    admin_token = os.environ.get("EDEN_ADMIN_TOKEN")
    if admin_token:
        return f"admin:{admin_token}"
    return None


def variant_facts(client: StoreClient, variant_ids: list[str]) -> dict[str, Any]:
    """Return ``{variant_id: {status, evaluation, idea_id}}`` for the join.

    One read per variant rather than a ``list_variants`` sweep: a report
    scoped to one variant should not pull an entire long-running
    experiment's variant set, and the ledger's variant count is bounded
    by the number of attempts that actually spent money. A variant the
    store does not have maps to ``None`` facts — see the module
    docstring on why those rows survive.
    """
    facts: dict[str, Any] = {}
    for variant_id in variant_ids:
        try:
            variant = client.read_variant(variant_id)
        except NotFound:
            facts[variant_id] = {
                "status": None,
                "evaluation": None,
                "idea_id": None,
            }
            continue
        facts[variant_id] = {
            "status": variant.status,
            "evaluation": variant.evaluation,
            "idea_id": variant.idea_id,
        }
    return facts


def build_report(
    *,
    client: StoreClient,
    experiment_id: str,
    role: str | None,
    variant_id: str | None,
) -> dict[str, Any]:
    """Fetch the ledger, reduce it, and join per-variant facts."""
    entries = client.list_cost_entries(role=role, variant_id=variant_id)
    summary = summarize(experiment_id, entries)
    facts = variant_facts(client, sorted(summary.by_variant))
    return {
        "experiment_id": experiment_id,
        "filters": {"role": role, "variant_id": variant_id},
        "totals": summary.totals.model_dump(mode="json"),
        "by_role": {
            name: totals.model_dump(mode="json")
            for name, totals in sorted(summary.by_role.items())
        },
        "by_variant": [
            {
                "variant_id": vid,
                **facts[vid],
                "cost": summary.by_variant[vid].model_dump(mode="json"),
            }
            for vid in sorted(summary.by_variant)
        ],
        "entries": [entry.to_payload() for entry in entries],
    }


def _fmt_usd(value: float) -> str:
    return f"${value:.4f}"


def render_table(report: dict[str, Any]) -> str:
    """Human-readable rendering; the JSON form is the machine contract."""
    lines: list[str] = [f"experiment: {report['experiment_id']}"]
    totals = report["totals"]
    lines.append(
        f"total: {_fmt_usd(totals['total_cost_usd'])} over "
        f"{totals['entries']} attempt(s)"
        + (
            f" — {totals['entries_missing_cost_usd']} attempt(s) reported "
            "tokens but no dollar figure"
            if totals["entries_missing_cost_usd"]
            else ""
        )
    )
    lines.append("")
    # `no_usd` is per-role rather than only in the header line: a role
    # whose entries all lack a dollar figure otherwise renders as
    # $0.0000, which reads as "this role was free".
    lines.append(
        f"{'role':<12} {'attempts':>8} {'no_usd':>7} {'usd':>12} "
        f"{'in_tok':>12} {'out_tok':>10}"
    )
    for name, row in report["by_role"].items():
        lines.append(
            f"{name:<12} {row['entries']:>8} {row['entries_missing_cost_usd']:>7} "
            f"{_fmt_usd(row['total_cost_usd']):>12} "
            f"{row['input_tokens']:>12} {row['output_tokens']:>10}"
        )
    lines.append("")
    lines.append(
        f"{'variant':<26} {'status':<18} {'usd':>12} {'evaluation':<30}"
    )
    for row in report["by_variant"]:
        evaluation = row["evaluation"]
        lines.append(
            f"{row['variant_id']:<26} {str(row['status']):<18} "
            f"{_fmt_usd(row['cost']['total_cost_usd']):>12} "
            f"{json.dumps(evaluation) if evaluation else '-':<30}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Fetch, reduce, and print the report. Returns a process exit code."""
    args = parse_args(argv)
    with StoreClient(
        args.task_store_url,
        args.experiment_id,
        bearer=resolve_bearer(),
        timeout=args.timeout,
    ) as client:
        report = build_report(
            client=client,
            experiment_id=args.experiment_id,
            role=args.role,
            variant_id=args.variant_id,
        )
    if args.format == "table":
        print(render_table(report))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
