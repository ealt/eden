"""Every ``format`` declared in a spec schema has a FormatChecker handler.

Without this test, adding (say) ``format: email`` to a new schema would
silently pass the parity validator — the JSON Schema ``format`` keyword
is advisory by default, and ``jsonschema`` enforces only the formats
registered in the supplied FormatChecker. The parity contract would
weaken, and the per-schema test_schema_parity would still be green.

This test fails loudly when a new format appears in any schema without
a corresponding ``@FORMAT_CHECKER.checks("...")`` handler in
conftest.py, so the gap can't go unnoticed.
"""

from __future__ import annotations

import json
from typing import Any

from .conftest import EXPLICIT_FORMATS, SCHEMAS_DIR


def _collect_formats(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        fmt = node.get("format")
        if isinstance(fmt, str):
            out.add(fmt)
        for value in node.values():
            _collect_formats(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_formats(item, out)


def test_every_schema_format_has_a_checker() -> None:
    used: set[str] = set()
    for path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text())
        _collect_formats(schema, used)

    missing = used - EXPLICIT_FORMATS
    assert not missing, (
        f"schemas declare format(s) {sorted(missing)} but tests/conftest.py "
        f"has no explicit `@_register_format(...)` handler for them. "
        f"jsonschema's built-in FormatChecker defaults are permissive and "
        f"don't necessarily match the model-side enforcement — add a "
        f"handler in conftest.py AND a matching validator in "
        f"reference/packages/eden-contracts/src/eden_contracts/_common.py "
        f"for each missing format."
    )
