"""Poll /health, emit JSON events, and optionally publish metrics to CloudWatch."""

import argparse
import json
import math
import time
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import urlopen

import boto3


def emit(event, **fields):
    """Print one structured JSON event."""
    print(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event,
                **fields,
            }
        ),
        flush=True,
    )


def check_health(url, timeout):
    """Return success, HTTP status, and error for one health check."""
    try:
        with urlopen(url, timeout=timeout) as response:
            status = response.status
            healthy = 200 <= status < 300
            return healthy, status, None if healthy else f"HTTP {status}"
    except (URLError, OSError, ValueError) as error:
        return False, getattr(error, "code", None), str(error)


class Monitor:
    """Track health-check state and optionally publish CloudWatch metrics."""

    def __init__(self, url, timeout=2, cooldown=60, cloudwatch=False):
        self.url = url
        self.timeout = timeout
        self.cooldown = cooldown
        self.failures = 0
        self.last_alert = None

        # Only create an AWS client when CloudWatch publishing is requested.
        self.cloudwatch = boto3.client("cloudwatch") if cloudwatch else None

    def publish_metrics(self, healthy, duration_ms):
        """Publish availability, latency, and failure metrics to CloudWatch."""
        if self.cloudwatch is None:
            return

        self.cloudwatch.put_metric_data(
            Namespace="SREPlatform",
            MetricData=[
                {
                    "MetricName": "Availability",
                    "Value": 1 if healthy else 0,
                    "Unit": "Count",
                },
                {
                    "MetricName": "LatencyMs",
                    "Value": duration_ms,
                    "Unit": "Milliseconds",
                },
                {
                    "MetricName": "Failures",
                    "Value": 0 if healthy else 1,
                    "Unit": "Count",
                },
            ],
        )

    def poll(self):
        """Perform one health check and update monitoring state."""
        started = time.monotonic()

        healthy, status, error = check_health(self.url, self.timeout)

        now = time.monotonic()
        duration_ms = round((now - started) * 1000, 2)

        self.failures = 0 if healthy else self.failures + 1

        emit(
            "check",
            url=self.url,
            success=healthy,
            status_code=status,
            error=error,
            consecutive_failures=self.failures,
            duration_ms=duration_ms,
        )

        # Send the same result recorded locally to CloudWatch.
        self.publish_metrics(healthy, duration_ms)

        if self.failures >= 3 and (
            self.last_alert is None
            or now - self.last_alert >= self.cooldown
        ):
            emit(
                "alert",
                url=self.url,
                consecutive_failures=self.failures,
                message="Health check failed at least 3 consecutive times",
            )
            self.last_alert = now

        return healthy


def positive_number(value):
    """Require a finite number greater than zero."""
    number = float(value)

    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(
            "must be a finite number greater than zero"
        )

    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000/health",
    )
    parser.add_argument(
        "--interval",
        type=positive_number,
        default=5,
    )
    parser.add_argument(
        "--retry-delay",
        type=positive_number,
        default=1,
    )
    parser.add_argument(
        "--timeout",
        type=positive_number,
        default=2,
    )
    parser.add_argument(
        "--cooldown",
        type=positive_number,
        default=60,
    )

    # CloudWatch remains optional so the monitor still works locally.
    parser.add_argument(
        "--cloudwatch",
        action="store_true",
        help="Publish health metrics to Amazon CloudWatch",
    )

    args = parser.parse_args()

    monitor = Monitor(
        args.url,
        args.timeout,
        args.cooldown,
        args.cloudwatch,
    )

    try:
        while True:
            healthy = monitor.poll()
            time.sleep(
                args.interval if healthy else args.retry_delay
            )
    except KeyboardInterrupt:
        emit("stopped", message="Monitor stopped by user")


if __name__ == "__main__":
    main()
