#!/usr/bin/env bash
# Safe test of the credential handoff (run on the Mac host).
#
# Part 1 (offline): proves the helper rejects missing, unsafe, expired and nearly expired
#   credentials, using clearly fake values in a throwaway directory. No AWS calls.
# Part 2 (live): runs `aws sts get-caller-identity` inside Jenkins and requires the
#   assumed role, then checks that the credentials do not appear in `docker inspect`,
#   process command lines or environments, or the Jenkins home. Only the (non-secret)
#   identity ARN is printed.
#
# Usage: jenkins/aws-creds-test.sh [--offline]
set -uo pipefail

export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
CONTAINER="${JENKINS_CONTAINER:-sre-jenkins}"
ROLE_NAME="${JENKINS_DEPLOY_ROLE_NAME:-sre-jenkins-deploy-role}"
OFFLINE=0
[[ "${1:-}" == "--offline" ]] && OFFLINE=1

fails=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }

[[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)" == "true" ]] \
  || { echo "error: container '$CONTAINER' is not running." >&2; exit 2; }

echo "== Part 1: offline rejection tests (fake values, no AWS calls)"
offline_output="$(docker exec -i "$CONTAINER" sh -s <<'EOF'
dir=$(mktemp -d); trap 'rm -rf "$dir"' EXIT
helper=/usr/local/bin/jenkins-aws-credential-process
export JENKINS_AWS_CREDENTIAL_FILE="$dir/session.json"
fake() { # $1 = expiration
  printf '{"AccessKeyId":"FAKEFAKEFAKE","SecretAccessKey":"FAKE","SessionToken":"FAKE","Expiration":"%s"}' "$1" > "$JENKINS_AWS_CREDENTIAL_FILE"
}
check() { # $1 = name, $2 = expected exit (0 or nonzero)
  "$helper" >/dev/null 2>"$dir/err"; rc=$?
  if { [ "$2" = ok ] && [ $rc -eq 0 ]; } || { [ "$2" = reject ] && [ $rc -ne 0 ]; }; then echo "PASS  $1"; else echo "FAIL  $1 (exit $rc)"; fi
}
check "missing file is rejected" reject
fake "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)"; chmod 600 "$JENKINS_AWS_CREDENTIAL_FILE"
check "expired credentials are rejected" reject
grep -q expired "$dir/err" && echo "PASS  rejection message says expired" || echo "FAIL  rejection message"
fake "$(date -u -d '+1 minute' +%Y-%m-%dT%H:%M:%SZ)"; chmod 600 "$JENKINS_AWS_CREDENTIAL_FILE"
check "credentials expiring in under 5 minutes are rejected" reject
fake "$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)"; chmod 644 "$JENKINS_AWS_CREDENTIAL_FILE"
check "group/world-readable file is rejected" reject
chmod 600 "$JENKINS_AWS_CREDENTIAL_FILE"
check "valid unexpired fake file is accepted by the helper" ok
printf 'not json' > "$JENKINS_AWS_CREDENTIAL_FILE"; chmod 600 "$JENKINS_AWS_CREDENTIAL_FILE"
check "malformed file is rejected" reject
# End to end through the AWS CLI with an expired fake file: fails locally, before any network call.
fake "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)"; chmod 600 "$JENKINS_AWS_CREDENTIAL_FILE"
if aws sts get-caller-identity >/dev/null 2>"$dir/err"; then echo "FAIL  AWS CLI accepted expired credentials"; else
  grep -q 'expired' "$dir/err" && echo "PASS  AWS CLI refuses expired credentials" || echo "FAIL  AWS CLI error unexpected"; fi
EOF
)"
echo "$offline_output"
grep -q '^FAIL' <<<"$offline_output" && fails=$((fails + 1))

if (( OFFLINE )); then
  echo "== Part 2 skipped (--offline)"
else
  echo "== Part 2: live identity check inside Jenkins"
  if ! docker exec "$CONTAINER" test -s /run/aws-creds/session.json; then
    fail "no credentials loaded. Run jenkins/aws-creds-handoff.sh first."
  else
    arn="$(docker exec "$CONTAINER" aws sts get-caller-identity --query Arn --output text 2>&1)" \
      || { fail "get-caller-identity failed: $arn"; arn=""; }
    if [[ -n "$arn" ]]; then
      echo "Identity: ${arn#arn:aws:sts::*:}"
      [[ "$arn" == *":assumed-role/${ROLE_NAME}/"* ]] \
        && pass "identity is assumed-role/${ROLE_NAME}" \
        || fail "identity is not assumed-role/${ROLE_NAME}"
    fi

    # Build a pattern file of the secret values inside the container (never on the host).
    docker exec "$CONTAINER" sh -c 'umask 077; python3 -c "
import json
d = json.load(open(\"/run/aws-creds/session.json\"))
print(d[\"AccessKeyId\"]); print(d[\"SecretAccessKey\"]); print(d[\"SessionToken\"])" > /run/aws-creds/.patterns'

    # Positive control: the same scan must find the values when they really are present.
    docker exec "$CONTAINER" cat /run/aws-creds/.patterns | docker exec -i "$CONTAINER" grep -qFf /run/aws-creds/.patterns \
      && pass "leak scan detects planted values (control)" || fail "leak scan control failed, results below are not trustworthy"

    if docker inspect "$CONTAINER" | docker exec -i "$CONTAINER" grep -qFf /run/aws-creds/.patterns; then
      fail "credentials found in docker inspect output"; else pass "credentials absent from docker inspect"; fi

    if docker exec "$CONTAINER" sh -c "cat /proc/[0-9]*/cmdline /proc/[0-9]*/environ 2>/dev/null | tr '\0' '\n' | grep -qFf /run/aws-creds/.patterns"; then
      fail "credentials found in a process command line or environment"; else pass "credentials absent from process command lines and environments"; fi

    if docker exec "$CONTAINER" grep -rqFf /run/aws-creds/.patterns /var/jenkins_home --exclude-dir=plugins --exclude-dir=war --exclude-dir=.cache 2>/dev/null; then
      fail "credentials found under /var/jenkins_home"; else pass "credentials absent from /var/jenkins_home (job configs, build logs)"; fi

    docker exec "$CONTAINER" rm -f /run/aws-creds/.patterns
    docker exec "$CONTAINER" grep -q ' /run/aws-creds tmpfs ' /proc/mounts \
      && pass "credential directory is a tmpfs" || fail "credential directory is not a tmpfs"
  fi
fi

echo
(( fails == 0 )) && echo "RESULT: all checks passed" || echo "RESULT: $fails check group(s) failed"
exit $(( fails == 0 ? 0 : 1 ))
