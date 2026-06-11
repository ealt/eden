#!/usr/bin/env bash
set -euo pipefail

# setup-aws — idempotent create-if-absent provisioning of everything the
# EDEN Helm chart needs upstream of setup-experiment-helm.sh, on AWS
# (issue #309, AWS MVP milestone):
#
#   1. EKS    — verify the named cluster (or create it via eksctl),
#               associate the IAM OIDC provider, ensure the aws-ebs-csi-driver
#               addon (without it, no RWO PVC provisions on EKS >= 1.23 and
#               the chart's StatefulSets stay Pending), update kubeconfig.
#   2. ECR    — create-if-absent repository; build the reference image from
#               reference/compose/Dockerfile; push; emit image.repository/tag.
#   3. RDS    — create-if-absent Postgres instance in the cluster's VPC
#               (or accept an existing DSN via --postgres-dsn and skip);
#               master password is AWS-managed (Secrets Manager), so re-runs
#               re-read it instead of losing it; emit the postgres.mode=external
#               connection string.
#   4. S3+IRSA — create-if-absent bucket; create/verify the IAM policy + role
#               whose trust policy lets the chart's task-store-server
#               ServiceAccount assume it (blob.backend=s3 pod identity).
#   5. Handoff — write a Helm values file carrying everything gathered
#               (+ generated dev secrets, preserved across re-runs) and emit
#               the exact setup-experiment-helm.sh invocation.
#
# Idempotency: every step probes current state and creates only what is
# missing; re-running converges (mirrors setup-experiment.sh / repo_init).
# Resource names and the region are operator-supplied — there are NO
# fictional defaults; a missing required flag fails loud naming the flag.
#
# --dry-run prints every mutating command verbatim (DRY-RUN: ...) instead of
# executing it. State probes are read-only AWS calls and still run (so the
# printed plan reflects reality); they require read-only credentials. For
# offline testing, EDEN_SETUP_AWS_MOCK names a file sourced after the probe
# definitions that may override any probe_* function (used by
# test-setup-aws.sh; not an operator surface).
#
# Bash-3.2-clean per AGENTS.md (no mapfile, no associative arrays).

usage() {
    cat <<'EOF' >&2
Usage:
  setup-aws.sh --cluster-name <name> --region <region>
               --ecr-repo <name> --s3-bucket <name>
               (--db-instance-id <id> | --postgres-dsn <url>)
               [--node-type <ec2-type>] [--nodes <N>]
               [--db-instance-class <class>] [--db-allocated-storage <GiB>]
               [--db-name <name>] [--db-master-username <user>]
               [--image-tag <tag>] [--image-platform <os/arch>]
               [--irsa-role-name <name>] [--irsa-policy-name <name>]
               [--namespace <ns>] [--release <name>]
               [--values-out <path>] [--experiment-config <path>]
               [--dry-run]

Required (no fictional defaults — provisioning costs money, so every
resource name is operator-chosen):
  --cluster-name      EKS cluster to verify or create.
  --region            AWS region (or env AWS_REGION).
  --ecr-repo          ECR repository name for the eden-reference image.
  --s3-bucket         S3 bucket for the blob.backend=s3 artifact store.
  --db-instance-id    RDS instance identifier to verify or create
                      (omit and pass --postgres-dsn to reuse an existing
                      Postgres instead; the two are mutually exclusive).

Required only when the named resource must be CREATED this run:
  --node-type, --nodes              EKS managed-nodegroup shape.
  --db-instance-class               RDS instance class (e.g. db.t4g.small).

Optional:
  --db-allocated-storage  RDS storage in GiB (default 20, the RDS minimum).
  --db-name               Database name (default eden, matching the chart).
  --db-master-username    Master username (default eden).
  --image-tag             Image tag (default: short git SHA of HEAD).
  --image-platform        Passed to docker build --platform. Set
                          linux/amd64 when building on Apple Silicon for
                          x86 nodes (or linux/arm64 for Graviton nodes).
  --irsa-role-name        IAM role name (default <cluster-name>-eden-blob-irsa).
  --irsa-policy-name      IAM policy name (default <s3-bucket>-eden-blob-rw).
  --namespace, --release  Target of the eventual helm install (default
                          eden/eden, mirroring setup-experiment-helm.sh);
                          baked into the IRSA trust policy's ServiceAccount
                          subject, so they must match the handoff invocation.
  --values-out            Where to write the generated Helm values file
                          (default ./eden-aws-values.yaml). Contains secrets;
                          written 0600. Secrets are preserved on re-run.
  --experiment-config     Forwarded verbatim into the emitted
                          setup-experiment-helm.sh invocation.
  --dry-run               Print every mutating command instead of running it.
EOF
}

log()  { echo "--- $* ---" >&2; }
die()  { echo "setup-aws.sh: $*" >&2; exit 2; }

# --- Parse args ---
CLUSTER_NAME=""
REGION="${AWS_REGION:-}"
ECR_REPO=""
S3_BUCKET=""
DB_INSTANCE_ID=""
POSTGRES_DSN=""
NODE_TYPE=""
NODES=""
DB_INSTANCE_CLASS=""
DB_ALLOCATED_STORAGE="20"
DB_NAME="eden"
DB_MASTER_USERNAME="eden"
IMAGE_TAG=""
IMAGE_PLATFORM=""
IRSA_ROLE_NAME=""
IRSA_POLICY_NAME=""
NAMESPACE="eden"
RELEASE="eden"
VALUES_OUT="./eden-aws-values.yaml"
EXPERIMENT_CONFIG=""
DRY_RUN=""

