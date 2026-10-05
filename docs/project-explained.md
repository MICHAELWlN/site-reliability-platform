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
- infrastructure as code
- automated deployment
- centralized log aggregation
- external monitoring from a separate host
- application authentication
- dependency-level health checks

The current IAM permissions can also be tightened further toward least privilege.

## Future Improvements

Logical next steps include:

- infrastructure as code
- least-privilege IAM
- TLS
- automated deployment
- centralized logging
- external monitoring
- additional failure scenarios
- dependency-aware health checks
- automated integration testing
