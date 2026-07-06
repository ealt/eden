# Long-running experiments need session-independent substrate

An EDEN experiment that is meant to run for hours-to-weeks MUST NOT depend on
any process, tunnel, or network path whose lifetime is tied to a user's
interactive session. A stack on a laptop is a **validation harness**; it is not
an experiment substrate.

This is the liveness complement to
[experiment data durability](experiment-data-durability.md): that page
guarantees the *state* survives restarts (spec
[`01-concepts.md`](../../spec/v0/01-concepts.md) §13's aggregate invariant);
this page is about keeping the *processes* alive long enough to make progress
in the first place.

## The rule

Before starting a run intended to outlive the working session, walk every
component and network path the experiment depends on and ask: **"what happens
to this when the operator's machine sleeps, their SSH session drops, or their
laptop leaves the network?"** If the answer for any component is "it stops" or
"it hangs", the run is session-bound and will die at the first lid-close.

Session-bound dependencies to look for:

- **The stack itself on a workstation.** Laptop sleep freezes every host
  process mid-task — orchestrator polling, executor builds, evaluator runs.
  Nothing crashes; everything silently stops making progress, which is worse
  (no failure signal, no restart policy fires).
- **Tunnels / port-forwards originating from a workstation** (SSM
  port-forwarding, `ssh -L`, kubectl port-forward) that a service on the run's
  critical path calls through. Sleep breaks the TCP session; the far side may
  hang forever on the dead socket unless the caller sets an explicit network
  timeout. A keepalive/auto-restart loop shortens the outage but cannot help
  while the machine is asleep — it is a mitigation, not a substrate.
- **Foreground processes in an interactive shell / terminal multiplexer on the
  operator's machine** — watchdogs, drivers, monitors that the run needs (as
  opposed to ones that merely observe it).
- **Interactive-session credentials** — cloud CLI sessions that expire and
  need a human re-login mid-run, on a path the stack calls.

## What a conforming long-run substrate looks like

- Every EDEN service under a **process supervisor with a restart policy**
  (Compose `restart:` on a server that stays up, systemd units, or Kubernetes —
  see [`docs/deployment/helm.md`](../deployment/helm.md)), on machines that do
  not sleep.
- **All critical-path network hops server-to-server.** If a component can only
  be reached on loopback of a particular host (a local-only gateway, a
  unix-socket service), the EDEN service that calls it runs **on that host** —
  EDEN's services are distributed by design, so co-locate the one host that
  needs the loopback service and point it at the task-store over the network,
  rather than proxying the loopback service across a workstation.
- **Auto-checkpointing enabled** (orchestrator `--auto-checkpoint-dir`), so
  even a substrate loss is a resume, not a restart.
- The operator's machine is an **observer only**: anything running there
  (dashboards, log tails, ad-hoc queries) can die without the experiment
  noticing.

## Where the line sits

Local session-bound stacks are the right tool for what they're good at —
fast-feedback validation (see the smoke harness pattern: prove one full cycle
through the real host path before any remote run). The failure mode this page
exists to prevent is letting that validation setup *become* the run substrate
because it happens to be working: a stack that survives a 20-minute smoke will
still die at the first laptop sleep of a multi-hour run. Validate locally;
run on managed substrate. The point where a run is expected to outlive the
sitting is the point where the substrate requirement flips.
