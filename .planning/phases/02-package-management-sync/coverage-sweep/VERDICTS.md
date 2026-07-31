# Adversarial verification verdicts

Read against `src/` on branch `gsd/phase-02-package-management-sync`. Symbols, not line numbers.

### A62/A63 — CONFIRMED
`origins.is_origin_mismatch` returns True only when `OriginPlan.vendor_source_origins` and `vendor_target_origins` are BOTH non-empty, and `OriginClassifier.classify` builds each side as that machine's origins minus that machine's own distribution origins — so a target serving the package from the Ubuntu archive has an empty vendor set and the comparison short-circuits to False. `diff_apt_packages` therefore skips the `ORIGIN_MISMATCH` branch and falls through to `source_item.version != target_item.version`, emitting `VERSION_MISMATCH`/`REPORT_ONLY` when the versions differ and no diff at all when they match.

Consequence: the exact case the user requirements use as their worked example — `gh` from GitHub's repository on one machine and `gh` from Ubuntu's archive on the other — is never reported as an origin divergence, and where the two builds happen to carry the same version string the user is shown nothing at all. `PKG-FR-DISTRO-ORIGIN` makes the distribution ONE origin computed per machine (the mirror exemption); the code instead treats it as no origin, which is a stronger suppression than that article buys.

### B27 — CONFIRMED
`AptSyncJob._plan_packages` runs `filter_inert` over the source manifest only, while `AptProbe.collect_hold_sets` returns both `apt-mark showhold` sets raw; `diff_apt_holds` then emits an `AptHoldItem` INSTALL diff keyed `apt:hold:<name>`, and `_drop_inert_diffs` looks that id up in the source decision file, where the recorded mark is `apt:package:<name>` (`APT_PACKAGE_ID_PREFIX` vs `APT_HOLD_ID_PREFIX`) — so the hold item survives. At converge, `PackageConverger._hold_refusal` searches `diffs` for an `ItemClass.APT_PACKAGE` diff with id `apt:package:<name>`, finds none because the source item was filtered out and the target does not have the package, and returns `None`, letting `hold()` run `apt-mark hold`.

Consequence: the target ends up holding a package it does not have — the exact state `hold()`'s own docstring says the guard exists to prevent (measured: `apt-mark hold` exits 0 and records the selection for a merely-uninstalled package), which then blocks every later attempt to install that package on the target.

### B28 — CONFIRMED
`AptSyncJob._plan_packages` builds `source_versions = {item.name: item.version for item in source_items if item.version}` and then admits a name into `self._held_versions` only `if name in source_hold_names and name in source_versions`; `PackageConverger._install` reads `self._held_versions.get(name)` and, on `None`, calls `install_args([name])` with no `=<version>` suffix, so the target installs whatever its repositories offer and `_held_version_refusal` is never reached.

Consequence: a held package captured with an empty version is installed at the target's own candidate version and then frozen there, which is what `PKG-FR-APT-HOLD-VERSION`'s "MUST NOT fall back to another version" forbids.

Caveat the claimant did not state, and which bounds the severity: the empty version is a defensive default (`AptProbe._resolve_versions` returns `AptPackageItem(name=name, version=versions.get(name, ""))`), and the same method's measured note says `dpkg-query` exits non-zero when any queried name is unknown, which `require_answer` turns into a job failure before the item is built. The code path is real and unguarded; reachability of the empty version is not established by reading `src/`.

### G72 — CONFIRMED
`ManualInstallsSyncJob._guard_registry_overwrite` computes `lost` from item-id absence and `changed` from `source_snippets[item_id].body != snippet.body` alone, returning early ("purely additive") when both are empty. `Snippet` (`packages/state.py`) carries `label`, `authored_at` and `authored_on` besides `body`, and `_serialize_snippets` writes all four, so `_push_snippet_registry`'s whole-file `send_file` replaces them without the confirmer being consulted.

Consequence: a target entry whose body matches but whose label or authoring record differs is overwritten silently, where `PKG-FR-REGISTRY-CONSENT` gates any transfer that would "lose or change an entry the target holds". The overwritten fields are metadata — the replayed body stays byte-identical — so nothing the target can execute changes; what is lost is the record of where and when that snippet was authored.

