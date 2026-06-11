#!/usr/bin/env bash
set -euo pipefail

# test-setup-aws — offline tests for setup-aws.sh's --dry-run path.
#
# Drives the script through EDEN_SETUP_AWS_MOCK (probe_* overrides), so no
# AWS credentials, network, or docker/eksctl/kubectl/helm are needed. Four
# state fixtures:
#   * all-absent  — fresh account: every create command must be emitted, in
#                   dependency order.
#   * all-present — converged account: every step must take its skip path
#                   (no create/build/push emitted) and the DSN must be
#                   recomposed from the probed endpoint + managed password.
#   * partial     — mixed state (cluster exists, image tag missing, bucket
#                   missing, IRSA trust drifted, --postgres-dsn passthrough).
#   * flag errors — missing/conflicting required flags fail loud (exit 2)
#                   naming the flag.
#
# No CI job runs bash tests today; run this manually before touching
# setup-aws.sh:  bash reference/scripts/setup-aws/test-setup-aws.sh
#
# Bash-3.2-clean per AGENTS.md.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SETUP="${SCRIPT_DIR}/setup-aws.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FAILURES=0
CHECKS=0

pass() { CHECKS=$((CHECKS + 1)); echo "  ok: $1"; }
fail() { CHECKS=$((CHECKS + 1)); FAILURES=$((FAILURES + 1)); echo "  FAIL: $1" >&2; }

assert_contains() {
    # assert_contains <name> <haystack> <needle>  (-- guards needles that
    # start with a dash, e.g. flag names)
    if printf '%s' "$2" | grep -F -q -- "$3"; then
        pass "$1"
    else
        fail "$1 — output does not contain: $3"
    fi
}

assert_not_contains() {
    if printf '%s' "$2" | grep -F -q -- "$3"; then
        fail "$1 — output unexpectedly contains: $3"
    else
        pass "$1"
    fi
}

line_of() {
    # line_of <haystack> <needle> — first matching line number (empty if none)
    printf '%s\n' "$1" | grep -n -F -- "$2" | head -n1 | cut -d: -f1
}

assert_order() {
    # assert_order <name> <haystack> <earlier-needle> <later-needle>
    local a b
    a="$(line_of "$2" "$3")"
    b="$(line_of "$2" "$4")"
    if [[ -n "$a" && -n "$b" && "$a" -lt "$b" ]]; then
        pass "$1"
    else
        fail "$1 — expected '$3' (line ${a:-absent}) before '$4' (line ${b:-absent})"
    fi
}

assert_rc() {
    # assert_rc <name> <actual> <expected>
    if [[ "$2" -eq "$3" ]]; then
        pass "$1"
    else
        fail "$1 — exit code $2, expected $3"
    fi
}

# Base operator args shared by the state-fixture cases.
base_args() {
    echo --cluster-name test-eks --region us-east-1 \
        --ecr-repo eden-reference --s3-bucket test-eden-blob \
        --image-tag testtag --dry-run
}

run_setup() {
    # run_setup <mock-file> <args...> — captures stdout+stderr into OUT and
    # the exit code into RC (AWS_REGION cleared so only flags drive it).
    set +e
    # shellcheck disable=SC2034  # OUT/RC are the function's outputs
    OUT="$(AWS_REGION="" EDEN_SETUP_AWS_MOCK="$1" "$SETUP" "${@:2}" 2>&1)"
    RC=$?
    set -e
}

# ----------------------------------------------------------------------
# Mock fixtures
# ----------------------------------------------------------------------
MOCK_ABSENT="${WORK}/mock-absent.sh"
cat >"$MOCK_ABSENT" <<'EOF'
probe_caller_account() { echo 123456789012; }
probe_eks_cluster_status() { return 1; }
probe_eks_oidc_issuer() { echo "https://oidc.eks.us-east-1.amazonaws.com/id/MOCK"; }
probe_eks_vpc_id() { echo vpc-mock; }
probe_eks_subnet_ids() { echo "subnet-aaa subnet-bbb"; }
probe_eks_cluster_sg() { echo sg-cluster; }
probe_oidc_provider() { return 1; }
probe_eks_addon_status() { return 1; }
probe_ecr_repo() { return 1; }
probe_ecr_image() { return 1; }
probe_rds_instance_status() { return 1; }
probe_rds_endpoint() { return 1; }
probe_rds_master_secret_arn() { return 1; }
probe_rds_master_password() { return 1; }
probe_db_subnet_group() { return 1; }
probe_db_security_group_id() { return 1; }
probe_db_sg_ingress() { return 1; }
probe_s3_bucket() { return 1; }
probe_iam_policy() { return 1; }
probe_iam_policy_document() { return 1; }
probe_iam_role_exists() { return 1; }
probe_iam_role_trust() { return 1; }
probe_role_policy_attached() { return 1; }
EOF

