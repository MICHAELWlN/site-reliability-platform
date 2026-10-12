# Jenkins LTS controller for the local CI pipeline.
# It adds only what the Jenkinsfile and the planned ECR/SSM steps need: git, python3 + venv,
# curl, the Docker CLI with the Buildx plugin, AWS CLI v2, and two plugins (Pipeline and Git).

# Docker CLI and Buildx plugin only (static binaries). The daemon is the separate dind
# sidecar, not this image.
FROM docker:29-cli AS dockercli

# AWS CLI v2 from AWS's official image. It is a self-contained bundle (its own Python),
# so it is copied as-is. Pin the tag to upgrade deliberately.
FROM amazon/aws-cli:2.37.12 AS awscli

FROM jenkins/jenkins:lts-jdk21

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 python3-venv curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
# Buildx is a Docker CLI plugin. It talks to the same remote dind daemon over TLS and
# needs no extra privileges or mounts.
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx

COPY --from=awscli /usr/local/aws-cli /usr/local/aws-cli
RUN ln -s /usr/local/aws-cli/v2/current/bin/aws /usr/local/bin/aws

# Credential handoff (no secrets in the image). The config is root-owned and read-only. It
# makes the AWS CLI call the helper, which reads short-lived STS credentials from the
# tmpfs at /run/aws-creds and rejects expired ones. See jenkins/README.md.
RUN mkdir -p /usr/local/etc/aws
COPY --chmod=0644 aws-config /usr/local/etc/aws/config
COPY --chmod=0755 aws-credential-process.py /usr/local/bin/jenkins-aws-credential-process
COPY --chmod=0755 aws-creds-clear-in-container.sh /usr/local/bin/jenkins-aws-clear

# The repository's .git directory is mounted read-only at /srv/repo.git by a different
# owner, which Git would otherwise refuse to read.
RUN git config --system --add safe.directory /srv/repo.git

USER jenkins
RUN jenkins-plugin-cli --plugins workflow-aggregator git
