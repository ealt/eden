"""Plumbing primitives on ``GitRepo``.

Covers object-database ops (``write_blob``, ``empty_tree_sha``,
``write_tree_from_entries``, ``write_tree_with_file``), commit
construction (``commit_tree``, ``commit_tree_sha``, ``commit_parents``,
``commit_message_subject``), ref management (``create_ref``,
``update_ref``, ``delete_ref``, ``list_refs``), and tree inspection
(``ls_tree``, ``tree_entry_exists``, ``read_blob``). Every test runs
against a real ``git`` subprocess against a fresh ``tmp_path`` bare
repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eden_git import GitError, GitRepo, Identity, TreeEntry

FIXED_DATE = "2026-04-23T00:00:00+00:00"
TEST_AUTHOR = Identity(name="EDEN Test", email="test@eden.example")


class TestBlobsAndTrees:
    """Object-database primitives."""

    def test_write_blob_returns_stable_sha(self, bare_repo: GitRepo) -> None:
        sha1 = bare_repo.write_blob(b"hello\n")
        sha2 = bare_repo.write_blob(b"hello\n")
        assert sha1 == sha2
        assert len(sha1) == 40
        assert bare_repo.read_blob(sha1) == b"hello\n"

    def test_write_blob_handles_binary_content(self, bare_repo: GitRepo) -> None:
        payload = bytes(range(256))
        sha = bare_repo.write_blob(payload)
        assert bare_repo.read_blob(sha) == payload

    def test_empty_tree_sha_resolvable(self, bare_repo: GitRepo) -> None:
        tree = bare_repo.empty_tree_sha()
        assert len(tree) == 40
        assert bare_repo.ls_tree(tree) == []

    def test_write_tree_from_entries_roundtrip(self, bare_repo: GitRepo) -> None:
        blob_a = bare_repo.write_blob(b"a\n")
        blob_b = bare_repo.write_blob(b"b\n")
        tree = bare_repo.write_tree_from_entries(
            [
                TreeEntry(mode="100644", type="blob", sha=blob_a, path="a.txt"),
                TreeEntry(mode="100644", type="blob", sha=blob_b, path="b.txt"),
            ]
        )
        entries = {e.path: e for e in bare_repo.ls_tree(tree)}
        assert set(entries) == {"a.txt", "b.txt"}
        assert entries["a.txt"].sha == blob_a
        assert entries["b.txt"].sha == blob_b

    def test_write_tree_with_file_nested(
        self, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        """The eval-manifest path lives under .eden/trials/<id>/eval.json."""
        repo, seed = repo_with_main
        seed_tree = repo.commit_tree_sha(seed)
        manifest = repo.write_blob(b'{"trial_id":"t1"}\n')
        new_tree = repo.write_tree_with_file(
            seed_tree, ".eden/trials/t1/eval.json", manifest
        )

        entries = {e.path: e for e in repo.ls_tree(new_tree, recursive=True)}
        # README survives, manifest appears, no extra entries.
        assert set(entries) == {"README", ".eden/trials/t1/eval.json"}
        assert entries[".eden/trials/t1/eval.json"].sha == manifest

    def test_write_tree_with_file_rejects_existing_path(
        self, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        """§3.2: the eval manifest path must not pre-exist in the worker tree."""
        repo, seed = repo_with_main
        seed_tree = repo.commit_tree_sha(seed)
        other_blob = repo.write_blob(b"collision\n")
        with pytest.raises(GitError):
            repo.write_tree_with_file(seed_tree, "README", other_blob)

    def test_write_tree_with_file_isolates_index(
        self, tmp_path: Path, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        """The operation must not leave state behind in the repo's index."""
        repo, seed = repo_with_main
        seed_tree = repo.commit_tree_sha(seed)
        manifest = repo.write_blob(b"{}")
        repo.write_tree_with_file(seed_tree, ".eden/trials/t1/eval.json", manifest)
        # No lingering .git/index in the bare repo (bare repos don't
        # have one by default; the isolated GIT_INDEX_FILE must not
        # have accidentally written into the bare repo dir).
        assert not (repo.path / "index").exists()


