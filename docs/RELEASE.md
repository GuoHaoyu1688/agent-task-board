# Version 0.1.0

A local SQLite queue and kanban for AI agents with fenced leases, retries, dependencies and MCP.

Local verification: 30 tests passed; 86% measured coverage; Ruff lint and formatting checks passed. README quick start was exercised with fictional data.

## Known limitations

Single-machine local-filesystem operation. Completion hooks are best-effort after commit, without a durable outbox. External effects require idempotent handlers. The browser shows up to 10,000 tasks and the first 1,000 task events; CLI event cursors can read full history.

See README and AGENTS.md for installation, configuration, machine-readable CLI/MCP interfaces and operational boundaries.
