"""Environment-isolation regression tests.

Covers the three isolation fixes addressed after round-0 codex review:

- Repo-redirecting env vars (``GIT_DIR``, ``GIT_INDEX_FILE``, …) must be
  stripped so ambient state on the developer machine cannot steer the
  wrapper to a different target.
- ``init`` / ``init_bare`` must run under a sanitized environment so
  system / global git config (template dir, signing config, hook
  paths) does not leak into freshly created repos.
- ``create_ref`` must use a zero-OID sized to the repo's hash
  algorithm, so the §1.2 `trial/*` CAS-against-absent primitive works
  under SHA-256 as well as SHA-1.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from eden_git import GitRepo, TreeEntry
from eden_git.repo import _git_env_vars_to_strip, _sanitized_git_env

FIXED_DATE = "2026-04-23T00:00:00+00:00"


class TestSanitizedEnv:
    """The helper that builds every child env."""

    def test_strips_every_listed_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        strip = _git_env_vars_to_strip()
        for var in strip:
            monkeypatch.setenv(var, "poison-value")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.signingkey")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "DEADBEEF")
        env = _sanitized_git_env()
        for var in strip:
            assert var not in env
        # Indexed GIT_CONFIG_* pairs are stripped too (not just COUNT).
        assert "GIT_CONFIG_KEY_0" not in env
        assert "GIT_CONFIG_VALUE_0" not in env

    def test_strip_set_covers_git_local_env_vars(self) -> None:
        """The strip set must be a superset of git's own `--local-env-vars`.

        Drift here is exactly the class of bug Round-1 caught:
        missing a var like ``GIT_GRAFT_FILE`` lets ambient state
        fabricate commit parents and spoof ancestry checks.
        """
        probe = subprocess.run(
            ["git", "rev-parse", "--local-env-vars"],
            capture_output=True,
            text=True,
            check=True,
        )
        declared = {tok.strip() for tok in probe.stdout.split() if tok.strip()}
        missing = declared - _git_env_vars_to_strip()
        assert not missing, f"git reports these local-env-vars that our strip set misses: {missing}"

    def test_pins_config_isolation(self) -> None:
        env = _sanitized_git_env()
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_TERMINAL_PROMPT"] == "0"


class TestRedirectingEnvIsIgnored:
    """Ambient ``GIT_*`` env vars must not steer the wrapper."""

    def test_git_dir_does_not_redirect(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``GIT_DIR`` in the parent env must not reroute our operations."""
        repo = GitRepo.init_bare(tmp_path / "real.git")
        decoy = tmp_path / "decoy.git"
        decoy.mkdir()
        monkeypatch.setenv("GIT_DIR", str(decoy))
        # Every read and write must still hit real.git, not decoy.git.
        blob = repo.write_blob(b"isolated\n")
        assert repo.read_blob(blob) == b"isolated\n"
        # The decoy dir should still be empty of any git objects we
        # wrote, since the wrapper stripped GIT_DIR.
        assert not (decoy / "objects" / blob[:2]).exists()

    def test_git_index_file_does_not_redirect(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        repo_with_main: tuple[GitRepo, str],
    ) -> None:
        """An ambient ``GIT_INDEX_FILE`` must not escape into plumbing ops."""
        repo, seed = repo_with_main
        decoy_index = tmp_path / "decoy-index"
        monkeypatch.setenv("GIT_INDEX_FILE", str(decoy_index))
        manifest = repo.write_blob(b"{}\n")
        seed_tree = repo.commit_tree_sha(seed)
        # This should succeed without ever touching decoy-index.
        repo.write_tree_with_file(seed_tree, ".eden/trials/t1/eval.json", manifest)
        assert not decoy_index.exists()

    def test_git_graft_file_cannot_fabricate_parents(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        repo_with_main: tuple[GitRepo, str],
    ) -> None:
        """Ambient ``GIT_GRAFT_FILE`` must not spoof commit ancestry.

        Without stripping, a caller who sets ``GIT_GRAFT_FILE`` can
        make any commit claim any parent, which would let an attacker
        fool the §1.4 reachability check the Phase 7b integrator
        performs via ``commit_parents`` / ``is_ancestor``.
        """
        from eden_git import Identity

        repo, seed = repo_with_main
        # Build two unrelated commits: `seed` and `unrelated`.
        unrelated_blob = repo.write_blob(b"unrelated content\n")
        unrelated_tree = repo.write_tree_from_entries(
            [TreeEntry(mode="100644", type="blob", sha=unrelated_blob, path="README")]
        )
        unrelated = repo.commit_tree(
            unrelated_tree,
            parents=[],
            message="unrelated",
            author=Identity(name="T", email="t@example.test"),
            author_date=FIXED_DATE,
            committer_date=FIXED_DATE,
        )
        # Seed a grafts file that claims `seed` has `unrelated` as a
        # parent. Without stripping, git would honor this and make
        # `is_ancestor(unrelated, seed)` return True.
        grafts = tmp_path / "grafts"
        grafts.write_text(f"{seed} {unrelated}\n")
        monkeypatch.setenv("GIT_GRAFT_FILE", str(grafts))

        # Ancestry check must return False (seed and unrelated are
        # still unrelated, grafts file notwithstanding).
        assert repo.is_ancestor(unrelated, seed) is False
        # commit_parents must report the true parents (none for seed,
        # which is a root commit).
        assert repo.commit_parents(seed) == []

    def test_committer_identity_env_does_not_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
        repo_with_main: tuple[GitRepo, str],
    ) -> None:
        """Ambient ``GIT_COMMITTER_NAME`` must not leak into our commits.

        The wrapper strips the var, then ``commit_tree`` sets it back
        explicitly from the caller's ``Identity``. A stale ambient
        value would have survived without the strip step.
        """
        from eden_git import Identity

        monkeypatch.setenv("GIT_COMMITTER_NAME", "stale-ambient")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "stale@example.test")
        repo, seed = repo_with_main
        tree = repo.commit_tree_sha(seed)
        ident = Identity(name="Pinned", email="pinned@example.test")
        commit = repo.commit_tree(
            tree,
            parents=[seed],
            message="probe",
            author=ident,
            author_date=FIXED_DATE,
            committer_date=FIXED_DATE,
        )
        committer = repo._run(
            ["log", "-1", "--format=%cn <%ce>", commit]
        ).stdout.strip()
        assert committer == "Pinned <pinned@example.test>"


