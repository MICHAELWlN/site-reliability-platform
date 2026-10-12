# Project Explained

## Purpose

This project is a small end-to-end site reliability engineering exercise.

The application itself is intentionally simple: FastAPI exposes a `/health` endpoint. The main focus is the reliability layer around the application—deployment, process management, reverse proxying, synthetic monitoring, observability, alerting, failure testing, and recovery.

## Request Path

The deployed request path is:

```text
Internet -> Nginx :80 -> Uvicorn 127.0.0.1:8000 -> FastAPI /health
```

Nginx accepts public HTTP traffic on port 80 and proxies requests to Uvicorn.

Uvicorn listens only on localhost, so the application server is not directly exposed to the internet.

## Service Management

The application runs under systemd using `deploy/sre-service.service`.

The unit:

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

`monitor/monitor.py` independently checks the health endpoint.

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

Metrics:

- `Availability`
- `LatencyMs`
- `Failures`

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

## Additional Tooling

The sections above describe the AWS EC2 deployment, which was built and configured by hand. The tools below are
separate from that instance. Docker and Ansible stay local. Terraform's EC2 demo was one isolated security group,
since destroyed. Jenkins publishes images to a private ECR repository and does not deploy them to EC2.

### Docker (local)

`Dockerfile` builds a container image of the FastAPI service. Verified locally: the image built, the container's
health status became `healthy`, `GET /health` returned HTTP 200, and the container ran as a non-root user.
The EC2 deployment still runs under systemd.

### Jenkins (local)

A local Jenkins LTS with a Docker-in-Docker sidecar runs `Jenkinsfile`. The original local stages still run first:
checkout, dependency install, the 4 unit tests, image build, a temporary container, health verification, and
cleanup. After those pass, the pipeline checks that it is using `sre-jenkins-deploy-role`, publishes a
`linux/amd64` image to the private ECR repository `sre-platform`, and checks the digest, architecture, and
source revision label. The image tag is the full commit SHA. An existing tag is reused only when that digest
matches a recorded or operator-supplied value. The pipeline does not pull or run anything on EC2.

Build #2 on `sre-platform-ci` succeeded for the local stages, before publishing existed. Build #4 on the
temporary job `sre-platform-ci-ecr-test` succeeded for the publish path: 4 tests passed, the temporary container
was healthy, and ECR stored tag `110adbe1f0cdd3e355486b3dd415f5dea97a06c5` at digest
`sha256:5f6ab0e1caf0bc49ff1b9cb6cc14885a10b60847b8e4096405b00d7eee70755d` (`linux/amd64`, revision label
matched). That tag is a snapshot commit kept inside Jenkins, not a commit in this repository. Credentials and
the temporary container were removed afterwards. See `jenkins/README.md`.

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

A Datadog Synthetic HTTP test checks `/health` on the EC2 endpoint every 15 minutes from one AWS Ohio location.
Two executions passed, all three assertions passed, and the monitor status is OK. Failure alerting and recovery
have not been verified. See `docs/datadog-monitoring.md`.

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
- automated deployment to EC2 (Jenkins publishes an image to ECR and does not pull or run it on EC2)
- centralized log aggregation
- verified external alerting (a Datadog synthetic check exists, but failure alerting and recovery have not been verified)
- application authentication
- dependency-level health checks

The current IAM permissions can also be tightened further toward least privilege.

## Future Improvements

Logical next steps include:

- extending infrastructure as code to the EC2 deployment
- configuration management of the EC2 instance
- least-privilege IAM
- TLS
- automated deployment to EC2 from CI
- centralized logging
- verifying Datadog failure alerting and recovery
- additional failure scenarios
- dependency-aware health checks
- automated integration testing
