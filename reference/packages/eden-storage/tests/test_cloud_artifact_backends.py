"""Unit tests for the S3 / GCS artifact blob backends (issue #174).

Both backends satisfy the metadata-free ``ArtifactBackend`` Protocol
(``store`` / ``load`` keyed by the server-minted opaque id). The tests
inject duck-typed fake clients via the constructors' ``client=`` hook —
no network, no credentials — and raise the REAL SDK exception classes
(``botocore.exceptions.ClientError``, ``google.api_core.exceptions.*``)
so the backends' exception mapping is exercised against what the SDKs
actually throw.
"""

from __future__ import annotations

import io
import sys
from typing import Any

import pytest
from botocore.exceptions import ClientError
from eden_storage import GcsBackend, NotFound, S3Backend
from google.api_core.exceptions import Forbidden, PreconditionFailed
from google.api_core.exceptions import NotFound as GcsNotFound

_HEX = "0123456789abcdef0123456789abcdef"
_HEX2 = "fedcba9876543210fedcba9876543210"


def _client_error(code: str, status: int, op: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        op,
    )


class _FakeS3Client:
    """Duck-typed boto3 S3 client: put_object / get_object.

    Mirrors real S3 semantics for the paths the backend relies on:
    ``IfNoneMatch="*"`` is the create-only conditional write (412
    PreconditionFailed on an existing key), a missing bucket is
    ``NoSuchBucket`` (also HTTP 404 — distinct from a missing KEY), and a
    missing key on GET is ``NoSuchKey``.
    """

    def __init__(
        self,
        *,
        transport_error: bool = False,
        bucket_missing: bool = False,
        conflicts: int = 0,
    ) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self._transport_error = transport_error
        self._bucket_missing = bucket_missing
        self._conflicts = conflicts

    def put_object(
        self,
        *,
        Bucket: str,  # noqa: N803
        Key: str,  # noqa: N803
        Body: bytes,  # noqa: N803
        IfNoneMatch: str | None = None,  # noqa: N803
    ) -> None:
        if self._transport_error:
            raise _client_error("InternalError", 500, "PutObject")
        if self._bucket_missing:
            raise _client_error("NoSuchBucket", 404, "PutObject")
        if self._conflicts > 0:
            # AWS's retryable "another conditional write to this key is in
            # flight" response to an If-None-Match PUT.
            self._conflicts -= 1
            raise _client_error("ConditionalRequestConflict", 409, "PutObject")
        if IfNoneMatch != "*":
            # The create-only precondition IS the §5.4 no-overwrite
            # guarantee; a PUT without it would silently overwrite.
            raise AssertionError("store must put with IfNoneMatch='*'")
        if (Bucket, Key) in self.objects:
            raise _client_error("PreconditionFailed", 412, "PutObject")
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if self._transport_error:
            raise _client_error("InternalError", 500, "GetObject")
        if self._bucket_missing:
            raise _client_error("NoSuchBucket", 404, "GetObject")
        if (Bucket, Key) not in self.objects:
            raise _client_error("NoSuchKey", 404, "GetObject")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


