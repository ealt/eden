# Reference bindings

This directory holds **non-normative** descriptions of how the
reference implementation binds the spec's normative shapes to a
particular transport, host, or invocation pattern. These bindings
are *examples*, not part of the protocol contract.

A conforming EDEN implementation does not have to match the
bindings recorded here. They exist so that:

- An alternative host can match the reference shape if it wants
  drop-in compatibility with the reference fixture experiment.
- Readers of the reference impl source can find a written
  description of the conventions instead of inferring them from
  code.

The HTTP wire binding for the chapter-4/5/6/8 store API is a
**normative** chapter ([07-wire-protocol.md](../07-wire-protocol.md))
because it pins a byte-for-byte interop surface that several
components must agree on. The bindings here are not at that level —
they're invocation-time conventions between a host and a
user-supplied command, scoped to a single reference deployment.

## Index

- [`worker-host-subprocess.md`](worker-host-subprocess.md) — how
  the reference planner / implementer / evaluator hosts invoke
  user-supplied `*_command` strings as subprocesses.
