#!/usr/bin/env python3
"""Fail if pre-rename EDEN vocabulary appears outside an allowlist.

The directed-evolution rename (PR #45) replaced:

  planner    -> ideator      Proposal -> Idea
  implementer-> executor      Trial    -> Variant
  metrics    -> evaluation    (on EvaluateSubmission / Variant)
  plan       -> ideate        (task kind)
  implement  -> execute       (task kind)

Subsequent commits have re-introduced legacy vocab in doc-touching
PRs (PR #46, #49). This script is the regression guard: it greps the
working tree for legacy tokens and exits non-zero on any hit not on
the allowlist.

Usage:
    scripts/check-rename-discipline.py [--write-baseline]

The optional ``--write-baseline`` flag dumps the full hit set so an
operator can audit what would be allow-listed if they knowingly
introduce a new "intentional historical" reference.

Exit codes:
    0  no findings
    1  legacy vocab found outside the allowlist
    2  setup error
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Files explicitly allowed to contain pre-rename vocabulary. These
# are intentional historical references that survive the rename:
#
# - Plan / review docs from before the rename.
# - The rename plan itself.
# - Glossary explanatory text and fixture-history prose.
# - AGENTS.md / CLAUDE.md, which cite past rename mistakes as
#   lessons-learned.
# - Phase plan and review records (untouched by the rename pass per
#   the plan's "Tricky areas" section).
#
# Adding a path here is a deliberate act: it carves out an exception.
ALLOWLIST_PATHS: tuple[str, ...] = (
    "docs/archive/",
    "docs/plans/eden-phase-",
    "docs/plans/review/",
    "docs/plans/rename-to-directed-evolution.md",
    "docs/plans/eden-protocol-bootstrap.md",
    "MANUAL_UI_ISSUES.md",
    "tests/fixtures/experiment/README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "scripts/check-rename-discipline.py",  # this file documents the names
)

# Always-skip directories.
SKIP_DIR_FRAGMENTS: tuple[str, ...] = (
    "/.git/", "/.venv/", "/node_modules/", "/__pycache__/",
)

# File extensions in scope.
INCLUDE_EXTS: tuple[str, ...] = (
    ".py", ".md", ".json", ".yaml", ".yml", ".html", ".j2",
    ".toml", ".sh", ".cfg", ".ini", ".css",
)
INCLUDE_BASENAMES_PREFIX: tuple[str, ...] = ("Dockerfile",)


# Patterns labelled by category. Each is a compiled regex; a hit on
# any one of them outside the allowlist is a CI failure.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Misspellings (-or instead of -er, both pre- and post-rename).
    ("implementor",      re.compile(r"\bimplementor[s]?\b", re.IGNORECASE)),
    # Old role nouns (any case, any context — including snake_case
    # compounds and CamelCase compounds).
    ("planner",          re.compile(r"(?i)planner")),
    ("implementer",      re.compile(r"(?i)implementer")),
    # Old artifact nouns. ``trial`` excludes English meanings ("trial
    # run", "court trial") via a small negative-lookahead; ``proposal``
    # has no such collision in this codebase.
    ("proposal",         re.compile(r"(?i)proposal")),
    ("trial",            re.compile(
        r"(?i)\btrial(?!ed|ing|s? run|s? phase|s? participant|s? court)"
    )),
    # Storage / config keys.
    ("metrics_schema",   re.compile(r"\bmetrics_schema\b")),
    ("MetricsSchema",    re.compile(r"\bMetricsSchema\b")),
    ("plan_command",     re.compile(r"\bplan_command\b")),
    ("implement_command",re.compile(r"\bimplement_command\b")),
    ("parallel_trials",  re.compile(r"\bparallel_trials\b")),
    ("max_trials",       re.compile(r"\bmax_trials\b")),
    # CLI flags / env vars.
    ("--plan-tasks",     re.compile(r"--plan-tasks\b")),
    ("--implement-",     re.compile(r"--implement-[a-z]")),
    ("--plan-",          re.compile(r"--plan-[a-z]")),
    ("EDEN_PLAN_TASKS",  re.compile(r"\bEDEN_PLAN_TASKS\b")),
    ("EDEN_PROPOSALS_PER_PLAN", re.compile(r"\bEDEN_PROPOSALS_PER_PLAN\b")),
    # Refs / paths.
    ("/proposals path",  re.compile(r"/proposals(?:/|\b|\")")),
    ("/trials path",     re.compile(r"/trials(?:/|\b|\")")),
    ("trial/* ref",      re.compile(r"trial/\*")),
    ("refs/heads/trial/",re.compile(r"refs/heads/trial/")),
    # Submission/Task class names.
    ("PlanSubmission",       re.compile(r"\bPlanSubmission\b")),
    ("ImplementSubmission",  re.compile(r"\bImplementSubmission\b")),
    ("PlanTask",             re.compile(r"\bPlanTask\b")),
    ("ImplementTask",        re.compile(r"\bImplementTask\b")),
    ("PlanPayload",          re.compile(r"\bPlanPayload\b")),
    ("ImplementPayload",     re.compile(r"\bImplementPayload\b")),
    ("PlanOutcome",          re.compile(r"\bPlanOutcome\b")),
    ("ImplementOutcome",     re.compile(r"\bImplementOutcome\b")),
    # Quoted task-kind values (string literals). Both pre-rename
    # ("plan"/"implement") and the verb-form ("ideate"/"execute") that
    # the second-pass cleanup replaced with the gerund noun.
    ('"plan" string',      re.compile(r'"plan"')),
    ('"implement" string', re.compile(r'"implement"')),
    ('"ideate" string',    re.compile(r'"ideate"')),
    ('"execute" string',   re.compile(r'"execute"')),
    # Numbered task-ID prefixes from before each rename.
    ("plan-NNNN id",     re.compile(r"\bplan-\d{4,}\b")),
    ("implement-NNNN id",re.compile(r"\bimplement-\d{4,}\b")),
    ("ideate-NNNN id",   re.compile(r"\bideate-\d{4,}\b")),
    ("execute-NNNN id",  re.compile(r"\bexecute-\d{4,}\b")),
    # Submission classes — verb-prefix forms were replaced by
    # artifact-noun-prefix forms in the cleanup pass.
    ("IdeateSubmission",   re.compile(r"\bIdeateSubmission\b")),
    ("ExecuteSubmission",  re.compile(r"\bExecuteSubmission\b")),
    ("EvaluateSubmission", re.compile(r"\bEvaluateSubmission\b")),
    # Task / Payload / Outcome class prefixes — verb-form was
    # replaced by gerund (Ideate -> Ideation, Execute -> Execution,
    # Evaluate -> Evaluation).
    ("Ideate<X> class",    re.compile(r"\bIdeate(?:Task|Payload|Outcome|Draft)\b")),
    ("Execute<X> class",   re.compile(r"\bExecute(?:Task|Payload|Outcome|Draft)\b")),
    ("Evaluate<X> class",  re.compile(r"\bEvaluate(?:Task|Payload|Outcome|Draft)\b")),
    # Helper-name verb-suffix (replaced by artifact-noun forms).
    ("submit_<verb>",      re.compile(r"\bsubmit_(?:ideate|execute|evaluate)\b")),
    # Bare task-kind in code-fence-ish position. Both pre-rename
    # forms (kind=plan / kind=implement) and the verb-form
    # (kind=ideate / kind=execute) that the second pass replaced.
    ("kind=plan",        re.compile(r"\bkind\s*=\s*plan\b")),
    ("kind=implement",   re.compile(r"\bkind\s*=\s*implement\b")),
    ("kind=ideate",      re.compile(r"\bkind\s*=\s*ideate\b")),
    ("kind=execute",     re.compile(r"\bkind\s*=\s*execute\b")),
    # Verb-as-noun-head: a renamed task-kind verb directly followed by
    # an action noun reads awkwardly ("submit_ideate", "ideate
    # reclamation"). Catch these so future commits can't reintroduce
    # them. The clean form is either the artifact noun (`submit_idea`,
    # `submit_evaluation`) or a hyphenated kind-modifier
    # (`ideation-task reclamation`).
    ("verb noun-head", re.compile(
        r"(?i)\b(?:plan|implement|ideate|execute|evaluate) "
        r"(?:reclamation|submit|submission|dispatch|terminal|reject|accept)\b"
    )),
]


def is_allowlisted(rel: str) -> bool:
    """True if a path is on the legacy-vocabulary allowlist."""
    return any(rel.startswith(p) or rel == p.rstrip("/") for p in ALLOWLIST_PATHS)


def included_file(path: Path) -> bool:
    """True if a file's extension or name brings it into scope for the scan."""
    if not path.is_file():
        return False
    if path.suffix in INCLUDE_EXTS:
        return True
    return any(path.name.startswith(p) for p in INCLUDE_BASENAMES_PREFIX)


