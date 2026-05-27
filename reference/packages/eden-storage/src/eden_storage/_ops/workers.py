"""Worker-registry operations mixin (chapter 02 §6, chapter 08 §7).

Part of the ``_StoreBase`` mixin family; see
[`.._base`](../_base.py) for ``_StoreCore`` and the composite.
"""

from __future__ import annotations

import secrets
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from eden_contracts import Worker

from .._base import _StoreCore, _Tx
from ..errors import AlreadyExists, NotFound
from ._helpers import _deep


class _WorkerOpsMixin(_StoreCore):
    """Worker registration, credential issuance, and verification."""

    def register_worker(
        self,
        worker_id: str,
        *,
        labels: dict[str, str] | None = None,
        registered_by: str | None = None,
    ) -> tuple[Worker, str | None]:
        """Register ``worker_id`` for this experiment.

        Returns ``(worker, registration_token)``. On first registration
        the second element is the freshly-minted plaintext token (≥256
        bits); on idempotent re-registration of an existing
        ``worker_id`` it is ``None`` and the existing record is
        returned unchanged. The plaintext token is returned ONLY by
        this call and ``reissue_credential``; subsequent reads MUST NOT
        return it.

        Raises ``ReservedIdentifier`` for ``admin`` / ``system`` /
        ``internal`` or any id that violates the §6.1 grammar from
        [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md).
        """
        self._validate_registry_id(worker_id, kind="worker")
        with self._atomic_operation():
            existing = self._get_worker(worker_id)
            if existing is not None:
                return (_deep(existing), None)
            # §7.1 disjoint-namespaces: a group with the same id MUST
            # NOT exist. Otherwise the resolver in §7.2 can't tell
            # whether a string in `Group.members` resolves through the
            # worker registry (leaf) or the group registry (recursive
            # descent).
            if self._get_group(worker_id) is not None:
                raise AlreadyExists(
                    f"id {worker_id!r} is already registered as a group; "
                    f"worker / group namespaces MUST be disjoint per "
                    f"chapter 02 §7.1"
                )
            token = self._generate_credential_token()
            credential_hash = self._hash_credential(token)
            # Build via dict so optional fields whose value is None are
            # omitted entirely. The `NotNone` validators on Worker
            # reject explicit-null inputs (mirroring the JSON-schema
            # absent-vs-null distinction in `_common.py`).
            worker_data: dict[str, Any] = {
                "worker_id": worker_id,
                "experiment_id": self._experiment_id,
                "registered_at": self._ts(),
            }
            if registered_by is not None:
                worker_data["registered_by"] = registered_by
            if labels:
                worker_data["labels"] = dict(labels)
            worker = Worker.model_validate(worker_data)
            tx = _Tx()
            tx.workers[worker_id] = _deep(worker)
            tx.worker_credentials[worker_id] = credential_hash
            self._apply_commit(tx)
            return (_deep(worker), token)

    def reissue_credential(self, worker_id: str) -> str:
        """Mint a fresh credential for ``worker_id``; invalidates the prior one.

        Returns the new plaintext registration token. Atomic with the
        write that replaces the stored hash. Raises ``NotFound`` if
        ``worker_id`` is not registered.
        """
        with self._atomic_operation():
            worker = self._get_worker(worker_id)
            if worker is None:
                raise NotFound(f"worker {worker_id!r}")
            token = self._generate_credential_token()
            credential_hash = self._hash_credential(token)
            tx = _Tx()
            # The wire-visible Worker shape is unchanged on reissue —
            # only the credential hash rotates. Stage an empty Worker
            # delta keyed off the existing record so a backend that
            # binds credential rotation to the row update commits both
            # in one statement; backends that store creds separately
            # ignore the workers-side stage and apply only the
            # credential update.
            tx.workers[worker_id] = _deep(worker)
            tx.worker_credentials[worker_id] = credential_hash
            self._apply_commit(tx)
            return token

    def verify_worker_credential(
        self, worker_id: str, registration_token: str
    ) -> bool:
        """Return ``True`` iff ``registration_token`` is the current credential.

        Returns ``False`` for unknown ``worker_id`` (rather than
        raising) so binding-layer callers can collapse "no such
        worker" and "wrong secret" into a single unauthorized outcome
        without leaking which arm hit. The unknown-worker branch
        verifies against a class-level dummy hash so the two failure
        modes incur the same argon2id cost — a timing oracle MUST NOT
        be able to distinguish "worker absent" from "secret wrong".
        """
        with self._atomic_operation():
            stored = self._get_worker_credential_hash(worker_id)
            if stored is None:
                # Constant-time defence: run verify against a dummy
                # hash so the unknown-worker path takes the same time
                # as a wrong-secret check. Discard the result.
                self._check_credential_hash(
                    registration_token, self._UNKNOWN_WORKER_DUMMY_HASH
                )
                return False
            return self._check_credential_hash(registration_token, stored)

    def read_worker(self, worker_id: str) -> Worker:
        """Return the wire-visible Worker, or raise ``NotFound``."""
        with self._atomic_operation():
            worker = self._get_worker(worker_id)
            if worker is None:
                raise NotFound(f"worker {worker_id!r}")
            return _deep(worker)

    def list_workers(self) -> list[Worker]:
        """Return all registered workers (deep copies, sorted by ``worker_id``)."""
        with self._atomic_operation():
            return [_deep(w) for w in self._iter_workers()]

    def _generate_credential_token(self) -> str:
        """Mint a fresh ≥256-bit registration token (URL-safe hex).

        ``secrets.token_hex(32)`` returns 64 hex chars / 256 bits of
        entropy. Hex is chosen over urlsafe_b64 so the token is safe to
        place after the ``:`` in the bearer format from
        [`spec/v0/07-wire-protocol.md`](../../../../spec/v0/07-wire-protocol.md)
        §13.1 without escape handling — the bearer parser splits on the
        first colon, and hex contains no ``:`` characters.
        """
        return secrets.token_hex(32)

    # argon2id PasswordHasher with the RFC 9106 SECOND-CHOICE-LOW-MEMORY
    # parameters (`time_cost=3, memory_cost=64 MiB, parallelism=4`) —
    # argon2-cffi's defaults as of v23. The slow-KDF properties are
    # cited as the spec posture in chapter 07 §13.1 and chapter 08 §7.
    _PASSWORD_HASHER = PasswordHasher()

    # Dummy hash computed once at class-load so the unknown-worker
    # branch of ``verify_worker_credential`` can perform a real
    # argon2id verify against it (constant-time compared to a hit;
    # see §13.4 / chunk-review item #4).
    _UNKNOWN_WORKER_DUMMY_HASH: str = _PASSWORD_HASHER.hash("eden-unknown-worker-dummy")

    def _hash_credential(self, registration_token: str) -> str:
        """Return an argon2id-encoded hash of ``registration_token``.

        Per chapter 07 §13.1 / chapter 08 §7, the reference backend
        uses argon2id as the credential KDF. The encoded form is the
        standard PHC string (carries algorithm, params, salt, and
        digest together) so a single column stores everything needed
        for verification.
        """
        return self._PASSWORD_HASHER.hash(registration_token)

    def _check_credential_hash(self, registration_token: str, stored: str) -> bool:
        """Verify ``registration_token`` against ``stored`` (argon2id encoded).

        Returns ``True`` on match, ``False`` otherwise.
        ``argon2-cffi``'s verify is itself constant-time (the only
        timing-meaningful difference is the brief decode-fail path for
        a malformed ``stored``; legitimate hashes always reach the KDF
        comparison).
        """
        try:
            return self._PASSWORD_HASHER.verify(stored, registration_token)
        except VerifyMismatchError:
            return False
        except Exception:
            # Malformed stored encoding (corrupted record, wrong
            # column type, etc.). Treat as mismatch rather than
            # propagate; the credential check contract is binary.
            return False