MOCK_PRESENT="${WORK}/mock-present.sh"
cat >"$MOCK_PRESENT" <<'EOF'
probe_caller_account() { echo 123456789012; }
probe_eks_cluster_status() { echo ACTIVE; }
probe_eks_oidc_issuer() { echo "https://oidc.eks.us-east-1.amazonaws.com/id/MOCK"; }
probe_eks_vpc_id() { echo vpc-mock; }
probe_eks_subnet_ids() { echo "subnet-aaa subnet-bbb"; }
probe_eks_cluster_sg() { echo sg-cluster; }
probe_oidc_provider() { return 0; }
probe_eks_addon_status() { echo ACTIVE; }
probe_ecr_repo() { return 0; }
probe_ecr_image() { return 0; }
probe_rds_instance_status() { echo available; }
probe_rds_endpoint() { echo "db.example.com:5432"; }
probe_rds_master_secret_arn() { echo "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds-mock"; }
probe_rds_master_password() { echo "p@ss w0rd"; }
probe_db_subnet_group() { return 0; }
probe_db_security_group_id() { echo sg-db; }
probe_db_sg_ingress() { return 0; }
probe_s3_bucket() { return 0; }
probe_iam_policy() { return 0; }
probe_iam_policy_document() { printf '%s' '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],"Resource":"arn:aws:s3:::test-eden-blob/*"},{"Effect":"Allow","Action":["s3:ListBucket"],"Resource":"arn:aws:s3:::test-eden-blob"}]}'; }
probe_iam_role_exists() { return 0; }
probe_iam_role_trust() { printf '%s' '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Federated":"arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/MOCK"},"Action":"sts:AssumeRoleWithWebIdentity","Condition":{"StringEquals":{"oidc.eks.us-east-1.amazonaws.com/id/MOCK:sub":"system:serviceaccount:eden:eden-task-store-server","oidc.eks.us-east-1.amazonaws.com/id/MOCK:aud":"sts.amazonaws.com"}}}]}'; }
probe_role_policy_attached() { return 0; }
EOF

# Mixed state: cluster converged; ECR repo exists but the tag is unpushed;
# bucket missing; IRSA role exists with a DRIFTED trust subject; policy
# exists but is not attached.
MOCK_PARTIAL="${WORK}/mock-partial.sh"
cat >"$MOCK_PARTIAL" <<'EOF'
probe_caller_account() { echo 123456789012; }
probe_eks_cluster_status() { echo ACTIVE; }
probe_eks_oidc_issuer() { echo "https://oidc.eks.us-east-1.amazonaws.com/id/MOCK"; }
probe_eks_vpc_id() { echo vpc-mock; }
probe_eks_subnet_ids() { echo "subnet-aaa subnet-bbb"; }
probe_eks_cluster_sg() { echo sg-cluster; }
probe_oidc_provider() { return 0; }
probe_eks_addon_status() { echo ACTIVE; }
probe_ecr_repo() { return 0; }
probe_ecr_image() { return 1; }
probe_s3_bucket() { return 1; }
probe_iam_policy() { return 0; }
probe_iam_policy_document() { printf '%s' '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],"Resource":"arn:aws:s3:::test-eden-blob/*"},{"Effect":"Allow","Action":["s3:ListBucket"],"Resource":"arn:aws:s3:::test-eden-blob"}]}'; }
probe_iam_role_exists() { return 0; }
probe_iam_role_trust() { printf '%s' '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Federated":"arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/MOCK"},"Action":"sts:AssumeRoleWithWebIdentity","Condition":{"StringEquals":{"oidc.eks.us-east-1.amazonaws.com/id/MOCK:sub":"system:serviceaccount:OTHER-NS:other-task-store-server","oidc.eks.us-east-1.amazonaws.com/id/MOCK:aud":"sts.amazonaws.com"}}}]}'; }
probe_role_policy_attached() { return 1; }
EOF

