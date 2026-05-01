"""Verify every conformance scenario cites a real spec MUST.

Walks every test under ``conformance/scenarios/`` (excluding ``_meta/``),
extracts the first docstring line, parses out
``spec/v0/<chapter>.md §<sec>``, and asserts:

1. The cited section exists in the target chapter.
2. The cited section (or any ancestor section in the same chapter)
   contains a normative MUST. Per chapter 9 §3, the suite asserts
   MUSTs only; SHOULDs and MAYs are interop guidance, not interop
   contracts. The hierarchy walk lets a §3.2-level citation inherit
   from a §3-level MUST.
3. Every chapter-9 §5 scenario-index group has at least one citing test.

Exit 0 on clean, 1 on unresolved citations, 2 on setup errors.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SPEC_DIR = REPO_ROOT / "spec" / "v0"
SCENARIOS_DIR = REPO_ROOT / "conformance" / "scenarios"

CITATION_RE = re.compile(
    r"spec/v0/(?P<chapter>[0-9]{2}-[a-z-]+\.md)\s+§(?P<section>\d+(?:\.\d+)*)"
)
HEADING_RE = re.compile(r"^(?P<hashes>#{2,4})\s+(?P<num>\d+(?:\.\d+)*)(?:[.\s]|$)")
MUST_RE = re.compile(r"\bMUST\b")

# Spec citation parsed from §5 index rows.
# Example: "[`04-task-protocol.md`](04-task-protocol.md) §1, §3, §4"
# — match the link + chain of §refs, then peel each §ref off the chain.
INDEX_LINK_RE = re.compile(
    r"\]\((?P<chapter>[0-9]{2}-[a-z-]+\.md)\)"
    r"(?P<chain>\s+§\d+(?:\.\d+)*(?:\s*,\s*§\d+(?:\.\d+)*)*)"
)
SECTION_REF_RE = re.compile(r"§(\d+(?:\.\d+)*)")


def _heading_depth(num: str) -> int:
    return num.count(".") + 1


def parse_chapter_9_index() -> list[tuple[str, str, set[tuple[str, str]]]]:
    """Parse the §5 scenario-index table from chapter 9.

    Returns a list of ``(group_name, scope_text, citations)`` tuples
    where ``citations`` is the set of ``(chapter_filename, section)``
    pairs each group cites. Coverage requires at least one valid
    citing test for each *group*, where "valid" means a test whose
    own citation is to one of the group's chapters/sections (or any
    descendant of one).
    """
    chapter_9 = SPEC_DIR / "09-conformance.md"
    text = chapter_9.read_text()
    # Locate the section index table. The §5 heading is "## 5. ..."
    # and the table runs until the next heading of <= depth.
    index_re = re.compile(r"^##\s+5\.[^\n]*\n", re.MULTILINE)
    m = index_re.search(text)
    if not m:
        raise RuntimeError("chapter 9 §5 not found")
    body = text[m.end() :]
    next_heading = re.search(r"^##\s+\d+\.", body, re.MULTILINE)
    if next_heading:
        body = body[: next_heading.start()]
    # Each row is like "| Group name | Scope. | citations |" — three
    # pipe-delimited columns plus leading/trailing pipes.
    groups: list[tuple[str, str, set[tuple[str, str]]]] = []
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) != 3:
            continue
        group, scope, raw_cites = cols
        if group in {"Group", "---"} or not group:
            continue
        cites: set[tuple[str, str]] = set()
        for cm in INDEX_LINK_RE.finditer(raw_cites):
            chapter_name = cm.group("chapter")
            for ref in SECTION_REF_RE.finditer(cm.group("chain")):
                cites.add((chapter_name, ref.group(1)))
        if cites:
            groups.append((group, scope, cites))
    return groups


def _section_or_ancestor_has_must(
    sections: dict[str, str], section: str
) -> bool:
    r"""Check that the section, or any ancestor in the same chapter, has MUST.

    Walks from the cited section up to its top-level parent; returns
    True if any of those texts contains a `\bMUST\b` token.
    """
    parts = section.split(".")
    while parts:
        candidate = ".".join(parts)
        text = sections.get(candidate)
        if text and MUST_RE.search(text):
            return True
        parts.pop()
    return False


def build_section_text_index() -> dict[str, dict[str, str]]:
    """Return ``{filename: {section_num: text}}`` for every spec heading.

    The "text" is everything from the heading line through (but not
    including) the next heading whose depth is the same or shallower.
    This lets the MUST-check examine the full section body.
    """
    index: dict[str, dict[str, str]] = {}
    for f in sorted(SPEC_DIR.glob("*.md")):
        sections: dict[str, str] = {}
        lines = f.read_text().splitlines()
        # Pre-extract heading positions.
        positions: list[tuple[int, str, int]] = []
        for i, line in enumerate(lines):
            m = HEADING_RE.match(line)
            if m:
                positions.append((i, m.group("num"), _heading_depth(m.group("num"))))
        for idx, (start, num, depth) in enumerate(positions):
            end = len(lines)
            for j in range(idx + 1, len(positions)):
                _next_start, _next_num, next_depth = positions[j]
                if next_depth <= depth:
                    end = positions[j][0]
                    break
            sections[num] = "\n".join(lines[start:end])
        index[f.name] = sections
    return index


def iter_test_functions(path: Path):
    """Yield (path, function_name, first_docstring_line) for every test_*."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            doc = ast.get_docstring(node)
            first_line = doc.splitlines()[0] if doc else ""
            yield (path, node.name, first_line)


