---
schema_version: 1
open_count: 1
waived_count: 0
fixed_count: 2
total_count: 3
last_updated: 2026-07-23T10:31:16.493Z
---

# Broken Windows Ledger

> Cross-phase defect register. `/gsd-ship` blocks while `open_count > 0`.
> Waive with `gsd-tools windows waive <id> "<reason>"` (reason required).
> Mark fixed with `gsd-tools windows fixed <id>`.

| id | phase | kind | file | line | description | status | reason | recorded_at | resolved_at |
| -- | ----- | ---- | ---- | ---- | ----------- | ------ | ------ | ----------- | ----------- |
| 1 | 02 | deviation | src/pcswitcher/orchestrator.py |  | PackageItemFailures no longer aborts the run, but session.status/CLI exit code is still derived purely from whether an exception propagated (SessionStatus.COMPLETED unconditionally after job loop; CLI _run_sync returns 0 whenever orchestrator.run() doesn't raise), never from job_results content. A sync where one package manager's items failed can now exit 0. Fixing requires touching cli.py session-status logic, out of plan 02-03's stated 'two narrow changes' scope. | fixed |  | 2026-07-23T06:24:50.013Z | 2026-07-23T06:33:11.003Z |
| 2 | 02 | unrun-verify | .planning/phases/02-package-management-sync/02-06-PLAN.md |  | Plan 02-06's <verification> VM-level check (dry-run against a target missing one vendor repo, key+source shown as separate review entries, intended apt-get update reported) not run — no VM access in this autonomous session; deferred to plan 02-13's end-to-end suite, same precedent as plans 02-03/02-05. | open |  | 2026-07-23T10:28:30.221Z |  |
| 3 | 02 | deviation | src/pcswitcher/jobs/apt_sync.py |  | AptSyncJob.validate() checks passwordless sudo only on the target; the new /etc/apt/* sha256sum capture also runs 'sudo find' on the SOURCE, so a source machine without passwordless sudo degrades silently to empty digest maps (no repo state captured) instead of a validation error. | fixed |  | 2026-07-23T10:28:30.299Z | 2026-07-23T10:31:16.493Z |

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
  },
  {
    "id": 2,
    "kind": "unrun-verify",
    "phase": "02",
    "file": ".planning/phases/02-package-management-sync/02-06-PLAN.md",
    "line": null,
    "description": "Plan 02-06's <verification> VM-level check (dry-run against a target missing one vendor repo, key+source shown as separate review entries, intended apt-get update reported) not run — no VM access in this autonomous session; deferred to plan 02-13's end-to-end suite, same precedent as plans 02-03/02-05.",
    "status": "open",
    "reason": "",
    "recorded_at": "2026-07-23T10:28:30.221Z",
    "resolved_at": null
  },
  {
    "id": 3,
    "kind": "deviation",
    "phase": "02",
    "file": "src/pcswitcher/jobs/apt_sync.py",
    "line": null,
    "description": "AptSyncJob.validate() checks passwordless sudo only on the target; the new /etc/apt/* sha256sum capture also runs 'sudo find' on the SOURCE, so a source machine without passwordless sudo degrades silently to empty digest maps (no repo state captured) instead of a validation error.",
    "status": "fixed",
    "reason": "",
    "recorded_at": "2026-07-23T10:28:30.299Z",
    "resolved_at": "2026-07-23T10:31:16.493Z"
  }
]
````
