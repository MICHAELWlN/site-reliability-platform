"""Isolated tests for deploy/ec2_deploy.py.

No AWS calls, no Docker, no Nginx, and no network. Run from the repository root:

    python3 deploy/test_ec2_deploy.py
"""

import contextlib
import fcntl
import io
import json
import os
import re
import shutil
import tempfile
import unittest

import ec2_deploy as deploy

LIVE = "sha256:5f6ab0e1caf0bc49ff1b9cb6cc14885a10b60847b8e4096405b00d7eee70755d"
NEW = "sha256:" + "ab" * 32
ACCOUNT = "123456789012"
SITE = """server {
    listen 80;
    server_name localhost;
    location / {
        proxy_pass http://127.0.0.1:18000;
        proxy_set_header Host $host;
    }
}
"""


class FakeBackend:
    def __init__(self, root, fail=None):
        self.fail = fail
        self.site_path = os.path.join(root, "sre-platform")
        self.backup_dir = os.path.join(root, "backups")
        os.makedirs(self.backup_dir, exist_ok=True)
        with open(self.site_path, "w", encoding="utf-8") as handle:
            handle.write(SITE)
        self.loaded_port = 18000
        self.cleaned = False
        self.config_open = False
        self.reload_failures = 1 if fail == "reload" else 0
        self.removed = []
        self.stopped = []
        self.removed_images = []
        self.events = []
        self.port80_checks = 0
        self.previous_restarted = False
        self.images = {}
        self.containers = {
            "sre-platform-candidate": {
                "name": "sre-platform-candidate",
                "digest": LIVE,
                "port": 18000,
                "host_ip": "127.0.0.1",
                "running": True,
                "healthy": True,
                "restart": "no",
            }
        }

    def live_container(self):
        ports = deploy.upstream_ports(self._site())
        for container in self.containers.values():
            if container["running"] and container["port"] == ports[0]:
                return container
        return None

    def http_status(self, url):
        if "127.0.0.1" not in url and "localhost" not in url:
            raise deploy.DeployError("non-local health check", 6)
        if url == "http://127.0.0.1/health":
            port = 80
        else:
            port = int(url.split(":")[2].split("/")[0])
        if self.fail in ("public", "rollback-health", "previous-stopped", "start-previous") and port == 80:
            self.port80_checks += 1
            if self.port80_checks == 1 or self.fail == "rollback-health":
                return 502
        if self.fail == "rollback-health" and port == 18000 and self.port80_checks:
            return 502
        return 200

    def nginx_upstream_ports(self):
        return [self.loaded_port]

    def memory_available_mib(self):
        return 100 if self.fail == "memory" else 400

    def disk_available_mib(self):
        return 100 if self.fail == "disk" else 2800

    def account_id(self):
        return ACCOUNT

    def ecr_login(self, host):
        self.events.append(("login", host))
        if not re.fullmatch(r"\d{12}\.dkr\.ecr\.us-east-2\.amazonaws\.com", host):
            raise deploy.DeployError("unexpected registry", 4)
        self.config_open = True
        return "sre-ecr-test"

    def pull(self, config_dir, image):
        self.events.append(("pull", image))
        if config_dir != "sre-ecr-test" or "@sha256:" not in image:
            raise RuntimeError("refusing an unpinned pull")
        if self.fail == "pull":
            raise RuntimeError("pull failed")

    def image_identity(self, image):
        self.events.append("inspect")
        if self.fail == "platform":
            return "linux/arm64", "appuser"
        return "linux/amd64", "appuser"

    def cleanup_login(self, config_dir):
        self.cleaned = True
        self.config_open = False
        self.events.append("cleanup")

    def container(self, name):
        item = self.containers.get(name)
        if item is None:
            return None
        if (
            self.fail in ("previous-stopped", "start-previous")
            and name == "sre-platform-candidate"
            and not self.previous_restarted
        ):
            item["running"] = False
        return item

    def remove_container(self, name):
        self.removed.append(name)
        self.containers.pop(name, None)

    def allocate_port(self, used):
        taken = set(used)
        for container in self.containers.values():
            if container["running"]:
                taken.add(container["port"])
        for port in range(18000, 18020):
            if port not in taken:
                return port
        raise deploy.DeployError("no free port", 6)

    def start_container(self, args):
        joined = " ".join(args)
        if "0.0.0.0:" in joined or "--restart no" not in joined:
            raise deploy.DeployError("unsafe container arguments", 6)
        name = args[args.index("--name") + 1]
        publish = args[args.index("-p") + 1]
        image = args[-1]
        port = int(publish.split(":")[1])
        self.containers[name] = {
            "name": name,
            "digest": image.rsplit("@", 1)[1],
            "port": port,
            "host_ip": "127.0.0.1",
            "running": True,
            "healthy": self.fail != "health",
            "restart": "no",
        }
        self.events.append(("start", name, port, publish))

    def wait_healthy(self, name):
        return self.containers[name]["healthy"]

    def nginx_test(self):
        self.events.append("nginx-test")
        if self.fail == "nginx-test":
            raise RuntimeError("nginx -t failed")
        if len(deploy.upstream_ports(self._site())) != 1:
            raise RuntimeError("site file is invalid")

    def nginx_reload(self):
        self.events.append("nginx-reload")
        if self.reload_failures:
            self.reload_failures -= 1
            raise RuntimeError("reload failed")
        self.loaded_port = deploy.upstream_ports(self._site())[0]

    def set_restart(self, name, policy):
        self.events.append(("restart", name, policy))
        self.containers[name]["restart"] = policy

    def other_platform_containers(self, keep_name):
        return [item["name"] for item in self.running_platform_containers() if item["name"] != keep_name]

    def running_platform_containers(self):
        return [
            container for container in self.containers.values()
            if container["name"].startswith("sre-platform-") and container["running"]
        ]

    def stopped_platform_containers(self):
        return [
            name for name, container in self.containers.items()
            if name.startswith("sre-platform-") and not container["running"]
        ]

    def start_stopped(self, name):
        self.events.append(("start-stopped", name))
        if self.fail == "start-previous":
            raise RuntimeError("could not start the previous container")
        self.previous_restarted = True
        self.containers[name]["running"] = True
        self.containers[name]["healthy"] = True

    def remove_stopped_container(self, name):
        if self.containers[name]["running"]:
            raise deploy.DeployError("refusing to remove a running container", 10)
        self.remove_container(name)

    def remove_unused_images(self, keep_digests):
        used = set(keep_digests)
        for container in self.containers.values():
            if container.get("digest"):
                used.add(container["digest"])
            used.update(container.get("digests") or ())
        for digest, image in list(self.images.items()):
            repository = image.get("repository", "")
            if repository != "sre-platform" and not repository.endswith("/sre-platform"):
                continue
            if digest in used:
                continue
            self.removed_images.append(digest)
            del self.images[digest]

    def stop_container(self, name):
        self.stopped.append(name)
        self.containers[name]["running"] = False

    def _site(self):
        with open(self.site_path, encoding="utf-8") as handle:
            return handle.read()


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sre-deploy-")
        self.addCleanup(shutil.rmtree, self.root)

    def test_successful_deployment(self):
        backend = FakeBackend(self.root)
        result, logs = self.run_deploy(backend, NEW)
        self.assertEqual(result["result"], "deployed")
        self.assertEqual(result["port"], 18001)
        self.assertIn("127.0.0.1:18001:8000", logs)
        self.assertEqual(backend.loaded_port, 18001)
        self.assertEqual(deploy.upstream_ports(backend._site()), [18001])
        self.assertEqual(backend.containers[result["container"]]["restart"], "unless-stopped")
        self.assertFalse(backend.containers["sre-platform-candidate"]["running"])
        self.assertNotIn("sre-platform-candidate", backend.removed)
        self.assertTrue(backend.cleaned)
        self.assertFalse(backend.config_open)
        self.assert_no_secrets(logs)

    def test_failed_image_pull(self):
        backend = FakeBackend(self.root, fail="pull")
        error, logs = self.run_deploy(backend, NEW)
        self.assertEqual(error.code, 4)
        self.assertTrue(backend.cleaned)
        self.assertFalse(backend.config_open)
        self.assertNotIn("start", [event[0] for event in backend.events if isinstance(event, tuple)])
        self.assertEqual(deploy.upstream_ports(backend._site()), [18000])
        self.assertTrue(backend.containers["sre-platform-candidate"]["running"])
        self.assert_no_secrets(logs)

    def test_failed_container_health(self):
        backend = FakeBackend(self.root, fail="health")
        error, logs = self.run_deploy(backend, NEW)
        self.assertEqual(error.code, 6)
        self.assertIn(deploy.container_name(NEW), backend.removed)
        self.assertNotIn("sre-platform-candidate", backend.removed)
        self.assertTrue(backend.containers["sre-platform-candidate"]["running"])
        self.assertEqual(deploy.upstream_ports(backend._site()), [18000])
        self.assertNotIn("nginx-reload", backend.events)
        self.assert_no_secrets(logs)

    def test_failed_nginx_validation(self):
        backend = FakeBackend(self.root, fail="nginx-test")
        error, logs = self.run_deploy(backend, NEW)
        self.assertEqual(error.code, 7)
        self.assertEqual(deploy.upstream_ports(backend._site()), [18000])
        self.assertEqual(backend.loaded_port, 18000)
        self.assertNotIn("nginx-reload", backend.events)
        self.assertIn(deploy.container_name(NEW), backend.removed)
        self.assertTrue(backend.containers["sre-platform-candidate"]["running"])
        self.assert_no_secrets(logs)

    def test_rollback_after_public_health_failure(self):
        backend = FakeBackend(self.root, fail="public")
        error, logs = self.run_deploy(backend, NEW)
        self.assertEqual(error.code, 9)
        self.assertEqual(deploy.upstream_ports(backend._site()), [18000])
        self.assertEqual(backend.loaded_port, 18000)
        self.assertGreaterEqual(backend.events.count("nginx-reload"), 2)
        self.assertIn(deploy.container_name(NEW), backend.removed)
        self.assertTrue(backend.containers["sre-platform-candidate"]["running"])
        self.assertNotIn("sre-platform-candidate", backend.stopped)
        self.assert_no_secrets(logs)

    def test_rollback_after_nginx_reload_failure(self):
        backend = FakeBackend(self.root, fail="reload")
        error, logs = self.run_deploy(backend, NEW)
        self.assertEqual(error.code, 8)
        self.assertEqual(deploy.upstream_ports(backend._site()), [18000])
        self.assertEqual(backend.loaded_port, 18000)
        self.assertIn(deploy.container_name(NEW), backend.removed)
        self.assertTrue(backend.containers["sre-platform-candidate"]["running"])
        self.assert_no_secrets(logs)

    def test_repeated_deployment_of_the_same_digest(self):
        backend = FakeBackend(self.root)
        first, first_logs = self.run_deploy(backend, LIVE)
        self.assertEqual(first["result"], "noop")
        self.assertEqual(first["container"], "sre-platform-candidate")
        self.assertNotIn("pull", [event[0] for event in backend.events if isinstance(event, tuple)])
        self.assertEqual(deploy.upstream_ports(backend._site()), [18000])
        self.assertEqual(backend.containers["sre-platform-candidate"]["restart"], "unless-stopped")
        self.assertTrue(backend.containers["sre-platform-candidate"]["running"])

        second, second_logs = self.run_deploy(backend, LIVE)
        self.assertEqual(second["result"], "noop")
        self.assertEqual(len(backend.containers), 1)
        self.assert_no_secrets(first_logs + second_logs)

        fresh = FakeBackend(self.root)
        deployed, _logs = self.run_deploy(fresh, NEW)
        again, _logs = self.run_deploy(fresh, NEW)
        self.assertEqual(deployed["result"], "deployed")
        self.assertEqual(again["result"], "noop")
        self.assertEqual(again["container"], deployed["container"])
        starts = [event for event in fresh.events if isinstance(event, tuple) and event[0] == "start"]
        self.assertEqual(len(starts), 1)

        recorded = FakeBackend(self.root)
        recorded.containers["sre-platform-candidate"]["digest"] = "sha256:" + "c" * 64
        recorded.containers["sre-platform-candidate"]["digests"] = [LIVE]
        matched, _logs = self.run_deploy(recorded, LIVE)
        self.assertEqual(matched["result"], "noop")
        self.assertEqual(len(recorded.containers), 1)

    def test_concurrent_deployment_is_rejected(self):
        lock_path = os.path.join(self.root, "deploy.lock")
        held = open(lock_path, "a+")
        self.addCleanup(held.close)
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        backend = FakeBackend(self.root)
        error, logs = self.run_deploy(backend, NEW, lock_path=lock_path)
        self.assertEqual(error.code, 11)
        self.assertEqual(deploy.upstream_ports(backend._site()), [18000])
        self.assertTrue(backend.containers["sre-platform-candidate"]["running"])
        self.assertFalse(any(isinstance(event, tuple) and event[0] == "pull" for event in backend.events))
        self.assert_no_secrets(logs)
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        released, _logs = self.run_deploy(backend, NEW, lock_path=lock_path)
        self.assertEqual(released["result"], "deployed")

    def test_lock_is_released_after_failure(self):
        backend = FakeBackend(self.root, fail="pull")
        error, _logs = self.run_deploy(backend, NEW)
        self.assertEqual(error.code, 4)
        backend.fail = None
        result, _logs = self.run_deploy(backend, LIVE)
        self.assertEqual(result["result"], "noop")

    def test_failed_rollback_returns_nonzero(self):
        backend = FakeBackend(self.root, fail="rollback-health")
        error, logs = self.run_deploy(backend, NEW)
        self.assertEqual(error.code, 12)
        self.assertEqual(backend.containers["sre-platform-candidate"]["digest"], LIVE)
        self.assertTrue(backend.containers["sre-platform-candidate"]["running"])
        self.assertEqual(deploy.upstream_ports(backend._site()), [18000])
        self.assertNotIn("sre-platform-candidate", backend.removed)
        self.assert_no_secrets(logs)

        refused = FakeBackend(self.root, fail="start-previous")
        refused_error, _logs = self.run_deploy(refused, NEW)
        self.assertEqual(refused_error.code, 12)
        self.assertIn(("start-stopped", "sre-platform-candidate"), refused.events)
        self.assertEqual(refused.containers["sre-platform-candidate"]["digest"], LIVE)
        self.assertFalse(refused.containers["sre-platform-candidate"]["running"])

    def test_previous_container_is_preserved_until_health_passes(self):
        backend = FakeBackend(self.root, fail="public")
        error, logs = self.run_deploy(backend, NEW)
        self.assertEqual(error.code, 9)
        previous = backend.containers["sre-platform-candidate"]
        self.assertEqual(previous["digest"], LIVE)
        self.assertTrue(previous["running"])
        self.assertEqual(previous["restart"], "unless-stopped")
        self.assertNotIn("sre-platform-candidate", backend.stopped)
        self.assertNotIn("retire_previous", logs)
        self.assertEqual(deploy.upstream_ports(backend._site()), [18000])
        self.assertEqual(backend.http_status("http://127.0.0.1:18000/health"), 200)

        restarted = FakeBackend(self.root, fail="previous-stopped")
        restarted_error, restarted_logs = self.run_deploy(restarted, NEW)
        self.assertEqual(restarted_error.code, 9)
        self.assertIn(("start-stopped", "sre-platform-candidate"), restarted.events)
        self.assertEqual(restarted.containers["sre-platform-candidate"]["digest"], LIVE)
        self.assertTrue(restarted.containers["sre-platform-candidate"]["running"])
        self.assertIn('"digest": "%s"' % LIVE, restarted_logs)

    def test_restart_policy_and_single_active_container(self):
        failed = FakeBackend(self.root, fail="health")
        error, _logs = self.run_deploy(failed, NEW)
        self.assertEqual(error.code, 6)
        candidate = deploy.container_name(NEW)
        self.assertNotIn(candidate, failed.containers)
        self.assertNotIn(("restart", candidate, "unless-stopped"), failed.events)
        self.assertEqual(failed.containers["sre-platform-candidate"]["restart"], "no")
        self.assertEqual(len(failed.running_platform_containers()), 1)

        backend = FakeBackend(self.root)
        old_digest = "sha256:" + "d" * 64
        unused_digest = "sha256:" + "e" * 64
        unrelated = "sha256:" + "f" * 64
        backend.containers["sre-platform-old"] = {
            "name": "sre-platform-old",
            "digest": old_digest,
            "port": 18002,
            "host_ip": "127.0.0.1",
            "running": False,
            "healthy": False,
            "restart": "no",
        }
        backend.images = {
            LIVE: {"repository": "sre-platform"},
            old_digest: {"repository": "sre-platform"},
            unused_digest: {"repository": "sre-platform"},
            unrelated: {"repository": "unrelated"},
        }
        result, logs = self.run_deploy(backend, NEW)
        self.assertEqual(result["result"], "deployed")
        active = backend.containers[result["container"]]
        self.assertEqual(active["restart"], "unless-stopped")
        self.assertTrue(active["running"])
        self.assertFalse(backend.containers["sre-platform-candidate"]["running"])
        self.assertEqual(backend.containers["sre-platform-candidate"]["digest"], LIVE)
        self.assertNotIn("sre-platform-candidate", backend.removed)
        self.assertNotIn("sre-platform-old", backend.containers)
        self.assertEqual(len(backend.running_platform_containers()), 1)
        self.assertIn(unused_digest, backend.removed_images)
        self.assertNotIn(LIVE, backend.removed_images)
        self.assertIn(unrelated, backend.images)
        self.assertNotIn("prune", logs)
        again, _logs = self.run_deploy(backend, NEW)
        self.assertEqual(again["result"], "noop")
        self.assertEqual(deploy.upstream_ports(backend._site()), [18001])
        self.assertEqual(len(backend.running_platform_containers()), 1)
        self.assertIn("sre-platform-candidate", backend.containers)
        self.assertFalse(backend.containers["sre-platform-candidate"]["running"])
        self.assertEqual(backend.containers["sre-platform-candidate"]["digest"], LIVE)
        starts = [event for event in backend.events if isinstance(event, tuple) and event[0] == "start"]
        self.assertEqual(len(starts), 1)

    def test_low_memory_or_disk_does_not_start_a_container(self):
        for reason in ("memory", "disk"):
            backend = FakeBackend(self.root, fail=reason)
            error, _logs = self.run_deploy(backend, NEW)
            self.assertEqual(error.code, 3)
            self.assertEqual(len(backend.containers), 1)
            self.assertTrue(backend.containers["sre-platform-candidate"]["running"])
            self.assertFalse(any(isinstance(event, tuple) and event[0] == "start" for event in backend.events))

    def test_rejects_invalid_digest_before_touching_the_host(self):
        with self.assertRaises(deploy.DeployError) as caught:
            deploy.deploy(None, "sha256:" + "a" * 64 + "; rm -rf /")
        self.assertEqual(caught.exception.code, 2)

    def test_refuses_non_amd64_image(self):
        backend = FakeBackend(self.root, fail="platform")
        error, _logs = self.run_deploy(backend, NEW)
        self.assertEqual(error.code, 5)
        self.assertTrue(backend.cleaned)
        self.assertEqual(len(backend.containers), 1)
        self.assertEqual(deploy.upstream_ports(backend._site()), [18000])

    def test_container_publish_is_localhost_only(self):
        _host, image = deploy.image_ref(ACCOUNT, NEW)
        args = deploy.container_run_args(deploy.container_name(NEW), image, 18001)
        self.assertIn("127.0.0.1:18001:8000", args)
        self.assertNotIn("0.0.0.0:18001:8000", args)
        self.assertEqual(args[args.index("--restart") + 1], "no")
        with self.assertRaises(deploy.DeployError):
            deploy.container_run_args(deploy.container_name(NEW), image, 8000)

    def test_nginx_rewrite_changes_only_the_upstream_line(self):
        source = os.path.join(os.path.dirname(__file__), "nginx.conf")
        with open(source, encoding="utf-8") as handle:
            original = handle.read()
        updated, old_port = deploy.render_proxy_pass(original, 18000)
        self.assertEqual(old_port, 8000)
        original_lines = original.splitlines()
        updated_lines = updated.splitlines()
        changed = [
            (left, right) for left, right in zip(original_lines, updated_lines) if left != right
        ]
        self.assertEqual(changed, [(
            "        proxy_pass http://127.0.0.1:8000;",
            "        proxy_pass http://127.0.0.1:18000;",
        )])
        site = os.path.join(self.root, "site.conf")
        with open(site, "w", encoding="utf-8") as handle:
            handle.write(original)
        backup, _old = deploy.apply_site_update(site, self.root, 18001)
        deploy.restore_site(site, backup)
        with open(site, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)

    def test_logs_redact_credentials(self):
        self.assertIn("[redacted]", deploy.safe_text("key AKIAIOSFODNN7EXAMPLE leaked"))
        self.assertIn("password=[redacted]", deploy.safe_text("password=super-secret"))

    def test_script_does_not_stop_systemd(self):
        path = os.path.join(os.path.dirname(__file__), "ec2_deploy.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("systemctl stop", source)
        self.assertNotIn("systemctl restart", source)
        self.assertNotIn("system prune", source)
        self.assertNotIn("image prune", source)
        self.assertIn('["systemctl", "reload", "nginx"]', source)
        self.assertIn('["docker", "rmi", repository + "@" + digest]', source)

    def test_main_does_not_call_aws_when_not_root(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = deploy.main(["--digest", NEW])
        self.assertEqual(code, 1)
        self.assertIn("root is required", buffer.getvalue())

    def run_deploy(self, backend, digest, lock_path=None):
        if lock_path is None:
            lock_path = os.path.join(self.root, "deploy.lock")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            try:
                result = deploy.deploy(backend, digest, lock_path=lock_path)
            except deploy.DeployError as exc:
                return exc, buffer.getvalue()
        return result, buffer.getvalue()

    def assert_no_secrets(self, logs):
        self.assertNotIn(ACCOUNT, logs)
        self.assertNotIn("password", logs.lower())
        self.assertNotIn("AKIA", logs)
        for line in logs.splitlines():
            json.loads(line)


class SsmDocumentTests(unittest.TestCase):
    def test_document_accepts_only_a_digest(self):
        path = os.path.join(os.path.dirname(__file__), "ssm", "SREPlatformDeploy.json")
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document["schemaVersion"], "2.2")
        self.assertEqual(list(document["parameters"]), ["imageDigest"])
        pattern = document["parameters"]["imageDigest"]["allowedPattern"]
        self.assertEqual(pattern, r"^sha256:[0-9a-f]{64}$")
        self.assertIsNotNone(re.fullmatch(pattern, LIVE))
        rejected = [
            LIVE + "; rm -rf /",
            LIVE[:-1],
            LIVE + "a",
            "sha256:" + "A" * 64,
            "sha256:" + "g" * 64,
            "sha256:" + "a" * 63 + "'",
            "sha256:" + "a" * 63 + '"',
            "sha256:" + "a" * 63 + "`",
            "sha256:" + "a" * 63 + "$",
            "sha256:" + "ab\n" + "c" * 61,
            "sha256:" + "a" * 32 + " " + "b" * 31,
        ]
        for sample in rejected:
            self.assertIsNone(re.fullmatch(pattern, sample), sample)
        step = document["mainSteps"]
        self.assertEqual(len(step), 1)
        command = step[0]["inputs"]["runCommand"][0]
        self.assertEqual(command.count("{{"), 1)
        self.assertIn("--digest '{{ imageDigest }}'", command)
        self.assertEqual(step[0]["inputs"]["runCommand"], [
            "/usr/local/sbin/sre-platform-deploy --digest '{{ imageDigest }}'"
        ])
        self.assertNotIn("AWS-RunShellScript", json.dumps(document))


if __name__ == "__main__":
    unittest.main()
