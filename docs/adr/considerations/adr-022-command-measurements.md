# ADR-022 considerations — per-command measured behaviour

The per-command evidence behind the classifications ADR-022 requires, for the reads pc-switcher uses today. Measurements are against `ubuntu:24.04` unless otherwise noted. A tool that changes its exit-code behaviour in a future release breaks the classification silently, so this file is what a future reader consults before touching a classification.

## Exit code separates cleanly — guarded on exit code alone

- `apt-mark showmanual` / `apt-mark showhold` — exits 100 when apt cannot read the status file or parse `apt.conf.d`; exits 0 with the real answer otherwise, including exit 0 for a package name apt has never heard of.
- `apt-cache policy` — same shape as `apt-mark`.
- `snap list --all` — exits 1 when snapd is unreachable; exits 0 with the hint on stderr when snapd reports zero snaps.
- `flatpak list` / `flatpak remotes` / `flatpak mask` — exit 1 when the installation cannot be opened or its config cannot be parsed; exit 0 when the machine has none of what was asked for.
- `dpkg-query --show` — exits 1 when its admin directory is unreadable.
- `sudo cat <path>` — exits 1 on an absent or unreadable path and nothing else. Guarded on exit code because every path we `cat` was named by a digest capture root ran moments earlier, so the file provably exists and root can read it; a non-zero exit is only ever a real failure.

## Non-zero is the normal answer — unguarded, or guarded on the reply

- `sha256sum <glob>` over a glob that matches nothing — exits 1. This is what a flatpak scope with no remote keyring looks like. Unguarded.
- `sha256sum <path>` over a single path — exits 1 when the file is absent. This is how `apt_sync` learns a machine has no `/etc/apt/sources.list`; absence IS the answer the caller wants, and the reader returns `None` rather than guarding.
- `dpkg --search <paths>` — exits 1 as soon as one queried path is unowned, which is what `manual_installs_sync`'s unowned-install scan is looking for. Guarded on the answer, not on the exit code: `manual_installs_sync._DPKG_OWNERSHIP_WITNESS` adds `/usr/bin/dpkg` (which dpkg owns on every machine) to the same batch; a reply that does not claim it proves the tool did not answer, and without that witness every candidate under `/opt` and `/usr/local` would look unowned.

## Exit code is ambiguous — reshape the command

- `find <dir>` — exits 1 both when the directory is absent (a legitimate machine with no `/etc/apt/preferences.d`) and when it cannot be read. Neither reading of that exit code is acceptable: failing breaks ordinary machines; accepting reads an unreadable directory as "nothing here" and offers everything on the other machine for removal.

  Reshapes in use:
  - `_capture_dir_digests` wraps its `find` in `if sudo test -d <dir>; then ... fi`.
  - `_scan_source_file_references` walks the single always-present `/etc/apt` with `-path` selectors rather than naming a possibly-absent `/etc/apt/sources.list` as a start point.
  - `manual_installs_sync._scan_unowned_installs` drives one `find` per scan root from a loop that skips roots that are not there.

  In each case absence now answers "nothing" at exit 0, and a non-zero exit means only a real failure.

  A reshape's outer test must hold the same privilege as the wrapped query. Measured: an unprivileged `test -d` on a directory inside an unsearchable parent exits 1, collapsing the whole `if` to exit 0 with no output — a directory root would list as "this machine has no pins or keys".

- `apt-get --dry-run install` — `E: Unable to locate package` and a held dpkg lock both exit 100, and apt offers no way to separate them.

  Reshape via ARGUMENTS: the plan-time batch names only packages the target's own `apt-cache policy` gave a candidate for one command earlier in the same run (`apt_sync.origins.OriginClassifier.target_resolvable`), so `E: Unable to locate package` cannot occur. An install whose repository this run itself writes during converge is excluded from the plan-time rehearsal — apt refuses the whole batch on one such name and would take every other package's protection down with it — and gets its collateral question after `/etc/apt` has converged.

  At apply time the same command stays on the per-item side: it simulates one approved install or removal, apt's refusal is a fact about that request, and a lock met there cannot be undone by failing fast — items already converged stay converged.

## Empty is data unless impossible — the `answers=` exception

Most package-sync reads have a legitimate empty answer: a machine with no snaps, no flatpaks, no held packages, no pins, no third-party keyrings.

Only reads whose answer set is genuinely owed use `require_answer`'s `answers=` parameter, which today is three `apt-cache policy` probes:

- `apt_sync.probe.AptProbe.source_policy` — over names installed on the source. apt prints one block per known name.
- `manual_installs_sync._scan_no_candidate_apt_packages` — the same command over the same set on the target.
- `apt_sync.origins.OriginClassifier._verify` — over names this run has already established the target has a candidate for.

`apt-cache policy <unknown-name>` exits 0 and prints nothing; a broken sources configuration also exits 0 and prints nothing under the same call. Measured: corrupt lists, an unreadable cache, an unprivileged user and a bad `apt.conf` all exit 0 and answer. Output alone cannot separate "know none of these" from "died".

`477f191e` resolved this toward failing fast — misattributing environment failure to every package's provenance is the worse reading — and this is the one place emptiness is treated as silence.

Measured: `apt-cache policy $(apt-mark showmanual)` on the development machine produced 152 blocks for 152 names at exit 0. Every installed name yields a block.

## The one accepted residual

`apt-mark showmanual` with `/var/lib/dpkg/status` absent exits **0** and prints **nothing** — byte-identical to a machine with no manually-installed packages. No `answers=` guard: an empty manual set is legitimate (if strange), and a machine whose dpkg status is deleted has larger problems than a sync. Every other way apt fails to read its status exits 100 and is caught.

## The run-ending escalation for the snippet registry

An unparsable `package-snippets.yaml` raises `SyncAborted` from `packages.state`, not the `SyncAbortedByUser` subclass — nobody was asked. See `PKG-FR-REGISTRY-CONSENT` in the spec: the USER can repair it, the fix has to land before the next sync reads it, and a passing sync with `manual_installs_sync` down reads as green rather than "go fix this file". Absent or empty registry stays ordinary data.

The general shape is stated in ADR-022's *Subsystem-level failure is the ceiling* section: subsystems MAY escalate a specific read to a run-ending abort where nothing in the run can repair it, the USER can, and the fix has to land before the next sync reads it. `PKG-FR-REGISTRY-CONSENT` is the one such escalation package sync uses today.

## Shared mechanism, no shared base class

Free functions in `src/pcswitcher/jobs/packages/probes.py`, alongside `apt_policy.py`. Package sync's own decision to keep its four unreproducible jobs independent (see `PKG-FR-JOB-INDEPENDENCE` in the spec) forbids a base class that only some jobs supply inputs for. `apt_sync` keeps a thin `commands.require_apt_answer` wrapper holding apt's own evidence and the `answers=` judgement; the others call `require_answer` directly.
