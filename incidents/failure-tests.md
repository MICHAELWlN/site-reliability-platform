# Local failure exercises and evidence

Use the README to create `.venv`, install dependencies, and start the service and
monitor in separate VS Code terminals. These are instructions, not claims that every
scenario below has passed. Record your observations at the bottom after each run.

## Healthy response

Before starting the monitor in its terminal, run:

```sh
curl -i http://127.0.0.1:8000/health
```

Expect HTTP 200 and `{"status":"healthy"}`. Then start the monitor:

```sh
mkdir -p logs
python monitor/monitor.py | tee -a logs/monitor.log
```

Expect `check` records with `success: true`, status 200, and failure count zero.

## Outage and cooldown

1. Press Control+C in the service terminal only.
2. Watch the monitor record failures 1, 2, and 3. Each attempt gets its own record.
3. Verify the third failure also produces an `alert` record.
4. Leave the service stopped for at least 65 seconds after the first alert.
5. Compare alert timestamps: the next alert must be at least 60 seconds later.

The first failure may wait for the normal five-second interval. Subsequent failures
retry after one second, plus request time. Continue logging even during cooldown.

## Recovery

In the service terminal:

```sh
python -m uvicorn service.main:app --host 127.0.0.1 --port 8000
```

Expect the next successful check to reset the failure count to zero. A successful
`check` record is the recovery evidence; this version has no separate recovery event.

## Temporary failure without an alert

For a less rushed manual exercise, stop the monitor with Control+C and restart it:

```sh
python monitor/monitor.py --interval 1 --retry-delay 10 | tee -a logs/monitor.log
```

With the service initially healthy, stop it. After the first failed check appears,
restart the service within ten seconds. The next check should succeed and reset the
count. That isolated failure must not produce an alert. If you miss the timing,
repeat the exercise. Restore default monitor settings afterward by restarting it
without the timing options.

## HTTP error rather than a stopped process

Keep the service running. Stop the monitor and run:

```sh
python monitor/monitor.py --url http://127.0.0.1:8000/missing | tee -a logs/monitor.log
```

`/missing` is intentionally not a route. Expect HTTP 404 in failed check records,
then an alert at count three. This differs from connection refused: here the service
answered HTTP but the requested endpoint did not succeed. Stop this monitor and
restart with the default `/health` URL when finished.

## Faster cooldown demonstration

```sh
python monitor/monitor.py --interval 1 --retry-delay 1 --cooldown 5 | tee -a logs/monitor.log
```

Use this instead of the default monitor command for a shorter exercise. Stop the
service, observe the first alert after three failures, and verify repeat alerts are
at least five seconds apart. Record the custom settings with your evidence so the
shorter interval is not mistaken for a failure of the default 60-second cooldown.

## Automated checks, including simulated timeout

Stop the monitor, then run:

```sh
python -m unittest discover -s tests -v
```

Expect four tests and `OK`. The timeout case injects `TimeoutError`; it verifies
error handling without requiring a real stalled server. Stopping uvicorn normally
causes connection refused, which is a different failure mode from a timeout.

## Evidence record

Copy this block for each exercise and fill it with actual observations. Keep raw
log lines unchanged and compare UTC timestamps when measuring cooldown.

```text
Date/time and time zone:
Scenario:
Service command and port:
Monitor command and settings:
Action performed:
Expected result:
Observed result:
Relevant JSON lines or their timestamps:
Pass/fail:
What I learned or would investigate next:
```

Do not claim cloud reliability from these local checks. Finish by stopping both
processes with Control+C in their respective terminals. Leave the logs available
for review; `tee -a` will append on the next run.
