# Datadog External Synthetic Monitoring

A Datadog Synthetic HTTP test checks the application's `/health` endpoint from outside AWS.
It adds an external view of availability to the on-host Python monitor and CloudWatch.

**Status in one line:** the success path is verified (the test ran, passed all assertions, and the monitor
shows OK). **Failure detection, alert notifications, and recovery have not been verified.**

## Where this configuration lives

The test was created in the Datadog web UI. Nothing in this repository defines, deploys, or configures it:
there is no Datadog agent on the EC2 instance, no Datadog Terraform, and no Datadog Ansible role. The
configuration below is documented by hand and could drift from what is in Datadog.

For security, this repository contains no Datadog API key, application key, account details, or the address
of the EC2 endpoint. The results below were recorded by the project owner from the Datadog UI. No Datadog
screenshots are committed.

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
- Datadog monitor status: **OK**

That is two successful runs, one location, and no failures. Nothing beyond this has been observed.

## How the three monitoring layers differ

| | Python synthetic monitor | CloudWatch | Datadog external monitoring |
| --- | --- | --- | --- |
| Role | Generates the health signal | Stores metrics, evaluates the alarm, notifies | Independent outside-in check |
| Where it runs | Process on the EC2 host (`monitor/monitor.py`) | AWS managed service | Datadog SaaS, from an AWS Ohio location |
| What it requests | The local endpoint through Nginx (for example `http://127.0.0.1/health` in the AWS run example) | Does not probe anything itself; it uses the monitor's metrics | The public endpoint, across the internet |
| Check interval | Every 5 seconds by default, faster retries after a failure | Metrics arrive at the monitor's pace; alarm period is set in CloudWatch | Every 15 minutes |
| Output | JSON log events, a local alert after 3 consecutive failures | Custom metrics `Availability`, `LatencyMs`, `Failures` in `SREPlatform`, a dashboard, a `Failures` alarm, SNS email | Pass/fail per run, response time, monitor status |
| Verified | Yes, including a controlled outage | Yes, alarm and SNS email verified end to end in a controlled outage | Success path only |

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

- **Failure detection.** The test has not been observed failing, so it is not known that the monitor turns
  to Alert when the endpoint is down.
- **Alert notifications.** No Datadog notification (email or other channel) has been configured or tested.
- **Recovery.** A transition from alerting back to OK has not been observed.
- **Retry and alert-condition settings.** Behavior under failure (retries, how many failures trigger an alert)
  has not been configured or tested.
- **Multiple locations.** Only one location is used, so a regional problem at that location could look like an
  application failure.

## Other limitations

- **Liveness only.** `/health` returns a fixed response and checks no dependencies, so a passing test means the
  web path answered, not that the system is fully functional.
- **Slow interval.** 15 minutes gives coarse detection and little data (96 scheduled runs per day).
- **Small sample.** Two runs show the setup works but say nothing about long-term availability or latency.
- **No TLS.** TLS is not configured in this deployment (see the main README), so certificate and expiry
  monitoring does not apply yet.
- **UI-only configuration.** The test is not reproducible from this repository.
- **No Datadog agent or telemetry.** There are no Datadog infrastructure metrics, logs, or APM data.
- **No linkage to CloudWatch.** Datadog results do not feed CloudWatch, SNS, or the Python monitor.

## Remaining verification tasks

These are suggestions, not completed work:

1. Create a controlled outage on the EC2 application with the same method used before
   (`sudo systemctl stop sre-service`) and confirm the Datadog monitor changes to Alert. The 15-minute interval
   means waiting for a scheduled run or triggering a manual run.
2. Configure a Datadog notification channel and confirm a message is actually delivered.
3. Restore the service (`sudo systemctl start sre-service`) and confirm the monitor returns to OK and a recovery
   notification is sent.
4. Record the observed timings and keep screenshots as evidence, then update this document.
