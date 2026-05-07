**Findings**

- **Major:** The duplicate-Pod story still is not implementable as written. The per-task flow only says “read pod logs” after `status.succeeded == 1` in [eden-phase-13b-executor-k8s-job.md](/Users/ericalt/Documents/eden-worktrees/phase-13b-executor-k8s-job/docs/plans/eden-phase-13b-executor-k8s-job.md:596), but the duplicate-execution section later says the host reads logs from a Job “primary” Pod via `pod-template-hash` in [eden-phase-13b-executor-k8s-job.md](/Users/ericalt/Documents/eden-worktrees/phase-13b-executor-k8s-job/docs/plans/eden-phase-13b-executor-k8s-job.md:1279). That does not define a concrete selection rule, and for Jobs the stable association labels are `batch.kubernetes.io/job-name` / `batch.kubernetes.io/controller-uid`, not a “primary pod” concept. The plan needs an explicit algorithm for “multiple Pods exist for one Job: which Pod do we inspect, and when do we instead treat the Job as error?” Without that, §8.10 is descriptive but not executable.

- **Moderate:** The alternatives section still preserves the old rationale that a Job “handles eviction-with-restart for free” with `backoffLimit: 0` in [eden-phase-13b-executor-k8s-job.md](/Users/ericalt/Documents/eden-worktrees/phase-13b-executor-k8s-job/docs/plans/eden-phase-13b-executor-k8s-job.md:351), which now contradicts the corrected semantics in Decision 1 and §8.10 in [eden-phase-13b-executor-k8s-job.md](/Users/ericalt/Documents/eden-worktrees/phase-13b-executor-k8s-job/docs/plans/eden-phase-13b-executor-k8s-job.md:155) and [eden-phase-13b-executor-k8s-job.md](/Users/ericalt/Documents/eden-worktrees/phase-13b-executor-k8s-job/docs/plans/eden-phase-13b-executor-k8s-job.md:1247). The plan’s chosen approach is now reasonable, but the compare-and-reject writeup should be reconciled so the decision logic is internally consistent.

- **Moderate:** §8.2 adds a new tuning knob, `--job-image-pull-deadline-seconds`, in [eden-phase-13b-executor-k8s-job.md](/Users/ericalt/Documents/eden-worktrees/phase-13b-executor-k8s-job/docs/plans/eden-phase-13b-executor-k8s-job.md:1115), but that flag is absent from the CLI section in [eden-phase-13b-executor-k8s-job.md](/Users/ericalt/Documents/eden-worktrees/phase-13b-executor-k8s-job/docs/plans/eden-phase-13b-executor-k8s-job.md:530), the files-to-touch list in [eden-phase-13b-executor-k8s-job.md](/Users/ericalt/Documents/eden-worktrees/phase-13b-executor-k8s-job/docs/plans/eden-phase-13b-executor-k8s-job.md:930), and the test plan. Either wire that option through end-to-end or remove it from the mitigation and keep image-pull failure handling on the existing deadline path.

- **Moderate:** The distroless guidance is still factually wrong. The plan says `gcr.io/distroless/base` includes a shell in [eden-phase-13b-executor-k8s-job.md](/Users/ericalt/Documents/eden-worktrees/phase-13b-executor-k8s-job/docs/plans/eden-phase-13b-executor-k8s-job.md:1203), but the distroless docs say distroless images do not contain shells by default; only the `:debug` variants provide one. That should be corrected so the operator guidance does not point readers at a base image that still fails the wrapper contract.

**Level Assessments**

- **1. Missing context:** Much better. The main-container contract is now explicit enough that the problem is well-defined.

- **2. Feasibility:** Mostly improved. The corrected Job semantics and explicit image contract make the core approach plausible.

- **3. Alternatives:** One inconsistency remains: the rejected Pod comparison still argues from the old eviction-restart assumption.

- **4. Completeness:** This is where the main remaining work is. The duplicate-Pod handling needs a concrete log-selection rule, and the new image-pull deadline knob is not wired through the rest of the plan.

- **5. Edge cases and risks:** The distroless/base recommendation should be fixed. I’d also tighten the smoke-test wording around “jobs were created during the run” before implementation, since the current text leans on an audit-log mechanism that is not described.

**Overall assessment**

This revision is substantially stronger than the previous one. I’d treat missing context and the core feasibility questions as largely resolved, but I would still revise the plan before implementation to close the duplicate-Pod selection gap, reconcile the alternatives section with the corrected semantics, and either fully define or drop the new image-pull deadline knob.

Sources checked for the Kubernetes/distroless points: [Kubernetes Job docs](https://kubernetes.io/docs/concepts/workloads/controllers/job/), [Kubernetes well-known Job labels](https://kubernetes.io/docs/reference/labels-annotations-taints/), [Distroless README](https://github.com/GoogleContainerTools/distroless).