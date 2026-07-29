# Package sync behaviour ledger

Evidence base for `docs/planning/package-sync-user-requirements.md`. Every user-visible behaviour the narrative states, with the source that establishes it. Read from the code on branch `gsd/phase-02-package-management-sync`; decision documents were consulted for rationale only. Where code and a decision document disagree, the entry says so and the item is carried into `02-DIVERGENCES.md`.

Line references outside "The review" were taken at `ffa06900` and the branch has since advanced to `1db1fc6b`; `review.py`, `sync_core.py` and `apt_sync.py` all grew, so those line numbers are approximate and the surrounding symbol name is the reliable anchor. "The review" was re-read at `1db1fc6b` after `9ba0d437` replaced the two-pass checkbox flow with one decision screen per group and `6541eae4` made every screen name the machines.

## Scope and opt-in

- Four jobs exist and all four ship disabled; each is enabled on its own line. `default-config.yaml:52-55`
- Package jobs must be listed before `folder_sync`; the run refuses to start otherwise, with a `ConfigError` naming the job. Validation covers `apt_sync`, `snap_sync`, `flatpak_sync` only — `manual_installs_sync` is absent from the tuple. `orchestrator.py:1046-1075`
- Reason for the ordering: installing software writes its own stock defaults, so software must land before the user's synced data. `default-config.yaml:46-51`, `orchestrator.py:1047-1050`
- No package job takes any configuration beyond its enable flag; all four declare an empty `CONFIG_SCHEMA`. `apt_sync.py:1537`, `snap_sync.py:451`, `flatpak_sync.py:1149`, `manual_installs_sync.py:177`
- Each job declares its destructive first-sync scope for the ADR-015 gate. `apt_sync.py:3919`, `snap_sync.py:755`, `flatpak_sync.py:2067`, `manual_installs_sync.py:663`

## Convergence model

- The target's own package managers do the work; no package database, store or unpacked file is copied. Established by construction — every converge path shells out to `apt-get`/`snap`/`flatpak`. `apt_sync.py:3055`, `snap_sync.py:598`, `flatpak_sync.py:1795`
- flatpak's OSTree store is read in exactly one place (`<installation>/repo/<remote>.trustedkeys.gpg`, for a digest) and never written. `flatpak_sync.py:206-213`, `flatpak_sync.py:493`
- The source is never modified by a converge command. Exception: the source's snippet registry is written when a snippet is authored (`manual_installs_sync.py:589`), the source's decision file is written for a source-held skip-always (`sync_core.py:464`), and the source's snapd `refresh.hold` is set and restored for the run (`orchestrator.py:1445`).
- Identity per ecosystem: apt = name + origin (`apt_sync.py:1-21`); snap = name alone (`snap_sync.py:20-29`); flatpak = scope + full `<application>/<arch>/<branch>` ref, origin deliberately excluded (`flatpak_sync.py:286-312`); manual = origin-kind + identifier (`manual_installs_sync.py:109-136`).
- Why flatpak origin is not identity: `flatpak install <other remote> <installed ref>` refuses with `already installed from remote <name>`, so the install-plus-removal pair could never run. `flatpak_sync.py:306-312`
- Why branch *is* flatpak identity: two branches of one id coexist in one scope, so `(scope, application)` is not a unique key. `flatpak_sync.py:298-305`
- Distribution origins are computed per machine from that machine's own distribution source files, so two machines on different Ubuntu mirrors are one vendor. `apt_sync.py:1264-1277`
- Versions float for apt and flatpak — reported with both values, never forced. `apt_sync.py:760-770`, `flatpak_sync.py:663-677`, `items.py:123`
- Snap converges the source's exact revision *and* channel. `snap_sync.py:285-302`, `snap_sync.py:607-648`
- Confinement (`classic`/`devmode`) is captured on the source and passed to install/refresh, but is not identity and produces no diff of its own. `snap_sync.py:126-134`, `snap_sync.py:220-240`
- Blocks replicate as their own items, separate from the software: apt holds `apt:hold:<name>` (`apt_sync.py:492-516`), snap holds `snap:hold:<name>` (`snap_sync.py:305-319`), flatpak masks `flatpak:mask:<scope>:<pattern>` (`flatpak_sync.py:386-410`).
- A package held on the target produces no package-level item at all; only its hold item. `apt_sync.py:706-710`

