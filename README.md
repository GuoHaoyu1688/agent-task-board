# Agent Task Board

A local SQLite task queue and browser board for AI agents working on one machine.
Claim tasks atomically, renew fenced leases, hand work to another agent, and request
human approval before a result becomes final. Version **0.1.0**, MIT licensed.

## Quick start

Python 3.11 or newer is required. From this repository, these three commands install
the tool, load fictional examples, and open the board server:

```sh
python3 -m pip install -e .
atb import examples/tasks.json --json
atb serve --host 127.0.0.1 --port 8765
```

Visit `http://127.0.0.1:8765`. The responsive board works offline without a build step,
CDN, account, or API key. Use `atb serve --read-only` for display-only mode.
For an isolated environment and headless-agent setup, see [AGENTS.md](AGENTS.md).

```json
{"ok":true,"data":{"imported":3,"skipped":0},"warnings":[]}
```

## What is included

- ULID task IDs, priorities (larger values run first), queues, optional assignees,
  deadlines, tags, JSON inputs/results, and artifact paths.
- SQLite WAL plus `BEGIN IMMEDIATE` for every state transition; unique active
  deduplication keys; dependencies with cycle detection.
- Atomic claims, renewable 15-minute leases, and fresh random tokens for every
  claim. Expired workers cannot complete, fail, or renew a replacement lease.
- Bounded attempts and exponential retry delays: 30, 60, 120 seconds by default,
  capped at one day. Expired leases return immediately to the queue unless the
  attempt limit is exhausted. Each claim consumes one attempt.
- Five-field cron schedules, bounded catch-up, comments, reassignment, and an
  append-only event timeline.
- Human review: `needs_approval` tasks enter `review`; only approval unblocks their
  dependents. Rejection starts a new attempt budget and preserves the prior result
  for inspection.
- English browser kanban with agent filtering, deadline highlighting, timeline,
  approval/return controls, notes, and reassignment; read-only mode rejects writes
  on the server.
- Official Python MCP SDK via stdio, sharing the same business logic as the CLI.
- JSON/CSV import/export, live SQLite backup ZIPs, and optional completion hooks.

## CLI reference

Every command accepts `--data-dir`, `--config`, `--json`, and `--quiet`, before or
after the subcommand. All normal command results are one JSON envelope on stdout,
including without `--json`. Errors are structured and return a nonzero exit code.
`--version` also returns JSON. `serve` and `mcp` are long-running services.

| Command | Purpose / example |
| --- | --- |
| `add` | `atb add "Prepare example report" --queue report --priority 3 --dedupe report-example --params '{"rows":10}' --needs-approval` |
| `claim` | `atb claim --agent report-bot --queue report --lease-seconds 900` |
| `heartbeat` | `atb heartbeat ID --agent report-bot --token TOKEN` |
| `done` | `atb done ID --agent report-bot --token TOKEN --result '{"rows":10}' --artifact output/report.md` |
| `fail` | `atb fail ID --agent report-bot --token TOKEN --error "Input unavailable"` |
| `list` / `show` | `atb list --status todo --agent report-bot`; `atb show ID` |
| `events` | `atb events --task ID --after 0 --limit 1000` |
| `comment` | `atb comment ID "Prepared the input" --actor report-bot` |
| `reassign` | `atb reassign ID --assignee second-bot` (omit assignee to allow any worker) |
| `depend` | `atb depend CHILD_ID PARENT_ID` |
| `approve` / `reject` | `atb approve ID`; `atb reject ID --reason "Please add evidence"` |
| `cancel` / `retry` | `atb cancel ID --yes`; `atb retry ID` |
| `schedule add` | `atb schedule add "30 7 * * *" --template examples/schedule.json` |
| `schedule list/pause/resume` | `atb schedule list`; `atb schedule pause SCHEDULE_ID` |
| `tick` | `atb tick --max-catchup 100` |
| `export` / `import` | `atb export --format json --out output/tasks.json`; `atb import output/tasks.json` |
| `backup` | `atb backup --out output/backups` |
| `serve` / `mcp` | `atb serve --read-only`; `atb mcp` |

Additional `add` options: `--body`, `--assignee`, `--due`, repeatable `--tag`,
repeatable `--depends-on`, `--max-attempts`, and `--actor`. Use `atb COMMAND --help`
for exact parameter names. Lease tokens are returned by claim/heartbeat and must
be retained by the claiming worker; list/show/export/Web do not expose them.

## Configuration and data

Default data: `./data/tasks.db`. Precedence for the directory is CLI `--data-dir`,
`ATB_DATA_DIR`, TOML `data_dir`, then `./data`. See [examples/config.toml](examples/config.toml).
Naive ISO dates and cron expressions use `Asia/Shanghai` by default. Storage uses
UTC with explicit offsets. The browser displays the configured time zone.