# ----------------------------------------------------------------------
# Case 1: all-absent — every create emitted, in dependency order
# ----------------------------------------------------------------------
echo "case: all-absent (fresh account)"
# shellcheck disable=SC2046  # base_args is deliberately word-split
run_setup "$MOCK_ABSENT" $(base_args) \
    --db-instance-id test-eden-pg --db-instance-class db.t4g.small \
    --node-type m5.large --nodes 2 \
    --values-out "${WORK}/values-absent.yaml"
assert_rc "exits 0" "$RC" 0
assert_contains "creates the cluster"        "$OUT" "DRY-RUN: eksctl create cluster"
assert_contains "passes node shape"          "$OUT" "--node-type m5.large --nodes 2"
assert_not_contains "no redundant OIDC associate after --with-oidc create" "$OUT" "associate-iam-oidc-provider"
assert_contains "creates the EBS CSI role"   "$OUT" "DRY-RUN: eksctl create iamserviceaccount"
assert_contains "installs the EBS CSI addon" "$OUT" "DRY-RUN: eksctl create addon"
assert_contains "updates kubeconfig"         "$OUT" "DRY-RUN: aws eks update-kubeconfig"
assert_contains "creates the ECR repo"       "$OUT" "DRY-RUN: aws ecr create-repository"
assert_contains "ECR login pipeline"         "$OUT" "aws ecr get-login-password --region us-east-1 | docker login"
assert_contains "builds the image"           "$OUT" "DRY-RUN: docker build"
assert_contains "pushes the image"           "$OUT" "DRY-RUN: docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/eden-reference:testtag"
assert_contains "creates the DB subnet group" "$OUT" "DRY-RUN: aws rds create-db-subnet-group"
# The cluster does not exist yet, so VPC facts are unknowable in dry-run —
# the plan must carry the explicit placeholder, not a fabricated value.
assert_contains "subnet ids are the pending placeholder" "$OUT" "pending-cluster-create"
assert_contains "creates the DB security group" "$OUT" "DRY-RUN: aws ec2 create-security-group"
assert_contains "authorizes 5432 from the cluster SG" "$OUT" "DRY-RUN: aws ec2 authorize-security-group-ingress"
assert_contains "creates the RDS instance"   "$OUT" "DRY-RUN: aws rds create-db-instance"
assert_contains "uses the managed master password" "$OUT" "--manage-master-user-password"
assert_contains "waits for availability"     "$OUT" "DRY-RUN: aws rds wait db-instance-available"
assert_contains "creates the bucket"         "$OUT" "DRY-RUN: aws s3api create-bucket"
assert_contains "creates the IAM policy"     "$OUT" "DRY-RUN: aws iam create-policy"
assert_contains "creates the IRSA role"      "$OUT" "DRY-RUN: aws iam create-role"
assert_contains "attaches policy to role"    "$OUT" "DRY-RUN: aws iam attach-role-policy"
assert_contains "emits the values file plan" "$OUT" "would write ${WORK}/values-absent.yaml"
assert_order "cluster before ECR"            "$OUT" "eksctl create cluster" "aws ecr create-repository"
assert_order "subnet group before instance"  "$OUT" "create-db-subnet-group" "create-db-instance"
assert_order "policy before attach"          "$OUT" "aws iam create-policy" "aws iam attach-role-policy"
assert_contains "emits the handoff command"  "$OUT" "setup-experiment-helm.sh"
assert_not_contains "did not write the values file in dry-run" "$(ls "$WORK")" "values-absent.yaml"

# ----------------------------------------------------------------------
# Case 2: all-present — every step takes its skip path
# ----------------------------------------------------------------------
echo "case: all-present (converged account)"
# shellcheck disable=SC2046
run_setup "$MOCK_PRESENT" $(base_args) \
    --db-instance-id test-eden-pg \
    --values-out "${WORK}/values-present.yaml"