## Reads that do not answer

- A probe that did not answer raises `ProbeFailed`, once, naming the command — never read as "that machine has nothing". `probes.py:37-84`
- An empty answer at exit 0 is ordinary data. Per-command evidence for which is which is recorded in the module docstring. `probes.py:13-27`
- Documented counter-examples that must *not* be guarded: `dpkg --search` over deliberately-unowned paths, and `sha256sum` over a glob that legitimately matches nothing. `probes.py:25-27`, `manual_installs_sync.py:425-428`, `flatpak_sync.py:1226-1229`
- **`ProbeFailed` ends the whole run.** It is a plain `RuntimeError`, so it lands in the orchestrator's `except Exception` arm, which records FAILED and re-raises. Only `PackageItemFailures` and `JobSkipped` are non-fatal. `probes.py:37`, `orchestrator.py:1315-1335` — see DIV-01.

## The review

- Three outcomes per item — apply / skip / always skip — answered on **one** screen per group, one decision per row. Arrows move, one key per option sets the focused row (`y` apply, `s` skip, `n` always skip; `a` is rejected as a decision key because it conventionally means abort), Enter confirms. `decision_list.py:1-30`, `decision_list.py:FORBIDDEN_KEY`, `review.py:313-317`, `review.py:363-386`
- Row state is carried by a glyph (`●` `○` `⊘`), never by background colour alone. `review.py:337-339`, `decision_list.py:26-29`
- Rows start where confirming unread does no harm: install-direction at apply, removal-direction and the repository/remote overwrite at skip-once. `review.py:350-360`
- Nothing is echoed after the screen; the answered list stays in the scrollback and is the record. Interactive runs print no group panel — the screen lists the items itself. `decision_list.py:22-25`, `review.py` `review_items` docstring
- A two-answer screen is the same widget with one option missing, so the user sees a shorter legend rather than a different flow. `review.py:363-386`
- Report-only groups are never offered the permanence option. `review.py:136-148`
- Every screen names the two machines by hostname; "source"/"target" appear only in code, docstrings and logs. `review_items` and `TerminalUIReviewer` require both names with no default or fallback. `review.py:494`, `review.py:541`, `review.py:734`, `review.py:783`, `sync_core.py:216`
- Each answer states its effect on a named machine: skip reads `keep it on <target>` on a removal screen and `keep <target>'s version` on a conflict screen. `review.py:_skip_once_word`
- Group titles name the concrete verb and the concrete noun; "apply" is never shown. `sync_core.py:97-143`, `sync_core.py:296-306`
- Emission order: install → change → remove → report, item classes in first-seen order within an action. `sync_core.py:145-153`, `sync_core.py:285-287`
- Ctrl-C at any review screen aborts the entire sync (`SyncAbortedByUser`), never a per-item skip. `review.py:371`, `review.py:441`, `review.py:634`, `review.py:557`, `review.py:703`
- Every untrusted string is wrapped in `Text` before reaching Rich, so a bracketed package name cannot crash the render. `review.py:290-307`, `review.py:474`, `review.py:535`
- Review is per manager. There is no coordinator and no cross-manager review. `sync_core.py:8-10`, `review.py:713-715` — note `orchestrator.py:1293` carries a comment asserting the opposite; see DIV-05.
- Each job's `execute()` is plan → review → after_review → apply, with nothing mutating before the review returns. `sync_core.py:498-531`
- A missing reviewer is an assertion failure, not a silent skip of the review. `sync_core.py:518-521`

### The five prompt shapes

