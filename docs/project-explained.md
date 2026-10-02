# Explaining the project

## A short walkthrough you can give someone

“This project runs a small HTTP service and monitors it from a separate process.
The service exposes `/health`. The monitor checks the endpoint, writes structured
JSON records, and counts consecutive failures. Three failures trigger a local alert.
A cooldown limits repeat alerts while checks keep running. I can stop the service
to demonstrate an outage, restart it to show recovery, and inspect the saved logs.”

Only claim a test passed after running it and recording the result. Templates in
`deploy/` demonstrate a possible later Linux layout; their presence does not mean
Linux, nginx, systemd, or AWS deployment has been tested or completed.

## How a request moves through the project

1. Uvicorn listens on `127.0.0.1:8000` and passes requests to FastAPI.
2. FastAPI matches a GET request for `/health` to the `health` function.
3. That function returns a dictionary, which FastAPI sends as JSON with HTTP 200.
4. The monitor's `check_health` function requests that URL and reports the outcome.
5. `Monitor.poll` updates the consecutive failure count and emits a `check` record.
6. If at least three failures have occurred and cooldown permits, it emits `alert`.
7. The main loop sleeps for the success or failure delay before trying again.
8. The shell's `tee` command displays and appends those records to a local file.

## Why these choices are reasonable

| Choice | Explanation you can defend | Limitation |
| --- | --- | --- |
| Tiny health endpoint | Makes service reachability easy to demonstrate | Does not check dependencies or business operations |
| Separate monitor process | Can observe the service after the service stops | Cannot observe anything if the monitor itself stops |
| Built-in urllib | Avoids adding a dependency for one simple GET request | Less convenient than Requests for larger HTTP clients |
| Any HTTP 2xx is success | Uses HTTP's success category consistently | Does not validate the body; a wrong body with 200 still passes |
| Three consecutive failures | Filters out one or two isolated failures | Adds detection delay; threshold is a fixed learning choice |
| One-second failure retry | Rechecks a temporary failure sooner | Continues at a fixed rate during long outages; no backoff |
| Sixty-second cooldown | Limits repeat alerts while preserving check logs | Can delay a new outage alert following a brief recovery |
| Monotonic elapsed time | Clock corrections do not disturb duration/cooldown arithmetic | Values are internal elapsed time, not dates |
| UTC timestamps | Makes logged event times comparable | Convert to local time when discussing an incident |
| JSON Lines | Each record is independently readable by tools | The complete log is not a single JSON document |
| In-memory state | Keeps the monitor understandable | Restarting resets failure count and cooldown |
| Localhost binding | Supports a local-only exercise | Does not show remote network availability |
| Version ranges | Bound the permitted dependency versions | Does not guarantee identical installs like a lockfile would |

## Reading the Python syntax

- `import` brings an existing library into this file.
- `def` defines a function; its indented lines execute when it is called.
- `class` groups data and operations; `self` refers to one instance.
- `__init__` initializes the state for a new instance.
- `return` hands a result back to the caller.
- `healthy, status, error = ...` unpacks three returned values.
- `a if condition else b` chooses one value based on a condition.
- `None` means no value; JSON represents it as `null`.
- `True` and `False` become JSON `true` and `false`.
- `with` manages a resource or temporary context, including cleanup on exit.
- `try` / `except` handles anticipated errors without stopping the whole program.
- `**fields` gathers named arguments, or expands them into another dictionary.
- `if __name__ == "__main__"` runs the entry point only when executed as a script.
- `@app.get(...)` registers the following function as the handler for a GET route.

Detailed comments beside each operation explain the project's actual use of these
constructs. The extra comments make the monitor longer than the original compact
version, but its runtime logic remains the same.

## What the tests establish

The four automated tests cover failure reset and cooldown across recovery; network
and HTTP failures; successful responses and timeout forwarding; retry delays and
clean loop shutdown. They replace HTTP calls and elapsed time with controlled values.
That makes them fast and repeatable, but it does not prove an actual socket timeout
or a real server's availability. Manual curl and outage checks provide live evidence.

The cooldown test uses alerts at simulated seconds 5, 65, and 125. A success at
second 66 clears the failure streak but preserves the alert timer from second 65.
The next streak reaches three at second 69, so its alert must wait for cooldown.

## What remains outside this local version

No remote notifications, persistent monitor state, log rotation, automatic service
recovery, dashboard, database checks, or cloud resources are implemented. urllib can
follow redirects, and its timeout is for blocking socket operations rather than a
strict total deadline. The monitor only reads response status, not the response body.
The systemd template would restart a crashed service if installed later; that is
separate from the local monitor and does not react to its alert records.