class TestS3Backend:
    def test_store_then_load_roundtrips(self) -> None:
        client = _FakeS3Client()
        backend = S3Backend(bucket="b", client=client)
        backend.store(_HEX, b"payload")
        assert backend.load(_HEX) == b"payload"
        assert client.objects == {("b", _HEX): b"payload"}

    def test_prefix_namespaces_the_key(self) -> None:
        client = _FakeS3Client()
        backend = S3Backend(bucket="b", prefix="/eden/exp-1/", client=client)
        backend.store(_HEX, b"x")
        # Leading/trailing slashes are stripped; key is <prefix>/<id>.
        assert list(client.objects) == [("b", f"eden/exp-1/{_HEX}")]
        assert backend.load(_HEX) == b"x"

    def test_store_reused_id_raises_file_exists(self) -> None:
        # 08-storage.md §5.4 no-overwrite, independent of caller discipline.
        backend = S3Backend(bucket="b", client=_FakeS3Client())
        backend.store(_HEX, b"one")
        with pytest.raises(FileExistsError):
            backend.store(_HEX, b"two")
        assert backend.load(_HEX) == b"one"

    def test_load_absent_raises_not_found(self) -> None:
        backend = S3Backend(bucket="b", client=_FakeS3Client())
        with pytest.raises(NotFound):
            backend.load(_HEX)

    def test_transport_error_propagates_not_notfound(self) -> None:
        # A 5xx is transport-indeterminate; mapping it to NotFound would
        # misclassify an outage as a missing artifact (AGENTS.md pitfall:
        # narrow exception handling on store reads).
        backend = S3Backend(bucket="b", client=_FakeS3Client(transport_error=True))
        with pytest.raises(ClientError):
            backend.load(_HEX)
        with pytest.raises(ClientError):
            backend.store(_HEX2, b"x")

    def test_missing_bucket_propagates_not_notfound(self) -> None:
        # NoSuchBucket is ALSO an HTTP 404, but it means the deployment is
        # misconfigured — presenting it as "artifact absent" would hide the
        # breakage behind client-facing 404s (codex round-0 P1).
        backend = S3Backend(bucket="b", client=_FakeS3Client(bucket_missing=True))
        with pytest.raises(ClientError, match="NoSuchBucket"):
            backend.load(_HEX)
        with pytest.raises(ClientError, match="NoSuchBucket"):
            backend.store(_HEX, b"x")

    def test_conditional_conflict_retries_once_then_succeeds(self) -> None:
        # 409 ConditionalRequestConflict is AWS's retryable response to a
        # concurrent If-None-Match write (codex round-1 P3).
        client = _FakeS3Client(conflicts=1)
        backend = S3Backend(bucket="b", client=client)
        backend.store(_HEX, b"payload")
        assert backend.load(_HEX) == b"payload"

    def test_persistent_conditional_conflict_propagates(self) -> None:
        backend = S3Backend(bucket="b", client=_FakeS3Client(conflicts=5))
        with pytest.raises(ClientError, match="ConditionalRequestConflict"):
            backend.store(_HEX, b"payload")

    @pytest.mark.parametrize("bad_id", ["", "UPPER", "../../etc", "ab", _HEX + "0"])
    def test_invalid_opaque_id_rejected(self, bad_id: str) -> None:
        backend = S3Backend(bucket="b", client=_FakeS3Client())
        with pytest.raises(ValueError, match="invalid opaque artifact id"):
            backend.store(bad_id, b"x")
        with pytest.raises(ValueError, match="invalid opaque artifact id"):
            backend.load(bad_id)

    def test_empty_bucket_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty bucket"):
            S3Backend(bucket="", client=_FakeS3Client())

    def test_missing_boto3_raises_install_guidance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # None in sys.modules makes `import boto3` raise ImportError —
        # simulating a plain eden-storage install without the s3 extra.
        monkeypatch.setitem(sys.modules, "boto3", None)
        with pytest.raises(RuntimeError, match=r"eden-storage\[s3\]"):
            S3Backend(bucket="b")


class _FakeGcsBlob:
    def __init__(self, objects: dict[str, bytes], name: str) -> None:
        self._objects = objects
        self.name = name

    def upload_from_string(
        self, data: bytes | str, if_generation_match: int | None = None
    ) -> None:
        if if_generation_match != 0:
            # The create-only precondition IS the §5.4 no-overwrite
            # guarantee; uploading without it would silently overwrite.
            raise AssertionError("upload must pass if_generation_match=0")
        if self.name in self._objects:
            raise PreconditionFailed(f"object {self.name} already exists")
        self._objects[self.name] = (
            data if isinstance(data, bytes) else data.encode("utf-8")
        )

    def download_as_bytes(self) -> bytes:
        if self.name not in self._objects:
            raise GcsNotFound(f"object {self.name} not found")
        return self._objects[self.name]


class _FakeGcsBucket:
    def __init__(
        self,
        objects: dict[str, bytes],
        name: str,
        *,
        missing: bool = False,
        exists_forbidden: bool = False,
    ) -> None:
        self._objects = objects
        self.name = name
        self._missing = missing
        self._exists_forbidden = exists_forbidden

    def blob(self, name: str) -> _FakeGcsBlob | _FakeGcsMissingBucketBlob:
        if self._missing:
            # A missing BUCKET surfaces as the same google.api_core NotFound
            # the missing-object path raises — only at download time.
            return _FakeGcsMissingBucketBlob(name)
        return _FakeGcsBlob(self._objects, name)

    def exists(self) -> bool:
        if self._exists_forbidden:
            raise Forbidden("missing storage.buckets.get")
        return not self._missing


