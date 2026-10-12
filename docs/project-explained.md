# Project Explained

## Purpose

This project is a small end-to-end site reliability engineering exercise.

The application itself is intentionally simple: FastAPI exposes a `/health` endpoint. The main focus is the reliability layer around the application—deployment, process management, reverse proxying, synthetic monitoring, observability, alerting, failure testing, and recovery.

## Request Path

The deployed request path is:

```text
Jenkins -> Docker -> ECR -> SSM SREPlatformDeploy -> EC2
Internet -> Nginx :80 -> container 127.0.0.1:18001 -> Uvicorn :8000 -> FastAPI /health
```

Nginx accepts public HTTP traffic on port 80 and proxies requests to the container Jenkins Build #5 deployed. That container listens only on `127.0.0.1:18001`.

`sre-service` still runs Uvicorn on `127.0.0.1:8000`. Nginx does not proxy to it. The process is the manual rollback target, and it is not directly exposed to the internet.

## Service Management

The original application process still runs under systemd using `deploy/sre-service.service`. The public path is the container deployed by Jenkins Build #5. The unit:

- runs as the Ubuntu user
- starts from the deployed repository
- uses the project's Python virtual environment
- runs Uvicorn on `127.0.0.1:8000`
- is enabled during normal system boot
- is configured with `Restart=on-failure`
- waits five seconds before a configured restart attempt

Boot persistence was verified by restarting the EC2 instance and confirming that the service returned active and `/health` returned HTTP 200.

Three separate reliability behaviors were tested.

### Automatic Process Recovery

The Uvicorn process was forcibly terminated with SIGKILL to simulate an unexpected process failure. systemd detected the failure and automatically restarted the service because the unit is configured with `Restart=on-failure`. After the restart, the service returned active and `/health` returned HTTP 200.

### Controlled Application Outage

The application was intentionally stopped with `systemctl stop sre-service`. Unlike an unexpected process crash, this administrative stop kept the service unavailable. This allowed the synthetic monitor, CloudWatch alarm, and SNS notification path to be tested. The service was then manually restored with `systemctl start sre-service`, and the monitor detected recovery.

### Boot Persistence

The EC2 instance was restarted and the enabled systemd service started during normal boot. The service returned active and `/health` returned HTTP 200.

These tests demonstrate three different behaviors: automatic recovery from an unexpected process failure, monitoring and alerting during a sustained application outage, and service persistence across host restart.

## Synthetic Monitor

`monitor/monitor.py` independently checks the health endpoint. `deploy/sre-monitor.service` is installed, active, and enabled. It checks `http://127.0.0.1/health` through Nginx and publishes CloudWatch metrics with the instance role. It does not use access keys. The script's default URL is still the systemd process on port 8000. The unit overrides that default, so the metrics follow the container Nginx is serving.

The monitor:

1. sends an HTTP health check
2. measures response duration
3. records a structured JSON event
4. tracks consecutive failures
5. retries failed checks
6. emits a local alert after three consecutive failures
7. limits repeated alerts using a cooldown
8. detects recovery
9. resets the consecutive failure count after recovery

## CloudWatch Metrics

With the `--cloudwatch` option enabled, the monitor publishes custom metrics through boto3.

CloudWatch namespace:

```text
SREPlatform
```

Metrics published from the Nginx check:

- `Availability` (`1` when the check is healthy, otherwise `0`)
- `LatencyMs` (check duration in milliseconds)
- `Failures` (`1` when the check fails, otherwise `0`)

The EC2 instance uses an IAM role for AWS access rather than storing AWS access keys in the repository.

## Observability and Alerting

A CloudWatch dashboard displays the custom application metrics.

The alert path is:

```text
Application failure
        |
        v
Synthetic monitor
        |
        v
CloudWatch custom metrics
        |
        v
CloudWatch alarm
        |
        v
Amazon SNS
        |
        v
Email notification
```