assert_rc "exits 0" "$RC" 0
assert_contains "cluster skip"  "$OUT" "EKS cluster exists and is ACTIVE — skipping create"
assert_contains "OIDC skip"     "$OUT" "already associated — skipping"
assert_contains "addon skip"    "$OUT" "aws-ebs-csi-driver addon already ACTIVE — skipping"
assert_contains "ECR repo skip" "$OUT" "ECR repository exists — skipping create"
assert_contains "image skip"    "$OUT" "already pushed — skipping build + push"
assert_contains "RDS skip"      "$OUT" "RDS instance exists and is available — skipping create"
assert_contains "bucket skip"   "$OUT" "S3 bucket exists and is accessible — skipping create"
assert_contains "policy skip"   "$OUT" "exists — skipping create"
assert_contains "role skip"     "$OUT" "exists with the expected trust policy — skipping"
assert_contains "attach skip"   "$OUT" "policy already attached to the IRSA role — skipping"
assert_not_contains "no eksctl mutation"  "$OUT" "DRY-RUN: eksctl"
assert_not_contains "no docker mutation"  "$OUT" "DRY-RUN: docker"
assert_not_contains "no create mutation"  "$OUT" "create-db-instance"
assert_not_contains "no bucket create"    "$OUT" "s3api create-bucket"
assert_not_contains "no IAM mutation"     "$OUT" "aws iam create-"
assert_contains "DSN from probed endpoint (userinfo redacted in the preview)" \
    "$OUT" 'connectionString: "postgresql://<redacted>@db.example.com:5432/eden?sslmode=require"'
assert_not_contains "managed master password never printed" "$OUT" "p%40ss%20w0rd"
assert_not_contains "raw master password never printed"     "$OUT" "p@ss w0rd"
assert_contains "IRSA roleArn emitted" "$OUT" 'roleArn: "arn:aws:iam::123456789012:role/test-eks-eden-blob-irsa"'
assert_contains "image values emitted" "$OUT" 'repository: "123456789012.dkr.ecr.us-east-1.amazonaws.com/eden-reference"'

# ----------------------------------------------------------------------
# Case 3: secrets are preserved from an existing values file
# ----------------------------------------------------------------------
echo "case: secret preservation on re-run"
PRESEED="${WORK}/values-preseed.yaml"
cat >"$PRESEED" <<'EOF'
secrets:
  adminToken: "deadbeefcafe0000deadbeefcafe0000"
EOF
# shellcheck disable=SC2046
run_setup "$MOCK_PRESENT" $(base_args) \
    --db-instance-id test-eden-pg \
    --values-out "$PRESEED"
assert_rc "exits 0" "$RC" 0
assert_contains "adminToken preserved, not rotated" "$OUT" 'adminToken: "<preserved>"'
assert_contains "absent secrets freshly generated" "$OUT" 'sessionSecret: "<generated>"'
assert_not_contains "preserved secret value never printed" "$OUT" "deadbeefcafe0000"

# ----------------------------------------------------------------------
# Case 4: partial state — only the missing pieces are created
# ----------------------------------------------------------------------
echo "case: partial state (skip + create mixed; --postgres-dsn passthrough)"
# shellcheck disable=SC2046
run_setup "$MOCK_PARTIAL" $(base_args) \
    --postgres-dsn "postgresql://eden:secret@external-db.example.com:5432/eden?sslmode=require" \
    --values-out "${WORK}/values-partial.yaml"
assert_rc "exits 0" "$RC" 0
assert_contains "cluster skip"               "$OUT" "skipping create"
assert_contains "ECR repo skip"              "$OUT" "ECR repository exists — skipping create"
assert_contains "missing tag still builds"   "$OUT" "DRY-RUN: docker build"
assert_contains "missing tag still pushes"   "$OUT" "DRY-RUN: docker push"
assert_contains "RDS skipped on DSN"         "$OUT" "RDS — skipped (--postgres-dsn supplied)"
assert_not_contains "no RDS mutation"        "$OUT" "create-db-instance"
assert_contains "operator DSN passed through (userinfo redacted in the preview)" \
    "$OUT" 'connectionString: "postgresql://<redacted>@external-db.example.com:5432/eden?sslmode=require"'