1. **Ordinary checkbox pair** — everything with a converge verb that may be recorded. `review.py:617-643`
2. **Report-only group** — shown, decided, never promoted. `review.py:136-148`
3. **Two-answer checkbox, nothing recorded** (`REPO_REMOVAL_REVIEW_ACTION`) — apt repository deletion, apt pin deletion, flatpak remote deletion. Unticked like any removal; never promoted; `SKIP_ALWAYS` unreachable. `review.py:118-128`, `review.py:144-148`, `apt_sync.py:2047-2066`, `flatpak_sync.py:1507-1517`
4. **Two-answer per-entry conflict screen** (`REPO_CONFLICT_REVIEW_ACTION`) — both configurations shown whole, target first, never a computed diff; overwrite or skip-once; nothing recorded. `review.py:504-558`
5. **Three-way per-entry screens** — manual collateral (install anyway / skip / abort, `review.py:451-501`) and unreproducible items (snippet / machine-specific / skip for now, `review.py:319-399`).
- Plus the **gate**, which is a question about the machine rather than an item, asked before any group is built. `review.py:654-704`

### Non-interactive and automation

- No TTY: nothing is prompted, every item comes back `SKIP_ONCE`, a warning names the count, the groups are still printed as panels. `review.py:584-595`
- A non-interactive run with a non-empty plan raises `JobSkipped` before `after_review()` and before any mutation; an empty plan stays SUCCESS. `sync_core.py:524-528`
- **`PCSWITCHER_PACKAGE_REVIEW_AUTOMATION`** bypasses all prompting from a JSON `item_id → decision` map and returns `was_interactive=True`, so permanent marks and snippets *are* written on that path. Undocumented by design. `review.py:35-39`, `review.py:114-116`, `review.py:580-582` — see DIV-02.

## Decisions and their memory

- The holder rule: `INSTALL`/`CHANGE` diffs are source-held, `REMOVE` diffs target-held. One definition serves both the write path and the read path. `sync_core.py:213-223`
- Writes go to the holder's own machine through that machine's executor — the target's file is written remotely, never locally. `sync_core.py:464-473`, `state.py:9-17`
- File location: `~/.config/pc-switcher/<manager>.decisions.yaml`, one per manager. `state.py:70-77`
- A marked item is inert on that machine **in both roles**. `state.py:5-7`
- Two filter passes: `filter_inert` on capture input, `_drop_inert_diffs` on the finished diffs for identities no input item carries (holds, repo diffs). `state.py:117-125`, `sync_core.py:225-253`
- Never recorded during a dry run, and never for a non-interactive outcome. `sync_core.py:454-455`
- `REPORT_ONLY` diffs are never recorded — no holder exists for them. `sync_core.py:461-462`, `sync_core.py:445-447`
- Absent, empty or malformed decision file all degrade to "no permanent decisions"; only malformed logs a warning. `state.py:189-209`
- Ids that may never be recorded, filtered by prefix so the rule holds even off the automation path: `apt:source:`, `apt:pin:` (`apt_sync.py:344-345`, `apt_sync.py:2831-2851`) and `flatpak:remote:` (`flatpak_sync.py:235-239`, `flatpak_sync.py:1560-1579`). `apt:config:` and `flatpak:mask:` deliberately keep the registry.
- Decision files are excluded from `folder_sync` unconditionally, not gated on any package job being enabled. `state.py:75-77`, `folder_sync.py:450`
- The generated file header contains a leaked identifier: "never synced to any peerfilter_inert." `state.py:93` — see DIV-06.

## The snippet registry

- Lives at `~/.config/pc-switcher/package-snippets.yaml` and **does** travel, unlike the decision files. `state.py:79-83`, `state.py:25-34`
- Pushed by `manual_installs_sync` itself via its own `send_file`, not by `config_sync` or `folder_sync` — no job's correctness depends on another job running. `manual_installs_sync.py:252-291`, `manual_installs_sync.py:205-207`
- A snippet is stored and replayed verbatim, never parsed. `state.py:409-436`
- Replayed as `bash -c <quoted body>` with `login_shell=False`, as the target user, no outer sudo, no stdin. `state.py:433-436`
- An empty snippet is not accepted; the three-way choice re-prompts. `review.py:386-399`
- The whole-file push is gated: purely additive proceeds silently; one that would lose or change a target-only entry shows exactly which entries and asks; declining or being unable to ask aborts the whole sync. `manual_installs_sync.py:293-337`
- A snippet authored during this run's review is persisted, pushed, then promoted `REPORT_ONLY → INSTALL` decided `APPLY`, so it converges the same run. `manual_installs_sync.py:193-250`
- Reproducibility is judged by what the **source** holds; a snippet only on the target leaves the item unresolved. `manual_installs_sync.py:485-487`

