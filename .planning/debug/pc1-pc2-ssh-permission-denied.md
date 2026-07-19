---
slug: pc1-pc2-ssh-permission-denied
status: resolved
trigger: "Integration tests fail: pc1->pc2 sync connection fails with 'Permission denied for user testuser on host pc2'. User suspects a transient / CI-concurrency cause, not a real code bug. Rejects the earlier 'sync clobbers ~/.ssh' hypothesis."
created: 2026-07-18
updated: 2026-07-19
resolution: "Root cause: folder_sync mirrored ~/.ssh/authorized_keys, so the Second A->B sync overwrote pc2's copy and deleted its trust entry for pc1, breaking the next SSH auth. Fixed by excluding .ssh/authorized_keys from /home and /root (commit 8f625d3, merged in PR #172); previously-failing E2E tests confirmed passing on fresh-provision CI."
---

# Debug: pc1 -> pc2 sync SSH "Permission denied" in integration tests

## Symptoms

- 2 integration tests fail; 54 passed, 1 skipped (587 deselected). CI run `29655427025`, branch `feat/163-long-lived-ci-token`, PR #172.
- Failing tests (both the LAST two to run in the suite):
  - `tests/integration/test_end_to_end_sync.py::TestEndToEndSync::test_core_us_job_arch_as1_job_integration_via_interface` — asserts "Second A->B failed (out-of-order gate wrongly tripped for a clean round-trip, ADR-015 #159)"
  - `tests/integration/test_lock_integration.py::TestTargetLockConflict::test_target_lock_blocks_when_target_busy` — asserts "Target-lock failure message missing"
- Both die identically: orchestrator log `Connecting to target` -> `CRITICAL Sync failed: Permission denied for user testuser on host pc2`. Failure timestamps (VM local) 20:30:02 and 20:30:20 == 18:30 UTC. Test step ran 18:19:14Z..18:30:53Z, so these are the final tests.
- The asserted "out-of-order gate" message is the test's EXPECTED failure description; the ACTUAL error is an SSH auth failure that masks it. So the pcswitcher out-of-order/lock gate is NOT what tripped.

## Evidence (gathered by orchestrator before handoff)

