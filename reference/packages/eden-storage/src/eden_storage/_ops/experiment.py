"""Experiment-lifecycle + dispatch-mode + checkpoint operations mixin.

Chapters 02 §2.4/§2.5, 04 §7/§8, 10. Owns the evaluation-schema
validator (``validate_evaluation`` / ``_validate_evaluation``)
because it references the experiment-scoped ``_evaluation_schema``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from eden_contracts import DispatchMode, Experiment, ExperimentState

from .._base import _METRIC_PY_TYPES, _StoreCore, _Tx
from ..errors import IllegalTransition, InvalidPrecondition
from ._helpers import _deep, _validated_update

if TYPE_CHECKING:
    from .._base import _StoreBase


class _ExperimentOpsMixin(_StoreCore):
    """Experiment state, dispatch-mode, policy-error, and checkpoint ops."""

    def read_experiment(self) -> Experiment:
        """Return the experiment runtime object (state + created_at).

        Per [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §2.5. The runtime object is distinct from the declarative
        ``experiment-config``; it carries only observed runtime state.
        """
        with self._atomic_operation():
            return _deep(self._get_experiment())

    def read_experiment_state(self) -> ExperimentState:
        """Return the experiment's current lifecycle state.

        Defaults to ``"running"`` on a freshly-initialized experiment;
        becomes ``"terminated"`` after :meth:`terminate_experiment` or
        the policy-driven termination branch commits the transition.
        """
        with self._atomic_operation():
            return self._get_experiment().state

    def update_experiment_state(self, new_state: ExperimentState) -> Experiment:
        """Internal primitive: atomically update the experiment lifecycle state.

        Not a public wire op in v0 (per
        [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §8.3). Used by :meth:`terminate_experiment` and the
        orchestrator's policy-driven termination branch
        ([`spec/v0/03-roles.md`](../../../../spec/v0/03-roles.md)
        §6.2 decision-type 0). v0 defines exactly one legal transition
        (``"running" → "terminated"``); other values raise
        ``IllegalTransition``.

        This method does NOT emit ``experiment.terminated`` on its own
        — composite commit with the appropriate event is the caller's
        responsibility. Use :meth:`terminate_experiment` for the
        normal public-op shape.
        """
        if new_state not in ("running", "terminated"):
            raise InvalidPrecondition(
                f"experiment.state value {new_state!r} is not "
                "'running' or 'terminated'"
            )
        with self._atomic_operation():
            current = self._get_experiment()
            if current.state == new_state:
                return _deep(current)
            if not (current.state == "running" and new_state == "terminated"):
                raise IllegalTransition(
                    f"cannot transition experiment state "
                    f"{current.state!r} → {new_state!r}"
                )
            tx = _Tx()
            tx.experiment_state = new_state
            self._apply_commit(tx)
            return _validated_update(current, state=new_state)

    def terminate_experiment(
        self, *, reason: str, terminated_by: str
    ) -> Experiment:
        """Atomically commit the ``running → terminated`` lifecycle transition.

        Per [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §8.1: the state field update and the ``experiment.terminated``
        event are a single transaction. Idempotent on the terminated
        state — a second call returns success without committing a
        second transition and without appending a second event; the
        winning caller's ``reason`` (the first commit) is the one
        recorded.

        Authority enforcement (caller in ``admins``) is the binding's
        responsibility; the Store trusts ``terminated_by`` as data.
        Composite-commits the state update and the event per
        [`spec/v0/05-event-protocol.md`](../../../../spec/v0/05-event-protocol.md)
        §2.
        """
        self._validate_actor_id(terminated_by, kind="terminated_by")
        with self._atomic_operation():
            current = self._get_experiment()
            if current.state == "terminated":
                # §8.1 idempotency: success, no second event, prior
                # reason preserved.
                return _deep(current)
            tx = _Tx()
            tx.experiment_state = "terminated"
            tx.events.append(
                self._event(
                    "experiment.terminated",
                    {"reason": reason, "terminated_by": terminated_by},
                )
            )
            self._apply_commit(tx)
            return _validated_update(current, state="terminated")

    def emit_policy_error(
        self,
        *,
        policy_kind: str,
        error_type: str,
        error_message: str,
    ) -> None:
        """Append an ``experiment.policy_error`` event (12a-3).

        Per [`spec/v0/03-roles.md`](../../../../spec/v0/03-roles.md)
        §6.2 decision-type 0 fault-tolerance: when an orchestrator
        policy callable raises, the orchestrator MUST emit a
        registered ``experiment.policy_error`` event so operators see
        the failure in the admin event log. The event is registered
        but EXEMPT from the §2 transactional invariant (no
        protocol-owned state mutation pairs with it).
        """
        if not policy_kind:
            raise InvalidPrecondition("policy_kind MUST be non-empty")
        if not error_type:
            raise InvalidPrecondition("error_type MUST be non-empty")
        with self._atomic_operation():
            tx = _Tx()
            tx.events.append(
                self._event(
                    "experiment.policy_error",
                    {
                        "policy_kind": policy_kind,
                        "error_type": error_type,
                        "error_message": error_message,
                    },
                )
            )
            self._apply_commit(tx)

    def read_dispatch_mode(self) -> DispatchMode:
        """Return the experiment's current dispatch_mode (every key).

        Defaults to all-``auto`` on the four operational keys and
        ``"manual"`` on ``termination`` for a freshly-initialized
        experiment ([`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §2.4). Unknown keys persisted by older writes are returned via
        the model's ``extra="allow"`` carry-through.
        """
        with self._atomic_operation():
            return DispatchMode.model_validate(self._get_dispatch_mode())

    def update_dispatch_mode(
        self,
        updates: DispatchMode | dict[str, str],
        *,
        updated_by: str,
    ) -> DispatchMode:
        """Atomically merge ``updates`` into the experiment's dispatch_mode.

        Spec: [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §7 + [`spec/v0/05-event-protocol.md`](../../../../spec/v0/05-event-protocol.md)
        §3.4. Omitted keys are preserved; unknown keys in ``updates``
        round-trip through (§2.5 tolerance). When no key actually
        changes value, NO event fires (the spec records changes, not
        idempotent no-ops).
        """
        self._validate_actor_id(updated_by, kind="updated_by")
        if isinstance(updates, DispatchMode):
            update_map = updates.model_dump(mode="json", exclude_none=True)
        else:
            update_map = dict(updates)
        # Reject values that are not in the closed value-set; tolerate
        # unknown keys per §2.5 but keep the value-grammar strict.
        for key, value in update_map.items():
            if value not in {"auto", "manual"}:
                raise InvalidPrecondition(
                    f"dispatch_mode.{key} value {value!r} is not 'auto' or 'manual'"
                )
        with self._atomic_operation():
            current = dict(self._get_dispatch_mode())
            changed: dict[str, str] = {}
            for key, value in update_map.items():
                if current.get(key) != value:
                    changed[key] = value
            if not changed:
                # No-op flip: no event, no write. The committed state
                # is exactly what we read.
                return DispatchMode.model_validate(current)
            new_state = {**current, **changed}
            tx = _Tx()
            tx.dispatch_mode = new_state
            tx.events.append(
                self._event(
                    "experiment.dispatch_mode_changed",
                    {
                        "dispatch_mode": new_state,
                        "changed": changed,
                        "updated_by": updated_by,
                    },
                )
            )
            self._apply_commit(tx)
            return DispatchMode.model_validate(new_state)

    def validate_evaluation(self, evaluation: dict[str, Any]) -> None:
        """Validate evaluation against the registered schema.

        Public entry point for both submit-time (``08-storage.md`` §4)
        and integration-time (``06-integrator.md`` §2) validation.
        Raises ``InvalidPrecondition`` on violation; no-op when the
        store was constructed without an ``evaluation_schema``.
        """
        self._validate_evaluation(evaluation)

    def _validate_evaluation(self, evaluation: dict[str, Any]) -> None:
        """Validate evaluation against the registered schema (``08-storage.md`` §4)."""
        if self._evaluation_schema is None:
            return
        schema = self._evaluation_schema.root
        for key, value in evaluation.items():
            if key not in schema:
                raise InvalidPrecondition(
                    f"evaluation key {key!r} is not in the experiment's evaluation_schema"
                )
            if value is None:
                continue
            mtype = schema[key]
            # Reject bools for integer/real per spec §1.3 (bool is a separate domain).
            if isinstance(value, bool):
                raise InvalidPrecondition(
                    f"evaluation key {key!r} is bool; declared type is {mtype!r}"
                )
            expected = _METRIC_PY_TYPES[mtype]
            if not isinstance(value, expected):
                raise InvalidPrecondition(
                    f"evaluation key {key!r} value {value!r} is not of declared type {mtype!r}"
                )
            # Non-finite floats (NaN, +inf, -inf) fail JSON round-trip
            # and can't be stored in the event log or evaluation manifest. The
            # ``real`` type in the evaluation schema implies "finite IEEE
            # 754 double" per the spec's JSON grounding.
            if mtype == "real" and not math.isfinite(value):
                raise InvalidPrecondition(
                    f"evaluation key {key!r} value {value!r} is not finite"
                )

    def export_checkpoint(
        self,
        stream: Any,
        *,
        experiment_config: str | bytes = "",
        repo_bundle: bytes = b"",
        repo_bundle_provider: Callable[[], bytes] | None = None,
        exporter_info: Any | None = None,
    ) -> Any:
        """Write a portable-checkpoint archive of the store's state.

        Delegates to :func:`eden_storage._checkpoint.export_checkpoint`;
        see that function's docstring for the full contract (including
        the ``repo_bundle_provider`` post-snapshot ordering from issue
        #294). Runs inside :meth:`_atomic_operation` so the snapshot is
        transactionally consistent per ``spec/v0/10-checkpoints.md`` §6.

        Returns the :class:`CheckpointManifest` written into the archive.
        """
        from .._checkpoint import export_checkpoint as _export

        # `_checkpoint` needs the full composite surface (it calls both
        # `_StoreCore` primitives and `_WorkerOpsMixin` credential
        # helpers); at runtime `self` is always a composed `_StoreBase`
        # backend. Plan §8.2 option (b).
        return _export(
            cast("_StoreBase", self),
            stream,
            experiment_config=experiment_config,
            repo_bundle=repo_bundle,
            repo_bundle_provider=repo_bundle_provider,
            exporter_info=exporter_info,
        )

    def import_checkpoint(
        self,
        stream: Any,
        *,
        as_experiment_id: str | None = None,
        extract_dir: Any | None = None,
    ) -> Any:
        """Bulk-insert a portable-checkpoint archive into the store.

        Delegates to :func:`eden_storage._checkpoint.import_checkpoint`;
        see that function's docstring for the full contract. The store
        MUST be empty (chapter 10 §11 collision rule) and the manifest's
        ``spec_version`` MUST match this binding's
        :data:`CHECKPOINT_SPEC_VERSION`. Returns an
        :class:`ImportResult` carrying the substrate-external pieces the
        caller must wire (experiment_config text, git bundle path,
        artifact digests).
        """
        from .._checkpoint import import_checkpoint as _import

        return _import(
            cast("_StoreBase", self),
            stream,
            as_experiment_id=as_experiment_id,
            extract_dir=extract_dir,
        )
