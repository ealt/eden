"""Per-experiment integrator-repo materialization for the executor module.

Issue #145 §3.5 / Decision 9. The executor module needs a local bare
clone of each experiment's integrator repo to render ``work/*`` refs and
produce diffs. Today that is one clone bound at ``--repo-path``; in the
multi-experiment world each experiment has its own integrator repo.

:class:`RepoMaterializer` vends per-experiment :class:`~eden_git.GitRepo`
clones under ``<repo-root>/<experiment_id>.git``, cloning-if-missing from
the Forgejo remote (with the per-experiment URL substituted from the
configured ``--forgejo-url`` org base) and fetching on each access. It is
consulted only for NON-default experiments; the deployment-default
experiment continues to use the startup-materialized ``app.state.repo``
so single-experiment deployments are unchanged (see
:func:`eden_web_ui.routes._helpers.repo_for`).
"""

from __future__ import annotations

from pathlib import Path

from eden_git import GitRepo


def forgejo_url_for(forgejo_url: str, experiment_id: str) -> str:
    """Rewrite a per-experiment Forgejo URL for ``experiment_id``.

    ``--forgejo-url`` is ``http(s)://<host>/<org>/<exp>.git`` — a
    per-experiment URL. The org base is everything up to the last path
    segment; the active experiment's clone lives at
    ``<org-base>/<experiment_id>.git``.
    """
    org_base = forgejo_url.rsplit("/", 1)[0]
    return f"{org_base}/{experiment_id}.git"


class RepoMaterializer:
    """Lazily materializes + caches per-experiment bare clones.

    ``repo_root`` is the directory that holds one ``<experiment_id>.git``
    bare clone per experiment. When ``forgejo_url`` is set a missing
    clone is created from the substituted remote URL and refreshed via
    ``fetch_all_heads`` on each access (AGENTS.md "long-lived clones need
    read-before-display fetches"); without it the clone must already
    exist on disk.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        forgejo_url: str | None,
        credential_helper: str | None,
    ) -> None:
        self._repo_root = repo_root
        self._forgejo_url = forgejo_url
        self._credential_helper = credential_helper
        self._cache: dict[str, GitRepo] = {}

    def for_experiment(self, experiment_id: str) -> GitRepo:
        """Return the bare clone for ``experiment_id``, materializing if needed."""
        cached = self._cache.get(experiment_id)
        if cached is not None:
            if self._forgejo_url is not None:
                cached.fetch_all_heads()
            return cached
        dest = self._repo_root / f"{experiment_id}.git"
        if (dest / "HEAD").is_file():
            repo = GitRepo(str(dest))
            if self._forgejo_url is not None:
                repo.fetch_all_heads()
        elif self._forgejo_url is not None:
            repo = GitRepo.clone_from(
                url=forgejo_url_for(self._forgejo_url, experiment_id),
                dest=dest,
                bare=True,
                credential_helper=self._credential_helper,
            )
        else:
            # No remote and no on-disk clone — nothing to materialize.
            raise FileNotFoundError(
                f"no integrator clone for experiment {experiment_id!r} at "
                f"{dest} and no --forgejo-url to clone from"
            )
        repo.rev_parse("HEAD")
        self._cache[experiment_id] = repo
        return repo
