No findings.

On your three checks:

1. `CONTROLLER_AGGREGATE_INCOMPLETE_KEY` covers the `worker_errordown` path correctly. In xdist, the controller calls `pytest_testnodedown` from both clean `workerfinished` and crash `worker_errordown`; the branch at [plugin.py](/Users/ericalt/Documents/eden-worktrees/conformance-speedup/conformance/src/conformance/harness/plugin.py:92) marks the aggregate incomplete on either `error is not None` or missing `_WORKEROUTPUT_OBSERVED_KEY`. That matches the two ways the controller can end up with a partial union.

2. Folding in partial observations on the crash path is safe. The update at [plugin.py](/Users/ericalt/Documents/eden-worktrees/conformance-speedup/conformance/src/conformance/harness/plugin.py:96) can only enlarge the observed set with real values already emitted by the worker; it cannot fabricate absence. Then [plugin.py](/Users/ericalt/Documents/eden-worktrees/conformance-speedup/conformance/src/conformance/harness/plugin.py:120) suppresses *all* closure assertions once incompleteness is known. That does mean an out-of-vocabulary type emitted before a crash may not be separately reported in that already-failing run, but it cannot turn the run green or mask the primary failure. That tradeoff is reasonable and better than reporting false “missing core type” noise from a partial union.

3. I don’t see a remaining parallel-safety or correctness concern in this path. The controller-side stash mutation is single-process/controller-only, clean workers still publish an empty list so zero-observation workers are handled, and the aggregate skip is limited to distributed runs with known incompleteness. The updated behavior is coherent.

I would not block merge.