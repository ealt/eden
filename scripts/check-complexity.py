#!/usr/bin/env python3
"""Tier-1 complexity gate — fail on threshold violations in reference/.

Thresholds (per ``docs/audits/2026-05-20-code-quality-audit.md`` §6
and the operator's Phase C scope):

- File SLOC > 800
- File maintainability index < 20
- Function cyclomatic complexity > 20
- Function length (LEN, total span) > 100

Escape hatch — ``# slop-allow: <justification>``:

  Functions and files may bypass the gate by carrying a
  ``# slop-allow: <one-line justification>`` comment. The annotation
  MUST include a justification string (anything after the colon, up to
  newline). Adding a new annotation requires explicit operator review
  in the PR — see AGENTS.md "Slop prevention".

Annotation placement:

- **Function-level**: a ``# slop-allow:`` comment on the function's
  ``def`` line (trailing) OR within 5 lines immediately before the
  ``def``. Whitespace/blank lines / docstring don't count against the
  5-line budget; pure comments do.
- **File-level**: a ``# slop-allow-file: <justification>`` comment
  anywhere in the first 30 lines of the file. Suppresses file-level
  SLOC + MI checks for that file only.

Usage:

  scripts/check-complexity.py            # CI mode — fail loud, exit 1
  scripts/check-complexity.py --list     # report-only, never exit non-zero
  scripts/check-complexity.py --json     # machine-readable diagnostic
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TARGET_DIR = ROOT / "reference"

FILE_SLOC_LIMIT = 800
FILE_MI_FLOOR = 20
FUNCTION_CC_LIMIT = 20
FUNCTION_LEN_LIMIT = 100

_ALLOW_FUNCTION_RE = re.compile(r"#\s*slop-allow\s*:\s*(.+?)\s*$")
_ALLOW_FILE_RE = re.compile(r"#\s*slop-allow-file\s*:\s*(.+?)\s*$")

# Files / paths the gate doesn't reach into. Test files are excluded
# because test bodies are routinely long for fixture setup; the
# audit's complexity bar is for impl files. Conformance scenarios
# are exercised by the citation checker, not the structural one.
_EXCLUDE_PATH_FRAGMENTS = (
    "/tests/",
    "/.venv/",
    "/conformance/",
)


@dataclass
class Violation:
    kind: str  # "file_sloc" | "file_mi" | "function_cc" | "function_len"
    path: str
    name: str  # function name (or "" for file-level)
    line: int  # function line (or 1 for file-level)
    value: float
    threshold: float
    allowed: bool = False
    justification: str = ""


@dataclass
class FunctionInfo:
    name: str
    line: int  # def's lineno
    end_line: int
    cc: int = 0
    allowed: bool = False
    justification: str = ""


@dataclass
class FileInfo:
    path: Path
    rel: str
    sloc: int = 0
    mi: float = 100.0
    file_allowed: bool = False
    file_justification: str = ""
    functions: list[FunctionInfo] = field(default_factory=list)


def _should_skip(path: Path) -> bool:
    """Return True if ``path`` is outside the gate's scope."""
    spath = str(path)
    if any(fragment in spath for fragment in _EXCLUDE_PATH_FRAGMENTS):
        return True
    # Top-level conformance/ + test scripts. Already covered by
    # fragments but be defensive against future layout changes.
    if not spath.startswith(str(TARGET_DIR)):
        return True
    return False


def _file_slop_allow(src_lines: list[str]) -> tuple[bool, str]:
    """Look for ``# slop-allow-file: <justification>`` in the first 30 lines."""
    for line in src_lines[:30]:
        m = _ALLOW_FILE_RE.search(line)
        if m:
            return True, m.group(1).strip()
    return False, ""


def _function_slop_allow(src_lines: list[str], def_line: int) -> tuple[bool, str]:
    """Look for ``# slop-allow: <justification>`` annotating the def at ``def_line``.

    Accepts: (a) trailing comment on the def line itself; (b) any
    ``# slop-allow:`` comment on the 5 lines immediately preceding the
    def (blank lines + decorators allowed in the gap; non-allow
    comments don't shadow the annotation).
    """
    # def_line is 1-indexed; src_lines is 0-indexed.
    idx = def_line - 1
    if 0 <= idx < len(src_lines):
        m = _ALLOW_FUNCTION_RE.search(src_lines[idx])
        if m:
            return True, m.group(1).strip()
    # Scan up to 5 lines back, allowing intervening decorators / blanks.
    for back in range(1, 6):
        prev = idx - back
        if prev < 0:
            break
        line = src_lines[prev]
        m = _ALLOW_FUNCTION_RE.search(line)
        if m:
            return True, m.group(1).strip()
    return False, ""


