---
schema_version: 1
open_count: 0
waived_count: 0
fixed_count: 1
total_count: 1
last_updated: 2026-07-23T06:33:11.003Z
---

# Broken Windows Ledger

> Cross-phase defect register. `/gsd-ship` blocks while `open_count > 0`.
> Waive with `gsd-tools windows waive <id> "<reason>"` (reason required).
> Mark fixed with `gsd-tools windows fixed <id>`.

| id | phase | kind | file | line | description | status | reason | recorded_at | resolved_at |
|----|-------|------|------|------|-------------|--------|--------|-------------|-------------|
| 1 | 02 | deviation | src/pcswitcher/orchestrator.py |  | PackageItemFailures no longer aborts the run, but session.status/CLI exit code is still derived purely from whether an exception propagated (SessionStatus.COMPLETED unconditionally after job loop; CLI _run_sync returns 0 whenever orchestrator.run() doesn't raise), never from job_results content. A sync where one package manager's items failed can now exit 0. Fixing requires touching cli.py session-status logic, out of plan 02-03's stated 'two narrow changes' scope. | fixed |  | 2026-07-23T06:24:50.013Z | 2026-07-23T06:33:11.003Z |

````json
[
  {
    "id": 1,
    "kind": "deviation",
    "phase": "02",
    "file": "src/pcswitcher/orchestrator.py",
    "line": null,
    "description": "PackageItemFailures no longer aborts the run, but session.status/CLI exit code is still derived purely from whether an exception propagated (SessionStatus.COMPLETED unconditionally after job loop; CLI _run_sync returns 0 whenever orchestrator.run() doesn't raise), never from job_results content. A sync where one package manager's items failed can now exit 0. Fixing requires touching cli.py session-status logic, out of plan 02-03's stated 'two narrow changes' scope.",
    "status": "fixed",
    "reason": "",
    "recorded_at": "2026-07-23T06:24:50.013Z",
    "resolved_at": "2026-07-23T06:33:11.003Z"
  }
]
````
