#!/usr/bin/env python3
"""Generate a coverage matrix mapping spec MUST/SHOULD claims to citing tests.

Walks every paragraph in spec/v0/*.md, classifies each by RFC 2119
keyword (MUST, MUST NOT, SHOULD, SHOULD NOT, MAY), and records the
section it lives under. Walks every conformance scenario docstring
to collect citations (chapter + section). Produces a markdown table
keyed by (chapter, section, line) with citing-scenario references.

Per chapter 9 §3, only MUSTs are normative conformance contracts;
SHOULDs are interop guidance. The output preserves both for visibility
but classifies them differently.

Output: docs/conformance-coverage.md.

Run: python3 scripts/conformance-coverage.py

WARNING: this script OVERWRITES docs/conformance-coverage.md. The
output ships with manual prose sections above the auto-generated
tables ("How to read the gap list", "Per-claim assertion-coverage
pilot — chapter 04"). Those sections are NOT regenerated; if you
re-run this script, the prose layers must be re-spliced. The simplest
discipline: keep both prose sections at the top of the rendered file,
generate into a temp file, and merge by hand. A future cleanup could
move the prose layers into this script as static templates if the
overwrite cost becomes a real friction.
"""

from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = REPO_ROOT / "spec" / "v0"
SCENARIOS_DIR = REPO_ROOT / "conformance" / "scenarios"
OUT = REPO_ROOT / "docs" / "conformance-coverage.md"

HEADING_RE = re.compile(r"^(?P<hashes>#{2,4})\s+(?P<num>\d+(?:\.\d+)*)(?:[.\s]|$)\s*(?P<title>.*)$")
KEYWORD_RE = re.compile(r"\b(MUST NOT|MUST|SHOULD NOT|SHOULD|MAY)\b")
CITATION_RE = re.compile(
    r"spec/v0/(?P<chapter>[0-9]{2}-[a-z-]+\.md)\s+§(?P<section>\d+(?:\.\d+)*)"
)


def parse_chapter(path: Path) -> list[dict]:
    """Return one entry per RFC2119-bearing line.

    Each entry: {"chapter", "section", "section_title", "line_no",
    "keywords": [...], "text": <paragraph snippet>}.
    Lines outside numbered sections (e.g. preamble) get section "(preamble)".
    """
    entries: list[dict] = []
    section_num = "(preamble)"
    section_title = ""
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        m = HEADING_RE.match(line)
        if m:
            section_num = m.group("num")
            section_title = m.group("title").strip()
            continue
        kws = KEYWORD_RE.findall(line)
        if not kws:
            continue
        entries.append(
            {
                "chapter": path.name,
                "section": section_num,
                "section_title": section_title,
                "line_no": lineno,
                "keywords": kws,
                "text": _trim_paragraph(line),
            }
        )
    return entries


def _trim_paragraph(line: str, max_len: int = 220) -> str:
    """Single-line excerpt for the matrix cell."""
    line = line.strip()
    # Strip leading list bullet / table pipes for readability.
    line = re.sub(r"^[-*|]\s*", "", line)
    line = re.sub(r"\s+\|.*$", "", line)
    line = re.sub(r"`[^`]+`", lambda m: m.group(0), line)  # keep code spans
    if len(line) > max_len:
        line = line[: max_len - 1] + "…"
    return line


def parse_scenarios() -> dict[tuple[str, str], list[str]]:
    """Return {(chapter_filename, section): [scenario_file:test_name, ...]}.

    A scenario covers (chapter, section) if the test docstring's first
    line cites that chapter+section, OR cites a descendant section
    (e.g. §3.2 covers §3 too).
    """
    cov: dict[tuple[str, str], list[str]] = defaultdict(list)
    for f in sorted(SCENARIOS_DIR.glob("test_*.py")):
        try:
            tree = ast.parse(f.read_text(), filename=str(f))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            doc = ast.get_docstring(node) or ""
            first_line = doc.split("\n", 1)[0] if doc else ""
            for m in CITATION_RE.finditer(first_line):
                chapter = m.group("chapter")
                section = m.group("section")
                # Cover the cited section AND every ancestor section.
                parts = section.split(".")
                for i in range(len(parts), 0, -1):
                    ancestor = ".".join(parts[:i])
                    cov[(chapter, ancestor)].append(f"{f.name}::{node.name}")
    return cov