class _FakeGcsMissingBucketBlob:
    def __init__(self, name: str) -> None:
        self.name = name

    def upload_from_string(
        self, data: bytes | str, if_generation_match: int | None = None
    ) -> None:
        raise GcsNotFound("The specified bucket does not exist")

    def download_as_bytes(self) -> bytes:
        raise GcsNotFound("The specified bucket does not exist")


class _FakeGcsClient:
    def __init__(
        self, *, bucket_missing: bool = False, exists_forbidden: bool = False
    ) -> None:
        self.objects: dict[str, bytes] = {}
        self.bucket_names: list[str] = []
        self._bucket_missing = bucket_missing
        self._exists_forbidden = exists_forbidden

    def bucket(self, name: str) -> _FakeGcsBucket:
        self.bucket_names.append(name)
        return _FakeGcsBucket(
            self.objects,
            name,
            missing=self._bucket_missing,
            exists_forbidden=self._exists_forbidden,
        )


class TestGcsBackend:
    def test_store_then_load_roundtrips(self) -> None:
        client = _FakeGcsClient()
        backend = GcsBackend(bucket="b", client=client)
        backend.store(_HEX, b"payload")
        assert backend.load(_HEX) == b"payload"
        assert client.bucket_names == ["b"]
        assert client.objects == {_HEX: b"payload"}

    def test_prefix_namespaces_the_object_name(self) -> None:
        client = _FakeGcsClient()
        backend = GcsBackend(bucket="b", prefix="/eden/exp-1/", client=client)
        backend.store(_HEX, b"x")
        assert list(client.objects) == [f"eden/exp-1/{_HEX}"]
        assert backend.load(_HEX) == b"x"

    def test_store_reused_id_raises_file_exists(self) -> None:
        # §5.4 no-overwrite via GCS's native if_generation_match=0.
        backend = GcsBackend(bucket="b", client=_FakeGcsClient())
        backend.store(_HEX, b"one")
        with pytest.raises(FileExistsError):
            backend.store(_HEX, b"two")
        assert backend.load(_HEX) == b"one"

    def test_load_absent_raises_not_found(self) -> None:
        backend = GcsBackend(bucket="b", client=_FakeGcsClient())
        with pytest.raises(NotFound):
            backend.load(_HEX)

    def test_missing_bucket_propagates_not_notfound(self) -> None:
        # google.api_core's NotFound covers bucket-level 404s too; the
        # backend disambiguates via bucket.exists() and propagates the
        # deployment error instead of reporting a missing artifact
        # (codex round-0 P1).
        backend = GcsBackend(bucket="b", client=_FakeGcsClient(bucket_missing=True))
        with pytest.raises(GcsNotFound, match="bucket"):
            backend.load(_HEX)

    def test_exists_forbidden_falls_back_to_not_found(self) -> None:
        # Least-privilege roles (object-only) lack storage.buckets.get, so
        # bucket.exists() raises Forbidden. The backend must NOT turn every
        # absent artifact into a 403 in that posture; it assumes the common
        # case (object-level absence) instead (codex round-1 P2).
        backend = GcsBackend(
            bucket="b", client=_FakeGcsClient(exists_forbidden=True)
        )
        with pytest.raises(NotFound):
            backend.load(_HEX)

    @pytest.mark.parametrize("bad_id", ["", "UPPER", "../../etc", "ab", _HEX + "0"])
    def test_invalid_opaque_id_rejected(self, bad_id: str) -> None:
        backend = GcsBackend(bucket="b", client=_FakeGcsClient())
        with pytest.raises(ValueError, match="invalid opaque artifact id"):
            backend.store(bad_id, b"x")
        with pytest.raises(ValueError, match="invalid opaque artifact id"):
            backend.load(bad_id)

    def test_empty_bucket_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty bucket"):
            GcsBackend(bucket="", client=_FakeGcsClient())