def scan() -> list[tuple[str, int, str, str]]:
    """Return list of (relpath, lineno, label, snippet)."""
    findings: list[tuple[str, int, str, str]] = []
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in (".git", ".venv", "node_modules", "__pycache__")]
        s = dp + "/"
        if any(frag in s for frag in SKIP_DIR_FRAGMENTS):
            continue
        for fn in fns:
            p = Path(dp) / fn
            if not included_file(p):
                continue
            rel = str(p.relative_to(ROOT))
            if is_allowlisted(rel):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                for label, rx in PATTERNS:
                    if rx.search(line):
                        findings.append((rel, i, label, line.strip()[:160]))
                        break  # one finding per line is enough
    return findings


def main() -> int:
    """CLI entry point. See module docstring for behavior + exit codes."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Print all findings instead of failing — useful when extending the allowlist.",
    )
    args = parser.parse_args()

    findings = scan()
    if not findings:
        print("rename-discipline: clean (no legacy EDEN vocabulary outside the allowlist)")
        return 0

    if args.write_baseline:
        print(f"rename-discipline baseline ({len(findings)} hits):")
        for rel, lineno, label, snippet in findings:
            print(f"  {rel}:{lineno}  [{label}]  {snippet}")
        return 0

    print(
        "rename-discipline: FAIL — legacy EDEN vocabulary detected outside the allowlist.\n"
        "Pre-rename names (planner / implementer / proposal / trial / metrics) MUST NOT\n"
        "appear in normal source/spec/doc files. See docs/plans/rename-to-directed-evolution.md\n"
        "for the naming map. To carve out an exception, edit ALLOWLIST_PATHS in this script\n"
        "with a justification in the commit message.\n",
        file=sys.stderr,
    )
    for rel, lineno, label, snippet in findings:
        print(f"  {rel}:{lineno}  [{label}]  {snippet}", file=sys.stderr)
    print(f"\nTotal: {len(findings)} hit(s).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
