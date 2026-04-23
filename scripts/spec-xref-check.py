#!/usr/bin/env python3
"""Validate §N.M cross-references in ``spec/v0/*.md`` resolve to real sections.

Phase 4's codex-review caught multiple instances of a recurring ripple
failure: a section was renumbered or restructured, and §-references in
neighboring chapters still pointed at the old number. This script catches
that class of drift mechanically.

It parses each spec chapter for numbered section headings, scans all files
for §-references, resolves each reference against its target chapter
(either the nearest preceding ``](FILE.md)`` markdown link — which may
span whitespace/newlines — or the current file for intra-chapter refs),
and reports any whose section number does not exist in the target.

Exit 0 on clean, 1 on unresolved references, 2 on setup errors.
"""

from __future__ import annotations

import bisect
import re
import sys
from pathlib import Path

SPEC_DIR = Path(__file__).resolve().parents[1] / "spec" / "v0"

# Heading forms in the spec:
#   ## 6. Ordering and concurrency
#   ### 1.2 Transitions
#   #### 4.2.1 Content equivalence
# The section number is the 1–3-dot-separated digit sequence immediately
# after the #s.
HEADING_RE = re.compile(r"^#{2,4}\s+(\d+(?:\.\d+)*)(?:[.\s]|$)")

# A cross-chapter chain: ](FILE.md) followed by one §-reference plus any
# number of comma-separated sibling §-references that share the same target.
# Example matches: "](foo.md) §1.3", "](foo.md) §1.3, §7.2", or "](foo.md)\n§5.1".
# A semicolon, period, or any non-whitespace/non-comma character between
# §-references terminates the chain — so "](a.md) §1, §2; ](b.md) §3" puts
# §1/§2 under a.md and §3 under b.md.
CROSS_CHAIN_RE = re.compile(
    r"\]\(([^)\s]+\.md)\)"
    r"(?P<chain>\s*§\d+(?:\.\d+)*(?:\s*,\s*§\d+(?:\.\d+)*)*)"
)

# Every §-reference, regardless of whether it is cross-chapter or intra-chapter.
ALL_REF_RE = re.compile(r"§(\d+(?:\.\d+)*)")


def build_heading_index(files: list[Path]) -> dict[str, set[str]]:
    """Return ``{filename: {section_num, …}}`` for every numbered heading."""
    index: dict[str, set[str]] = {}
    for f in files:
        sections: set[str] = set()
        for line in f.read_text().splitlines():
            m = HEADING_RE.match(line)
            if m:
                sections.add(m.group(1))
        index[f.name] = sections
    return index


def _line_of(text: str, pos: int, line_starts: list[int]) -> int:
    return bisect.bisect_right(line_starts, pos)


def find_refs(
    path: Path,
) -> list[tuple[int, str, str | None]]:
    """Return ``(lineno, section_num, target_filename_or_None)`` per §-ref.

    ``target_filename`` is the filename from the nearest preceding
    ``](FILE.md)`` link when one attaches to this reference; ``None``
    means the reference is intra-chapter.
    """
    text = path.read_text()
    line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]

    cross_by_pos: dict[int, str] = {}
    for chain_m in CROSS_CHAIN_RE.finditer(text):
        target_file = chain_m.group(1)
        chain_start = chain_m.start("chain")
        chain_text = chain_m.group("chain")
        for section_m in ALL_REF_RE.finditer(chain_text):
            cross_by_pos[chain_start + section_m.start()] = target_file

    out: list[tuple[int, str, str | None]] = []
    for m in ALL_REF_RE.finditer(text):
        pos = m.start()
        target = cross_by_pos.get(pos)
        out.append((_line_of(text, pos, line_starts), m.group(1), target))
    return out


def main() -> int:
    """Exit 0 if every §-reference resolves; 1 otherwise; 2 on setup error."""
    files = sorted(SPEC_DIR.glob("*.md"))
    if not files:
        print(f"error: no markdown files in {SPEC_DIR}", file=sys.stderr)
        return 2
    index = build_heading_index(files)

    total = 0
    errors: list[str] = []
    for f in files:
        for lineno, section, target in find_refs(f):
            total += 1
            target_file = target if target else f.name
            # Only validate against files we indexed (spec/v0 chapters).
            # References to schemas/ or docs/ are out of scope for this check.
            if target_file not in index:
                continue
            if section not in index[target_file]:
                errors.append(
                    f"{f}:{lineno}: §{section} → {target_file} "
                    f"(no such section in target)"
                )

    if errors:
        print("Unresolved §-references:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print(f"\n{len(errors)} unresolved of {total} total.", file=sys.stderr)
        return 1

    print(f"OK: all {total} §-references resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
