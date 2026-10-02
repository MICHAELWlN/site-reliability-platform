# Site reliability platform

A small local reliability exercise: FastAPI answers a health request, and a separate
Python process checks it, records results, and generates local alerts during outages.
Local testing comes first; initial AWS infrastructure setup has now begun, as recorded
below. The application is not yet deployed on EC2. Python 3.10+ is required.

## AWS deployment checkpoint — 2026-10-02

This checkpoint records the owner's reported progress. EC2 status checks were still
**initializing** when work stopped; passing checks have not been confirmed.

### Completed locally

FastAPI `/health`, Uvicorn, synthetic health monitoring, structured JSON logging,
consecutive-failure detection, cooldown-limited local alerts, local failure/recovery
exercises, and automated tests are in place. These are local capabilities and results,
not evidence of EC2 application deployment. The walkthrough below and
[failure exercises](incidents/failure-tests.md) explain how to repeat the local checks.

### AWS infrastructure prepared

- Created and launched an Ubuntu EC2 instance named `sre-platform` as a small
  learning/development server.
- Created an SSH key pair for later remote access.
- Restricted security-group SSH access (port 22) to the owner's IP.
- Allowed HTTP (port 80) from `0.0.0.0/0` for a future public Nginx entry point.
- Intentionally did not expose Uvicorn port 8000 publicly.
- Reached the EC2 status-check stage; checks were still initializing at this checkpoint.

The intended architecture is:

```text
Internet → EC2 Ubuntu VM → Nginx → Uvicorn/FastAPI → /health
```

This is the architecture goal, not a running deployment. Uvicorn should remain bound
to `127.0.0.1:8000` inside EC2. Nginx will eventually accept public HTTP on port 80
and forward it to localhost:8000. Opening port 80 alone does not install Nginx or
make the application available.

### Next AWS step — not completed yet

1. Open the EC2 console and select `sre-platform`. If it is stopped, start it;
   then wait for its status checks to pass before connecting.
2. Obtain its current public IPv4 address. Keep it out of this repository.
3. Keep the private SSH key outside the repository, restrict its local file
   permissions to owner-read-only (for example, `chmod 400` on the key file), and
   use it to connect over SSH as the `ubuntu` user. Confirm the SSH rule still
   permits your current IP.
4. Verify the remote operating system is Ubuntu, then install Git, Python 3,
   Python's venv/pip support, and curl.
5. Clone this repository on EC2, enter its directory, create and activate `.venv`,
   and install `requirements.txt`.
6. Run `python -m uvicorn service.main:app --host 127.0.0.1 --port 8000` on EC2.
7. In another SSH session, run `curl -i http://127.0.0.1:8000/health` from inside
   EC2 and verify HTTP 200 with `{"status":"healthy"}`. Record the actual result.

No SSH connection, repository clone, EC2 virtual environment, dependency installation,
Uvicorn/FastAPI run, or EC2 `/health` test has been completed at this checkpoint.
Never commit the private key or a `.pem` file, account ID, public IP address,
credentials, access keys, or other secrets.

### Future work after the first EC2 health check

1. Run `monitor/monitor.py` on EC2 and verify and document hosted outage/recovery behavior.
2. Configure systemd to manage the service.
3. Configure Nginx as the public port-80 reverse proxy to `127.0.0.1:8000`.
4. Add CloudWatch dashboards/alarms and SNS notifications.
5. Perform and document final controlled failure tests, including evidence of
   automated recovery before claiming it works.

None of these future steps has been completed. The files in `deploy/` remain
uninstalled templates; automated recovery has not been demonstrated.

## What each file does

| File | Purpose |
| --- | --- |
| `service/main.py` | Defines the FastAPI app and `GET /health` route |
| `monitor/monitor.py` | Requests the endpoint, counts failures, logs checks and alerts |
| `tests/test_monitor.py` | Checks monitor decisions using simulated responses and time |
| `requirements.txt` | Lists the external packages needed to run the service |
| `incidents/failure-tests.md` | Manual failure exercises and a place to record evidence |
| `deploy/sre-service.service` | Inactive template for running the service under Linux systemd later |
| `deploy/nginx.conf` | Inactive template for forwarding HTTP traffic through nginx later |
| `logs/monitor.log` | Generated JSON records from your own runs; excluded from Git |
| `.gitignore` | Keeps generated files and virtual environments out of commits |
| `LICENSE` | MIT license terms; retained as legal text rather than annotated code |
| `docs/project-explained.md` | Design choices, limitations, and explanations for a walkthrough |

