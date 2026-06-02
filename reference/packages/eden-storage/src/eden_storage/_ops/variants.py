"""Variant-store operations mixin (chapter 02 §4, chapter 06)."""

from __future__ import annotations

from eden_contracts import Variant

from .._base import _StoreCore, _Tx
from ..errors import AlreadyExists, IllegalTransition, InvalidPrecondition, NotFound
from ._helpers import _deep, _validated_update


class _VariantOpsMixin(_StoreCore):
    """Variant creation, evaluation-error / integration writes, and reads."""

    def read_variant(self, variant_id: str) -> Variant:
        """Return a snapshot of the current variant, or raise ``NotFound``."""
        with self._atomic_operation():
            variant = self._get_variant(variant_id)
            if variant is None:
                raise NotFound(f"variant {variant_id!r}")
            return _deep(variant)

    def list_variants(self, *, status: str | None = None) -> list[Variant]:
        """Return snapshots of variants matching an optional ``status`` filter."""
        with self._atomic_operation():
            return [_deep(t) for t in self._iter_variants(status=status)]

    def create_variant(self, variant: Variant) -> None:
        """Persist a new variant. Emits ``variant.started`` (``08-storage.md`` §1.7).

        An ordinary variant MUST be created in ``starting``. A
        ``kind == "baseline"`` variant (``02-data-model.md`` §9.4) relaxes
        this: it MAY be created directly in ``success`` carrying its
        ``evaluation`` payload (the override path, §2.7), in which case the
        store validates the metrics against the experiment's
        ``evaluation_schema`` at create time and emits ``variant.started``
        **then** ``variant.succeeded`` atomically in the one transaction
        (``05-event-protocol.md`` §3.3). Per-``kind`` create authority
        (``orchestrators`` group for baselines) is a binding-layer concern
        enforced at the wire (``07-wire-protocol.md`` §4), not here.
        """
        with self._atomic_operation():
            if self._get_variant(variant.variant_id) is not None:
                raise AlreadyExists(f"variant {variant.variant_id!r}")
            if variant.experiment_id != self._experiment_id:
                raise InvalidPrecondition(
                    f"variant experiment_id {variant.experiment_id!r} "
                    f"does not match store experiment {self._experiment_id!r}"
                )
            is_baseline = variant.kind == "baseline"
            direct_success = is_baseline and variant.status == "success"
            if variant.status != "starting" and not direct_success:
                hint = (
                    " (a baseline MAY be created directly in 'success')"
                    if is_baseline
                    else ""
                )
                raise InvalidPrecondition(
                    f"new variant must start in 'starting'{hint}, "
                    f"not {variant.status!r}"
                )
            if direct_success:
                if variant.evaluation is None:
                    raise InvalidPrecondition(
                        "a baseline created directly in 'success' must carry "
                        "evaluation metrics"
                    )
                if variant.commit_sha is None:
                    raise InvalidPrecondition(
                        "a baseline created directly in 'success' must carry commit_sha"
                    )
                # Apply the §9.2 evaluation-schema check at create time,
                # mirroring evaluation-submission acceptance.
                self._validate_evaluation(variant.evaluation)
            tx = _Tx()
            tx.variants[variant.variant_id] = _deep(variant)
            started_data: dict[str, str] = {"variant_id": variant.variant_id}
            if is_baseline:
                # kind is REQUIRED on a baseline variant.started so event-only
                # subscribers get an explicit signal (05-event-protocol.md §3.3).
                started_data["kind"] = "baseline"
            if variant.idea_id is not None:
                started_data["idea_id"] = variant.idea_id
            tx.events.append(self._event("variant.started", started_data))
            if direct_success:
                tx.events.append(
                    self._event(
                        "variant.succeeded",
                        {
                            "variant_id": variant.variant_id,
                            "commit_sha": variant.commit_sha,
                        },
                    )
                )
            self._apply_commit(tx)

    def declare_variant_evaluation_error(self, variant_id: str) -> None:
        """Retry-exhausted: ``starting → evaluation_error`` (``05-event-protocol.md`` §2.2).

        Writes ``completed_at`` atomically; MUST NOT set metrics or
        artifacts_uri (``03-roles.md`` §4.4).
        """
        with self._atomic_operation():
            variant = self._require_variant(variant_id)
            if variant.status != "starting":
                raise IllegalTransition(
                    f"cannot declare evaluation_error from variant status {variant.status!r}"
                )
            now = self._ts()
            tx = _Tx()
            tx.variants[variant_id] = _validated_update(
                variant, status="evaluation_error", completed_at=now
            )
            tx.events.append(self._event("variant.evaluation_errored", {"variant_id": variant_id}))
            self._apply_commit(tx)

    def integrate_variant(self, variant_id: str, variant_commit_sha: str) -> None:
        """Integrator integration: write ``variant_commit_sha`` and emit ``variant.integrated``.

        Per ``08-storage.md`` §1.7: ``variant_commit_sha`` is the one
        post-terminal write permitted on a variant; it must be written
        atomically with its event.

        **Same-value idempotency** (``07-wire-protocol.md`` §5): a
        repeated call whose ``variant_commit_sha`` equals the value
        already stored on the variant is a no-op and MUST NOT append a
        second ``variant.integrated`` event. This rule lets an HTTP-
        mediated caller retry a transport-indeterminate
        ``integrate_variant`` request without risking double-commit;
        the same-value branch also keeps direct-``Store`` callers
        and wire-mediated callers on identical contracts.

        A repeated call with a **different** ``variant_commit_sha``
        raises ``InvalidPrecondition`` — the chapter 6 §1.2 sole-
        writer rule has been violated and operator intervention is
        required. The caller (e.g. ``Integrator``) maps this to an
        ``AtomicityViolation`` rather than compensating the ref.
        """
        with self._atomic_operation():
            variant = self._require_variant(variant_id)
            if variant.kind == "baseline":
                raise InvalidPrecondition(
                    f"variant {variant_id!r} is a baseline and is never integrated "
                    "(02-data-model.md §9.4, 06-integrator.md §2)"
                )
            if variant.status != "success":
                raise InvalidPrecondition(
                    f"variant {variant_id!r} must be in 'success' to integrate, "
                    f"not {variant.status!r}"
                )
            if variant.variant_commit_sha is not None:
                if variant.variant_commit_sha == variant_commit_sha:
                    return
                raise InvalidPrecondition(
                    f"variant {variant_id!r} is already integrated with a "
                    f"different variant_commit_sha "
                    f"({variant.variant_commit_sha!r} != {variant_commit_sha!r})"
                )
            tx = _Tx()
            tx.variants[variant_id] = _validated_update(
                variant, variant_commit_sha=variant_commit_sha
            )
            tx.events.append(
                self._event(
                    "variant.integrated",
                    {"variant_id": variant_id, "variant_commit_sha": variant_commit_sha},
                )
            )
            self._apply_commit(tx)
