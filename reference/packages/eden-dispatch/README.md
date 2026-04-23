# eden-dispatch

In-memory reference dispatch loop for the EDEN protocol (spec/v0).

Implements the task store, event log, proposal store, and trial store
as one cohesive in-memory component that enforces the transactional
invariant in
[`spec/v0/05-event-protocol.md`](../../../spec/v0/05-event-protocol.md) §2
— every state change commits atomically with its event(s) — plus the
full state machine from
[`spec/v0/04-task-protocol.md`](../../../spec/v0/04-task-protocol.md).

Also ships scripted planner, implementer, evaluator, and integrator
workers that drive the stores through a complete experiment lifecycle
with deterministic fake outputs. Together these prove the v0 spec is
implementable.

Explicitly out of scope: git, SQLite persistence, cross-process wire
protocols. Those land in later phases (6, 7, 8).
