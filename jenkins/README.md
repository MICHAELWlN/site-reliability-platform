# Local Jenkins CI/CD

A minimal Jenkins LTS setup that runs `../Jenkinsfile` locally on Docker Desktop.
The first six stages are local only. The next three publish a `linux/amd64` image to Amazon ECR using
[temporary credentials](#aws-credential-handoff-temporary-credentials) that you load first
(see [Publishing to ECR](#publishing-to-ecr)). **Deploy to EC2** then sends the verified digest to SSM.
Build #5 of `sre-platform-ci-ecr-test` completed that path: Jenkins → Docker → ECR → SSM → EC2 → Nginx.
See `deploy/ec2-deployment.md`.

## Pipeline

| Stage | What it does |
| --- | --- |
| Checkout | `checkout scm` of the committed branch |
| Install Dependencies | venv + `pip install -r requirements.txt` |
| Test | `python -m unittest discover -s tests -v` |
| Build Image | `docker build` using the repository `Dockerfile` |
| Deploy Temporary Container | `docker run -d` on port 18100 |
| Verify Health | waits for the Docker `HEALTHCHECK`, then requires `GET /health` = HTTP 200 and `{"status":"healthy"}` |
| Verify AWS Deployment Role | reads the full commit SHA; `aws sts get-caller-identity` must be `assumed-role/sre-jenkins-deploy-role`; derives the ECR registry host |
| Publish Image to ECR | logs in with `--password-stdin`. If the SHA tag is absent, `docker buildx build --platform linux/amd64 --push`. If it already exists, reuse it only after the digest matches a publish record or `EXPECTED_IMAGE_DIGEST`. Never overwrites or deletes. |
| Verify Image in ECR | `ecr:DescribeImages` by tag, compares the digest, reads the image config as JSON and requires `linux/amd64` (and a matching revision label on a fresh push), writes `ecr-image-record.txt` |
| Deploy to EC2 | `jenkins/ssm_deploy_tool.py` sends that digest to registered `SREPlatformDeploy` version 1 on `i-0de56d01b124c0e69`, waits for Success and exit 0, and requires the script output to show Nginx `/health` was HTTP 200. Build #5 succeeded. |
| post: always | prints container logs, removes the container and images, deletes the registry login and the temporary AWS credentials |

Any failing stage fails the build, and the stages run in this order, so nothing is published unless the tests, the
image build and the local health test all passed. Cleanup runs whether the build passes or fails.

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

## AWS CLI and Buildx (preparation only)

The Jenkins image contains the tools for ECR publishing and, later, SSM. No AWS credentials are configured in
Jenkins by default. The publish stages use them only after you load them (see below).

- **AWS CLI v2** (`aws-cli/2.37.12`), copied from the official `amazon/aws-cli:2.37.12` image into
  `/usr/local/aws-cli` with `/usr/local/bin/aws` linked to it.
- **Docker Buildx** (v0.38.0), copied from `docker:29-cli` as a CLI plugin. It uses the same TLS connection to
  the dind daemon, so it adds no socket mount, privilege or port.
- **Credentials:** none are baked in or configured. `~/.aws` is not mounted and no credential variables are set.
  Short-lived credentials are loaded on demand, see the next section.

Rebuild and recreate only the controller, keeping the dind sidecar and all volumes:

```bash
docker compose -f jenkins/docker-compose.yml up -d --build --no-deps jenkins
```

Verify:

```bash
docker exec sre-jenkins aws --version
docker exec sre-jenkins docker version
docker exec sre-jenkins docker buildx version
```

### linux/amd64 builds

The dind daemon runs on Apple Silicon (`linux/arm64`) with the containerd image store, and the EC2 instance is
`linux/amd64`. Docker Desktop's emulation is shared with the sidecar, so a plain `docker build` produces an
arm64 image, and a target platform has to be requested:

```bash
docker buildx build --platform linux/amd64 --load -t sre-platform:amd64-test .
```

Verified on this setup: the repository `Dockerfile`, taken from the committed `.git`, built for `linux/amd64`
inside the Jenkins container. The image reported `linux/amd64`, reached `healthy`, and `/health` returned
`{"status":"healthy"}` with `platform.machine()` equal to `x86_64`. The test image and container were removed
afterwards. The build runs under emulation, so it is slower than a native build.

## AWS credential handoff (temporary credentials)

Lets the local Jenkins use the IAM role `sre-jenkins-deploy-role` with **short-lived STS credentials** from
your Mac's `sre-lab` profile. The handoff scripts only load, verify and clear credentials. The ECR stages of the
pipeline (next section) are the only consumer. No SSM commands are run.

### How it works

```text
Mac host                                              sre-jenkins container
aws sts assume-role (profile sre-lab)
   | stdout pipe                    docker exec -i
   +-------------------------------> sh -c 'cat > tmpfs file'  ---> /run/aws-creds/session.json
                                                                    (tmpfs, mode 0600, owner jenkins)
                                      aws CLI -> credential_process -> jenkins-aws-credential-process
                                                 reads the file, rejects expired/unsafe credentials
```

- **No disk, argv or environment.** The credentials go through a pipe into the container's stdin. They are not
  written on the Mac, not placed in any command-line argument or environment variable, and not printed.
- **In memory only.** `/run/aws-creds` is a `tmpfs` (mode `0700`, owned by `jenkins`, `noexec`, 1 MB). It is not on
  a volume, so it is not in `jenkins-home`, a job configuration, or Git. It disappears when the container restarts.
- **Read through `credential_process`.** The image's read-only `/usr/local/etc/aws/config` makes every `aws`
  call run `jenkins-aws-credential-process`. Nothing needs to be exported, so `env`, `ps` and `docker inspect`
  show no secrets. The Jenkins container only gets `AWS_CONFIG_FILE` (a path) and `AWS_EC2_METADATA_DISABLED`.
- **Expired credentials are rejected on every call.** The helper refuses a missing file, a symlink, a file not owned
  by the current user, a file readable by anyone else, malformed content, expired credentials, and credentials with
  less than 5 minutes left. Error messages never contain credential values.
- **Nothing extra mounted.** No Docker socket and no `~/.aws` mount. The host scripts use your normal `docker exec`.

### Commands (run on the Mac, from the repository root)

```bash
aws login --profile sre-lab          # only if the host login has expired
jenkins/aws-creds-handoff.sh         # assume the role and load credentials (default 1 hour)
jenkins/aws-creds-test.sh            # safe verification, see below
jenkins/aws-creds-clear.sh           # remove credentials when the deployment is finished
```

`jenkins/aws-creds-handoff.sh` reads the account ID from the profile, so no account ID is stored in Git. Set
`JENKINS_AWS_SESSION_SECONDS` (900 to 3600) for a shorter session, or `JENKINS_DEPLOY_ROLE_NAME` to change the role.
After loading, it checks `aws sts get-caller-identity` from inside Jenkins and removes the credentials again if
the identity is anything other than `assumed-role/sre-jenkins-deploy-role/...`. It prints only the role, session
name and expiry.

The pipeline runs `jenkins-aws-clear` in `post { always { ... } }`, so every build removes the credentials when it
finishes, pass or fail. Load them again before each build that should publish.

### Safe test

`jenkins/aws-creds-test.sh` has two parts. `--offline` runs only part 1.

1. **Offline rejection tests** with obviously fake values in a throwaway directory and no AWS calls: missing file,
   expired, under 5 minutes left, group/world-readable file, malformed file, and the AWS CLI itself refusing expired
   credentials. It also confirms a valid, unexpired fake file is accepted by the helper.
2. **Live check** inside Jenkins: `aws sts get-caller-identity` must return `assumed-role/sre-jenkins-deploy-role`.
   It then searches for the real access key, secret key and session token in `docker inspect` output, in every
   process command line and environment in the container, and under `/var/jenkins_home`, and confirms the
   directory is a tmpfs. A positive control proves the search can find the values. Only the identity ARN is printed.

### Verified results

Run on this machine with the `sre-lab` profile: the role was assumed with a 1-hour session, and Jenkins reported
`assumed-role/sre-jenkins-deploy-role/jenkins-lab-<timestamp>`. All offline and live checks passed, including the
positive control. After `jenkins/aws-creds-clear.sh`, `aws sts get-caller-identity` inside Jenkins failed locally
with "no credentials loaded", and the credential file was gone. The Jenkins controller was recreated for the
tmpfs, and the dind sidecar, volumes, job and builds #1 and #2 were unchanged.

### Security tradeoffs and limits

- **The file is readable by the `jenkins` user while it exists.** Anyone who can configure a job, run a pipeline
  or open a shell in the container can use or copy the credentials. Anyone with Docker access on your Mac can
  `docker exec` into the container. This is acceptable for a single-user lab only.
- **Clearing is not revocation.** Deleting the file removes Jenkins' copy. A copy already taken stays valid at AWS
  until it expires. Use a short session, and rely on the role's permissions for the real limit.
- **The role's permissions are the blast radius.** This work does not review or change that policy.
- **Interactive Mac authentication means this cannot run indefinitely unattended.** The `sre-lab` profile uses a
  login that expires and has to be renewed by you in a browser (`aws login`). Each session lasts at most 1 hour.
  When it expires, builds that call AWS fail until you run `jenkins/aws-creds-handoff.sh` again, so a nightly or
  webhook-triggered deployment will not work unattended. Truly unattended operation would need a different
  design, such as Jenkins running on AWS with an instance role. That is out of scope here.
- A container restart removes the credentials, so reload them after restarting Jenkins.
- Jenkins log masking is not involved. Credentials stay out of build logs because they are never in an environment
  variable or argument, but a pipeline step that deliberately reads the file could still print it.

## Publishing to ECR

**Status:** implemented in the `Jenkinsfile`. A real Jenkins build has published one image (see below). Nothing in
this section is deployment: **no image is pulled or run on EC2.**

### Run it

```bash
aws login --profile sre-lab                 # only if the host login has expired
jenkins/aws-creds-handoff.sh                # load temporary credentials (expire in 1 hour)
# In Jenkins: Build Now on sre-platform-ci
```

The job reads committed code from `.git`, so the `Jenkinsfile` change has to be committed on the branch the job
builds before a build can see it.

### What the publish stages do

1. **Verify AWS Deployment Role.** `git rev-parse HEAD` must give a full 40-character SHA. The identity must be
   `assumed-role/sre-jenkins-deploy-role`, otherwise the build fails before anything is published. The registry
   host is derived from the credentials at run time, so no account ID is stored in Git.
2. **Publish Image to ECR.**
   - Login is `aws ecr get-login-password | docker login --username AWS --password-stdin`. The token goes through
     a pipe and never appears in an argument. The login is written to `/run/aws-creds/docker-config` (the
     credential tmpfs), not to `JENKINS_HOME`.
   - The repository is `IMMUTABLE`. `DescribeImages` for the SHA tag decides the path:
     - **Tag absent** (`ImageNotFoundException`): `docker buildx build --platform linux/amd64 --provenance=false --sbom=false --label org.opencontainers.image.revision=<SHA> --tag <registry>/sre-platform:<SHA> --push`. The digest comes from the Buildx metadata file. A publish record is written immediately so a later verify failure cannot orphan the image.
     - **Tag present:** nothing is pushed, overwritten or deleted. Reuse is allowed only when the digest ECR holds matches a publish record in `JENKINS_HOME` or the optional `EXPECTED_IMAGE_DIGEST` parameter. Those two must agree if both are set. A mismatch, a missing expected digest, or an unreadable record fails the build. The decision is made by `jenkins/ecr_publish_tool.py decide-reuse`.
   - Re-running the same commit is therefore idempotent once a record (or an operator-attested digest) exists. A new commit produces a new tag.
3. **Verify Image in ECR.** `aws ecr describe-images --image-ids imageTag=<SHA>` must return the same digest the
   publish stage recorded. The image config is read with `docker buildx imagetools inspect --format '{{json .Image}}'`
   (not the default text listing) and checked by `jenkins/ecr_publish_tool.py check-config`: one `linux/amd64`
   image, no extra platform entries, and a revision label that matches the commit if present. A freshly pushed
   image must have that label. A reused image built before labels existed may have none, and is trusted only
   because its digest matched. The tag, digest, platform, mode and push time are written to `ecr-image-record.txt`,
   which is archived with the build. The publish record is marked `verified`.
4. **post: always.** Removes the temporary container and local images, including a local reference left by a
   push, deletes `/run/aws-creds/docker-config`, and runs `jenkins-aws-clear`. **Nothing in ECR is deleted.**

The tag is the full commit SHA and nothing else. There is no `latest` tag and nothing is ever overwritten.

### Security properties

- Credentials are never printed, never in the environment, arguments or job configuration. The `sh` steps that
  handle them start with `set +x`, and Docker's `--password` option is not used.
- The existing local stages need no credentials and still run first. A build without loaded credentials passes
  the first six stages and fails at **Verify AWS Deployment Role**.
- The pipeline never deletes the repository and never changes Terraform or its state.

### Real Jenkins results (test job `sre-platform-ci-ecr-test`)

Read from the build records. The existing job `sre-platform-ci` was not used.

- **Build #1:** the first six stages passed. The image was pushed as
  `sre-platform:43eb11cef129969bf70df72267950fb0b57ceac5` with digest
  `sha256:047d71df3dc9e93099c4d095926ea2a8b49f890b8f7534974b8ff7a6584081c1`. Verify then failed: the default
  `imagetools inspect` text listing was grepped for `Platform:.*linux/amd64` and did not match, even though the
  config of that digest is `linux/amd64` (later confirmed independently). Post-actions removed the local
  container and the local image reference. ECR was not deleted. Result: `FAILURE`, image left in ECR.
- **Build #2:** failed at **Verify AWS Deployment Role** because credentials had been cleared after Build #1.
  Nothing was pushed.
- **Build #3:** failed at **Publish Image to ECR** because the tag already existed and the pipeline of that
  revision treated an existing immutable tag as an error. Nothing was pushed, overwritten or deleted.

Reuse applies only to a later build of **that same commit**. The digest above is evidence for tag
`43eb11cef129969bf70df72267950fb0b57ceac5` only. A refreshed snapshot is a new commit and a new image tag; it
must take the publish path with `EXPECTED_IMAGE_DIGEST` left empty. Do not pass the old digest as evidence for a
different SHA.

The first reuse of `43eb11cef129969bf70df72267950fb0b57ceac5` itself (same commit, no publish record) still needs
**Build with Parameters** and
`EXPECTED_IMAGE_DIGEST=sha256:047d71df3dc9e93099c4d095926ea2a8b49f890b8f7534974b8ff7a6584081c1`. After that
succeeds, later builds of that same commit reuse the record and the parameter can stay empty.

### Build #4: SUCCESS

Temporary job `sre-platform-ci-ecr-test`, snapshot `110adbe1f0cdd3e355486b3dd415f5dea97a06c5`. The production job
`sre-platform-ci` was not used. `EXPECTED_IMAGE_DIGEST` was empty, so this new SHA was published rather than
matched to the older image.

- Checkout of that snapshot, then the 4 unit tests, `OK`.
- Temporary container `sre-platform-ci-4` became `healthy`. `GET http://docker:18100/health` returned HTTP 200.
- Identity was `assumed-role/sre-jenkins-deploy-role`.
- Pushed `sre-platform:110adbe1f0cdd3e355486b3dd415f5dea97a06c5`.
- Digest from Buildx, from `ecr:DescribeImages` during the build, and from a later read-only `DescribeImages`:
  `sha256:5f6ab0e1caf0bc49ff1b9cb6cc14885a10b60847b8e4096405b00d7eee70755d`.
- Verify stage recorded `platform=linux/amd64` and `revision_label=110adbe1f0cdd3e355486b3dd415f5dea97a06c5`.
- Publish record at `ecr-publish-records/sre-platform/110adbe1….record` is `verified`, source `pipeline-push`, build 4.
- Post actions removed the temporary container and the local image references, then cleared the AWS session.
  `/run/aws-creds/session.json` was gone afterwards. The Docker-in-Docker daemon had no leftover application containers.
- The earlier tag `43eb11cef129969bf70df72267950fb0b57ceac5` was still present with its original digest. EC2 was
  not modified: the build log has no EC2 or SSM calls, and the instance was still the same running instance.

The `sre-lab` user can describe images but cannot download the image config, so the architecture and revision
label are from the build's verify stage, tied to the digest confirmed by `DescribeImages`.

### Build #5: SUCCESS

Temporary job `sre-platform-ci-ecr-test`, snapshot `1654be9a8e0e08ce4c480b01f18b41b62da88ed4`. That snapshot is
the Jenkins test repository's `ecr-test` branch. It is not a commit in this project. The production job
`sre-platform-ci` was not used. `EXPECTED_IMAGE_DIGEST` was empty, so this SHA was published.

Verified workflow:

```text
Jenkins -> Docker -> ECR -> SSM SREPlatformDeploy -> EC2 -> Nginx
```

- Checkout of that snapshot, then the 4 unit tests, `OK`.
- Temporary container `sre-platform-ci-5` became `healthy`. `GET http://docker:18100/health` returned HTTP 200.
- Identity was `assumed-role/sre-jenkins-deploy-role`.
- Pushed `sre-platform:1654be9a8e0e08ce4c480b01f18b41b62da88ed4`.
- Digest from Buildx, from `ecr:DescribeImages` during the build, from archived `ecr-image-record.txt`, and from a later read-only `DescribeImages`:
  `sha256:6951edc16cb827ad3c13814eaa9fb490ffb9f1402453e7ccad31de376ac48e8e`.
- Verify stage recorded `platform=linux/amd64` and `revision_label=1654be9a8e0e08ce4c480b01f18b41b62da88ed4`.
- Deploy to EC2 sent only `SREPlatformDeploy` version 1. SSM command `a4a8d4f3-36a7-418b-80c1-b9c8382a554e` finished Success with response code 0 and empty stderr.
- The host script pulled that digest, started `sre-platform-6951edc16cb8` on `127.0.0.1:18001`, and recorded `candidate_health` result ok.
- Nginx was tested, reloaded, and `public_health` on port 18001 was ok. Nginx now proxies port 80 to `127.0.0.1:18001`.
- `sre-platform-candidate` was stopped and kept. It had been serving `sha256:5f6ab0e1caf0bc49ff1b9cb6cc14885a10b60847b8e4096405b00d7eee70755d` on port 18000. The invocation logged `retire_previous` with action `stop`, not a removal.
- Post actions removed the temporary Jenkins container and the local image references, then cleared the AWS session. ECR still has the image. The two earlier tags were still present.

This was a successful cutover. The script's automatic rollback is implemented and tested with a fake host. Build #5 did not take a failure path on the instance, so live automatic rollback was not verified.

### Static checks for the reuse change

Run from the repository root, no Jenkins, no AWS:

```bash
python3 -m unittest jenkins.test_ecr_publish_tool -v
```

These cover platform and revision-label checks, refuse-to-reuse cases (missing expected digest, mismatch,
record/operator disagreement), and the attested digest of the image already in ECR.

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
- **Deploy to EC2** was executed by Build #5. An earlier same-digest invocation of the document, made outside Jenkins, only refreshed the restart policy of the container that was already serving traffic. See `deploy/ec2-deployment.md`.
- Re-running a commit whose tag already exists reuses the image only after the digest and platform checks pass. It never overwrites or deletes. The first reuse of an image pushed before publish records existed needs `EXPECTED_IMAGE_DIGEST`.
- The ECR stages need credentials loaded with `jenkins/aws-creds-handoff.sh` before every build. They are removed when the build ends, and they expire after at most an hour.
- Build #5 ran the success path through Deploy to EC2. Failure paths (a failing test or health check, followed by cleanup) are designed in the `Jenkinsfile` but have not been exercised. Builds #1–#3 failed for other reasons and are described above. Live automatic rollback on the instance has not been exercised.
- Build #4 and Build #5 each ran `docker build` for the local health-check image and `docker buildx build --platform linux/amd64` for the published image.
- The published image has no provenance or SBOM attestations (`--provenance=false --sbom=false`), so it is a plain single manifest.
- The dind daemon is privileged (see above). Do not expose it or add published ports to the `docker` service.
- The initial admin account and job configuration are manual, one-time steps stored in the `jenkins-home` volume.
