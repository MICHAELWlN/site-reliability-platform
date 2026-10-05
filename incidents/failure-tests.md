# Failure Testing Report

This document records the primary reliability tests performed against the deployed AWS environment.

## Test 1 — Automatic Process Recovery

### Purpose

Verify that systemd automatically recovers the application from an unexpected Uvicorn process failure.

### Failure Injection

The main service process was forcibly terminated with SIGKILL to simulate an unexpected process crash.

### Expected Behavior

Because `sre-service` is configured with:

- `Restart=on-failure`
- `RestartSec=5`

systemd should recognize the unexpected termination as a failure and automatically start a replacement process.

### Result

The test passed:

- the Uvicorn process was forcibly terminated
- systemd detected the process failure
- systemd automatically restarted the service
- `sre-service` returned to the active state
- `/health` returned HTTP 200 after recovery

This demonstrates automatic process-level recovery without manual service restoration.

## Test 2 — End-to-End Controlled Outage

### Purpose

Verify the complete monitoring, alerting, notification, restoration, and recovery path.

### Healthy State

Before failure injection:

- `sre-service` was active
- Nginx successfully proxied `/health` to Uvicorn
- `/health` returned HTTP 200
- the synthetic monitor reported successful checks
- healthy measurements were published to CloudWatch

### Failure Injection

The application was intentionally stopped with:

```bash
sudo systemctl stop sre-service