class TestCommitAndRefs:
    """Commit construction and ref management."""

    def test_commit_tree_records_parents_and_tree(
        self, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, seed = repo_with_main
        new_blob = repo.write_blob(b"v2\n")
        new_tree = repo.write_tree_from_entries(
            [TreeEntry(mode="100644", type="blob", sha=new_blob, path="README")]
        )
        commit = repo.commit_tree(
            new_tree,
            parents=[seed],
            message="update",
            author=TEST_AUTHOR,
            author_date=FIXED_DATE,
            committer_date=FIXED_DATE,
        )
        assert repo.commit_exists(commit)
        assert repo.commit_parents(commit) == [seed]
        assert repo.commit_tree_sha(commit) == new_tree
        assert repo.commit_message_subject(commit) == "update"

    def test_commit_tree_no_parents_root_commit(self, bare_repo: GitRepo) -> None:
        tree = bare_repo.empty_tree_sha()
        commit = bare_repo.commit_tree(
            tree,
            parents=[],
            message="root",
            author=TEST_AUTHOR,
            author_date=FIXED_DATE,
            committer_date=FIXED_DATE,
        )
        assert bare_repo.commit_parents(commit) == []

    def test_commit_tree_identity_matches_argument(
        self, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, seed = repo_with_main
        tree = repo.commit_tree_sha(seed)
        author = Identity(name="Integrator", email="integrator@example.test")
        commit = repo.commit_tree(
            tree,
            parents=[seed],
            message="probe",
            author=author,
            author_date=FIXED_DATE,
            committer_date=FIXED_DATE,
        )
        raw = repo._run(["log", "-1", "--format=%an <%ae>", commit]).stdout.strip()
        assert raw == "Integrator <integrator@example.test>"

    def test_create_ref_and_resolve(
        self, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, seed = repo_with_main
        repo.create_ref("refs/heads/trial/t1-demo", seed)
        assert repo.ref_exists("refs/heads/trial/t1-demo")
        assert repo.resolve_ref("refs/heads/trial/t1-demo") == seed

    def test_create_ref_rejects_existing(
        self, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        """§1.2: trial/* branches are immutable — create must not overwrite."""
        repo, seed = repo_with_main
        repo.create_ref("refs/heads/trial/t1-demo", seed)
        with pytest.raises(GitError):
            repo.create_ref("refs/heads/trial/t1-demo", seed)

    def test_update_ref_cas_matches(self, repo_with_main: tuple[GitRepo, str]) -> None:
        repo, seed = repo_with_main
        repo.create_ref("refs/heads/tmp", seed)
        # Create a new commit to move the ref to.
        blob = repo.write_blob(b"v2\n")
        tree = repo.write_tree_from_entries(
            [TreeEntry(mode="100644", type="blob", sha=blob, path="README")]
        )
        newer = repo.commit_tree(
            tree,
            parents=[seed],
            message="v2",
            author=TEST_AUTHOR,
            author_date=FIXED_DATE,
            committer_date=FIXED_DATE,
        )
        repo.update_ref("refs/heads/tmp", newer, expected_old_sha=seed)
        assert repo.resolve_ref("refs/heads/tmp") == newer

    def test_update_ref_cas_mismatch_raises(
        self, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, seed = repo_with_main
        repo.create_ref("refs/heads/tmp", seed)
        with pytest.raises(GitError):
            repo.update_ref(
                "refs/heads/tmp",
                seed,
                expected_old_sha="0" * 40,
            )

    def test_delete_ref(self, repo_with_main: tuple[GitRepo, str]) -> None:
        repo, seed = repo_with_main
        repo.create_ref("refs/heads/tmp", seed)
        assert repo.ref_exists("refs/heads/tmp")
        repo.delete_ref("refs/heads/tmp")
        assert not repo.ref_exists("refs/heads/tmp")
        assert repo.resolve_ref("refs/heads/tmp") is None

    def test_list_refs_filters_by_pattern(
        self, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, seed = repo_with_main
        repo.create_ref("refs/heads/trial/t1-demo", seed)
        repo.create_ref("refs/heads/trial/t2-demo", seed)
        repo.create_ref("refs/heads/work/t1-impl", seed)
        trial_refs = dict(repo.list_refs("refs/heads/trial/*"))
        assert set(trial_refs) == {
            "refs/heads/trial/t1-demo",
            "refs/heads/trial/t2-demo",
        }


class TestAncestryAndInspection:
    """Reachability-rule primitives (§1.4)."""

    def test_is_ancestor_true(self, repo_with_main: tuple[GitRepo, str]) -> None:
        repo, seed = repo_with_main
        blob = repo.write_blob(b"v2\n")
        tree = repo.write_tree_from_entries(
            [TreeEntry(mode="100644", type="blob", sha=blob, path="README")]
        )
        child = repo.commit_tree(
            tree,
            parents=[seed],
            message="child",
            author=TEST_AUTHOR,
            author_date=FIXED_DATE,
            committer_date=FIXED_DATE,
        )
        assert repo.is_ancestor(seed, child) is True
        assert repo.is_ancestor(seed, seed) is True  # self-ancestor

    def test_is_ancestor_false_for_unrelated(
        self, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, seed = repo_with_main
        unrelated_blob = repo.write_blob(b"unrelated\n")
        unrelated_tree = repo.write_tree_from_entries(
            [TreeEntry(mode="100644", type="blob", sha=unrelated_blob, path="README")]
        )
        unrelated = repo.commit_tree(
            unrelated_tree,
            parents=[],
            message="unrelated",
            author=TEST_AUTHOR,
            author_date=FIXED_DATE,
            committer_date=FIXED_DATE,
        )
        assert repo.is_ancestor(unrelated, seed) is False
        assert repo.is_ancestor(seed, unrelated) is False

    def test_tree_entry_exists(self, repo_with_main: tuple[GitRepo, str]) -> None:
        repo, seed = repo_with_main
        tree = repo.commit_tree_sha(seed)
        assert repo.tree_entry_exists(tree, "README") is True
        assert repo.tree_entry_exists(tree, ".eden/trials/t1/eval.json") is False

    def test_ls_tree_recursive_descends(
        self, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, seed = repo_with_main
        seed_tree = repo.commit_tree_sha(seed)
        manifest = repo.write_blob(b"{}")
        new_tree = repo.write_tree_with_file(
            seed_tree, ".eden/trials/t1/eval.json", manifest
        )
        recursive = {e.path for e in repo.ls_tree(new_tree, recursive=True)}
        assert recursive == {"README", ".eden/trials/t1/eval.json"}
        # Non-recursive returns only the top level, showing a tree
        # entry for `.eden`.
        top = {e.path: e.type for e in repo.ls_tree(new_tree)}
        assert top["README"] == "blob"
        assert top[".eden"] == "tree"


class TestRepoIntrospection:
    """rev_parse / commit_exists / ref_exists basics."""

    def test_init_bare_then_rev_parse_fails_empty(self, bare_repo: GitRepo) -> None:
        assert bare_repo.is_bare() is True
        with pytest.raises(GitError):
            bare_repo.rev_parse("HEAD")

    def test_rev_parse_resolves_branch_head(
        self, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, seed = repo_with_main
        assert repo.rev_parse("refs/heads/main") == seed
        assert repo.rev_parse("main") == seed

    def test_commit_exists_false_for_nonexistent_sha(
        self, bare_repo: GitRepo
    ) -> None:
        assert bare_repo.commit_exists("0" * 40) is False

    def test_ref_exists_false_for_absent(self, bare_repo: GitRepo) -> None:
        assert bare_repo.ref_exists("refs/heads/does-not-exist") is False
        assert bare_repo.resolve_ref("refs/heads/does-not-exist") is None