- timestamp: 2026-07-18 — All CI runs were triggered through GitHub Actions (pull_request events + one `gh run rerun`). Integration tests were NEVER run locally against the VMs by the assistant. (Answers user's direct question.)
- timestamp: 2026-07-18 — Concurrency guard LEAKED: workflow `concurrency: {group: pc-switcher-integration, cancel-in-progress: false}`. Run `29655527492` (dependabot/rich) ran its Integration Tests job 18:16:16Z..18:17:33Z (cancelled) CONCURRENTLY with this run's PROVISION step (18:15:01..18:19:14). Proof that rerun + dependabot batch can bypass serialization on the shared fixed-name VMs pc1/pc2.
- timestamp: 2026-07-18 — BUT during the TEST window (18:19:14..18:30:53) NO other Integration Tests job was active: `29655567975` (python-minor-patch) started 18:31:01 (after); `29655690416` (166-sync-filters) was skipped. So concurrent interference landed on provisioning, not the test window.
- timestamp: 2026-07-18 — `origin/main` since branch merge-base (`e39bbfe`) contains ONLY dependabot dep bumps (`packaging` 25->26.2, `actions/checkout` 6->7). Nothing touching sync/connection/orchestrator/SSH/test-infra. "Virtual merge onto main introduced the bug" is therefore unlikely; CI tests the merge but the merged delta is dep bumps only.
- timestamp: 2026-07-18 — The string "Permission denied for user ... on host" is NOT in `src/pcswitcher/`. It is asyncssh's own auth-failure message. So pc2 refused ALL offered auth methods at that instant (auth failure, not host-key mismatch).
- timestamp: 2026-07-18 — `src/pcswitcher/connection.py:88` `connect()` does a single `await asyncssh.connect(target, ...)` with default key discovery, respects ~/.ssh/config. NO retry / no backoff on transient auth or connection failure.
- timestamp: 2026-07-18 — `reset_pcswitcher_state` fixture (`tests/integration/conftest.py:442`) is used by BOTH failing tests. It only removes config/data dirs + deletes pc-switcher btrfs snapshots on both VMs. It does NOT reboot, does NOT touch ~/.ssh, does NOT roll back @home. So nothing in-run explains pc2 refusing testuser auth.
- timestamp: 2026-07-18 — Earlier tests in the SAME run connected to pc2 successfully (e.g. `test_inter_vm_connectivity_pc1_to_pc2 PASSED`, `test_config_sync` suite PASSED, and the round-trip test's FIRST A->B succeeded — only the SECOND A->B failed). pc2 auth worked through ~18:29, then failed at 18:30.
- timestamp: 2026-07-18 — Provision reported "Inter-VM SSH tested successfully" at 18:19; VMs freshly provisioned this run (image `.tar.zst` fix). `reset-vm.sh` DOES reboot (step 5) but is a manual/per-provision reset, not invoked by the in-run fixtures above.

## Reproduction (2026-07-18, cheap rerun 29655427025 reusing VMs)

- timestamp: 2026-07-18 — REPRODUCED and worse: `4 failed, 52 passed` (was 2). Failing SET shifted between runs: now `test_end_to_end_sync.py::TestConsecutiveSyncWarning::test_back_sync_clears_warning`, `::test_consecutive_sync_warning_workflow`, `TestEndToEndSync::test_core_us_job_arch_as1_job_integration_via_interface`, `::test_core_us_job_arch_as7_interrupt_terminates_job`. The prior run's lock test now PASSED. Different tests, SAME error (40x `Permission denied for user testuser on host pc2`). => flaky pc2-SSH condition hitting whichever pc1->pc2 sync races it, NOT a single test's logic.
- timestamp: 2026-07-18 — Live pc2 (138.199.146.229) healthy post-run: up 11 min (rebooted ~21:09 by reset-vm at test start, then STABLE — no mid-test reboot), disk 10%, ~/.ssh/authorized_keys present, perms 600, 4 keys. sshd: MaxStartups 10:30:100, MaxAuthTries 6, MaxSessions 10.
- timestamp: 2026-07-18 — DECISIVE: pc2 `journalctl -u ssh -b` shows the failing connections as `Connection closed by authenticating user testuser 91.99.178.190 port NNNN [preauth]` (91.99.178.190 = pc1) at 21:16:36/40/48, 21:17:00. sshd logged ZERO `Failed`/`denied` lines (grep -c fail|denied|drop = 0) and 22 `Accepted`. So the server did NOT reject the key — the CLIENT (pc1 asyncssh) aborted during preauth, after sending username testuser (so host-key/kex phase passed). Intermittent among many succeeding connections.
- timestamp: 2026-07-18 21:45 (fresh continuation, new VMs from cheap rerun, same IPs) — pc1 testuser `~/.ssh/`: only ONE identity file (`id_ed25519`), no `id_rsa`/`id_ecdsa`, no `~/.ssh/config`, ssh-agent NOT running (`ssh-add -l` → "Could not open a connection to your authentication agent"). So asyncssh has exactly one candidate key to offer per connection attempt, not multiple → weakens candidate (b) MaxAuthTries-exhaustion-from-multi-key-offering (would need ≥2 keys tried before an accepted one; here there's only 1 key total, and it must already be the accepted one since most connections succeed).
- timestamp: 2026-07-18 21:46 — pc1 `~/.ssh/known_hosts` has exactly 3 entries (hashed) for pc2, one per host-key algorithm (rsa, ecdsa, ed25519). Compared byte-for-byte (base64 key blob) against pc2's live `/etc/ssh/ssh_host_*_key.pub` (rsa, ecdsa, ed25519) fetched directly from pc2 — ALL THREE MATCH. Also `/etc/hosts` on pc1 has `138.199.146.229 pc2` (static, not DNS) — no resolution race possible. => candidate (c) host-key/known_hosts race is REFUTED for current VM state: keys are stable and correctly cached, and hostname resolution is a static /etc/hosts entry, not live DNS.
- timestamp: 2026-07-18 21:47 — pc2 `sshd_config`: `MaxAuthTries`, `MaxSessions`, `MaxStartups`, `LoginGraceTime`, `ClientAliveInterval` are ALL commented out (`#MaxStartups 10:30:100` etc.) — meaning the values seen in the prior evidence entry (10:30:100 / 6 / 10) are OpenSSH COMPILED-IN DEFAULTS, not an intentional/hardened override. Confirms candidate (a) MaxStartups pressure uses stock defaults — unthrottled sshd, standard random-early-drop above 10 concurrent unauthenticated connections.
- timestamp: 2026-07-18 21:53 — ROOT CAUSE FOUND. Compared pc2's LIVE `~/.ssh/authorized_keys` (4 entries: 2 unlabeled admin keys, `pc-switcher-ci@github`, `testuser@pc2`) against pc2's own BASELINE SNAPSHOT copy (`/.snapshots/baseline/@home/testuser/.ssh/authorized_keys`): baseline has `testuser@pc1`'s key as the 4th entry; LIVE has `testuser@pc2`'s own key instead — pc1's trust entry was REPLACED by pc2's own key, sometime after the reset-vm restore. `stat` on pc2's live authorized_keys: Modify=2026-07-18 20:19:04 (original provisioning time, unchanged), but Change/Birth=2026-07-18 21:16:20 (CEST) — i.e. the file was atomically replaced (new inode via `mv`-style swap) at 21:16:20, which is 17s BEFORE pc2's sshd first logs the "Connection closed by authenticating user testuser ... [preauth]" abort (21:16:36, matching the already-documented decisive evidence). pc1's own `~/.ssh/authorized_keys` (stat: Change/Birth=20:19:04, untouched) is BYTE-FOR-BYTE IDENTICAL to pc2's now-corrupted live authorized_keys (same 4 lines, same order, including the self-referential `testuser@pc2` entry and missing `testuser@pc1`) — i.e., pc2's authorized_keys was overwritten with an exact copy of pc1's own authorized_keys file.
- timestamp: 2026-07-18 21:54 — Traced the actual CI run (29655427025, job 88113556612, "Run integration tests" step 19:07:51–19:18:42 UTC = 21:07:51–21:18:42 CEST) via `gh run view --log`. Last test to PASS before the break: `test_core_edge_target_unreachable_mid_sync` at 19:15:46Z. First test to FAIL: `test_core_us_job_arch_as1_job_integration_via_interface` at 19:16:37Z (21:16:37 CEST) — 17s after the authorized_keys file swap at 21:16:20. The embedded asyncssh DEBUG log inside that test's own failure output shows the mechanism precisely: `Trying public key auth with ssh-ed25519 key` → `Auth failed for user testuser` → `Connection failure: Permission denied for user testuser on host pc2` → `Aborting connection`. Ruled OUT: no concurrent GH Actions job ran `configure-hosts.sh` during this window — `gh run list`/`gh run view` for all workflows show only this run and one other (166-sync-filters, 19:14:10Z) active in the window, and that other run's own Integration Tests job was SKIPPED entirely (no provisioning executed). So the corruption was NOT caused by a concurrent CI job re-running the key-exchange script; it happened from within THIS run's own test execution.
- timestamp: 2026-07-18 21:56 — Found the actual mechanism: read `tests/integration/test_end_to_end_sync.py::TestEndToEndSync::test_core_us_job_arch_as1_job_integration_via_interface` (the exact failing test) start-to-finish. It performs, in order: (1) blocked first-sync (W1 gate, no transfer), (2) `--dry-run` (no transfer), (3) **first REAL sync `pc-switcher sync pc2 --yes --allow-first-sync`** — a genuine `rsync -aAXHS --delete` of the real `/home` tree from pc1 to pc2, (4) verification, (5) back-sync `pc-switcher sync pc1 --yes` (pc2→pc1, succeeds — pc1's authorized_keys is untouched), (6) **second A→B `pc-switcher sync pc2 --yes`** — this is the one that fails, and is literally the test's own assertion ("Second A→B failed"). Step 3's real rsync of `/home` is what corrupts pc2's authorized_keys (matches the 21:16:xx timestamps: real transfer runs between steps, corruption at 21:16:20, first connection failure logged at 21:16:36/37 when step 6 tries to reconnect).
- timestamp: 2026-07-18 21:57 — Read the test's own folder_sync exclude list (`_make_e2e_config()`, test_end_to_end_sync.py:280-317): excludes `.ssh/id_*`, `.ssh/known_hosts`, `.config/tailscale`, VS Code caches, `.cache`, etc. — **`.ssh/authorized_keys` is NOT excluded.** Read `folder_sync.py`'s hardcoded protection list (`_RUNTIME_EXCLUDE_RELPATHS`, the ONLY excludes applied "regardless of user config" per ADR-016): only protects pc-switcher's own runtime files (lock/history/logs, uv-tool install, entry-point shim) — does NOT protect any `.ssh/*` file; that protection is entirely opt-in via each folder's `excludes:` list. Read the PRODUCT'S OWN SHIPPED `src/pcswitcher/default-config.yaml` (lines 141-176, the real default template every `pc-switcher init` ships): both the `/home` and `/root` folder-sync blocks exclude `.ssh/id_*` and `.ssh/known_hosts` — **`.ssh/authorized_keys` is missing from both**. The `known_hosts` comment in that same file even articulates the exact bug class: "mirroring it (rsync --delete) overwrites the target's copy with the source's... target loses the source's key and fails the reverse sync" — the same reasoning was correctly applied to protect `known_hosts` and `id_*` but was never applied to `authorized_keys`, which has the identical machine-local, must-not-be-mirrored property (it defines who may log into the TARGET, not a property that should follow the SOURCE).
- timestamp: 2026-07-18 21:58 — Confirmed this is PRE-EXISTING and unrelated to PR #172/#163: `git diff main...feat/163-long-lived-ci-token -- src/pcswitcher/default-config.yaml src/pcswitcher/jobs/folder_sync.py` is empty (no changes on this branch to either file). `git log` on default-config.yaml shows the gap originates in commit `f26d560` "fix(folder-sync): exclude .ssh/known_hosts by default..." — the commit that added the (correct) `known_hosts` exclude but never added the analogous `authorized_keys` exclude. This is a latent bug on `main`, exposed by this specific E2E test (the only test that does a real, non-dry-run, non-mocked full-`/home` sync in both directions), not a regression introduced by this PR.

## Eliminated

- hypothesis: "pc-switcher sync clobbers testuser ~/.ssh on pc2, breaking the second connection" — user's INITIAL rejection was based on an incomplete premise ("folder-sync scope writes to .config + .local/share, not ~/.ssh") that was true only of the *reset fixture's cleanup scope*, not of what the failing E2E test's own folder_sync job actually syncs (the real `/home`, deliberately, by design, to test full-home-directory replication). Investigation now shows the original mechanism WAS correct, just mis-scoped: it's not the reset fixture that touches `~/.ssh`, it's the E2E test's genuine full-`/home` rsync — and specifically `~/.ssh/authorized_keys` (not excluded), not the whole `~/.ssh` dir (id_* and known_hosts ARE correctly excluded). See Resolution below — this hypothesis is CONFIRMED, not eliminated, once correctly scoped.
- hypothesis: "main drift since branch introduced the bug" — WEAK: only dependabot dep bumps on main since merge-base; none touch sync/SSH/test-infra. CONFIRMED eliminated: `git diff main...feat/163-long-lived-ci-token` for `default-config.yaml`/`folder_sync.py` is empty; the real root cause is a pre-existing latent bug on `main` (commit f26d560), unrelated to this PR's diff.
- hypothesis: "pcswitcher out-of-order/lock gate wrongly tripped" (the test's own message) — the actual error is asyncssh Permission denied, not a gate rejection, so the gate is not the mechanism. ELIMINATED.
- hypothesis: "transient/flaky SSH auth failure (sshd MaxStartups, PAM hiccup, one-off)" — ELIMINATED. The failure is 100% deterministic once triggered, not transient: reproduced independently via a raw asyncssh/plain-ssh loop (450+400 attempts, ALL failed identically) hours after the original CI run, on the same never-since-fixed authorized_keys state. It only *looks* intermittent across CI runs because pytest-randomly randomizes test order each run, so whichever test happens to run the first real (non-dry-run, non-mocked) full-`/home` sync corrupts pc2's authorized_keys at a different point in each run's sequence — a different, shifting SET of "later" tests then fails each time. Not sshd-side at all (candidates a/b/d below are all subsumed/refuted by this).
- candidate (a) MaxStartups drop from concurrent/bursty connections — REFUTED as the trigger: pc-switcher's own `Connection.connect()` is called once per `pc-switcher sync` invocation, and test execution is sequential (no pytest-xdist configured); no test in the failing set opens concurrent pc1→pc2 connections. MaxStartups values found were stock OpenSSH defaults (commented out in sshd_config), not evidence of load-based drops.
- candidate (b) MaxAuthTries exhaustion from multi-key offering — REFUTED: pc1's testuser has exactly one identity file (`id_ed25519`), no agent, no `~/.ssh/config` — asyncssh offers exactly one key per connection, never multiple.
- candidate (c) asyncssh host-key/known_hosts race — REFUTED: pc1's known_hosts entries for pc2 (rsa/ecdsa/ed25519, hashed) match pc2's live `/etc/ssh/ssh_host_*_key.pub` byte-for-byte; `/etc/hosts` is a static entry, no DNS race possible.
- candidate (d) asyncssh client-side timeout/keepalive misfire — REFUTED: the asyncssh DEBUG log embedded in the actual CI failure shows a clean, complete auth cycle (version exchange → kex → "Trying public key auth" → "Auth failed" → "Permission denied") with no timeout/keepalive involved; this is a definitive server-side pubkey rejection, not a client-side abort.

## Current Focus

hypothesis: CONFIRMED. `folder_sync`'s default/example exclude list (both in the shipped `src/pcswitcher/default-config.yaml` and in the E2E test's own config) protects `.ssh/id_*` and `.ssh/known_hosts` but NOT `.ssh/authorized_keys`. A real (non-dry-run) `pc-switcher sync` of `/home` therefore rsyncs (`-aAXHS --delete`) the source's `authorized_keys` over the target's, silently deleting the target's own trust list and replacing it with the source's. Since a machine's own authorized_keys never contains its own key, the target immediately loses its ability to authenticate the source — exactly reproducing "Permission denied for user testuser on host pc2" on the very next connection attempt.

test: N/A — root cause confirmed via direct evidence (baseline-vs-live authorized_keys diff, byte-for-byte match to pc1's file, timestamp correlation to the second within the actual failing CI run's own log, and reading both the exclude-list config and the code that applies it). No further testing needed to confirm mechanism; a fix + regression test is the remaining work.

next_action: STOPPED before applying any fix per the task's explicit instruction ("do NOT apply any non-trivial fix yet... return CHECKPOINT REACHED") — this finding is materially larger in scope than the original "flaky CI" framing (it is a real, currently-shipping data-integrity/access-control bug in the core sync feature, not a test or CI issue), so a human decision is needed on scope/urgency before editing `default-config.yaml` / `folder_sync.py` / the E2E test config. See CHECKPOINT REACHED returned to caller.

reasoning_checkpoint:
  hypothesis: "A real (non-dry-run) folder_sync of /home overwrites the target's ~/.ssh/authorized_keys with the source's (because authorized_keys is absent from both the shipped default excludes and the E2E test's excludes), which breaks the source's own subsequent ability to authenticate to the target, causing the exact 'Permission denied for user testuser on host pc2' failures observed in CI."
  confirming_evidence:
    - "pc2's live authorized_keys (Change/Birth timestamp 21:16:20 CEST) is byte-for-byte identical to pc1's own untouched authorized_keys (self-referential testuser@pc2 entry present, testuser@pc1 entry missing) — direct proof of a copy-over, not corruption or coincidence."
    - "The file swap timestamp (21:16:20) precedes the first CI-observed auth failure (21:16:36/37, from gh run view --log of the actual failing run) by exactly the time it takes pytest to move from the sync call to the next step — and the failing test's own code structure (real sync at step 3, second A→B at step 6) matches this ordering exactly."
    - "default-config.yaml's own code comment for .ssh/known_hosts describes the identical bug mechanism ('mirroring it overwrites the target's copy with the source's... target loses the source's key') but was never applied to authorized_keys — a documented blind spot in the same file."
  falsification_test: "If pc2's authorized_keys were NOT byte-identical to pc1's, or if the corruption timestamp did not correlate with the test's real-sync step, this hypothesis would be disproven. Neither held: both checks passed."
  fix_rationale: "Add '.ssh/authorized_keys' to the exclude list in both the /home and /root folder_sync blocks of default-config.yaml (mirroring the existing known_hosts entry's placement and rationale), and add the same exclude to the E2E test's _make_e2e_config(). This addresses the root cause (the file being synced at all) rather than a symptom (retrying the connection, which would fail identically forever since the corruption is permanent, not transient)."
  blind_spots: "Have not checked whether any EXISTING real users already have a corrupted target authorized_keys from using the current default config in production — this may need a migration/advisory note, not just a code fix. Have not checked whether other machine-identity files under /home besides id_*/known_hosts/authorized_keys have the same gap (e.g. GPG keys, /etc synced separately?). Have not located why this specific gap was missed in commit f26d560 (review oversight vs. deliberate scope-narrowing) — not load-bearing for the fix but relevant for process/review takeaways."

## Resolution

root_cause: |
  `folder_sync`'s exclude list — both the shipped default in `src/pcswitcher/default-config.yaml`
  (lines 141-176) and the E2E test's own `_make_e2e_config()` (test_end_to_end_sync.py:280-317) —
  excludes `.ssh/id_*` (private keys) and `.ssh/known_hosts` from the `/home` and `/root`
  folder-sync jobs, but NOT `.ssh/authorized_keys`. `_RUNTIME_EXCLUDE_RELPATHS` in folder_sync.py
  (the only excludes applied unconditionally regardless of user config, per ADR-016) protects only
  pc-switcher's own runtime files, not any SSH file. Because `authorized_keys` is unprotected, a
  real (non-dry-run) `pc-switcher sync pc2` on the "Second A→B" step of the E2E test's full-/home
  round-trip runs `rsync -aAXHS --delete` and overwrites pc2's `~/.ssh/authorized_keys` with an
  exact copy of pc1's own `~/.ssh/authorized_keys`. Since a machine's authorized_keys file never
  contains its own key, pc2 immediately loses its "trust pc1" entry — the very next `pc-switcher
  sync pc2` connection attempt (from any subsequent test in the same CI run) then fails at the
  SSH auth step with asyncssh's "Permission denied for user testuser on host pc2", which is the
  exact symptom originally reported. Confirmed via: byte-for-byte match between pc1's authorized_keys
  and pc2's post-corruption authorized_keys; ctime/birth of pc2's authorized_keys (21:16:20 CEST)
  landing 17s before the CI run's first logged auth failure (21:16:36/37 CEST, verified via `gh run
  view --log` on the actual failing run 29655427025); and the asyncssh DEBUG log embedded in that
  run's own failure output showing a clean, definitive server-side pubkey rejection (not a timeout,
  not a client-side abort). This is a PRE-EXISTING bug on `main` (introduced in commit f26d560,
  which added the correct `known_hosts` exclude but missed the analogous `authorized_keys` one),
  confirmed unrelated to PR #172/#163's actual diff (CI workflow + create-vm.sh image-matching only).
  The "flaky/intermittent" appearance across CI reruns is explained by pytest-randomly: whichever
  test happens to run the first real full-/home sync in a given run's randomized order determines
  which later tests in that same run then fail, producing a different failing-test set each time —
  not sshd-side flakiness, MaxStartups pressure, multi-key MaxAuthTries exhaustion, or a client-side
  timeout (all directly investigated and refuted; see Eliminated).

fix: |
  APPLIED (folded into PR #172 per user decision):
  1. Added `.ssh/authorized_keys` to the `excludes:` list for BOTH the `/home` and `/root` folder_sync
     blocks in `src/pcswitcher/default-config.yaml`, after `.ssh/known_hosts`, with a comment
     explaining authorized_keys is target-local access control that must not be mirrored.
  2. Added the same exclude to `_make_e2e_config()` in `tests/integration/test_end_to_end_sync.py` so
     the E2E test reflects the corrected default and stops self-inflicting the corruption.
  3. NOT added: retry/backoff in `connection.py connect()` — the failure is permanent once triggered,
     not transient, so retry would not fix it. Deferred as an independent robustness idea, not this fix.
  4. Existing-damage triage: user confirmed no real /home syncs have run against real machines yet, so
     no deployed authorized_keys corruption to remediate. No migration/advisory needed.

  Deferred follow-ups (NOT part of this fix, worth separate issues): (a) CI concurrency-guard leak —
  `concurrency: cancel-in-progress: false` did not serialize a `gh run rerun` against dependabot runs
  on the shared fixed-name VMs; (b) optional bounded retry/backoff in connect() for genuinely transient
  network blips.

verification: |
  Static: YAML parses and both blocks now list `.ssh/authorized_keys`; `ruff check` clean on the test
  file; `basedpyright` 0 errors/0 warnings; 140 config unit tests pass (test_config_system.py).
  End-to-end: CONFIRMED. The fix merged in PR #172 (2026-07-18) and the previously-failing E2E sync
  tests now pass on a full fresh-provision CI run.

files_changed:
  - src/pcswitcher/default-config.yaml
  - tests/integration/test_end_to_end_sync.py