require_value() {
    local flag="$1" remaining="$2"
    if [[ "$remaining" -lt 2 ]]; then
        echo "$flag requires a value" >&2
        usage
        exit 2
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cluster-name)         require_value "$1" "$#"; CLUSTER_NAME="$2";         shift 2 ;;
        --region)               require_value "$1" "$#"; REGION="$2";               shift 2 ;;
        --ecr-repo)             require_value "$1" "$#"; ECR_REPO="$2";             shift 2 ;;
        --s3-bucket)            require_value "$1" "$#"; S3_BUCKET="$2";            shift 2 ;;
        --db-instance-id)       require_value "$1" "$#"; DB_INSTANCE_ID="$2";       shift 2 ;;
        --postgres-dsn)         require_value "$1" "$#"; POSTGRES_DSN="$2";         shift 2 ;;
        --node-type)            require_value "$1" "$#"; NODE_TYPE="$2";            shift 2 ;;
        --nodes)                require_value "$1" "$#"; NODES="$2";                shift 2 ;;
        --db-instance-class)    require_value "$1" "$#"; DB_INSTANCE_CLASS="$2";    shift 2 ;;
        --db-allocated-storage) require_value "$1" "$#"; DB_ALLOCATED_STORAGE="$2"; shift 2 ;;
        --db-name)              require_value "$1" "$#"; DB_NAME="$2";              shift 2 ;;
        --db-master-username)   require_value "$1" "$#"; DB_MASTER_USERNAME="$2";   shift 2 ;;
        --image-tag)            require_value "$1" "$#"; IMAGE_TAG="$2";            shift 2 ;;
        --image-platform)       require_value "$1" "$#"; IMAGE_PLATFORM="$2";       shift 2 ;;
        --irsa-role-name)       require_value "$1" "$#"; IRSA_ROLE_NAME="$2";       shift 2 ;;
        --irsa-policy-name)     require_value "$1" "$#"; IRSA_POLICY_NAME="$2";     shift 2 ;;
        --namespace)            require_value "$1" "$#"; NAMESPACE="$2";            shift 2 ;;
        --release)              require_value "$1" "$#"; RELEASE="$2";              shift 2 ;;
        --values-out)           require_value "$1" "$#"; VALUES_OUT="$2";           shift 2 ;;
        --experiment-config)    require_value "$1" "$#"; EXPERIMENT_CONFIG="$2";    shift 2 ;;
        --dry-run)              DRY_RUN="1";                                        shift ;;
        -h|--help)              usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

[[ -n "$CLUSTER_NAME" ]] || die "--cluster-name is required"
[[ -n "$REGION"       ]] || die "--region is required (or set AWS_REGION)"
[[ -n "$ECR_REPO"     ]] || die "--ecr-repo is required"
[[ -n "$S3_BUCKET"    ]] || die "--s3-bucket is required"
if [[ -n "$DB_INSTANCE_ID" && -n "$POSTGRES_DSN" ]]; then
    die "--db-instance-id and --postgres-dsn are mutually exclusive (the DSN means 'skip RDS provisioning')"
fi
if [[ -z "$DB_INSTANCE_ID" && -z "$POSTGRES_DSN" ]]; then
    die "one of --db-instance-id (provision RDS) or --postgres-dsn (reuse existing Postgres) is required"
fi

# Derived (not fictional) defaults: deterministic functions of operator input.
[[ -n "$IRSA_ROLE_NAME"   ]] || IRSA_ROLE_NAME="${CLUSTER_NAME}-eden-blob-irsa"
[[ -n "$IRSA_POLICY_NAME" ]] || IRSA_POLICY_NAME="${S3_BUCKET}-eden-blob-rw"

# --- Resolve paths ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

if [[ -z "$IMAGE_TAG" ]]; then
    IMAGE_TAG="$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD 2>/dev/null || true)"
    [[ -n "$IMAGE_TAG" ]] || die "--image-tag is required (could not derive one from git HEAD)"
fi

# Mirror the chart's eden.fullname helper EXACTLY (trunc 40 then trimSuffix
# of a SINGLE trailing hyphen) — the IRSA trust policy must name the
# ServiceAccount the chart will actually render.
CHART_NAME="eden"
if [[ "$RELEASE" == *"$CHART_NAME"* ]]; then
    FULLNAME="$RELEASE"
else
    FULLNAME="${RELEASE}-${CHART_NAME}"
fi
FULLNAME="$(printf '%s' "$FULLNAME" | cut -c1-40 | sed 's/-$//')"
SERVICE_ACCOUNT="${FULLNAME}-task-store-server"

# --- Tooling preflight ---
# python3 always (DSN encoding); aws always unless fully mocked. The
# mutating tools are needed only when we will actually execute mutations.
command -v python3 >/dev/null || die "requires 'python3' on PATH"
if [[ -z "${EDEN_SETUP_AWS_MOCK:-}" ]]; then
    command -v aws >/dev/null || die "requires the 'aws' CLI on PATH"
fi
if [[ -z "$DRY_RUN" ]]; then
    for tool in eksctl kubectl helm docker; do
        command -v "$tool" >/dev/null || die "requires '$tool' on PATH (only --dry-run works without it)"
    done
fi

# --- Mutation wrapper ---
render_cmd() {
    local out="" arg
    for arg in "$@"; do
        out+="$(printf '%q' "$arg") "
    done
    printf '%s' "${out% }"
}

run_mutate() {
    # Every state-changing command goes through here. In --dry-run the
    # command is printed VERBATIM (shell-quoted) and not executed.
    if [[ -n "$DRY_RUN" ]]; then
        echo "DRY-RUN: $(render_cmd "$@")"
    else
        echo "+ $(render_cmd "$@")" >&2
        "$@"
    fi
}

# --- State probes (read-only AWS calls; overridable via EDEN_SETUP_AWS_MOCK) ---
# Each probe either prints a value on stdout (empty/non-zero on absence) or
# uses its exit status as exists/absent. Probes are the ONLY place the
# script reads AWS state, so a mock file can simulate any account state.

probe_caller_account() {
    aws sts get-caller-identity --query Account --output text 2>/dev/null
}