assert_not_contains "operator DSN credentials never printed" "$OUT" "eden:secret@"
assert_contains "missing bucket created"     "$OUT" "DRY-RUN: aws s3api create-bucket"
assert_contains "policy skip"                "$OUT" "exists — skipping create"
assert_contains "trust drift converged"      "$OUT" "DRY-RUN: aws iam update-assume-role-policy"
assert_not_contains "drifted role not recreated" "$OUT" "aws iam create-role"
assert_contains "unattached policy attached" "$OUT" "DRY-RUN: aws iam attach-role-policy"

# ----------------------------------------------------------------------
# Case 5: RDS absent against an EXISTING cluster — network plumbing is
# derived from the live cluster facts (subnets / VPC / cluster SG)
# ----------------------------------------------------------------------
echo "case: RDS absent against an existing cluster"
MOCK_RDS_ABSENT="${WORK}/mock-rds-absent.sh"
cat >"$MOCK_RDS_ABSENT" <<'EOF'
probe_caller_account() { echo 123456789012; }
probe_eks_cluster_status() { echo ACTIVE; }
probe_eks_oidc_issuer() { echo "https://oidc.eks.us-east-1.amazonaws.com/id/MOCK"; }
probe_eks_vpc_id() { echo vpc-mock; }
probe_eks_subnet_ids() { echo "subnet-aaa subnet-bbb"; }
probe_eks_cluster_sg() { echo sg-cluster; }
probe_oidc_provider() { return 0; }
probe_eks_addon_status() { echo ACTIVE; }
probe_ecr_repo() { return 0; }
probe_ecr_image() { return 0; }
probe_rds_instance_status() { return 1; }
probe_rds_endpoint() { return 1; }
probe_rds_master_secret_arn() { return 1; }
probe_rds_master_password() { return 1; }
probe_db_subnet_group() { return 1; }
probe_db_security_group_id() { return 1; }
probe_db_sg_ingress() { return 1; }
probe_s3_bucket() { return 0; }
probe_iam_policy() { return 0; }
probe_iam_policy_document() { printf '%s' '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],"Resource":"arn:aws:s3:::test-eden-blob/*"},{"Effect":"Allow","Action":["s3:ListBucket"],"Resource":"arn:aws:s3:::test-eden-blob"}]}'; }
probe_iam_role_exists() { return 0; }
probe_iam_role_trust() { printf '%s' '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Federated":"arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/MOCK"},"Action":"sts:AssumeRoleWithWebIdentity","Condition":{"StringEquals":{"oidc.eks.us-east-1.amazonaws.com/id/MOCK:sub":"system:serviceaccount:eden:eden-task-store-server","oidc.eks.us-east-1.amazonaws.com/id/MOCK:aud":"sts.amazonaws.com"}}}]}'; }
probe_role_policy_attached() { return 0; }
EOF
# shellcheck disable=SC2046
run_setup "$MOCK_RDS_ABSENT" $(base_args) \
    --db-instance-id test-eden-pg --db-instance-class db.t4g.small \
    --values-out "${WORK}/values-rds.yaml"
assert_rc "exits 0" "$RC" 0
assert_contains "subnet group spans the cluster subnets" "$OUT" "subnet-aaa subnet-bbb"
assert_contains "security group lands in the cluster VPC" "$OUT" "--vpc-id vpc-mock"
assert_contains "5432 source is the cluster SG" "$OUT" "--source-group sg-cluster"
assert_contains "instance class passed through" "$OUT" "--db-instance-class db.t4g.small"
assert_not_contains "no cluster mutation" "$OUT" "eksctl create cluster"

# ----------------------------------------------------------------------
# Case 6: interrupted creates converge — cluster stuck CREATING is waited
# on, not failed on and not duplicated
# ----------------------------------------------------------------------
echo "case: cluster CREATING (interrupted earlier create)"
MOCK_CREATING="${WORK}/mock-creating.sh"
sed 's/probe_eks_cluster_status() { echo ACTIVE; }/probe_eks_cluster_status() { echo CREATING; }/' \
    "$MOCK_PRESENT" > "$MOCK_CREATING"
# shellcheck disable=SC2046
run_setup "$MOCK_CREATING" $(base_args) \
    --postgres-dsn "postgresql://eden:x@db/eden" \
    --values-out "${WORK}/values-creating.yaml"
