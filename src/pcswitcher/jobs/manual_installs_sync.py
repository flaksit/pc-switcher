"""`manual_installs_sync`: software an install script dropped straight onto the filesystem,
which no package manager knows about at all (D-15, D-18, D-19, D-20, D-21).

One detector, run on BOTH machines (`PKG-FR-MANUAL-DIFF`) — the source's findings are the
candidates, and a finding the target already holds is dropped rather than presented: paths
under `/opt`, directly under `/usr/local`, and inside `/usr/local`'s `bin`, `sbin`, `lib`,
`games` and `src` that no dpkg package owns, bypassing apt entirely
(`PKG-FR-MANUAL-SCOPE`). Never `/usr/local`'s own skeleton (`_USR_LOCAL_SKELETON`), and
never a directory with no file anywhere beneath it. An unowned entry directly under `/opt`
is judged by its own shape, which can take a question (`PKG-FR-MANUAL-OPT-SHAPE`).

Hand-installed `.deb` packages are the OTHER thing no package manager can reproduce, and
they are `manual_deb_sync`'s (`PKG-FR-DEB-OWNERSHIP`) — a package, reproducible by a
snippet, but a package. This job's half is not apt's business at all, which is D-18's whole
argument for keeping it off `apt_sync`: folding it in would make disabling apt silently
disable it with nothing telling the user. It does its OWN `dpkg` queries rather than
sharing anyone else's, so ownership stays clean — it never imports `apt_sync` (D-18) and
never imports `manual_deb_sync`.

`ManualInstallsSyncJob` subclasses `UnreproducibleSyncJob`, which owns everything from the
diff onwards: the item shape, the plan pipeline, the shared install-snippet registry with
its push and consent question, the review grouping and the replay. What is here is the
detection those hooks call, plus this job's own validation and first-sync scope.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, override

from pcswitcher.executor import Executor
from pcswitcher.jobs.packages.probes import ProbeFailed, require_answer
from pcswitcher.jobs.packages.state import DecisionEntry
from pcswitcher.jobs.packages.unreproducible import UnreproducibleItem, UnreproducibleSyncJob, lines_of
from pcswitcher.models import FirstSyncScope, Host, ValidationError

__all__ = ["ManualInstallsSyncJob"]

# The origin every item this job produces carries, and so the slice of an `item_id` space
# that belongs to it. Named once: detection and the mark reconciliation key on the same
# string, and the file this job shares with nobody still holds another job's ids.
_ORIGIN = "unowned-path"

# The bounded unowned-install scan (`PKG-FR-MANUAL-SCOPE`): `/opt`, every entry directly
# under `/usr/local`, and the entries of the five `/usr/local` subdirectories software
# actually lands in. One shell loop runs `find <root> -mindepth 1 -maxdepth 1` over each,
# skipping any that is not there so a missing root is not an error to tell apart from a
# broken one (`_list_scan_entries`). Enough to NAME a finding, never enough to walk a tree:
# the item is decided on, not replicated.
#
# `etc`, `include`, `man` and `share` are deliberately absent. What a hand install puts
# there arrives with an application this scan finds elsewhere, so scanning them would raise
# a second finding for software already named once — and `man` is a symlink to `share/man`,
# which a scan that followed it would walk twice.
_UNOWNED_SCAN_ROOTS = (
    "/opt",
    "/usr/local",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/local/lib",
    "/usr/local/games",
    "/usr/local/src",
)

# The nine entries `base-files.postinst` creates directly under `/usr/local`: eight through
# its own `install_local_dir` helper, plus `man` as a symlink to `share/man`. Hardcoded for
# predictability rather than read off the machine — the scan's shape must not change with
# whatever a postinst happens to say on the day — with a VM test asserting that the machine's
# own `base-files.postinst` still declares exactly these, so a distribution that changes the
# skeleton is a failing test rather than a surprise in a review.
#
# None of them is ever a finding (`PKG-FR-MANUAL-SCOPE`): the distribution ships them and no
# package need own them, so presenting one would ask every user, on every machine, on every
# run, to write an install snippet for a stock directory whose contents this scan already
# names one level deeper. This is also what keeps a scanned directory out of its own scan —
# the five `/usr/local` roots above are all in this set, and `find` names them as entries of
# `/usr/local` like any other candidate.
_USR_LOCAL_SKELETON = frozenset(
    {
        "/usr/local/bin",
        "/usr/local/etc",
        "/usr/local/games",
        "/usr/local/include",
        "/usr/local/lib",
        "/usr/local/man",
        "/usr/local/sbin",
        "/usr/local/share",
        "/usr/local/src",
    }
)

# Where the vendor-or-application shape question applies (`PKG-FR-MANUAL-OPT-SHAPE`).
# `/usr/local` has no such ambiguity: its own layout puts an application's parts under the
# skeleton directories this scan already looks in one at a time.
_OPT_ROOT = "/opt"

# `find`'s type letter for a directory (`-printf '%y'`). Every other letter — `f`, `l`, `s`,
# `p`, `b`, `c` — means "not a directory", which is all this scan asks of it: a file, a
# symlink and a socket are equally a thing that is there rather than an empty shape.
_DIRECTORY = "d"

# Matches one `dpkg --search` "owned" line: `<package>[,<package>...]: <path>`. A path dpkg does
# not own produces no such line at all (its "no path found" diagnostic goes to stderr,
# which this scan never inspects) — absence from stdout is the only signal
# `_owned_paths_from_dpkg_s` needs. A private copy of apt_sync's identical regex: D-18
# keeps ownership clean by NOT importing apt_sync, and this parser is small enough that
# one duplicated line is cheaper than a shared-core coupling.
_DPKG_S_OWNED_RE = re.compile(r"^[^:]+:\s+(?P<path>/\S.*)$")

# One extra path handed to the unowned scan's `dpkg --search`, whose "owned" line is the
# proof that dpkg answered at all (`_scan_unowned_installs`). dpkg owns its own binary by
# construction — the package is Essential and ships it — so a batch that comes back without
# this line came back from a dpkg that did not answer, whatever it exited. It is filtered
# out of the candidates before anything is reported.
_DPKG_OWNERSHIP_WITNESS = "/usr/bin/dpkg"


def _owned_paths_from_dpkg_s(output: str) -> frozenset[str]:
    """Every path `dpkg --search` reports as owned, parsed from its stdout alone (D-19's
    unowned-install scan). A queried path dpkg does NOT own is simply absent from this set
    — its "no path found matching pattern" diagnostic is a stderr message this scan never
    reads, so a batched multi-path `dpkg --search` degrades cleanly even when some paths are
    unowned and others are not. A private copy of apt_sync's identical parser (D-18).
    """
    owned: set[str] = set()
    for line in output.splitlines():
        match = _DPKG_S_OWNED_RE.match(line)
        if match:
            owned.add(match.group("path"))
    return frozenset(owned)


def _typed_entries(output: str) -> list[tuple[str, str]]:
    """`(type letter, path)` per line of a `find -printf '%y\\t%p\\n'` listing.

    The type rides along with the path because every rule after the listing needs it — the
    `/opt` shape question counts directories against files, and the empty-directory rule
    only applies to a directory. Asking `find` for it costs nothing; asking again per path
    would be one command per candidate.

    A line without a tab is dropped rather than guessed at: a path containing a newline
    would arrive as one, and this scan reports what it can name for certain.
    """
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        kind, tab, path = line.partition("\t")
        if tab and path.startswith("/"):
            entries.append((kind, path))
    return entries


class ManualInstallsSyncJob(UnreproducibleSyncJob):
    """Detect, review and reproduce software no package owns under `/usr/local` and `/opt`
    (D-15/D-18/D-19), on this job's own enable flag independent of `apt_sync`'s and of
    `manual_deb_sync`'s.

    Supplies the two detection hooks `UnreproducibleSyncJob` leaves abstract; everything
    from the diff onwards — the snippet registry, its push and consent question, the review
    grouping and the replay — is inherited.
    """

    name: ClassVar[str] = "manual_installs_sync"
    manager_id: ClassVar[str] = "manual"

    # No configurable properties: mirrors AptSyncJob's empty schema — only the enable flag
    # in sync_jobs is needed. A job earns a config SECTION only when it has a real key, so there
    # is no `manual_installs_sync:` block in default-config.yaml, but the in-code CONFIG_SCHEMA
    # ClassVar still declares the empty object every job carries.
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    # -- Detection (D-19), run on both machines (`PKG-FR-MANUAL-DIFF`) -------------------

    async def _scan_unowned_installs(
        self, executor: Executor, machine: str, *, ask_when_ambiguous: bool
    ) -> list[UnreproducibleItem]:
        """Paths no dpkg package owns under this scan's roots — software an install script
        dropped there directly, bypassing apt entirely (`PKG-FR-MANUAL-SCOPE`).

        Four bounded steps, each one command, each skipped when the step before it left
        nothing to ask about:

        1. `_list_scan_entries` names every entry of every root, with its type.
        2. `dpkg --search` decides ownership over what is left once `_USR_LOCAL_SKELETON` is
           dropped; a path absent from the reply is unowned.
        3. `_resolve_opt_shapes` turns an unowned `/opt` entry into the finding its own shape
           makes it (`PKG-FR-MANUAL-OPT-SHAPE`), asking the user where the shape is genuinely
           ambiguous and `ask_when_ambiguous` says this machine's findings are the items.
        4. `_directories_holding_a_file` drops a directory with no file anywhere beneath it:
           an empty shape is not software, and there would be nothing for a snippet to
           reproduce.

        `dpkg --search` exits 1 as soon as ONE queried path is unowned, which is precisely
        the finding this scan is looking for, so its exit code says nothing about whether it
        answered (ADR-022, `PKG-FR-READ-FAILS-JOB`). `_DPKG_OWNERSHIP_WITNESS` supplies the
        answer the exit code cannot: a path dpkg must claim rides along in the same batch,
        and its absence from the reply means dpkg did not answer. Without it a dead
        `dpkg --search` prints nothing, every candidate looks unowned, and the user is asked
        to write an install snippet for every entry under `/opt` and `/usr/local`.

        Runs on whichever machine it is handed (`PKG-FR-MANUAL-DIFF`): the source's findings
        are the candidates, the target's are what the diff subtracts.
        """
        entries = await self._list_scan_entries(executor, machine)
        candidates = {path: kind for kind, path in entries if path not in _USR_LOCAL_SKELETON}
        if not candidates:
            return []

        quoted_paths = " ".join(shlex.quote(path) for path in [*sorted(candidates), _DPKG_OWNERSHIP_WITNESS])
        ownership_command = f"dpkg --search {quoted_paths}"
        ownership = await executor.run_command(ownership_command)
        owned = _owned_paths_from_dpkg_s(ownership.stdout)
        if _DPKG_OWNERSHIP_WITNESS not in owned:
            raise ProbeFailed(
                f"probe on {machine} did not answer — `{ownership_command}` reported no owner for "
                f"{_DPKG_OWNERSHIP_WITNESS}, which dpkg owns on every machine, so its silence about the other "
                f"paths is not an answer about them: {ownership.stderr.strip()}"
            )
        unowned = {path: kind for path, kind in candidates.items() if path not in owned}

        shaped = await self._resolve_opt_shapes(executor, machine, unowned, ask_when_ambiguous=ask_when_ambiguous)
        directories = sorted(path for path, kind in shaped.items() if kind == _DIRECTORY)
        holds_a_file = await self._directories_holding_a_file(executor, machine, directories)
        findings = [path for path, kind in shaped.items() if kind != _DIRECTORY or path in holds_a_file]

        return [UnreproducibleItem(origin=_ORIGIN, identifier=path, label=path) for path in sorted(findings)]

    async def _list_scan_entries(self, executor: Executor, machine: str) -> list[tuple[str, str]]:
        """Every entry of every scan root, one level deep, as `(type letter, path)`.

        One `find` per root, driven from a shell loop that SKIPS a root that is not there, so
        the one tolerated error is gone from the exit code rather than hidden behind a
        `2>/dev/null`. What is left — an unreadable root, a missing binary — exits non-zero
        and reaches `require_answer`. Empty output on a clean exit stays an ordinary answer:
        a machine with nothing under `/opt` is an ordinary machine. Silence, on the other
        hand, is not "nothing is installed by hand here"; it would drop every finding this
        job exists to make (`PKG-FR-READ-FAILS-JOB`).
        """
        quoted_roots = " ".join(shlex.quote(root) for root in _UNOWNED_SCAN_ROOTS)
        # One line, never a multi-line script: the command is echoed verbatim into the debug
        # trace and the `--confirm-each-command` gate.
        command = (
            f'for root in {quoted_roots}; do [ -d "$root" ] || continue; '
            "find \"$root\" -mindepth 1 -maxdepth 1 -printf '%y\\t%p\\n' || exit 1; done"
        )
        listing = await executor.run_command(command)
        require_answer(command, listing, machine)
        return _typed_entries(listing.stdout)

    async def _resolve_opt_shapes(
        self, executor: Executor, machine: str, unowned: Mapping[str, str], *, ask_when_ambiguous: bool
    ) -> dict[str, str]:
        """Replace each unowned `/opt/<name>` directory with the finding its shape makes it
        (`PKG-FR-MANUAL-OPT-SHAPE`), leaving every other candidate untouched.

        `/opt/<application>` and `/opt/<publisher>/<application>` look the same from outside,
        so what the directory holds decides: a file of its own makes it the application; no
        file and exactly one directory makes that directory the application; no file and
        several directories cannot be told apart and is the one question this job asks
        outside the review.

        `ask_when_ambiguous` is what separates the two machines. On the machine whose
        findings become items, the user answers. On the other, the shape decides nothing —
        the reading is only used to subtract what is already there — so BOTH readings are
        kept as held and nothing is asked: whichever one the answer produced on the other
        machine is then subtracted, and asking twice for one fact that changes no item is
        exactly the noise the diff exists to remove.

        One `find` for all the `/opt` directories together, listing each one level deep.

        Known cost: the question comes before the answers that could make it pointless. The
        finding's identity IS the answer, so neither the source's own machine-specific marks
        nor what the target already holds can be applied to a shape nobody has read yet — and
        an ambiguous directory whose every reading is already settled is therefore asked
        about once per run. Resolving it would mean reading the target before the source's
        shape step, which costs that read on every machine that has no findings at all.
        """
        opt_directories = sorted(
            path
            for path, kind in unowned.items()
            if kind == _DIRECTORY and path.startswith(f"{_OPT_ROOT}/") and "/" not in path[len(_OPT_ROOT) + 1 :]
        )
        if not opt_directories:
            return dict(unowned)

        quoted = " ".join(shlex.quote(path) for path in opt_directories)
        command = f"find {quoted} -mindepth 1 -maxdepth 1 -printf '%y\\t%p\\n'"
        listing = await executor.run_command(command)
        # No `answers=` guard: every one of these directories may legitimately be empty, and
        # an empty answer is ordinary data (ADR-022).
        require_answer(command, listing, machine)

        children: dict[str, list[tuple[str, str]]] = {path: [] for path in opt_directories}
        for kind, path in _typed_entries(listing.stdout):
            parent = path.rpartition("/")[0]
            if parent in children:
                children[parent].append((kind, path))

        shaped = {path: kind for path, kind in unowned.items() if path not in children}
        for path, entries in children.items():
            subdirectories = sorted(child for kind, child in entries if kind == _DIRECTORY)
            if len(subdirectories) != len(entries):
                shaped[path] = _DIRECTORY  # holds a file of its own: one application
            elif not subdirectories:
                continue  # holds nothing at all: not a finding
            elif len(subdirectories) == 1:
                shaped[subdirectories[0]] = _DIRECTORY
            elif not ask_when_ambiguous:
                shaped[path] = _DIRECTORY
                shaped.update(dict.fromkeys(subdirectories, _DIRECTORY))
            elif await self._ask_whether_one_application(path, subdirectories):
                shaped[path] = _DIRECTORY
            else:
                shaped.update(dict.fromkeys(subdirectories, _DIRECTORY))
        return shaped

    async def _ask_whether_one_application(self, path: str, subdirectories: Sequence[str]) -> bool:
        """Ask whether `path` is one application or a publisher's directory holding several
        (`PKG-FR-MANUAL-OPT-SHAPE`). True keeps `path` as the finding; False makes each of
        `subdirectories` a finding of its own.

        Asked while the run is still planning, because the answer decides what the review
        lists rather than what happens to any item on it — and asked in a rehearsal too, since
        a dry run puts the same questions as a real one (`PKG-FR-DRY-RUN`).

        Both answers name the machine the software would land on and say what would be
        reproduced there rather than how this job would go about it
        (`PKG-FR-NAME-THE-MACHINES`, `PKG-FR-EFFECT-NOT-MECHANISM`). With nobody to ask, the
        directory itself is the finding: it is the shallower of the two readings, and this
        scan reports what it finds where it finds it.
        """
        assert self.context.reviewer is not None, (
            f"{self.manager_id} sync has no reviewer; the orchestrator must inject one "
            "through JobContext.reviewer before plan()."
        )
        source, target = self.machines.source, self.machines.target
        named = ", ".join(subdirectories)
        answer = await self.context.reviewer.ask_gate(
            title=f"What is {path} on {source}?",
            message=(
                f"{path} on {source} holds no file of its own — only the directories {named}. "
                f"Either it is one application whose parts live in those directories, or it is one "
                f"publisher's directory holding an application per directory. Nothing on {source} says "
                f"which, and the answer decides what {target} is offered."
            ),
            proceed_label=f"One application — {path} is what would be reproduced on {target}",
            stop_label=f"One per directory — {named} are what would be reproduced on {target}, each on its own",
        )
        return answer is None or answer

    async def _directories_holding_a_file(
        self, executor: Executor, machine: str, directories: Sequence[str]
    ) -> frozenset[str]:
        """Of `directories`, those holding a file somewhere beneath them — the ones that are
        software rather than an empty shape (`PKG-FR-MANUAL-SCOPE`).

        One `find` per directory, stopping at the first non-directory it meets (`-quit`), so
        the cost is bounded however large the tree is and no tree is ever walked whole.

        A directory whose own `find` FAILED is kept rather than dropped: an unreadable
        subtree says nothing about whether a file is down there, and the harmless reading is
        the one that still puts the finding in front of the user. Dropping it would be
        silence read as data — the failure this scan's other guards exist to prevent.
        """
        if not directories:
            return frozenset()

        quoted = " ".join(shlex.quote(path) for path in directories)
        # `exit 0` at the end, because the loop's own last status says nothing: every
        # directory's outcome is on stdout, and a shell that could not run at all still
        # fails the command itself, which is what `require_answer` reads.
        command = (
            f'for dir in {quoted}; do if found=$(find "$dir" ! -type d -print -quit); '
            'then [ -n "$found" ] && echo "$dir"; else echo "$dir"; fi; done; exit 0'
        )
        result = await executor.run_command(command)
        require_answer(command, result, machine)
        return frozenset(lines_of(result.stdout))

    @override
    async def capture_source_items(self) -> Sequence[UnreproducibleItem]:
        """The source's unowned installs under this scan's roots (D-19)."""
        return await self._scan_unowned_installs(self.source, self.machines.source, ask_when_ambiguous=True)

    @override
    async def query_target_items(self) -> Sequence[UnreproducibleItem]:
        """What the TARGET already holds, in the source's own identities, so `plan()` can
        drop a finding that is already there (`PKG-FR-MANUAL-DIFF`).

        It exists so that a snippet which has already run stops being asked about — one
        snippet installs one application and leaves several traces, and each trace is a
        finding of its own. A path is held when the same scan finds it there unowned.
        """
        return await self._scan_unowned_installs(self.target, self.machines.target, ask_when_ambiguous=False)

    @override
    async def observe_absent_marks(self, entries: Mapping[str, DecisionEntry], *, on_source: bool) -> frozenset[str]:
        """The marked paths one machine no longer has.

        Asked of BOTH machines, unlike `plan()`, which reads the source's file alone. The
        two questions are different: which marks silence a FINDING is the source's business,
        because a finding is something the source has and the target lacks, but whether a
        marked item is still on the machine holding the mark is a question about that machine
        and nothing else. Reconciling the source's file alone would leave a machine that is
        only ever synced TO carrying its dead marks for good.

        Not the SCAN's answer, and that is the point: `PKG-FR-MANUAL-SCOPE` bounds the scan
        to `/opt` and `/usr/local`, so a marked path outside those roots is absent from every
        scan while sitting on disk. `test -e` asks the filesystem instead.

        Entries this job cannot recognise are left exactly where they are.
        """
        executor = self.source if on_source else self.target
        machine = self.machines.source if on_source else self.machines.target

        prefix = UnreproducibleItem.id_prefix(_ORIGIN)
        paths = {item_id: item_id.removeprefix(prefix) for item_id in entries if item_id.startswith(prefix)}
        if not paths:
            return frozenset()

        present = await self._paths_that_exist(frozenset(paths.values()), executor, machine)
        return frozenset(item_id for item_id, path in paths.items() if path not in present)

    @staticmethod
    async def _paths_that_exist(paths: frozenset[str], executor: Executor, machine: str) -> frozenset[str]:
        """Which of `paths` are on `machine`, in ONE command — a `test -e` per path inside a
        single loop, never a command per path.

        The loop prints the paths that exist and exits 0 whatever the individual tests said
        (a `for` over `if`s ends on the `if`'s own 0), so the exit code stays a statement
        about the shell rather than about the paths, and `require_answer` can guard it. That
        matters here: silence read as data would say every marked path is gone and drop every
        mark this job holds.
        """
        listing = " ".join(shlex.quote(path) for path in sorted(paths))
        command = f'for p in {listing}; do if test -e "$p"; then printf "%s\\n" "$p"; fi; done'
        result = await executor.run_command(command)
        require_answer(command, result, machine)
        return frozenset(lines_of(result.stdout))

    @override
    async def validate(self) -> list[ValidationError]:
        """The commands this job's own detection runs: `dpkg` on the source, and `dpkg` on
        the target, which is read too now that a finding the target already holds is not
        presented (`PKG-FR-MANUAL-DIFF`). Both machines are only ever read for detection, so
        no sudo is needed for it. A snippet's own sudo needs are unpredictable (an opaque
        blob, D-20), so this job does NOT pre-validate target sudo; a snippet that needs it
        and lacks it fails as a per-item converge failure (D-27), reported like any other.

        Sequential checks appending to `errors`, never raising mid-validate (matches
        `AptSyncJob.validate()`'s shape).
        """
        errors: list[ValidationError] = []

        dpkg_check = await self.source.run_command("dpkg --version")
        if not dpkg_check.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE, "dpkg is not available on source (required to detect unowned installs)"
                )
            )

        target_dpkg_check = await self.target.run_command("dpkg --version")
        if not target_dpkg_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET, "dpkg is not available on target (required to tell what it already has)"
                )
            )

        return errors

    @classmethod
    @override
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Name this job's destructive first-sync scope (ADR-015): replaying install
        snippets for unowned installs under `/usr/local` and `/opt`."""
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=["unowned installs under /usr/local and /opt (via recorded install snippets)"],
            mechanism="replay install snippet per item, after review",
        )
