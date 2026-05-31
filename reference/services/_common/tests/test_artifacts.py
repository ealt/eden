"""Unit tests for the hierarchical artifact substrate (issue #168).

Covers the three ``(producer, entity_id) → dir`` mappings, the full §D.2
filename policy per write branch, the predict/write agreement, and the §5.4
no-overwrite guarantee. The bundle / read-side cases (moved here from the
web-UI's ``test_artifact_bundle.py`` when the writer relocated to
``eden_service_common``) round out the coverage.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from eden_service_common.artifacts import (
    MANIFEST_NAME,
    MANIFEST_VERSION,
    UploadedFile,
    entity_artifact_dir,
    idea_naming,
    is_bundle_uri,
    predict_artifact_uri,
    read_bundle_entry,
    read_bundle_manifest,
    submission_naming,
    write_artifact_bundle,
)

# --------------------------------------------------------------- path-builder


def test_entity_artifact_dir_ideator(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-1")
    assert d == (tmp_path.resolve() / "ideas" / "idea-1")


def test_entity_artifact_dir_executor(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="executor", entity_id="var-1")
    assert d == (tmp_path.resolve() / "variants" / "var-1" / "executor")


def test_entity_artifact_dir_evaluator(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="evaluator", entity_id="var-1")
    assert d == (tmp_path.resolve() / "variants" / "var-1" / "evaluator")


def test_entity_artifact_dir_unknown_producer_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown artifact producer"):
        entity_artifact_dir(tmp_path, producer="integrator", entity_id="x")


# ------------------------------------------------------------- filename policy


def test_idea_text_only_writes_content_md(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-1")
    uri = write_artifact_bundle(
        d, idea_naming(), text_content="# hello", uploads=[]
    )
    assert uri.endswith("/ideas/idea-1/content.md")
    assert (d / "content.md").read_text() == "# hello"
    assert not is_bundle_uri(uri)


def test_idea_single_upload_keeps_original_name(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-2")
    up = UploadedFile(filename="diagram.svg", data=b"<svg/>", content_type=None)
    uri = write_artifact_bundle(
        d, idea_naming(), text_content=None, uploads=[up]
    )
    assert uri.endswith("/ideas/idea-2/diagram.svg")
    assert (d / "diagram.svg").read_bytes() == b"<svg/>"


def test_idea_bundle_named_bundle_tar_gz(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-3")
    up = UploadedFile(filename="diagram.svg", data=b"<svg/>", content_type=None)
    uri = write_artifact_bundle(
        d, idea_naming(), text_content="# headline", uploads=[up]
    )
    assert uri.endswith("/ideas/idea-3/bundle.tar.gz")
    assert is_bundle_uri(uri)
    manifest = read_bundle_manifest(d / "bundle.tar.gz")
    assert manifest is not None
    # The idea bundle's text headline is content.md (§D.3).
    paths = [e["path"] for e in manifest["entries"]]
    assert paths == ["content.md", "diagram.svg"]


def test_evaluator_text_only_uses_stem(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="evaluator", entity_id="var-1")
    naming = submission_naming("eval-abc", headline="evaluation.md")
    uri = write_artifact_bundle(d, naming, text_content="score 9", uploads=[])
    assert uri.endswith("/variants/var-1/evaluator/eval-abc.md")


def test_evaluator_single_upload_is_stem_plus_ext(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="evaluator", entity_id="var-1")
    naming = submission_naming("eval-abc", headline="evaluation.md")
    up = UploadedFile(filename="report.pdf", data=b"%PDF", content_type=None)
    uri = write_artifact_bundle(d, naming, text_content=None, uploads=[up])
    assert uri.endswith("/variants/var-1/evaluator/eval-abc.pdf")


def test_evaluator_bundle_headline_is_evaluation_md(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="evaluator", entity_id="var-1")
    naming = submission_naming("eval-abc", headline="evaluation.md")
    up = UploadedFile(filename="report.pdf", data=b"%PDF", content_type=None)
    uri = write_artifact_bundle(d, naming, text_content="notes", uploads=[up])
    assert uri.endswith("/variants/var-1/evaluator/eval-abc.tar.gz")
    manifest = read_bundle_manifest(d / "eval-abc.tar.gz")
    assert manifest is not None
    paths = [e["path"] for e in manifest["entries"]]
    # On-disk tarball is stem-named; the text entry inside stays role-coherent.
    assert paths == ["evaluation.md", "report.pdf"]


def test_executor_bundle_headline_is_variant_md(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="executor", entity_id="var-1")
    naming = submission_naming("exec-xyz", headline="variant.md")
    up = UploadedFile(filename="out.bin", data=b"x", content_type=None)
    uri = write_artifact_bundle(d, naming, text_content="impl notes", uploads=[up])
    assert uri.endswith("/variants/var-1/executor/exec-xyz.tar.gz")
    manifest = read_bundle_manifest(d / "exec-xyz.tar.gz")
    assert manifest is not None
    assert [e["path"] for e in manifest["entries"]] == ["variant.md", "out.bin"]


# ----------------------------------------------------------- predict == write


def test_predict_matches_write_text_only(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-5")
    predicted = predict_artifact_uri(
        d, idea_naming(), has_text=True, uploads=[]
    )
    written = write_artifact_bundle(
        d, idea_naming(), text_content="x", uploads=[]
    )
    assert predicted == written


def test_predict_matches_write_single_upload(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="evaluator", entity_id="var-2")
    naming = submission_naming("eval-q", headline="evaluation.md")
    up = UploadedFile(filename="design.PDF", data=b"pdf", content_type=None)
    predicted = predict_artifact_uri(d, naming, has_text=False, uploads=[up])
    written = write_artifact_bundle(d, naming, text_content=None, uploads=[up])
    assert predicted == written
    assert predicted.endswith("/eval-q.PDF")


def test_predict_matches_write_bundle(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-6")
    ups = [
        UploadedFile(filename="x.pdf", data=b"pdf", content_type=None),
        UploadedFile(filename="y.txt", data=b"txt", content_type=None),
    ]
    predicted = predict_artifact_uri(
        d, idea_naming(), has_text=False, uploads=ups
    )
    written = write_artifact_bundle(
        d, idea_naming(), text_content=None, uploads=ups
    )
    assert predicted == written


# ----------------------------------------------------------- no-overwrite §5.4


def test_second_write_to_same_target_raises(tmp_path: Path) -> None:
    """A reused target path must raise rather than clobber (O_EXCL guard)."""
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-7")
    write_artifact_bundle(d, idea_naming(), text_content="one", uploads=[])
    with pytest.raises(FileExistsError):
        write_artifact_bundle(d, idea_naming(), text_content="two", uploads=[])
    # The first write's bytes are intact — not clobbered.
    assert (d / "content.md").read_text() == "one"


def test_second_bundle_write_to_same_target_raises(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="evaluator", entity_id="var-3")
    naming = submission_naming("eval-dup", headline="evaluation.md")
    up = UploadedFile(filename="a.bin", data=b"a", content_type=None)
    write_artifact_bundle(d, naming, text_content="t", uploads=[up])
    with pytest.raises(FileExistsError):
        write_artifact_bundle(d, naming, text_content="t", uploads=[up])


def test_distinct_stems_never_collide(tmp_path: Path) -> None:
    """Two evaluator submissions for one variant land at distinct paths."""
    d = entity_artifact_dir(tmp_path, producer="evaluator", entity_id="var-4")
    uri1 = write_artifact_bundle(
        d, submission_naming("eval-1", headline="evaluation.md"),
        text_content="first", uploads=[],
    )
    uri2 = write_artifact_bundle(
        d, submission_naming("eval-2", headline="evaluation.md"),
        text_content="second", uploads=[],
    )
    assert uri1 != uri2
    assert (d / "eval-1.md").read_text() == "first"
    assert (d / "eval-2.md").read_text() == "second"


# ------------------------------------------------------------ validation paths


def test_empty_inputs_raises(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-8")
    with pytest.raises(ValueError, match="requires text content"):
        write_artifact_bundle(d, idea_naming(), text_content="", uploads=[])


def test_whitespace_only_text_is_empty(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-9")
    with pytest.raises(ValueError, match="requires text content"):
        write_artifact_bundle(
            d, idea_naming(), text_content="  \n\t ", uploads=[]
        )


def test_duplicate_filename_rejected(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-10")
    ups = [
        UploadedFile(filename="a.md", data=b"one", content_type=None),
        UploadedFile(filename="a.md", data=b"two", content_type=None),
    ]
    with pytest.raises(ValueError, match="duplicate filename"):
        write_artifact_bundle(d, idea_naming(), text_content=None, uploads=ups)


def test_text_headline_collides_with_upload_rejected(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-11")
    # An upload named content.md collides with the idea bundle's headline.
    ups = [UploadedFile(filename="content.md", data=b"x", content_type=None)]
    with pytest.raises(ValueError, match="duplicate"):
        write_artifact_bundle(
            d, idea_naming(), text_content="primary", uploads=ups
        )


def test_manifest_filename_reserved(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-12")
    ups = [
        UploadedFile(filename="x.txt", data=b"x", content_type=None),
        UploadedFile(filename=MANIFEST_NAME, data=b"{}", content_type=None),
    ]
    with pytest.raises(ValueError, match="reserved"):
        write_artifact_bundle(d, idea_naming(), text_content=None, uploads=ups)


def test_path_traversal_sanitized(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-13")
    ups = [
        UploadedFile(
            filename="../../../etc/passwd", data=b"x", content_type=None
        ),
        UploadedFile(filename="b.txt", data=b"y", content_type=None),
    ]
    write_artifact_bundle(d, idea_naming(), text_content=None, uploads=ups)
    with tarfile.open(d / "bundle.tar.gz", mode="r:gz") as tar:
        names = tar.getnames()
    assert "passwd" in names
    assert "../../../etc/passwd" not in names


# ------------------------------------------------------------------- bundle io


def test_bundle_contains_manifest_json(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-14")
    write_artifact_bundle(
        d, idea_naming(), text_content="hello",
        uploads=[UploadedFile(filename="x.bin", data=b"x", content_type=None)],
    )
    with tarfile.open(d / "bundle.tar.gz", mode="r:gz") as tar:
        names = tar.getnames()
    assert MANIFEST_NAME in names
    assert "content.md" in names
    assert "x.bin" in names


def test_read_bundle_entry_round_trip(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-15")
    write_artifact_bundle(
        d, idea_naming(), text_content="hi",
        uploads=[UploadedFile(filename="a.bin", data=b"abc", content_type=None)],
    )
    bundle = d / "bundle.tar.gz"
    assert read_bundle_entry(bundle, "a.bin") == b"abc"
    assert read_bundle_entry(bundle, "content.md") == b"hi"
    assert read_bundle_entry(bundle, "nope") is None


def test_read_bundle_entry_oversize_returns_none(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-16")
    write_artifact_bundle(
        d, idea_naming(), text_content="hi",
        uploads=[
            UploadedFile(filename="big.bin", data=b"x" * 1024, content_type=None)
        ],
    )
    assert read_bundle_entry(
        d / "bundle.tar.gz", "big.bin", max_bytes=100
    ) is None


def test_read_bundle_manifest_corrupt_returns_none(tmp_path: Path) -> None:
    (tmp_path / "bogus.tar.gz").write_bytes(b"not a tarball")
    assert read_bundle_manifest(tmp_path / "bogus.tar.gz") is None


def test_atomic_write_no_partial_files(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-17")
    write_artifact_bundle(
        d, idea_naming(), text_content="hi",
        uploads=[UploadedFile(filename="a.bin", data=b"a", content_type=None)],
    )
    assert list(d.glob("*.tmp")) == []


def test_manifest_json_shape(tmp_path: Path) -> None:
    d = entity_artifact_dir(tmp_path, producer="ideator", entity_id="idea-18")
    ups = [
        UploadedFile(
            filename="diagram.svg", data=b"<svg/>", content_type="image/svg+xml"
        ),
    ]
    write_artifact_bundle(d, idea_naming(), text_content="# h", uploads=ups)
    with tarfile.open(d / "bundle.tar.gz", mode="r:gz") as tar:
        f = tar.extractfile(MANIFEST_NAME)
        assert f is not None
        manifest = json.loads(f.read())
    assert manifest["version"] == MANIFEST_VERSION
    assert {"path", "size", "content_type"} <= manifest["entries"][0].keys()
