"""Unit tests for the multi-file artifact bundler (issue #120)."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from eden_web_ui.artifacts import (
    MANIFEST_NAME,
    MANIFEST_VERSION,
    UploadedFile,
    is_bundle_uri,
    predict_artifact_uri,
    read_bundle_entry,
    read_bundle_manifest,
    write_artifact_bundle,
)


def test_text_only_writes_single_md(tmp_path: Path) -> None:
    uri = write_artifact_bundle(
        tmp_path, "idea-1",
        text_content="# hello world",
        text_filename="idea.md",
        uploads=[],
    )
    assert uri.endswith("/idea-1.md")
    assert (tmp_path / "idea-1.md").read_text() == "# hello world"
    assert not is_bundle_uri(uri)


def test_single_upload_no_text_writes_direct(tmp_path: Path) -> None:
    up = UploadedFile(filename="diagram.svg", data=b"<svg/>", content_type=None)
    uri = write_artifact_bundle(
        tmp_path, "idea-2",
        text_content=None,
        text_filename="idea.md",
        uploads=[up],
    )
    assert uri.endswith("/idea-2.svg")
    assert (tmp_path / "idea-2.svg").read_bytes() == b"<svg/>"


def test_text_plus_one_upload_writes_bundle(tmp_path: Path) -> None:
    up = UploadedFile(filename="diagram.svg", data=b"<svg/>", content_type=None)
    uri = write_artifact_bundle(
        tmp_path, "idea-3",
        text_content="# headline",
        text_filename="idea.md",
        uploads=[up],
    )
    assert uri.endswith("/idea-3.tar.gz")
    assert is_bundle_uri(uri)

    bundle_path = tmp_path / "idea-3.tar.gz"
    manifest = read_bundle_manifest(bundle_path)
    assert manifest is not None
    assert manifest["version"] == MANIFEST_VERSION
    paths = [e["path"] for e in manifest["entries"]]
    assert paths == ["idea.md", "diagram.svg"]
    assert manifest["entries"][1]["content_type"] == "image/svg+xml"


def test_multi_upload_no_text_writes_bundle(tmp_path: Path) -> None:
    ups = [
        UploadedFile(filename="a.pdf", data=b"%PDF-1.4", content_type=None),
        UploadedFile(filename="b.txt", data=b"hi", content_type=None),
    ]
    uri = write_artifact_bundle(
        tmp_path, "idea-4",
        text_content=None,
        text_filename="idea.md",
        uploads=ups,
    )
    assert uri.endswith("/idea-4.tar.gz")
    manifest = read_bundle_manifest(tmp_path / "idea-4.tar.gz")
    assert manifest is not None
    paths = [e["path"] for e in manifest["entries"]]
    assert paths == ["a.pdf", "b.txt"]


def test_bundle_contains_manifest_json(tmp_path: Path) -> None:
    write_artifact_bundle(
        tmp_path, "idea-5",
        text_content="hello",
        text_filename="idea.md",
        uploads=[UploadedFile(filename="x.bin", data=b"x", content_type=None)],
    )
    with tarfile.open(tmp_path / "idea-5.tar.gz", mode="r:gz") as tar:
        names = tar.getnames()
    assert MANIFEST_NAME in names
    assert "idea.md" in names
    assert "x.bin" in names


def test_empty_inputs_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires text content"):
        write_artifact_bundle(
            tmp_path, "idea-6",
            text_content="",
            text_filename="idea.md",
            uploads=[],
        )


def test_whitespace_only_text_is_empty(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires text content"):
        write_artifact_bundle(
            tmp_path, "idea-7",
            text_content="   \n  \t  ",
            text_filename="idea.md",
            uploads=[],
        )


def test_duplicate_filename_rejected(tmp_path: Path) -> None:
    ups = [
        UploadedFile(filename="a.md", data=b"one", content_type=None),
        UploadedFile(filename="a.md", data=b"two", content_type=None),
    ]
    with pytest.raises(ValueError, match="duplicate filename"):
        write_artifact_bundle(
            tmp_path, "idea-8",
            text_content=None,
            text_filename="idea.md",
            uploads=ups,
        )


def test_text_collides_with_upload_rejected(tmp_path: Path) -> None:
    ups = [UploadedFile(filename="idea.md", data=b"override", content_type=None)]
    with pytest.raises(ValueError, match="duplicate"):
        write_artifact_bundle(
            tmp_path, "idea-9",
            text_content="primary",
            text_filename="idea.md",
            uploads=ups,
        )


def test_manifest_filename_reserved(tmp_path: Path) -> None:
    ups = [
        UploadedFile(filename="x.txt", data=b"x", content_type=None),
        UploadedFile(filename=MANIFEST_NAME, data=b"{}", content_type=None),
    ]
    with pytest.raises(ValueError, match="reserved"):
        write_artifact_bundle(
            tmp_path, "idea-10",
            text_content=None,
            text_filename="idea.md",
            uploads=ups,
        )


def test_path_traversal_sanitized(tmp_path: Path) -> None:
    ups = [
        UploadedFile(
            filename="../../../etc/passwd", data=b"x", content_type=None
        ),
        UploadedFile(filename="b.txt", data=b"y", content_type=None),
    ]
    write_artifact_bundle(
        tmp_path, "idea-11",
        text_content=None,
        text_filename="idea.md",
        uploads=ups,
    )
    with tarfile.open(tmp_path / "idea-11.tar.gz", mode="r:gz") as tar:
        names = tar.getnames()
    # The traversal path is stripped to its basename "passwd".
    assert "passwd" in names
    assert "../../../etc/passwd" not in names


def test_predict_matches_write(tmp_path: Path) -> None:
    ups = [
        UploadedFile(filename="x.pdf", data=b"pdf", content_type=None),
        UploadedFile(filename="y.txt", data=b"txt", content_type=None),
    ]
    predicted = predict_artifact_uri(
        tmp_path, "idea-12", has_text=False, uploads=ups
    )
    written = write_artifact_bundle(
        tmp_path, "idea-12",
        text_content=None,
        text_filename="idea.md",
        uploads=ups,
    )
    assert predicted == written


def test_predict_single_upload_uses_original_extension(tmp_path: Path) -> None:
    ups = [UploadedFile(filename="design.pdf", data=b"", content_type=None)]
    uri = predict_artifact_uri(tmp_path, "id-x", has_text=False, uploads=ups)
    assert uri.endswith("/id-x.pdf")


def test_read_bundle_entry_returns_bytes(tmp_path: Path) -> None:
    ups = [UploadedFile(filename="a.bin", data=b"abc", content_type=None)]
    write_artifact_bundle(
        tmp_path, "idea-13",
        text_content="hi",
        text_filename="idea.md",
        uploads=ups,
    )
    data = read_bundle_entry(tmp_path / "idea-13.tar.gz", "a.bin")
    assert data == b"abc"
    data2 = read_bundle_entry(tmp_path / "idea-13.tar.gz", "idea.md")
    assert data2 == b"hi"


def test_read_bundle_entry_missing_returns_none(tmp_path: Path) -> None:
    write_artifact_bundle(
        tmp_path, "idea-14",
        text_content="hi",
        text_filename="idea.md",
        uploads=[UploadedFile(filename="a.bin", data=b"a", content_type=None)],
    )
    assert read_bundle_entry(tmp_path / "idea-14.tar.gz", "nope.bin") is None


def test_read_bundle_entry_oversize_returns_none(tmp_path: Path) -> None:
    write_artifact_bundle(
        tmp_path, "idea-15",
        text_content="hi",
        text_filename="idea.md",
        uploads=[UploadedFile(filename="big.bin", data=b"x" * 1024, content_type=None)],
    )
    assert read_bundle_entry(
        tmp_path / "idea-15.tar.gz", "big.bin", max_bytes=100
    ) is None


def test_read_bundle_manifest_corrupt_returns_none(tmp_path: Path) -> None:
    (tmp_path / "bogus.tar.gz").write_bytes(b"not a tarball")
    assert read_bundle_manifest(tmp_path / "bogus.tar.gz") is None


def test_zero_byte_upload_round_trips(tmp_path: Path) -> None:
    """A 0-byte uploaded file is a legal entry in the bundle."""
    ups = [UploadedFile(filename="empty.txt", data=b"", content_type=None)]
    write_artifact_bundle(
        tmp_path, "idea-16",
        text_content="hi",
        text_filename="idea.md",
        uploads=ups,
    )
    data = read_bundle_entry(tmp_path / "idea-16.tar.gz", "empty.txt")
    assert data == b""


def test_atomic_write_no_partial_files(tmp_path: Path) -> None:
    """After a successful write there should be no `.tmp` siblings left."""
    write_artifact_bundle(
        tmp_path, "idea-17",
        text_content="hi",
        text_filename="idea.md",
        uploads=[UploadedFile(filename="a.bin", data=b"a", content_type=None)],
    )
    tmps = list(tmp_path.glob("*.tmp"))
    assert tmps == []


class TestBundleEntryServingSecurity:
    """Issue #120: the ``?entry=`` query param must not escape the bundle."""

    def _setup_signed_in(self, store, artifacts_dir):
        from datetime import UTC, datetime

        from conftest import EXPERIMENT_ID, SESSION_SECRET, WORKER_ID, _config
        from eden_web_ui import make_app
        from fastapi.testclient import TestClient

        app = make_app(
            store=store,
            admin_store=store,
            experiment_id=EXPERIMENT_ID,
            experiment_config=_config(),
            worker_id=WORKER_ID,
            session_secret=SESSION_SECRET,
            claim_ttl_seconds=3600,
            artifacts_dir=artifacts_dir,
            secure_cookies=False,
            now=lambda: datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
        )
        c = TestClient(app)
        c.post("/signin", follow_redirects=False)
        return c

    def _make_bundle(self, artifacts_dir):
        write_artifact_bundle(
            artifacts_dir, "bundle-x",
            text_content="headline",
            text_filename="idea.md",
            uploads=[UploadedFile(filename="payload.txt", data=b"safe", content_type=None)],
        )
        return artifacts_dir / "bundle-x.tar.gz"

    def test_entry_slash_rejected(self, store, artifacts_dir) -> None:
        from urllib.parse import quote
        self._make_bundle(artifacts_dir)
        c = self._setup_signed_in(store, artifacts_dir)
        uri = f"file://{(artifacts_dir / 'bundle-x.tar.gz').resolve()}"
        resp = c.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=a/b"
        )
        assert resp.status_code == 400

    def test_entry_dotdot_rejected(self, store, artifacts_dir) -> None:
        from urllib.parse import quote
        self._make_bundle(artifacts_dir)
        c = self._setup_signed_in(store, artifacts_dir)
        uri = f"file://{(artifacts_dir / 'bundle-x.tar.gz').resolve()}"
        resp = c.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=.."
        )
        assert resp.status_code == 400

    def test_entry_param_on_non_bundle_rejected(
        self, store, artifacts_dir
    ) -> None:
        from urllib.parse import quote
        target = artifacts_dir / "plain.md"
        target.write_text("not a bundle")
        c = self._setup_signed_in(store, artifacts_dir)
        uri = f"file://{target.resolve()}"
        resp = c.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=anything.txt"
        )
        assert resp.status_code == 400

    def test_missing_entry_returns_404(self, store, artifacts_dir) -> None:
        from urllib.parse import quote
        self._make_bundle(artifacts_dir)
        c = self._setup_signed_in(store, artifacts_dir)
        uri = f"file://{(artifacts_dir / 'bundle-x.tar.gz').resolve()}"
        resp = c.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=does-not-exist.txt"
        )
        assert resp.status_code == 404


def test_manifest_json_shape(tmp_path: Path) -> None:
    ups = [
        UploadedFile(filename="diagram.svg", data=b"<svg/>", content_type="image/svg+xml"),
    ]
    write_artifact_bundle(
        tmp_path, "idea-18",
        text_content="# headline",
        text_filename="idea.md",
        uploads=ups,
    )
    with tarfile.open(tmp_path / "idea-18.tar.gz", mode="r:gz") as tar:
        f = tar.extractfile(MANIFEST_NAME)
        assert f is not None
        manifest = json.loads(f.read())
    assert manifest["version"] == MANIFEST_VERSION
    assert {"path", "size", "content_type"} <= manifest["entries"][0].keys()
