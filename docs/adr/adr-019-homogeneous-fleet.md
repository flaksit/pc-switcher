# ADR-019: Homogeneous fleet — matching real users and paths across machines

Status: Accepted

Date: 2026-07-21

## TL;DR
The fleet's real (human) user accounts — uid/gid >= 1000 — are assumed identical across machines (same name, uid/gid, and home path); pc-switcher does no user, id, or path mapping. System accounts (uid/gid < 1000) may differ and are not relied upon.

## Implementation Rules
- The real user(s) a person logs in as (uid/gid >= 1000) MUST match across machines: same username, uid, gid, and home path. Sync jobs rely on this.
- System/service accounts (uid/gid < 1000) are NOT assumed identical across machines; nothing may depend on them matching.
- rsync transfers MUST pass `--numeric-ids` (ADR-013): ownership is copied as the raw uid/gid, a pure function of the source. For real-user-owned files this reproduces the correct account on the target; a system-owned file under a synced tree keeps its source number even if that maps to a different or absent account on the target (accepted).
- A job MAY compute a target-side absolute path from the local one (e.g. `Path.home() / relpath`) for the invoking user, since that real user's home resolves to the same path on both machines.
- No user, uid/gid, or path mapping is performed anywhere; there is no preflight that detects a mismatch, so keeping the real users uniform is the operator's responsibility.

## Context
pc-switcher's goal is near-complete system-state replication between a small set of the same operator's machines (`docs/planning/High level requirements.md`) — the machines are interchangeable clones for that person, not independently administered multi-tenant hosts. The real user(s) are the same person's accounts, kept uniform (name, uid/gid, home path). System accounts are assigned per distro/install and may diverge; the sync does not depend on them matching. Components already rely on the real-user assumption implicitly: folder_sync mirrors `/home` and `/root` with `--numeric-ids` and no name mapping (ADR-013), and vscode_state_sync computes each machine's `state.vscdb` path from `Path.home()` on the assumption that the invoking user's home resolves identically. This ADR states the assumption explicitly so future jobs can depend on it and reviewers can reject accidental per-machine "smartness."

This ADR covers the real-user / identity / path axis of fleet homogeneity. Other axes are assumed too and documented elsewhere — do not duplicate them here: CPU architecture (ADR-017, with a `uname -m` preflight guard) and OS plus filesystem (Ubuntu 24.04 LTS on a single flat btrfs filesystem — `docs/planning/High level requirements.md`).

## Decision
- Assume the real user accounts (uid/gid >= 1000) are identical across the fleet: same names, uid/gid numbering, and home/filesystem paths.
- Do not assume system accounts (uid/gid < 1000) match; do not rely on them.
- Perform no user/id/path mapping anywhere; treat a real user's file identity and location as machine-independent.
- Keep `--numeric-ids` on all rsync transfers (ADR-013) as the concrete enforcement of the ownership half of this assumption.

## Consequences
**Positive**:
- Jobs stay simple for real-user data (the sync's focus): a path or owner computed on the source is valid on the target verbatim — no mapping tables, no per-machine translation.
- Real-user ownership and paths are deterministic and match the source exactly (true replication).

**Negative**:
- A file owned by a system account (uid/gid < 1000) under a synced tree may land with a numerically-preserved owner that denotes a different or absent account on the target — accepted, not guarded.
- Heterogeneous real-user layouts (different usernames, uids, or home paths) are unsupported and misbehave silently — files owned by the wrong account or written to the wrong path — because no guard detects the mismatch.
- Introducing per-machine user/path mapping later would require revisiting every job that assumes machine-independent identity.

## References
- ADR-013: rsync-over-SSH as user-data transport (`--numeric-ids`)
- ADR-017: single-architecture fleet (a related homogeneity constraint, on CPU arch)
- `docs/planning/High level requirements.md` (near-complete system-state replication goal; Ubuntu 24.04 + single btrfs filesystem — the OS/filesystem homogeneity axis)