`.venv/` and any older `venv/` contain installed tools, not application source. Use
`.venv` consistently below. Empty folders from earlier scaffolding are not needed
for this workflow. Logs remain raw JSON so tools can read them; explanations live here.

## 1. Open VS Code and the first terminal

Open this project folder in VS Code. Choose **Terminal → New Terminal**.
This will be the **service terminal**. Run:

```sh
cd /Users/michaelnguyen/site-reliability-platform
python3 --version
```

`cd` changes your current directory. Other users should replace that path with their
own checkout location. Run all project commands from this directory. If Python is
missing or older than 3.10, install a supported Python 3 version before continuing.

## 2. Create and activate the virtual environment

```sh
python3 -m venv .venv
source .venv/bin/activate
```

The first command creates a local folder for this project's Python and packages.
`-m` runs a Python module. The second command makes this terminal use that environment.
You will usually see `(.venv)` in the prompt. Confirm the selected interpreter:

```sh
python -c "import sys; print(sys.executable)"
```

The printed path should end in `.venv/bin/python`. Create the environment once;
activate it again in each new terminal. In VS Code, **Command+Shift+P → Python:
Select Interpreter** lets you select the same `.venv` interpreter for editor tooling.

## 3. Install the service dependencies

```sh
python -m pip install -r requirements.txt
```

`pip` installs packages into the active environment. `-r` reads the package list.
FastAPI defines the API; Uvicorn serves it over HTTP. The monitor uses Python's
built-in `urllib`, so it does not require Requests. If a separate learning exercise
explicitly requires installing Requests, you can run `python -m pip install requests`;
that package is optional and is not used by this project's code.

## 4. Start the service in the first terminal

```sh
python -m uvicorn service.main:app --host 127.0.0.1 --port 8000
```

- `service.main` tells Python to load `service/main.py`.
- `:app` selects the FastAPI object named `app` in that file.
- `127.0.0.1` listens on this Mac's local loopback address.
- `8000` is the port, which identifies this listener on your Mac.

