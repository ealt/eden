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

from eden_storage import CostEntry, PriceTable, derive_cost, load_price_table, summarize
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
        "--price-table",
        help=(
            "Path to a JSON rate table (see "
            "reference/pricing/price-table.example.json). Without it, only "
            "provider-reported dollars are reported; with it, entries no "
            "provider priced are derived from tokens x rates and labelled "
            "as derived."
        ),
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


def idea_facts(client: StoreClient, idea_ids: list[str]) -> dict[str, Any]:
    """Return ``{idea_id: {slug, state}}`` for the per-idea join.

    What makes per-idea cost worth reporting is the ideation-efficiency
    question — *which expensive ideas produced nothing* — and answering
    it needs the idea's own state next to its spend, not just a total.
    An idea the store no longer has maps to ``None`` facts for the same
    reason a missing variant does: the spend happened regardless.
    """
    facts: dict[str, Any] = {}
    for idea_id in idea_ids:
        try:
            idea = client.read_idea(idea_id)
        except NotFound:
            facts[idea_id] = {"slug": None, "state": None}
            continue
        facts[idea_id] = {"slug": idea.slug, "state": idea.state}
    return facts


def build_report(
    *,
    client: StoreClient,
    experiment_id: str,
    role: str | None,
    variant_id: str | None,
    price_table: PriceTable | None = None,
) -> dict[str, Any]:
    """Fetch the ledger, reduce it, and join per-variant / per-idea facts.

    ``price_table`` enables derivation for entries no provider priced.
    Derived dollars stay in their own fields all the way out to the JSON,
    and ``price_table`` provenance is echoed into the report so a figure
    can be audited against the rates that produced it.
    """
    entries = client.list_cost_entries(role=role, variant_id=variant_id)
    summary = summarize(experiment_id, entries, price_table=price_table)
    variants = variant_facts(client, sorted(summary.by_variant))
    ideas = idea_facts(client, sorted(summary.by_idea))
    # Which variants each idea produced, straight from the per-variant
    # join — an idea with no variants (or only errored ones) is the
    # "paid for nothing" case the report exists to surface.
    variants_by_idea: dict[str, list[str]] = {}
    for vid, facts in variants.items():
        if facts["idea_id"] is not None:
            variants_by_idea.setdefault(facts["idea_id"], []).append(vid)
    return {
        "experiment_id": experiment_id,
        "filters": {"role": role, "variant_id": variant_id},
        "price_table": summary.price_table,
        "totals": {**summary.totals.model_dump(mode="json"), "basis": summary.totals.basis},
        "unattributed": summary.unattributed,
        "by_role": {
            name: {**totals.model_dump(mode="json"), "basis": totals.basis}
            for name, totals in sorted(summary.by_role.items())
        },
        "by_variant": [
            {
                "variant_id": vid,
                **variants[vid],
                "cost": summary.by_variant[vid].model_dump(mode="json"),
                "basis": summary.by_variant[vid].basis,
            }
            for vid in sorted(summary.by_variant)
        ],
        "by_idea": [
            {
                "idea_id": iid,
                **ideas[iid],
                "variant_ids": sorted(variants_by_idea.get(iid, [])),
                "cost": summary.by_idea[iid].model_dump(mode="json"),
                "basis": summary.by_idea[iid].basis,
            }
            for iid in sorted(summary.by_idea)
        ],
        "by_model": [
            {
                "model": name,
                "cost": totals.model_dump(mode="json"),
                "basis": totals.basis,
            }
            for name, totals in sorted(summary.by_model.items())
        ],
        "pricing_gaps": _pricing_gaps(entries, price_table),
        "entries": [entry.to_payload() for entry in entries],
    }


def _pricing_gaps(
    entries: list[CostEntry], price_table: PriceTable | None
) -> list[dict[str, Any]]:
    """Per-entry pricing shortfalls, so a gap is visible not inferred.

    Only entries that are actually short appear: a reported figure has
    nothing to explain, and an entry priced cleanly from tokens has no
    gap. An unpriced-and-unexplained run would otherwise look identical
    to a free one.
    """
    gaps: list[dict[str, Any]] = []
    for entry in entries:
        derived = derive_cost(entry, price_table)
        if derived.basis == "reported" or not derived.gaps:
            continue
        gaps.append(
            {
                "entry_id": entry.entry_id,
                "basis": derived.basis,
                "gaps": list(derived.gaps),
            }
        )
    return gaps


def _fmt_usd(value: float) -> str:
    return f"${value:.4f}"


def _render_totals(report: dict[str, Any]) -> list[str]:
    """The headline, plus everything needed to read it honestly."""
    totals = report["totals"]
    lines = [
        f"total: {_fmt_usd(totals['total_cost_usd'])} [{totals['basis']}] over "
        f"{totals['entries']} attempt(s)"
    ]
    if totals["entries_reported"] and totals["entries_derived"]:
        lines.append(
            f"  of which {_fmt_usd(totals['reported_cost_usd'])} was reported by "
            f"the provider ({totals['entries_reported']} attempt(s)) and "
            f"{_fmt_usd(totals['derived_cost_usd'])} was DERIVED from tokens x "
            f"rates ({totals['entries_derived']} attempt(s))"
        )
    elif totals["entries_derived"]:
        lines.append(
            f"  all of it DERIVED from tokens x rates "
            f"({totals['entries_derived']} attempt(s)) — no provider reported a "
            "dollar figure"
        )
    if totals["entries_unpriced"]:
        lines.append(
            f"  {totals['entries_unpriced']} attempt(s) could not be priced at "
            "all: this total is a FLOOR, not a total (see pricing_gaps)"
        )
    table = report.get("price_table")
    if table:
        unfilled = "" if table.get("usable") == "yes" else " — UNFILLED, nothing derived"
        lines.append(
            f"  rates: {table['source']} (as of {table['as_of']}, "
            f"{table['unit']}){unfilled}"
        )
    else:
        lines.append("  rates: none supplied — reported figures only")
    return lines