## apt

### Installing

- Classification ladder: no source origin → fall back to the presence question; source origin already served by the target's candidate → ordinary install; origin declared by a writable source file → install with that repository derived; otherwise `REPO_UNAVAILABLE`/report-only. `apt_sync.py:582-604`
- Vendor disclosure: an install whose origin is not the distribution carries `from <vendor>` in its review detail. `apt_sync.py:433-445`, `apt_sync.py:734`
- `REPO_UNAVAILABLE` detail names both the origin and the cause. `apt_sync.py:448-457`, `apt_sync.py:618-627`
- Post-convergence verification: one batched `apt-cache policy` after this run's single `apt-get update` and before the first install; an install apt would satisfy from none of the source's origins fails as its own item naming both. `apt_sync.py:2933-2999`, `apt_sync.py:3029-3031`
- Packages whose source origins are all distribution origins are exempt from that check. `apt_sync.py:2977-2982`
- Bare-`.deb` packages are dropped at capture and never produce an apt item in any configuration. `apt_sync.py:1674-1703`, `apt_sync.py:37-48`
- Enabling `apt_sync` without `manual_installs_sync` leaves them replicated by nobody — stated as a knowing consequence. `apt_sync.py:44-48`

### Removing and reporting

- A package on the target the source lacks is offered for removal (`apt-get remove`, never `--purge`). `apt_sync.py:737-747`, `apt_sync.py:3087`
- Cross-vendor divergence is checked **before** the version comparison and outranks it. `apt_sync.py:748-759`, `apt_sync.py:644-656`
- A vendor mismatch is suppressed when either side has no vendor origin — that is what stops mirror differences reporting every package. `apt_sync.py:649-656`
- A pin says nothing about the packages it names; no per-package "pinned" echo exists. `apt_sync.py:170-176`, `items.py:76-81`

### Collateral

- Auto-installed collateral proceeds silently; manual-on-target collateral becomes a three-way item. `apt_sync.py:2578-2607`
- The protected set is the **target's** `apt-mark showmanual` set alone; the source's manual set is deliberately not unioned in. `apt_sync.py:2434-2450`
- Machine-specific marks are not consulted for collateral protection. `apt_sync.py:2447-2448`
- Two batched rehearsals per run (whole install set, whole removal set), never one per package. `apt_sync.py:2495-2526`
- Attribution: if a batch finds manual collateral, each candidate is re-rehearsed alone so `skip` cancels only the causes. Collateral no single candidate reproduces is attributed to the whole batch and the question says "the selected packages". `apt_sync.py:2528-2576`, `apt_sync.py:1468-1474`
- A skip never overwrites a `SKIP_ALWAYS`; only an `APPLY` is overridden. `apt_sync.py:2634-2676`
- Installs whose repository this run must itself provision are excluded from plan-time rehearsal entirely and covered by a per-item rehearsal after `/etc/apt` converges — told afterwards, not asked beforehand. `apt_sync.py:2467-2493`, `apt_sync.py:3035-3053`
- The apply-time guard is a drift check, not the decision point. `apt_sync.py:3002-3010`

### Repositories, keys, pins, config

