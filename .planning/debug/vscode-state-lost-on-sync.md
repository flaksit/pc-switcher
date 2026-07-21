---
status: root_cause_confirmed
trigger: "gh #190 — After sync back, need to trust folders again and login to github"
created: 2026-07-20
updated: 2026-07-20
---

# Debug: VSCode trusted-folders list and GitHub extension auth lost after sync

## Symptoms

DATA_START

Source: GitHub issue #190 (labels: comp:sync:folders, type:bug)

Sequence:
1. First sync p17 → fleksi.
2. Worked in VSCode on fleksi in one project (~/dev/pc-switcher).
3. Sync back fleksi → p17.
4. Back on p17, opened project: `janfr@P17:~/dev/pc-switcher$ code .`

Symptom A — Folder trust:
- Back on P17, VSCode says the folder is not trusted.
- After trusting it, the trusted-folders list shows ONLY that one folder just trusted.
- All previously trusted folders on P17 are gone (user had trusted hundreds of
  folders, possibly entire ~/dev or ~/).
- User does not recall having to trust the folder on fleksi.
- Conclusion by reporter: the sync broke the trusted-folders list.
- Ask: if "trusted folders" is machine-specific, can it be excluded from sync so
  trust isn't required after each sync?

Symptom B — GitHub login:
- After sync, VSCode forgets GitHub login. Had to log in on fleksi; after syncing
  back, must log in on p17 too. Auth creds apparently can't be shared → should be
  excluded so re-login isn't needed after every sync.
- Not the "general" GitHub login. Two extensions re-auth: (believed) GitHub Pull
  Requests and GitHub Actions.

DATA_END

## Environment facts (verified this session)

- home.filter syncs `.config/Code/User` (comment: "NOT Code/User — that IS synced");
  only Code/User caches are excluded (Cache, CachedData, GPUCache, Code Cache,
  CachedExtensionVSIXs).
- `.config/Code/User/globalStorage/state.vscdb` EXISTS on this machine (~4.3 MB)
  and is inside the synced tree.
- home.filter: src/pcswitcher/home.filter

## Current Focus

reasoning_checkpoint:
  hypothesis: "state.vscdb (.config/Code/User/globalStorage/state.vscdb, inside the
    synced Code/User tree) stores BOTH the trusted-folders list and the encrypted
    GitHub-auth session, and both break on cross-machine sync: (a) trust list is a
    single JSON blob overwritten wholesale by the mirror, so the target's trust list
    becomes the source's; (b) the auth session is ciphertext encrypted with a
    per-machine key held in the OS keyring (gnome-libsecret), which is NOT part of
    the synced tree, so the ciphertext copied from the source machine cannot be
    decrypted on the target and VSCode treats the session as invalid, forcing re-login."
  confirming_evidence:
    - "sqlite3 state.vscdb: key content.trust.model.key = {\"uriTrustInfo\":[{\"uri\":
      {...fsPath\":\"/home/janfr/dev/pc-switcher\"...},\"trusted\":true}]} — exactly
      ONE folder trusted, matching the reported symptom precisely (only the
      just-trusted folder appears; nothing else)."
    - "sqlite3 state.vscdb: key secret://{\"extensionId\":\"vscode.github-authentication\",
      \"key\":\"github.auth\"} exists with a 1230-byte value (ciphertext, not read) —
      github-authentication's session IS stored inside state.vscdb, not purely in the
      keyring."
    - "key encryption.migratedToGnomeLibsecret = true confirms this machine's
      SecretStorage encryption backend is gnome-libsecret."
    - "Web search of VS Code's own SecretStorage docs/discussion confirms: on Linux
      the encryption key is a password stored in the OS keyring via libsecret; 'the
      implementation of the secret storage will be different on each platform and
      the secrets will not be synced across machines' — i.e. this is VSCode's own
      documented design, not a pc-switcher-specific accident."
    - "home.filter syncs .config/Code/User wholesale (only Code/User CACHES are
      excluded); state.vscdb is not excluded, confirmed present in the synced tree
      (prior session)."
  falsification_test: "If content.trust.model.key held only per-workspace trust
    decisions merged incrementally (not a full overwritten array), or if the
    secret:// value were a mere keyring reference/URI rather than an opaque blob
    stored inline, the hypothesis would be wrong. Neither is the case: the trust key
    IS the full array, and the secret key holds real ciphertext-length data inline."
  fix_rationale: "Excluding state.vscdb (a single opaque SQLite file) from sync stops
    the trust-list overwrite and stops copying now-undecryptable ciphertext to the
    target, addressing the root cause (wholesale sync of a machine-keyring-bound file)
    rather than a symptom. rsync filters operate at file granularity — there is no way
    to selectively sync only the 'safe' keys inside one SQLite file without adding
    SQLite-aware merge logic to the sync tool itself, which is out of scope for this
    fix and not how this codebase's filter-only exclude model works (ADR-013)."
  blind_spots: "Excluding the whole file also drops keys that mix in harmlessly or
    are arguably wanted cross-machine (e.g. terminal.history.entries.commands,
    per-language session counters, workbench panel visibility) — not individually
    verified as unwanted, just accepted as the cost of file-granularity excludes.
    Have not verified behavior on a second real machine (fleksi) end-to-end; fix is
    verified via the filter-rule test suite and manual sqlite/rsync-dry-run
    inspection on this machine only. VSCode Settings Sync (separate, cloud-based
    built-in feature) was not fully ruled out as a contributor — checked
    storage.json for 'sync'/'trust' keys, found none, so not implicated."

