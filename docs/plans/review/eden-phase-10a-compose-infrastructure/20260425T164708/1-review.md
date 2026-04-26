**Missing Context**

Brief assessment: the revised plan is much clearer. The Gitea/Postgres relationship is now explicit, and the earlier SSH/blob-init ambiguities are addressed.

- There is still one small internal inconsistency on file naming. The design section says “a single `docker-compose.yml` in 10a” at [docs/plans/eden-phase-10a-compose-infrastructure.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10a-compose-infrastructure.md:66), but the rest of the plan standardizes on `compose.yaml` at [docs/plans/eden-phase-10a-compose-infrastructure.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10a-compose-infrastructure.md:134). That should be normalized so the plan has one filename throughout.

**Feasibility**

Brief assessment: much improved, but I still see one material feasibility gap.

- The plan now depends on an unused top-level volume being created at `docker compose up` time, but the source it cites does not actually prove that case. The key design claim is at [docs/plans/eden-phase-10a-compose-infrastructure.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10a-compose-infrastructure.md:121), and the smoke test enforces it at [docs/plans/eden-phase-10a-compose-infrastructure.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10a-compose-infrastructure.md:340) and [docs/plans/eden-phase-10a-compose-infrastructure.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10a-compose-infrastructure.md:435). Docker’s volumes docs say `docker compose up` creates the volume in examples where a service mounts that volume; they do not clearly state that a completely unreferenced top-level volume is created just because it is declared. Since `eden-blob-data` is intentionally unmounted in 10a at [docs/plans/eden-phase-10a-compose-infrastructure.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10a-compose-infrastructure.md:175), this needs either:
  - a more specific authoritative source proving the behavior, or
  - a small implementation spike before the plan treats it as an invariant.
  
  Without that, the blob-volume strategy and its smoke test are still resting on an unverified assumption. Source used for this check: [Docker Compose volumes docs](https://docs.docker.com/reference/compose-file/volumes/).

I would stop here before reviewing alternatives, completeness, or later edge cases, because that blob-volume assumption is central to the revised design.

**Overall Assessment**

This draft is materially better than the previous one. The earlier SSH-port and one-shot-service problems are fixed. The remaining blocker is narrower: prove or replace the assumption that an unmounted named volume is created by `docker compose up`, and the plan will be in much better shape for a deeper round.