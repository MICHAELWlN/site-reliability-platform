#!/usr/bin/env python3
"""Helpers for the ECR publish stages of the Jenkinsfile.

Subcommands (all stdlib only, no network, no AWS calls, no credentials):

  check-config   Read an image config (JSON, from `docker buildx imagetools inspect
                 --format '{{json .Image}}'`) on stdin and require a single linux/amd64 image
                 whose revision label, if present, equals the expected commit SHA.
  decide-reuse   Given the digest ECR already holds and the expected digest (from a publish
                 record and/or the operator), decide whether the existing immutable tag may
                 be reused. Never implies a push, overwrite or delete.
  record-read    Print the digest stored in a publish record, or exit 4 if there is no record.
  record-write   Create or update a publish record without ever silently changing a digest.

A publish record is a small key=value file in JENKINS_HOME that the pipeline writes after it
pushes an image. It is how a later build of the same commit knows which digest is expected
before it agrees to reuse an existing, immutable ECR tag. It is not a signature: it is only as
trustworthy as write access to JENKINS_HOME.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

REVISION_LABEL = "org.opencontainers.image.revision"
EXPECTED_PLATFORM = "linux/amd64"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
STATUSES = ("pushed", "verified")
SOURCES = ("pipeline-push", "operator-attested")
EXIT_NO_RECORD = 4


class CheckError(Exception):
    pass


def find_configs(obj):
    """Return the image config objects found in the parsed JSON, whatever shape buildx used."""
    if not isinstance(obj, dict):
        raise CheckError("image config is not a JSON object")
    if "architecture" in obj or "os" in obj:
        return [obj]
    if isinstance(obj.get("image"), dict):
        return find_configs(obj["image"])
    values = list(obj.values())
    if values and all(isinstance(v, dict) and ("architecture" in v or "os" in v) for v in values):
        return values
    raise CheckError("could not find an image config in the buildx output")


def check_config(text, expect_sha, require_label):
    """Return (platform, revision_label_or_None). Raise CheckError on any mismatch."""
    try:
        parsed = json.loads(text)
    except ValueError:
        raise CheckError("buildx output is not valid JSON")
    configs = find_configs(parsed)
    if len(configs) != 1:
        raise CheckError(
            f"expected one single-platform image, found {len(configs)} platform entries "
            "(an index or attestation manifest was not expected)"
        )
    config = configs[0]
    platform = f"{config.get('os')}/{config.get('architecture')}"
    if config.get("variant"):
        platform += f"/{config['variant']}"
    if platform != EXPECTED_PLATFORM:
        raise CheckError(f"image platform is {platform}, expected {EXPECTED_PLATFORM}")

    labels = (config.get("config") or {}).get("Labels") or {}
    revision = labels.get(REVISION_LABEL)
    if revision is not None and revision != expect_sha:
        raise CheckError(f"image revision label {revision!r} does not match the commit {expect_sha}")
    if revision is None and require_label:
        raise CheckError("image has no revision label, but this build just created it with one")
    return platform, revision


def decide_reuse(existing, recorded, supplied):
    """Return (digest, trust_source). Raise CheckError if the existing tag must not be reused.

    The expected digest has to come from somewhere other than ECR itself: a publish record
    written after a previous push, or an operator-supplied value. If both are present they
    must agree. A digest mismatch refuses reuse and never suggests overwriting or deleting.
    """
    if not DIGEST_RE.match(existing or ""):
        raise CheckError("ECR returned an unexpected digest value")
    if supplied and not DIGEST_RE.match(supplied):
        raise CheckError("EXPECTED_IMAGE_DIGEST is not a valid sha256 digest")
    if recorded and not DIGEST_RE.match(recorded):
        raise CheckError("publish record digest is not a valid sha256 digest")
    if recorded and supplied and recorded != supplied:
        raise CheckError(
            "EXPECTED_IMAGE_DIGEST disagrees with the publish record. Refusing to reuse. "
            "Nothing was pushed, overwritten or deleted."
        )
    if recorded:
        expected, trust = recorded, "record"
    elif supplied:
        expected, trust = supplied, "operator-attested"
    else:
        raise CheckError(
            "No publish record exists for this commit and EXPECTED_IMAGE_DIGEST is empty. "
            "The existing image cannot be attributed to this build, so it will not be reused. "
            "Nothing was pushed, overwritten or deleted."
        )
    if existing != expected:
        raise CheckError(
            f"Digest mismatch for the existing tag. Refusing to reuse. "
            f"ECR holds {existing}; expected {expected} ({trust}). "
            "Nothing was pushed, overwritten or deleted."
        )
    return expected, trust


def read_record(path):
    fields = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key] = value
    return fields


def cmd_check_config(args):
    if not SHA_RE.match(args.expect_sha):
        print("error: --expect-sha must be a full 40-character commit SHA", file=sys.stderr)
        return 1
    try:
        platform, revision = check_config(sys.stdin.read(), args.expect_sha, args.require_label)
    except CheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"platform={platform}")
    print(f"revision_label={revision if revision is not None else 'absent'}")
    return 0


def cmd_decide_reuse(args):
    try:
        digest, trust = decide_reuse(args.existing, args.recorded, args.supplied)
    except CheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"MODE=reused")
    print(f"DIGEST={digest}")
    print(f"TRUST={trust}")
    return 0


def cmd_record_read(args):
    try:
        fields = read_record(args.path)
    except FileNotFoundError:
        return EXIT_NO_RECORD
    digest = fields.get("digest", "")
    if not DIGEST_RE.match(digest):
        print(f"error: publish record {args.path} has no valid digest", file=sys.stderr)
        return 1
    print(digest)
    return 0


def cmd_record_write(args):
    if not DIGEST_RE.match(args.digest):
        print("error: --digest is not a valid sha256 digest", file=sys.stderr)
        return 1
    if not SHA_RE.match(args.sha):
        print("error: --sha must be a full 40-character commit SHA", file=sys.stderr)
        return 1

    source = args.source
    status = args.status
    try:
        existing = read_record(args.path)
    except FileNotFoundError:
        existing = None

    if existing is not None:
        old_digest = existing.get("digest")
        if old_digest != args.digest:
            if not args.replace_stale:
                print(
                    f"error: publish record already holds a different digest ({old_digest}); "
                    "refusing to change it",
                    file=sys.stderr,
                )
                return 1
            print(f"replacing stale publish record (old digest {old_digest})", file=sys.stderr)
        else:
            # Same digest: keep the original source, and never downgrade verified to pushed.
            source = existing.get("source", source)
            if existing.get("status") == "verified":
                status = "verified"

    lines = [
        f"repository={args.repository}",
        f"tag={args.sha}",
        f"digest={args.digest}",
        f"platform={EXPECTED_PLATFORM}",
        f"status={status}",
        f"source={source}",
        f"job={args.job}",
        f"build={args.build}",
        f"recorded_at={datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    ]
    directory = os.path.dirname(os.path.abspath(args.path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".record.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, args.path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"record {status} ({source}) for {args.sha}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-config")
    check.add_argument("--expect-sha", required=True)
    check.add_argument("--require-label", action="store_true")
    check.set_defaults(func=cmd_check_config)

    decide = sub.add_parser("decide-reuse")
    decide.add_argument("--existing", required=True, help="digest ECR currently holds for the tag")
    decide.add_argument("--recorded", default="", help="digest from the publish record, if any")
    decide.add_argument("--supplied", default="", help="digest supplied by the operator, if any")
    decide.set_defaults(func=cmd_decide_reuse)

    read = sub.add_parser("record-read")
    read.add_argument("path")
    read.set_defaults(func=cmd_record_read)

    write = sub.add_parser("record-write")
    write.add_argument("path")
    write.add_argument("--repository", required=True)
    write.add_argument("--sha", required=True)
    write.add_argument("--digest", required=True)
    write.add_argument("--status", choices=STATUSES, required=True)
    write.add_argument("--source", choices=SOURCES, required=True)
    write.add_argument("--job", default="unknown")
    write.add_argument("--build", default="unknown")
    write.add_argument("--replace-stale", action="store_true")
    write.set_defaults(func=cmd_record_write)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
