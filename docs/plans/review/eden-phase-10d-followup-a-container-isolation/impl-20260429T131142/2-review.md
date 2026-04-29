One low-severity doc drift remains.

1. Low: [docs/roadmap.md](/Users/ericalt/Documents/eden/docs/roadmap.md:202) still describes the pre-fix shape. It still talks about a single compose overlay applying `group_add`, says the planner-orphan-after-stop smoke proves `post_kill_callback` fired, and summarizes the older docker-test coverage (`SIGKILL-via-cidfile`) instead of the current split-overlay + end-to-end `Subprocess.terminate()` test. The plan, `AGENTS.md`, and the smoke comment are now aligned, but the roadmap entry is still stale.

Assessment: implementation is ready; I don’t see a remaining correctness/lifecycle/security/test issue. I’d update `docs/roadmap.md` to match the now-correct plan/AGENTS wording, then ship.