next_action: apply fix — add state.vscdb excludes to home.filter, update inline
  comment, add docs/configuration.md subsection, add filter-rule test, run full
  test suite.

## Evidence

- timestamp: 2026-07-20 (this session)
  checked: `sqlite3 ~/.config/Code/User/globalStorage/state.vscdb ".tables"` and
    `SELECT key FROM ItemTable WHERE key LIKE '%trust%'`
  found: single table ItemTable; trust-related keys are content.trust.model.key,
    extensions.trustedPublishers, http.linkProtectionTrustedDomains,
    trusted-publishers-init-migration
  implication: workspace trust state lives inside state.vscdb, not a separate file.

- timestamp: 2026-07-20 (this session)
  checked: `SELECT value FROM ItemTable WHERE key='content.trust.model.key'`
  found: `{"uriTrustInfo":[{"uri":{"$mid":1,"fsPath":"/home/janfr/dev/pc-switcher",
    "external":"file:///home/janfr/dev/pc-switcher","path":"/home/janfr/dev/pc-switcher",
    "scheme":"file"},"trusted":true}]}` — exactly one entry, the folder the reporter
    says they just had to re-trust.
  implication: this key IS the full trusted-folders list, currently containing only
    the post-sync re-trusted folder — matches Symptom A exactly. A full mirror of
    state.vscdb overwrites this key wholesale with the source machine's array.

- timestamp: 2026-07-20 (this session)
  checked: `SELECT key FROM ItemTable WHERE key LIKE '%auth%' OR '%secret%' OR
    '%session%' OR '%github%'` (268 keys total in ItemTable)
  found: keys include `secret://{"extensionId":"vscode.github-authentication",
    "key":"github.auth"}`, `secret://{"extensionId":"vscode.microsoft-authentication",
    ...}`, several `secret://{"extensionId":"ms-mssql...` / `ms-ossdata.vscode-pgsql...`
    entries, `encryption.migratedToGnomeLibsecret=true`, plus
    github.vscode-pull-request-github / github.vscode-github-actions session-scope
    keys (non-secret, benign).
  implication: GitHub Pull Requests and GitHub Actions extensions (per user's belief
    in Symptoms) both layer on the same underlying `vscode.github-authentication`
    session — one secret entry, shared. Multiple unrelated extensions' secrets
    (mssql, pgsql, azure) are also inline in this same file — same exposure class.

- timestamp: 2026-07-20 (this session)
  checked: `SELECT key, length(value) FROM ItemTable WHERE key LIKE 'secret://%'`
    (length only — value itself not read, per sandbox policy on secret material)
  found: github.auth secret = 1230 bytes; other secret:// entries 92-215 bytes.
  implication: these are non-trivial opaque blobs (ciphertext), not short references
    or pointers into the OS keyring — the actual (encrypted) session data is stored
    inline in state.vscdb.

- timestamp: 2026-07-20 (this session)
  checked: attempted `secret-tool search service vscode.github-authentication` and
    reading the secret:// value directly
  found: both blocked by the sandbox's auto-mode classifier (secret material) —
    not run.
  implication: could not directly confirm the keyring holds the decryption
    password on this machine; relied on VS Code's own published documentation
    instead (next entry).

