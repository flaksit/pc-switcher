---
status: testing
phase: 01-home-sync-mvp-user-data-sync
source: [01-VERIFICATION.md]
started: "2026-07-02T13:04:39Z"
updated: "2026-07-02T13:04:39Z"
---

## Current Test

number: 1

name: A→B Byte-Identical Content Sync (ROADMAP SC1)

expected: Every included file exists on B and has the same md5sum as A; excluded files (.ssh/id_*, .config/tailscale, VS Code cache dirs) absent from B.

awaiting: user response

## Tests

### 1. A→B Byte-Identical Content Sync (ROADMAP SC1)

expected: Run `pc-switcher sync <B>` from A with the default /home and /root config; every included file exists on B and has the same md5sum as A; excluded files (.ssh/id_*, .config/tailscale, VS Code cache dirs) are absent from B.

why_human: Requires live rsync-as-root over SSH to real btrfs VMs.

result: [pending]

### 2. File Metadata Preservation (ROADMAP SC2)

expected: After A→B sync, numeric uid/gid, permissions, mtime, POSIX ACL entries, and hard-link inode sharing are identical between A and B for every synced file (compare `stat` + `getfacl`).

why_human: Requires real rsync with --numeric-ids and -aA on VMs with files owned by multiple users.

result: [pending]

### 3. Machine-Specific Exclusions vs Dev-Tool Caches (ROADMAP SC3)

expected: After A→B sync, machine-specific items (.ssh/id_*, .config/tailscale, GPU/shader + fontconfig caches, VS Code cache) are absent on B, while dev-tool caches (uv, pip, cargo, npm) ARE present.

why_human: Requires live VM execution to verify rsync --filter rules actually suppress or pass the right files.

result: [pending]

### 4. Round-Trip Propagation (ROADMAP SC4)

expected: After A→B, mutate B (add/modify/delete), then run `pc-switcher sync <A>` from B; A reflects B byte-identically — addition present on A, modified file has new content (md5sum match), deleted file absent on A (propagated by --delete), exclusions honored in reverse, metadata preserved.

why_human: Requires bidirectional VM execution and deletion propagation via --delete across real SSH.

result: [pending]

### 5. Two-Consecutive-Sync Live Check — topology out-of-order step (D-07, now unblocked by ADR-015)

expected: On a real machine pair with default /home config, run `pc-switcher sync <target>` twice in a row without --allow-out-of-order. First sync succeeds (or prompts W1/W3 if no prior history — answer y); second sync exits 0; a clean A→B / B→A / A→B alternation proceeds silently.

why_human: Requires destructive --delete mirror of real /home on a real machine pair; blocked on VMs.

result: [pending]

### 6. Full VM Integration Test Suite (ROADMAP SC5)

expected: Run `tests/run-integration-tests.sh tests/integration/test_folder_sync.py` against the Hetzner pc1/pc2 VMs (HCLOUD_TOKEN + VM env vars set, branch pushed to origin). All integration tests pass, including the topology-model round-trip and the out-of-order/dry-run scenario (`test_out_of_order_and_dry_run`, renamed from the old divergence-guard test by plan 01-14).

why_human: Requires live Hetzner VM infrastructure and SSH access; cannot run offline.

result: [pending]

## Summary

total: 6

passed: 0

issues: 0

pending: 6

skipped: 0

blocked: 0

## Gaps