def _radon_invoke(args: list[str]) -> dict[str, Any]:
    """Run ``radon <args>`` against ``reference/`` and return parsed JSON.

    Tries ``radon`` (assumed on PATH), then ``python3 -m radon``,
    then ``uv run radon``. The CI job installs radon via setup-python
    + ``pip install radon``, so the ``radon`` bin is on PATH; locally
    the script picks up whichever flavor is available.
    """
    candidates = [
        ["radon", *args],
        [sys.executable, "-m", "radon", *args],
        ["uv", "run", "radon", *args],
    ]
    last_err: Exception | None = None
    for cmd in candidates:
        try:
            out = subprocess.run(
                cmd, capture_output=True, check=True, text=True, cwd=ROOT
            )
            return json.loads(out.stdout)
        except FileNotFoundError as exc:
            last_err = exc
            continue
        except subprocess.CalledProcessError as exc:
            # Try the next candidate; if all fail, raise the last.
            last_err = exc
            continue
    if isinstance(last_err, subprocess.CalledProcessError):
        sys.stderr.write(last_err.stderr or "")
    print(
        "ERROR: could not run radon; install via 'uv pip install radon' "
        "or 'pip install radon'.",
        file=sys.stderr,
    )
    sys.exit(2)


def load_radon_metrics() -> tuple[dict[str, int], dict[str, float], dict[str, list[Any]]]:
    """One-shot radon scan over ``reference/``.

    Returns ``(sloc_by_path, mi_by_path, cc_by_path)`` keyed by repo-
    relative path. Empty dicts are returned for files radon couldn't
    parse (syntax errors etc.).
    """
    raw = _radon_invoke(["raw", "-j", "reference"])
    mi = _radon_invoke(["mi", "-j", "reference"])
    cc = _radon_invoke(["cc", "-j", "reference"])

    sloc_by_path: dict[str, int] = {}
    for rel, info in raw.items():
        if isinstance(info, dict) and "error" not in info:
            sloc_by_path[rel] = int(info.get("sloc", 0))

    mi_by_path: dict[str, float] = {}
    for rel, info in mi.items():
        if isinstance(info, dict) and "error" not in info:
            mi_by_path[rel] = float(info.get("mi", 100.0))

    cc_by_path: dict[str, list[Any]] = {}
    for rel, blocks in cc.items():
        if isinstance(blocks, list):
            cc_by_path[rel] = blocks
    return sloc_by_path, mi_by_path, cc_by_path


def _cc_map_for(blocks: list[Any]) -> dict[tuple[str, int], int]:
    """Flatten one file's radon-cc blocks list into ``{(name, lineno): cc}``."""
    result: dict[tuple[str, int], int] = {}
    for b in blocks:
        try:
            name = b["name"]
            line = int(b["lineno"])
            ccv = int(b["complexity"])
        except (KeyError, ValueError, TypeError):
            continue
        result[(name, line)] = ccv
        for m in b.get("methods", []) or []:
            try:
                m_name = f"{name}.{m['name']}"
                m_line = int(m["lineno"])
                m_cc = int(m["complexity"])
            except (KeyError, ValueError, TypeError):
                continue
            result[(m_name, m_line)] = m_cc
    return result


def _walk_functions(tree: ast.AST) -> list[tuple[str, int, int]]:
    """Return ``[(qualified_name, def_line, end_line)]`` for every fn/method."""
    items: list[tuple[str, int, int]] = []

    def visit(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}" if prefix else child.name
                end = getattr(child, "end_lineno", child.lineno)
                items.append((qual, child.lineno, end))
                visit(child, f"{qual}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")

    visit(tree)
    return items


def collect_file_info(
    path: Path,
    *,
    sloc_by_path: dict[str, int],
    mi_by_path: dict[str, float],
    cc_by_path: dict[str, list[Any]],
) -> FileInfo:
    """Build a ``FileInfo`` for ``path`` (uses the pre-scanned radon metrics)."""
    rel = str(path.relative_to(ROOT))
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()

    info = FileInfo(path=path, rel=rel)
    info.sloc = sloc_by_path.get(rel, 0)
    info.mi = mi_by_path.get(rel, 100.0)
    info.file_allowed, info.file_justification = _file_slop_allow(lines)

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return info
    cc_map = _cc_map_for(cc_by_path.get(rel, []))
    name_index: dict[str, list[tuple[str, int]]] = {}
    for (qual, line), _cc in cc_map.items():
        # cc_map keys are (qualified_name, line). We want to match
        # against ast-level qualified names. Index by short name too
        # so a class-method match works either way.
        name_index.setdefault(qual.rsplit(".", 1)[-1], []).append((qual, line))
        name_index.setdefault(qual, []).append((qual, line))

    for qual, def_line, end_line in _walk_functions(tree):
        # Find the matching radon entry by line proximity + short name.
        short = qual.rsplit(".", 1)[-1]
        candidates = name_index.get(qual) or name_index.get(short) or []
        cc = 0
        for _cand_name, cand_line in candidates:
            if cand_line == def_line:
                cc = cc_map.get((_cand_name, cand_line), 0)
                break
        allowed, just = _function_slop_allow(lines, def_line)
        info.functions.append(
            FunctionInfo(
                name=qual,
                line=def_line,
                end_line=end_line,
                cc=cc,
                allowed=allowed,
                justification=just,
            )
        )
    return info


