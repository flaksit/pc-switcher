# Architecture Review

**Scope**: Review of the foundation plan artifacts (`architecture.md`, `plan.md`, `data-model.md`, `contracts/`, `quickstart.md`) for architectural soundness, simplicity, and constitution alignment.

## Strengths
- Clear layering between CLI → orchestrator → jobs, with explicit job contract (`contracts/job-interface.md`) that should keep future sync jobs plug-and-play.
- Three-phase validation pipeline and pre/post snapshot guards in `architecture.md` reinforce reliability-first intent and match the constitution’s “Reliability Without Compromise”.
- Single SSH connection and job isolation keep the core orchestration simple while leaving room for later job-level concurrency.
- Documentation coverage is high for a foundation phase (diagrams, data model, quickstart), which aligns with the “Up-to-date Documentation” principle.

## Issues / Risks
1. **Locking design drift (reliability, simplicity)**: `architecture.md` describes a long-lived target lock tied to the SSH session (`flock -n ... -c 'cat'`), while `research.md` shows a one-shot `flock` that releases immediately after writing the file. If the shorter pattern is implemented, concurrent A→B and C→B syncs will not be prevented. The plan should pick one mechanism (the session-tied approach) and specify how it is verified in tests.
2. **Event bus backpressure (reliability)**: Event queues are unbounded and producers use `put_nowait()` with “guaranteed delivery” claims in `architecture.md`. A stalled UI/file logger would allow unbounded growth and OOM, which conflicts with “Reliability Without Compromise” and “Deliberate Simplicity”. Consider bounded queues with drop/backpressure policy, and define behavior when consumers die (e.g., fail-fast vs. shedding logs).
3. **No SSH reconnection/continuation story (reliability, UX)**: The plan assumes a single persistent connection but does not cover reconnect/backoff or partial job restart if the link blips (common on laptops). Without guidance, implementations will likely abort entire runs on transient drops, undermining smooth UX. Recommend adding a recovery policy (retry budget, which phases/jobs are safe to retry, when to abort) to keep behavior predictable.
4. **SSD-wear measurement missing (constitution gap)**: The constitution requires measuring/estimating write volume per run and triggering remediation when thresholds are exceeded. Current artifacts mention CoW snapshots and minimal temp files but no plan for capturing or reporting write metrics (e.g., `btrfs fi usage`, `iostat`, or rsync stats) or surfacing them in logs/UI. Add a lightweight measurement plan so later features don’t bolt this on ad hoc.
5. **Config leniency vs. reliability**: `contracts/config-schema.yaml` sets `additionalProperties: true` at the top level, so typos in keys (e.g., `log_cli_levl`) silently succeed. That erodes the clear validation story in `architecture.md`. Consider rejecting unknown top-level keys (or at least logging WARN) while still allowing forward-compatible job configs under a dedicated namespace.
6. **Install/upgrade dependency on live internet**: Installation flows (`architecture.md` Self-Installation Flow, `quickstart.md`) assume fetching scripts and packages from GitHub and apt in real time. For LAN-only sync scenarios this blocks “Frictionless Command UX”. Suggest documenting an offline/air-gapped path (cached wheel/artifact and install.sh from the source machine) so foundation doesn’t bake in an always-online requirement.

## Overall
The architectural direction is clear and not over-complicated, but the reliability story has a few gaps (locking consistency, event bus backpressure, connection recovery, SSD-wear metrics). Addressing these in the plan will better align with the constitution and avoid expensive rework when implementing the first real sync jobs.