class TestInitIsolation:
    """``init`` / ``init_bare`` run under the same sanitized env."""

    def test_init_ignores_poisoned_global_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A malformed ``GIT_CONFIG_GLOBAL`` must not reach init."""
        poison = tmp_path / "poison.gitconfig"
        poison.write_text("[this is not valid git config\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(poison))
        # Wrapper pins GIT_CONFIG_GLOBAL=/dev/null so init succeeds.
        repo = GitRepo.init_bare(tmp_path / "clean.git")
        assert repo.is_bare()

    def test_init_does_not_copy_user_template(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--template=`` must block template-dir copying."""
        template_dir = tmp_path / "template"
        (template_dir / "hooks").mkdir(parents=True)
        (template_dir / "hooks" / "post-commit").write_text("#!/bin/sh\nexit 1\n")
        monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template_dir))
        # GIT_TEMPLATE_DIR isn't in the strip set by design (it's not a
        # repo-redirecting var), but --template= on the init command
        # line overrides it. Verify the poisoned hook didn't land.
        repo = GitRepo.init_bare(tmp_path / "clean.git")
        hook = repo.path / "hooks" / "post-commit"
        if hook.exists():
            # A git build might still copy the system default template;
            # what we care about is that the custom user template did
            # NOT override it.
            assert hook.read_text() != "#!/bin/sh\nexit 1\n"


