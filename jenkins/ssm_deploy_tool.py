#!/usr/bin/env python3
"""Decide how Jenkins invokes SREPlatformDeploy. No AWS calls.

The pipeline supplies a verified image digest and the JSON from
get-command-invocation. This module checks the digest, builds the
SendCommand request, and decides whether to keep waiting, succeed, or fail.
"""

import argparse
import json
import re
import sys

DOCUMENT_NAME = "SREPlatformDeploy"
DOCUMENT_VERSION = "1"
INSTANCE_ID = "i-0de56d01b124c0e69"
ROLE_MARKER = ":assumed-role/sre-jenkins-deploy-role/"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMAND_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
PENDING = {"Pending", "InProgress", "Delayed"}
TIMEOUT_SECONDS = 540
EXIT_WAIT = 10


class DeployCheckError(Exception):
    pass


def validate_digest(digest):
    if not DIGEST_RE.fullmatch(digest or ""):
        raise DeployCheckError("image digest must be sha256 and 64 lowercase hex characters")


def validate_command_id(command_id):
    if not COMMAND_ID_RE.fullmatch(command_id or ""):
        raise DeployCheckError("SSM did not return a command id")


def check_identity(arn):
    if not arn or ROLE_MARKER not in arn:
        raise DeployCheckError("AWS identity is not sre-jenkins-deploy-role")


def auth_failure_message(text):
    """A short reason that never repeats the AWS error body."""
    lowered = (text or "").lower()
    if not lowered.strip() or "unable to locate credentials" in lowered or "could not find credentials" in lowered:
        return "AWS credentials are not available."
    if "expired" in lowered:
        return "AWS credentials are expired."
    if "accessdenied" in lowered or "not authorized" in lowered:
        return "AWS credentials were rejected."
    if "invocationdoesnotexist" in lowered:
        return "SSM command is not visible yet."
    return "AWS request failed."


def retryable_invocation_error(text):
    return "invocationdoesnotexist" in (text or "").lower()


def build_send_command(digest):
    validate_digest(digest)
    payload = {
        "DocumentName": DOCUMENT_NAME,
        "DocumentVersion": DOCUMENT_VERSION,
        "InstanceIds": [INSTANCE_ID],
        "Parameters": {"imageDigest": [digest]},
    }
    if "AWS-RunShellScript" in json.dumps(payload):
        raise DeployCheckError("refusing to invoke AWS-RunShellScript")
    if list(payload["Parameters"]) != ["imageDigest"]:
        raise DeployCheckError("SSM parameters are not limited to the image digest")
    if payload["InstanceIds"] != [INSTANCE_ID]:
        raise DeployCheckError("SSM target is not the designated instance")
    return payload


def parse_events(stdout):
    events = []
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DeployCheckError("deployment output is not JSON") from exc
        if not isinstance(event, dict) or "event" not in event:
            raise DeployCheckError("deployment output is missing an event")
        events.append(event)
    return events


def output_succeeded(stdout, digest):
    """True when the host script finished on this digest and Nginx answered.

    A cutover logs public_health result ok, then success. A repeated deploy
    logs noop. The installed script emits noop only after HTTP 200 from
    Nginx on port 80, which is the public /health path on the instance.
    """
    validate_digest(digest)
    events = parse_events(stdout)
    if any(event.get("event") == "error" for event in events):
        return False
    starts = [event for event in events if event.get("event") == "start"]
    if len(starts) != 1 or starts[0].get("digest") != digest:
        return False
    finals = [event for event in events if event.get("event") in ("success", "noop")]
    if len(finals) != 1 or finals[0].get("digest") != digest:
        return False
    health = [event for event in events if event.get("event") == "public_health"]
    if any(event.get("result") != "ok" for event in health):
        return False
    if finals[0]["event"] == "success" and not health:
        return False
    return True


def judge(invocation, digest, elapsed):
    """Return success, wait, timeout, or failed.

    elapsed is seconds since SendCommand. A command still running at
    TIMEOUT_SECONDS is a timeout even if SSM has not marked it TimedOut.
    """
    validate_digest(digest)
    if not isinstance(invocation, dict):
        raise DeployCheckError("invocation result is not an object")
    status = str(invocation.get("Status") or "")
    if status in PENDING or status == "":
        if elapsed >= TIMEOUT_SECONDS:
            return "timeout"
        return "wait"
    if status == "TimedOut":
        return "timeout"
    if status != "Success":
        return "failed"
    code = invocation.get("ResponseCode")
    if code not in (0, "0"):
        return "failed"
    if not output_succeeded(invocation.get("StandardOutputContent") or "", digest):
        return "failed"
    return "success"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check an SREPlatformDeploy invocation.")
    sub = parser.add_subparsers(dest="command", required=True)

    identity = sub.add_parser("check-identity")
    identity.add_argument("arn")

    digest_cmd = sub.add_parser("validate-digest")
    digest_cmd.add_argument("digest")

    command_id = sub.add_parser("validate-command-id")
    command_id.add_argument("command_id")

    send = sub.add_parser("write-send")
    send.add_argument("path")
    send.add_argument("digest")

    auth = sub.add_parser("explain-auth-failure")
    retry = sub.add_parser("retryable-error")

    classify = sub.add_parser("classify")
    classify.add_argument("path")
    classify.add_argument("--digest", required=True)
    classify.add_argument("--elapsed", type=int, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "check-identity":
            check_identity(args.arn)
            return 0
        if args.command == "validate-digest":
            validate_digest(args.digest)
            return 0
        if args.command == "validate-command-id":
            validate_command_id(args.command_id)
            return 0
        if args.command == "write-send":
            payload = build_send_command(args.digest)
            with open(args.path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.write("\n")
            return 0
        if args.command == "explain-auth-failure":
            print(auth_failure_message(sys.stdin.read()), file=sys.stderr)
            return 1
        if args.command == "retryable-error":
            return 0 if retryable_invocation_error(sys.stdin.read()) else 1
        with open(args.path, encoding="utf-8") as handle:
            payload = json.load(handle)
        verdict = judge(payload, args.digest, args.elapsed)
    except (DeployCheckError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if verdict == "success":
        print("deployment succeeded")
        return 0
    if verdict == "wait":
        print("deployment still running")
        return EXIT_WAIT
    if verdict == "timeout":
        print("deployment timed out", file=sys.stderr)
        return 1
    print("deployment failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
