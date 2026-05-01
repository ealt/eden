# EDEN Conformance Suite

Black-box test suite that any third-party implementation of an EDEN component can run against itself to prove conformance with the protocol specification.

## Status

**v1 shipped (Phase 11 chunk 11a + 11b).** The v1 suite covers the task-store + wire-binding subset: chapters 02 / 04 / 05 / 07 plus the storage MUSTs (chapter 08 §1.1, §1.7) the wire binding exposes. The chapter-03 role contracts (chunk 11c) and chapter-06 integrator scenarios (chunk 11d) are out of v1 scope.

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

For the full plan-writing pitfalls and conformance-discipline guidance, see [`AGENTS.md`](../AGENTS.md).