### H167 — CONFIRMED
`review_items` returns `ReviewOutcome(decisions=_decisions_from_automation(groups, automation_raw), was_interactive=True)` as its first act when `PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` is set, and `_decisions_from_automation` maps item ids to `Decision` values only. `ReviewOutcome.snippets` defaults to an empty dict (`field(default_factory=dict)`), and only the interactive `UNREPRODUCIBLE_REVIEW_ACTION` per-entry flow ever populates it, so `ManualInstallsSyncJob.accept_review` sees no authored snippets on the automation path.

Consequence: `PKG-NG-AUTOMATION-ENV`'s "a permanent one writes a machine-specific mark **or an install snippet**" overstates the hatch — only the mark half is reachable, so snippet authoring cannot be exercised through the environment variable at all. `review.py`'s own module docstring states the narrower, correct version ("a permanent one writes a machine-specific mark"), so the divergence is in the article, not between two pieces of code.

### K81 — CONFIRMED
`FolderSyncJob._build_rsync_cmd` emits the snap filters only inside `if self._package_job_enabled("snap_sync")`, and `_package_job_enabled` reads `self.context.enabled_sync_jobs`, returning `False` both when `snap_sync` is disabled and when the sibling map is `None`. The three unconditional groups (`_runtime_exclude_filters`, `_vscode_state_exclude_filters`, `_decision_file_exclude_filters`) name nothing under `~/snap`, so with `snap_sync` off nothing in the global-first chain excludes any `~/snap/<app>/<revision>` directory and the whole tree is mirrored subject only to user filters.

Consequence: on a `snap_sync`-off, `folder_sync`-on run the target receives data directories for every revision the source retains — including the source's current revision, which the target's snapd need never have installed — which `PKG-FR-SNAP-DATA-BOUNDARY` forbids outright. The module comment records this as a deliberate trade ("excluding them anyway would strand that data unmirrored rather than protect it"), so the finding is a requirements conflict with a documented choice, not an oversight.

### K82 — CONFIRMED
`snap_sync_exclude_paths()` enumerates `Path.home() / "snap"` on the machine running the tool (the source) and excludes every revision dir EXCEPT the one each app's own `current` symlink resolves to; it takes no decisions, no `ReviewOutcome` and no converge results as input, so nothing about what `snap_sync` was allowed to do or managed to do can narrow it. `FolderSyncJob._snap_sync_exclude_filters` translates that list verbatim into rsync filters.

Consequence: where an app's revision convergence was declined at the review or failed at converge, the target stays on its own revision while `folder_sync` still mirrors the SOURCE's current-revision data dir into `~/snap/<app>/<source-revision>` on the target — a data directory for a revision the target's snapd never installed, which `PKG-FR-SNAP-DATA-BOUNDARY` forbids. The function's docstring states the assumption that makes the exclusion safe ("snap_sync converges the target onto the source's revision before folder_sync runs"), and that assumption is exactly what a declined or failed item breaks.

One detail of the claim is wrong and does not change the finding: the exclude set is NOT computed "before the review". `snap_sync_exclude_paths()` runs inside `FolderSyncJob._build_rsync_cmd`, called from `FolderSyncJob.execute()`, which `Orchestrator._check_package_jobs_precede_folder_sync` guarantees runs after `snap_sync` has finished reviewing and converging. The defect is that the set is computed from the source's filesystem alone, not that it is computed early.

### K23 — CONFIRMED (gating exists as described)
`FolderSyncJob._build_rsync_cmd` wraps `_snap_sync_exclude_filters` in `if self._package_job_enabled("snap_sync")` and `_flatpak_sync_exclude_filters` in `if self._package_job_enabled("flatpak_sync")`; `_package_job_enabled` reads `self.context.enabled_sync_jobs`, the orchestrator-populated `sync_jobs` enablement map. The other three global-first groups are emitted unconditionally, and the module comment states the asymmetry and its reason explicitly.

Consequence: the rsync command `folder_sync` issues — and therefore which files reach the target — differs according to whether `snap_sync` and `flatpak_sync` are enabled.

Whether that VIOLATES `PKG-FR-JOB-INDEPENDENCE` ("no job's behaviour may depend on whether another is enabled") is a requirements question, not a code question, and is not ruled on here. The code carries a stated rationale for the dependency — an unmanaged path hidden from the mirror is invisible to every sync mechanism at once — which is an argument about what the article should say, and belongs to whoever owns the article.
