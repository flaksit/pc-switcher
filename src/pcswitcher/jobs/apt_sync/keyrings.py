"""Signing keys: the two file operations that bracket the repository unit (D-12).

A key is NOT an item. It has no `ItemClass`, no `item_id`, no diff, no review entry and no
decision-file identity: the user thinks in repositories and packages, and a key is only how a
repository is made to work. So everything here is driven by the decisions the user made about
SOURCES, and nothing here ever asks a question, builds an `ItemDiff`, or writes a decision
file.

Provisioning runs before any source file is written and collection after every source write
and deletion — the order is apt's, and `Keyrings.gap` is what stops a repository being written
ahead of its key. Both are ownership-aware in one direction only: a key the target LACKS is
copied whatever owns it, because a vendor `.deb` that ships both a repository entry and the
keyring trusting it cannot be installed until that keyring is present.

Ownership means the target's own DISTRIBUTION packaging, never any package
(`PKG-FR-KEY-REFRESH`, `AptProbe.capture_distribution_owned_keys`). A vendor ships its
keyring in a `.deb` of its own and rotates it there; leaving that key alone because
something owned it is how a rotation on the source never reaches the target and the
target's apt starts failing that repository's signature check.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from pcswitcher.jobs.apt_sync.files import TargetFiles, staged_name_for
from pcswitcher.jobs.apt_sync.items import (
    APT_KEYRINGS_DIR,
    APT_SOURCE_ID_PREFIX,
    APT_TRUSTED_GPG_DIR,
    METADATA_REFRESH_ITEM_ID,
)
from pcswitcher.jobs.apt_sync.probe import AptProbe, KeyDigests, SourceFileRefs, scan_source_file_references
from pcswitcher.jobs.apt_sync.reporting import Log
from pcswitcher.jobs.packages.items import DiffAction, ItemClass, ItemDiff, Machines
from pcswitcher.jobs.packages.review import Decision
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed
from pcswitcher.models import Host, LogLevel


def dangling_ref(keyring_refs: Sequence[str], source_key_filenames: frozenset[str]) -> str | None:
    """The first `keyring_refs` entry whose basename is absent from
    `source_key_filenames`, or `None` if every reference resolves to a real file on the
    source. A source file with no `Signed-By:`/`signed-by=` at all (`keyring_refs` is
    empty) has nothing to validate — it is not itself a dangling reference.

    `source_key_filenames` spans all three key directories (`KEY_DIRS`), so a reference is
    dangling only when the source machine really has no such key — not merely when it keeps
    it somewhere this job did not think to look.
    """
    for ref in keyring_refs:
        if Path(ref).name not in source_key_filenames:
            return ref
    return None


class Keyrings:
    """Every key decision and both key operations, over the two machines' captured key
    digests.

    The digest maps ARE the whole key model (package docstring): provisioning compares them
    to decide what to copy, the readiness check consults them instead of re-probing the
    target, and collection uses the per-directory pair to tell a key the source machine still
    has from one it dropped.
    """

    def __init__(
        self,
        *,
        source_keys: KeyDigests,
        target_keys: KeyDigests,
        source_refs: SourceFileRefs,
        target_refs: SourceFileRefs,
        distribution_owned: frozenset[str],
        probe: AptProbe,
        files: TargetFiles,
        log: Log,
        machines: Machines,
    ) -> None:
        self._machines = machines
        self._source_keys = source_keys
        self._target_keys = target_keys
        self._source_refs = source_refs
        self._target_refs = target_refs
        # Absolute paths of every key file on the TARGET that the target's own DISTRIBUTION
        # packaging owns, from the plan-time probe. Consulted in one direction only: it
        # never blocks copying a key the target LACKS, it only stops a differing key the
        # target's distribution manages from being overwritten.
        self._distribution_owned = distribution_owned
        self._probe = probe
        self._files = files
        self._log = log
        # Absolute target paths provisioning successfully wrote this run. A source file may
        # only be written once every keyring it references is either already byte-identical
        # on the target or in here.
        self._provisioned: set[str] = set()

    def manages(self, ref: str) -> bool:
        """Whether the target already has the key `ref` names AND its own DISTRIBUTION
        packaging owns that path — the one case where a differing keyring is deliberately
        left alone (`PKG-FR-KEY-REFRESH`).

        Not a general ownership gate (package docstring), twice over: a key the target
        LACKS is copied whatever owns it, because a vendor `.deb` that ships both a
        repository entry and the keyring trusting it cannot be installed until that keyring
        is present; and a key some VENDOR's package owns is refreshed like any other, since
        that is exactly how a vendor rotates one.
        """
        return self._target_keys.digest_of(ref) is not None and ref in self._distribution_owned

    def writes(self, refs: frozenset[str]) -> list[tuple[str, str]]:
        """`(local path, target destination)` for every keyring this run must copy, given
        the set of references that will be live on the target.

        Content-based, not presence-based: a key already on the target whose bytes differ
        from the source machine's is copied too. That is what keeps a ROTATED key correct —
        the vendor's new key changes no source FILE, so nothing else in the run would ever
        notice, and the target's apt would fail that repository's signature check.

        Two populations, one rule ("the target's copy matches the source machine's"):

        - Every `/etc/apt/trusted.gpg.d` key the source has. Nothing references these —
          they are ambient trust — so a reference count cannot select among them and their
          own content is the only signal there is.
        - The `/etc/apt/keyrings` and `/usr/share/keyrings` files that `refs` actually
          names. Neither directory is mirrored wholesale: a keyring no source on the target
          points at is litter, and `/usr/share/keyrings` is mostly the distro's own.

        Overwriting is ownership-aware, copying is not (package docstring): a key the target
        already has with different bytes is skipped when the target's own distribution
        packaging owns that path,
        while a key the target LACKS is always copied — including a package-owned one,
        which is the only way a repository whose keyring ships inside a package it hosts can
        ever be bootstrapped.

        A destination is emitted at most once, so one rotated key serving three
        repositories is still exactly one write.
        """
        writes: dict[str, str] = {}
        for name, digest in self._source_keys.in_dir(APT_TRUSTED_GPG_DIR).items():
            dest = f"{APT_TRUSTED_GPG_DIR}/{name}"
            if self._target_keys.in_dir(APT_TRUSTED_GPG_DIR).get(name) == digest or self.manages(dest):
                continue
            writes[dest] = dest
        writes.update(self.referenced_writes(refs))
        return [(writes[dest], dest) for dest in sorted(writes)]

    def referenced_writes(self, refs: Iterable[str]) -> dict[str, str]:
        """`{destination: local path}` for the subset of `refs` whose key this run must
        copy — the reference-driven half of `writes`, without the ambient
        `/etc/apt/trusted.gpg.d` population.

        Split out because it is also what a single source file's review detail may name: the
        keys that travel BECAUSE of that file. The global keys travel regardless of any
        source file, so attributing them to one would name the same key on every repository
        in the review.
        """
        writes: dict[str, str] = {}
        for ref in refs:
            local = self._source_keys.path_of(ref)
            if local is None:
                # The source machine has no such key. That is D-12's dangling reference,
                # already reported on the REPOSITORY item; inventing a key here is exactly
                # what "never re-fetched from a vendor" forbids.
                continue
            if self._source_keys.digest_of(ref) == self._target_keys.digest_of(ref) or self.manages(ref):
                continue
            writes[ref] = local
        return writes

    def surviving_refs(
        self, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision], written: frozenset[str]
    ) -> frozenset[str]:
        """Every keyring reference that will be live on the target once this run's derived
        writes and approved removals have been applied. `written` is the basenames of the
        source files this run writes.

        Three populations, and getting any of them wrong provisions or deletes the wrong
        key: source files this run WRITES — the derived set, since ADR-020 D-37 leaves no
        other way for one to travel — contribute the SOURCE machine's references (a
        repository this run overwrites may point somewhere new); source files this run
        REMOVES contribute nothing (their keyring is about to be collected, not refreshed);
        every other source file on the target — untouched, recorded machine-specific, or
        never synced at all — contributes the references it currently carries.
        """
        removed = {
            diff.item_id.removeprefix(APT_SOURCE_ID_PREFIX)
            for diff in diffs
            if diff.item_class == ItemClass.APT_SOURCE
            and diff.item_id != METADATA_REFRESH_ITEM_ID
            and diff.action == DiffAction.REMOVE
            and decisions.get(diff.item_id) == Decision.APPLY
        }

        refs: set[str] = set()
        for filename, (target_refs, _uris) in self._target_refs.by_filename.items():
            if filename not in removed and filename not in written:
                refs.update(target_refs)
        for filename in written:
            refs.update(self._source_refs.refs_of(filename))
        return frozenset(refs)

    def pending_work(self) -> bool:
        """Whether ANY keyring could need writing this run, judged before the derived write
        set is consulted — the trigger that lets the repository unit run for a rotated key
        whose source file is byte-identical and therefore derives no write at all.

        Deliberately a superset: it counts the references of every source file on BOTH
        machines, because which of them survive is decided later. A false positive costs
        nothing — the unit recomputes the exact set from `surviving_refs` and returns early
        when it turns out to be empty.
        """
        return bool(self.writes(self._target_refs.all_refs() | self._source_refs.all_refs()))

    def unreferenced(self, surviving_refs: frozenset[str]) -> list[str]:
        """The `/etc/apt/keyrings` files `remove_unused` would collect, given the references
        that survive this run — the dry-run counterpart of that pass
        (`PKG-FR-DERIVED-VISIBLE`).

        Predicted from `surviving_refs` rather than from the fresh target scan the real pass
        takes, because a dry run has not made the source removals that scan would observe.
        The two agree whenever the run does what it planned, which is what a preview claims.
        """
        candidates = frozenset(self._target_keys.in_dir(APT_KEYRINGS_DIR)) - frozenset(
            self._source_keys.in_dir(APT_KEYRINGS_DIR)
        )
        referenced = {Path(ref).name for ref in surviving_refs}
        return [f"{APT_KEYRINGS_DIR}/{filename}" for filename in sorted(candidates - referenced)]

    def gap(self, dest: str) -> str | None:
        """Why writing this derived source file would leave apt with a repository it cannot
        verify, or `None` when every keyring it names is in place (D-12).

        A repository written without its key is a repository apt refuses on every
        subsequent operation, so writing it anyway is strictly worse than leaving the target
        alone. The refusal lands on the destination and, through the derived-write
        attribution, on the packages that needed it — the things the user actually decided
        about (D-39).

        A key the target has and its own distribution packaging owns counts as ready even
        though this run
        deliberately did not overwrite it: the target's package manages that file, so the
        repository is trusted there. Pin and apt-config destinations name no keys and always
        return `None`.
        """
        for ref in self._source_refs.refs_of(Path(dest).name):
            if ref in self._provisioned:
                continue
            source_digest = self._source_keys.digest_of(ref)
            if source_digest is not None and source_digest == self._target_keys.digest_of(ref):
                continue
            if self.manages(ref):
                continue
            return (
                f"it references keyring {ref!r}, which is neither already present on {self._machines.target} "
                f"with {self._machines.source}'s own bytes nor among the keys this run provisioned "
                "(D-12/T-02-16)"
            )
        return None

    async def provision(self, writes: Sequence[tuple[str, str]], staging_dir: str) -> None:
        """Copy each planned keyring onto the target, recording the destinations that
        landed so `gap` can let their repositories be written.

        A failure here fails no ITEM — there is no key item to fail. It is logged and the
        destination is simply left out of the provisioned set, which makes every source
        file referencing that keyring refuse its own write with a message naming the key.
        That is the D-12 outcome either way, reported against the thing the user reviewed.

        Each key that lands gets its own FULL line, the same one a derived `/etc/apt` file
        gets (`PKG-FR-DERIVED-VISIBLE`): a key is never a review entry, so the log is the only
        record that one reached the target at all.
        """
        for local, dest in writes:
            try:
                await self._files.stage_and_promote(local, dest, staging_dir, staged_name_for(dest))
            except ConvergeItemFailed as exc:
                self._log(
                    Host.TARGET, LogLevel.ERROR, f"failed to provision signing key {dest}: {exc}", stderr=str(exc)
                )
                continue
            self._provisioned.add(dest)
            self._log(Host.TARGET, LogLevel.FULL, f"wrote signing key {dest} from {self._machines.source}")

    async def remove_unused(self, backup_dir: str, existed_before: dict[str, bool]) -> None:
        """Delete every `/etc/apt/keyrings` file on the target that no surviving source
        references — the garbage-collection half of transparent key handling.

        Called only after every source write and deletion in the unit, and only when this
        run removed at least one source file. The reference count comes from a FRESH scan of
        the target's real source files, which is what makes the two cases the user cares
        about come out right without a guard of their own: a repository this run deleted has
        stopped referencing its key, and one whose deletion the user declined — or that
        failed to be deleted — still references it and keeps it alive.

        Scoped to `/etc/apt/keyrings`, the one directory that exists purely for this. Legacy
        `/etc/apt/trusted.gpg.d` keys are ambient trust that nothing references by
        construction, so "unused" is not computable for them; `/usr/share/keyrings` is
        package territory and holds keys the distro's own tooling put there. Both are left
        to accumulate rather than deleted on a guess.

        Each deletion is backed up into the unit's own backup directory first and recorded
        in `existed_before`, so a failing `apt-get update` rolls a collected key back with
        everything else. A key that cannot be backed up is not deleted: without the backup a
        rollback could not restore it, and an unused keyring costs nothing to keep.
        """
        candidates = frozenset(self._target_keys.in_dir(APT_KEYRINGS_DIR)) - frozenset(
            self._source_keys.in_dir(APT_KEYRINGS_DIR)
        )
        if not candidates:
            return
        references = await scan_source_file_references(self._probe.target_run, self._machines.target)
        referenced = {Path(ref).name for ref in references.all_refs()}

        for filename in sorted(candidates - referenced):
            dest = f"{APT_KEYRINGS_DIR}/{filename}"
            try:
                existed = await self._files.backup(dest, backup_dir)
            except ConvergeItemFailed as exc:
                self._log(
                    Host.TARGET,
                    LogLevel.WARNING,
                    f"keeping unused signing key {dest}: it could not be backed up first ({exc})",
                )
                continue
            if not existed:
                continue
            existed_before[dest] = True
            result = await self._files.delete(
                dest, mutates=f"delete signing key {dest}, which no repository references any more"
            )
            if not result.success:
                self._log(
                    Host.TARGET,
                    LogLevel.WARNING,
                    f"could not delete unused signing key {dest}: {result.stderr.strip()}",
                    stderr=result.stderr,
                )
                continue
            self._log(Host.TARGET, LogLevel.FULL, f"deleted signing key {dest}, which no repository references")