def main() -> int:
    chapters = sorted(SPEC_DIR.glob("*.md"))
    chapters = [c for c in chapters if c.name != "README.md"]

    coverage = parse_scenarios()
    rows_by_chapter: dict[str, list[dict]] = defaultdict(list)
    totals = {"MUST": 0, "MUST NOT": 0, "SHOULD": 0, "SHOULD NOT": 0, "MAY": 0}
    covered_must = 0
    uncovered_must: list[dict] = []

    for ch in chapters:
        entries = parse_chapter(ch)
        for e in entries:
            citing = coverage.get((ch.name, e["section"]), [])
            e["citing"] = sorted(set(citing))
            for kw in e["keywords"]:
                if kw in totals:
                    totals[kw] += 1
            has_must = "MUST" in e["keywords"] or "MUST NOT" in e["keywords"]
            if has_must:
                if e["citing"]:
                    covered_must += 1
                else:
                    uncovered_must.append(e)
            rows_by_chapter[ch.name].append(e)

    # --- Render markdown ---
    out = []
    out.append("# Conformance coverage matrix")
    out.append("")
    out.append("Maps every RFC 2119 keyword line in `spec/v0/*.md` to the conformance")
    out.append("scenarios that cite it. Generated by `scripts/conformance-coverage.py`;")
    out.append("re-run to refresh.")
    out.append("")
    out.append("Per [`spec/v0/09-conformance.md`](../spec/v0/09-conformance.md) §3,")
    out.append("the v1 / v1+roles / v1+roles+integrator suites assert MUSTs only;")
    out.append("SHOULDs and MAYs are interop guidance, not interop contracts. This")
    out.append("matrix surfaces both for visibility but classifies coverage gaps")
    out.append("by keyword level.")
    out.append("")
    out.append("**Coverage rule.** A scenario whose docstring cites")
    out.append("`spec/v0/<chapter>.md §<sec>` covers that section AND every ancestor")
    out.append("section (e.g. a citation of §3.2 also counts as coverage of §3).")
    out.append("This mirrors")
    out.append("[`conformance/src/conformance/tools/check_citations.py`](../conformance/src/conformance/tools/check_citations.py)'s")
    out.append("section-walk logic.")
    out.append("")
    out.append("**What this matrix is NOT.** A scenario citing a section is not")
    out.append("proof that every MUST in that section is *exercised* by the test —")
    out.append("only that the test claims authority from somewhere in that section.")
    out.append("A weak-coverage column would require reading each test body; that")
    out.append("step is recommended as a follow-up audit per finding #24.")
    out.append("")
    out.append("## Summary")
    out.append("")
    out.append(f"- Total RFC 2119 keyword lines: **{sum(totals.values())}**")
    for kw, n in totals.items():
        out.append(f"  - {kw}: {n}")
    must_total = totals["MUST"] + totals["MUST NOT"]
    out.append("")
    out.append(
        f"- MUST/MUST-NOT lines with at least one citing scenario: "
        f"**{covered_must} / {must_total}** "
        f"({100*covered_must/must_total:.0f}% line-coverage)"
    )
    out.append(
        f"- MUST/MUST-NOT lines with NO citing scenario: "
        f"**{len(uncovered_must)}**"
    )
    out.append("")
    out.append(
        "Line-coverage is a lower bound on assertion-coverage: many lines "
        "carry multiple distinct claims (e.g. a row that says \"MUST X and "
        "MUST NOT Y\" counts as one line). A future pass should split lines "
        "into individual claims before classifying gap level."
    )
    out.append("")

    # --- Top of matrix: uncovered MUSTs grouped by chapter ---
    out.append("## Uncovered MUST / MUST NOT lines (priority)")
    out.append("")
    if not uncovered_must:
        out.append("None.")
    else:
        out.append(
            "Each row is a line in the spec that contains a MUST or MUST NOT "
            "and has no scenario citing its section (or any ancestor)."
        )
        out.append("")
        unc_by_chapter: dict[str, list[dict]] = defaultdict(list)
        for e in uncovered_must:
            unc_by_chapter[e["chapter"]].append(e)
        out.append("| Chapter | § | Line | Excerpt |")
        out.append("|---|---|---|---|")
        for ch in sorted(unc_by_chapter):
            for e in unc_by_chapter[ch]:
                anchor = e["section"]
                excerpt = e["text"].replace("|", "\\|")
                out.append(
                    f"| `{ch}` | §{anchor} | {e['line_no']} | {excerpt} |"
                )
    out.append("")

    # --- Per-chapter detailed matrix ---
    out.append("## Full matrix by chapter")
    out.append("")
    out.append(
        "Within each chapter, lines are listed in order. The 'covered by' "
        "column lists `scenario_file::test_name` entries; an empty cell "
        "means no scenario cites this section or any ancestor."
    )
    out.append("")
    for ch in sorted(rows_by_chapter):
        out.append(f"### `{ch}`")
        out.append("")
        out.append("| § | Line | Keyword(s) | Excerpt | Covered by |")
        out.append("|---|---|---|---|---|")
        for e in rows_by_chapter[ch]:
            kws = ", ".join(sorted(set(e["keywords"])))
            excerpt = e["text"].replace("|", "\\|")
            citing = e["citing"]
            if citing:
                # Truncate long lists for readability.
                if len(citing) > 4:
                    cell = ", ".join(f"`{c}`" for c in citing[:3]) + f", *+{len(citing)-3} more*"
                else:
                    cell = ", ".join(f"`{c}`" for c in citing)
            else:
                cell = ""
            out.append(
                f"| §{e['section']} | {e['line_no']} | {kws} | {excerpt} | {cell} |"
            )
        out.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Strip the trailing empty line each section appends so the file ends with
    # exactly one terminating newline (markdownlint MD012).
    while out and out[-1] == "":
        out.pop()
    OUT.write_text("\n".join(out) + "\n")
    print(f"wrote {OUT.relative_to(REPO_ROOT)}", file=sys.stderr)
    print(
        f"  {sum(totals.values())} keyword lines; "
        f"{covered_must}/{must_total} MUST lines covered "
        f"({100*covered_must/must_total:.0f}%); "
        f"{len(uncovered_must)} MUST gaps",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
