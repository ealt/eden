# codex-review: attempted, environment-blocked

**No review was performed.** Round 0 aborted before Codex read a single file, and
the operator elected to ship without it (the change carries full local gates plus
green PR CI). This record exists so the absence of a review is explicit rather
than inferred, and so the next session that reaches for `/codex-review` on this
pod does not spend an hour re-deriving the cause.

The review brief in [`0.md`](0.md) is complete and accurate; a retry needs only a
working Codex sandbox, which this pod cannot currently give it.

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
| A shadowed / newly-installed `bwrap` | Falsified — see "A hypothesis this record used to carry" below |

## Why it fails

`bwrap` needs `CAP_SYS_ADMIN` for `mount(NULL, "/", MS_SLAVE|MS_REC)`, and this
container's `CapEff: 00000000a80425fb` (the Docker default set) has bit 21 clear,
so that call returns `EPERM` — precisely the first error. User namespaces are not
sysctl-disabled (`max_user_namespaces` = 251409): the namespace is created, then
the propagation change is refused.

**Do not read that as "this pod cannot be sandboxed" — that is too strong.** The
kernel here does offer an unprivileged sandbox: `landlock_create_ruleset` with
`LANDLOCK_CREATE_RULESET_VERSION` returns **ABI v7, errno 0**, and Landlock needs
no `CAP_SYS_ADMIN` at all (operator-measured). The accurate, narrower statement
is:

> *Codex* can no longer reach the sandbox this pod can provide.

`codex features list` shows `use_linux_sandbox_bwrap` as **removed** and
`use_legacy_landlock` as **deprecated** — bwrap is the only supported backend in
0.145.0. The decisive test on the deprecated escape hatch (operator-run):
`codex exec --enable use_legacy_landlock -s workspace-write`, instructed to
`printf SANDBOX_LIVE > proof.txt`, produced **no file** and reported "Unable to
run: the shell sandbox failed before executing the command" (11,467 tokens).

So the resolution is to run the review **off this pod**, for a sharper reason
than the capability set: the config surface that could reach Landlock is
deprecated and non-functional here.

## Execution integrity: a "DONE" is not evidence

Two behaviours were observed from Codex on this pod, and the difference matters
more than either run:

- The `use_legacy_landlock` run **failed honestly** — it said the sandbox failed
  instead of claiming success.
- An earlier observed run burned **15,303 tokens, replied `DONE`, and ran
  nothing.**

Because both behaviours exist, a review's validity here cannot be read off the
model's tone or its closing summary. It has to be checked **mechanically**:
**zero tool-execution events ⇒ the run is void, not passing.** The JSONL stream
is the place to check it (`item.completed` events of type `command_execution`); a
run with none of them read no files, whatever its prose says.

## A hypothesis this record used to carry — falsified, kept as a correction

An earlier revision of this file led with: *Codex resolves `bwrap` by bare name,
`~/.local/bin` precedes `/usr/bin`, so installing bubblewrap 0.9.0 flipped Codex
off a working bundled fallback onto a real `bwrap` this container can't run* —
and recommended **removing the install** as the cheapest fix.

**That premise is false**, and it was the one action the hypothesis told a reader
to take, so it is corrected here rather than deleted:

```text
$ which -a bwrap
/usr/bin/bwrap
/bin/bwrap
$ ls -l ~/.local/bin/bwrap
ls: cannot access '/home/dev/.local/bin/bwrap': No such file or directory
$ stat -c '%i %n' /usr/bin/bwrap /bin/bwrap
7926183 /usr/bin/bwrap
7926183 /bin/bwrap          # same inode; /bin -> /usr/bin
```

There is no `bwrap` in `~/.local/bin`, so nothing was shadowed and PATH order
never mattered. **Removing the bubblewrap install would fix nothing.**

The hypothesis was built from `strings` on the Codex binary (bare `bwrap`, no
absolute path, no `CODEX_LINUX_SANDBOX_EXE`) plus the report that Codex had
previously warned about bubblewrap and used a fallback. The string evidence was
real; the inference that a *new* install displaced something was not tested
before being written down. Given `use_legacy_landlock` is now deprecated, the
likelier explanation for the earlier working runs is a **Codex version change**
that retired the Landlock backend — but that is inference, not measurement, and
should be treated as such.

## Fix options

1. **Run the review off this pod.** The current resolution. Nothing on the pod
   needs changing, and it is the only option that doesn't trade away a sandbox
   layer.
2. **Grant `CAP_SYS_ADMIN`** (securityContext capabilities add, or privileged).
   Makes real `bwrap` work; costs a pod restart, and widens what any process
   under this uid can do — not just Codex.
3. **~~Revert the bubblewrap install.~~** Falsified above; it would change
   nothing.
4. **`--dangerously-bypass-approvals-and-sandbox`.** Declined, and the reasoning
   is worth preserving: the container isolates the *pod from the cluster*, while
   Codex's sandbox isolates *Codex from the pod's contents* — AWS credentials,
   Claude credentials, GitHub PAT helpers, the eden DSN, and ~14 repos, all
   readable by any process under this single uid. The two layers are not
   redundant, and "we're already externally sandboxed" is the wrong reason to
   drop the inner one.

## Suggested skill improvements

`/codex-review` checks that `codex` is on `PATH` but never that its sandbox can
**start**, and never that a completed round actually **ran** anything. Both gaps
cost real tokens here (a ~250k-token round 0 that read nothing; a 15k-token round
that claimed `DONE` and ran nothing). Two cheap additions:

```bash
# Preflight: does Codex's only supported backend work at all?
bwrap --dev-bind / / --unshare-all true 2>&1 || echo "codex sandbox unavailable"
```

```bash
# Postflight: a round with no tool-execution events is void, not passing.
grep -c '"command_execution"' "${RUN_DIR}/${N}.jsonl"
```
