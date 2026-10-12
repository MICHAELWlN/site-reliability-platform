#!/usr/bin/env bash
# Remove the AWS session credentials from the Jenkins container (run on the Mac host).
#
# This deletes the only copy Jenkins holds. It does not revoke the session at AWS: the
# credentials stay valid until they expire, so keep the session length short.
set -euo pipefail

export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
CONTAINER="${JENKINS_CONTAINER:-sre-jenkins}"

if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)" != "true" ]]; then
  echo "Container '$CONTAINER' is not running. Its credential tmpfs is already gone."
  exit 0
fi

docker exec "$CONTAINER" jenkins-aws-clear
if docker exec "$CONTAINER" test -e /run/aws-creds/session.json; then
  echo "error: credential file still present." >&2
  exit 1
fi
echo "Verified: no credential file in '$CONTAINER'."
