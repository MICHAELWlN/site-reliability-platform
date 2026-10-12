#!/usr/bin/env bash
# Hand short-lived AWS credentials to the local Jenkins container (run on the Mac host).
#
# Flow: aws sts assume-role (profile sre-lab) -> stdout pipe -> docker exec stdin -> tmpfs
# file in the container. The credentials are never written to the host disk, never placed
# in a command-line argument or environment variable, and never printed.
#
# Usage: jenkins/aws-creds-handoff.sh
# Optional environment variables:
#   AWS_HANDOFF_PROFILE         host AWS CLI profile (default: sre-lab)
#   JENKINS_DEPLOY_ROLE_NAME    role to assume (default: sre-jenkins-deploy-role)
#   JENKINS_AWS_SESSION_SECONDS session length, 900-3600 (default: 3600)
#   JENKINS_CONTAINER           Jenkins container name (default: sre-jenkins)
set -euo pipefail
umask 077

export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"

PROFILE="${AWS_HANDOFF_PROFILE:-sre-lab}"
ROLE_NAME="${JENKINS_DEPLOY_ROLE_NAME:-sre-jenkins-deploy-role}"
DURATION="${JENKINS_AWS_SESSION_SECONDS:-3600}"
CONTAINER="${JENKINS_CONTAINER:-sre-jenkins}"
REGION="us-east-2"
TARGET_DIR="/run/aws-creds"

die() { echo "error: $*" >&2; exit 1; }

[[ "$DURATION" =~ ^[0-9]+$ ]] && (( DURATION >= 900 && DURATION <= 3600 )) \
  || die "JENKINS_AWS_SESSION_SECONDS must be between 900 and 3600."
[[ "$ROLE_NAME" =~ ^[A-Za-z0-9+=,.@_-]+$ ]] || die "invalid role name."

command -v aws >/dev/null || die "aws CLI not found on the host."
command -v docker >/dev/null || die "docker CLI not found on the host."

# 1. The target must be a running Jenkins container whose credential directory is in-memory.
[[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)" == "true" ]] \
  || die "container '$CONTAINER' is not running."
docker exec "$CONTAINER" sh -c "grep -q ' $TARGET_DIR tmpfs ' /proc/mounts" \
  || die "$TARGET_DIR is not a tmpfs in '$CONTAINER'. Recreate the controller with the current docker-compose.yml."

# 2. Host login must be valid. The account ID is read from the profile, not stored in Git.
ACCOUNT="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text 2>/dev/null)" \
  || die "profile '$PROFILE' has no valid login. Run: aws login --profile $PROFILE"
[[ "$ACCOUNT" =~ ^[0-9]{12}$ ]] || die "unexpected account ID format from profile '$PROFILE'."
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}"
SESSION="jenkins-lab-$(date -u +%Y%m%dT%H%M%SZ)"

# 3. Assume the role and stream the result straight into the container. The role ARN and
#    session name are not secret. The credentials travel only through the pipe. The
#    container side refuses an empty payload so a failed assume-role never overwrites a
#    good file, and moves the file into place only after it is complete.
aws sts assume-role \
    --profile "$PROFILE" \
    --region "$REGION" \
    --role-arn "$ROLE_ARN" \
    --role-session-name "$SESSION" \
    --duration-seconds "$DURATION" \
    --query '{Version: `1`, AccessKeyId: Credentials.AccessKeyId, SecretAccessKey: Credentials.SecretAccessKey, SessionToken: Credentials.SessionToken, Expiration: Credentials.Expiration}' \
    --output json \
  | docker exec -i "$CONTAINER" sh -c '
      set -e
      umask 077
      tmp=/run/aws-creds/.session.tmp
      cat > "$tmp"
      [ -s "$tmp" ] || { rm -f "$tmp"; echo "empty credential payload" >&2; exit 1; }
      chmod 600 "$tmp"
      mv "$tmp" /run/aws-creds/session.json
    ' \
  || die "could not assume the role or load the credentials (nothing was loaded)."

# 4. Verify the identity from inside Jenkins. Anything else is removed immediately.
ARN="$(docker exec "$CONTAINER" aws sts get-caller-identity --query Arn --output text)" || {
  docker exec "$CONTAINER" jenkins-aws-clear >/dev/null || true
  die "credentials were loaded but get-caller-identity failed. They were removed."
}
if [[ "$ARN" != *":assumed-role/${ROLE_NAME}/"* ]]; then
  docker exec "$CONTAINER" jenkins-aws-clear >/dev/null || true
  die "unexpected identity. Credentials were removed."
fi

EXPIRES="$(docker exec "$CONTAINER" python3 -c \
  'import json;print(json.load(open("/run/aws-creds/session.json"))["Expiration"])')"
echo "Loaded credentials into '$CONTAINER'."
echo "Identity: assumed-role/${ROLE_NAME} (session $SESSION)"
echo "Expires:  $EXPIRES"
echo "Remove them when finished: jenkins/aws-creds-clear.sh"
