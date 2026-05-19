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
# are intentional historical references whose entire purpose is to
# preserve the old vocabulary verbatim:
#
# - Archived docs and pre-rename plan / review records.
# - The rename plan itself.
# - The fixture-history prose explaining what the fixture used to
#   look like before the rename.
# - This file: the script's own pattern definitions reference the
#   old names by necessity.
#
# AGENTS.md / CLAUDE.md are NOT allowlisted. They are
# active project documentation and MUST stay current with the
# vocabulary; phase narratives there should be updated when names
# change. The only legitimate references to old vocab in AGENTS.md
# are explicit lesson-learned citations (e.g., "the
# `EDEN_PLAN_TASKS → EDEN_IDEATE_TASKS` mistake taught us X"), and
# those should be kept inside backtick-fenced code spans so they
# read as quoted citations rather than live references. The
# guardrail's pattern set already exempts most code-fenced content;
# if a citation trips the guardrail, the right fix is usually to
# tighten the prose around the quoted token, not to allowlist the
# file.
#
# Adding a path here is a deliberate act: it carves out an exception.
ALLOWLIST_PATHS: tuple[str, ...] = (
    "docs/archive/",
    "docs/plans/eden-phase-",
    "docs/plans/review/",
    "CHANGELOG.md",  # historical per-chunk completion record; preserves pre-rename verbatim text
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


# Shared sub-patterns used by multiple groups below.
_SHOUTING_SUFFIXES = (
    r"(?:TASKS?|COMPLETED|PENDING|IN_PROGRESS|FAILED|ERRORED|FN|COMMAND|"
    r"PREFIX|COUNT|IDS?)"
)
_KEBAB_SUFFIXES = (
    r"(?:task|tasks|completed|started|errored|fn|command|prefix|test)"
)
_KEBAB_SUFFIXES_IDEATE = (
    r"(?:task|tasks|completed|started|errored|fn|command|prefix|"
    r"test|done|outcome)"
)
_VERB_ID_SUFFIXES = (
    r"(?:pending|completed|submitted|in_progress|failed|errored|"
    r"task[a-z_]*|then_[a-z_]*)"
)
_VERB_ID_SUFFIXES_ACCEPT = (
    r"(?:pending|completed|submitted|in_progress|failed|errored|"
    r"task[a-z_]*|acceptance)"
)


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
    # Backticked task-kind values in spec/docstring prose. Earlier
    # rename passes used \b-anchored regex which doesn't fire on
    # backticked tokens (the backtick is a word boundary but the
    # literal pattern then needs the kind value as a standalone
    # word, which it is — yet `\bplan\b` matched `plan_command` /
    # `parallel_trials` snake_case compounds and was patched around
    # rather than tightened). This pattern catches the
    # backticked-prose form: "for ``execute`` tasks" / "create an
    # ``evaluate`` task" / "kind (one of `plan`, `implement`,
    # `evaluate`)". The (single|double) backtick alternation handles
    # both spec-prose (`X`) and Python-docstring-prose (``X``).
    ("`plan` backticked",      re.compile(r"`{1,2}plan`{1,2}")),
    ("`implement` backticked", re.compile(r"`{1,2}implement`{1,2}")),
    ("`ideate` backticked",    re.compile(r"`{1,2}ideate`{1,2}")),
    ("`execute` backticked",   re.compile(r"`{1,2}execute`{1,2}")),
    ("`evaluate` backticked",  re.compile(r"`{1,2}evaluate`{1,2}")),
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
        r"(?:reclamation|submit|submission|dispatch|terminal|reject|accept|task|tasks)\b"
    )),
    # eval_error wire enum was renamed to evaluation_error. Same for
    # the matching event type variant.eval_errored. The reference
    # impl's evaluation manifest path was renamed from eval.json to
    # evaluation.json and the colloquial "eval manifest" to
    # "evaluation manifest".
    ("eval_error",        re.compile(r"\beval_error\b")),
    ("eval_errored",      re.compile(r"\beval_errored\b")),
    ("VariantEvalErrored",re.compile(r"\bVariantEvalErroredEvent\b")),
    ("eval.json",         re.compile(r"\beval\.json\b")),
    ("eval manifest",     re.compile(r"\beval manifest\b")),
    # promote / promotion / promoted / promoting were retired as
    # synonyms of integrate / integration / integrated / integrating.
    # The integrator integrates; chapter 6 §2 + §3 use "Integration
    # trigger" / "Integration output" as the canonical headings. Any
    # surviving "promote" usage is legacy and should be renamed.
    ("promot*",          re.compile(r"\bpromot(?:e|es|ed|ing|ion|ions)\b", re.IGNORECASE)),

    # Phase 12a-1d (May 2026): the idea's markdown body was renamed
    # from `rationale` to `content` (`rationale` only conveys WHY; the
    # actual document covers WHAT-to-attempt + WHY). Bare English
    # "rationale" / "Rationale" (meaning "reasoning") is still
    # legitimate, so the patterns here are identifier-context only:
    # filenames, snake_case compounds, SHOUTING_CASE constants, kebab
    # CSS classes, and the subprocess-protocol JSON key.
    ("rationale.md",     re.compile(r"\brationale\.md\b")),
    ("rationale_<X>",    re.compile(
        r"\brationale_(?:path|text|markdown|note|max_bytes|completed|errored|from_uri)\b"
    )),
    ("_RATIONALE_<X>",   re.compile(r"\b_RATIONALE_(?:MAX_BYTES|PATH|TEXT)\b")),
    ("<X>_rationale",    re.compile(
        r"\b(?:idea|read_idea|_write|_try_read|_read|_)_rationale\b"
    )),
    ("idea-rationale",   re.compile(r"\bidea-rationale\b")),
    ("rationale-<X>",    re.compile(
        r"\brationale-(?:markdown|note|not-[a-z-]+)\b"
    )),
    ("eden-rationale-",  re.compile(r"\beden-rationale-")),
    ('"rationale" key',  re.compile(r'["\']rationale["\']')),

    # ---------------------------------------------------------------
    # Pattern classes added by the comprehensive audit (May 2026):
    # the prior pattern set enumerated full-word forms only and missed
    # whole categories of legacy. The following classes derive each
    # pattern from the rename map mechanically rather than per-instance.
    # ---------------------------------------------------------------

    # Verb-form snake_case identifiers. The rename retired
    # plan/implement/ideate/execute as task-kind names in favor of
    # gerund nouns (ideation/execution/evaluation), which means any
    # snake_case identifier prefixed with the verb is legacy:
    # `plan_task`, `_plan_task`, `plan_fn`, `make_plan_fn`,
    # `_insert_plan_task`, `handle_plan_task`, `_claim_plan_task`,
    # `plan_task_id_factory`, etc. The leading-underscore variant is
    # included because private helpers and test-fixture factories use
    # it. The trailing-context list is broad on purpose: anything that
    # could plausibly be a noun-suffix in the codebase.
    ("plan_<noun>",      re.compile(
        r"\b_?plan_(?:task|tasks|task_id|task_ids|fn|command|payload|outcome|"
        r"draft|completed|submitted|in_progress|pending|failed|errored|"
        r"reclamation|prefix|spec|count|seed|ids?|then_implement)\b"
    )),
    ("implement_<noun>", re.compile(
        r"\b_?implement_(?:task|tasks|task_id|task_ids|fn|command|payload|"
        r"outcome|draft|completed|submitted|in_progress|pending|failed|"
        r"errored|reclamation|prefix|spec|count|seed|ids?|acceptance)\b"
    )),
    ("evaluate_<noun>",  re.compile(
        r"\b_?evaluate_(?:task|tasks|task_id|task_ids|fn|command|payload|"
        r"outcome|draft|completed|submitted|in_progress|pending|failed|"
        r"errored|reclamation|prefix|spec|count|seed|ids?|acceptance)\b"
    )),
    ("ideate_<noun>",    re.compile(
        r"\b_?ideate_(?:task|tasks|task_id|task_ids|fn|command|payload|"
        r"outcome|draft|completed|submitted|in_progress|pending|failed|"
        r"errored|reclamation|prefix|spec|count|seed|ids?)\b"
    )),
    ("execute_<noun>",   re.compile(
        r"\b_?execute_(?:task|tasks|task_id|task_ids|fn|command|payload|"
        r"outcome|draft|completed|submitted|in_progress|pending|failed|"
        r"errored|reclamation|prefix|spec|count|seed|ids?|acceptance)\b"
    )),

    # Verb-form CamelCase types. Parallel to snake_case above, the
    # gerund-rename retired Plan/Implement/Evaluate as type prefixes for
    # task-protocol shapes. ``Idea`` and ``Variant`` (artifact nouns) are
    # kept where they're submission classes, but the **callback type
    # aliases** (``PlanFn`` / ``ImplementFn`` / ``EvaluateFn``) and the
    # web-ui form ``ImplementDraft`` violate the gerund-coherence rule.
    # ``Ideate<X>`` / ``Execute<X>`` / ``Evaluate<X>`` and the new
    # ``Plan<Fn|Driver|Spec>`` / ``Implement<Fn|Driver|Spec|Config>`` set
    # below extend the prior coverage.
    ("Plan<X>",          re.compile(r"\bPlan(?:Fn|Driver|Spec|Config|Worker|Host)\b")),
    ("Implement<X>",     re.compile(r"\bImplement(?:Fn|Draft|Driver|Spec|Config|Worker|Host)\b")),
    ("Evaluate<X>",      re.compile(r"\bEvaluate(?:Fn|Draft|Driver|Spec|Config|Worker|Host)\b")),
    ("Ideate<X>",        re.compile(r"\bIdeate(?:Fn|Draft|Driver|Spec|Config|Worker|Host)\b")),
    ("Execute<X>",       re.compile(r"\bExecute(?:Fn|Draft|Driver|Spec|Config|Worker|Host)\b")),

    # Verb-form SHOUTING_CASE shell / env vars (PLAN_COMPLETED is the
    # smoke-test family). Same gerund-vs-verb rule applies: the
    # canonical form is IDEATION_*, EXECUTION_*, EVALUATION_*.
    ("PLAN_<X>",         re.compile(r"\bPLAN_" + _SHOUTING_SUFFIXES + r"\b")),
    ("IMPLEMENT_<X>",    re.compile(r"\bIMPLEMENT_" + _SHOUTING_SUFFIXES + r"\b")),
    ("EVALUATE_<X>",     re.compile(r"\bEVALUATE_" + _SHOUTING_SUFFIXES + r"\b")),
    ("IDEATE_<X>",       re.compile(r"\bIDEATE_" + _SHOUTING_SUFFIXES + r"\b")),
    ("EXECUTE_<X>",      re.compile(r"\bEXECUTE_" + _SHOUTING_SUFFIXES + r"\b")),

    # Abbreviation: `impl` always parses to either `implement` (retired
    # verb) or `implementation` (English noun, not a canonical EDEN
    # noun). Either way it's legacy; the canonical abbreviation for the
    # ``execution`` task kind is `exec`. Distinguish from English
    # "implementation" by requiring identifier context (``impl_X`` /
    # ``ImplX`` / ``IMPL_X``).
    #
    # Counterexample by design: ``eval`` is a legitimate abbreviation
    # of the canonical ``evaluation`` gerund and stays. ``prop`` was
    # retired with ``proposal`` → ``idea``, so prop_X is legacy.
    ("impl_<X>",         re.compile(r"\bimpl_[a-z][a-zA-Z_]*\b")),
    ("Impl<X>",          re.compile(r"\bImpl[A-Z][a-zA-Z]*\b")),
    ("IMPL_<X>",         re.compile(r"\bIMPL_[A-Z]")),
    ("prop_<X>",         re.compile(r"\bprop_[a-z][a-zA-Z_]*\b")),
    ("PROP_<X>",         re.compile(r"\bPROP_[A-Z]")),

    # Test-fixture ID slugs (string literals). These embed retired
    # stems in test data: `"tr-1"` (trial), `"prop-x"` (proposal),
    # `"p-1"` (proposal abbrev). Each catches the legacy slug shapes
    # we found in tests/.
    #
    # `"properties":` is a JSON Schema keyword and a known false
    # positive of `prop`-prefix matching; we narrow `prop-` to require a
    # quote/hyphen separator that JSON Schema's `properties` doesn't have.
    ("\"tr-N\" slug",    re.compile(r"['\"]tr-[0-9a-z]+['\"]")),
    ("\"prop-X\" slug",  re.compile(r"['\"][a-z0-9-]*prop-[a-z0-9-]+['\"]")),
    ("\"p-N\" idea slug",re.compile(r"['\"]p-[0-9]+['\"]")),

    # Verb-form kebab-case event-topic prefixes and prose. Parallel to
    # snake_case rule: gerund is canonical for the task-kind family.
    # `ideate-task.completed` → `ideation-task.completed`,
    # `execute-task` → `execution-task`, `evaluate-task` →
    # `evaluation-task`. Catches the bare kebab form too
    # (`ideate-something`).
    ("ideate-<X>",       re.compile(r"\bideate-" + _KEBAB_SUFFIXES_IDEATE + r"\b", re.IGNORECASE)),
    ("execute-<X>",      re.compile(r"\bexecute-" + _KEBAB_SUFFIXES + r"\b", re.IGNORECASE)),
    ("evaluate-<X>",     re.compile(r"\bevaluate-" + _KEBAB_SUFFIXES + r"\b", re.IGNORECASE)),
    ("plan-<X>",         re.compile(r"\bplan-" + _KEBAB_SUFFIXES + r"\b", re.IGNORECASE)),
    ("implement-<X>",    re.compile(r"\bimplement-" + _KEBAB_SUFFIXES + r"\b", re.IGNORECASE)),

    # Test-function names embedding retired verbs. `def
    # test_implement_*` and `def test_plan_*` should follow the
    # gerund-rename: `test_execution_*` / `test_ideation_*`. Note the
    # rule pivots on whether the test exercises the **process / task**
    # (gerund wins) or the **artifact** (idea/variant wins) — and the
    # current test-suite naming is process-side, so gerund is the
    # right replacement.
    ("def test_plan_",   re.compile(r"\bdef test_[a-zA-Z_]*\bplan[a-z_]*\b")),
    ("def test_implement_", re.compile(r"\bdef test_[a-zA-Z_]*\bimplement[a-z_]*\b")),
    ("def test_ideate_", re.compile(r"\bdef test_[a-zA-Z_]*\bideate[a-z_]*\b")),
    ("def test_execute_", re.compile(r"\bdef test_[a-zA-Z_]*\bexecute[a-z_]*\b")),

    # Bare retired verb in citation-like contexts (`"plan_pending"` case-id,
    # `"plan_command"` config key). The string-form catches conformance
    # `cases.py` IDs that embed the retired verb plus a behavioral suffix.
    ("\"plan_<verb>\" id",
        re.compile(r"['\"]plan_" + _VERB_ID_SUFFIXES + r"['\"]")),
    ("\"implement_<verb>\" id",
        re.compile(r"['\"]implement_" + _VERB_ID_SUFFIXES_ACCEPT + r"['\"]")),
    ("\"evaluate_<verb>\" id",
        re.compile(r"['\"]evaluate_" + _VERB_ID_SUFFIXES_ACCEPT + r"['\"]")),

    # Filename citations. `plan.py`, `implement.py` (verb-form fixture
    # names) were renamed to `ideation.py`, `execution.py`. Anywhere
    # docs/tests/docstrings cite the old names is legacy. ``eval.py``
    # was a separate retired form; the fixture is now ``evaluation.py``
    # so cite-form `eval.py` is also legacy. (The runtime artifact
    # ``eval-task.json`` / ``eval-outcome.json`` is unrelated and stays
    # since `eval` is fine as an abbreviation of `evaluation`.)
    ("plan.py cite",     re.compile(r"\bplan\.py\b")),
    ("implement.py cite",re.compile(r"\bimplement\.py\b")),
    ("eval.py cite",     re.compile(r"\beval\.py\b")),

    # Worker-id slug "eval-w" violates the role-based "<role>-w"
    # convention used elsewhere (ideator-w / executor-w /
    # evaluator-w). Should be `evaluator-w`.
    ("\"eval-w\" worker", re.compile(r"['\"]eval-w['\"]")),
]

# Per-line opt-out marker for legitimate citations of legacy patterns
# (e.g., a naming-discipline doc that has to literally write
# `submit_ideate` to teach "don't write submit_ideate"). The marker is
# an HTML comment so it doesn't render in markdown view but is
# greppable in source. Use sparingly — every marker should accompany a
# citation that genuinely needs the literal token. If a section's
# every line carries the marker, that's a sign the section belongs
# in a fully-allowlisted teaching doc instead.
INLINE_CITE_MARKER = "<!-- rename-discipline:cite -->"


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
                if INLINE_CITE_MARKER in line:
                    # Legitimate citation; explicit per-line carve-out.
                    continue
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
