"""Poll /health and emit JSON checks and cooldown-limited alerts to stdout."""

# Read optional settings such as --timeout from the terminal command.
import argparse
# Turn Python values into machine-readable JSON text.
import json
# Reject infinite numbers and NaN (not a number) in timing settings.
import math
# Measure elapsed time and pause between requests.
import time
# Create human-readable timestamps in UTC so logs share one time zone.
from datetime import datetime, timezone
# Recognize connection errors; HTTPError is also a kind of URLError.
from urllib.error import URLError
# Send HTTP requests with Python itself; Requests is not required.
from urllib.request import urlopen


# One logging function keeps all records consistent. **fields accepts named details.
def emit(event, **fields):
    # UTC is for log timestamps. **fields adds the supplied details to the dictionary.
    # json.dumps serializes it; flush=True makes each line appear immediately, even in a pipe.
    print(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                      "event": event, **fields}), flush=True)


# Return three values: success flag, HTTP status (if known), and error (if any).
def check_health(url, timeout):
    # Try the operation; the matching except below handles the expected interruption or error.
    try:
        # GET the URL. timeout limits blocking socket operations, not a strict total deadline.
        # The with block closes the response. urllib follows ordinary redirects automatically.
        with urlopen(url, timeout=timeout) as response:
            # Read the HTTP status number from the final response.
            status = response.status
            # Any 2xx response counts as success; this monitor does not inspect the JSON body.
            healthy = 200 <= status < 300
            # None means no error; an unsuccessful status gets a readable HTTP message.
            return healthy, status, None if healthy else f"HTTP {status}"
    # Convert network, socket, HTTP and invalid-value errors into failed checks.
    except (URLError, OSError, ValueError) as error:
        # Some errors have an HTTP code; getattr returns None when there is no code.
        # JSON later represents Python None as null. str(error) gives a readable reason.
        return False, getattr(error, "code", None), str(error)


# Keep the failure count and last alert time together for one monitored URL.
class Monitor:
    # Initialize state once when creating a Monitor. self means this particular monitor.
    def __init__(self, url, timeout=2, cooldown=60):
        # Save the health endpoint to request on every attempt.
        self.url = url
        # Save the request timeout in seconds.
        self.timeout = timeout
        # Save the minimum seconds between alerts.
        self.cooldown = cooldown
        # A newly started monitor has no failures yet.
        self.failures = 0
        # None means no alert has been sent; the first alert does not have to wait.
        self.last_alert = None

    # Perform exactly one attempt, update state, log it, and possibly alert.
    def poll(self):
        # A monotonic clock measures elapsed time without wall-clock adjustment problems.
        started = time.monotonic()
        # Unpack the three values returned by the HTTP check.
        healthy, status, error = check_health(self.url, self.timeout)
        # Capture completion time for request duration and cooldown checks.
        now = time.monotonic()
        # Success breaks the failure streak; otherwise add one to the current streak.
        self.failures = 0 if healthy else self.failures + 1
        # Always log an attempt, including successes and failures suppressed by cooldown.
        # Duration converts seconds to milliseconds and rounds to two decimal places.
        emit("check", url=self.url, success=healthy, status_code=status,
             error=error, consecutive_failures=self.failures,
             duration_ms=round((now - started) * 1000, 2))
        # Both conditions must hold: at least three failures AND permission from cooldown.
        # Use >= so an ongoing outage can alert again after cooldown expires.
        if self.failures >= 3 and (
            # Allow the first alert immediately, or a later alert once enough time has elapsed.
            self.last_alert is None or now - self.last_alert >= self.cooldown
        ):
            # An alert is another JSON record; this does not send email or restart the service.
            emit("alert", url=self.url, consecutive_failures=self.failures,
                 message="Health check failed at least 3 consecutive times")
            # Start the cooldown now. Success resets failures but does not erase this timer.
            self.last_alert = now
        # Tell the caller which delay to use before the next attempt.
        return healthy


# argparse calls this validator for each numeric option supplied by the user.
def positive_number(value):
    # Convert terminal text to a number; argparse also reports conversion errors.
    number = float(value)
    # Reject zero, negative values, infinity and NaN to avoid invalid waits.
    if not math.isfinite(number) or number <= 0:
        # Return a readable command-line error rather than starting with bad settings.
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    # Give argparse the validated number of seconds.
    return number


# Set up the command-line program and run until the user presses Control+C.
def main():
    # Use the module description at the top of this file as the --help description.
    parser = argparse.ArgumentParser(description=__doc__)
    # Default to this Mac only: 127.0.0.1 is the local loopback address.
    parser.add_argument("--url", default="http://127.0.0.1:8000/health")
    # Wait this many seconds after a successful attempt.
    parser.add_argument("--interval", type=positive_number, default=5)
    # Wait this many seconds after a failed attempt; every retry is a new check.
    parser.add_argument("--retry-delay", type=positive_number, default=1)
    # Do not let a stalled socket operation wait indefinitely.
    parser.add_argument("--timeout", type=positive_number, default=2)
    # Limit alert frequency while continuing to record every check.
    parser.add_argument("--cooldown", type=positive_number, default=60)
    # Read command-line options, or use their defaults when omitted.
    args = parser.parse_args()
    # Create one stateful monitor; restarting the program creates fresh state.
    monitor = Monitor(args.url, args.timeout, args.cooldown)
    # Try the operation; the matching except below handles the expected interruption or error.
    try:
        # Keep checking indefinitely; Control+C exits through KeyboardInterrupt below.
        while True:
            # Finish one check before starting another; requests never overlap.
            healthy = monitor.poll()
            # Retry failures sooner; every attempt counts and is recorded.
            # Wait after the request finishes, so request time adds to the polling period.
            time.sleep(args.interval if healthy else args.retry_delay)
    # Control+C asks this foreground process to stop without a Python traceback.
    except KeyboardInterrupt:
        # Record the normal user-requested shutdown as JSON too.
        emit("stopped", message="Monitor stopped by user")


# Run only when launched as a script; importing this module for tests starts no loop.
if __name__ == "__main__":
    # Enter the command-line program.
    main()