- Adding or changing a repository is never a question; it travels because an approved package comes from it. A repository feeding no package this run syncs does not travel. `apt_sync.py:2387-2411`, `apt_sync.py:2740-2763`
- Repository conflict: a differing file that feeds machine-specific target packages gets the two-answer screen showing both bodies whole. Declining seeds it as a failed derived write, so every package that needed it fails naming the decision. `apt_sync.py:2365-2385`, `apt_sync.py:2714-2724`
- Repository deletion names the machine-specific packages it would strand — disclosure, not refusal. `apt_sync.py:960-973`, `apt_sync.py:2293-2363`
- Distribution files (`ubuntu.sources`, the two `ubuntu-esm-*`, `/etc/apt/sources.list`) are written when missing, overwritten when different, never removed and never offered for removal. `apt_sync.py:266-268`, `apt_sync.py:2409-2411`, `apt_sync.py:2726-2738`
- Files apt does not read (`.save`, `.curtin.orig`) are invisible; only `.list`/`.sources` are captured. `apt_sync.py:255-258`
- Every `preferences.d` file the source has is written when missing or different, always, without review. Only deletion is reviewed. `apt_sync.py:1316-1342`, `apt_sync.py:2707-2711`
- Keys are not items in any direction — no `ItemClass`, no id, no diff, no decision. `apt_sync.py:121-124`
- Three key directories: `/etc/apt/keyrings`, `/etc/apt/trusted.gpg.d`, `/usr/share/keyrings`. `apt_sync.py:285-293`
- A key the target lacks is copied whatever owns it on the source; a differing key the target's own dpkg owns is left alone; a matching key is untouched. `apt_sync.py:3648-3717`
- Keys travel byte-for-byte, never fetched from a vendor. `apt_sync.py:161`, `apt_sync.py:3708-3712`
- Rotated keys are caught by content, not presence — a vendor's new key changes no source file. `apt_sync.py:3664-3667`
- Collection is `/etc/apt/keyrings` only, only after an approved repository removal, counted against a fresh scan of the target's real post-write source files. `apt_sync.py:3790-3847`
- `apt.conf.d` is the one class reviewed in all three directions with the full decision including the permanent mark. `apt_sync.py:1345-1360`, `apt_sync.py:2842-2844`

### Ubuntu Pro / ESM

- Asked before any review group is built and before the package diff, because one answer ends the job. `apt_sync.py:1969-1974`, `apt_sync.py:1950-1956`
- Exactly two answers; "I have attached" **re-probes** rather than believing the answer, unbounded. `apt_sync.py:2207-2228`
- Skip raises `JobSkipped` for the whole apt job, leaving `/etc/apt` as found; other jobs continue. `apt_sync.py:2220-2225`, `orchestrator.py:1271-1291`
- No TTY takes the skip and says why; a dry run warns instead of asking. `apt_sync.py:2182-2189`, `apt_sync.py:2214-2219`
- Only the parsed `attached` boolean leaves the probe; every failure mode answers False. `apt_sync.py:2141-2158`
- The remedy is on screen as copy-paste commands. `apt_sync.py:2201-2203`

### Applying

- Converge order by rank: keys → pins/config → sources → metadata refresh → packages → holds last. `apt_sync.py:310-318`, `apt_sync.py:2813-2819`
- The `/etc/apt` group is one transaction: back up everything, write, one `apt-get update`, roll the whole group back if it fails. `apt_sync.py:3146-3279`
- A rollback marks every group item failed — including ones whose own write succeeded — and every derived write with them. `apt_sync.py:3272-3279`, `apt_sync.py:3364-3383`
- A failed rollback keeps the backup directory and names the path; the user finishes by hand. `apt_sync.py:3323-3333`
- Exactly one `apt-get update` per run across both refresh paths. `apt_sync.py:1645-1654`, `apt_sync.py:2899-2931`
- Deletion order inside the group is the reverse of the write order — repository before the pin that prefers it. `apt_sync.py:355-361`, `apt_sync.py:3424-3433`
- A derived write has no item; its failure is charged to every approved package that needed it, naming the file. `apt_sync.py:3497-3510`, `apt_sync.py:3446-3466`
- Every derived write is logged as it lands and previewed under dry-run. `apt_sync.py:2853-2866`, `apt_sync.py:3466`
- Files reach `/etc/apt` by SFTP into the user's cache then `sudo install` — one atomic promotion, staging removed in a `finally`. `apt_sync.py:3531-3581`

