# EDEN

**E**ric's **D**irected **E**volution **N**exus — the meeting point where intelligent planning and evolutionary selection converge.

## Tagline

> Intelligent evolution.

## Short description

EDEN is a protocol for orchestrating directed code evolution, with an open reference implementation. Planners propose experiments, parallel trials run against a shared data model, results feed back into the next round — an automated loop of diversify, evaluate, amplify. Anyone can build a conforming planner, implementer, evaluator, or backing store in any language and interoperate with other conforming components.

## Elevator pitch

Most research automation tools are either a single-agent loop (try something, check if it worked, repeat) or a static job runner (execute a fixed matrix of experiments). EDEN is neither — and, importantly, EDEN is not a single system. It is a **protocol**: a specification that defines the roles (planner, implementer, evaluator, integrator), the messages they exchange, and the invariants they must honor. A conforming system coordinates concurrent research trials in which a planner — human, AI, or hybrid — proposes experiments, an integrator dispatches them against a versioned workspace, implementers modify code on isolated branches, and evaluators score the results against a fitness function. Results flow back to the planner to inform the next generation of proposals. The architecture mirrors directed evolution in the lab: generate a library of variants, screen them, amplify the winners, repeat.

The repo is structured to house a **reference implementation** of the protocol, built up incrementally over the roadmap, plus a **conformance suite** that any independently-built component will be able to run against itself once the suite lands. The planner brings the intelligence; EDEN's contracts bring the infrastructure to run the search at scale, regardless of which implementation is plugged in where.

## Etymology

The name operates on two levels.

**The acronym:** Eric's Directed Evolution Nexus — a nexus where experimental proposals, parallel execution, and evaluative selection meet. The system is the convergence point between a planner's intelligence and the outcomes of automated trials.

**The allusion:** The Garden of Eden is where creation happens by design — an omniscient creator, a deliberate act. Directed evolution is the opposite: creation through iterative selection, not foresight. EDEN sits in the tension between these two ideas. The planner brings intent and strategy (intelligent design), but the mechanism is evolutionary — propose variants, run trials, select the fittest, repeat. The tagline "Intelligent evolution" captures this tension directly: it inverts "intelligent design," preserving the cadence but swapping the noun.

## Scientific lineage

Directed evolution is a Nobel Prize-winning technique (Frances Arnold, 2018) for engineering proteins through iterative cycles of diversification, screening, and amplification. A researcher generates a library of variants, evaluates them against a fitness function, and uses the best results as the template for the next round.

The parallel to EDEN's architecture is nearly 1:1:

| Directed Evolution          | EDEN                                        |
| --------------------------- | ------------------------------------------- |
| Library of variants         | Parallel trials in isolated git worktrees   |
| Screening / assay           | Eval script producing JSON metrics          |
| Amplification (next round)  | Planner reads results, proposes next batch  |
| Iterative rounds            | The propose / execute / evaluate loop       |
| Mutagenesis                 | Planner proposes modifications to the code  |
| Intelligent guidance        | Planner is strategic, not random            |

## Package name (reference implementation, planned)

The Python reference implementation **will be published** as `direvo` (**dir**ected **evo**lution) once it exists (landing in Phase 3 per `docs/roadmap.md`). Both `direvo` and `eden` will work as CLI commands after install — users can think of the tool as EDEN while the package name stays short, unique, and conflict-free.

```bash
# After Phase 3 lands, installation will look like:
pip install direvo
eden run --config .eden/config.yaml
```

The name "eden" is heavily used across the software ecosystem (Eden AI, Eden emulator, multiple PyPI packages), making it impractical as a package name. The dual-name strategy avoids all registry conflicts while giving users the brand name at the command line.

This naming applies to the Python reference implementation only. Third-party implementations in other languages / ecosystems are free to choose their own names and should not use `direvo` or `eden` without coordination; the protocol version they target (e.g. `eden-protocol/v0`) is the identity that matters for interoperability.

## Availability check — `direvo`

Historical name-squatting check carried over from the predecessor project. Re-verify before actually publishing in Phase 3+.

| Registry    | Name   | Status as of 2025-03-27 |
| ----------- | ------ | ----------------------- |
| PyPI        | direvo | Available               |
| GitHub user | direvo | Available               |
| GitHub org  | direvo | Available               |
| npm         | direvo | Available               |
