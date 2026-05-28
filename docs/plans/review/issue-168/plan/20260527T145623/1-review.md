**Missing Context**

Assessment: much better. The plan now explains the filename policy, the predict/write/read lockstep, and the helper-home decision clearly enough for an implementer to follow. The key factual framing still checks out against the code: the ideator subprocess already writes hierarchical idea artifacts ([subprocess_mode.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/services/ideator/src/eden_ideator_host/subprocess_mode.py:300)), the serve route is layout-agnostic ([artifacts.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/services/web-ui/src/eden_web_ui/routes/artifacts.py:149)), the admin listing is recursive ([admin_artifacts.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/services/web-ui/src/eden_web_ui/routes/admin_artifacts.py:57)), and the web UI executor still only accepts a user-supplied `artifacts_uri` ([executor.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/services/web-ui/src/eden_web_ui/routes/executor.py:680)).

- The only remaining context problem is internal plan drift: the detailed design says the helper lives in `eden_service_common` and takes a target dir + filename policy ([plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:148)), but the scope section still says “one shared Python path-builder in `eden_web_ui/artifacts.py`” ([plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:306)).

**Feasibility**

Assessment: the revised approach can work. The full filename table and the redesigned helper interface close the main round-0 feasibility problem.

- The predict/write/read lockstep is now grounded in real code paths: the current writer still passes `idea.md` in the web UI ([ideator.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/services/web-ui/src/eden_web_ui/routes/ideator.py:414)), the reader still expects `IDEA_BUNDLE_HEADLINE = "idea.md"` ([routes/_helpers.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/services/web-ui/src/eden_web_ui/routes/_helpers.py:25)), and the CLI still passes `idea.md` today ([eden-manual](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/scripts/manual-ui/eden-manual:722)). The plan now correctly treats that as one coordinated change rather than a filename tweak.

**Alternatives**

Assessment: the chosen approach still looks right. Putting the shared helper in `eden_service_common` is the correct dependency direction, and the “full target path policy” approach is better than trying to keep the old `artifact_id`-driven helper shape.

- No stronger alternative stands out. The main thing I would keep is the current explicit decision not to make worker-host packages depend on `eden_web_ui` ([plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:148)).

**Completeness**

Assessment: substantially improved, but there are still a couple of stale sections that should be reconciled before implementation.

- The scope section is stale versus the design/files-to-touch sections. It still locates the shared helper in `eden_web_ui/artifacts.py` ([plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:306)), while the design and file inventory correctly move the source of truth to `eden_service_common` ([plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:148), [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:377)).
- Wave 1 also still describes the old implementation shape: “Add `entity_artifact_dir()` + thread entity-scoped base through `write_artifact_bundle` / `predict_artifact_uri`” and “decide the shared-helper home” ([plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:543)). That now contradicts D.1, which already decided the home and interface. This is the biggest remaining plan bug because it could cause the implementation to follow the outdated execution bullets instead of the corrected design.
- The test inventory is now much more honest. Good catch calling out that `test_ideator_subprocess.py` does not currently assert the path shape and that shared fixtures still fabricate flat paths.

**Edge Cases And Risks**

Assessment: the risk section is much stronger now. One concrete implementation trap is still worth calling out a bit more explicitly.

- The CLI writer currently stamps URIs with `CONTAINER_ARTIFACTS_DIR / host_path.name`, i.e. basename-only ([eden-manual](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/scripts/manual-ui/eden-manual:351)). With nested artifact paths, that must become “container artifacts dir + full relative path,” not just “change where files are written on disk.” The plan implies this in D.4, but it is easy to miss because `_translate_artifacts_uri_to_host` is already nested-path-safe while the write-side URI stamping is not.

Overall assessment: this is now a solid plan. The architectural issues from round 0 are addressed, and the remaining problems are mostly document-internal consistency. I would fix the stale scope/Wave-1 bullets before implementation so the execution section matches the corrected design exactly.