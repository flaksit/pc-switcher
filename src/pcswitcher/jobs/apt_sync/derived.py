"""Which `/etc/apt` files travel without a review line of their own, and who pays when one
fails to land (ADR-020 D-37/D-38/D-39).

Three buckets, in the order they are written, and each is a different answer to "why does
this file travel":

- every `/etc/apt/preferences.d` file the source has. A pin decides which origin wins, which
  is exactly what origin replication turns on; one naming an origin the target lacks is inert,
  so always-sync costs nothing and cannot get a derivation wrong.
- the distribution's own source files. The user wants both machines pointed at the same
  archive, and these are the files that say where it is.
- the repository files serving the approved installs, from each package's own `OriginPlan`
  (ruling 4). Nothing else makes a repository travel: one that feeds no package this run
  syncs stays where it is.

A derived write has no item, so it cannot fail as one. It is recorded against its destination
and charged to every approved package whose origin depended on it (D-39) — the refusal lands
on the thing the user actually decided about, naming the file.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from pcswitcher.jobs.apt_sync.items import (
    APT_PREFERENCES_DIR,
    APT_SOURCES_DIR,
    APT_SOURCES_LIST,
    CONFLICT_ID_PREFIX,
    DISTRO_SOURCE_FILENAMES,
    package_name,
    source_file_destination,
)
from pcswitcher.jobs.apt_sync.origins import OriginClassifier
from pcswitcher.jobs.apt_sync.probe import OriginFacts, RepoFacts
from pcswitcher.jobs.packages.items import DiffAction, ItemClass, ItemDiff, Machines
from pcswitcher.jobs.packages.review import Decision


class StrandedRepository(NamedTuple):
    """One repository file that landed on the target for an install the user then declined,
    and that no surviving approved install needs.

    Carries the URLs as well as the destination because the filename is whatever its author
    chose — the same reason `build_repo_removal_detail` names URLs — and the packages it was
    written for, which is what makes the sentence say why it is there.
    """

    dest: str
    uris: tuple[str, ...]
    packages: tuple[str, ...]


class DerivedWrites:
    """The `/etc/apt` write set the accepted decisions imply, and its failure bookkeeping."""

    def __init__(  # noqa: PLR0913 - both machines' origin and repo facts; all keyword-only
        self,
        *,
        source_origin_facts: OriginFacts,
        target_origin_facts: OriginFacts,
        source_repo_facts: RepoFacts,
        target_repo_facts: RepoFacts,
        origins: OriginClassifier,
        machines: Machines,
    ) -> None:
        self._machines = machines
        self._source_origin = source_origin_facts
        self._target_origin = target_origin_facts
        self._source_repo = source_repo_facts
        self._target_repo = target_repo_facts
        self._origins = origins
        self._pin_writes: tuple[str, ...] = ()
        self._distro_writes: tuple[str, ...] = ()
        self._repo_writes: tuple[str, ...] = ()
        # `{absolute destination: why it failed}`. A derived write fails no item of its own —
        # there is no item — so it is recorded here and charged to every approved package
        # that needed the file (D-39). A rollback puts EVERY derived write in here, matching
        # what it already does to the reviewed half of the group.
        self._failed: dict[str, str] = {}
        # `{package item_id: the derived destinations that package needs}`, the inverse
        # lookup D-39's attribution runs at install time.
        self._package_dests: dict[str, frozenset[str]] = {}

    @property
    def pin_writes(self) -> tuple[str, ...]:
        return self._pin_writes

    @property
    def distro_writes(self) -> tuple[str, ...]:
        return self._distro_writes

    @property
    def repo_writes(self) -> tuple[str, ...]:
        return self._repo_writes

    @property
    def failed(self) -> Mapping[str, str]:
        """`{destination: why it failed}` — read by the install refusal, and the one place a
        derived write's outcome is observable at all, since it has no item to fail."""
        return self._failed

    def all_writes(self) -> tuple[str, ...]:
        """Every derived destination, in the order the repository unit writes them: pins
        before sources (so a pin is in place the moment its origin becomes fetchable), the
        distribution's files before the vendors'.

        Signing keys are not here and must not be: this set is what the unit backs up, rolls
        back and charges a failed install to, and a key has none of those relationships to a
        package item. `Keyrings` computes its own writes from the same decisions, and both
        sets are logged and previewed (`PKG-FR-DERIVED-VISIBLE`).
        """
        return (*self._pin_writes, *self._distro_writes, *self._repo_writes)

    @property
    def written_source_filenames(self) -> frozenset[str]:
        """The basenames of the repository files this run writes — what decides which
        machine's keyring references survive on the target (`Keyrings.surviving_refs`)."""
        return frozenset(Path(dest).name for dest in (*self._distro_writes, *self._repo_writes))

    def build(
        self,
        diffs: Sequence[ItemDiff],
        decisions: Mapping[str, Decision],
        *,
        conflicts: Mapping[str, object],
        withheld_esm: frozenset[str],
    ) -> None:
        """Turn the accepted decisions into the `/etc/apt` files this run writes WITHOUT a
        review line (ADR-020 D-37/D-38) — the counterpart to the reviewed half the repository
        unit carries.

        Only files the target lacks or holds different bytes for are listed — an identical
        file needs no write and can therefore fail nothing. `_package_dests` records which
        packages each write serves, because a derived write has no item of its own to fail
        (D-39) and must charge its failure to the packages that needed it.
        """
        self._failed = {}
        self._package_dests = {}

        def differs(source_digests: Mapping[str, str], target_digests: Mapping[str, str], filename: str) -> bool:
            return target_digests.get(filename) != source_digests[filename]

        self._pin_writes = tuple(
            f"{APT_PREFERENCES_DIR}/{filename}"
            for filename in sorted(self._source_repo.pin_digests)
            if differs(self._source_repo.pin_digests, self._target_repo.pin_digests, filename)
        )

        # A conflict the user declined is a file this run may NOT write (ruling 6). It is
        # seeded as a failed derived write rather than merely dropped, because a package
        # whose origin depended on it cannot be delivered and installing it anyway would put
        # the wrong vendor's software on the target — the one outcome D-34 exists to prevent.
        skipped = {
            source_file_destination(filename): (
                f"the user chose to keep {self._machines.target}'s version of this file for now (ADR-020 D-37)"
            )
            for filename in conflicts
            if decisions.get(f"{CONFLICT_ID_PREFIX}{filename}") != Decision.APPLY
        }
        self._failed = dict(skipped)

        distro: list[str] = [
            f"{APT_SOURCES_DIR}/{filename}"
            for filename in sorted(
                (DISTRO_SOURCE_FILENAMES - withheld_esm) & frozenset(self._source_origin.source_digests)
            )
            if differs(self._source_origin.source_digests, self._target_origin.source_digests, filename)
        ]
        if (
            self._source_origin.sources_list_digest is not None
            and self._source_origin.sources_list_digest != self._target_origin.sources_list_digest
        ):
            distro.append(APT_SOURCES_LIST)
        self._distro_writes = tuple(dest for dest in distro if dest not in skipped)

        repo: set[str] = {
            source_file_destination(filename)
            for filename in conflicts
            if decisions.get(f"{CONFLICT_ID_PREFIX}{filename}") == Decision.APPLY
        }
        for diff in diffs:
            if diff.item_class is not ItemClass.APT_PACKAGE or diff.action is not DiffAction.INSTALL:
                continue
            if decisions.get(diff.item_id) != Decision.APPLY:
                continue
            origin_plan = self._origins.plans.get(diff.item_id)
            if origin_plan is None:
                continue
            # The attribution set keeps a skipped conflict; the write list does not. That
            # asymmetry IS D-39's rule: the package still depended on the file.
            needed = {
                source_file_destination(filename)
                for filename in origin_plan.derived_files
                if self._target_origin.source_digests.get(filename) != self._source_origin.source_digests.get(filename)
            }
            if needed:
                repo.update(needed)
                self._package_dests[diff.item_id] = frozenset(needed)
        self._repo_writes = tuple(sorted(repo - frozenset(distro) - frozenset(skipped)))

    def record_failure(self, dest: str, reason: str) -> None:
        self._failed[dest] = reason

    def fail_all(self, message: str) -> None:
        """Mark every derived write failed — the rollback path (D-39).

        The derived half needs the same treatment for the same reason a rollback fails items
        whose own write succeeded: what landed on the target is the pre-run state, so every
        package whose origin depended on one of those files must fail rather than install from
        wherever apt would now serve it.
        """
        for dest in self.all_writes():
            self._failed[dest] = message

    def stranded(self, declined: frozenset[str]) -> tuple[StrandedRepository, ...]:
        """The repository files this run wrote for `declined` installs that nothing approved
        still needs — what the target keeps after a mid-apply answer withdrew the packages
        the file was derived from.

        Read off the same `{package: the files it needs}` map the install refusal uses, so a
        file a surviving approved install still needs cannot be named: it is doing exactly
        the job it was written for. A file that never landed is not here either — a failed
        write left nothing on the target to report.

        Pins and the distribution's own files can never appear: they travel because the
        source has them, not because a package was approved (`PKG-FR-REPO-DERIVED`), so no
        answer about a package strands one.
        """
        needed = {dest for item_id, dests in self._package_dests.items() if item_id not in declined for dest in dests}
        left: dict[str, list[str]] = {}
        for item_id in sorted(declined):
            for dest in sorted(self._package_dests.get(item_id, frozenset())):
                if dest in needed or dest in self._failed or dest not in self._repo_writes:
                    continue
                left.setdefault(dest, []).append(package_name(item_id))
        return tuple(
            StrandedRepository(dest, self._source_origin.refs.uris_of(Path(dest).name), tuple(names))
            for dest, names in left.items()
        )

    def install_refusal(self, item_id: str, name: str) -> str | None:
        """Why this approved install may not run because a file it needed never landed
        (D-39), or `None` when every derived file it depends on is in place.

        The attribution a derived write cannot make for itself: it has no item, so its
        failure has to be charged to the packages whose origin depended on it. Naming the
        file and the reason is what keeps "the install failed" from reading as an apt
        problem when it is a `/etc/apt` write problem.
        """
        for dest in sorted(self._package_dests.get(item_id, frozenset())):
            reason = self._failed.get(dest)
            if reason is not None:
                return f"install of {name} refused: {dest} was not written ({reason})"
        return None
