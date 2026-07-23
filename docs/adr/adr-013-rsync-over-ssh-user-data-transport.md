# ADR-013: rsync-over-SSH as User-Data Transport (Root via sudo)

Status: Accepted

Date: 2026-06-30

## TL;DR
Use rsync-over-SSH as the user-data transport, running rsync as root on both ends via sudo to preserve cross-owner file metadata across /home and /root.

## Implementation Rules

**Required:**
- Run rsync as root on the source: `sudo rsync ...`
- Run rsync as root on the target: pass `--rsync-path='sudo rsync'` in the local rsync invocation, over the existing normal-user SSH connection
- Both endpoints require a passwordless sudoers entry that grants at least the rsync binary path, e.g. `username ALL=(ALL) NOPASSWD: /usr/bin/rsync`. A broader grant is equally acceptable — this is a lower bound on what must be permitted, not a requirement that the entry be scoped to exactly this one binary. Other jobs need their own binaries, and a machine may legitimately grant the user wider sudo rights for unrelated reasons
- Always pass `--numeric-ids` to preserve raw UID/GID numbers across machines. rsync's default maps ownership by *name* — it rewrites each file's UID/GID to whatever the target's account table assigns that name. For exact machine-state replication that is wrong: it produces target-table-dependent, non-deterministic ownership and silently diverges from the source's numeric layout. `--numeric-ids` makes ownership a pure function of the source (see RESEARCH Pitfall 3)
- Use the flag baseline `-aAXHS` (archive + ACLs + xattrs + hard links + sparse) per D-13
- Drive rsync via asyncio subprocess (`asyncio.create_subprocess_exec` or equivalent) — never a blocking call (ADR-005)
- Use rsync's own SSH transport via `-e 'ssh ...'`, honoring the user's `~/.ssh/config` for hostname, port, identity, and known_hosts (ADR-002 hostname model)
- Preserve the SSH identity under sudo: pass `-E` to sudo or explicitly specify `-i <identity_file>` in the `-e ssh` argument, because root's SSH environment differs from the normal user's (RESEARCH Pitfall 1)

**Forbidden:**
- Do NOT enable root SSH login; the SSH session remains as the normal user — privilege escalation applies only to the rsync binary via sudo
- Do NOT use a blocking subprocess call (e.g., `subprocess.run()`) in the async event loop (ADR-005)

## Context
PC-switcher syncs /home and /root between machines. This requires reading and writing files owned by multiple users and preserving POSIX ACLs, xattrs, and owner/group metadata — all of which need root access on both ends.

rsync was chosen over btrfs send/receive (D-04) because it provides filter rules, `--delete`, metadata preservation, built-in transfer verification, hard-link and sparse-file handling, and ACL/xattr support in a single proven tool. SFTP was rejected as it would require hand-rolling all of these capabilities.

The asyncssh connection (ADR-002) handles orchestration only; rsync spawns its own independent SSH transport using the system `ssh` binary, honoring the user's `~/.ssh/config`.

## Decision
- rsync-over-SSH is the user-data transport, chosen over btrfs send/receive (D-04)
- rsync runs as root on both ends: local `sudo rsync` on the source; `--rsync-path='sudo rsync'` on the target, reached over the normal-user SSH connection (D-05)
- Root privilege is required to read/write files of all owners across /home and /root and to preserve owner/group metadata
- The asyncssh connection (ADR-002) is for orchestration; rsync uses its own system-ssh transport

## Consequences

**Positive:**
- Full metadata preservation: owner, group, permissions, POSIX ACLs, xattrs, mtimes, hard links, sparse files
- rsync's built-in transfer verification, filter rules, `--delete`, and partial-transfer resume require no additional implementation
- Normal-user SSH login is preserved; privilege escalation is narrowly scoped to the rsync binary path

**Negative:**
- Requires a passwordless sudoers entry on both machines granting at least `/usr/bin/rsync` — a system configuration prerequisite users must apply before first sync; `validate()` must check and fail fast if absent, and say concretely how to configure it
- Shared-extent (reflink/CoW) topology is NOT preserved: rsync copies file data but does not replicate btrfs reflink relationships (accepted tradeoff, D-13)
- SSH identity under sudo requires explicit configuration to avoid authentication failures when root's SSH environment lacks the user's agent or config (RESEARCH Pitfall 1)

## References
- ADR-002: SSH as communication channel (orchestration layer; rsync uses its own independent SSH transport)
- ADR-005: Asyncio concurrency (rsync subprocess must be async and non-blocking)
- D-04: Transport is rsync-over-SSH (01-CONTEXT.md)
- D-05: rsync runs as root on both ends via sudo (01-CONTEXT.md)
- D-13: rsync flag baseline `-aAXHS` (01-CONTEXT.md)
- RESEARCH.md Pattern 1: rsync invocation (root on both ends)
- RESEARCH.md Pitfall 1: SSH identity breaks under sudo
- RESEARCH.md Pitfall 3: `--numeric-ids` missing lets rsync remap ownership by name, breaking exact replication
