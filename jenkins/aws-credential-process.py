#!/usr/bin/env python3
"""AWS CLI `credential_process` helper for the local Jenkins lab.

Reads short-lived STS credentials from a protected file on an in-memory tmpfs and hands
them to the AWS CLI on stdout. It refuses to do so if:

- the file is missing, a symlink, not a regular file, or not owned by the current user,
- the file is readable by anyone other than its owner,
- the file is malformed, or
- the credentials are expired or about to expire.

Error messages never include credential values. Nothing is written to disk.
"""

import json
import os
import stat
import sys
from datetime import datetime, timezone

CREDENTIAL_FILE = os.environ.get("JENKINS_AWS_CREDENTIAL_FILE", "/run/aws-creds/session.json")
# Refuse credentials that would expire mid-deployment.
MIN_REMAINING_SECONDS = 300
REQUIRED = ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")


def fail(message):
    print(f"jenkins-aws-credential-process: {message}", file=sys.stderr)
    sys.exit(1)


def load_file():
    try:
        fd = os.open(CREDENTIAL_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        fail("no credentials loaded. Run jenkins/aws-creds-handoff.sh on the host.")
    except OSError as exc:
        fail(f"cannot open the credential file ({exc.strerror}).")

    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            fail("the credential path is not a regular file.")
        if info.st_uid != os.geteuid():
            fail("the credential file is not owned by the current user.")
        if info.st_mode & 0o077:
            fail("the credential file is accessible to other users (expected mode 0600).")
        try:
            return json.load(handle)
        except ValueError:
            fail("the credential file is not valid JSON.")


def main():
    data = load_file()
    if not isinstance(data, dict):
        fail("the credential file has an unexpected format.")
    if any(not isinstance(data.get(key), str) or not data[key] for key in REQUIRED):
        fail("the credential file is missing required fields.")

    try:
        expiration = datetime.fromisoformat(data["Expiration"])
    except ValueError:
        fail("the credential expiration time is not valid.")
    if expiration.tzinfo is None:
        fail("the credential expiration time has no time zone.")

    remaining = (expiration - datetime.now(timezone.utc)).total_seconds()
    when = expiration.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if remaining <= 0:
        fail(f"credentials expired at {when}. Run jenkins/aws-creds-handoff.sh on the host.")
    if remaining < MIN_REMAINING_SECONDS:
        fail(
            f"credentials expire at {when}, in under {MIN_REMAINING_SECONDS} seconds. "
            "Run jenkins/aws-creds-handoff.sh on the host."
        )

    json.dump(
        {
            "Version": 1,
            "AccessKeyId": data["AccessKeyId"],
            "SecretAccessKey": data["SecretAccessKey"],
            "SessionToken": data["SessionToken"],
            "Expiration": when,
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
