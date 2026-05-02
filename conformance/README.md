# EDEN Conformance Suite

Black-box test suite that any third-party implementation of an EDEN component can run against itself to prove conformance with the protocol specification.

## Status

**v1+roles shipped (Phase 11 chunk 11a + 11b + 11c).** The v1 suite covers the task-store + wire-binding subset: chapters 02 / 04 / 05 / 07 plus the storage MUSTs (chapter 08 §1.1, §1.7) the wire binding exposes. The v1+roles addendum (chunk 11c) covers chapter 03 §2.4 / §3.4 / §4.2 / §4.4 role-contract MUSTs across three new index groups (`Planner submission`, `Implementer submission`, `Evaluator submission`); 20 new scenarios bring the suite to 106 total. The chapter-06 integrator scenarios (chunk 11d) are out of v1+roles scope.

See [`spec/v0/09-conformance.md`](../spec/v0/09-conformance.md) for the normative chapter, including the level taxonomy (`v1` / `v1+roles` / `v1+roles+integrator`).

## Running the suite

Against the reference implementation:

```bash
uv run pytest -q conformance/
```

Against a third-party IUT (whose adapter implements `conformance.harness.adapter.IutAdapter`):

```bash
uv run pytest -q conformance/ --iut-adapter=my_pkg.my_module:MyAdapter
```

Verify every scenario cites a real spec section:

```bash
uv run python conformance/src/conformance/tools/check_citations.py
```

## Layout

- [`src/conformance/harness/`](src/conformance/harness/) — `IutAdapter` Protocol, pytest plugin, thin httpx `WireClient`, event-log helpers, scenario seeding helpers.
- [`src/conformance/adapters/reference/`](src/conformance/adapters/reference/) — reference adapter that spawns `python -m eden_task_store_server` against in-memory storage.
- [`src/conformance/_meta/`](src/conformance/_meta/) — the `MisbehavingAdapter` + proxy used by the self-validation scenario.
- [`src/conformance/tools/`](src/conformance/tools/) — citation-check tool.
- [`src/conformance/fixtures/`](src/conformance/fixtures/) — minimal experiment-config used by the harness.
- [`scenarios/`](scenarios/) — the test files; each citation in a docstring's first line is verified by `tools/check_citations.py`.

## Layer discipline

- `src/conformance/harness/` and `scenarios/` MUST NOT import from `reference/` packages.
- `src/conformance/adapters/reference/` is the only place where the reference impl is taught how to be a subject; it MAY import `eden_*` packages.
- CI gates this directionality.

## Adding a scenario

Every test's docstring's first line MUST cite a normative MUST in the spec, in the format `spec/v0/<chapter>.md §<sec>`. CI fails if the citation can't be resolved or if a chapter-9 §5 scenario-index group has no citing test.

### Asserting end-state, not endpoint

Some spec MUSTs allow the IUT latitude on **where** in the wire flow a rejection surfaces. For example, [`spec/v0/03-roles.md`](../spec/v0/03-roles.md) §3.4 says "`commit_sha` — required when `status == "success"`" but does not pin whether the IUT enforces this at `/submit` (shape validation) or `/accept` (acceptance validation). Both are conforming. Pinning the failing endpoint in a scenario codifies a reference-impl quirk as a conformance contract and locks third-party IUTs into the reference's choice — which the reference impl is explicitly NOT entitled to do (chapter 9 §1: "the spec is the source of truth; the suite is one faithful implementation of 'what would test that?'").

When writing a negative scenario for a MUST that has this latitude, follow this pattern:

```text
1. Drive the flow through every endpoint that could legally reject.
2. On each endpoint that responds:
   - 4xx → conforming-rejected; ALSO assert the trial-side end-state still
     satisfies the MUST (don't just return — a buggy server could 4xx and
     also corrupt state).
   - 2xx → must reject downstream; continue to the next endpoint.
   - 5xx → server bug, not §X latitude. FAIL the test (assert 4xx OR 2xx;
     5xx must surface, not silently pass).
3. After the final endpoint, assert the terminal observable state the MUST
   cares about (e.g. `trial.status != "success"`, `trial.metrics is None`).
```

Where the spec MUST is unambiguous about the endpoint (e.g. §3.4 "duplicate submit disagreeing on `commit_sha` MUST be rejected" — the rejection clearly belongs at `/submit` because that's the only endpoint a duplicate hits), pin the endpoint and status code as usual.

The chunk-11c [`test_implementer_submission.py`](scenarios/test_implementer_submission.py) `test_success_without_commit_sha_must_not_complete_trial` and the chunk-11c [`test_evaluator_submission.py`](scenarios/test_evaluator_submission.py) undeclared-metric / wrong-type tests are reference examples of the pattern.

### Hardening setup helpers

Setup helpers in [`src/conformance/harness/_seed.py`](src/conformance/harness/_seed.py) that drive multi-step wire flows (`drive_to_starting_trial`, `drive_to_success_trial`) MUST `raise_for_status()` on every wire response AND assert the resulting object's end-state matches the helper's docstring claim before returning. A helper that silently leaves a setup precondition violated turns a downstream-test failure into an opaque "the test failed for unrelated reasons" mystery; a helper that asserts up-front turns the same regression into a clear `AssertionError: setup precondition: ...` at the helper boundary. Cost is one extra `read_*` per helper invocation; benefit is every downstream scenario gets fail-fast setup verification for free.

### Schema-aware negative-end-state assertions

A scenario that asserts "field X is absent" on a rejection path MUST respect the schema's representation of "absent" — typically that means checking `field not in obj` (or `obj.get(field) is None`), NOT `obj.get(field) in (None, "")`. The schema for most EDEN entities (e.g. [`spec/v0/schemas/trial.schema.json`](../spec/v0/schemas/trial.schema.json) `trial_commit_sha`) permits the field to be absent OR a positively-shaped value (a SHA pattern, a URI, etc.) — empty string is **not** a conforming "unset" representation. A `in (None, "")` check would silently accept a non-conforming IUT that serialized `""` after a rejected operation; the suite would pass when it shouldn't. This is the negative-end-state dual of the chunk-11c "end-state, not endpoint" pattern: when asserting that something didn't happen, pin the schema-conforming shape of "didn't happen" rather than an over-broad "falsy".

For the full plan-writing pitfalls and conformance-discipline guidance, see [`AGENTS.md`](../AGENTS.md).
