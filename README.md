# Site Reliability Platform

A hands-on site reliability engineering project focused on deploying, monitoring, testing, and recovering a web service on AWS.

The application itself is intentionally minimal. The focus is the reliability layer around it: Linux service management, reverse proxying, synthetic monitoring, observability, alerting, failure testing, and recovery.

## Architecture

```text
Internet
   |
   v
Nginx :80
   |
   v
Uvicorn 127.0.0.1:8000
   |
   v
FastAPI /health
```

Monitoring and alerting:

```text
Python synthetic monitor
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

The service runs on an Ubuntu EC2 instance. Uvicorn is bound to localhost, while Nginx provides the public HTTP entry point on port 80.

## Technologies

- Python
- FastAPI
- Uvicorn
- Linux / Ubuntu
- systemd
- Nginx
- AWS EC2
- Amazon CloudWatch
- Amazon SNS
- boto3
- Git / GitHub

## Service

`service/main.py` exposes a deliberately simple health endpoint:

```text
GET /health
```

Healthy response:

```json
{"status":"healthy"}
```

Keeping the application small makes the reliability behavior easy to observe and test.

## Process Management

Uvicorn runs as a systemd-managed service using `deploy/sre-service.service`.

The service:

- runs as the Ubuntu user
- uses the project's Python virtual environment
- binds Uvicorn to `127.0.0.1:8000`
- starts automatically during normal system boot
- is configured with `Restart=on-failure`

Uvicorn is not directly exposed to the internet.

## Nginx Reverse Proxy

Nginx provides the public HTTP entry point:

```text
Client -> Nginx :80 -> Uvicorn 127.0.0.1:8000 -> FastAPI
```

Requests received by Nginx are forwarded to the application server over localhost.

## Synthetic Monitoring

`monitor/monitor.py` continuously checks the health endpoint.

The monitor:

- performs HTTP health checks
- measures response duration
- records structured JSON events
- tracks consecutive failures
- retries failed checks
- alerts after three consecutive failures
- limits repeated alerts with a cooldown
- detects recovery and resets the failure counter

## CloudWatch Observability

When CloudWatch publishing is enabled, the monitor publishes three custom metrics to the `SREPlatform` namespace:

- `Availability`
- `LatencyMs`
- `Failures`

The EC2 instance uses an IAM role for AWS access rather than hard-coded AWS credentials.

A CloudWatch dashboard provides centralized visibility into these metrics.

## Alerting

A CloudWatch alarm monitors the failure metric.

The alert path is:

```text
Synthetic monitor
      |
      v
CloudWatch Failures metric
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

This path was tested end-to-end during a controlled application outage.

## Automatic Process Recovery Test

Process-level recovery was tested separately from the controlled outage test.

The Uvicorn process was forcibly terminated with SIGKILL to simulate an unexpected process crash. Because `sre-service` is configured with `Restart=on-failure`, systemd detected the failed process and automatically started a replacement process after the configured restart delay.

After the restart:

- `sre-service` returned to the active state
- Uvicorn was running again
- `/health` returned HTTP 200

This test demonstrates automatic process recovery. It is separate from the controlled outage test below, where `systemctl stop` intentionally keeps the service stopped until manual restoration.

Boot persistence was also verified separately by restarting the EC2 instance and confirming that the enabled systemd service started during normal boot and `/health` returned HTTP 200.
## Controlled Failure Test

The deployed system was tested by deliberately creating an application outage.

### Healthy State

Before failure injection:

- `sre-service` was active
- Nginx successfully proxied `/health`
- the endpoint returned HTTP 200
- the monitor reported successful checks
- healthy metrics were published to CloudWatch

### Failure Injection

The application service was intentionally stopped:

```bash
sudo systemctl stop sre-service
```

During the outage:

- the health endpoint became unavailable
- the monitor detected failed checks
- the consecutive failure count increased
- the local threshold alert fired after three consecutive failures
- failure metrics were published to CloudWatch
- the CloudWatch alarm entered its alarm state
- SNS delivered the alarm email

### Recovery

The service was restored:

```bash
sudo systemctl start sre-service
```

After restoration:

- systemd reported the service active
- `/health` returned HTTP 200 through Nginx
- the monitor detected recovery
- the consecutive failure count reset to zero
- healthy CloudWatch measurements resumed

Detailed failure-test notes are available in `incidents/failure-tests.md`.

## Running Locally

Create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Start the FastAPI service:

```bash
python -m uvicorn service.main:app --host 127.0.0.1 --port 8000
```

Verify the endpoint:

```bash
curl -i http://127.0.0.1:8000/health
```

Run the synthetic monitor:

```bash
python monitor/monitor.py --url http://127.0.0.1:8000/health
```

On the AWS deployment, CloudWatch publishing can be enabled with:

```bash
python monitor/monitor.py --url http://127.0.0.1/health --cloudwatch
```

## Repository Structure

```text
site-reliability-platform/
├── service/
│   └── main.py
├── monitor/
│   └── monitor.py
├── tests/
├── deploy/
│   ├── nginx.conf
│   └── sre-service.service
├── incidents/
│   └── failure-tests.md
├── docs/
│   └── project-explained.md
├── requirements.txt
└── README.md
```

## Project Scope

This is intentionally a small learning and portfolio project rather than a production-ready platform.

It demonstrates:

- AWS EC2 deployment
- Linux service management
- Nginx reverse proxying
- synthetic HTTP monitoring
- structured health-check logging
- failure thresholds and local alerting
- custom CloudWatch metrics
- CloudWatch dashboarding
- CloudWatch alarms
- SNS email notification
- controlled failure injection
- service restoration and recovery detection

Potential improvements include infrastructure as code, tighter least-privilege IAM, TLS, automated deployment, centralized logging, external monitoring, and additional failure scenarios.