probe_eks_cluster_status() {
    aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
        --query cluster.status --output text 2>/dev/null
}

probe_eks_oidc_issuer() {
    aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
        --query cluster.identity.oidc.issuer --output text 2>/dev/null
}

probe_eks_vpc_id() {
    aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
        --query cluster.resourcesVpcConfig.vpcId --output text 2>/dev/null
}

probe_eks_subnet_ids() {
    # Space-separated subnet ids.
    aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
        --query 'cluster.resourcesVpcConfig.subnetIds' --output text 2>/dev/null
}

probe_eks_cluster_sg() {
    aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
        --query cluster.resourcesVpcConfig.clusterSecurityGroupId --output text 2>/dev/null
}

probe_oidc_provider() {
    # probe_oidc_provider <issuer-host-path> — exists?
    aws iam list-open-id-connect-providers --output text 2>/dev/null \
        | grep -F -q "${1#https://}"
}

probe_eks_addon_status() {
    # probe_eks_addon_status <addon-name> — prints the addon status
    # (ACTIVE / CREATING / DEGRADED / CREATE_FAILED / …); fails if absent.
    aws eks describe-addon --cluster-name "$CLUSTER_NAME" --region "$REGION" \
        --addon-name "$1" --query addon.status --output text 2>/dev/null
}

probe_ecr_repo() {
    aws ecr describe-repositories --region "$REGION" \
        --repository-names "$ECR_REPO" >/dev/null 2>&1
}

probe_ecr_image() {
    # probe_ecr_image <tag> — image with this tag already pushed?
    aws ecr describe-images --region "$REGION" --repository-name "$ECR_REPO" \
        --image-ids "imageTag=$1" >/dev/null 2>&1
}

probe_rds_instance_status() {
    aws rds describe-db-instances --region "$REGION" \
        --db-instance-identifier "$DB_INSTANCE_ID" \
        --query 'DBInstances[0].DBInstanceStatus' --output text 2>/dev/null
}

probe_rds_endpoint() {
    # Prints host:port.
    aws rds describe-db-instances --region "$REGION" \
        --db-instance-identifier "$DB_INSTANCE_ID" \
        --query 'DBInstances[0].Endpoint.[Address,Port]' --output text 2>/dev/null \
        | tr '\t' ':'
}

probe_rds_master_secret_arn() {
    aws rds describe-db-instances --region "$REGION" \
        --db-instance-identifier "$DB_INSTANCE_ID" \
        --query 'DBInstances[0].MasterUserSecret.SecretArn' --output text 2>/dev/null
}

probe_rds_master_password() {
    # probe_rds_master_password <secret-arn> — read the AWS-managed master
    # password (read-only; the password never appears in any mutating call).
    aws secretsmanager get-secret-value --region "$REGION" --secret-id "$1" \
        --query SecretString --output text 2>/dev/null \
        | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["password"])'
}

probe_db_subnet_group() {
    # probe_db_subnet_group <name> — exists?
    aws rds describe-db-subnet-groups --region "$REGION" \
        --db-subnet-group-name "$1" >/dev/null 2>&1
}

probe_db_security_group_id() {
    # probe_db_security_group_id <group-name> <vpc-id> — prints sg id.
    aws ec2 describe-security-groups --region "$REGION" \
        --filters "Name=group-name,Values=$1" "Name=vpc-id,Values=$2" \
        --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null
}