Task JSON accepts `title`, `body`, `queue`, `assignee`, `priority`, `due_at`, `tags`,
`dedupe_key`, `blocked_by`, `params`, `max_attempts`, and `needs_approval`.
JSON export additionally contains task state, events, and schedules. CSV is a task
table with nested arrays/objects encoded as JSON cells, UTF-8 with BOM by default;
use `export --format csv --utf8` for plain UTF-8. CSV does not carry event history
or schedules. Imports are transactional, preserve IDs, and skip existing IDs or
active dedupe keys. Imported claimed/running tasks require a fresh claim; imported
tasks with an existing ID are never updated. JSON imports retain historical task
events, assigning fresh local event sequence IDs.

`backup` uses SQLite's online backup API, including committed WAL changes, and
stores `tasks.db` plus metadata in a timestamped ZIP. To restore, stop all workers
and servers, make a backup, then extract into a **new empty data directory** and
start with `--data-dir NEW_DIRECTORY`. Runtime data and backups are git-ignored.

Run `atb tick` every minute with cron to generate scheduled tasks and reclaim
expired leases even while no workers are polling. Claims also reclaim expired
leases. Repeated ticks do not regenerate the same occurrence. Catch-up is capped
per schedule per call, and `catchup_pending` signals more work. Pause/resume skips
the paused interval. Schedule occurrence keys override template `dedupe_key`.

## MCP configuration

```json
{
  "mcpServers": {
    "agent-task-board": {
      "command": "/workspace/apps/agent-task-board/.venv/bin/atb",
      "args": ["--data-dir", "/workspace/data/agent-task-board", "mcp"]
    }
  }
}
```

Tools: `create_task`, `claim_task`, `heartbeat`, `complete_task`, `fail_task`,
`list_tasks`, `add_comment`, `get_task`, and `cancel_task` (`confirm=true` required).
Approval is deliberately available through the human CLI/Web workflow rather than
the agent tool set. Full generated schemas are in [docs/mcp-tools.json](docs/mcp-tools.json).
The runtime is pinned to the official SDK's compatible `mcp>=1.20,<2` series.

## Integrations and optional hooks

Daily digest tools can consume `atb list --json` and `atb events --json`, or the JSON
export. Reading data does not reclaim leases or generate scheduled work.

Completion hooks are disabled by default. Explicitly set `[hooks] enabled=true`
and a trusted `command` argv array in TOML to run a local program after the task is
committed `done`; it receives the task JSON on stdin, has a 10-second timeout, and
does not run through a shell. No command is taken from task content. A fixed
`webhook` URL additionally requires `allow_network=true` and has a 5-second timeout.
See [examples/config.toml](examples/config.toml). Review tasks trigger hooks only
after approval. Delivery success/failure appears in events. Hooks are best effort,
not a durable outbox: a process crash after commit can prevent delivery. Hook
failure never rolls back a completed task.

## Verification

```sh
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest -q --cov=agent_task_board
```

Tests include ten real processes claiming and completing 1,000 tasks with no
duplicate claim, no omission, and one attempt each; token fencing after expiry;
renewal; exponential backoff; dependency cycles; review; schedules; import/backup;
every MCP tool via the SDK; and HTTP read-only and mutation protections. CI runs
Python 3.11 and 3.12 on Ubuntu.

## FAQ and boundaries

**Does this promise exactly-once external effects?** No. Lease fencing protects
queue state. A worker that loses its lease must stop; effects performed before a
crash may repeat on retry. Make external operations idempotent using task ID.

**Can it run across machines?** No. Use a local filesystem on one machine. SQLite
WAL on network filesystems and distributed workers are outside scope.

**Is the web server authenticated?** No account system is included. It defaults
to loopback and protects mutations against cross-origin browser requests. All local
users able to access the service can operate it. Keep it on loopback, or use an
authenticated reverse proxy you manage. Read-only mode is a server-side restriction.

**Why is a task blocked?** Every dependency must be `done`. Failed, cancelled, or
review dependencies keep descendants blocked. Inspect `show` and `events`, then
resolve or retry the parent. Deleting task history is intentionally unsupported.

**Why do queries still show an expired claim?** Queries do not change state. Run
`tick`, or let the next `claim` reclaim it. The worker still cannot mutate an expired
lease before reclamation.

**How large is the browser view?** It displays at most 10,000 tasks; the CLI supports
up to 100,000 per list call. Event queries support `--after` cursor pagination.
The board refreshes every ten seconds and task timelines load the first 1,000
events. Use the CLI cursor for longer histories.

## Roadmap

Durable hook outbox, paginated browser histories, and optional task archival.
No cloud dependency, broker execution, account login, or multi-machine coordination
is included. All examples are fictional. See [CHANGELOG.md](CHANGELOG.md) and [LICENSE](LICENSE).
