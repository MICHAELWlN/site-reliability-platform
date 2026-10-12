#!/usr/bin/env python3
"""Deploy one immutable ECR image on the EC2 host.

The script is meant to run on the instance as root, invoked only by the
SSM document SREPlatformDeploy. It uses the instance role for ECR, binds
the container to 127.0.0.1, and does not stop the systemd unit sre-service.

Nothing in this module contacts AWS, Docker, or Nginx unless RealBackend
is used. Tests pass a fake backend.
"""

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REGION = "us-east-2"
REPOSITORY = "sre-platform"
NGINX_SITE = "/etc/nginx/sites-available/sre-platform"
BACKUP_DIR = "/var/backups"
PORT_MIN = 18000
PORT_MAX = 18019
# Free memory required while the previous container is still running.
# A t3.micro has about 1 GiB total; the candidate has to fit beside it.
MIN_AVAILABLE_MIB = 180
MIN_DISK_MIB = 500
HEALTH_TIMEOUT_SECONDS = 60
LOCK_PATH = "/var/lock/sre-platform-deploy.lock"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PROXY_RE = re.compile(r"^(\s*)proxy_pass http://127\.0\.0\.1:(\d+);\s*$", re.MULTILINE)
CONTAINER_PREFIX = "sre-platform-"


class DeployError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.message = message
        self.code = code


