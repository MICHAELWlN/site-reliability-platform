#!/bin/sh
# Runs inside the Jenkins container (installed as /usr/local/bin/jenkins-aws-clear).
# Removes any loaded AWS session credentials. Safe to call when none are loaded, so a
# future pipeline can call it from `post { always { ... } }`.
set -eu
dir=/run/aws-creds
rm -f "$dir/session.json" "$dir/.session.tmp" "$dir/.patterns"
echo "AWS session credentials cleared from the Jenkins container."