def collect_violations(files: list[FileInfo]) -> list[Violation]:
    """Run threshold checks. Returns ALL hits — `.allowed` flagged separately."""
    violations: list[Violation] = []
    for info in files:
        # File-level checks (skippable via slop-allow-file)
        if not info.file_allowed:
            if info.sloc > FILE_SLOC_LIMIT:
                violations.append(
                    Violation(
                        kind="file_sloc",
                        path=info.rel,
                        name="",
                        line=1,
                        value=info.sloc,
                        threshold=FILE_SLOC_LIMIT,
                    )
                )
            if info.mi < FILE_MI_FLOOR:
                violations.append(
                    Violation(
                        kind="file_mi",
                        path=info.rel,
                        name="",
                        line=1,
                        value=info.mi,
                        threshold=FILE_MI_FLOOR,
                    )
                )
        else:
            # File-allowed: still record (for --list visibility) but
            # mark them as allowed so CI mode doesn't fail.
            if info.sloc > FILE_SLOC_LIMIT:
                violations.append(
                    Violation(
                        kind="file_sloc", path=info.rel, name="", line=1,
                        value=info.sloc, threshold=FILE_SLOC_LIMIT,
                        allowed=True, justification=info.file_justification,
                    )
                )
            if info.mi < FILE_MI_FLOOR:
                violations.append(
                    Violation(
                        kind="file_mi", path=info.rel, name="", line=1,
                        value=info.mi, threshold=FILE_MI_FLOOR,
                        allowed=True, justification=info.file_justification,
                    )
                )

        # Function-level checks
        for fn in info.functions:
            length = fn.end_line - fn.line + 1
            if fn.cc > FUNCTION_CC_LIMIT:
                violations.append(
                    Violation(
                        kind="function_cc",
                        path=info.rel,
                        name=fn.name,
                        line=fn.line,
                        value=fn.cc,
                        threshold=FUNCTION_CC_LIMIT,
                        allowed=fn.allowed,
                        justification=fn.justification,
                    )
                )
            if length > FUNCTION_LEN_LIMIT:
                violations.append(
                    Violation(
                        kind="function_len",
                        path=info.rel,
                        name=fn.name,
                        line=fn.line,
                        value=length,
                        threshold=FUNCTION_LEN_LIMIT,
                        allowed=fn.allowed,
                        justification=fn.justification,
                    )
                )
    return violations


def render_table(violations: list[Violation], header: str) -> str:
    if not violations:
        return ""
    lines = [header, "-" * len(header)]
    for v in violations:
        suffix = f" — slop-allow: {v.justification}" if v.allowed else ""
        if v.name:
            loc = f"{v.path}:{v.line} {v.name}"
        else:
            loc = v.path
        lines.append(
            f"  [{v.kind}] {loc} — value={v.value:g} threshold={v.threshold:g}{suffix}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-complexity",
        description=__doc__.split("\n\n", 1)[0],  # type: ignore[union-attr]
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Report-only: print all hits incl. slop-allow-flagged ones. Exit 0.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of human text.",
    )
    args = parser.parse_args(argv)

    if not TARGET_DIR.exists():
        print(f"ERROR: target directory {TARGET_DIR} does not exist", file=sys.stderr)
        return 2

    sloc_by_path, mi_by_path, cc_by_path = load_radon_metrics()

    # Walk reference/ for .py files
    files: list[FileInfo] = []
    for py_path in sorted(TARGET_DIR.rglob("*.py")):
        if _should_skip(py_path):
            continue
        files.append(
            collect_file_info(
                py_path,
                sloc_by_path=sloc_by_path,
                mi_by_path=mi_by_path,
                cc_by_path=cc_by_path,
            )
        )

    violations = collect_violations(files)

    if args.json:
        payload = [
            {
                "kind": v.kind,
                "path": v.path,
                "name": v.name,
                "line": v.line,
                "value": v.value,
                "threshold": v.threshold,
                "allowed": v.allowed,
                "justification": v.justification,
            }
            for v in violations
        ]
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    blocking = [v for v in violations if not v.allowed]
    allowed = [v for v in violations if v.allowed]

    if blocking:
        sys.stdout.write(render_table(blocking, "BLOCKING violations:"))
        sys.stdout.write("\n")
    if allowed and (args.list or blocking):
        sys.stdout.write(render_table(
            allowed, "Allowed (via # slop-allow / # slop-allow-file):"
        ))
        sys.stdout.write("\n")

    if blocking and not args.list:
        sys.stdout.write(
            "\nFAIL: complexity gate found violations without # slop-allow "
            "annotations.\n"
            "Either refactor the offending code or add a justified annotation "
            "(see AGENTS.md 'Slop prevention').\n"
        )
        return 1

    if not blocking and not allowed:
        sys.stdout.write("OK: no complexity-gate violations.\n")
    elif not blocking:
        sys.stdout.write(
            f"\nOK: {len(allowed)} violation(s) carry # slop-allow annotations; "
            "0 blocking.\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
