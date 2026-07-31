# codex-review: attempted, environment-blocked

**No review was performed.** Round 0 aborted before Codex read a single file, and
the operator elected to ship without it (the change carries full local gates plus
green PR CI). This record exists so the absence of a review is explicit rather
than inferred, and so the next session that reaches for `/codex-review` on this
pod does not spend an hour re-deriving the cause.

The review brief in [`0.md`](0.md) is complete and accurate; a retry needs only a
working Codex sandbox.

## The failure

Every Codex command — including a read-only `pwd` — failed *before execution*
with:

```text
bwrap: Failed to make / slave: Permission denied
```

Codex therefore produced no review; [`0-review.md`](0-review.md) contains only
its own report of being blocked.

Bare `bubblewrap`, no Codex involved, with the harness's own sandbox explicitly
disabled:

```text
$ bwrap --dev-bind / / --unshare-all true
bwrap: Failed to make / slave: Permission denied            (exit 1)

$ bwrap --dev-bind / / true
bwrap: Creating new namespace failed: Operation not permitted (exit 1)
```

## What it is not

| Hypothesis | Eliminated by |
|---|---|
| The skill's `codex exec` flags / wrapper | Bare `bwrap` fails identically with no Codex in the picture |
| The harness Bash tool's sandbox | Identical failure with `dangerouslyDisableSandbox: true` |
| A nested namespace in this session's tmux | This shell shares PID 1's exact namespaces (`mnt:[4026534204]`, `user:[4026531837]`) — same as every other `tmux: server` / `bash` on the pod |
| Codex project trust | Same failure with `-C /home/dev/Documents/eden`, which `~/.codex/config.toml` marks `trust_level = "trusted"` |
| A seccomp filter | `/proc/self/status`: `Seccomp: 0`, no filters |

## Why it fails

`CapEff: 00000000a80425fb` is the Docker default capability set — **no
`CAP_SYS_ADMIN`** (bit 21 clear). `mount(NULL, "/", MS_SLAVE|MS_REC)` requires
it, and `EPERM` there is precisely the first error. User namespaces are not
sysctl-disabled (`max_user_namespaces` = 251409), so the namespace is created and
then the propagation change is refused.

## Leading hypothesis: the bubblewrap install is the regression

Codex 0.145.0 resolves `bwrap` by **bare name** — the binary contains the strings
`bwrap` and `codex-bwrap-synthetic-mount-targets` but no absolute path and no
`CODEX_LINUX_SANDBOX_EXE`. `~/.local/bin` precedes `/usr/bin` on `PATH`.

Before bubblewrap 0.9.0 was installed on this pod, Codex warned and used a
**bundled fallback**, and `codex exec --sandbox read-only` / `workspace-write`
both ran shell commands successfully (operator-verified). Installing system
bubblewrap plausibly flipped Codex onto a real `bwrap` that this container's
capability set cannot run — i.e. the install, now in bootstrap, may have broken a
path that worked.

Untested here on purpose: confirming it means shadowing or removing a binary the
operator had just installed, which is theirs to do, not something to work around
silently.

## Fix options, cheapest first

1. **Revert/shadow the bubblewrap install** and re-run a Codex smoke. If the
   bundled fallback works again, this fixes `/codex-review` for every future
   session on this pod, not just one.
2. **Grant `CAP_SYS_ADMIN`** to the pod (securityContext capabilities add, or
   privileged). Makes real `bwrap` work; costs a pod restart.
3. **`--dangerously-bypass-approvals-and-sandbox`.** Declined here, and the
   reasoning is worth preserving: the container isolates the *pod from the
   cluster*, while Codex's sandbox isolates *Codex from the pod's contents* — AWS
   credentials, Claude credentials, GitHub PAT helpers, the eden DSN, and ~14
   repos, all readable by any process under this single uid. The two layers are
   not redundant, and "we're already externally sandboxed" is the wrong reason to
   drop the inner one.

## Suggested skill improvement

`/codex-review` has no preflight for *sandbox functionality* — only for `codex`
being on `PATH`. A two-second check before round 0 would have failed loudly
instead of burning a ~250k-token round that read nothing:

```bash
bwrap --dev-bind / / --unshare-all true 2>&1 || echo "codex sandbox unavailable"
```