def _render_roles(report: dict[str, Any]) -> list[str]:
    # `unpriced` and `basis` are per-row rather than only in the header:
    # a reader scanning rows must not have to remember a caveat printed
    # a dozen lines above them.
    lines = [
        "",
        f"{'role':<12} {'attempts':>8} {'unpriced':>8} {'usd':>12} "
        f"{'basis':<9} {'in_tok':>12} {'out_tok':>10}",
    ]
    for name, row in report["by_role"].items():
        lines.append(
            f"{name:<12} {row['entries']:>8} {row['entries_unpriced']:>8} "
            f"{_fmt_usd(row['total_cost_usd']):>12} {row['basis']:<9} "
            f"{row['input_tokens']:>12} {row['output_tokens']:>10}"
        )
    return lines


def _render_models(report: dict[str, Any]) -> list[str]:
    if not report["by_model"]:
        return []
    lines = [
        "",
        f"{'model':<34} {'slices':>7} {'usd':>12} {'basis':<9} "
        f"{'in_tok':>12} {'out_tok':>10} {'cache_rd':>10}",
    ]
    for row in report["by_model"]:
        cost = row["cost"]
        lines.append(
            f"{row['model']:<34} {cost['entries']:>7} "
            f"{_fmt_usd(cost['total_cost_usd']):>12} {row['basis']:<9} "
            f"{cost['input_tokens']:>12} {cost['output_tokens']:>10} "
            f"{cost['cache_read_input_tokens']:>10}"
        )
    unattributed = (report.get("unattributed") or {}).get("by_model")
    if unattributed:
        lines.append(
            f"  ({unattributed} attempt(s) reported no per-model split and are "
            "absent from this table)"
        )
    return lines


def _render_variants(report: dict[str, Any]) -> list[str]:
    lines = [
        "",
        f"{'variant':<26} {'status':<18} {'usd':>12} {'basis':<9} "
        f"{'evaluation':<30}",
    ]
    for row in report["by_variant"]:
        evaluation = row["evaluation"]
        lines.append(
            f"{row['variant_id']:<26} {str(row['status']):<18} "
            f"{_fmt_usd(row['cost']['total_cost_usd']):>12} {row['basis']:<9} "
            f"{json.dumps(evaluation) if evaluation else '-':<30}"
        )
    return lines


def _render_ideas(report: dict[str, Any]) -> list[str]:
    """Per-idea spend + what it produced — the paid-for-nothing view."""
    lines = [
        "",
        f"{'idea':<26} {'slug':<14} {'state':<11} {'usd':>12} {'basis':<9} "
        f"{'variants':>8}",
    ]
    for row in report["by_idea"]:
        # `basis` per row for the same reason the role table has it: a
        # $0.0000 row is "we couldn't price it", not "it was free".
        lines.append(
            f"{row['idea_id']:<26} {str(row['slug']):<14} "
            f"{str(row['state']):<11} "
            f"{_fmt_usd(row['cost']['total_cost_usd']):>12} {row['basis']:<9} "
            f"{len(row['variant_ids']):>8}"
        )
    unattributed = (report.get("unattributed") or {}).get("by_idea")
    if unattributed:
        lines.append(
            f"  ({unattributed} attempt(s) are attributable to no single idea "
            "— a dispatch that produced several ideas spent one call on all "
            "of them)"
        )
    return lines


def _render_gaps(report: dict[str, Any]) -> list[str]:
    if not report.get("pricing_gaps"):
        return []
    lines = ["", f"pricing gaps ({len(report['pricing_gaps'])} attempt(s)):"]
    for row in report["pricing_gaps"]:
        lines.extend(f"  {row['entry_id']}: {gap}" for gap in row["gaps"])
    return lines


def render_table(report: dict[str, Any]) -> str:
    """Human-readable rendering; the JSON form is the machine contract.

    Every dollar column is accompanied by its basis, and each bucket
    shows its own unpriced count, so no figure can be read as more
    certain than it is.
    """
    lines = [f"experiment: {report['experiment_id']}"]
    lines.extend(_render_totals(report))
    lines.extend(_render_roles(report))
    lines.extend(_render_models(report))
    lines.extend(_render_variants(report))
    lines.extend(_render_ideas(report))
    lines.extend(_render_gaps(report))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Fetch, reduce, and print the report. Returns a process exit code."""
    args = parse_args(argv)
    table = load_price_table(args.price_table) if args.price_table else None
    if table is not None and not table.is_usable():
        # Loud, because the alternative is a report that silently says
        # "unpriced" while the operator believes they supplied rates.
        print(
            f"warning: price table {args.price_table} is unfilled "
            f"(as_of={table.as_of!r}, {len(table.rates)} model(s)); "
            "nothing will be derived from it",
            file=sys.stderr,
        )
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
            price_table=table,
        )
    if args.format == "table":
        print(render_table(report))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
