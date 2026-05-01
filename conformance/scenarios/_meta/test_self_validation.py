"""Self-validation: prove the suite catches a deliberately broken adapter.

Runs three subprocess pytest invocations:

a. Control run: targeted test against the unmodified ReferenceAdapter
   (must pass).
b. Broken run: same targeted test against the MisbehavingAdapter
   (must fail with the specific test ID and assertion text).
c. Hit-counter check: the misbehaving proxy must have mutated ≥ 1
   response (otherwise the broken run failed for some other reason).

A passing meta test is the proof the harness has teeth and the teeth
are biting the right victim.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.conformance_meta

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TARGET_TEST = (
    "conformance/scenarios/test_claim_tokens.py::test_wrong_token_rejected"
)
_REFERENCE_ADAPTER = "conformance.adapters.reference.adapter:ReferenceAdapter"
_MISBEHAVING_ADAPTER = (
    "conformance._meta.misbehaving_adapter:MisbehavingAdapter"
)
_HIT_COUNTER_ENV = "EDEN_CONFORMANCE_MISBEHAVE_HIT_COUNTER"


def _run_pytest_subprocess(
    *,
    adapter: str,
    json_report_path: Path,
    hit_counter_path: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env[_HIT_COUNTER_ENV] = str(hit_counter_path)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            _TARGET_TEST,
            f"--iut-adapter={adapter}",
            "--no-header",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "--json-report",
            f"--json-report-file={json_report_path}",
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _read_json_report(path: Path) -> dict:
    return json.loads(path.read_text())


def test_self_validation(tmp_path: Path) -> None:
    """Harness self-validation per spec/v0/09-conformance.md §3.

    Three sub-assertions are checked together: control passes, broken
    fails on the targeted test, and the misbehaving proxy actually
    mutated a response. All three must hold.
    """
    control_report = tmp_path / "control.json"
    broken_report = tmp_path / "broken.json"
    control_counter = tmp_path / "control-counter"
    broken_counter = tmp_path / "broken-counter"

    # (a) Control run.
    control = _run_pytest_subprocess(
        adapter=_REFERENCE_ADAPTER,
        json_report_path=control_report,
        hit_counter_path=control_counter,
    )
    assert control.returncode == 0, (
        f"Control run failed unexpectedly:\n"
        f"stdout={control.stdout}\nstderr={control.stderr}"
    )

    # (b) Broken run.
    broken = _run_pytest_subprocess(
        adapter=_MISBEHAVING_ADAPTER,
        json_report_path=broken_report,
        hit_counter_path=broken_counter,
    )
    assert broken.returncode != 0, (
        f"Broken run unexpectedly passed — harness lacks teeth.\n"
        f"stdout={broken.stdout}\nstderr={broken.stderr}"
    )
    report = _read_json_report(broken_report)
    target_results = [
        t
        for t in report.get("tests", [])
        if t.get("nodeid") == _TARGET_TEST
    ]
    assert target_results, f"Expected target test in report; got {report}"
    target = target_results[0]
    assert target.get("outcome") == "failed", target
    # Tighten the failure-shape assertion: the broken adapter must
    # cause the SPECIFIC wrong-token assertion to fail with the exact
    # message text from the production scenario. A looser substring
    # match would let unrelated failures (import errors, fixture
    # crashes) satisfy the meta test.
    longrepr_blob = json.dumps(target)
    assert "Expected 403 wrong-token, got 200" in longrepr_blob, (
        f"Failure repr did not match expected wrong-token assertion: {target}"
    )

    # (c) Hit-counter check.
    assert broken_counter.exists(), (
        "Misbehaving proxy never wrote a hit counter — proxy was not exercised."
    )
    count = int(broken_counter.read_text().strip() or "0")
    assert count >= 1, "Misbehaving proxy mutated 0 responses; expected ≥ 1"
