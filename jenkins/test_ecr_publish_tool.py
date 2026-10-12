"""Static tests for jenkins/ecr_publish_tool.py (no AWS, no Docker, no network).

Run from the repository root:  python3 -m unittest jenkins.test_ecr_publish_tool -v
or:                            python3 jenkins/test_ecr_publish_tool.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ecr_publish_tool as tool  # noqa: E402

SHA = "43eb11cef129969bf70df72267950fb0b57ceac5"
OTHER_SHA = "0" * 40
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


def config(arch="amd64", os_name="linux", labels=None, variant=None):
    body = {"architecture": arch, "os": os_name, "config": {}}
    if labels is not None:
        body["config"]["Labels"] = labels
    if variant:
        body["variant"] = variant
    return body


def run_cli(argv, stdin_text=""):
    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(argv)
    finally:
        sys.stdin = old_stdin
    return code, out.getvalue(), err.getvalue()


class CheckConfigTests(unittest.TestCase):
    def check(self, obj, require_label=False, sha=SHA):
        return tool.check_config(json.dumps(obj), sha, require_label)

    def test_single_amd64_without_label_is_accepted_when_label_not_required(self):
        self.assertEqual(self.check(config()), ("linux/amd64", None))

    def test_matching_label_is_accepted(self):
        labels = {tool.REVISION_LABEL: SHA}
        self.assertEqual(self.check(config(labels=labels), require_label=True), ("linux/amd64", SHA))

    def test_mismatched_label_is_rejected(self):
        with self.assertRaises(tool.CheckError):
            self.check(config(labels={tool.REVISION_LABEL: OTHER_SHA}))

    def test_missing_label_is_rejected_when_required(self):
        with self.assertRaises(tool.CheckError):
            self.check(config(), require_label=True)

    def test_arm64_is_rejected(self):
        with self.assertRaises(tool.CheckError):
            self.check(config(arch="arm64"))

    def test_arm_variant_is_rejected(self):
        with self.assertRaises(tool.CheckError):
            self.check(config(arch="amd64", variant="v3"))

    def test_non_linux_is_rejected(self):
        with self.assertRaises(tool.CheckError):
            self.check(config(os_name="windows"))

    def test_wrapped_image_key_is_accepted(self):
        self.assertEqual(self.check({"image": config()}), ("linux/amd64", None))

    def test_single_entry_platform_map_is_accepted(self):
        self.assertEqual(self.check({"linux/amd64": config()}), ("linux/amd64", None))

    def test_multi_platform_map_is_rejected(self):
        with self.assertRaises(tool.CheckError):
            self.check({"linux/amd64": config(), "linux/arm64": config(arch="arm64")})

    def test_unknown_shape_is_rejected(self):
        with self.assertRaises(tool.CheckError):
            self.check({"something": "else"})

    def test_garbage_is_rejected(self):
        with self.assertRaises(tool.CheckError):
            tool.check_config("not json", SHA, False)

    def test_non_object_is_rejected(self):
        with self.assertRaises(tool.CheckError):
            tool.check_config("[1, 2]", SHA, False)


class CheckConfigCliTests(unittest.TestCase):
    def test_success_prints_platform_and_label_state(self):
        code, out, _ = run_cli(["check-config", "--expect-sha", SHA], json.dumps(config()))
        self.assertEqual(code, 0)
        self.assertIn("platform=linux/amd64", out)
        self.assertIn("revision_label=absent", out)

    def test_failure_exit_code_and_message(self):
        code, _, err = run_cli(["check-config", "--expect-sha", SHA], json.dumps(config(arch="arm64")))
        self.assertEqual(code, 1)
        self.assertIn("arm64", err)

    def test_short_sha_is_rejected(self):
        code, _, err = run_cli(["check-config", "--expect-sha", "abc123"], json.dumps(config()))
        self.assertEqual(code, 1)
        self.assertIn("40-character", err)


class DecideReuseTests(unittest.TestCase):
    def decide(self, existing=DIGEST, recorded="", supplied=""):
        return tool.decide_reuse(existing, recorded, supplied)

    def test_matching_record_is_reused(self):
        self.assertEqual(self.decide(recorded=DIGEST), (DIGEST, "record"))

    def test_matching_operator_digest_is_reused_when_no_record(self):
        self.assertEqual(self.decide(supplied=DIGEST), (DIGEST, "operator-attested"))

    def test_record_wins_when_both_agree(self):
        self.assertEqual(self.decide(recorded=DIGEST, supplied=DIGEST), (DIGEST, "record"))

    def test_no_expected_digest_is_refused(self):
        with self.assertRaises(tool.CheckError) as ctx:
            self.decide()
        self.assertIn("will not be reused", str(ctx.exception))
        self.assertIn("Nothing was pushed", str(ctx.exception))

    def test_record_mismatch_is_refused(self):
        with self.assertRaises(tool.CheckError) as ctx:
            self.decide(recorded=OTHER_DIGEST)
        self.assertIn("Digest mismatch", str(ctx.exception))
        self.assertIn("Nothing was pushed", str(ctx.exception))

    def test_operator_mismatch_is_refused(self):
        with self.assertRaises(tool.CheckError):
            self.decide(supplied=OTHER_DIGEST)

    def test_disagreement_between_record_and_operator_is_refused(self):
        with self.assertRaises(tool.CheckError) as ctx:
            self.decide(recorded=DIGEST, supplied=OTHER_DIGEST)
        self.assertIn("disagrees", str(ctx.exception))

    def test_invalid_existing_digest_is_refused(self):
        with self.assertRaises(tool.CheckError):
            self.decide(existing="sha256:short", recorded=DIGEST)

    def test_invalid_supplied_digest_is_refused(self):
        with self.assertRaises(tool.CheckError):
            self.decide(supplied="not-a-digest")

    def test_known_existing_image_is_reusable_when_attested(self):
        # Digest recorded in sre-platform-ci-ecr-test Build #1 after the push succeeded.
        known = "sha256:047d71df3dc9e93099c4d095926ea2a8b49f890b8f7534974b8ff7a6584081c1"
        self.assertEqual(self.decide(existing=known, supplied=known), (known, "operator-attested"))

    def test_cli_prints_state_on_success_and_exits_1_on_refusal(self):
        code, out, _ = run_cli(
            ["decide-reuse", "--existing", DIGEST, "--recorded", DIGEST]
        )
        self.assertEqual(code, 0)
        self.assertIn("MODE=reused", out)
        self.assertIn(f"DIGEST={DIGEST}", out)
        self.assertIn("TRUST=record", out)
        code, _, err = run_cli(["decide-reuse", "--existing", DIGEST])
        self.assertEqual(code, 1)
        self.assertIn("will not be reused", err)


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "records", SHA + ".record")

    def write(self, digest=DIGEST, status="pushed", source="pipeline-push", extra=()):
        return run_cli(
            ["record-write", self.path, "--repository", "sre-platform", "--sha", SHA,
             "--digest", digest, "--status", status, "--source", source, *extra]
        )

    def test_read_missing_record_exits_4_with_no_output(self):
        code, out, _ = run_cli(["record-read", self.path])
        self.assertEqual((code, out), (4, ""))

    def test_write_then_read_roundtrip_and_mode(self):
        self.assertEqual(self.write()[0], 0)
        self.assertEqual(run_cli(["record-read", self.path]), (0, DIGEST + "\n", ""))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        fields = tool.read_record(self.path)
        self.assertEqual(fields["tag"], SHA)
        self.assertEqual(fields["platform"], "linux/amd64")
        self.assertEqual(fields["status"], "pushed")

    def test_upgrade_to_verified_keeps_original_source(self):
        self.write(status="pushed", source="pipeline-push")
        self.write(status="verified", source="operator-attested")
        fields = tool.read_record(self.path)
        self.assertEqual(fields["status"], "verified")
        self.assertEqual(fields["source"], "pipeline-push")

    def test_verified_is_never_downgraded(self):
        self.write(status="verified")
        self.write(status="pushed")
        self.assertEqual(tool.read_record(self.path)["status"], "verified")

    def test_different_digest_is_refused_and_record_unchanged(self):
        self.write()
        code, _, err = self.write(digest=OTHER_DIGEST)
        self.assertEqual(code, 1)
        self.assertIn("refusing", err)
        self.assertEqual(tool.read_record(self.path)["digest"], DIGEST)

    def test_stale_record_can_be_replaced_only_when_asked(self):
        self.write()
        code, _, err = self.write(digest=OTHER_DIGEST, extra=("--replace-stale",))
        self.assertEqual(code, 0)
        self.assertIn("stale", err)
        self.assertEqual(tool.read_record(self.path)["digest"], OTHER_DIGEST)

    def test_malformed_digest_is_rejected_on_write_and_read(self):
        self.assertEqual(self.write(digest="sha256:short")[0], 1)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as handle:
            handle.write("digest=not-a-digest\n")
        self.assertEqual(run_cli(["record-read", self.path])[0], 1)

    def test_no_temp_files_left_behind(self):
        self.write()
        self.write(status="verified")
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [SHA + ".record"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
