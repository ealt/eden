**Findings**

- `Bug` [reference/services/orchestrator/src/eden_orchestrator/loop.py](/Users/ericalt/Documents/eden/reference/services/orchestrator/src/eden_orchestrator/loop.py:50)  
  `run_orchestrator_loop()` swallows **every** exception while seeding plan tasks, even though the comment says only restart-safe `AlreadyExists` should be tolerated. A transport/auth/store failure here becomes a log line plus continued execution, so the orchestrator can quiesce and exit `0` without ever seeding work. This deviates from the plan’s orchestrator startup behavior. Fix: catch `AlreadyExists` specifically and re-raise everything else.

- `Bug` [reference/services/orchestrator/tests/test_e2e.py](/Users/ericalt/Documents/eden/reference/services/orchestrator/tests/test_e2e.py:208)  
  The timeout path does not dump subprocess stderr. `orchestrator.wait(timeout=60)` raises `subprocess.TimeoutExpired`, skips the `rc != 0` branch, and falls straight into teardown, so the failure loses the diagnostics the plan called “non-negotiable” in Tests §6 / Known risks. Fix: catch `TimeoutExpired` and fail with `_dump_stderr(procs)` before teardown; ideally do the same for all post-run assertion failures.

- `Risk` [reference/packages/eden-dispatch/src/eden_dispatch/workers.py](/Users/ericalt/Documents/eden/reference/packages/eden-dispatch/src/eden_dispatch/workers.py:97)  
  The worker `run_pending()` methods drain the entire pending queue before returning. Because the host loops only check `StopFlag` after `run_pending()` returns, SIGTERM is not observed “between iterations” in the way the plan and service docs describe; under backlog or slower task bodies, shutdown can overrun the harness timeout. Fix: make each host process at most one task per loop iteration, or thread a stop predicate into `run_pending()` so it can break between tasks.

- `Risk` [reference/services/orchestrator/tests/test_e2e.py](/Users/ericalt/Documents/eden/reference/services/orchestrator/tests/test_e2e.py:220)  
  The real-subprocess E2E does not verify the event log at all, despite the plan’s Tests §6 requiring assertions on “the event log and bare-repo state.” As written, regressions in emitted events or missing `trial.integrated` events can still pass if the final task/trial tables look right. Fix: reopen the store and assert key event counts/types, at minimum covering plan accept, implement accept, evaluate accept, and trial integration events.

- `Risk` [reference/services/orchestrator/src/eden_orchestrator/cli.py](/Users/ericalt/Documents/eden/reference/services/orchestrator/src/eden_orchestrator/cli.py:29)  
  The planned `--integrator-author NAME <EMAIL>` CLI surface was not implemented. The service always uses the hard-coded identity from [loop.py](/Users/ericalt/Documents/eden/reference/services/orchestrator/src/eden_orchestrator/loop.py:83), which is a plan-adherence gap and removes a promised operator control point. Fix: add the flag, parse it, and thread the resulting `Identity` into `Integrator(...)`.

- `Risk` [reference/services/orchestrator/src/eden_orchestrator/cli.py](/Users/ericalt/Documents/eden/reference/services/orchestrator/src/eden_orchestrator/cli.py:58)  
  `--max-quiescent-iterations` accepts `0` or `1`, even though the plan explicitly documents values `<=1` as unsafe because the orchestrator can exit while a worker is mid-submit. Fix: validate `>= 2` at parse time or reject unsafe values before entering the loop.

**Overall assessment**

The happy path is coherent: the service split, auth wiring, shared scaffolding, and subprocess E2E all hang together, and the focused tests plus the real E2E pass locally (`27` targeted tests passed; the `pytest.mark.e2e` test also passed). The main gaps are in robustness and plan completion rather than core architecture.

I’d treat the exception swallowing in orchestrator startup and the missing timeout diagnostics in the E2E as the highest-priority fixes. After that, I’d tighten shutdown behavior and close the remaining plan-adherence gaps around event-log assertions and the missing CLI surface.