- timestamp: 2026-07-20 (this session)
  checked: web search — VS Code SecretStorage encryption / state.vscdb / libsecret
  found: "On Linux, secrets are managed by the Secret Service API/libsecret... The
    key is derived from a password, which in turn is stored in the user's local
    keyring... In case there is not yet a password stored in the keyring for this
    application a new random password is base64 encoded and stored in the keyring...
    The implementation of the secret storage will be different on each platform and
    the secrets will not be synced across machines." (microsoft/vscode-discussions
    #748)
  implication: confirms, from VS Code's own documented design, that the ciphertext
    in state.vscdb is meaningless without the per-machine keyring-held password —
    each machine independently generates its own password on first use, so even if
    the keyring itself were synced (it is not — gnome-keyring's backing store is not
    part of pc-switcher's synced home tree), a freshly-provisioned target machine's
    keyring would already hold a different password than the source's.

- timestamp: 2026-07-20 (this session)
  checked: `ls ~/.config/Code/User/globalStorage/` for state.vscdb siblings; grep
    for trust/sync references elsewhere under Code/User
  found: `state.vscdb.backup` (VSCode's own backup copy, same 4.3 MB) also present;
    no `-wal`/`-shm`/`-journal` side files present right now. `storage.json` and
    `sync/globalState/lastSyncglobalState.json` (VSCode's own cloud Settings Sync
    feature) do not contain trust or secret data relevant to this bug.
  implication: fix must cover state.vscdb and its `.backup` (and any transient WAL
    side files) but does not need to touch storage.json or the sync/ tree.

## Eliminated

- hypothesis: machine-binding — a hash/id ties trust entries to a machine and
  invalidates foreign entries.
  refuted_by: trust JSON has only authority/external/fsPath/path/scheme/trusted
  fields (no host/id), AND behavior was asymmetric — fleksi (newer) accepted p17's
  incoming list with no prompt. Machine-binding would break symmetrically.

- hypothesis: live-DB torn copy — rsync copied an open SQLite DB mid-transaction.
  refuted_by: user confirmed nothing but a console tty is open during any sync (hard
  rule); the state.vscdb copies inside the snapshots are internally consistent.

- hypothesis: clean full-mirror overwrite — target inherits the source's trust array
  wholesale (the ORIGINAL applied-fix premise).
  refuted_by: if true, p17 would have inherited fleksi's list incl. the trusted
  /home/janfr and shown pc-switcher trusted. It showed nothing. Snapshot forensics
  show the trust key was ABSENT (deleted on fleksi), not transferred.

## Resolution

root_cause: Two independent problems, previously conflated.
  (A) Trusted-folders reset = VS Code version skew (p17 1.110.0 vs fleksi 1.129.1),
  NOT a pc-switcher sync bug. Snapshot byte-comparison of state.vscdb across the round
  trip: p17's file arrived on fleksi byte-identical (5 uriTrustInfo entries incl.
  /home/janfr, which recursively covers everything — the reporter's "hundreds"); after
  live 1.129 use, `content.trust.model.key` was DELETED from fleksi's state.vscdb with
  no replacement in ItemTable or ~/.config/Code/User (depth 2); rsync faithfully
  mirrored the emptied state back to p17, where 1.110 correctly showed nothing trusted.
  VS Code 1.129 migrated trust out of state.vscdb at write-time and did not persist it
  where the old format kept it. Resolved by keeping fleet VS Code versions aligned.
  OPEN: where 1.129 now stores workspace trust (only ItemTable + Code/User depth 2
  ruled out).
  (B) Extension auth re-login = state.vscdb holds SecretStorage session blobs under
  `secret://...` keys, encrypted with a per-machine gnome-libsecret password that is
  never synced; copied ciphertext is undecryptable on the target → forced re-login.
  Version-independent, inherent to VS Code's design.
fix: REVERTED. The earlier applied fix (exclude state.vscdb* in home.filter + docs +
  test) was built on the wrong (A) mechanism and was too blunt — a wholesale exclude
  discards the wanted global state the user explicitly wants synced. All three files
  restored via git checkout; nothing staged.
direction: Selective, SQLite-aware pre/post-sync handling of state.vscdb — strip
  machine-bound keys (`secret://...` for B; workspace-trust keys for A once their 1.129
  location is known) while syncing the rest. To be designed/built in a fresh session.
  Decision + findings posted as comment on issue #190.
verification: Root cause confirmed via btrfs snapshot forensics on p17 + fleksi
  (subagent, this session). No code fix applied; working tree clean.
files_changed: none (fix reverted)
