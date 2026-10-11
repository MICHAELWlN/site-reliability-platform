# Local Jenkins CI/CD

A minimal Jenkins LTS setup that runs `../Jenkinsfile` locally on Docker Desktop.
It does not touch AWS, the EC2 instance, systemd, or Nginx.

## Pipeline

| Stage | What it does |
| --- | --- |
| Checkout | `checkout scm` of the committed branch |
| Install Dependencies | venv + `pip install -r requirements.txt` |
| Test | `python -m unittest discover -s tests -v` |
| Build Image | `docker build` using the repository `Dockerfile` |
| Deploy Temporary Container | `docker run -d` on port 18100 |
| Verify Health | waits for the Docker `HEALTHCHECK`, then requires `GET /health` = HTTP 200 and `{"status":"healthy"}` |
| post: always | prints container logs, removes the container and image |

Any failing stage fails the build. Cleanup runs whether the build passes or fails.

## Architecture and security

```text
macOS host
  127.0.0.1:8080  ->  [sre-jenkins]  --TLS (2376)-->  [sre-jenkins-dind]
                       Jenkins LTS                     separate Docker daemon
                       no socket, unprivileged         privileged, no published ports,
                       .git mounted read-only          no host bind mounts
```

- Jenkins listens on `127.0.0.1:8080` only. The agent port (50000) is not published.
- The host Docker socket is **not** mounted anywhere.
- Jenkins builds and runs containers on a separate Docker-in-Docker daemon over TLS. The pipeline never sees your other containers or images.
- The dind sidecar must run `privileged`. This is the main residual risk: a compromise of that container is root in Docker Desktop's Linux VM. It is limited by having no published ports and no host bind mounts.
- The only host path shared is `.git`, read-only. Jenkins can see committed code and history, not untracked files or `.env`.
- The temporary container is reachable only on the private compose network. It is not published to macOS or your LAN.
- No credentials are used or stored in the repository.

Jenkins only sees committed work. Commit the `Jenkinsfile` and `jenkins/` to the branch you build
(`sre-resume-sprint`) before the first build. A local commit is enough; no push is needed.

### Lab-only exception: `ALLOW_LOCAL_CHECKOUT`

`docker-compose.yml` sets this on the Jenkins controller:

```yaml
JAVA_OPTS: "-Dhudson.plugins.git.GitSCM.ALLOW_LOCAL_CHECKOUT=true"
```

**Why it is needed.** The job checks out `file:///srv/repo.git`, the read-only `.git` mount. By default
the Jenkins Git plugin refuses any remote that points at a local directory. Build #1 failed at Checkout
with: `Checkout of Git remote 'file:///srv/repo.git' aborted because it references a local directory,
which may be insecure.` Setting the property is what allowed Build #2 to check out the committed branch.

**Why it must not be enabled in untrusted Jenkins environments.** The restriction exists because anyone
who can create or configure a job could point the checkout at any directory the Jenkins process can
read on the controller's filesystem, such as `JENKINS_HOME` with its secrets and credentials, and pull
that content into a workspace or build output. This lab is acceptable only because Jenkins is bound to
`127.0.0.1`, has a single administrator, stores no credentials for the pipeline, and the only local
repository it is pointed at is the read-only `.git` mount. Do not copy this setting to a Jenkins that
other users or networks can reach. There, use a Git remote over HTTPS or SSH instead.

Authentication and the Groovy sandbox are unchanged by this setting.

## Start

From the repository root:

```bash
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"   # if `docker` is not found
docker compose -f jenkins/docker-compose.yml up -d --build
docker compose -f jenkins/docker-compose.yml ps
```

## First login

1. Print the one-time admin password in your own terminal (do not paste it into chat or commit it):

   ```bash
   docker exec sre-jenkins cat /var/jenkins_home/secrets/initialAdminPassword
   ```

2. Open <http://localhost:8080> and paste the password.
3. On "Customize Jenkins", choose **Select plugins to install**, then **None**. Pipeline and Git are already installed in the image.
4. Create your admin user and keep the defaults for the instance URL.

## Create the job

1. **New Item**, name `sre-platform-ci`, type **Pipeline**.
2. Under **Pipeline**, set Definition to **Pipeline script from SCM**.
3. SCM: **Git**. Repository URL: `file:///srv/repo.git`. No credentials.
4. Branch Specifier: `*/sre-resume-sprint`.
5. Script Path: `Jenkinsfile`.
6. **Save**, then **Build Now**.

## Verify

In the build's **Console Output** you should see, in order: the four unit tests passing, a successful `docker build`, a `healthy` status, `GET http://docker:18100/health -> HTTP 200` with `{"status":"healthy"}`, the cleanup section, and `Finished: SUCCESS`.

Confirm that the temporary container was removed from the dind daemon:

```bash
docker exec sre-jenkins-dind docker ps -a
docker exec sre-jenkins-dind docker images
```

Neither should list `sre-platform-ci-*` or `sre-platform:ci-*`.

## Verified results: Build #2 SUCCESS

Job `sre-platform-ci`, Build #2, commit `3e8d7ef` on `sre-resume-sprint`: **SUCCESS** (about 29 seconds).
This was read from the build record and console log in Jenkins. Build #1 had failed at Checkout
(see the exception above).

Stages that executed, in order:

1. **Checkout**: Git clone of `file:///srv/repo.git`, revision `3e8d7ef` on `sre-resume-sprint`, no credentials.
2. **Install Dependencies**: venv created and `requirements.txt` installed.
3. **Test**: `python -m unittest discover -s tests -v` ran 4 tests, all `ok`.
4. **Build Image**: `docker build -t sre-platform:ci-2 .` using the repository `Dockerfile`.
5. **Deploy Temporary Container**: `docker run -d --name sre-platform-ci-2 -p 18100:8000 sre-platform:ci-2`
   on the Docker-in-Docker daemon.
6. **Verify Health**: the Docker health status went from `starting` to `healthy`, then
   `GET http://docker:18100/health` returned HTTP 200 with `{"status":"healthy"}`.
7. **Cleanup** (`post: always`): the container `sre-platform-ci-2` and the image `sre-platform:ci-2` were removed.

Cleanup was checked independently afterwards on the Docker-in-Docker daemon: 0 containers (including
stopped ones), no `sre-platform` images, no volumes, and port 18100 closed. The only image left was the
cached base image `python:3.12-slim`. The macOS host Docker daemon was not used by the pipeline.

## Cleanup

Stop Jenkins and delete all of its state (jobs, users, dind images and cache):

```bash
docker compose -f jenkins/docker-compose.yml down -v
```

Optionally remove the downloaded images:

```bash
docker rmi sre-jenkins:lts jenkins/jenkins:lts-jdk21 docker:29-dind docker:29-cli
```

Use `down` without `-v` to keep the Jenkins configuration and dind cache between sessions.

## Limitations

- The tests run on the Python version in the Jenkins image (3.13). The application image uses Python 3.12.
- Dependencies in `requirements.txt` are not pinned, so builds are not fully reproducible.
- Jenkins uses a single built-in node and a local file path as the repository source. It does not use GitHub webhooks.
- There is no push or deploy stage. Nothing is published to a registry or AWS.
- Only the success path has been run. Failure paths (a failing test or health check, followed by cleanup) are designed in the `Jenkinsfile` but have not been exercised.
- `docker build` uses the legacy builder and prints a deprecation warning, because the Jenkins image has no `buildx`.
- The dind daemon is privileged (see above). Do not expose it or add published ports to the `docker` service.
- The initial admin account and job configuration are manual, one-time steps stored in the `jenkins-home` volume.