## snap

- One store, no origin model, no repository or key screen. `snap_sync.py:20-29`
- Never `snap refresh --hold` with no name — that form sets an indefinite global hold on every snap. `snap_sync.py:4-14`, `snap_sync.py:670-693`
- Install/refresh at the exact revision, then always switch channel. `snap_sync.py:581-605`
- Removal is `snap remove`, never `--purge`, so snapd's pre-removal snapshot survives. `snap_sync.py:659-668`
- Sideloaded snaps (revision `x<N>`) are warned about once and dropped from the diff; the target's matching entry is withheld too, so "cannot reproduce" does not become "propose deleting it there". A sideloaded snap the source does *not* have stays an ordinary removal candidate. `snap_sync.py:243-260`, `snap_sync.py:518-533`
- Auto-refresh is paused on both machines for the run via a **timed** system-wide `refresh.hold`; the prior value per host is captured and restored; the hold self-expires if the run dies. `orchestrator.py:76-99`, `orchestrator.py:1445-1467`
- Where the prior value cannot be read on a host, that host's policy is left untouched rather than cleared. `orchestrator.py:1497-1503`
- Hold intent is source-authoritative: a hold recorded for a snap the source no longer has produces no item. `snap_sync.py:322-342`
- `folder_sync` mirrors the current revision's data dir (resolved through the `current` symlink) plus `common`, and excludes retained older revision dirs. A missing or dangling `current` excludes all of that app's revision dirs. `snap_sync.py:401-435`
- Passwordless sudo required on **both** machines — the target for install/refresh/remove, the source because the refresh pause writes there too, and because snapd admin-gates even *reading* snap config. `snap_sync.py:695-751`

## flatpak

- A remote is derived from the refs approved from it and is never ticked in the add or change direction. `flatpak_sync.py:22-38`, `flatpak_sync.py:1520-1557`
- No distribution remote exists — a fresh flatpak install configures zero remotes, so even Flathub travels only because something needs it. `flatpak_sync.py:29-33`
- Derivation covers the approved ref's own origin **and** the origin of the runtime it is built against, looked up in either scope but always derived in the app's scope. `flatpak_sync.py:837-897`
- Remotes are provisioned before the converge loop issues its first install. `flatpak_sync.py:1581-1610`
- A remote carries its trust — verification setting and its own ostree keyring — not just name and URL. Without the key a replicated remote is configured but unusable. `flatpak_sync.py:72-82`, `flatpak_sync.py:333-372`
- `--no-gpg-verify` is emitted if and only if the source remote is itself unverified; a verified remote is never silently downgraded. `flatpak_sync.py:987-1007`
- Key bytes travel byte-for-byte from the source, staged in the target's cache and discarded in a `finally`. `flatpak_sync.py:1843-1896`
- Repointing is silent except where it would move the origin of a **machine-specific** target ref, which raises the two-answer conflict screen naming the refs. A key-only difference never raises it. `flatpak_sync.py:1400-1465`, `flatpak_sync.py:250-259`
- Remote deletion is reviewed and names the target refs it would orphan in that scope. `flatpak_sync.py:900-918`, `flatpak_sync.py:1021-1033`
- Deleting a remote takes its per-remote keyring with it. `flatpak_sync.py:1744-1747`
- Origin is verified twice: before the install the target's own remote list must carry the source remote's URL and verification setting; after it, the landed origin is re-resolved to a URL. Either failure fails that ref alone naming both URLs. `flatpak_sync.py:1922-1997`
- A remote-add's exit code is not evidence — `remote-add --if-not-exists <name> <other url>` exits 0 and changes nothing. `flatpak_sync.py:1908-1920`
- A ref whose origin remote exists neither on the target nor among this run's additions is refused as its own item. `flatpak_sync.py:1948-1950`
- Filters do not travel: the remote is provisioned unfiltered and the run warns once per remote, in dry runs too, with the command to re-apply it. `flatpak_sync.py:1612-1639`
- A third named installation (neither user nor system) is skipped rather than guessed at. `flatpak_sync.py:552-575`
- Masks replicate per scope as pure patterns, never filtered to installed refs; an edit reads as remove-old + add-new. `flatpak_sync.py:1089-1119`
- Refs converge before masks so a mask cannot suppress an auto-pulled dependency. `flatpak_sync.py:1386-1394`
- `sudo` is used if and only if the item's scope is `system`; a user-scope run needs no root. `flatpak_sync.py:466-473`
- Target sudo is validated only when a system-scope item actually exists on either machine. `flatpak_sync.py:1999-2016`, `flatpak_sync.py:2051-2061`
- `folder_sync` stops mirroring `~/.local/share/flatpak` but keeps `~/.var/app`, which is user data. `flatpak_sync.py:1122-1131`

