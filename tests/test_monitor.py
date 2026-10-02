"""Behavior tests using fake HTTP outcomes and time; see README for live checks."""

# Provide an in-memory text buffer so tests can inspect printed output.
import io
# Parse each emitted JSON line back into Python values for assertions.
import json
# Use the built-in test runner and assertion helpers.
import unittest
# Temporarily send printed output to a buffer instead of the terminal.
from contextlib import redirect_stdout
# Replace external operations with controlled stand-ins during each test.
from unittest.mock import patch
# Construct representative connection errors and HTTP errors.
from urllib.error import HTTPError, URLError

# Import the real code under test; the main guard prevents its loop from starting.
from monitor.monitor import Monitor, check_health, main


# Group tests; unittest discovers methods whose names start with test_.
class MonitorTests(unittest.TestCase):
    # Exercise short failures, recovery, first alert, suppressed alerts, and repeated alerts.
    def test_failure_reset_and_cooldown_across_recovery(self):
        # False means failure and True means success; each entry represents one attempt.
        outcomes = [False, False, True, False, False, False,
                    False, False, True, False, False, False, False]
        # Simulated elapsed seconds: alerts should occur at 5, 65, and 125 seconds.
        times = [0, 1, 2, 3, 4, 5, 6, 65, 66, 67, 68, 69, 125]
        # Capture JSON lines without creating a real log file.
        output = io.StringIO()
        # Replace the HTTP helper and elapsed-time clock for deterministic, instant tests.
        # Backslashes continue this with statement across lines. All patches undo on exit.
        with patch('monitor.monitor.check_health') as check, \
                patch('monitor.monitor.time.monotonic') as clock, \
                redirect_stdout(output):
            # Create a fresh monitor with the default 60-second cooldown.
            monitor = Monitor('http://localhost/health')
            # Pair each outcome with its simulated time.
            for healthy, now in zip(outcomes, times):
                # Supply the next pretend HTTP result. No real network request is made.
                check.return_value = (healthy, 200 if healthy else None, None)
                # Keep start and finish at the same simulated time; duration is zero in this test.
                clock.return_value = now
                # Run the real failure-counter, logging, and alert logic once.
                monitor.poll()
        # Split captured output into lines and decode each independent JSON record.
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        # Collect the normal check records separately from alerts.
        checks = [event for event in events if event['event'] == 'check']
        # Collect alerts so their count and associated failure streaks can be verified.
        alerts = [event for event in events if event['event'] == 'alert']
        # Verify every attempt is logged and each success resets the counter.
        self.assertEqual([e['consecutive_failures'] for e in checks],
                         [1, 2, 0, 1, 2, 3, 4, 5, 0, 1, 2, 3, 4])
        # Only three alerts should occur: streaks 3, 5, and 4 at the allowed times.
        self.assertEqual([e['consecutive_failures'] for e in alerts], [3, 5, 4])
        # Every emitted event should carry a timestamp.
        self.assertTrue(all('timestamp' in event for event in events))

    # Verify timeouts, connection failures, and HTTP 503 all become failed checks.
    def test_network_failures_and_http_error(self):
        # Each pair gives a simulated exception and its expected HTTP status (if any).
        for error, status in [(TimeoutError('timed out'), None),
                              (URLError('connection refused'), None),
                              (HTTPError('http://localhost', 503, 'Unavailable', {}, None), 503)]:
            # Name each error case in test output and make urlopen raise that error.
            with self.subTest(error=error), patch('monitor.monitor.urlopen', side_effect=error):
                # Call the real error-handling function with a two-second timeout.
                healthy, code, detail = check_health('http://localhost', 2)
                # The failure must not be reported as healthy.
                self.assertFalse(healthy)
                # Keep HTTP 503 when available; use None for errors without HTTP responses.
                self.assertEqual(code, status)
                # The returned failure reason must not be empty.
                self.assertTrue(detail)

    # Verify success and confirm the requested timeout reaches the HTTP library.
    def test_success_and_timeout_forwarding(self):
        # Replace the network call with a mock response.
        with patch('monitor.monitor.urlopen') as request:
            # urlopen is used inside with; __enter__ supplies the response object there.
            request.return_value.__enter__.return_value.status = 200
            # An HTTP 200 response should return success, status 200, and no error.
            self.assertEqual(check_health('http://localhost', 2), (True, 200, None))
            # Verify exactly one request used the intended URL and timeout.
            request.assert_called_once_with('http://localhost', timeout=2)

    # Verify the loop uses the failure delay, then success delay, and handles Control+C.
    def test_retry_delay_and_clean_shutdown(self):
        # Pretend no command-line options were supplied, so defaults apply.
        # poll returns failure then success; sleep raises KeyboardInterrupt on its second call.
        # This simulates Control+C without actually waiting or running an endless loop.
        with patch('sys.argv', ['monitor.py']), \
                patch('monitor.monitor.Monitor.poll', side_effect=[False, True]), \
                patch('monitor.monitor.time.sleep', side_effect=[None, KeyboardInterrupt]) as sleep, \
                redirect_stdout(io.StringIO()) as output:
            # Run the real command-line loop with those controlled dependencies.
            main()
        # The first sleep should be one second; the next should be five seconds.
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 5])
        # Confirm the interruption produces a structured stopped event.
        self.assertEqual(json.loads(output.getvalue())['event'], 'stopped')


# Entry point if run with the project root on Python's import path.
# Prefer the README's unittest discovery command from the repository root.
if __name__ == '__main__':
    # Start the test runner when this file is executed directly.
    unittest.main()