def _matches_group_citation(
    test_chapter: str,
    test_section: str,
    group_cites: set[tuple[str, str]],
) -> bool:
    """Check whether a test citation lies inside one of an index group's pairs.

    A test matches an index group if its citation is at one of the
    group's chapter+section pairs OR a descendant section in the
    same chapter.
    """
    test_parts = test_section.split(".")
    for group_chapter, group_section in group_cites:
        if test_chapter != group_chapter:
            continue
        gs = group_section.split(".")
        if test_parts[: len(gs)] == gs:
            return True
    return False


def read_module_conformance_group(path: Path) -> str | None:
    """Read the module-level ``CONFORMANCE_GROUP`` constant from a scenario file.

    Returns the string value or ``None`` if the constant is missing
    (or set to a non-string).
    """
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if (
                isinstance(target, ast.Name)
                and target.id == "CONFORMANCE_GROUP"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                return node.value.value
    return None


def main() -> int:
    """Run the citation check and report findings."""
    if not SCENARIOS_DIR.is_dir():
        print(f"FAIL: {SCENARIOS_DIR} not found", file=sys.stderr)
        return 2
    spec_index = build_section_text_index()
    try:
        chapter_9_groups = parse_chapter_9_index()
    except RuntimeError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2
    failures: list[str] = []
    coverage: dict[str, list[tuple[Path, str, str]]] = {
        chapter: [] for chapter in spec_index
    }
    # Group identity per scenario file: tests are matched to chapter-9
    # §5 groups via this explicit mapping rather than citation overlap.
    # (path, test_name, chapter, section) for every test that cites a
    # valid MUST. Used by the per-group coverage + relevance checks.
    group_to_valid_tests: dict[str, list[tuple[Path, str, str, str]]] = {}
    file_groups: dict[Path, str | None] = {}
    for test_file in sorted(SCENARIOS_DIR.glob("test_*.py")):
        file_group = read_module_conformance_group(test_file)
        file_groups[test_file] = file_group
        if file_group is None:
            failures.append(
                f"{test_file.relative_to(REPO_ROOT)}: missing module-level "
                f"`CONFORMANCE_GROUP = '<group name>'` constant. Required "
                f"so the chapter-9 §5 coverage check can map this file to "
                f"its index group."
            )
        for path, name, first_line in iter_test_functions(test_file):
            m = CITATION_RE.search(first_line)
            if not m:
                failures.append(
                    f"{path.relative_to(REPO_ROOT)}::{name}: docstring "
                    f"first line missing 'spec/v0/<chapter>.md §<sec>' citation:\n"
                    f"    {first_line!r}"
                )
                continue
            chapter = m.group("chapter")
            section = m.group("section")
            sections = spec_index.get(chapter)
            if sections is None:
                failures.append(
                    f"{path.relative_to(REPO_ROOT)}::{name}: cites unknown "
                    f"chapter {chapter}"
                )
                continue
            text = sections.get(section)
            if text is None:
                failures.append(
                    f"{path.relative_to(REPO_ROOT)}::{name}: cites "
                    f"{chapter} §{section} (no such section in target)"
                )
                continue
            if not _section_or_ancestor_has_must(sections, section):
                failures.append(
                    f"{path.relative_to(REPO_ROOT)}::{name}: cites "
                    f"{chapter} §{section}, but neither that section nor "
                    f"any ancestor contains a normative MUST. Per "
                    f"chapter 9 §3 the suite asserts MUSTs only; either "
                    f"cite a different section or strengthen the spec."
                )
                continue
            coverage[chapter].append((path, name, section))
            file_group = file_groups.get(path)
            if file_group is not None:
                group_to_valid_tests.setdefault(file_group, []).append(
                    (path, name, chapter, section)
                )
    # Index-group coverage check — combines TWO conditions: a scenario
    # file must declare CONFORMANCE_GROUP matching the index group AND
    # at least one of its tests must cite a section that the index
    # entry calls out (or a descendant of one). Identity alone (the
    # old fix) would let a file declare any group regardless of what
    # its tests cite; citation overlap alone (the older fix) would
    # let groups bleed into each other when they share citations.
    chapter_9_group_names = {g for (g, _s, _c) in chapter_9_groups}
    declared_groups = {g for g in file_groups.values() if g}
    unknown = declared_groups - chapter_9_group_names
    if unknown:
        failures.append(
            f"scenario files declare CONFORMANCE_GROUP values that are "
            f"not in chapter 9 §5: {sorted(unknown)}"
        )
    cites_by_group = {g: cites for (g, _s, cites) in chapter_9_groups}
    # Per-test relevance check: every test's citation must match its
    # declared group's index entries.
    for group, entries in group_to_valid_tests.items():
        cites = cites_by_group.get(group)
        if cites is None:
            continue  # already reported as unknown above
        for path, name, chapter, section in entries:
            if not _matches_group_citation(chapter, section, cites):
                failures.append(
                    f"{path.relative_to(REPO_ROOT)}::{name}: cites "
                    f"{chapter} §{section}, but its CONFORMANCE_GROUP "
                    f"is {group!r}, whose chapter-9 §5 entry calls out: "
                    f"{sorted(f'{c} §{s}' for (c, s) in cites)}. Either "
                    f"re-cite to one of those (or a descendant) or move "
                    f"the test to a file whose CONFORMANCE_GROUP matches "
                    f"its citation."
                )
    # Per-group existence check: every chapter-9 §5 group needs at
    # least one valid citing test that ALSO declares itself as that
    # group AND cites a relevant section.
    for group, _scope, cites in chapter_9_groups:
        relevant = [
            (path, name)
            for (path, name, ch, sec) in group_to_valid_tests.get(group, [])
            if _matches_group_citation(ch, sec, cites)
        ]
        if not relevant:
            failures.append(
                f"chapter 9 §5 index group {group!r}: no valid citing "
                f"test declares CONFORMANCE_GROUP = {group!r} AND cites "
                f"one of: "
                f"{sorted(f'{c} §{s}' for (c, s) in cites)}."
            )
    if failures:
        print("Citation-check FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    total = sum(len(v) for v in coverage.values())
    print(f"OK: {total} scenarios cite valid spec MUSTs.")
    for chapter in sorted(coverage):
        if not coverage[chapter]:
            continue
        sections = sorted({s for (_p, _n, s) in coverage[chapter]})
        print(f"  {chapter}: §§ {', '.join(sections)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
