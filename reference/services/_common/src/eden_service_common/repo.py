"""Git repo helper for bootstrapping bare repos in tests.

Phase 8b's real-subprocess E2E seeds the bare repo with a single
empty commit so the ideator's ``--base-commit-sha`` has something
real to point at. Consolidated here so every worker-host test can
reuse the same shape.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from eden_git import GitRepo, Identity, TreeEntry

_SEED_IDENTITY = Identity(name="EDEN Seed", email="seed@eden.invalid")
_SEED_DATE = "2026-04-01T00:00:00+00:00"


def seed_bare_repo(repo_path: str) -> str:
    """Write an empty initial commit on ``refs/heads/main``.

    The repo must already exist (caller runs ``git init --bare``).
    Returns the seed commit's SHA for threading into the ideator's
    ``--base-commit-sha`` flag.
    """
    repo = GitRepo(repo_path)
    empty_blob = repo.write_blob(b"")
    tree = repo.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=empty_blob, path=".gitkeep")]
    )
    seed_sha = repo.commit_tree(
        tree,
        parents=[],
        message="eden: seed\n",
        author=_SEED_IDENTITY,
        committer=_SEED_IDENTITY,
        author_date=_SEED_DATE,
        committer_date=_SEED_DATE,
    )
    repo.create_ref("refs/heads/main", seed_sha)
    return seed_sha


# When the seed runs inside a container against a host bind-mount,
# the host UID and the container UID typically differ, which trips
# git's "dubious ownership" check. ``-c safe.directory=*`` permits
# the read-only operations (rev-parse, ls-files) regardless. Same
# posture as the eden-runtime container's identity wiring.
_SAFE_DIR_FLAGS = ["-c", "safe.directory=*"]


def _is_git_repo(path: Path) -> bool:
    """True if ``path`` is the root of a git working tree."""
    if not (path / ".git").exists():
        return False
    result = subprocess.run(
        ["git", *_SAFE_DIR_FLAGS, "-C", str(path),
         "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _git_listed_files(src: Path) -> list[Path]:
    """Return relative paths of tracked + untracked-but-not-ignored files.

    Mirrors ``git ls-files -z --cached --others --exclude-standard``
    so the snapshot honors the source repo's ``.gitignore``. Submodule
    directories (paths git treats as gitlinks) are returned, but the
    caller skips them — submodule contents would need recursive
    cloning, which we don't support in a snapshot seed.
    """
    result = subprocess.run(
        [
            "git", *_SAFE_DIR_FLAGS, "-C", str(src),
            "ls-files", "-z",
            "--cached", "--others", "--exclude-standard",
        ],
        capture_output=True, check=True,
    )
    if not result.stdout:
        return []
    return [Path(p) for p in result.stdout.decode().split("\0") if p]


def seed_bare_repo_from_dir(repo_path: str, src_dir: str) -> str:
    """Seed ``refs/heads/main`` with a snapshot of ``src_dir``.

    Behavior:

    - If ``src_dir`` is a git working tree, only files git would
      consider (tracked + untracked-but-not-ignored, per
      ``git ls-files --cached --others --exclude-standard``) are
      copied. This honors the source's ``.gitignore`` and avoids
      pulling in build artifacts, virtualenvs, caches, etc.
    - Otherwise, the entire directory's contents are copied verbatim,
      skipping nested ``.git`` metadata.

    Either way, no history is preserved — the seed is a single commit
    on ``main`` with the same fixed identity/date as
    :func:`seed_bare_repo`.

    The bare repo at ``repo_path`` must already exist
    (``git init --bare --initial-branch=main``). Returns the seed
    commit's SHA.
    """
    src = Path(src_dir).resolve()
    if not src.is_dir():
        raise ValueError(f"src_dir is not a directory: {src_dir}")

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": _SEED_IDENTITY.name,
        "GIT_AUTHOR_EMAIL": _SEED_IDENTITY.email,
        "GIT_AUTHOR_DATE": _SEED_DATE,
        "GIT_COMMITTER_NAME": _SEED_IDENTITY.name,
        "GIT_COMMITTER_EMAIL": _SEED_IDENTITY.email,
        "GIT_COMMITTER_DATE": _SEED_DATE,
    }

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "seed-work"
        work.mkdir()
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(work)],
            check=True, capture_output=True,
        )
        if _is_git_repo(src):
            for rel in _git_listed_files(src):
                src_path = src / rel
                # Skip gitlinks (submodules) — we don't recursively
                # snapshot submodules in this MVP.
                if src_path.is_dir():
                    continue
                if not src_path.exists():
                    # File was deleted from the worktree but still
                    # tracked; nothing to copy.
                    continue
                target = work / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, target, follow_symlinks=False)
        else:
            for entry in src.iterdir():
                if entry.name == ".git":
                    continue
                target = work / entry.name
                if entry.is_dir():
                    shutil.copytree(entry, target, symlinks=True)
                else:
                    shutil.copy2(entry, target, follow_symlinks=False)
        subprocess.run(
            [
                "git", "-C", str(work),
                "-c", "commit.gpgsign=false",
                "add", "-A",
            ],
            check=True, capture_output=True, env=env,
        )
        subprocess.run(
            [
                "git", "-C", str(work),
                "-c", "commit.gpgsign=false",
                "commit", "--allow-empty", "-m", "eden: seed",
            ],
            check=True, capture_output=True, env=env,
        )
        subprocess.run(
            ["git", "-C", str(work), "push", repo_path, "main:main"],
            check=True, capture_output=True,
        )

    sha = GitRepo(repo_path).resolve_ref("refs/heads/main")
    if sha is None:
        raise RuntimeError(
            f"seed push to refs/heads/main left no ref in {repo_path!r}"
        )
    return sha


def ensure_repo_clone(
    *,
    log,  # noqa: ANN001 — _CtxAdapter, not exposed
    repo_path: str,
    forgejo_url: str | None,
    credential_helper: str | None,
) -> None:
    """Materialize a worker host's local bare clone per Phase 10d follow-up B §D.5.

    No-op when ``forgejo_url`` is None (chunk-10d behavior — the operator
    pre-populates ``repo_path`` via setup-experiment). Otherwise:
    clone bare at first run, ``fetch_all_heads`` on subsequent starts
    so the local clone reflects the remote.

    Shared by the executor and evaluator hosts (12c factoring — both
    cloned the same shape verbatim in `cli.py`).
    """
    if forgejo_url is None:
        return
    path = Path(repo_path)
    if (path / "HEAD").is_file():
        log.info("fetching_remote_heads", url=forgejo_url)
        GitRepo(path).fetch_all_heads()
        return
    log.info("cloning_from_remote", url=forgejo_url, dest=str(path))
    GitRepo.clone_from(
        url=forgejo_url,
        dest=path,
        bare=True,
        credential_helper=credential_helper,
    )