The SNS email subscription was confirmed and tested.

## Controlled Failure Test

This outage is historical. Nginx proxied to `sre-service` at the time. Current public traffic goes to the Docker container on `127.0.0.1:18001`. The results below are unchanged.

The complete monitoring and alerting path was tested by deliberately creating an application outage.

Before the outage:

- `sre-service` was active
- Nginx successfully proxied `/health`
- the endpoint returned HTTP 200
- the monitor reported healthy checks

The application service was then intentionally stopped.

During the outage:

- the endpoint became unavailable
- the monitor detected consecutive failures
- the local threshold alert fired after three failures
- failure metrics were published to CloudWatch
- the CloudWatch alarm entered its alarm state
- SNS delivered the alarm email

The service was then started again.

After restoration:

- the service returned active
- `/health` returned HTTP 200 through Nginx
- the monitor detected recovery
- the consecutive failure count reset to zero

Detailed results are documented in `incidents/failure-tests.md`.

This recording is live failure detection and manual recovery of `sre-service`. It is not a test of the deploy script. Automatic rollback is implemented in `deploy/ec2_deploy.py` and covered by `deploy/test_ec2_deploy.py` against a fake host. Build #5 did not fail a cutover, so that rollback path has not been exercised on the instance. After Build #5, Nginx serves the container on port 18001, and the installed monitor follows that Nginx path.

## Additional Tooling

The sections above describe the hand-built EC2 host and the recorded monitor outage. Ansible stays local.
Terraform's EC2 demo was one isolated security group, since destroyed. Jenkins publishes images to the private ECR
repository `sre-platform` and Build #5 deployed one verified digest through `SREPlatformDeploy` version 1.
Nginx serves that container on `127.0.0.1:18001`. The systemd service is still running for manual rollback.
See `deploy/ec2-deployment.md`.

### Docker (local)

`Dockerfile` builds a container image of the FastAPI service. Verified locally: the image built, the container's
health status became `healthy`, `GET /health` returned HTTP 200, and the container ran as a non-root user.
The systemd service is still running on the instance. Nginx now serves the container deployed by Jenkins Build #5.

### Jenkins (local)

A local Jenkins LTS with a Docker-in-Docker sidecar runs `Jenkinsfile`. The original local stages still run first:
checkout, dependency install, the 4 unit tests, image build, a temporary container, health verification, and
cleanup. After those pass, the pipeline checks that it is using `sre-jenkins-deploy-role`, publishes a
`linux/amd64` image to the private ECR repository `sre-platform`, and checks the digest, architecture, and
source revision label. The image tag is the full commit SHA. An existing tag is reused only when that digest
matches a recorded or operator-supplied value. The next stage sends that verified digest to the registered
document `SREPlatformDeploy` version 1. The document accepts only an `imageDigest` matching `sha256:` and 64
lowercase hex characters, and its only command is the installed host script. Jenkins does not send
`AWS-RunShellScript`.
See `deploy/ec2-deployment.md`.

Build #2 on `sre-platform-ci` succeeded for the local stages, before publishing existed. Build #4 on the
temporary job `sre-platform-ci-ecr-test` succeeded for the publish path: 4 tests passed, the temporary container
was healthy, and ECR stored tag `110adbe1f0cdd3e355486b3dd415f5dea97a06c5` at digest
`sha256:5f6ab0e1caf0bc49ff1b9cb6cc14885a10b60847b8e4096405b00d7eee70755d` (`linux/amd64`, revision label
matched). That tag is a snapshot commit kept inside Jenkins, not a commit in this repository. Credentials and
the temporary container were removed afterwards. EC2 was not modified by Build #4.