class TestZeroOidHashAlgo:
    """``zero_oid`` must match the repo's hash algorithm."""

    def test_sha1_zero_oid_is_40_hex_zeros(self, bare_repo: GitRepo) -> None:
        # git defaults to SHA-1 unless init specified otherwise.
        assert bare_repo.zero_oid() == "0" * 40

    def test_create_ref_uses_derived_zero(
        self, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        """End-to-end: create_ref succeeds and uses zero_oid() internally."""
        repo, seed = repo_with_main
        repo.create_ref("refs/heads/trial/t1-demo", seed)
        assert repo.resolve_ref("refs/heads/trial/t1-demo") == seed

    def test_sha256_zero_oid_is_64_hex_zeros(self, tmp_path: Path) -> None:
        """SHA-256 repos need a 64-char zero OID for CAS-against-absent.

        Skipped if the local ``git`` wasn't built with SHA-256 support.
        """
        # Probe git's SHA-256 support by trying to init with
        # --object-format=sha256 directly. Modern git (2.29+) supports
        # this, but some distro builds exclude it.
        probe = subprocess.run(
            ["git", "init", "--bare", "--object-format=sha256", str(tmp_path / "probe.git")],
            capture_output=True,
            text=True,
            check=False,
            env=_sanitized_git_env(),
        )
        if probe.returncode != 0:
            pytest.skip(f"git does not support SHA-256 in this build: {probe.stderr!r}")

        repo = GitRepo(tmp_path / "probe.git")
        assert repo.zero_oid() == "0" * 64

        # A full plumbing round-trip confirms the derived zero OID is
        # what update-ref expects.
        blob = repo.write_blob(b"sha256 seed\n")
        tree = repo.write_tree_from_entries(
            [TreeEntry(mode="100644", type="blob", sha=blob, path="README")]
        )
        from eden_git import Identity

        commit = repo.commit_tree(
            tree,
            parents=[],
            message="seed",
            author=Identity(name="T", email="t@example.test"),
            author_date=FIXED_DATE,
            committer_date=FIXED_DATE,
        )
        repo.create_ref("refs/heads/main", commit)
        assert repo.resolve_ref("refs/heads/main") == commit


class TestWriteTreeWithFileEdgeCases:
    """Manifest-path collision cases beyond the basic blob-collision test."""

    def test_rejects_when_target_is_nested_under_existing_blob(
        self, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        """A blob at an intermediate path would make the add a type conflict.

        E.g., base tree has `.eden` as a blob, and we try to add
        `.eden/trials/t1/eval.json` — git can't nest under a blob.
        """
        repo, _ = repo_with_main
        # Build a tree where `.eden` is a blob (file), then try to add
        # under it.
        poison_blob = repo.write_blob(b"not a directory\n")
        seed_blob = repo.write_blob(b"seed\n")
        tree_with_eden_as_file = repo.write_tree_from_entries(
            [
                TreeEntry(mode="100644", type="blob", sha=seed_blob, path="README"),
                TreeEntry(mode="100644", type="blob", sha=poison_blob, path=".eden"),
            ]
        )
        from eden_git import GitError

        manifest = repo.write_blob(b"{}")
        # `tree_entry_exists` at `.eden/trials/t1/eval.json` returns
        # false (no match), so write_tree_with_file will attempt the
        # update-index. Git itself should reject because `.eden` is a
        # file, not a tree.
        with pytest.raises(GitError):
            repo.write_tree_with_file(
                tree_with_eden_as_file,
                ".eden/trials/t1/eval.json",
                manifest,
            )

    def test_rejects_path_that_exists_as_symlink(
        self, repo_with_main: tuple[GitRepo, str]
    ) -> None:
        """A symlink at the manifest path also counts as pre-existing.

        Mode 120000 (symlink) is still a tree entry, so
        ``tree_entry_exists`` should see it and reject.
        """
        repo, _ = repo_with_main
        # Symlink target content is the path string, stored as a blob.
        link_target = repo.write_blob(b"../../../somewhere")
        seed_blob = repo.write_blob(b"seed\n")
        inner_tree = repo.write_tree_from_entries(
            [
                TreeEntry(
                    mode="120000", type="blob", sha=link_target, path="eval.json"
                )
            ]
        )
        trials_tree = repo.write_tree_from_entries(
            [TreeEntry(mode="040000", type="tree", sha=inner_tree, path="t1")]
        )
        eden_tree = repo.write_tree_from_entries(
            [TreeEntry(mode="040000", type="tree", sha=trials_tree, path="trials")]
        )
        root_tree = repo.write_tree_from_entries(
            [
                TreeEntry(mode="100644", type="blob", sha=seed_blob, path="README"),
                TreeEntry(mode="040000", type="tree", sha=eden_tree, path=".eden"),
            ]
        )
        from eden_git import GitError

        manifest = repo.write_blob(b"{}")
        with pytest.raises(GitError):
            repo.write_tree_with_file(
                root_tree, ".eden/trials/t1/eval.json", manifest
            )