probe_db_sg_ingress() {
    # probe_db_sg_ingress <sg-id> <source-sg-id> — tcp/5432 INGRESS rule
    # from the source SG present? (Egress / other-proto / partial-port-range
    # rules must not satisfy the probe.)
    local count
    count="$(aws ec2 describe-security-group-rules --region "$REGION" \
        --filters "Name=group-id,Values=$1" \
        --query "length(SecurityGroupRules[?IsEgress==\`false\` && IpProtocol=='tcp' && FromPort==\`5432\` && ToPort==\`5432\` && ReferencedGroupInfo.GroupId=='$2'])" \
        --output text 2>/dev/null)"
    [[ -n "$count" && "$count" != "0" ]]
}

probe_s3_bucket() {
    # 0 = exists and accessible; 1 = absent; 2 = exists but NOT ours /
    # inaccessible (bucket names are global — fail loud, don't adopt).
    local err
    if err="$(aws s3api head-bucket --bucket "$S3_BUCKET" --region "$REGION" 2>&1)"; then
        return 0
    fi
    if printf '%s' "$err" | grep -q '404'; then
        return 1
    fi
    return 2
}

probe_iam_policy() {
    # probe_iam_policy <policy-arn> — exists?
    aws iam get-policy --policy-arn "$1" >/dev/null 2>&1
}

probe_iam_policy_document() {
    # probe_iam_policy_document <policy-arn> — prints the policy's DEFAULT
    # version document (JSON) so the caller can verify it references the
    # expected bucket (an existing same-named policy may belong to another
    # deployment).
    local version
    version="$(aws iam get-policy --policy-arn "$1" \
        --query Policy.DefaultVersionId --output text 2>/dev/null)" || return 1
    aws iam get-policy-version --policy-arn "$1" --version-id "$version" \
        --query PolicyVersion.Document --output json 2>/dev/null
}

probe_iam_role_exists() {
    # probe_iam_role_exists <role-name> — exists?
    aws iam get-role --role-name "$1" >/dev/null 2>&1
}

probe_iam_role_trust() {
    # Prints the role's decoded assume-role policy document (JSON).
    aws iam get-role --role-name "$IRSA_ROLE_NAME" \
        --query 'Role.AssumeRolePolicyDocument' --output json 2>/dev/null
}

probe_role_policy_attached() {
    # probe_role_policy_attached <policy-arn> — attached to the IRSA role?
    aws iam list-attached-role-policies --role-name "$IRSA_ROLE_NAME" \
        --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null \
        | grep -F -q "$1"
}

# Test hook: override any probe above. NOT an operator surface.
if [[ -n "${EDEN_SETUP_AWS_MOCK:-}" ]]; then
    # shellcheck disable=SC1090
    . "$EDEN_SETUP_AWS_MOCK"
fi

# --- Resolve the AWS account (needed for ECR registry + IAM ARNs) ---
ACCOUNT_ID="$(probe_caller_account || true)"
if [[ -z "$ACCOUNT_ID" ]]; then
    die "could not resolve the AWS account (aws sts get-caller-identity failed).
Even --dry-run needs READ-ONLY credentials so the printed plan reflects
actual account state. Configure credentials (aws configure / AWS_PROFILE)."
fi
ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
IRSA_POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${IRSA_POLICY_NAME}"

# ======================================================================
# Step 1: EKS — verify-or-create, OIDC provider, EBS CSI addon, kubeconfig
# ======================================================================
log "step 1/5: EKS cluster '${CLUSTER_NAME}' (${REGION})"

CLUSTER_STATUS="$(probe_eks_cluster_status || true)"
CLUSTER_EXISTS=""
CLUSTER_CREATED_THIS_RUN=""
case "$CLUSTER_STATUS" in
    ACTIVE)
        log "EKS cluster exists and is ACTIVE — skipping create"
        CLUSTER_EXISTS="1"
        ;;
    CREATING|UPDATING)
        # An earlier (possibly interrupted) create is still in flight —
        # converge by waiting for it rather than failing or duplicating.
        log "EKS cluster is ${CLUSTER_STATUS} — waiting for it to become ACTIVE"
        run_mutate aws eks wait cluster-active --name "$CLUSTER_NAME" --region "$REGION"
        if [[ -z "$DRY_RUN" ]]; then
            CLUSTER_EXISTS="1"
        fi
        ;;
    ""|None)
        # Creation needed → the node shape becomes required NOW (fail loud
        # before any mutation rather than mid-create).
        [[ -n "$NODE_TYPE" ]] || die "--node-type is required to CREATE cluster '${CLUSTER_NAME}' (it does not exist)"
        [[ -n "$NODES"     ]] || die "--nodes is required to CREATE cluster '${CLUSTER_NAME}' (it does not exist)"
        run_mutate eksctl create cluster \
            --name "$CLUSTER_NAME" \
            --region "$REGION" \
            --node-type "$NODE_TYPE" \
            --nodes "$NODES" \
            --managed \
            --with-oidc
        CLUSTER_CREATED_THIS_RUN="1"
        if [[ -z "$DRY_RUN" ]]; then
            CLUSTER_EXISTS="1"
        fi
        ;;
    *)
        die "EKS cluster '${CLUSTER_NAME}' exists but is in state '${CLUSTER_STATUS}' — resolve that first (a converging re-run only handles ACTIVE or absent)"
        ;;
esac

# Cluster-derived facts. Only unknown in --dry-run when the cluster does
# not exist yet (in a real run the eksctl create above is synchronous).
if [[ -n "$CLUSTER_EXISTS" ]]; then
    OIDC_ISSUER="$(probe_eks_oidc_issuer)"
    CLUSTER_VPC_ID="$(probe_eks_vpc_id)"
    CLUSTER_SUBNET_IDS="$(probe_eks_subnet_ids)"
    CLUSTER_SG="$(probe_eks_cluster_sg)"
else
    OIDC_ISSUER="https://oidc.eks.${REGION}.amazonaws.com/id/<pending-cluster-create>"
    CLUSTER_VPC_ID="<pending-cluster-create>"
    CLUSTER_SUBNET_IDS="<pending-cluster-create>"
    CLUSTER_SG="<pending-cluster-create>"
fi
OIDC_HOSTPATH="${OIDC_ISSUER#https://}"

# IAM OIDC provider (IRSA prerequisite). `eksctl create cluster --with-oidc`
# covers the fresh-create path; this covers pre-existing clusters.
if [[ -n "$CLUSTER_CREATED_THIS_RUN" ]]; then
    log "OIDC provider associated by 'eksctl create cluster --with-oidc' — skipping"
elif probe_oidc_provider "$OIDC_ISSUER"; then
    log "IAM OIDC provider for the cluster already associated — skipping"
else
    run_mutate eksctl utils associate-iam-oidc-provider \
        --cluster "$CLUSTER_NAME" --region "$REGION" --approve
fi

# aws-ebs-csi-driver addon: on EKS >= 1.23 the in-tree EBS provisioner is
# gone, so without this addon every PVC the chart requests stays Pending.
EBS_CSI_ROLE_NAME="${CLUSTER_NAME}-ebs-csi-driver"
ADDON_STATUS="$(probe_eks_addon_status aws-ebs-csi-driver || true)"
if [[ "$ADDON_STATUS" == "ACTIVE" ]]; then
    log "aws-ebs-csi-driver addon already ACTIVE — skipping"
elif [[ "$ADDON_STATUS" == "CREATING" || "$ADDON_STATUS" == "UPDATING" ]]; then
    log "aws-ebs-csi-driver addon is ${ADDON_STATUS} — waiting"
    run_mutate aws eks wait addon-active --cluster-name "$CLUSTER_NAME" \
        --region "$REGION" --addon-name aws-ebs-csi-driver
elif [[ -n "$ADDON_STATUS" && "$ADDON_STATUS" != "None" ]]; then
    # DEGRADED / CREATE_FAILED / DELETING etc. — installed-but-broken must
    # not be skip-converged as if healthy.
    die "aws-ebs-csi-driver addon exists but is in state '${ADDON_STATUS}' — inspect it ('aws eks describe-addon --cluster-name ${CLUSTER_NAME} --addon-name aws-ebs-csi-driver') and resolve before re-running"
else
    # Partial-state convergence: a prior run may have created the role but
    # died before the addon (eksctl's iamserviceaccount create is not
    # re-runnable against its own leftovers).
    if probe_iam_role_exists "$EBS_CSI_ROLE_NAME"; then
        log "EBS CSI driver IAM role '${EBS_CSI_ROLE_NAME}' exists — skipping role create"
    else
        run_mutate eksctl create iamserviceaccount \
            --cluster "$CLUSTER_NAME" --region "$REGION" \
            --namespace kube-system --name ebs-csi-controller-sa \
            --role-name "$EBS_CSI_ROLE_NAME" --role-only \
            --attach-policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy \
            --approve
    fi
    run_mutate eksctl create addon \
        --cluster "$CLUSTER_NAME" --region "$REGION" \
        --name aws-ebs-csi-driver \
        --service-account-role-arn "arn:aws:iam::${ACCOUNT_ID}:role/${EBS_CSI_ROLE_NAME}" \
        --force
fi

# Point kubectl at the cluster (local kubeconfig write; idempotent), then
# verify the context actually reaches it.
run_mutate aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$REGION"
if [[ -z "$DRY_RUN" ]]; then
    kubectl get nodes -o name >/dev/null \
        || die "kubectl cannot reach cluster '${CLUSTER_NAME}' after update-kubeconfig"
    if ! kubectl get storageclass 2>/dev/null | grep -q '(default)'; then
        echo "warning: no default StorageClass found — the chart needs one" >&2
        echo "         that provisions ReadWriteOnce PVCs (gp2 is usually" >&2
        echo "         default on eksctl clusters)." >&2
    fi
fi

# ======================================================================
# Step 2: ECR — create-if-absent repo; build + push the reference image
# ======================================================================
log "step 2/5: ECR repository '${ECR_REPO}'"

if probe_ecr_repo; then
    log "ECR repository exists — skipping create"
else
    run_mutate aws ecr create-repository --region "$REGION" \
        --repository-name "$ECR_REPO"
fi

IMAGE_REF="${ECR_REGISTRY}/${ECR_REPO}:${IMAGE_TAG}"
if probe_ecr_image "$IMAGE_TAG"; then
    log "image tag '${IMAGE_TAG}' already pushed — skipping build + push"
else
    # docker login reads the ECR password from a pipe; render the pipeline
    # verbatim in --dry-run (run_mutate can't carry a pipe).
    if [[ -n "$DRY_RUN" ]]; then
        echo "DRY-RUN: aws ecr get-login-password --region ${REGION} | docker login --username AWS --password-stdin ${ECR_REGISTRY}"
    else
        echo "+ aws ecr get-login-password --region ${REGION} | docker login --username AWS --password-stdin ${ECR_REGISTRY}" >&2
        aws ecr get-login-password --region "$REGION" \
            | docker login --username AWS --password-stdin "$ECR_REGISTRY"
    fi
    BUILD_PLATFORM_ARGS=()
    if [[ -n "$IMAGE_PLATFORM" ]]; then
        BUILD_PLATFORM_ARGS=(--platform "$IMAGE_PLATFORM")
    fi
    run_mutate docker build \
        ${BUILD_PLATFORM_ARGS[@]+"${BUILD_PLATFORM_ARGS[@]}"} \
        -t "$IMAGE_REF" \
        -f "${REPO_ROOT}/reference/compose/Dockerfile" \
        "$REPO_ROOT"
    run_mutate docker push "$IMAGE_REF"
fi
log "image: ${IMAGE_REF}"

# ======================================================================
# Step 3: RDS Postgres — create-if-absent (or accept --postgres-dsn)
# ======================================================================
if [[ -n "$POSTGRES_DSN" ]]; then
    log "step 3/5: RDS — skipped (--postgres-dsn supplied)"
else
    log "step 3/5: RDS instance '${DB_INSTANCE_ID}'"

    RDS_STATUS="$(probe_rds_instance_status || true)"
    case "$RDS_STATUS" in
        available)
            log "RDS instance exists and is available — skipping create"
            ;;
        ""|None)
            [[ -n "$DB_INSTANCE_CLASS" ]] || die "--db-instance-class is required to CREATE RDS instance '${DB_INSTANCE_ID}' (it does not exist)"

            # Network plumbing so pods can actually reach the instance:
            # a DB subnet group over the cluster's subnets + a security
            # group in the cluster VPC admitting 5432 from the cluster SG.
            DB_SUBNET_GROUP="${DB_INSTANCE_ID}-subnets"
            if probe_db_subnet_group "$DB_SUBNET_GROUP"; then
                log "DB subnet group '${DB_SUBNET_GROUP}' exists — skipping create"
            else
                SUBNET_ID_ARGS=()
                for subnet in $CLUSTER_SUBNET_IDS; do
                    SUBNET_ID_ARGS+=("$subnet")
                done
                run_mutate aws rds create-db-subnet-group --region "$REGION" \
                    --db-subnet-group-name "$DB_SUBNET_GROUP" \
                    --db-subnet-group-description "EDEN ${DB_INSTANCE_ID} (cluster ${CLUSTER_NAME} subnets)" \
                    --subnet-ids ${SUBNET_ID_ARGS[@]+"${SUBNET_ID_ARGS[@]}"}
            fi

            DB_SG_NAME="${DB_INSTANCE_ID}-sg"
            DB_SG_ID="$(probe_db_security_group_id "$DB_SG_NAME" "$CLUSTER_VPC_ID" || true)"
            if [[ -n "$DB_SG_ID" && "$DB_SG_ID" != "None" ]]; then
                log "DB security group '${DB_SG_NAME}' exists (${DB_SG_ID}) — skipping create"
            else
                run_mutate aws ec2 create-security-group --region "$REGION" \
                    --group-name "$DB_SG_NAME" \
                    --description "EDEN ${DB_INSTANCE_ID}: Postgres from EKS cluster ${CLUSTER_NAME}" \
                    --vpc-id "$CLUSTER_VPC_ID"
                if [[ -n "$DRY_RUN" ]]; then
                    DB_SG_ID="<pending-security-group-create>"
                else
                    DB_SG_ID="$(probe_db_security_group_id "$DB_SG_NAME" "$CLUSTER_VPC_ID")"
                fi
            fi
            if probe_db_sg_ingress "$DB_SG_ID" "$CLUSTER_SG"; then
                log "5432 ingress from the cluster SG already authorized — skipping"
            else
                run_mutate aws ec2 authorize-security-group-ingress --region "$REGION" \
                    --group-id "$DB_SG_ID" \
                    --protocol tcp --port 5432 \
                    --source-group "$CLUSTER_SG"
            fi

            # --manage-master-user-password: AWS generates and stores the
            # master password in Secrets Manager. Re-runs READ it back from
            # there (probe_rds_master_password) instead of needing to have
            # remembered it — that's what makes the DSN emission converge.
            run_mutate aws rds create-db-instance --region "$REGION" \
                --db-instance-identifier "$DB_INSTANCE_ID" \
                --engine postgres \
                --db-instance-class "$DB_INSTANCE_CLASS" \
                --allocated-storage "$DB_ALLOCATED_STORAGE" \
                --db-name "$DB_NAME" \
                --master-username "$DB_MASTER_USERNAME" \
                --manage-master-user-password \
                --db-subnet-group-name "$DB_SUBNET_GROUP" \
                --vpc-security-group-ids "$DB_SG_ID" \
                --no-publicly-accessible
            run_mutate aws rds wait db-instance-available --region "$REGION" \
                --db-instance-identifier "$DB_INSTANCE_ID"
            ;;
        creating|backing-up|modifying|configuring-enhanced-monitoring)
            log "RDS instance is '${RDS_STATUS}' — waiting for it to become available"
            run_mutate aws rds wait db-instance-available --region "$REGION" \
                --db-instance-identifier "$DB_INSTANCE_ID"
            ;;
        *)
            die "RDS instance '${DB_INSTANCE_ID}' exists but is in state '${RDS_STATUS}' — resolve that first"
            ;;
    esac

    # Compose the DSN from live state. Unknowable only in --dry-run while
    # the instance has not been created yet.
    RDS_ENDPOINT="$(probe_rds_endpoint || true)"
    if [[ -z "$RDS_ENDPOINT" || "$RDS_ENDPOINT" == "None" || "$RDS_ENDPOINT" == "None:None" ]]; then
        if [[ -n "$DRY_RUN" ]]; then
            RDS_ENDPOINT="<rds-endpoint>:5432"
            RDS_PASSWORD="<rds-master-password>"
        else
            die "could not read the RDS endpoint for '${DB_INSTANCE_ID}'"
        fi
    else
        SECRET_ARN="$(probe_rds_master_secret_arn || true)"
        if [[ -z "$SECRET_ARN" || "$SECRET_ARN" == "None" ]]; then
            die "RDS instance '${DB_INSTANCE_ID}' has no AWS-managed master-user
secret (it was created without --manage-master-user-password), so its
password cannot be read back. Pass the DSN explicitly via --postgres-dsn."
        fi
        RDS_PASSWORD="$(probe_rds_master_password "$SECRET_ARN")"
        [[ -n "$RDS_PASSWORD" ]] || die "could not read the master password from Secrets Manager (${SECRET_ARN})"
    fi

    # Percent-encode the password (it is AWS-generated and may contain
    # URI-reserved characters). sslmode=require: RDS enforces TLS by
    # default on modern Postgres (rds.force_ssl=1); for verify-full + CA
    # bundle see docs/deployment/migrating-to-managed-postgres.md.
    RDS_PASSWORD_ENC="$(python3 -c 'import sys, urllib.parse; sys.stdout.write(urllib.parse.quote(sys.argv[1], safe=""))' "$RDS_PASSWORD")"
    POSTGRES_DSN="postgresql://${DB_MASTER_USERNAME}:${RDS_PASSWORD_ENC}@${RDS_ENDPOINT}/${DB_NAME}?sslmode=require"
fi

# ======================================================================
# Step 4: S3 bucket + IRSA role for blob.backend=s3
# ======================================================================
log "step 4/5: S3 bucket '${S3_BUCKET}' + IRSA role '${IRSA_ROLE_NAME}'"

set +e
probe_s3_bucket
S3_PROBE_RC=$?
set -e
case "$S3_PROBE_RC" in
    0)
        log "S3 bucket exists and is accessible — skipping create"
        ;;
    1)
        if [[ "$REGION" == "us-east-1" ]]; then
            run_mutate aws s3api create-bucket --region "$REGION" \
                --bucket "$S3_BUCKET"
        else
            run_mutate aws s3api create-bucket --region "$REGION" \
                --bucket "$S3_BUCKET" \
                --create-bucket-configuration "LocationConstraint=${REGION}"
        fi
        ;;
    *)
        die "S3 bucket '${S3_BUCKET}' exists but is not accessible with these
credentials (bucket names are GLOBAL — it likely belongs to another
account). Pick a different --s3-bucket."
        ;;
esac

# IAM policy: object read/write + list on exactly this bucket. An existing
# same-named policy is accepted only if its document actually GRANTS those
# three permissions via plain Allow statements (semantic check, not a
# substring scan — a Deny that merely mentions the ARN must not pass).
policy_document_matches() {
    probe_iam_policy_document "$IRSA_POLICY_ARN" | python3 -c '
import json, sys

bucket = sys.argv[1]
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(1)

def aslist(v):
    return v if isinstance(v, list) else [v]

needed = {
    ("s3:GetObject", "arn:aws:s3:::%s/*" % bucket),
    ("s3:PutObject", "arn:aws:s3:::%s/*" % bucket),
    ("s3:ListBucket", "arn:aws:s3:::%s" % bucket),
}
granted = set()
for s in aslist(doc.get("Statement") or []):
    if s.get("Effect") != "Allow" or "NotAction" in s or "NotResource" in s:
        continue
    for action in aslist(s.get("Action") or []):
        for resource in aslist(s.get("Resource") or []):
            granted.add((action, resource))
sys.exit(0 if needed <= granted else 1)
' "$S3_BUCKET"
}

if probe_iam_policy "$IRSA_POLICY_ARN"; then
    if policy_document_matches; then
        log "IAM policy '${IRSA_POLICY_NAME}' exists and grants the bucket access — skipping create"
    else
        die "IAM policy '${IRSA_POLICY_NAME}' exists but does not reference
s3://${S3_BUCKET} with the expected grants (GetObject/PutObject on the
objects + ListBucket on the bucket) — it likely belongs to another
deployment. Pass a different --irsa-policy-name, or update that policy
manually."
    fi
else
    POLICY_DOC="$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::${S3_BUCKET}/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::${S3_BUCKET}"
    }
  ]
}
EOF
)"
    run_mutate aws iam create-policy \
        --policy-name "$IRSA_POLICY_NAME" \
        --policy-document "$POLICY_DOC"
fi

# IAM role trusted by the cluster's OIDC provider for EXACTLY the chart's
# task-store-server ServiceAccount (the only pod that touches the blob
# backend). Trust drift (e.g. the role pre-exists for a different
# namespace/release) is converged via update-assume-role-policy.
TRUST_SUB="system:serviceaccount:${NAMESPACE}:${SERVICE_ACCOUNT}"
TRUST_POLICY="$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_HOSTPATH}"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "${OIDC_HOSTPATH}:sub": "${TRUST_SUB}",
          "${OIDC_HOSTPATH}:aud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
EOF
)"
# Semantic trust comparison (not substring): exactly one Allow statement
# for sts:AssumeRoleWithWebIdentity, federated to THIS cluster's OIDC
# provider, with both the :sub and :aud conditions. Extra principals /
# statements / a missing aud all count as drift and are converged.
trust_policy_matches() {
    probe_iam_role_trust | python3 -c '
import json, sys

sub, host, fed = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(1)
stmts = doc.get("Statement") or []
if len(stmts) != 1:
    sys.exit(1)
s = stmts[0]
cond = (s.get("Condition") or {}).get("StringEquals") or {}
ok = (
    s.get("Effect") == "Allow"
    and s.get("Action") == "sts:AssumeRoleWithWebIdentity"
    and (s.get("Principal") or {}).get("Federated") == fed
    and cond.get(host + ":sub") == sub
    and cond.get(host + ":aud") == "sts.amazonaws.com"
)
sys.exit(0 if ok else 1)
' "$TRUST_SUB" "$OIDC_HOSTPATH" "arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_HOSTPATH}"
}

if probe_iam_role_exists "$IRSA_ROLE_NAME"; then
    if trust_policy_matches; then
        log "IAM role '${IRSA_ROLE_NAME}' exists with the expected trust policy — skipping"
    else
        log "IAM role '${IRSA_ROLE_NAME}' exists but its trust policy has drifted — converging"
        run_mutate aws iam update-assume-role-policy \
            --role-name "$IRSA_ROLE_NAME" \
            --policy-document "$TRUST_POLICY"
    fi
else
    run_mutate aws iam create-role \
        --role-name "$IRSA_ROLE_NAME" \
        --assume-role-policy-document "$TRUST_POLICY" \
        --description "EDEN blob.backend=s3 IRSA for ${TRUST_SUB} on ${CLUSTER_NAME}"
fi

if probe_role_policy_attached "$IRSA_POLICY_ARN"; then
    log "policy already attached to the IRSA role — skipping"
else
    run_mutate aws iam attach-role-policy \
        --role-name "$IRSA_ROLE_NAME" \
        --policy-arn "$IRSA_POLICY_ARN"
fi
IRSA_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${IRSA_ROLE_NAME}"

# ======================================================================
# Step 5: values file + handoff to setup-experiment-helm.sh
# ======================================================================
log "step 5/5: Helm values + handoff"

# Dev secrets, generate-or-preserve (the setup-experiment.sh .env idiom,
# applied to the values file): setup-experiment-helm.sh does NOT generate
# secrets when given --values, so the file must carry them — and a re-run
# must NOT rotate them (rotating the Postgres-adjacent secrets against
# retained PVCs is the documented reinstall hazard in docs/deployment/helm.md §5).
gen_hex() {
    python3 -c 'import secrets,sys; sys.stdout.write(secrets.token_hex(int(sys.argv[1])))' "${1:-32}"
}

read_values_secret() {
    # read_values_secret <key> — read `  <key>: "<value>"` back out of an
    # existing values file (our own fixed format; absent → empty).
    if [[ -f "$VALUES_OUT" ]]; then
        sed -n "s/^  $1: \"\(.*\)\"\$/\1/p" "$VALUES_OUT" | head -n1
    fi
}

resolve_secret() {
    # resolve_secret <values-key> — sets RESOLVED_VALUE (existing-or-fresh)
    # and RESOLVED_DISPLAY (<preserved>/<generated>, used by the dry-run
    # preview so live secret material never reaches stdout).
    local existing
    existing="$(read_values_secret "$1")"
    if [[ -n "$existing" ]]; then
        RESOLVED_VALUE="$existing"
        RESOLVED_DISPLAY="<preserved>"
    else
        RESOLVED_VALUE="$(gen_hex 32)"
        RESOLVED_DISPLAY="<generated>"
    fi
}

resolve_secret adminToken
ADMIN_TOKEN="$RESOLVED_VALUE";             ADMIN_TOKEN_DISPLAY="$RESOLVED_DISPLAY"
resolve_secret sessionSecret
SESSION_SECRET="$RESOLVED_VALUE";          SESSION_SECRET_DISPLAY="$RESOLVED_DISPLAY"
resolve_secret postgresPassword
POSTGRES_PASSWORD="$RESOLVED_VALUE";       POSTGRES_PASSWORD_DISPLAY="$RESOLVED_DISPLAY"
resolve_secret readonlyPassword
READONLY_PASSWORD="$RESOLVED_VALUE";       READONLY_PASSWORD_DISPLAY="$RESOLVED_DISPLAY"
resolve_secret forgejoRemotePassword
FORGEJO_REMOTE_PASSWORD="$RESOLVED_VALUE"; FORGEJO_REMOTE_PASSWORD_DISPLAY="$RESOLVED_DISPLAY"
resolve_secret forgejoSecretKey
FORGEJO_SECRET_KEY="$RESOLVED_VALUE";      FORGEJO_SECRET_KEY_DISPLAY="$RESOLVED_DISPLAY"
resolve_secret forgejoInternalToken
FORGEJO_INTERNAL_TOKEN="$RESOLVED_VALUE";  FORGEJO_INTERNAL_TOKEN_DISPLAY="$RESOLVED_DISPLAY"

emit_values() {
    # emit_values real|display — the values-file content. "display" (the
    # --dry-run preview) swaps every secret for its <preserved>/<generated>
    # marker and masks the DSN userinfo: dry-run output lands in terminals
    # and logs, so live credentials must never appear in it.
    local dsn admin session pg ro fpw fkey ftok
    if [[ "$1" == "real" ]]; then
        dsn="$POSTGRES_DSN"
        admin="$ADMIN_TOKEN";            session="$SESSION_SECRET"
        pg="$POSTGRES_PASSWORD";         ro="$READONLY_PASSWORD"
        fpw="$FORGEJO_REMOTE_PASSWORD";  fkey="$FORGEJO_SECRET_KEY"
        ftok="$FORGEJO_INTERNAL_TOKEN"
    else
        dsn="${POSTGRES_DSN/:\/\/*@/://<redacted>@}"
        admin="$ADMIN_TOKEN_DISPLAY";            session="$SESSION_SECRET_DISPLAY"
        pg="$POSTGRES_PASSWORD_DISPLAY";         ro="$READONLY_PASSWORD_DISPLAY"
        fpw="$FORGEJO_REMOTE_PASSWORD_DISPLAY";  fkey="$FORGEJO_SECRET_KEY_DISPLAY"
        ftok="$FORGEJO_INTERNAL_TOKEN_DISPLAY"
    fi
    cat <<EOF
# Generated by reference/scripts/setup-aws/setup-aws.sh — CONTAINS SECRETS,
# keep private. Re-running setup-aws.sh preserves the secrets (read back
# from this file) and refreshes the provisioned-resource values.
image:
  repository: "${ECR_REGISTRY}/${ECR_REPO}"
  tag: "${IMAGE_TAG}"
postgres:
  mode: external
  external:
    connectionString: "${dsn}"
blob:
  backend: s3
  s3:
    bucket: "${S3_BUCKET}"
    region: "${REGION}"
    irsa:
      enabled: true
      roleArn: "${IRSA_ROLE_ARN}"
secrets:
  adminToken: "${admin}"
  sessionSecret: "${session}"
  postgresPassword: "${pg}"
  readonlyPassword: "${ro}"
  forgejoRemotePassword: "${fpw}"
  forgejoSecretKey: "${fkey}"
  forgejoInternalToken: "${ftok}"
EOF
}

if [[ -n "$DRY_RUN" ]]; then
    echo "DRY-RUN: would write ${VALUES_OUT} (mode 0600; secrets redacted in this preview):"
    emit_values display
else
    # Atomic write (tmp + rename in the destination dir): a crash mid-write
    # must not leave a partial file, or the next run's read-back would
    # silently regenerate the missing secrets — the documented
    # rotation-against-retained-PVCs hazard (docs/deployment/helm.md §5).
    VALUES_TMP="$(mktemp "${VALUES_OUT}.XXXXXX")"
    chmod 0600 "$VALUES_TMP"
    emit_values real > "$VALUES_TMP"
    mv "$VALUES_TMP" "$VALUES_OUT"
    log "wrote ${VALUES_OUT}"
fi

EXPERIMENT_CONFIG_LINE=""
if [[ -n "$EXPERIMENT_CONFIG" ]]; then
    EXPERIMENT_CONFIG_LINE=" \\
    --experiment-config $(render_cmd "$EXPERIMENT_CONFIG")"
fi

cat <<EOF
setup-aws complete$( [[ -n "$DRY_RUN" ]] && echo " (dry-run — nothing was created)" ).

  EKS cluster:       ${CLUSTER_NAME} (${REGION})
  image:             ${IMAGE_REF}
  postgres DSN:      (in ${VALUES_OUT} — postgres.mode=external)
  S3 bucket:         ${S3_BUCKET}
  IRSA role:         ${IRSA_ROLE_ARN}
  service account:   ${NAMESPACE}/${SERVICE_ACCOUNT}
  values file:       ${VALUES_OUT}

Next step (hand off to the experiment bootstrap; --namespace/--release
MUST match what was provisioned here — the IRSA trust policy names them):

  bash reference/scripts/setup-experiment-helm.sh \\
    --namespace $(render_cmd "$NAMESPACE") \\
    --release $(render_cmd "$RELEASE") \\
    --values $(render_cmd "$VALUES_OUT")${EXPERIMENT_CONFIG_LINE}

Re-running setup-aws.sh is safe: every step probes current state, creates
only what is missing, and preserves the generated secrets.
EOF