Build #5 on that same temporary job is the verified deployment. Snapshot
`1654be9a8e0e08ce4c480b01f18b41b62da88ed4` is also outside this repository's history. The 4 tests passed, the
temporary container was healthy, and ECR stored digest
`sha256:6951edc16cb827ad3c13814eaa9fb490ffb9f1402453e7ccad31de376ac48e8e`. SSM command
`a4a8d4f3-36a7-418b-80c1-b9c8382a554e` returned Success and exit 0. The new container passed its health check,
Nginx switched to `127.0.0.1:18001`, and the previous container was stopped and kept. Credentials were removed
afterwards. See `jenkins/README.md`.

### Terraform (isolated demonstration)

`terraform/demo/` defined one isolated security group with no rules. It was applied (1 added, 0 changed,
0 destroyed), verified in AWS, checked for drift (none), and then destroyed. Afterwards `terraform state list`
was empty and AWS returned `InvalidGroup.NotFound` for the group. Terraform does not manage the EC2 instance or
any other existing resource. See `terraform/demo/README.md`.

### Ansible (local)

`ansible/` configures Nginx on a throwaway Ubuntu container. The first run reported `changed=4`, the second
`changed=0`, `nginx -t` passed, and the endpoint returned HTTP 200. It does not configure the EC2 instance.
See `ansible/README.md`.

### Datadog (external)

A Datadog Synthetic HTTP test checks `/health` on the public EC2 endpoint every 15 minutes from one AWS Ohio
location. Nginx serves that endpoint from the Docker container on `127.0.0.1:18001`. Two executions passed, and
a controlled demonstration produced failed HTTP checks. A Datadog monitor status of ALERT, alert notifications,
and a Datadog recovery notification have not been recorded. Stopping `sre-service` does not fail this path.
Manual recovery of the public endpoint is separate from automatic deployment rollback. See `docs/datadog-monitoring.md`.

## Design Decisions

### Minimal Application

The FastAPI application is deliberately small so the project can focus on operating and observing the service rather than application development.

### Localhost-Bound Uvicorn

Uvicorn listens on `127.0.0.1:8000` instead of accepting public traffic directly.

Nginx acts as the public HTTP entry point.

### systemd

systemd provides service management, normal boot startup, and configured restart-on-failure behavior without introducing a container orchestrator into a small single-instance project.

### Separate Synthetic Monitor

The monitor checks the service independently rather than relying on the application to report its own reliability state.

### Consecutive-Failure Threshold

The monitor waits for three consecutive failed checks before generating its local threshold alert. This prevents a single transient failure from immediately producing an alert.

### CloudWatch and SNS

CloudWatch centralizes application-level metrics and evaluates the failure alarm.

SNS provides the notification path from the CloudWatch alarm to email.

### IAM Role

AWS API access is provided through the EC2 instance's IAM role instead of hard-coded credentials.

## What the Project Demonstrates

The project demonstrates a basic reliability lifecycle:

```text
Deploy
  ->
Observe
  ->
Detect failure
  ->
Alert
  ->
Restore
  ->
Verify recovery
```

Every major part of this path was configured and tested directly.

## Current Limitations

This is a learning and portfolio project rather than a production-ready platform.

It currently does not provide:

- TLS
- high availability or multiple EC2 instances
- infrastructure as code for the EC2 deployment (Terraform was demonstrated only on one isolated security group, since destroyed)
- configuration management of the EC2 instance (the Ansible demonstration targets a local container)
- a live exercise of the deploy script's automatic rollback (Build #5 completed a successful cutover)
- centralized log aggregation
- a recorded Datadog monitor status of ALERT (failed HTTP checks were observed; alert notifications and Datadog recovery were not)
- application authentication
- dependency-level health checks

The current IAM permissions can also be tightened further toward least privilege.

## Future Improvements

Logical next steps include:

- extending infrastructure as code to the EC2 deployment
- configuration management of the EC2 instance
- least-privilege IAM
- TLS
- a live exercise of automatic deploy rollback
- centralized logging
- verifying Datadog failure alerting and recovery
- additional failure scenarios
- dependency-aware health checks
- automated integration testing
