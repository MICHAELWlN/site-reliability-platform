# Failure Testing Report

This document records the primary reliability tests performed against the deployed AWS environment.

Tests 1 and 2 below are historical. They were run when Nginx proxied to systemd unit `sre-service` on
`127.0.0.1:8000`. The results in those sections are unchanged.

The path verified by Jenkins Build #5, while the instance was running, is:

```text
Internet -> Nginx :80 -> Docker container 127.0.0.1:18001 -> FastAPI /health
```

The container served digest `sha256:6951edc16cb827ad3c13814eaa9fb490ffb9f1402453e7ccad31de376ac48e8e`.
Stopping `sre-service` does not interrupt that path. A later controlled manual failure injection on that
path is recorded in `docs/datadog-monitoring.md`: healthy checks were OK, failed HTTP checks followed,
the Datadog monitor went from OK to ALERT, and after manual application recovery it returned to OK.
The exact injection command is not recorded here. Automatic deployment rollback is implemented and
unit-tested. These historical tests restored `sre-service` by hand. They did not exercise that rollback.
The EC2 instance is now stopped, and the Datadog synthetic test is paused.

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
```

During the outage:

- The health endpoint became unavailable.
- The monitor recorded failed checks.
- The consecutive failure count increased.
- The monitor emitted its threshold alert after three consecutive failures.
- Failed checks published `Availability = 0` and `Failures = 1` to CloudWatch.
- The CloudWatch failure alarm entered the alarm state.
- Amazon SNS delivered the alarm notification to the confirmed email subscription.

### Recovery

The service was restored with:

`sudo systemctl start sre-service`

After recovery:

- systemd reported the service as active.
- Nginx again returned HTTP 200 from `/health`.
- The synthetic monitor detected the successful response.
- The consecutive failure count reset to zero.
- Healthy measurements resumed in CloudWatch.

### Result

The test demonstrated the complete monitoring and alerting path:

`Application outage -> synthetic check failure -> CloudWatch custom metric -> CloudWatch alarm -> SNS notification -> service restoration -> health-check recovery`

The test also confirmed that the monitoring system could distinguish a healthy state, sustained application failure, and subsequent recovery.