## Software no manager can reproduce

- Two detectors, both source-side: apt packages whose installed version comes from no configured repository, and paths directly under `/usr/local`, `/opt`, `/usr/local/bin`, `/usr/local/lib` that no dpkg package owns. `manual_installs_sync.py:4-12`, `manual_installs_sync.py:86-92`
- The scan names a finding; it never walks a tree. `manual_installs_sync.py:88-91`
- Three resolutions, all valid, no fourth undecided state; skip-once is a real resolution. `manual_installs_sync.py:21-27`, `review.py:332-345`
- No target-side manifest exists, so nothing is ever proposed for removal. `manual_installs_sync.py:465-471`
- Sideloaded snaps and flatpak local-bundle/dead-remote refs are **not** handled — `snap_sync` excludes sideloads and hands them to nobody; the flatpak equivalent is unverified. Issue #221, deferred. Contradicts `PKG-NG-SIDELOAD`'s "cannot be reproduced" — see DIV-04.
- This job requires no sudo anywhere; a snippet's privilege needs are its own author's problem. `manual_installs_sync.py:630-659`

## Outcomes

- SUCCESS when the job did what its review approved, including an empty review. `sync_core.py:379-381`
- SKIPPED for a non-interactive run with a non-empty plan (`sync_core.py:524-528`) and for the ESM gate (`apt_sync.py:2215-2225`); the run continues and the exit code is unaffected (`orchestrator.py:1271-1291`).
- FAILED when at least one approved item could not be applied. Every approved item is attempted, failures collected and raised once. `sync_core.py:330-412`
- One failed package job does not stop the others — `PackageItemFailures` is the only exception arm besides `JobSkipped` that does not re-raise. `orchestrator.py:1292-1314`
- Dry run: the review **is** still shown and answered; nothing is recorded, no snippet written, no registry pushed, no converge command issued; each intended action is logged with a `[dry-run]` prefix carrying the item's detail, and derived writes are previewed too. `sync_core.py:375-395`, `sync_core.py:454`, `manual_installs_sync.py:269-270`, `apt_sync.py:2853-2866`, `flatpak_sync.py:1599-1608`
- `--confirm-each-command` gates every write; every mutating call carries a `mutates=` phrase, reads carry none. Verified present on every write path read for this ledger.

## Preconditions per manager

| | source sudo | target sudo | other |
| - | - | - | - |
| apt | required — `sudo find`/`sha256sum` over `/etc/apt`; without it the capture degrades to empty digests silently | required | dpkg frontend lock must be free on the target (`apt_sync.py:3903-3913`) |
| snap | required — the refresh pause writes on both hosts, and reading snap config is admin-gated | required | `snap version` on both |
| flatpak | none | only when a system-scope item is in play | `flatpak --version` on both; absence is a reported validation error, not a crash |
| manual | none | none | `apt-cache` and `dpkg` on the source |

`apt_sync.py:3859-3915`, `snap_sync.py:695-751`, `flatpak_sync.py:2018-2063`, `manual_installs_sync.py:630-659`. None of this is stated in `package-sync-conformance-criteria.md` — see DIV-03.
