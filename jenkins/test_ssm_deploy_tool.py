"""Static tests for jenkins/ssm_deploy_tool.py (no AWS, no Jenkins, no network).

Run from the repository root:  python3 jenkins/test_ssm_deploy_tool.py
"""

import contextlib
import io
import json
import os
import tempfile
import unittest

import ssm_deploy_tool as tool

DIGEST = "sha256:5f6ab0e1caf0bc49ff1b9cb6cc14885a10b60847b8e4096405b00d7eee70755d"
OTHER = "sha256:" + "ab" * 32
ROLE = "arn:aws:sts:us-east-2:000000000000:assumed-role/sre-jenkins-deploy-role/jenkins-lab"


def events(*items):
    return "\n".join(json.dumps(item) for item in items)


def success_output(digest=DIGEST):
    return events(
        {"event": "start", "digest": digest, "repository": "sre-platform", "region": "us-east-2"},
        {"event": "public_health", "result": "ok", "port": 18001},
        {"event": "success", "digest": digest, "container": "sre-platform-abab", "port": 18001},
    )


def noop_output(digest=DIGEST):
    return events(
        {"event": "start", "digest": digest, "repository": "sre-platform", "region": "us-east-2"},
        {"event": "previous", "container": "sre-platform-candidate", "digest": digest, "port": 18000},
        {"event": "restart_policy", "container": "sre-platform-candidate", "policy": "unless-stopped"},
        {"event": "noop", "digest": digest, "container": "sre-platform-candidate", "port": 18000},
    )


def invocation(status, code, stdout, digest=DIGEST):
    return {"Status": status, "ResponseCode": code, "StandardOutputContent": stdout}


class SendCommandTests(unittest.TestCase):
    def test_request_is_limited_to_the_document_and_instance(self):
        payload = tool.build_send_command(DIGEST)
        self.assertEqual(payload["DocumentName"], "SREPlatformDeploy")
        self.assertEqual(payload["DocumentVersion"], "1")
        self.assertEqual(payload["InstanceIds"], ["i-0de56d01b124c0e69"])
        self.assertEqual(list(payload["Parameters"]), ["imageDigest"])
        self.assertEqual(payload["Parameters"]["imageDigest"], [DIGEST])
        self.assertNotIn("AWS-RunShellScript", json.dumps(payload))

    def test_repeated_digest_builds_the_same_request(self):
        self.assertEqual(tool.build_send_command(DIGEST), tool.build_send_command(DIGEST))

    def test_incorrect_digest_is_rejected(self):
        for bad in ("", "latest", DIGEST.upper(), DIGEST + ";rm", "sha256:" + "a" * 63, OTHER[:-1] + "G"):
            with self.assertRaises(tool.DeployCheckError):
                tool.build_send_command(bad)


class ClassifyTests(unittest.TestCase):
    def test_successful_ssm_deployment(self):
        result = tool.judge(invocation("Success", 0, success_output()), DIGEST, elapsed=12)
        self.assertEqual(result, "success")

    def test_failed_ssm_execution(self):
        failed = invocation("Failed", 6, events(
            {"event": "start", "digest": DIGEST},
            {"event": "error", "result": "candidate health check failed", "code": 6},
        ))
        self.assertEqual(tool.judge(failed, DIGEST, elapsed=20), "failed")
        self.assertEqual(
            tool.judge(invocation("Success", 8, success_output()), DIGEST, elapsed=20),
            "failed",
        )
        self.assertEqual(tool.judge(invocation("Cancelled", 0, success_output()), DIGEST, elapsed=1), "failed")

    def test_deployment_timeout(self):
        running = invocation("InProgress", None, "")
        self.assertEqual(tool.judge(running, DIGEST, elapsed=tool.TIMEOUT_SECONDS - 1), "wait")
        self.assertEqual(tool.judge(running, DIGEST, elapsed=tool.TIMEOUT_SECONDS), "timeout")
        self.assertEqual(tool.judge(invocation("TimedOut", None, ""), DIGEST, elapsed=5), "timeout")

    def test_output_for_a_different_digest_is_rejected(self):
        mismatched = invocation("Success", 0, success_output(OTHER))
        self.assertEqual(tool.judge(mismatched, DIGEST, elapsed=10), "failed")

    def test_success_without_nginx_health_is_rejected(self):
        body = events(
            {"event": "start", "digest": DIGEST},
            {"event": "success", "digest": DIGEST, "port": 18001},
        )
        self.assertEqual(tool.judge(invocation("Success", 0, body), DIGEST, elapsed=10), "failed")

    def test_repeated_deployment_of_the_same_digest(self):
        first = tool.judge(invocation("Success", 0, noop_output()), DIGEST, elapsed=8)
        second = tool.judge(invocation("Success", 0, noop_output()), DIGEST, elapsed=8)
        self.assertEqual(first, "success")
        self.assertEqual(second, "success")

    def test_concurrent_lock_is_not_success(self):
        body = events(
            {"event": "start", "digest": DIGEST},
            {"event": "error", "result": "another deployment is already running", "code": 11},
        )
        self.assertEqual(tool.judge(invocation("Failed", 11, body), DIGEST, elapsed=3), "failed")


class CredentialTests(unittest.TestCase):
    def test_deploy_role_is_accepted(self):
        tool.check_identity(ROLE)

    def test_missing_credentials_are_rejected(self):
        with self.assertRaises(tool.DeployCheckError):
            tool.check_identity("")
        with self.assertRaises(tool.DeployCheckError):
            tool.check_identity("arn:aws:iam::000000000000:user/sre-lab-admin")
        self.assertEqual(
            tool.auth_failure_message("Unable to locate credentials"),
            "AWS credentials are not available.",
        )
        self.assertEqual(tool.auth_failure_message(""), "AWS credentials are not available.")
        self.assertNotIn("AKIA", tool.auth_failure_message("token AKIAIOSFODNN7EXAMPLE"))


class CliTests(unittest.TestCase):
    def test_classify_exits_zero_on_success_and_ten_while_waiting(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "invocation.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(invocation("InProgress", None, ""), handle)
            waiting = io.StringIO()
            with contextlib.redirect_stdout(waiting):
                code = tool.main(["classify", path, "--digest", DIGEST, "--elapsed", "1"])
            self.assertEqual(code, 10)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(invocation("Success", 0, success_output()), handle)
            done = io.StringIO()
            with contextlib.redirect_stdout(done):
                code = tool.main(["classify", path, "--digest", DIGEST, "--elapsed", "4"])
            self.assertEqual(code, 0)
            self.assertIn("succeeded", done.getvalue())


class JenkinsfileTests(unittest.TestCase):
    def test_pipeline_invokes_only_the_restricted_document(self):
        path = os.path.join(os.path.dirname(__file__), "..", "Jenkinsfile")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("AWS-RunShellScript", source)
        self.assertIn("ssm_deploy_tool.py", source)
        self.assertIn("stage('Deploy to EC2')", source)
        self.assertIn("disableConcurrentBuilds()", source)
        self.assertIn("jenkins-aws-clear", source)


if __name__ == "__main__":
    unittest.main()
