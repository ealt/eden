"""Tests for the shared ``submit_with_readback`` helper.

The three worker hosts (ideator / executor / evaluator) all route their
submit through this one helper, so its contract is load-bearing: the
retry-before-orphan ladder, the definitive-rejection short-circuit, the
caller-owned ``reraise`` set, and the IllegalTransition → read-back
classification.
"""

from __future__ import annotations

import pytest
from eden_service_common import submit as submit_mod
from eden_service_common import submit_with_readback
from eden_storage import (
    ConflictingResubmission,
    DispatchError,
    IdeaSubmission,
    IllegalTransition,
    NotClaimed,
)


class _FakeStore:
    """Minimal Store stub recording submit attempts and a read-back value."""

    def __init__(self, *, submit_effects, prior=None):
        # submit_effects: list of None (success) or Exception to raise, one
        # per attempt, consumed left-to-right.
        self._submit_effects = list(submit_effects)
        self._prior = prior
        self.submit_calls = 0
        self.read_submission_calls = 0

    def submit(self, task_id, token, submission):  # noqa: ANN001, ARG002
        self.submit_calls += 1
        effect = self._submit_effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect

    def read_submission(self, task_id):  # noqa: ANN001, ARG002
        self.read_submission_calls += 1
        return self._prior


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Keep the retry backoff from actually sleeping during tests."""
    monkeypatch.setattr(submit_mod.time, "sleep", lambda _s: None)


def _submit(store, submission=None):
    submit_with_readback(
        store=store,
        task_id="t-1",
        token="wkr_x",
        submission=submission or IdeaSubmission(status="error"),
        role="ideator",
    )


def test_first_attempt_success_skips_readback():
    store = _FakeStore(submit_effects=[None])
    _submit(store)
    assert store.submit_calls == 1
    assert store.read_submission_calls == 0


@pytest.mark.parametrize(
    "exc", [NotClaimed("x"), ConflictingResubmission("x")]
)
def test_definitive_rejection_short_circuits(exc):
    # A definitive server-side rejection returns immediately: no retry,
    # no read-back (a retry would be rejected the same way).
    store = _FakeStore(submit_effects=[exc])
    _submit(store)
    assert store.submit_calls == 1
    assert store.read_submission_calls == 0


def test_reraise_set_propagates_to_caller():
    class _Custom(Exception):
        pass

    store = _FakeStore(submit_effects=[_Custom()])
    with pytest.raises(_Custom):
        submit_with_readback(
            store=store,
            task_id="t-1",
            token="wkr_x",
            submission=IdeaSubmission(status="error"),
            role="executor",
            reraise=(_Custom,),
        )
    assert store.read_submission_calls == 0


def test_transport_error_retries_then_reads_back():
    # DispatchError on every attempt (0.0 + 3 backoff delays = 4 tries),
    # then read-back returns None → clean return.
    store = _FakeStore(submit_effects=[DispatchError("x")] * 4, prior=None)
    _submit(store)
    assert store.submit_calls == 4
    assert store.read_submission_calls == 1


def test_illegal_transition_falls_through_to_readback_equivalent():
    # "We won, response lost, orchestrator terminalized": IllegalTransition
    # breaks to read-back; an equivalent committed submission classifies as
    # success (no exception, one read-back).
    sub = IdeaSubmission(status="error")
    store = _FakeStore(submit_effects=[IllegalTransition("x")], prior=sub)
    _submit(store, submission=sub)
    assert store.submit_calls == 1
    assert store.read_submission_calls == 1