def log(event, **fields):
    """Write one JSON object. Refuses fields that look like credentials."""
    for key, value in fields.items():
        lowered = key.lower()
        if lowered in ("password", "secret", "token", "authorization", "sessiontoken"):
            raise DeployError("refusing to log a secret field", 1)
        if re.search(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b", str(value)):
            raise DeployError("refusing to log a credential", 1)
    record = {"event": event, **fields}
    print(json.dumps(record, sort_keys=True), flush=True)


def safe_text(value):
    text = str(value)
    text = re.sub(r"(?i)(password|secret|token|authorization)\s*[:=]\s*\S+", r"\1=[redacted]", text)
    text = re.sub(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b", "[redacted]", text)
    return text[:200]


def container_name(digest):
    if not DIGEST_RE.fullmatch(digest or ""):
        raise DeployError("image digest must be sha256 and 64 hex characters", 2)
    return CONTAINER_PREFIX + digest[7:19]


def container_run_args(name, image, port):
    """Localhost-only candidate. Restart stays off until the deploy is verified."""
    if not isinstance(port, int) or not PORT_MIN <= port <= PORT_MAX:
        raise DeployError("refusing a container port outside 18000-18019", 6)
    if not name.startswith(CONTAINER_PREFIX):
        raise DeployError("refusing an unexpected container name", 6)
    if "@sha256:" not in image or not DIGEST_RE.fullmatch(image.rsplit("@", 1)[1]):
        raise DeployError("refusing to run an image that is not pinned by digest", 6)
    return [
        "docker", "run", "-d",
        "--name", name,
        "--restart", "no",
        "-p", f"127.0.0.1:{port}:8000",
        image,
    ]


def image_ref(account, digest):
    if not re.fullmatch(r"\d{12}", account or ""):
        raise DeployError("unexpected account id", 4)
    if not DIGEST_RE.fullmatch(digest or ""):
        raise DeployError("image digest must be sha256 and 64 hex characters", 2)
    host = f"{account}.dkr.ecr.{REGION}.amazonaws.com"
    return host, f"{host}/{REPOSITORY}@{digest}"


def render_proxy_pass(text, new_port):
    """Change the single localhost proxy_pass. Leave every other line alone."""
    if not isinstance(new_port, int) or not PORT_MIN <= new_port <= PORT_MAX:
        raise DeployError("refusing to switch nginx outside 18000-18019", 7)
    matches = list(PROXY_RE.finditer(text))
    if len(matches) != 1:
        raise DeployError(f"expected one localhost proxy_pass, found {len(matches)}", 7)
    old_port = int(matches[0].group(2))
    updated = PROXY_RE.sub(
        lambda match: f"{match.group(1)}proxy_pass http://127.0.0.1:{new_port};",
        text,
        count=1,
    )
    return updated, old_port


def upstream_ports(text):
    return [int(match.group(2)) for match in PROXY_RE.finditer(text)]


def apply_site_update(site_path, backup_dir, new_port):
    """Back up the live site file, then update only its proxy_pass line."""
    original = _read_text(site_path)
    updated, old_port = render_proxy_pass(original, new_port)
    os.makedirs(backup_dir, mode=0o755, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    fd, backup = tempfile.mkstemp(prefix=f"sre-platform-nginx-{stamp}-", suffix=".conf", dir=backup_dir)
    os.chmod(backup, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(original)
    if updated != original:
        _write_text(site_path, updated)
    return backup, old_port


def restore_site(site_path, backup):
    _write_text(site_path, _read_text(backup))


def local_health_url(port):
    if port == 80:
        return "http://127.0.0.1/health"
    # 8000 is the systemd process. Candidate ports stay in the localhost range.
    if port == 8000 or (isinstance(port, int) and PORT_MIN <= port <= PORT_MAX):
        return f"http://127.0.0.1:{port}/health"
    raise DeployError("refusing a health check on an unexpected port", 6)


def _read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _write_text(path, text):
    temporary = path + ".tmp"
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _matches_digest(live, digest):
    if live.get("digest") == digest:
        return True
    return digest in live.get("digests", ())


def _already_serving(backend, live, digest):
    if not live or not _matches_digest(live, digest) or not live.get("running"):
        return False
    if live.get("host_ip") != "127.0.0.1":
        raise DeployError("live container is not bound to localhost", 6)
    if not live.get("healthy"):
        return False
    if backend.http_status(local_health_url(live["port"])) != 200:
        return False
    if backend.http_status(local_health_url(80)) != 200:
        return False
    effective = backend.nginx_upstream_ports()
    return effective == [live["port"]]


def _remove_candidate(backend, name, live):
    if live and live.get("name") == name:
        return
    backend.remove_container(name)
    log("rollback", action="remove-candidate", container=name)


def _ensure_restart(backend, name):
    backend.set_restart(name, "unless-stopped")
    log("restart_policy", container=name, policy="unless-stopped")


def _retire_previous(backend, keep_name, keep_digests):
    """Stop other running platform containers only after the new one is verified.

    The container just stopped is kept for rollback. Older stopped platform
    containers, and platform images no remaining container uses, are removed.
    Images and containers outside this application are left alone.
    """
    stopped_now = []
    for name in backend.other_platform_containers(keep_name):
        backend.set_restart(name, "no")
        backend.stop_container(name)
        stopped_now.append(name)
        current = backend.container(name)
        if current and current.get("digest"):
            keep_digests.add(current["digest"])
        log("retire_previous", container=name, action="stop")
    # A repeated deploy did not replace the active container, so the stopped
    # rollback container stays. A new deploy removes older stopped leftovers.
    if stopped_now:
        kept = {keep_name, *stopped_now}
        for name in backend.stopped_platform_containers():
            if name in kept:
                continue
            backend.remove_stopped_container(name)
            log("retire_previous", container=name, action="remove-old")
    backend.remove_unused_images(keep_digests)


def _container_running(backend, name):
    current = backend.container(name)
    return bool(current and current.get("running"))


def _restore_previous(backend, live, backup, switched):
    """Put Nginx and the previous container back, then check they answer.

    Returns False when the previous endpoint is not healthy. A previous
    container that is stopped is started again. Its image digest is compared
    with the digest recorded at the start of the deploy.
    """
    expected_port = live["port"] if live else None
    try:
        if backup is not None:
            restore_site(backend.site_path, backup)
            if expected_port is None:
                ports = upstream_ports(_read_text(backend.site_path))
                expected_port = ports[0] if len(ports) == 1 else None
    except Exception as exc:
        log("rollback", result="failed", detail=safe_text(exc))
        return False

    container_ok = True
    try:
        if live and not _container_running(backend, live["name"]):
            backend.start_stopped(live["name"])
            log("rollback", action="start-previous", container=live["name"], digest=live.get("digest", ""))
        if live:
            current = backend.container(live["name"])
            if not current or not current.get("running"):
                container_ok = False
            elif live.get("digest") and not _matches_digest(current, live["digest"]):
                log("rollback", result="digest-mismatch", container=live["name"])
                container_ok = False
            elif backend.http_status(local_health_url(live["port"])) != 200:
                container_ok = False
    except Exception as exc:
        log("rollback", result="failed", detail=safe_text(exc))
        container_ok = False

    nginx_ok = True
    if switched:
        try:
            backend.nginx_test()
            backend.nginx_reload()
        except Exception as exc:
            log("rollback", result="nginx-restore-failed", detail=safe_text(exc))
            nginx_ok = False
    try:
        if expected_port is not None and backend.nginx_upstream_ports() != [expected_port]:
            nginx_ok = False
        elif backend.http_status(local_health_url(80)) != 200:
            nginx_ok = False
    except Exception as exc:
        log("rollback", result="failed", detail=safe_text(exc))
        nginx_ok = False
    return container_ok and nginx_ok


def _abort_cutover(backend, live, backup, candidate, switched, reason, code, detail):
    restored = _restore_previous(backend, live, backup, switched)
    if candidate and (not live or candidate != live.get("name")):
        try:
            backend.remove_container(candidate)
            log("rollback", action="remove-candidate", container=candidate)
        except Exception as exc:
            log("rollback", action="remove-candidate", result="failed", detail=safe_text(exc))
            restored = False
    if not restored:
        log("rollback", reason=reason, result="unhealthy", detail=safe_text(detail))
        raise DeployError("rollback could not restore health", 12)
    if live:
        backend.set_restart(live["name"], "unless-stopped")
        log("restart_policy", container=live["name"], policy="unless-stopped")
    messages = {
        7: "nginx validation failed",
        8: "nginx reload failed",
        9: "nginx health check failed",
    }
    log("rollback", reason=reason, result="restored", detail=safe_text(detail))
    raise DeployError(messages[code], code)


class DeployLock:
    """Exclusive flock held for the whole deploy and released on every path."""

    def __init__(self, path):
        self.path = path
        self.handle = None

    def acquire(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise DeployError("another deployment is already running", 11) from None
        self.handle = handle

    def release(self):
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def deploy(backend, digest, lock_path=LOCK_PATH):
    """Deploy one digest while holding an exclusive lock.

    The lock is released on success and on every failure. A second deploy
    that cannot take the lock exits 11 without changing Nginx or containers.
    """
    if not DIGEST_RE.fullmatch(digest or ""):
        raise DeployError("image digest must be sha256 and 64 hex characters", 2)
    lock = DeployLock(lock_path)
    lock.acquire()
    try:
        return _deploy_locked(backend, digest)
    finally:
        lock.release()


def _deploy_locked(backend, digest):
    """Pull one digest, health-check a localhost candidate, then reload nginx.

    The container nginx already serves stays running until the new candidate
    answers through nginx. Its digest is recorded and is not deleted.
    """
    log("start", digest=digest, repository=REPOSITORY, region=REGION)

    live = backend.live_container()
    previous = _snapshot(live)
    if previous:
        log("previous", container=previous["name"], digest=previous["digest"], port=previous["port"])
    if _already_serving(backend, live, digest):
        _ensure_restart(backend, live["name"])
        _retire_previous(backend, live["name"], _keep_digests(digest, previous))
        log("noop", digest=digest, container=live["name"], port=live["port"])
        return {"result": "noop", "container": live["name"], "port": live["port"]}

    if backend.memory_available_mib() < MIN_AVAILABLE_MIB:
        raise DeployError("not enough free memory to start a second container", 3)
    if backend.disk_available_mib() < MIN_DISK_MIB:
        raise DeployError("not enough free disk to pull an image", 3)

    config_dir = None
    try:
        host, image = image_ref(backend.account_id(), digest)
        config_dir = backend.ecr_login(host)
        log("login", result="ok")
        backend.pull(config_dir, image)
        log("pull", digest=digest, result="ok")
        platform, user = backend.image_identity(image)
        if platform != "linux/amd64":
            raise DeployError("refusing image platform " + platform, 5)
        if user != "appuser":
            raise DeployError("refusing image user " + (user or "empty"), 5)
        log("inspect", platform=platform, user=user, result="ok")
    except DeployError:
        raise
    except Exception as exc:
        raise DeployError("image pull failed: " + safe_text(exc), 4) from None
    finally:
        if config_dir is not None:
            backend.cleanup_login(config_dir)
            config_dir = None
            log("cleanup", target="registry-credentials", result="removed")

    name = container_name(digest)
    started = False
    promoted = False
    switched = False
    backup = None
    try:
        port, started = _prepare_candidate(backend, live, name, image, digest)
        backup, _old_port = apply_site_update(backend.site_path, backend.backup_dir, port)
        log("nginx_prepare", port=port, backup=os.path.basename(backup))
        try:
            backend.nginx_test()
        except Exception as exc:
            _abort_cutover(backend, previous, backup, name, False, "nginx-test", 7, exc)
        log("nginx_test", result="ok")
        switched = True
        try:
            backend.nginx_reload()
        except Exception as exc:
            _abort_cutover(backend, previous, backup, name, True, "nginx-reload", 8, exc)
        log("nginx_reload", result="ok")
        if backend.nginx_upstream_ports() != [port] or backend.http_status(local_health_url(80)) != 200:
            _abort_cutover(backend, previous, backup, name, True, "public-health", 9, "health check failed")
        log("public_health", result="ok", port=port)
        promoted = True
        _ensure_restart(backend, name)
        _retire_previous(backend, name, _keep_digests(digest, previous))
        running = [
            item["name"] for item in backend.running_platform_containers()
        ]
        if running != [name]:
            log("error", result="more than one platform container is running", containers=",".join(running))
            raise DeployError("more than one platform container is running", 10)
        log("success", digest=digest, container=name, port=port)
        return {"result": "deployed", "container": name, "port": port}
    except DeployError:
        raise
    except Exception as exc:
        if promoted:
            log("error", result="container lifecycle failed after cutover", detail=safe_text(exc))
            raise DeployError("container lifecycle failed after cutover", 10) from None
        if backup is not None or previous:
            restored = _restore_previous(backend, previous, backup, switched)
            if started:
                _remove_candidate(backend, name, previous)
            if not restored:
                raise DeployError("rollback could not restore health", 12) from None
        elif started:
            _remove_candidate(backend, name, previous)
        log("rollback", reason="candidate", detail=safe_text(exc))
        raise DeployError(safe_text(exc), 6) from None


def _snapshot(live):
    if not live:
        return None
    return {
        "name": live["name"],
        "digest": live.get("digest") or "",
        "digests": list(live.get("digests") or ()),
        "port": live["port"],
    }


def _keep_digests(digest, previous):
    kept = {digest}
    if previous and previous.get("digest"):
        kept.add(previous["digest"])
    for extra in (previous or {}).get("digests") or ():
        if extra:
            kept.add(extra)
    return kept


def _prepare_candidate(backend, live, name, image, digest):
    if backend.memory_available_mib() < MIN_AVAILABLE_MIB:
        raise DeployError("not enough free memory to start a second container", 3)
    existing = backend.container(name)
    if existing and live and existing.get("name") == live.get("name"):
        raise DeployError("live container failed its health check", 6)
    if existing and not (existing.get("running") and existing.get("healthy")):
        backend.remove_container(name)
        existing = None
    if existing and backend.http_status(local_health_url(existing["port"])) == 200:
        log("reuse_candidate", container=name, port=existing["port"])
        return existing["port"], False
    if existing:
        backend.remove_container(name)
    used = set()
    if live:
        used.add(live["port"])
    port = backend.allocate_port(used)
    backend.start_container(container_run_args(name, image, port))
    log("start_container", container=name, port=port, publish=f"127.0.0.1:{port}:8000", restart="no")
    if not backend.wait_healthy(name) or backend.http_status(local_health_url(port)) != 200:
        _remove_candidate(backend, name, live)
        log("rollback", reason="health", container=name)
        raise DeployError("candidate health check failed", 6)
    log("candidate_health", container=name, port=port, result="ok")
    return port, True


class RealBackend:
    """Host operations. Used only when this file is executed on the instance."""

    def __init__(self, site_path=NGINX_SITE, backup_dir=BACKUP_DIR):
        self.site_path = site_path
        self.backup_dir = backup_dir

    def account_id(self):
        account = self._run(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"]).strip()
        if not re.fullmatch(r"\d{12}", account):
            raise DeployError("instance role did not return an account id", 4)
        return account

    def ecr_login(self, registry_host):
        if not re.fullmatch(r"\d{12}\.dkr\.ecr\.us-east-2\.amazonaws\.com", registry_host):
            raise DeployError("refusing unexpected registry host", 4)
        config_dir = tempfile.mkdtemp(prefix="sre-ecr-")
        os.chmod(config_dir, 0o700)
        try:
            password = subprocess.run(
                ["aws", "ecr", "get-login-password", "--region", REGION],
                check=True, capture_output=True,
            )
            login = subprocess.run(
                ["docker", "--config", config_dir, "login", "--username", "AWS", "--password-stdin", registry_host],
                input=password.stdout, capture_output=True,
            )
            if login.returncode != 0:
                raise RuntimeError(safe_text(login.stderr.decode("utf-8", "replace")))
            return config_dir
        except Exception:
            shutil.rmtree(config_dir, ignore_errors=True)
            raise

    def cleanup_login(self, config_dir):
        if config_dir and os.path.isdir(config_dir) and os.path.basename(config_dir).startswith("sre-ecr-"):
            shutil.rmtree(config_dir, ignore_errors=True)

    def pull(self, config_dir, image):
        self._run(["docker", "--config", config_dir, "pull", image])

    def image_identity(self, image):
        raw = self._run([
            "docker", "image", "inspect", "--format",
            "{{.Os}}/{{.Architecture}}\t{{.Config.User}}", image,
        ]).strip()
        platform, _, user = raw.partition("\t")
        return platform, user

    def memory_available_mib(self):
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
        raise DeployError("MemAvailable is missing", 3)

    def disk_available_mib(self):
        stats = os.statvfs("/")
        return int(stats.f_bavail * stats.f_frsize / 1024 / 1024)

    def live_container(self):
        ports = upstream_ports(_read_text(self.site_path))
        if len(ports) != 1:
            raise DeployError("nginx site does not have one localhost upstream", 7)
        return self._container_on_port(ports[0])

    def container(self, name):
        if not name.startswith(CONTAINER_PREFIX):
            raise DeployError("refusing an unexpected container name", 6)
        found = self._inspect_names([name])
        return found[0] if found else None

    def other_platform_containers(self, keep_name):
        return [name for name in self._platform_names(running_only=True) if name != keep_name]

    def running_platform_containers(self):
        names = self._platform_names(running_only=True)
        return [{"name": name} for name in names]

    def stopped_platform_containers(self):
        raw = self._run(["docker", "ps", "-a", "--filter", "status=exited", "--format", "{{.Names}}"])
        return [name for name in raw.split() if name.startswith(CONTAINER_PREFIX)]

    def start_stopped(self, name):
        if not name.startswith(CONTAINER_PREFIX):
            raise DeployError("refusing to start an unrelated container", 12)
        self._run(["docker", "start", name])

    def remove_stopped_container(self, name):
        if not name.startswith(CONTAINER_PREFIX):
            raise DeployError("refusing to remove an unrelated container", 10)
        self._run(["docker", "rm", name])

    def remove_unused_images(self, keep_digests):
        """Remove sre-platform images no remaining container uses.

        Only repository@digest references for this repository are removed.
        Other repositories are left in place.
        """
        used = set(keep_digests)
        for name in self._platform_names(running_only=False):
            current = self.container(name)
            if not current:
                continue
            if current.get("digest"):
                used.add(current["digest"])
            used.update(current.get("digests") or ())
        listed = self._run([
            "docker", "image", "ls", "--digests", "--no-trunc",
            "--format", "{{.Repository}}\t{{.Digest}}",
        ])
        for line in listed.splitlines():
            repository, _, digest = line.partition("\t")
            if repository != REPOSITORY and not repository.endswith("/" + REPOSITORY):
                continue
            if not DIGEST_RE.fullmatch(digest) or digest in used:
                continue
            self._run(["docker", "rmi", repository + "@" + digest])

    def _platform_names(self, running_only):
        command = ["docker", "ps", "--format", "{{.Names}}"]
        if not running_only:
            command.insert(2, "-a")
        raw = self._run(command)
        return [name for name in raw.split() if name.startswith(CONTAINER_PREFIX)]

    def allocate_port(self, used):
        listening = self._run(["ss", "-lnt"])
        for port in range(PORT_MIN, PORT_MAX + 1):
            if port in used:
                continue
            if re.search(rf":{port}(?![0-9])", listening):
                continue
            return port
        raise DeployError("no free localhost port in 18000-18019", 6)

    def start_container(self, args):
        if args[0] != "docker" or "127.0.0.1:" not in " ".join(args) or "0.0.0.0:" in " ".join(args):
            raise DeployError("refusing a container publish that is not localhost-only", 6)
        self._run(args)

    def wait_healthy(self, name):
        deadline = time.time() + HEALTH_TIMEOUT_SECONDS
        while time.time() < deadline:
            status = self._run([
                "docker", "inspect", "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}", name,
            ]).strip()
            if status == "healthy":
                return True
            if status == "unhealthy":
                return False
            time.sleep(2)
        return False

    def http_status(self, url):
        parsed_host = urllib.request.urlparse(url).hostname
        if parsed_host not in ("127.0.0.1", "localhost"):
            raise DeployError("refusing a non-local health check", 6)
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read(4096)
                if response.status == 200 and b'"status"' in body and b"healthy" in body:
                    return 200
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except Exception:
            return 0

    def nginx_test(self):
        self._run(["nginx", "-t"])

    def nginx_reload(self):
        self._run(["systemctl", "reload", "nginx"])

    def nginx_upstream_ports(self):
        dumped = self._run(["nginx", "-T"])
        return upstream_ports(dumped)

    def set_restart(self, name, policy):
        if policy not in ("no", "unless-stopped"):
            raise DeployError("refusing restart policy " + policy, 10)
        self._run(["docker", "update", "--restart", policy, name])

    def stop_container(self, name):
        if not name.startswith(CONTAINER_PREFIX):
            raise DeployError("refusing to stop an unrelated container", 10)
        self._run(["docker", "stop", name])

    def remove_container(self, name):
        if not name.startswith(CONTAINER_PREFIX):
            raise DeployError("refusing to remove an unrelated container", 6)
        self._run(["docker", "rm", "-f", name])

    def _container_on_port(self, port):
        raw = self._run(["docker", "ps", "--format", "{{.Names}}"])
        for item in self._inspect_names(raw.split()):
            if item["port"] == port:
                return item
        return None

    def _inspect_names(self, names):
        found = []
        for name in names:
            if not name.startswith(CONTAINER_PREFIX):
                continue
            raw = self._run(["docker", "inspect", name], check=False)
            if not raw.strip():
                continue
            data = json.loads(raw)[0]
            bindings = data.get("NetworkSettings", {}).get("Ports", {}).get("8000/tcp") or []
            host_ip = bindings[0].get("HostIp", "") if bindings else ""
            host_port = int(bindings[0].get("HostPort", "0")) if bindings else 0
            if bindings and host_ip != "127.0.0.1":
                raise DeployError("a platform container is published beyond localhost", 6)
            digests = []
            configured = data.get("Config", {}).get("Image", "")
            if "@" in configured:
                candidate = configured.rsplit("@", 1)[1]
                if DIGEST_RE.fullmatch(candidate):
                    digests.append(candidate)
            for extra in self._image_digests(data.get("Image", "")):
                if extra not in digests:
                    digests.append(extra)
            found.append({
                "name": name,
                "digest": digests[0] if digests else "",
                "digests": digests,
                "port": host_port,
                "host_ip": host_ip or "127.0.0.1",
                "running": data.get("State", {}).get("Running", False),
                "healthy": (data.get("State", {}).get("Health") or {}).get("Status") == "healthy",
                "restart": data.get("HostConfig", {}).get("RestartPolicy", {}).get("Name", ""),
            })
        return found

    def _image_digests(self, image_id):
        if not image_id:
            return []
        raw = self._run(["docker", "image", "inspect", image_id], check=False)
        if not raw.strip():
            return []
        found = []
        for repo_digest in json.loads(raw)[0].get("RepoDigests") or []:
            candidate = repo_digest.rsplit("@", 1)[-1]
            if DIGEST_RE.fullmatch(candidate) and candidate not in found:
                found.append(candidate)
        return found

    def _run(self, args, check=True):
        proc = subprocess.run(args, capture_output=True)
        if check and proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace") or proc.stdout.decode("utf-8", "replace")
            raise RuntimeError(safe_text(detail))
        if proc.returncode != 0:
            return ""
        return proc.stdout.decode("utf-8", "replace")


def main(argv):
    parser = argparse.ArgumentParser(description="Deploy one sre-platform image digest.")
    parser.add_argument("--digest", required=True)
    args = parser.parse_args(argv)
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        log("error", result="root is required")
        return 1
    try:
        deploy(RealBackend(), args.digest)
    except DeployError as exc:
        log("error", result=exc.message, code=exc.code)
        return exc.code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
