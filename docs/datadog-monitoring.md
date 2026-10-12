# Datadog External Synthetic Monitoring

A Datadog Synthetic HTTP test checks the application's `/health` endpoint from outside AWS.
It adds an external view of availability to the on-host Python monitor and CloudWatch.

**Status in one line:** healthy checks were OK. Controlled manual failure injection produced failed HTTP
checks, the monitor went from OK to ALERT, and after manual application recovery it returned to OK. The
synthetic test is paused, and the EC2 instance is stopped. A Datadog notification delivery was not recorded.

## Where this configuration lives

The test was created in the Datadog web UI. Nothing in this repository defines, deploys, or configures it:
there is no Datadog agent on the EC2 instance, no Datadog Terraform, and no Datadog Ansible role. The
configuration below is documented by hand and could drift from what is in Datadog.

For security, this repository contains no Datadog API key, application key, account details, or the address
of the EC2 endpoint. The results below were recorded by the project owner from the Datadog UI. No Datadog
screenshots are committed.

## Current public path

While the instance was running, Nginx on port 80 proxied to the Docker container at `127.0.0.1:18001`. That
container is the digest deployed by Jenkins Build #5,
`sha256:6951edc16cb827ad3c13814eaa9fb490ffb9f1402453e7ccad31de376ac48e8e`. Datadog's public `GET /health`
followed that path. The EC2 instance is now stopped, and this synthetic test is paused.

`sre-service` listened on `127.0.0.1:8000`. Stopping it does not fail the public endpoint, because Nginx
does not route there. The earlier procedure `sudo systemctl stop sre-service` applied when Nginx proxied to
that systemd process. It is not the failure injection used for the Datadog demonstration. The exact command
for that demonstration is not recorded. It was controlled manual failure injection on the public path.

## Verified configuration

| Setting | Value |
| --- | --- |
| Test type | Synthetic HTTP test |
| Request | `GET /health` on the AWS EC2 application's public endpoint (address intentionally omitted) |
| Location | One location: AWS Ohio |
| Frequency | Every 15 minutes |
| Assertion: status code | Is `200` |
| Assertion: response time | Is below `2000 ms` |
| Assertion: `Content-Type` header | Is `application/json` |

## Actual test results

| Execution | Trigger | Result |
| --- | --- | --- |
| 1 | Scheduled (automatic, every 15 minutes) | Passed |
| 2 | Manually triggered | Passed |

Observed across both executions:

- HTTP status: `200 OK`
- Response time: approximately 7 to 10 ms
- Response-time assertion (below 2000 ms): passed
- `Content-Type: application/json` assertion: passed
- Datadog monitor status during those passing runs: **OK**

A later controlled manual failure injection produced failed HTTP checks from this same synthetic test.
After multiple failed checks the Datadog monitor went from **OK** to **ALERT**. The application was then
recovered by hand. The monitor returned from **ALERT** to **OK**.

That sequence is automated failure detection and manual runtime recovery. It is not the deploy script's
automatic rollback. That rollback is implemented in `deploy/ec2_deploy.py` and covered by
`deploy/test_ec2_deploy.py`. It has not been exercised on the instance.

No Datadog notification delivery is recorded. The exact injection command, timings, and failed-check count
beyond "multiple" are not recorded.

## How the three monitoring layers differ

| | Python synthetic monitor | CloudWatch | Datadog external monitoring |
| --- | --- | --- | --- |
| Role | Generates the health signal | Stores metrics, evaluates the alarm, notifies | Independent outside-in check |
| Where it runs | Process on the EC2 host (`monitor/monitor.py`) | AWS managed service | Datadog SaaS, from an AWS Ohio location |
| What it requests | `http://127.0.0.1/health` through Nginx, which proxies to the container on `127.0.0.1:18001` | Does not probe anything itself; it uses the monitor's metrics | The public endpoint, across the internet, which Nginx serves from that container |
| Check interval | Every 5 seconds by default, faster retries after a failure | Metrics arrive at the monitor's pace; alarm period is set in CloudWatch | Every 15 minutes |
| Output | JSON log events, a local alert after 3 consecutive failures | Custom metrics `Availability`, `LatencyMs`, `Failures` in `SREPlatform`, a dashboard, a `Failures` alarm, SNS email | Pass/fail per run, response time, monitor status |
| Verified | Yes, including a historical systemd outage and the Nginx check used during the demonstration | Yes, alarm and SNS email verified end to end in a controlled outage; the installed unit published from Nginx | Healthy OK, failed HTTP checks, OK → ALERT, then ALERT → OK after manual recovery. Notification delivery was not recorded |

### Why more than one layer

- **The on-host monitor shares the host's fate.** If the EC2 instance stops, the Python monitor stops with it.
  It also tests through `127.0.0.1`, so it does not exercise the public path (internet, security group, Nginx
  listening publicly). Whether the existing CloudWatch alarm would still fire when metrics stop arriving
  depends on its missing-data setting, which is not documented in this repository.
- **CloudWatch depends on the monitor.** It evaluates and alerts on whatever the monitor publishes.
- **Datadog is independent of the instance.** It sees what a remote client sees, so it can show a failure
  that the on-host monitor cannot observe.

The layers complement each other and none replaces another. The trade-off is speed: the Python monitor polls
every few seconds, while Datadog's 15-minute interval means an outage could take on the order of 15 minutes to
appear in Datadog, before any alert settings are considered.

## Not yet verified

- **Alert notification delivery.** The monitor status changed OK → ALERT → OK. Delivery of a Datadog
  notification message was not recorded.
- **Exact alert settings.** The transition followed multiple failed checks. The configured retry count and
  alert condition are not recorded in this repository.
- **Multiple locations.** Only one location is used, so a regional problem at that location could look like an
  application failure.

## Other limitations

- **Liveness only.** `/health` returns a fixed response and checks no dependencies, so a passing test means the
  web path answered, not that the system is fully functional.
- **Slow interval.** 15 minutes gives coarse detection and little data (96 scheduled runs per day).
- **Small sample.** The recorded runs show passing checks and failed HTTP checks. They do not describe long-term availability or latency.
- **No TLS.** TLS is not configured in this deployment (see the main README), so certificate and expiry
  monitoring does not apply yet.
- **UI-only configuration.** The test is not reproducible from this repository.
- **No Datadog agent or telemetry.** There are no Datadog infrastructure metrics, logs, or APM data.
- **No linkage to CloudWatch.** Datadog results do not feed CloudWatch, SNS, or the Python monitor.

## After the demonstration

The EC2 instance is stopped. This Datadog synthetic test is paused. `sudo systemctl stop sre-service` does
not fail the public check while Nginx routes to the container on `127.0.0.1:18001`. A remaining check, not
done here, is delivery of a Datadog notification message. That is separate from the verified monitor-status
transition and from automatic deployment rollback.
