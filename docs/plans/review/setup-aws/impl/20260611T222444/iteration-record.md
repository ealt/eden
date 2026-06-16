# Codex review record — setup-aws (issue #309, planless chunk), impl stage

Three synchronous codex rounds (gpt-5.5, codex CLI 1.0.4 via the
codex-companion runtime) over the `impl/issue-309-setup-aws` branch. Scope:
`reference/scripts/setup-aws/setup-aws.sh`, `test-setup-aws.sh`, and the
chart README / `docs/deployment/helm.md` pointer updates.

## Round 0 — full chunk review (commit 12ac82a)

Review axes requested: AWS CLI / eksctl flag + output-shape correctness;
idempotency / partial-state convergence; `--dry-run` fidelity; bash-3.2 +
shellcheck cleanliness; Helm chart contract integration (values schema,
IRSA trust subject vs the chart's ServiceAccount name, the
`setup-experiment-helm.sh --values` no-secret-generation contract);
security (secrets in argv/output, values-file permissions, IAM scoping);
structural erosion. Codex read both scripts, the chart schema/templates,
and live-queried `aws` CLI help for `create-db-instance`, `describe-addon`,
`head-bucket`, `describe-db-instances`; it also ran `bash -n` + shellcheck
(both clean).

**Verdict: fix-then-ship.** Findings and dispositions (all fixed in
e027948 unless noted):

| Sev | Finding | Disposition |
|---|---|---|
| P1 | `probe_eks_addon` accepted any addon status — `CREATE_FAILED` / `DEGRADED` skip-converged as healthy | Fixed: probe returns `addon.status`; `ACTIVE` skips, `CREATING`/`UPDATING` waits via `aws eks wait addon-active`, anything else dies loud |
| P2 | Interrupted `eksctl create cluster` (state `CREATING`) hit the fatal `*` branch — not convergent | Fixed: `CREATING`/`UPDATING` waits via `aws eks wait cluster-active` |
| P2 | Values-file write non-atomic — crash leaves a partial file; the next run's read-back silently regenerates missing secrets (the helm.md §5 rotation hazard) | Fixed: `mktemp "${VALUES_OUT}.XXXXXX"` + `chmod 0600` + `mv` |
| P2 | `--dry-run` printed the live DSN/password + secrets to stdout | Fixed: preview emits `<preserved>`/`<generated>` markers and masks DSN userinfo (`postgresql://<redacted>@…`); real values only ever reach the 0600 file |
| P2 | Existing same-named IAM policy never content-validated — a foreign policy was silently adopted | Fixed in two steps: e027948 added a bucket-ARN reference check; round 1 flagged it as PARTIAL (substring), d442d33 made it semantic (see round 1) |
| P2 | IRSA trust drift check was substring-based — extra principals / missing `:aud` not caught | Fixed: `trust_policy_matches` parses the JSON and requires exactly one Allow `sts:AssumeRoleWithWebIdentity` statement with the expected federated principal + `:sub` + `:aud` conditions |
| P2 | DB ingress probe missing `IsEgress==false` + `IpProtocol=='tcp'` + `ToPort==5432` predicates | Fixed: all predicates added to the JMESPath query |

Test harness grew with the fixes: mocks for the strengthened probes, new
cases for cluster-`CREATING` convergence and foreign-policy fail-loud, and
redaction assertions (97 checks, green under `/bin/bash` 3.2.57).

## Round 1 — fix verification (commit e027948)

Six of seven findings **VERIFIED-FIXED**; the IAM policy content check
**PARTIAL** (substring scan over the document — a Deny statement merely
mentioning the bucket ARN would have passed). Regression checks on the
fixes themselves all passed: the bash-3.2 `${var/pat/rep}` DSN masking,
the `mktemp` template shape, the `python3 -c` trust parser under
`set -euo pipefail`, and both `aws eks wait` subcommands verified against
AWS CLI v2.33.20 help. No new findings. **Verdict: fix-then-ship** (the
one partial).

## Round 2 — final verification (commit d442d33)

`policy_document_matches` confirmed **VERIFIED-FIXED**: semantic grant
check requiring Allow statements (NotAction/NotResource excluded) that
grant `s3:GetObject` + `s3:PutObject` on `arn:aws:s3:::<bucket>/*` and
`s3:ListBucket` on `arn:aws:s3:::<bucket>`; string-vs-list Action/Resource
normalization, pipefail exit-code routing, and malformed-document handling
all checked. No regressions. **Verdict: SHIP.**

## Process note

The first round-0 attempt aborted on an environment issue: the codex CLI
rejected `service_tier = "default"` in `~/.codex/config.toml` (1.0.4
accepts only `fast`/`flex`); the line was commented out and the review
re-run. Raw transcripts were streamed (regenerable; not committed per the
`.gitignore` policy for review transcripts); codex session thread ids:
round 0 `019eb8b9-a7b1-7c33-8474-a95350fb0ef2`.
