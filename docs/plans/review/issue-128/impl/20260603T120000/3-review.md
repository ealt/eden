Findings:

- **major**: [spec/v0/02-data-model.md](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/spec/v0/02-data-model.md:143) still says `experiment_id` is “freshly minted on import without an override.” That preserves the old single-model rule and contradicts the dual-model framing now used in checkpoint §10/§11 and the reference single-experiment importer.

- **major**: [spec/v0/08-storage.md](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/spec/v0/08-storage.md:97) still says “without an override the receiver mints a fresh `exp_*`, so an identity collision is impossible.” The operation table just above it is corrected at line 91, but this paragraph still contradicts the single-experiment reference behavior.

The checkpoint-import sections you named in `07-wire-protocol.md` and `10-checkpoints.md` now read consistently with the implementation. I did not find another blocking/major issue in this pass beyond the two remaining stale authoritative lines above.

`python3 scripts/spec-xref-check.py` passes.

**CONVERGED: no**.