assert_rc "exits 0" "$RC" 0
assert_contains "waits for the in-flight create" "$OUT" "DRY-RUN: aws eks wait cluster-active"
assert_not_contains "does not start a duplicate create" "$OUT" "eksctl create cluster"

# ----------------------------------------------------------------------
# Case 7: existing same-named IAM policy that does NOT reference the
# bucket fails loud instead of being silently adopted
# ----------------------------------------------------------------------
echo "case: foreign IAM policy under the derived name"
MOCK_POLICY_DRIFT="${WORK}/mock-policy-drift.sh"
sed 's|arn:aws:s3:::test-eden-blob|arn:aws:s3:::someone-elses-bucket|g' \
    "$MOCK_PRESENT" > "$MOCK_POLICY_DRIFT"
# shellcheck disable=SC2046
run_setup "$MOCK_POLICY_DRIFT" $(base_args) \
    --postgres-dsn "postgresql://eden:x@db/eden" \
    --values-out "${WORK}/values-drift.yaml"
assert_rc "exits 2" "$RC" 2
assert_contains "names the conflicting policy" "$OUT" "does not reference"
assert_contains "points at the escape hatch" "$OUT" "--irsa-policy-name"

# ----------------------------------------------------------------------
# Case 8: flag validation fails loud, naming the flag
# ----------------------------------------------------------------------
echo "case: flag validation"

run_setup "$MOCK_ABSENT" --region us-east-1 --ecr-repo r --s3-bucket b \
    --db-instance-id d --dry-run
assert_rc "missing --cluster-name exits 2" "$RC" 2
assert_contains "names --cluster-name" "$OUT" "--cluster-name is required"

run_setup "$MOCK_ABSENT" --cluster-name c --ecr-repo r --s3-bucket b \
    --db-instance-id d --dry-run
assert_rc "missing --region exits 2" "$RC" 2
assert_contains "names --region" "$OUT" "--region is required"

run_setup "$MOCK_ABSENT" --cluster-name c --region us-east-1 --s3-bucket b \
    --db-instance-id d --dry-run
assert_rc "missing --ecr-repo exits 2" "$RC" 2
assert_contains "names --ecr-repo" "$OUT" "--ecr-repo is required"

run_setup "$MOCK_ABSENT" --cluster-name c --region us-east-1 --ecr-repo r \
    --db-instance-id d --dry-run
assert_rc "missing --s3-bucket exits 2" "$RC" 2
assert_contains "names --s3-bucket" "$OUT" "--s3-bucket is required"

run_setup "$MOCK_ABSENT" --cluster-name c --region us-east-1 --ecr-repo r \
    --s3-bucket b --dry-run
assert_rc "missing both DB flags exits 2" "$RC" 2
assert_contains "names the DB choice" "$OUT" "--db-instance-id (provision RDS) or --postgres-dsn"

run_setup "$MOCK_ABSENT" --cluster-name c --region us-east-1 --ecr-repo r \
    --s3-bucket b --db-instance-id d --postgres-dsn postgresql://x --dry-run
assert_rc "conflicting DB flags exit 2" "$RC" 2
assert_contains "names the conflict" "$OUT" "mutually exclusive"

# shellcheck disable=SC2046
run_setup "$MOCK_ABSENT" $(base_args) --db-instance-id d \
    --db-instance-class db.t4g.small
assert_rc "absent cluster without node shape exits 2" "$RC" 2
assert_contains "names --node-type" "$OUT" "--node-type is required to CREATE"

# shellcheck disable=SC2046
run_setup "$MOCK_ABSENT" $(base_args) --db-instance-id d \
    --node-type m5.large --nodes 2
assert_rc "absent RDS without instance class exits 2" "$RC" 2
assert_contains "names --db-instance-class" "$OUT" "--db-instance-class is required to CREATE"

run_setup "$MOCK_ABSENT" --bogus-flag
assert_rc "unknown flag exits 2" "$RC" 2
assert_contains "reports the unknown flag" "$OUT" "unknown argument: --bogus-flag"

# ----------------------------------------------------------------------
echo
if [[ "$FAILURES" -gt 0 ]]; then
    echo "test-setup-aws: ${FAILURES}/${CHECKS} checks FAILED" >&2
    exit 1
fi
echo "test-setup-aws: all ${CHECKS} checks passed"
