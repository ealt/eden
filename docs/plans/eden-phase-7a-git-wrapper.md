# Phase 7a — `eden-git` git subprocess wrapper

## Goal

Ship the git subprocess wrapper that the Phase 7b integrator composes
into §3.2 squash commits. Phase 7a is plumbing only; no integrator
policy decisions happen here.

See [`docs/roadmap.md`](../roadmap.md) §Phase 7 for the full phase
context, and [`spec/v0/06-integrator.md`](../../spec/v0/06-integrator.md)
for the normative contract Phase 7b will implement against.

## Scope

### Files to create

| File | Purpose |
|---|---|
| `reference/packages/eden-git/pyproject.toml` | Workspace member metadata |
| `reference/packages/eden-git/README.md` | Package surface + roadmap |
| `reference/packages/eden-git/src/eden_git/__init__.py` | Public re-exports |
| `reference/packages/eden-git/src/eden_git/errors.py` | `GitError` carrying argv + streams |
| `reference/packages/eden-git/src/eden_git/repo.py` | `GitRepo` + `Identity` + `TreeEntry` + `WorktreeInfo` |
| `reference/packages/eden-git/tests/conftest.py` | Shared fixtures (bare / non-bare repos with seed commit) |
| `reference/packages/eden-git/tests/test_repo_plumbing.py` | Blob / tree / commit / ref plumbing tests |
| `reference/packages/eden-git/tests/test_repo_branches_worktrees.py` | Branch + worktree management tests |

### Files to modify

| File | Change |
|---|---|
| `pyproject.toml` | Add `eden-git` to `[tool.uv.workspace]`, `[tool.uv.sources]`, `[dependency-groups.dev]`, `[tool.pyright].include`, `[tool.pytest.ini_options].testpaths` |
| `CLAUDE.md` (and symlink `AGENTS.md`) | Phase-7a completion note + `uv sync` mentions `eden-git` |
| `docs/roadmap.md` | Mark 7a complete |

## `GitRepo` surface

Instance methods are grouped by concern. The package re-exports
`GitError`, `GitRepo`, `Identity`, `TreeEntry`, `WorktreeInfo`.

**Factories** (classmethods): `init_bare(path)`, `init(path)`.

**Introspection**: `is_bare`, `rev_parse(ref)`, `commit_exists(sha)`,
`ref_exists(refname)`, `resolve_ref(refname)`, `list_refs(pattern)`,
`is_ancestor(a, b)`, `commit_tree_sha(sha)`, `commit_parents(sha)`,
`commit_message(sha)`, `commit_message_subject(sha)`,
`ls_tree(tree_ish, path, recursive=False)`, `tree_entry_exists`,
`read_blob(sha)`.

**Plumbing** (object-DB writes): `empty_tree_sha`,
`write_tree_from_entries(entries)`, `write_tree_with_file(base, path,
blob_sha, mode="100644")`, `write_blob(content)`,
`commit_tree(tree_sha, parents, message, author, committer=None,
author_date=None, committer_date=None)`, `create_ref(name, sha)`,
`update_ref(name, new, *, expected_old_sha=None)`,
`delete_ref(name, *, expected_old_sha=None)`.

**Branch / worktree**: `create_branch`, `delete_branch`,
`current_branch`, `add_worktree(path, *, start_point, branch=None,
detach=False)`, `remove_worktree(path, *, force=False)`,
`prune_worktrees`, `list_worktrees`.

## Key design decisions

1. **Explicit identity, not git config.** `commit_tree` takes an
   `Identity` and injects `GIT_AUTHOR_*` / `GIT_COMMITTER_*` env vars.
   The user's global `user.name` / `user.email` is never consulted.
2. **`commit.gpgsign=false` pinned per invocation.** An ambient
   `commit.gpgsign=true` in user config would otherwise make
   `commit-tree` fail for lack of a signing key in headless
   integrator runs.
3. **Isolated index for tree shaping.** `write_tree_with_file` uses
   `GIT_INDEX_FILE` pointing at a tempdir so the operation never
   touches the repo's main index or any worktree.
4. **Pre-existing-path rejection.** `write_tree_with_file` raises
   `GitError` if the file path already exists in the base tree —
   §3.2 requires the eval-manifest path to be the *only* addition in
   the squash.
5. **Atomic ref creation.** `create_ref` calls `update-ref refname
   new_sha 0000...0`, which git treats as CAS-against-absent. The
   §1.2 `trial/*` immutability invariant requires rejecting overwrites.
6. **CAS on updates.** `update_ref` optionally takes
   `expected_old_sha` so callers can detect concurrent writes.
7. **Typed errors.** `GitError` carries the full argv, exit code,
   stdout, and stderr so callers can branch on failure modes without
   re-parsing git's English output.
8. **Hash-algo note.** The zero-OID is hard-coded as 40 zeros (SHA-1
   repos). SHA-256 repos would need 64 zeros; this is flagged in the
   package README as a known limitation but not fixed in 7a since
   SHA-1 remains the default across local + Gitea + GitHub.

## What's out of scope

- **The integrator flow itself.** Observing `trial.succeeded`,
  shaping the canonical commit, atomically coordinating with the
  `Store` — all Phase 7b.
- **Any §3.2 squash assembly.** 7a gives 7b the primitives; 7a does
  not compose them.
- **Gitea / GitHub transport.** 7a operates on a local filesystem
  repo only.

## Tests

- 36 tests in two files, all against a real `git` subprocess on a
  `tmp_path` repo. No mocks.
- Fixtures: `bare_repo` (empty bare repo) and `repo_with_main`
  (bare repo with a seed commit on main). A `non_bare_repo_with_main`
  fixture mirrors the seeded state for worktree tests.
- Coverage goal: every public method exercised at least once, with
  negative cases for refs that reject overwrite (§1.2) and trees that
  reject an existing eval-manifest path (§3.2).

## Verification

- `uv run pytest -q` — 282 tests total across the workspace (36 new in
  `eden-git/tests/`), all green.
- `uv run ruff check .` — clean.
- `uv run pyright` — 0 errors, 0 warnings.
- `npx markdownlint-cli2@0.14.0 ...` — clean.