Look for `Uvicorn running on http://127.0.0.1:8000`. Leave this terminal running.
It will not show a new shell prompt while the server is active.
The route and server approach follow FastAPI's official
[first steps](https://fastapi.tiangolo.com/tutorial/first-steps/) and
[manual server instructions](https://fastapi.tiangolo.com/deployment/manually/).

## 5. Open a second terminal and see HTTP 200 yourself

Choose **Terminal → New Terminal** again. This will be the **monitor terminal**.

```sh
cd /Users/michaelnguyen/site-reliability-platform
source .venv/bin/activate
curl -i http://127.0.0.1:8000/health
```

`curl` makes an HTTP request. `-i` includes the response headers, so you can see the
status code as well as the body. Expect headers including:

```text
HTTP/1.1 200 OK
content-type: application/json
```

Then expect the body:

```json
{"status":"healthy"}
```

HTTP 200 means this request succeeded. You can also open
`http://127.0.0.1:8000/health` in your browser. The `/` path is not defined and
returning 404 there is expected; use `/health`.

## 6. Start the monitor and save its output

In the second terminal:

```sh
mkdir -p logs
python monitor/monitor.py | tee -a logs/monitor.log
```

`mkdir -p` creates the log directory if needed. The pipe (`|`) sends monitor output
to `tee`. `tee` displays it and writes it to the file; `-a` appends to existing
records. Without `tee`, the monitor prints to the terminal only.

Here is an illustrative check, shown as one line just like the real log:

```json
{"timestamp":"2026-10-02T12:00:00+00:00","event":"check","url":"http://127.0.0.1:8000/health","success":true,"status_code":200,"error":null,"consecutive_failures":0,"duration_ms":0.71}
```

Your timestamp and duration will differ. Open `logs/monitor.log` in VS Code to see
saved results. It contains JSON Lines: each line is a separate JSON object, not
one large JSON array. Do not add comments to the generated log.

| Field | Meaning |
| --- | --- |
| `timestamp` | When the event was recorded, in UTC (`+00:00`) |
| `event` | `check`, `alert`, or `stopped` |
| `url` | Endpoint being checked |
| `success` | Whether the final HTTP response was in the 200–299 range |
| `status_code` | HTTP status, or `null` if no response code was available |
| `error` | Readable failure reason, or `null` on success |
| `consecutive_failures` | Unbroken failure streak; success resets it to zero |
| `duration_ms` | Time spent making this check, in milliseconds |
| `message` | Explanation attached to an alert or stopped event |

Different event types have different fields. Alerts are JSON log entries; there
is no email, Slack notification, or automatic recovery in this version.

## 7. Stop the service to demonstrate failure

Keep the monitor running. Click the **service terminal** and press **Control+C**
(the Control key, not Command). Uvicorn should shut down and return to a shell prompt.

Watch the monitor terminal. It should record failures with counts 1, 2, then 3.
At count 3, it emits an additional `alert` record. A stopped local service typically
produces `Connection refused` and a `null` status code because no HTTP reply arrived.

Keep the service stopped to observe cooldown: checks continue, but another alert
cannot appear until at least 60 seconds after the preceding alert. The first failure
may take roughly five seconds to appear if the monitor is waiting after a success.

## 8. Restart the service to demonstrate recovery

In the service terminal, run the same command again:

```sh
python -m uvicorn service.main:app --host 127.0.0.1 --port 8000
```

The next successful monitor check should show `success: true`, `status_code: 200`,
and `consecutive_failures: 0`. There is no separate recovery event; the successful
check is the recovery evidence. Follow [the failure exercises](incidents/failure-tests.md)
to test temporary failures, HTTP errors, and cooldown in more detail.

## 9. Run the automated tests

In the monitor terminal, press **Control+C** to stop monitoring. Then run:

```sh
python -m unittest discover -s tests -v
```

`unittest` is built into Python. `discover` finds test files, `-s tests` selects the
folder, and `-v` prints each test's name. Expect four tests and `OK`. Tests simulate
HTTP outcomes and time; they do not require a running server. The manual steps
above separately demonstrate actual network requests to FastAPI.

## 10. Stop everything when finished

1. Press **Control+C** in the monitor terminal if it is still running.
2. Press **Control+C** in the service terminal if it is still running.
3. Optionally run `deactivate` in each terminal to leave the virtual environment.

`deactivate` changes the shell environment; it does not stop a running server.
The saved log stays on disk. On your next session, activate `.venv` and run the
service and monitor commands again; you do not need to recreate the environment.

## Settings and timing

```sh
python monitor/monitor.py --help
python monitor/monitor.py --interval 5 --retry-delay 1 --timeout 2 --cooldown 60
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--url` | `http://127.0.0.1:8000/health` | Target URL |
| `--interval` | 5 seconds | Delay after success |
| `--retry-delay` | 1 second | Delay after failure |
| `--timeout` | 2 seconds | Timeout for blocking socket operations |
| `--cooldown` | 60 seconds | Minimum time between alerts |

Numeric settings must be finite and greater than zero. A delay starts after a request
finishes, so requests taking longer make checks farther apart. The timeout is not a
strict overall deadline for DNS, redirects, and the complete request combined.
Every failed retry counts toward the fixed threshold of three consecutive failures.
Cooldown continues across brief recoveries; restarting the monitor resets all state.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `No module named fastapi` or `uvicorn` | Activate `.venv`, then install `requirements.txt` in that terminal |
| Cannot import `service.main` | Run from the repository root, not from `service/` |
| `Address already in use` | Check your other terminals for an already running server and stop that instance with Control+C |
| Connection refused | Start the service and confirm the monitor URL uses the same port |
| HTTP 404 | Request `/health`; `/` and `/missing` are not defined |
| No new shell prompt | The service or monitor is running; use another terminal or stop it with Control+C |
| Log contains older events | `tee -a` preserves earlier runs; compare timestamps |
| No immediate alert after another outage | Confirm three consecutive failures and wait for the previous alert's cooldown |
| A stopped monitor logs no `stopped` line through `tee` | Control+C can stop both pipeline processes before tee saves the final line; earlier flushed checks remain evidence |

If you intentionally use another port, change both commands, for example:

```sh
python -m uvicorn service.main:app --host 127.0.0.1 --port 8001
```

Then, in the other terminal:

```sh
python monitor/monitor.py --url http://127.0.0.1:8001/health
```
