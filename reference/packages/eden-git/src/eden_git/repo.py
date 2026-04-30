"""Subprocess wrapper around a local git repository.

``GitRepo`` wraps a single repository directory (bare or non-bare) and
exposes the porcelain and plumbing operations the reference integrator
needs (spec/v0 chapter 6). Every failed ``git`` invocation raises
:class:`eden_git.errors.GitError` carrying the executed argv, exit
code, and captured streams.

Design points:

* Each instance is path-scoped: ``GitRepo(repo_path)`` runs commands
  under that directory. Worktrees attached to the same underlying
  repository are addressed by their own ``GitRepo`` instance pointed at
  the worktree path.
* Identity stamping is explicit. ``commit_tree`` takes
  ``author`` / ``committer`` so the caller can pin them; the wrapper
  does not read ``user.name`` / ``user.email`` from the ambient git
  config.
* Plumbing operations (``write_blob``, ``empty_tree_sha``,
  ``write_tree_from_entries``, ``write_tree_with_file``,
  ``commit_tree``, ``create_ref``, ``update_ref``) are the primitives
  the Phase 7b integrator composes into the §3.2 squash. Worktree
  and branch ops are for implementer and operator paths in later
  phases.
* Environment sanitized per invocation. Each child process runs
  with ``GIT_CONFIG_NOSYSTEM=1`` + ``GIT_CONFIG_GLOBAL=/dev/null``
  and with every repo-redirecting variable (``GIT_DIR``,
  ``GIT_INDEX_FILE``, ``GIT_OBJECT_DIRECTORY``, etc.) stripped so
  ambient developer-machine state cannot steer wrapper behavior.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import GitError, GitTransportError, RefRefused

# Git environment variables the wrapper strips from every child
# process. Three groups:
#
# 1. Every var ``git rev-parse --local-env-vars`` reports. These are
#    the canonical set of vars Git itself treats as repo-local — any
#    of them can redirect, rewrite, or fabricate state. Notably
#    includes ``GIT_GRAFT_FILE``, which can fabricate commit parents
#    (and thus fool ``commit_parents`` / ``is_ancestor``), and
#    ``GIT_REPLACE_REF_BASE`` / ``GIT_NO_REPLACE_OBJECTS``, which can
#    rewrite arbitrary commits via `git replace` refs.
# 2. ``GIT_NAMESPACE`` — not in the local-env-vars list on older
#    git builds but documented as a ref-scoping env var.
# 3. Identity, date, editor, and pager vars. ``commit_tree`` sets the
#    identity vars explicitly per invocation; stripping first ensures
#    no stale ambient value ever survives.
#
# The derived set is the union of these three plus the output of
# ``git rev-parse --local-env-vars`` at import time. We cache the
# derived set — queried once per process.
_GIT_LOCAL_ENV_VARS_STATIC: frozenset[str] = frozenset(
    {
        # From `git rev-parse --local-env-vars` as of git 2.44:
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_OBJECT_DIRECTORY",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_COMMON_DIR",
        # Additional vars not in older local-env-vars output but with
        # equivalent authority:
        "GIT_NAMESPACE",
        # Identity / date / editor / pager — commit_tree sets identity
        # explicitly; stripping first removes any ambient override.
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_DATE",
        "GIT_EDITOR",
        "GIT_PAGER",
    }
)


_GIT_ENV_VARS_TO_STRIP_CACHE: frozenset[str] | None = None


def _git_env_vars_to_strip() -> frozenset[str]:
    """Return the union of static + locally-queried git-local vars.

    Calls ``git rev-parse --local-env-vars`` once and caches the
    result. Falls back to the static list if the subprocess fails
    (e.g. git not installed, PATH issue). Either way, the returned
    set is a strict superset of the static list so callers are never
    worse off than without the dynamic query.
    """
    global _GIT_ENV_VARS_TO_STRIP_CACHE
    if _GIT_ENV_VARS_TO_STRIP_CACHE is not None:
        return _GIT_ENV_VARS_TO_STRIP_CACHE
    dynamic: set[str] = set()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--local-env-vars"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            dynamic = {tok.strip() for tok in result.stdout.split() if tok.strip()}
    except (OSError, FileNotFoundError):
        pass
    _GIT_ENV_VARS_TO_STRIP_CACHE = frozenset(_GIT_LOCAL_ENV_VARS_STATIC | dynamic)
    return _GIT_ENV_VARS_TO_STRIP_CACHE


# Back-compat alias: existing tests reference this name.
_GIT_ENV_VARS_TO_STRIP = _GIT_LOCAL_ENV_VARS_STATIC


@dataclass(frozen=True)
class TreeEntry:
    """One entry in a git tree object.

    ``mode`` is the six-character git mode string (``100644``,
    ``100755``, ``120000``, ``040000``, ``160000``). ``type`` is
    ``"blob"``, ``"tree"``, or ``"commit"``. ``sha`` is the referenced
    object's hex SHA. ``path`` is the entry name relative to the tree
    (or the full path when ``ls_tree`` is called recursively).
    """

    mode: str
    type: str
    sha: str
    path: str


@dataclass(frozen=True)
class WorktreeInfo:
    """One entry from ``git worktree list --porcelain``."""

    path: Path
    head: str | None
    branch: str | None


@dataclass(frozen=True)
class Identity:
    """An author/committer identity stamped on a git commit."""

    name: str
    email: str


class GitRepo:
    """A git repository on the local filesystem."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        self._cached_zero_oid: str | None = None

    # --- factories --------------------------------------------------------

    @classmethod
    def init_bare(cls, path: Path | str) -> GitRepo:
        """Initialize a new bare repository at ``path`` and return a repo.

        Runs under a sanitized environment (``GIT_CONFIG_NOSYSTEM=1``,
        ``GIT_CONFIG_GLOBAL=/dev/null``, ``--template=``) so the repo's
        initial state is deterministic regardless of the user's
        ambient git config or template dir.
        """
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        _run_git(
            ["init", "--bare", "--initial-branch=main", "--template=", str(target)],
            cwd=target.parent,
        )
        return cls(target)

    @classmethod
    def init(cls, path: Path | str) -> GitRepo:
        """Initialize a new non-bare repository at ``path`` and return a repo.

        Sanitized per :meth:`init_bare`.
        """
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        _run_git(
            ["init", "--initial-branch=main", "--template=", str(target)],
            cwd=target.parent,
        )
        return cls(target)

    # --- introspection ----------------------------------------------------

    def is_bare(self) -> bool:
        """Return whether the repository is bare."""
        result = self._run(["rev-parse", "--is-bare-repository"])
        return result.stdout.strip() == "true"

    def rev_parse(self, ref_or_sha: str) -> str:
        """Resolve a ref or SHA prefix to a full 40-character SHA."""
        result = self._run(["rev-parse", "--verify", f"{ref_or_sha}^{{commit}}"])
        return result.stdout.strip()

    def commit_exists(self, sha: str) -> bool:
        """Return whether ``sha`` names a commit object in the repository."""
        result = self._run(["cat-file", "-t", sha], check=False)
        return result.returncode == 0 and result.stdout.strip() == "commit"

    def ref_exists(self, refname: str) -> bool:
        """Return whether a ref exists (e.g. ``refs/heads/trial/t1-slug``)."""
        result = self._run(["show-ref", "--verify", "--quiet", refname], check=False)
        return result.returncode == 0

    def resolve_ref(self, refname: str) -> str | None:
        """Return the SHA a ref points to, or ``None`` if the ref is absent."""
        result = self._run(["show-ref", "--verify", refname], check=False)
        if result.returncode != 0:
            return None
        line = result.stdout.strip().split()
        return line[0] if line else None

    def list_refs(self, pattern: str) -> list[tuple[str, str]]:
        """Return ``(refname, sha)`` pairs matching a ``for-each-ref`` pattern."""
        result = self._run(
            ["for-each-ref", "--format=%(refname) %(objectname)", pattern],
        )
        pairs: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            name, _, sha = line.partition(" ")
            pairs.append((name, sha))
        return pairs

    def is_ancestor(self, maybe_ancestor: str, descendant: str) -> bool:
        """Return whether ``maybe_ancestor`` is an ancestor of ``descendant``.

        Wraps ``git merge-base --is-ancestor``. A commit is its own
        ancestor (git's convention).
        """
        result = self._run(
            ["merge-base", "--is-ancestor", maybe_ancestor, descendant],
            check=False,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise GitError(result.args, result.returncode, result.stdout, result.stderr)

    def commit_tree_sha(self, commit_sha: str) -> str:
        """Return the tree SHA recorded on a commit."""
        return self._run(["rev-parse", "--verify", f"{commit_sha}^{{tree}}"]).stdout.strip()

    def commit_parents(self, commit_sha: str) -> list[str]:
        """Return the list of parent SHAs of a commit, in order."""
        result = self._run(["rev-list", "--parents", "-n", "1", commit_sha])
        tokens = result.stdout.strip().split()
        return tokens[1:]  # first is the commit itself

    def commit_message(self, commit_sha: str) -> str:
        """Return the full commit message of a commit."""
        return self._run(["log", "-1", "--format=%B", commit_sha]).stdout

    def commit_message_subject(self, commit_sha: str) -> str:
        """Return the subject line of a commit."""
        return self._run(["log", "-1", "--format=%s", commit_sha]).stdout.strip()

    def ls_tree(
        self, tree_ish: str, path: str = "", *, recursive: bool = False
    ) -> list[TreeEntry]:
        """List entries of a tree (or commit's tree).

        With ``recursive=True`` the listing flattens sub-trees to blob
        entries and returns full paths. Tree entries are omitted from
        the recursive listing (matches ``git ls-tree -r`` behavior).
        """
        args = ["ls-tree"]
        if recursive:
            args.append("-r")
        args.append(tree_ish)
        if path:
            args.append(path)
        result = self._run(args)
        entries: list[TreeEntry] = []
        for line in result.stdout.splitlines():
            # Format: "<mode> <type> <sha>\t<path>"
            if "\t" not in line:
                continue
            head, _, entry_path = line.partition("\t")
            mode, kind, sha = head.split()
            entries.append(TreeEntry(mode=mode, type=kind, sha=sha, path=entry_path))
        return entries

    def tree_entry_exists(self, tree_ish: str, path: str) -> bool:
        """Return whether ``path`` exists anywhere in the tree."""
        result = self._run(["ls-tree", "-r", tree_ish, "--", path])
        return bool(result.stdout.strip())

    def read_blob(self, blob_sha: str) -> bytes:
        """Return the raw byte content of a blob."""
        result = subprocess.run(
            self._git_argv(["cat-file", "blob", blob_sha]),
            cwd=self.path,
            capture_output=True,
            check=False,
            env=self._env(),
        )
        if result.returncode != 0:
            raise GitError(
                list(result.args),
                result.returncode,
                result.stdout.decode("utf-8", errors="replace"),
                result.stderr.decode("utf-8", errors="replace"),
            )
        return result.stdout

    # --- plumbing ---------------------------------------------------------

    def write_blob(self, content: bytes) -> str:
        """Write a blob to the object database and return its SHA."""
        result = subprocess.run(
            self._git_argv(["hash-object", "-w", "--stdin"]),
            cwd=self.path,
            input=content,
            capture_output=True,
            check=False,
            env=self._env(),
        )
        if result.returncode != 0:
            raise GitError(
                list(result.args),
                result.returncode,
                result.stdout.decode("utf-8", errors="replace"),
                result.stderr.decode("utf-8", errors="replace"),
            )
        return result.stdout.decode().strip()

    def empty_tree_sha(self) -> str:
        """Return the SHA of the empty tree for the repo's hash algorithm.

        Resolved dynamically (via ``git mktree`` on empty input) rather
        than hard-coded, so a repository configured for SHA-256 still
        works. SHA-1's empty tree is
        ``4b825dc642cb6eb9a060e54bf8d69288fbee4904``; SHA-256 differs.
        """
        result = subprocess.run(
            self._git_argv(["mktree"]),
            cwd=self.path,
            input="",
            capture_output=True,
            text=True,
            check=False,
            env=self._env(),
        )
        if result.returncode != 0:
            raise GitError(list(result.args), result.returncode, result.stdout, result.stderr)
        return result.stdout.strip()

    def write_tree_from_entries(self, entries: Iterable[TreeEntry]) -> str:
        r"""Create a tree containing exactly the given entries; return its SHA.

        Uses ``git mktree`` which expects ``<mode> <type> <sha>\t<path>``
        lines on stdin. ``entries`` is iterated once.
        """
        lines = "".join(f"{e.mode} {e.type} {e.sha}\t{e.path}\n" for e in entries)
        result = subprocess.run(
            self._git_argv(["mktree"]),
            cwd=self.path,
            input=lines,
            capture_output=True,
            text=True,
            check=False,
            env=self._env(),
        )
        if result.returncode != 0:
            raise GitError(list(result.args), result.returncode, result.stdout, result.stderr)
        return result.stdout.strip()

    def write_tree_with_file(
        self,
        base_tree_sha: str,
        file_path: str,
        blob_sha: str,
        *,
        mode: str = "100644",
    ) -> str:
        """Return the SHA of a new tree = ``base_tree_sha`` plus one file added.

        Uses an isolated index file (``GIT_INDEX_FILE``) so the
        operation never touches the repo's main index or any worktree.
        Raises :class:`GitError` if ``file_path`` already exists in
        the base tree — the §3.2 squash rule requires the eval-manifest
        path to be the only addition, and a pre-existing file at that
        path would mean a silent overwrite.
        """
        if self.tree_entry_exists(base_tree_sha, file_path):
            raise GitError(
                ["write_tree_with_file", base_tree_sha, file_path, blob_sha],
                1,
                "",
                f"path {file_path!r} already exists in tree {base_tree_sha}",
            )
        with tempfile.TemporaryDirectory(prefix="eden-git-index-") as tmp:
            index_path = str(Path(tmp) / "index")
            env = self._env()
            env["GIT_INDEX_FILE"] = index_path
            # Seed the index from the base tree.
            self._run_with_env(["read-tree", base_tree_sha], env=env)
            # Add the one new entry.
            self._run_with_env(
                [
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"{mode},{blob_sha},{file_path}",
                ],
                env=env,
            )
            result = self._run_with_env(["write-tree"], env=env)
        return result.stdout.strip()

    def commit_tree(
        self,
        tree_sha: str,
        *,
        parents: Iterable[str],
        message: str,
        author: Identity,
        committer: Identity | None = None,
        author_date: str | None = None,
        committer_date: str | None = None,
    ) -> str:
        """Create a commit object pointing at ``tree_sha``.

        Returns the new commit's SHA. The commit is not attached to any
        ref — callers use :meth:`create_ref` / :meth:`update_ref` to
        publish it. ``parents`` may be empty (root commit), one (single-
        parent), or many (merge). ``committer`` defaults to ``author``.
        """
        args = ["commit-tree", tree_sha]
        for parent in parents:
            args.extend(["-p", parent])
        args.extend(["-m", message])
        committer = committer or author
        env = self._env()
        env["GIT_AUTHOR_NAME"] = author.name
        env["GIT_AUTHOR_EMAIL"] = author.email
        env["GIT_COMMITTER_NAME"] = committer.name
        env["GIT_COMMITTER_EMAIL"] = committer.email
        if author_date is not None:
            env["GIT_AUTHOR_DATE"] = author_date
        if committer_date is not None:
            env["GIT_COMMITTER_DATE"] = committer_date
        result = self._run_with_env(args, env=env)
        return result.stdout.strip()

    def zero_oid(self) -> str:
        """Return the all-zero OID of the repo's hash algorithm.

        SHA-1 repositories use 40 hex zeros; SHA-256 use 64. The value
        is cached per instance. `git update-ref <ref> <new> <zero>` uses
        this to express CAS-against-absent.
        """
        if self._cached_zero_oid is None:
            fmt = self._run(["rev-parse", "--show-object-format"]).stdout.strip()
            length = 64 if fmt == "sha256" else 40
            self._cached_zero_oid = "0" * length
        return self._cached_zero_oid

    def create_ref(self, refname: str, new_sha: str) -> None:
        """Create a ref atomically; raise if it already exists.

        Uses ``update-ref <ref> <new> <zero>`` which requires the ref
        to currently point at the zero OID (i.e. be absent). This is
        the primitive the integrator uses when publishing a `trial/*`
        branch — the §1.2 invariant forbids overwriting an existing
        `trial/*` ref.
        """
        self._run(["update-ref", refname, new_sha, self.zero_oid()])

    def update_ref(
        self, refname: str, new_sha: str, *, expected_old_sha: str | None = None
    ) -> None:
        """Compare-and-swap a ref to a new SHA.

        If ``expected_old_sha`` is None, overwrites unconditionally. If
        provided, ``git update-ref`` verifies the ref currently points
        at ``expected_old_sha`` and raises :class:`GitError` otherwise.
        """
        args = ["update-ref", refname, new_sha]
        if expected_old_sha is not None:
            args.append(expected_old_sha)
        self._run(args)

    def delete_ref(self, refname: str, *, expected_old_sha: str | None = None) -> None:
        """Delete a ref, optionally guarded by ``expected_old_sha``."""
        args = ["update-ref", "-d", refname]
        if expected_old_sha is not None:
            args.append(expected_old_sha)
        self._run(args)

    # --- branches & worktrees --------------------------------------------

    def create_branch(self, branch_name: str, start_point: str) -> None:
        """Create a new branch at ``start_point``. Raises if it exists."""
        self._run(["branch", branch_name, start_point])

    def delete_branch(self, branch_name: str) -> None:
        """Delete a local branch (force)."""
        self._run(["branch", "-D", branch_name])

    def current_branch(self) -> str | None:
        """Return the current branch name, or ``None`` if detached."""
        result = self._run(["symbolic-ref", "--short", "-q", "HEAD"], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def add_worktree(
        self,
        worktree_path: Path | str,
        *,
        start_point: str,
        branch: str | None = None,
        detach: bool = False,
    ) -> Path:
        """Add a worktree at ``worktree_path`` starting from ``start_point``.

        Exactly one of ``branch`` or ``detach`` must be supplied. When
        ``branch`` is given, a new branch is created at ``start_point``
        and the worktree checks it out. When ``detach`` is True, the
        worktree is a detached checkout of ``start_point``.
        """
        if (branch is None) == (not detach):
            raise ValueError("specify exactly one of `branch` or `detach=True`")
        target = Path(worktree_path).resolve()
        args = ["worktree", "add"]
        if detach:
            args.extend(["--detach", str(target), start_point])
        else:
            assert branch is not None
            args.extend(["-b", branch, str(target), start_point])
        self._run(args)
        return target

    def remove_worktree(self, worktree_path: Path | str, *, force: bool = False) -> None:
        """Remove a registered worktree."""
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(Path(worktree_path)))
        self._run(args)

    def prune_worktrees(self) -> None:
        """Run ``git worktree prune`` to clear stale metadata."""
        self._run(["worktree", "prune"])

    def list_worktrees(self) -> list[WorktreeInfo]:
        """Parse ``git worktree list --porcelain`` into structured records."""
        result = self._run(["worktree", "list", "--porcelain"])
        records: list[WorktreeInfo] = []
        current: dict[str, str] = {}
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if not line:
                if current:
                    records.append(_worktree_record(current))
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current:
            records.append(_worktree_record(current))
        return records

    # --- remote operations -----------------------------------------------

    @classmethod
    def clone_from(
        cls,
        *,
        url: str,
        dest: Path | str,
        bare: bool = False,
        credential_helper: str | None = None,
    ) -> GitRepo:
        """Clone ``url`` to ``dest``; return a ``GitRepo`` over the result.

        Used at worker-host startup to materialize a private clone of
        the Gitea-hosted repo per Phase 10d follow-up B §D.5.

        ``credential_helper``, when set, is passed via
        ``-c credential.helper=<value>`` for the clone itself AND
        persisted as repo-local config so subsequent fetch/push from
        the cloned repo pick it up automatically without re-passing.

        Network failures (Gitea unreachable, DNS failure) raise
        :class:`GitTransportError`; other failures (target dir not
        empty, ref-format problems) raise :class:`GitError`.
        """
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        argv: list[str] = ["git"]
        if credential_helper is not None:
            argv += ["-c", f"credential.helper={credential_helper}"]
        argv.append("clone")
        if bare:
            argv.append("--bare")
        argv += [url, str(target)]
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            env=_sanitized_git_env(),
        )
        if result.returncode != 0:
            _raise_remote_error(argv, result)
        repo = cls(target)
        if credential_helper is not None:
            repo._run(["config", "credential.helper", credential_helper])
        return repo

    def push_ref(
        self,
        ref: str,
        *,
        expected_old_sha: str | None = None,
        remote: str = "origin",
    ) -> None:
        """Push ``ref`` to ``remote`` with optional CAS via force-with-lease.

        ``expected_old_sha`` becomes ``--force-with-lease=<ref>:<sha>``
        — the daemon rejects with a non-fast-forward error if the
        remote has diverged. ``None`` does a regular push (rejected by
        default if the remote has a non-fast-forward).

        Raises :class:`RefRefused` on definite remote rejection (CAS
        miss / non-fast-forward / ref hook reject), or
        :class:`GitTransportError` on transport-layer failure
        (ambiguous: the remote may or may not have applied the ref).
        Callers writing ``trial/*`` per chapter 6 §3.4 MUST disambiguate
        the latter via ``ls_remote``.
        """
        argv = ["push"]
        if expected_old_sha is not None:
            argv.append(f"--force-with-lease={ref}:{expected_old_sha}")
        argv += [remote, f"{ref}:{ref}"]
        result = self._run(argv, check=False)
        if result.returncode != 0:
            _raise_remote_error(self._git_argv(argv), result)

    def fetch_ref(
        self, ref: str, *, remote: str = "origin"
    ) -> str | None:
        """Fetch ``ref`` from ``remote`` and return the fetched SHA.

        Equivalent to ``git fetch <remote> +<ref>:<ref>`` — overwrites
        the local ref with the remote's view.

        Returns the SHA the local ref points at after the fetch, or
        ``None`` if the remote does not have ``ref``. Raises
        :class:`GitTransportError` on transport failure.
        """
        argv = ["fetch", remote, f"+{ref}:{ref}"]
        result = self._run(argv, check=False)
        if result.returncode != 0:
            # `fetch` returns non-zero if the remote ref is missing;
            # disambiguate transport from no-such-ref via stderr
            # pattern, since neither has a clean exit code.
            if _stderr_indicates_no_such_ref(result.stderr):
                return None
            _raise_remote_error(self._git_argv(argv), result)
        return self.resolve_ref(ref)

    def fetch_all_heads(self, *, remote: str = "origin") -> None:
        """Fetch every remote head into the local namespace.

        Equivalent to
        ``git fetch --prune <remote> '+refs/heads/*:refs/heads/*'``.
        Used by worker-host startup to refresh the entire branch
        namespace AND prune local heads that no longer exist on the
        remote (orphan-cleanup as a side effect of fetch). Bare-repo-
        safe; used on every worker host's clone per §D.7c.
        """
        argv = [
            "fetch",
            "--prune",
            remote,
            "+refs/heads/*:refs/heads/*",
        ]
        result = self._run(argv, check=False)
        if result.returncode != 0:
            _raise_remote_error(self._git_argv(argv), result)

    def delete_remote_ref(
        self,
        ref: str,
        *,
        expected_sha: str | None = None,
        remote: str = "origin",
    ) -> None:
        """Delete ``ref`` from ``remote``.

        With ``expected_sha`` set, the delete is CAS-guarded via
        ``--force-with-lease=<ref>:<sha>`` so an integrator's
        compensating-delete never racially deletes a ref another
        integrator just published with a different SHA.

        Raises :class:`RefRefused` on CAS miss or if the remote does
        not have the ref (the latter is a "no-op vs. won the race"
        ambiguity; chapter 6 §3.4 callers handle it by reading
        ``ls_remote`` first).
        """
        argv = ["push"]
        if expected_sha is not None:
            argv.append(f"--force-with-lease={ref}:{expected_sha}")
        argv += [remote, f":{ref}"]
        result = self._run(argv, check=False)
        if result.returncode != 0:
            _raise_remote_error(self._git_argv(argv), result)

    def ls_remote(
        self, pattern: str = "refs/heads/*", *, remote: str = "origin"
    ) -> list[tuple[str, str]]:
        """Return ``[(refname, sha), ...]`` for refs on ``remote``.

        Patterns follow ``git ls-remote`` semantics. Used by
        ``Integrator`` step-2 transport-indeterminate read-back and
        the §D.7c remote-orphan reconciliation pass.

        Raises :class:`GitTransportError` on transport failure (which
        the integrator's callers handle by deferring to the startup
        sweep).
        """
        argv = ["ls-remote", remote, pattern]
        result = self._run(argv, check=False)
        if result.returncode != 0:
            _raise_remote_error(self._git_argv(argv), result)
        out: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            sha, _, name = line.partition("\t")
            sha = sha.strip()
            name = name.strip()
            if sha and name:
                out.append((name, sha))
        return out

    # --- internals --------------------------------------------------------

    def _git_argv(self, args: list[str]) -> list[str]:
        # `commit.gpgsign=false` pins signing off so an ambient
        # `commit.gpgsign=true` in the user's global config doesn't make
        # `commit-tree` fail for lack of a signing key in integrator
        # runs. Identity is supplied explicitly via env vars in
        # `commit_tree`, so user.name / user.email are never consulted.
        return [
            "git",
            "-c",
            f"safe.directory={self.path}",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(self.path),
            *args,
        ]

    def _env(self) -> dict[str, str]:
        return _sanitized_git_env()

    def _run(
        self, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        argv = self._git_argv(args)
        result = subprocess.run(
            argv,
            cwd=self.path,
            capture_output=True,
            text=True,
            check=False,
            env=self._env(),
        )
        if check and result.returncode != 0:
            raise GitError(argv, result.returncode, result.stdout, result.stderr)
        return result

    def _run_with_env(
        self, args: list[str], *, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        argv = self._git_argv(args)
        result = subprocess.run(
            argv,
            cwd=self.path,
            capture_output=True,
            text=True,
            check=False,
            env=dict(env),
        )
        if result.returncode != 0:
            raise GitError(argv, result.returncode, result.stdout, result.stderr)
        return result


def _sanitized_git_env() -> dict[str, str]:
    """Build a child environment stripped of repo-redirecting git vars.

    Also pins ``GIT_CONFIG_NOSYSTEM=1`` and
    ``GIT_CONFIG_GLOBAL=/dev/null`` so the host's system- and user-
    level git config never leaks into wrapper operations. Integrator
    runs must be deterministic regardless of developer-machine state.
    """
    strip = _git_env_vars_to_strip()
    env = {k: v for k, v in os.environ.items() if k not in strip}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    # Remove GIT_CONFIG_KEY_* / GIT_CONFIG_VALUE_* injections — there
    # can be arbitrarily many of these, paired with GIT_CONFIG_COUNT
    # (already stripped above, which makes any remaining KEY/VALUE
    # entries inert, but strip them too for hygiene).
    for k in list(env):
        if k.startswith("GIT_CONFIG_KEY_") or k.startswith("GIT_CONFIG_VALUE_"):
            del env[k]
    return env


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run git outside of any GitRepo scope (for ``init`` / ``init --bare``)."""
    argv = ["git", *args]
    result = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=_sanitized_git_env(),
    )
    if result.returncode != 0:
        raise GitError(argv, result.returncode, result.stdout, result.stderr)
    return result


# Heuristic stderr patterns. Git does not emit structured error
# output, so distinguishing transport-layer failure from
# remote-rejection has to grep stderr. The patterns below are the
# stable substrings git has emitted for years across versions
# 2.20+; tested locally against git 2.44 + 2.34 (Debian 12 / Ubuntu
# 24.04). When git's error vocabulary changes, these tighten on
# the new wording; see test_remote_ops.py for the
# round-trip checks.

_TRANSPORT_STDERR_MARKERS: tuple[str, ...] = (
    "Could not resolve host",
    "Connection refused",
    "Couldn't connect to server",
    "Could not connect to",
    "Failed to connect",
    "unable to access",
    "fatal: unable to access",
    "early EOF",
    "the remote end hung up",
    "RPC failed",
    "fatal: protocol",
    "operation timed out",
    "Operation timed out",
    "Network is unreachable",
)

_REJECTION_STDERR_MARKERS: tuple[str, ...] = (
    "[rejected]",
    "stale info",
    "non-fast-forward",
    "remote rejected",
    "would clobber existing tag",
    "fetch first",
    "deny updating a hidden ref",
    "pre-receive hook declined",
)


def _is_transport_stderr(stderr: str) -> bool:
    return any(marker in stderr for marker in _TRANSPORT_STDERR_MARKERS)


def _is_rejection_stderr(stderr: str) -> bool:
    return any(marker in stderr for marker in _REJECTION_STDERR_MARKERS)


def _stderr_indicates_no_such_ref(stderr: str) -> bool:
    return (
        "couldn't find remote ref" in stderr
        or "Couldn't find remote ref" in stderr
        or "fatal: refspec" in stderr
    )


def _raise_remote_error(
    argv: list[str], result: subprocess.CompletedProcess[str]
) -> None:
    """Map a failed remote-op subprocess result to the right exception class.

    Transport-layer failures raise :class:`GitTransportError`; remote
    rejections raise :class:`RefRefused`; everything else raises the
    generic :class:`GitError`.
    """
    rc = result.returncode
    out = result.stdout
    err = result.stderr
    if _is_transport_stderr(err):
        raise GitTransportError(argv, rc, out, err)
    if _is_rejection_stderr(err):
        raise RefRefused(argv, rc, out, err)
    raise GitError(argv, rc, out, err)


def _worktree_record(entry: dict[str, str]) -> WorktreeInfo:
    path = Path(entry["worktree"])
    head = entry.get("HEAD")
    branch_ref = entry.get("branch")
    branch = None
    if branch_ref and branch_ref.startswith("refs/heads/"):
        branch = branch_ref[len("refs/heads/") :]
    return WorktreeInfo(path=path, head=head, branch=branch)
