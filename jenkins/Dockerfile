# Jenkins LTS controller for the local CI pipeline.
# It adds only what the Jenkinsfile needs: git, python3 + venv, curl, the Docker CLI,
# and two plugins (Pipeline and Git).

# Docker CLI only (static binary). The daemon is the separate dind sidecar, not this image.
FROM docker:29-cli AS dockercli

FROM jenkins/jenkins:lts-jdk21

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 python3-venv curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

# The repository's .git directory is mounted read-only at /srv/repo.git by a different
# owner, which Git would otherwise refuse to read.
RUN git config --system --add safe.directory /srv/repo.git

USER jenkins
RUN jenkins-plugin-cli --plugins workflow-aggregator git
