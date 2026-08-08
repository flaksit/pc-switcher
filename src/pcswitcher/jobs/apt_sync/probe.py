"""Everything this job reads from the two machines, and the frozen facts each read returns.

One module for every read so "is anything asked per-package?" is answerable by looking in one
place. It is not: every command here is batched — one `apt-cache policy` over a whole name
set, one `sha256sum` listing per directory, one `awk` over every source file, one
`dpkg --search` over every key file.

The facts are frozen and answer their own questions (`SourceFileRefs.files_serving`,
`KeyDigests.digest_of`) rather than being dicts the rest of the job re-derives things from.
That is what lets the deciding half — origins, diffing, derivation, keyrings — be pure
functions over two snapshots instead of methods reaching into a job's mutable state.

Unlike apt packages, the five `/etc/apt` directories are diffed by whole-FILE digest: one
batched `sha256sum` listing per directory tells us which filenames differ without
transferring a single byte, and the full content of a file is only fetched for the files a
diff actually implicates (missing-on-target, extra-on-target, or digest-mismatched) — never
for a file that is already identical on both machines.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pcswitcher.executor import Executor, RemoteExecutor
from pcswitcher.jobs.apt_sync.commands import lines, policy_command, require_apt_answer
from pcswitcher.jobs.apt_sync.items import (
    APT_CONF_DIR,
    APT_PREFERENCES_DIR,
    APT_ROOT_DIR,
    APT_SOURCES_DIR,
    APT_SOURCES_LIST,
    DISTRIBUTION_ORIGIN_FILENAMES,
    KEY_DIRS,
    AptPackageItem,
    source_file_destination,
)
from pcswitcher.jobs.packages.apt_policy import (
    candidate_origins_by_package,
    installed_origins_by_package,
    normalise_repo_uri,
    packages_no_repository_can_install,
)
from pcswitcher.jobs.packages.items import Machines
from pcswitcher.jobs.packages.probes import require_answer
from pcswitcher.models import CommandResult

# What apt itself reads in each of its three fragment directories (`PKG-FR-APT-IGNORES`).
# `None` as the required extension means "no extension, or this one"; a tuple means one of
# these, mandatory. A directory absent from this map — the three KEY_DIRS — is captured
# whole, because a keyring is named by an explicit `Signed-By:` path rather than found by a
# fragment scan, and `trusted.gpg.d` has rules of its own that this job does not filter on.
#
# Measured on apt 2.8.3 / Ubuntu 24.04, by pointing apt at a scratch directory
# (`apt-cache policy -o Dir::Etc::preferencesparts=…`, `APT_CONFIG` with `Dir::Etc::parts`,
# `apt-get update --print-uris -o Dir::Etc::sourceparts=…`) and asking whether the file took
# effect, one filename at a time. Documented by `man 5 apt_preferences`, `man 5 apt.conf`
# and `man 5 sources.list`, which state the same three rules.
_FRAGMENT_EXTENSIONS: dict[str, tuple[str, ...] | None] = {
    APT_SOURCES_DIR: (".list", ".sources"),
    APT_PREFERENCES_DIR: None,
    APT_CONF_DIR: None,
}
_OPTIONAL_FRAGMENT_EXTENSION = {APT_PREFERENCES_DIR: ".pref", APT_CONF_DIR: ".conf"}
# The only characters apt accepts in a fragment filename. Measured: `99+vendor.pref` and
# `99 vendor.pref` are both ignored, as is any name starting with a dot.
_APT_FILENAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")

_SIGNED_BY_RE = re.compile(r"^Signed-By:\s*(?P<path>\S+)", re.IGNORECASE)
_LEGACY_SIGNED_BY_RE = re.compile(r"signed-by=(?P<path>[^\]\s,]+)")
# A deb822 stanza's repository URIs (one field, possibly several space-separated values,
# and one file may hold several stanzas), and a legacy `.list` line's single URI — which
# sits after the optional `[opt=val ...]` bracket, so the bracket must be consumed rather
# than treated as the URI.
_URIS_RE = re.compile(r"^URIs:\s*(?P<uris>.+)$", re.IGNORECASE)
_LEGACY_DEB_LINE_RE = re.compile(r"^deb(?:-src)?\s+(?:\[[^\]]*\]\s*)?(?P<uri>\S+)")

type Run = Callable[[str], Awaitable[CommandResult]]


# -- Parsers ----------------------------------------------------------------------------


def parse_sha256sum(output: str) -> dict[str, str]:
    """`<digest>  <path>` lines (one per `sha256sum` invocation) -> `{basename: digest}`.

    Basename, not the full path: every caller already knows which directory it asked
    about, and item identity is the filename, not the path.
    """
    digests: dict[str, str] = {}
    for line in lines(output):
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, path = parts
        digests[Path(path).name] = digest
    return digests


def apt_reads(directory: str, filename: str) -> bool:
    """Whether apt itself would read `filename` in `directory` (`PKG-FR-APT-IGNORES`).

    Three rules, and they are the same three in all three fragment directories, differing
    only in which extension each accepts:

    - the extension. `sources.list.d` requires `.list` or `.sources`; `preferences.d`
      accepts none or `.pref`; `apt.conf.d` accepts none or `.conf`. Case-sensitive: a
      `.PREF` file is ignored. The extension is what follows the LAST dot, so
      `99-vendor.pref.save` is a `.save` file and `a.b.pref` is a `.pref` one.
    - the characters. Letters, digits, underscore, hyphen and period only, and not a
      leading period.
    - `Dir::Ignore-Files-Silently` (`~$`, `\\.disabled$`, `\\.bak$`, `\\.dpkg-[a-z]+$`,
      `\\.ucf-[a-z]+$`, `\\.save$`, `\\.orig$`, `\\.distUpgrade$`) decides only whether apt
      PRINTS a notice about a file it is skipping. Every one of those patterns already
      fails one of the two rules above — `~` is not an accepted character and the rest are
      extensions no directory accepts — so nothing here needs to encode the list.

    Why it matters in both directions: a file apt ignores is not configuration, so copying
    the source's copy of it changes nothing on the target, and offering to delete the
    target's copy asks the user about a file that governs nothing. `PKG-FR-APT-IGNORES`
    forbids treating one as repository configuration in add, change and remove alike.

    A directory with no fragment rule — the three key directories — reads everything: a
    keyring is named by an explicit path, not found by a naming convention.
    """
    if directory not in _FRAGMENT_EXTENSIONS:
        return True
    if not _APT_FILENAME_RE.match(filename):
        return False
    _stem, dot, extension = filename.rpartition(".")
    required = _FRAGMENT_EXTENSIONS[directory]
    if not dot:
        return required is None
    return f".{extension}" in (required or (_OPTIONAL_FRAGMENT_EXTENSION[directory],))


def keyring_reference(value: str) -> str | None:
    """`value` as a keyring PATH reference, or `None` when it is not one.

    A `Signed-By:` field carries either a path or an INLINE armored key. Only an absolute
    path is a reference to a file that has to exist; anything else is the armored block
    itself and needs no provisioning, because it travels inside the source file. The
    distinction is not cosmetic: `add-apt-repository` writes the armor's first line on the
    field line (`Signed-By: -----BEGIN PGP PUBLIC KEY BLOCK-----`), so a bare `\\S+` capture
    turns every PPA added the normal way into a reference to a file named `-----BEGIN`,
    which resolves nowhere and downgrades the repository to `REPORT_ONLY`.
    """
    return value if value.startswith("/") else None


def parse_source_file(
    filename: str, content: str
) -> tuple[Literal["deb822", "list"], tuple[str, ...], tuple[str, ...]]:
    """A source file's format (by extension), every keyring path it names, and every
    repository URI it points at (normalised by `normalise_repo_uri`).

    deb822 `.sources` files name a key via a `Signed-By:` field and their repositories via
    `URIs:`; legacy `.list` files put both on the `deb` line, the key inside the options
    bracket as `[... signed-by=<path> ...]` and the URI immediately after it (RESEARCH
    Standard Stack). Parsed just far enough to extract these — never rewritten,
    normalised, or migrated between formats (RESEARCH Pitfall 3, deferred ideas).

    One parser, three consumers: the keyring refs drive `PKG-FR-KEY-COPY`'s dangling-reference check
    and keyring garbage collection, the URIs drive the source-removal impact (C26) by
    matching against the origin `apt-cache policy` reports for an installed package.

    A `Signed-By:` field may carry an INLINE armored key instead of a path, either with an
    empty field value and the block on continuation lines or with the block's first line on
    the field line itself. Neither yields a ref (`keyring_reference`), which is correct in
    both directions: the file depends on no key FILE, so nothing is invented for it, and no
    part of the armored block is mistaken for a path.
    """
    fmt: Literal["deb822", "list"] = "deb822" if filename.endswith(".sources") else "list"
    refs: list[str] = []
    uris: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if fmt == "deb822":
            signed_by = _SIGNED_BY_RE.match(line)
            uri_field = _URIS_RE.match(line)
            if uri_field:
                uris.extend(normalise_repo_uri(uri) for uri in uri_field.group("uris").split())
        else:
            signed_by = _LEGACY_SIGNED_BY_RE.search(raw_line)
            deb_line = _LEGACY_DEB_LINE_RE.match(line)
            if deb_line:
                uris.append(normalise_repo_uri(deb_line.group("uri")))
        if signed_by:
            ref = keyring_reference(signed_by.group("path"))
            if ref is not None:
                refs.append(ref)
    return fmt, tuple(refs), tuple(uris)


# -- Frozen facts -----------------------------------------------------------------------


@dataclass(frozen=True)
class SourceFileRefs:
    """One machine's source-file scan: `{filename: (keyring refs, repository URIs)}` for
    every file under `sources.list.d` AND `/etc/apt/sources.list`.

    Keyed by basename, which is what makes `/etc/apt/sources.list` a member of the same
    mapping as the directory's files (`source_file_destination` maps it back).
    """

    by_filename: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]]

    @classmethod
    def empty(cls) -> SourceFileRefs:
        return cls(by_filename={})

    def refs_of(self, filename: str) -> tuple[str, ...]:
        return self.by_filename.get(filename, ((), ()))[0]

    def uris_of(self, filename: str) -> tuple[str, ...]:
        return self.by_filename.get(filename, ((), ()))[1]

    def all_refs(self) -> frozenset[str]:
        return frozenset(ref for refs, _uris in self.by_filename.values() for ref in refs)

    def files_serving(self, origins: frozenset[str]) -> frozenset[str]:
        """Every file whose repository URIs intersect `origins` — the files that would have
        to travel for a package from those origins to be installable from the same place on
        the other machine (`PKG-FR-APT-IDENTITY`).

        The UNION, not a pick: a package's installed version can genuinely list several
        origins (a vendor repository and a security pocket both carrying it), and every one of
        them served it, so narrowing to one would drop a file the package really depends on.
        """
        return frozenset(filename for filename, (_refs, uris) in self.by_filename.items() if origins & frozenset(uris))

    def distribution_origins(self) -> frozenset[str]:
        """The URIs this machine's own distribution source files declare (`PKG-FR-APT-ORIGIN-VERIFY`).

        Computed per machine rather than matched against a list of known Ubuntu hostnames:
        the whole reason the exemption exists is that two machines legitimately point at
        different mirrors, so the only honest definition of "the distribution's archive" is
        "whatever this machine's `ubuntu.sources`/`sources.list`/ESM files say it is".
        """
        return frozenset(
            uri
            for filename, (_refs, uris) in self.by_filename.items()
            if filename in DISTRIBUTION_ORIGIN_FILENAMES
            for uri in uris
        )


@dataclass(frozen=True)
class KeyDigests:
    """One machine's key files across all three key directories, `{directory: {basename:
    digest}}` in `KEY_DIRS` order.

    Keys are not items (package docstring), so this IS the whole key model: provisioning
    compares two of these to decide what to copy, the readiness check consults them instead
    of re-probing the target, and collection uses the per-directory pair to tell a key the
    source machine still has from one it dropped. Keyed by filename, since that is what a
    `Signed-By:` reference resolves against.
    """

    by_dir: Mapping[str, Mapping[str, str]]

    @classmethod
    def empty(cls) -> KeyDigests:
        return cls(by_dir={})

    def in_dir(self, directory: str) -> Mapping[str, str]:
        return self.by_dir.get(directory, {})

    def dirs(self) -> tuple[tuple[str, Mapping[str, str]], ...]:
        """Each key directory paired with this machine's digest map for it, in `KEY_DIRS`
        order — the single place the three directories are enumerated, so adding or dropping
        one cannot be done in resolution but missed in provisioning.
        """
        return tuple((directory, self.by_dir.get(directory, {})) for directory in KEY_DIRS)

    @property
    def filenames(self) -> frozenset[str]:
        """Every key filename this machine has, across the three directories. A `Signed-By:`
        reference resolves against this set, so it is what decides whether a repository file
        can be written on the target at all — and therefore whether a package that needs that
        repository is replicable (`PKG-FR-APT-IDENTITY` class 4).
        """
        return frozenset(name for digests in self.by_dir.values() for name in digests)

    def digest_of(self, ref: str) -> str | None:
        """This machine's digest for the key a `Signed-By:` reference names, looked up by
        BASENAME across all three directories.

        Basename rather than the full path because that is how `keyrings.dangling_ref`
        already resolves a reference, and the two must agree: a reference this method
        cannot resolve is exactly one that check already downgraded the repository for.
        """
        name = Path(ref).name
        return next((digests[name] for _dir, digests in self.dirs() if name in digests), None)

    def path_of(self, ref: str) -> str | None:
        """Where this machine keeps the key a reference names, or `None` when it has no such
        key at all — `PKG-FR-KEY-COPY`'s dangling reference, already reported on the REPOSITORY item.
        """
        name = Path(ref).name
        return next((f"{directory}/{name}" for directory, digests in self.dirs() if name in digests), None)


@dataclass(frozen=True)
class OriginFacts:
    """One machine's `/etc/apt` state as the PACKAGE diff needs it (`PKG-FR-APT-IDENTITY`/`PKG-FR-APT-ORIGIN-VERIFY`).

    Captured before the package diff runs, because which repository file declares a
    package's origin, which of those files are the distribution's own, and whether the
    file's `Signed-By:` resolves to a key that machine actually has are all inputs to the
    package's diff class.
    """

    keys: KeyDigests
    source_digests: Mapping[str, str]
    """`{filename: digest}` for `sources.list.d`, narrowed to the extensions apt reads."""
    sources_list_digest: str | None
    """`/etc/apt/sources.list`'s digest, or `None` where the file is absent. Captured
    separately from the directories because it is a single file: it has no `find` listing to
    appear in, and it is one of the files written and updated but never removed (`PKG-FR-DISTRO-FILES`)."""
    refs: SourceFileRefs

    @classmethod
    def empty(cls) -> OriginFacts:
        """The state before anything has been captured. A job that never planned has these,
        which is what lets `accept_review` on a hand-assembled plan derive an empty write set
        rather than needing a capture it has no reason to run."""
        return cls(keys=KeyDigests.empty(), source_digests={}, sources_list_digest=None, refs=SourceFileRefs.empty())


@dataclass(frozen=True)
class RepoFacts:
    """One machine's remaining two reviewable `/etc/apt` directories, captured once the
    package diff has run."""

    pin_digests: Mapping[str, str]
    conf_digests: Mapping[str, str]

    @classmethod
    def empty(cls) -> RepoFacts:
        return cls(pin_digests={}, conf_digests={})


@dataclass(frozen=True)
class TargetPolicy:
    """One batched `apt-cache policy` on the target, parsed for every question this run asks
    of it — never one call per question, and never one call per package.

    The installed and the candidate rows are different rows and answer different questions
    (`apt_policy` module docstring): the candidate says what the target WOULD install, the
    installed says where what it already has came from.
    """

    candidate_origins: Mapping[str, frozenset[str]]
    installed_origins: Mapping[str, frozenset[str]]
    no_repository_can_install: frozenset[str] = frozenset()
    """The names in the TARGET's own manual set the target's apt has no repository to
    install from. The target's HALF of one verdict per name: it decides only for a name the
    source does not have, since `plan()` lets the source's own answer decide for every name
    the source has (`PKG-FR-DEB-OWNERSHIP`)."""

    @classmethod
    def empty(cls) -> TargetPolicy:
        return cls(candidate_origins={}, installed_origins={})


@dataclass(frozen=True)
class RepoConflict:
    """One repository file the two machines disagree about that feeds packages the target
    keeps (`PKG-FR-APTCONF`) — the only `/etc/apt` CHANGE that is still a question.

    Both whole versions are carried, never a diff of them: the question is which of two
    configurations the machine should have, and the user's position is that a diff of two
    repository definitions is not readable.
    """

    packages: tuple[str, ...]
    target_version: str
    source_version: str


# -- The reads --------------------------------------------------------------------------


async def capture_dir_digests(run: Run, directory: str, machine: str) -> dict[str, str]:
    """One `if sudo test -d <dir>; then sudo find <dir> -maxdepth 1 -type f -exec sha256sum
    {} +; fi` per directory — a single batched command, never one `sha256sum` per file.
    `-exec ... {} +` never runs at all when the directory has no matching files, so an empty
    directory degrades to an empty digest map rather than a shell error.

    What comes back is narrowed to the files apt itself reads (`apt_reads`), so the
    `.save`/`.curtin.orig`/`.dpkg-dist` copies Ubuntu's own tooling leaves behind never
    become syncable items in any of the three fragment directories. The narrowing is applied
    to the listing rather than expressed as a `find` predicate because the rule is not one
    of extension alone — an accepted character set, and a per-directory optional extension —
    and a `find` expression saying all of that would be unreadable and unlike the manpages
    it comes from.

    An ABSENT directory is a separate case and is what the `sudo test -d` wrapper is for
    (ADR-022). Measured: `find` on a path that does not exist exits 1, exactly as it does
    when it cannot read one that does — so without the wrapper the two would be one signal,
    and the only ways to resolve it are both wrong. Failing every run on a machine with no
    `/etc/apt/preferences.d` turns a legitimate "no pins here" into an error; accepting the
    exit code turns a directory this run could not read into "that machine has no
    repositories", which offers every file on the other machine for removal. With the
    wrapper an absent directory answers "nothing" at exit 0 and a non-zero exit is only
    ever a real failure, which `require_answer` then fails the job on.

    The test runs under `sudo` for the same WR-04 reason the `find` does, and the two must
    stay at the same privilege: measured, an unprivileged `test -d` on a directory inside an
    unsearchable parent exits 1, which collapses the whole `if` to exit 0 with no output —
    the reshape's one failure mode, answering "this machine has no pins or keys" for a
    directory root would have listed.
    """
    quoted = shlex.quote(directory)
    command = f"if sudo test -d {quoted}; then sudo find {quoted} -maxdepth 1 -type f -exec sha256sum {{}} +; fi"
    result = await run(command)
    require_answer(command, result, machine)
    return {
        filename: digest
        for filename, digest in parse_sha256sum(result.stdout).items()
        if apt_reads(directory, filename)
    }


async def capture_file_digest(run: Run, path: str) -> str | None:
    """One `sudo sha256sum <path>`, or `None` when the file is absent.

    The single-file counterpart to `capture_dir_digests`, for `/etc/apt/sources.list`,
    which is a file rather than a directory and so has no `find` listing to appear in.
    Verified: `sha256sum` on a missing path exits 1 and writes nothing to stdout, so the
    absent case falls out of the parse rather than needing a probe of its own.
    """
    result = await run(f"sudo sha256sum {shlex.quote(path)}")
    return parse_sha256sum(result.stdout).get(Path(path).name)


async def read_file_content(run: Run, path: str, machine: str) -> str:
    """One `sudo cat <path>` — used only for a file a diff actually implicates.

    `sudo`-qualified to match `capture_dir_digests`'s `sudo find ... sha256sum`
    privilege (WR-04): an unprivileged `cat` on a source file locked down to
    `0600`-or-similar would silently return empty stdout instead of failing, while
    the digest capture (root) still sees it and proposes a diff — an `AptSourceItem`
    parsed from that empty content would find zero `keyring_refs`, so a dangling key
    reference this run never actually validated would go undetected.

    Guarded on the exit code (ADR-022). Every path reaching here was named by the digest
    capture root ran moments earlier, so the file exists and root can read it; measured in a
    stock `ubuntu:24.04`, `cat` exits 1 on an absent or unreadable path and nothing else
    makes it exit non-zero, so a non-zero exit here is only ever a real failure. What the
    silence would otherwise become is file CONTENT: the repository-conflict review shows
    this text as the two machines' versions of a file it is asking permission to overwrite
    (`PKG-FR-APTCONF`), and two empty panes are an overwrite approved off a diff nobody
    could read. An empty answer at exit 0 stays data — an empty source file is a legitimate
    file.
    """
    command = f"sudo cat {shlex.quote(path)}"
    result = await run(command)
    require_answer(command, result, machine)
    return result.stdout


async def scan_source_file_references(run: Run, machine: str) -> SourceFileRefs:
    """`{filename: (keyring_refs, repository URIs)}` for EVERY source file on a machine,
    from ONE batched command — `sources.list.d` AND `/etc/apt/sources.list`.

    Machine-agnostic by construction (it takes the `run` callable and names no host), and
    run against BOTH machines: the target's answer drives the two consumers below, the
    source's answer is what maps a package's origin URIs back to the repository file that
    would have to travel for it (`PKG-FR-APT-IDENTITY`).

    Two target-side consumers, both of which need a fact no diff carries. The source-removal
    impact (C26) needs the repository URIs of a file whose deletion is offered. Keyring
    garbage collection needs the reference count of a key across every source file that
    exists, which is emphatically not the set of files any diff implicates: a keyring is
    commonly named only by files that are byte-identical on both machines, or that the user
    marked machine-specific, or — `/etc/apt/sources.list` — that pc-switcher never syncs at
    all. Missing any of those would delete a key that is still in use.

    Deliberately unfiltered by extension, unlike `capture_dir_digests`' `sources.list.d`
    capture: a keyring named only by a file apt ignores is still a key nothing else
    references, and keeping it is cheaper than deleting one that turns out to be in use.

    `find ... -exec awk {} +` passes every file to one awk process, never one command per
    file, and awk emits only the `URIs:`/`Signed-By:`/`deb` lines rather than whole
    files — so this stays compatible with the rule that full content is fetched only for a
    file a diff implicates. `sudo`-qualified to match `capture_dir_digests`'s privilege
    (WR-04): an unprivileged read of a locked-down source file returns empty output rather
    than failing, which would silently report no dependency where one exists.
    """
    awk = (
        r"tolower($0) ~ /^uris:/ || tolower($0) ~ /signed-by/ || tolower($0) ~ /^[ \t]*deb(-src)?[ \t]/ "
        r'{print FILENAME "\t" $0}'
    )
    # ONE start point that is always there, with `-path` selecting the two locations, rather
    # than naming `/etc/apt/sources.list.d` and `/etc/apt/sources.list` as two start points.
    # Measured: naming a `/etc/apt/sources.list` that is absent makes find complain on stderr
    # about that path and exit 1 while still walking the directory — so the two-start-point
    # form reports a legitimately-absent file with the same exit code as a scan that could
    # not run at all. That ambiguity is not affordable here: the scan's silence reads as "no
    # source file references any keyring", which deletes keys still in use. Selecting by
    # `-path` makes an absent file match nothing at exit 0, leaving a non-zero exit to mean
    # only a real failure, which `require_answer` fails the job on (ADR-022).
    selector = f"\\( -path {shlex.quote(APT_SOURCES_LIST)} -o -path {shlex.quote(f'{APT_SOURCES_DIR}/*')} \\)"
    command = (
        f"sudo find {shlex.quote(APT_ROOT_DIR)} -maxdepth 2 -type f {selector} -exec awk {shlex.quote(awk)} {{}} +"
    )
    result = await run(command)
    require_answer(command, result, machine)
    lines_by_file: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        path, tab, rest = line.partition("\t")
        if tab:
            lines_by_file.setdefault(Path(path).name, []).append(rest)
    parsed: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for filename, file_lines in lines_by_file.items():
        _fmt, refs, uris = parse_source_file(filename, "\n".join(file_lines))
        parsed[filename] = (refs, uris)
    return SourceFileRefs(by_filename=parsed)


class AptProbe:
    """Every read this job issues, against both machines.

    Holds the two executors and nothing else: each method returns its facts rather than
    storing them, so nothing here can be consulted before it was captured.
    """

    def __init__(self, source: Executor, target: RemoteExecutor, machines: Machines) -> None:
        self._source = source
        self._target = target
        self._machines = machines

    async def source_run(self, cmd: str) -> CommandResult:
        return await self._source.run_command(cmd)

    async def target_run(self, cmd: str) -> CommandResult:
        return await self._target.run_command(cmd, login_shell=False)

    # -- The manually-installed package set --------------------------------------------

    async def source_manual_names(self) -> list[str]:
        """The source's `apt-mark showmanual` set.

        Guarded (ADR-022) because its silence is the single most expensive misreading in this
        job: an empty source manifest is "the source has no apt packages", which offers every
        package on the target for removal. Measured, apt exits 100 when it cannot read the
        status file or parse `apt.conf.d`, so a non-zero exit is the discriminator. Emptiness
        is NOT — see ADR-022 for the one measured failure that survives it.
        """
        command = "apt-mark showmanual"
        result = await self._source.run_command(command)
        require_answer(command, result, self._machines.source)
        return lines(result.stdout)

    async def source_policy(self, manual_names: Sequence[str]) -> str:
        """One batched `apt-cache policy` over the source's whole manual set (never one call
        per package), as raw stdout for the caller to parse for both facts it answers.

        The no-repository half uses the same predicate `manual_deb_sync` uses, from the
        same shared parser rather than a shared result: `PKG-FR-JOB-INDEPENDENCE` keep the four jobs
        independent, so both jobs pay their own batched call instead of one importing the
        other. What apt can install from no repository it cannot install on the target
        either, so leaving it in would produce a proposed `INSTALL` that fails with "Unable
        to locate package" while `manual_deb_sync` offers the same package as an install
        snippet in the same run.

        Guarded, because BOTH facts read out of it fail silently and in the dangerous
        direction: an unanswered probe leaves the origin map empty, which exempts every
        package from the `PKG-FR-APT-ORIGIN-VERIFY` origin check, and leaves the no-repository set empty, which
        offers `manual_deb_sync`'s packages as apt installs that cannot work. Every
        name here came from `apt-mark showmanual`, so every name IS installed on the source
        and apt owes a block for each — which is what makes `blocks` unambiguous here.
        """
        if not manual_names:
            return ""

        command = policy_command(manual_names)
        result = await self._source.run_command(command)
        # A second walk over output already in memory, so the guard can count blocks without
        # the caller having to hand its own parse back down here.
        require_apt_answer(
            command, result, self._machines.source, blocks=len(installed_origins_by_package(result.stdout))
        )
        return result.stdout

    async def capture_source_items(
        self,
    ) -> tuple[list[AptPackageItem], Mapping[str, frozenset[str]], frozenset[str]]:
        """Manually-installed apt packages on the source with versions (`PKG-FR-APT-SCOPE`), plus
        `{package: origin URIs of its INSTALLED version}` — the provenance `PKG-FR-APT-IDENTITY`
        replicates — plus the SOURCE's answer to "which of these has apt no repository to
        install from".

        That third value is a fact, not a filter: `plan()` turns it into one verdict per
        name and applies that verdict to BOTH manifests (`PKG-FR-DEB-OWNERSHIP`). Excluding
        here instead would make it the SOURCE's manifest that loses the name while the
        target's keeps it, and one name in one manifest is a removal.

        The one batched `apt-cache policy` this needs answers two questions, so it is issued
        once and parsed twice: which names apt can install from nowhere, and where each of
        the rest came from (the left-hand side of every `PKG-FR-APT-IDENTITY` comparison). A
        second call over the same names would cost a second full policy run to learn
        something already on screen.
        """
        names = await self.source_manual_names()
        policy = await self.source_policy(names)
        origins = installed_origins_by_package(policy)
        no_repository = packages_no_repository_can_install(policy, names)
        items = await self._resolve_versions(names, self.source_run, self._machines.source)
        return items, origins, no_repository

    async def query_target_items(self) -> list[AptPackageItem]:
        """The target's own manually-installed apt packages, with versions — the read
        `capture_target_items` pairs with the target's policy.

        Guarded on the same terms as `capture_source_items` and for the mirror-image
        reason (ADR-022): an empty target manifest is "the target has no apt packages",
        which proposes installing the source's entire package set and hands the collateral
        rehearsal a simulation of it.
        """
        command = "apt-mark showmanual"
        manual = await self._target.run_command(command, login_shell=False)
        require_answer(command, manual, self._machines.target)
        return await self._resolve_versions(lines(manual.stdout), self.target_run, self._machines.target)

    async def capture_target_items(self, source_names: Sequence[str]) -> tuple[list[AptPackageItem], TargetPolicy]:
        """The target's manual manifest, plus the one batched `apt-cache policy` every
        target-side origin question is read out of — the mirror of `capture_source_items`.

        The policy carries the target's own "apt can install this from nowhere" answer, which
        `plan()` uses for the names the source does not have. `PKG-FR-DEB-OWNERSHIP` forbids
        `apt_sync` an item for that software "in any configuration", and the target-only case
        is the one the source's answer cannot cover: the name is absent from the source's
        manifest and present in the target's, so it would diff as an ordinary removal and the
        run would offer to `apt-get remove` software apt never installed and cannot reinstall.

        That answer costs no call of its own. The target's manual names are asked in the SAME
        `apt-cache policy` that already asks the source's names — one call for the machine,
        parsed for three questions (the candidate rows, the installed rows, and this one),
        never one call per question and never one per package. That is the economy A18/A10 fix
        for the source, held to on the target.
        """
        items = await self.query_target_items()
        policy = await self.collect_target_policy(source_names, [item.name for item in items])
        return items, policy

    @staticmethod
    async def _resolve_versions(names: Sequence[str], run: Run, machine: str) -> list[AptPackageItem]:
        """Resolve every name's version with ONE `dpkg-query` call (RESEARCH.md).

        Guarded on the exit code alone (ADR-022). A version this call fails to supply
        becomes the empty string, which reads as a version difference against the other
        machine and turns the whole manifest into upgrade proposals. Measured: `dpkg-query`
        exits 1 when ANY queried name is unknown to dpkg and when its admin directory is
        unreadable; every name here came from `apt-mark showmanual`, which lists only
        installed packages (measured: a package removed without `--purge` leaves the
        showmanual set), so the first case cannot arise and a non-zero exit means dpkg.
        """
        if not names:
            return []

        quoted = " ".join(shlex.quote(name) for name in names)
        # dpkg-query, not `apt list --installed`: apt's own manpage warns the latter's
        # output has no stable contract for scripting. The literal \t/\n below are
        # dpkg-query's OWN format-string escapes (interpreted by dpkg-query, not the
        # shell) — hence a plain (non-f) string so Python leaves them as two-char
        # backslash sequences for dpkg-query to expand into real tab/newline.
        versions_command = "dpkg-query --show --showformat='${Package}\\t${Version}\\n' " + quoted
        versions_result = await run(versions_command)
        require_answer(versions_command, versions_result, machine)

        versions: dict[str, str] = {}
        for line in versions_result.stdout.splitlines():
            if not line.strip():
                continue
            pkg_name, _, version = line.partition("\t")
            versions[pkg_name] = version

        return [AptPackageItem(name=name, version=versions.get(name, "")) for name in names]

    async def collect_hold_sets(self) -> tuple[frozenset[str], frozenset[str]]:
        """Source and target package-hold NAME sets from `apt-mark showhold` on BOTH
        machines (#208, D5). Read from both ends because the hold is replicated as a
        membership diff: a name held on the source but not the target becomes a hold
        (INSTALL), the reverse an unhold (REMOVE), and the target set also suppresses a
        held package's own install/upgrade action in the package diff.

        Exit code only (ADR-022): holding nothing is what most machines do, so an empty
        answer here is ordinary data — unlike the manifest reads, where emptiness at least
        means something is unusual. A failed read would still flip the membership diff in
        both directions, which is what the exit-code guard covers.
        """
        command = "apt-mark showhold"
        source_hold = await self._source.run_command(command)
        require_answer(command, source_hold, self._machines.source)
        target_hold = await self._target.run_command(command, login_shell=False)
        require_answer(command, target_hold, self._machines.target)
        return frozenset(lines(source_hold.stdout)), frozenset(lines(target_hold.stdout))

    async def capture_target_installed(self) -> frozenset[str]:
        """Every package NAME dpkg reports as installed on the target — not the manual set,
        the whole of it.

        Two callers need the same fact and neither can be answered from `apt-mark showmanual`:

        - a hold naming a package the machine does not HAVE ends the run
          (`PKG-FR-HOLD-WITHOUT-PACKAGE`). Measured on `ubuntu:24.04`: `apt-mark hold` exits
          0 and records the hold for a merely-uninstalled package, and `apt-get install` then
          refuses with `E: Held packages were changed`. Membership here is what separates
          that bookkeeping selection from a real hold.
        - a repository is withheld from deletion while anything on the target still installs
          from it (`PKG-FR-REPO-DELETE`), and an automatically-installed package is still
          something: `commands.remove_args` runs `apt-get remove`, never `autoremove`, so
          nothing in this job takes an unused dependency away, and a kept manual package can
          require it anyway.

        `${Package}`, not `${binary:Package}`: the arch-qualified form only appears for a
        foreign architecture, and both callers want the plain name apt-mark and apt-cache
        speak. Both also err safe on an over-inclusive answer — a hold stays real, a
        repository stays.

        Guarded on the exit code AND on emptiness (ADR-022): a machine with no installed
        packages does not exist, so nothing here is a legitimate empty answer, and silence
        read as data would end every run whose target holds anything.
        """
        return await self._capture_installed(self.target_run, self._machines.target)

    async def capture_source_installed(self) -> frozenset[str]:
        """The same set on the source, read only by a run whose source holds something.

        `PKG-FR-HOLD-WITHOUT-PACKAGE` binds both machines, and the source's own hold set is
        replicated wholesale, so a hold the source records for a package it does not have
        would otherwise be pushed onto the target as a hold on something neither machine has.
        The manual set cannot answer it: apt records a hold against a package it installed
        automatically just as readily.
        """
        return await self._capture_installed(self.source_run, self._machines.source)

    async def _capture_installed(self, run: Run, machine: str) -> frozenset[str]:
        command = "dpkg-query --show --showformat='${Package}\\t${db:Status-Status}\\n'"
        result = await run(command)
        fields = (line.partition("\t") for line in lines(result.stdout))
        installed = frozenset(name for name, _, status in fields if status == "installed")
        require_answer(command, result, machine, answers=len(installed), answer_noun="installed package")
        return installed

    async def collect_target_policy(self, names: Sequence[str], target_names: Sequence[str] = ()) -> TargetPolicy:
        """ONE batched `apt-cache policy` on the target over the source's whole package set
        AND the target's own manual set (never one call per package, and never one call per
        question it answers).

        The source's set is asked whole rather than only the missing names because two
        questions are asked of the same output: what the target would install for a name it
        lacks, and where the copy it already has came from — the second is what makes a
        package installed on both machines from two different vendors visible at all
        (`PKG-FR-APT-IDENTITY`). `target_names` adds the third: which of the target's own packages
        the target's apt can install from nowhere, which is `PKG-FR-DEB-OWNERSHIP`'s verdict
        for the names the source does not have.

        Exit code only, deliberately without the `blocks` half (ADR-022): most of these are
        the SOURCE's names asked of the TARGET's apt, and a name the target has never heard
        of is the ordinary case this call exists to detect. The third answer degrades in the
        safe direction on its own — `packages_no_repository_can_install` indicts no name apt
        printed nothing about, so an answer missing a block withholds nothing rather than
        withholding everything.
        """
        asked = sorted(set(names) | set(target_names))
        if not asked:
            return TargetPolicy.empty()

        command = policy_command(asked)
        result = await self._target.run_command(command, login_shell=False)
        require_apt_answer(command, result, self._machines.target)
        return TargetPolicy(
            candidate_origins=candidate_origins_by_package(result.stdout),
            installed_origins=installed_origins_by_package(result.stdout),
            no_repository_can_install=packages_no_repository_can_install(result.stdout, target_names),
        )

    async def capture_target_manual_set(self) -> frozenset[str]:
        """The target's `apt-mark showmanual` set — one batched command, the single
        source of the auto-versus-manual collateral split (`PKG-FR-COLLATERAL-MANUAL`). This is the same set
        apt itself consults to decide what it may remove, so classifying a collateral
        package by membership here matches apt's own notion of "the user chose this".

        Guarded (ADR-022): an unanswered read leaves the set empty, which classifies every
        collateral package as automatic and so switches `PKG-FR-COLLATERAL-MANUAL`'s protection off entirely,
        silently and in the direction that removes packages.
        """
        command = "apt-mark showmanual"
        result = await self._target.run_command(command, login_shell=False)
        require_answer(command, result, self._machines.target)
        return frozenset(lines(result.stdout))

    # -- `/etc/apt` ---------------------------------------------------------------------

    async def capture_origin_state(self) -> tuple[OriginFacts, OriginFacts]:
        """The `/etc/apt` facts the PACKAGE diff needs, both machines, captured before it
        runs. Returns `(source, target)`.

        They are captured here rather than with the rest of `/etc/apt` because the origin
        classification (`PKG-FR-APT-IDENTITY`) consumes them: which repository file declares a
        package's origin, which of those files are the distribution's own, and whether the
        file's `Signed-By:` resolves to a key the source actually has are all inputs to the
        package's diff class, and the package diff runs first.

        Unconditional, one batched command per machine for the scan: which keyrings the
        target's sources point at is what makes a key correct, and that is a property of
        EVERY source file on the target, not just the ones a diff implicates.

        The two source-file digest sets are captured here for a second reason: the ESM gate
        (`PKG-FR-DISTRO-FILES`) reads them to decide whether it must ask, and it asks before the package diff
        runs so a "skip" answer does not cost the user the whole planning pass and a review
        they would answer for nothing.
        """
        # One `sha256sum` listing per key directory per machine, driven by `KEY_DIRS` so
        # capture, reference resolution and provisioning can never disagree about which
        # directories exist.
        source_keys = {d: await capture_dir_digests(self.source_run, d, self._machines.source) for d in KEY_DIRS}
        target_keys = {d: await capture_dir_digests(self.target_run, d, self._machines.target) for d in KEY_DIRS}

        source_sources = await capture_dir_digests(self.source_run, APT_SOURCES_DIR, self._machines.source)
        target_sources = await capture_dir_digests(self.target_run, APT_SOURCES_DIR, self._machines.target)
        source_list_digest = await capture_file_digest(self.source_run, APT_SOURCES_LIST)
        target_list_digest = await capture_file_digest(self.target_run, APT_SOURCES_LIST)

        target_refs = await scan_source_file_references(self.target_run, self._machines.target)
        source_refs = await scan_source_file_references(self.source_run, self._machines.source)

        return (
            OriginFacts(
                keys=KeyDigests(by_dir=source_keys),
                source_digests=source_sources,
                sources_list_digest=source_list_digest,
                refs=source_refs,
            ),
            OriginFacts(
                keys=KeyDigests(by_dir=target_keys),
                source_digests=target_sources,
                sources_list_digest=target_list_digest,
                refs=target_refs,
            ),
        )

    async def capture_repo_state(self) -> tuple[RepoFacts, RepoFacts]:
        """The two remaining reviewable `/etc/apt` directories on both machines, by whole-file
        digest: one batched `sha256sum` listing per directory per machine. Returns
        `(source, target)`.
        """
        source_pins = await capture_dir_digests(self.source_run, APT_PREFERENCES_DIR, self._machines.source)
        target_pins = await capture_dir_digests(self.target_run, APT_PREFERENCES_DIR, self._machines.target)
        source_configs = await capture_dir_digests(self.source_run, APT_CONF_DIR, self._machines.source)
        target_configs = await capture_dir_digests(self.target_run, APT_CONF_DIR, self._machines.target)
        return (
            RepoFacts(pin_digests=source_pins, conf_digests=source_configs),
            RepoFacts(pin_digests=target_pins, conf_digests=target_configs),
        )

    async def capture_conf_filenames(self, *, on_source: bool) -> frozenset[str]:
        """The `/etc/apt/apt.conf.d` filenames ONE machine has, for the question
        `capture_repo_state`'s digests answer for both at once.

        Asked on its own because its one caller — the machine-specific mark reconciliation
        (`AptSyncJob.observe_absent_marks`) — runs at a different moment in the run than the
        diff does, on one machine at a time, and only when that machine's decision file
        actually names an `apt:config:` item.
        """
        run, machine = (
            (self.source_run, self._machines.source) if on_source else (self.target_run, self._machines.target)
        )
        return frozenset(await capture_dir_digests(run, APT_CONF_DIR, machine))

    async def capture_distribution_owned_keys(
        self, target_keys: KeyDigests, source_keys: KeyDigests, distribution_origins: frozenset[str]
    ) -> frozenset[str]:
        """Absolute paths of the target's key files that the target's own DISTRIBUTION
        packaging owns — the one exemption `PKG-FR-KEY-REFRESH` grants a differing key.

        Two reads, both batched and both narrowed to what the exemption can bite on:

        - ONE `dpkg --search` over every key file the target has (never one call per file —
          the `manual_installs_sync._scan_unowned_installs` shape), which gives `{path:
          owning packages}`. Its exit code is deliberately ignored: `dpkg --search` returns
          non-zero as soon as ANY argument matches no package, which for a machine with even
          one hand-placed key is always. Ownership is read out of stdout, where each matched
          path arrives as `<package[, package...]>: <path>`; unmatched paths go to stderr and
          simply produce no entry, which is exactly the "unowned" answer.
        - ONE `apt-cache policy` over the packages owning the keys whose bytes DIFFER from
          the source machine's, which is the only population `Keyrings.manages` is ever
          consulted about. A run where the two machines' keys match pays nothing.

        The distribution test is the origin its owning package's INSTALLED version came
        from, matched against the origins this machine's own distribution source files
        declare (`SourceFileRefs.distribution_origins`, `PKG-FR-APT-ORIGIN-VERIFY`) — the same per-machine
        definition every other origin question here uses, so a machine on a local mirror is
        not read as a vendor. A vendor's `.deb` that ships a keyring — `tailscale-archive
        -keyring` from `https://pkgs.tailscale.com/stable/ubuntu`, measured on the
        development machine — is therefore NOT the distribution, and its rotated key is
        refreshed like any other. `ubuntu-keyring` and `ubuntu-pro-client`, from the
        machine's archive and ESM suites, are.

        EVERY owning package must be the distribution's for the path to be exempt: the
        article's default is that a differing key is refreshed, so a path a vendor package
        also claims keeps the vendor's rotation rather than the exemption. A package whose
        installed version comes from no repository at all — a hand-installed `.deb` — has an
        empty origin set and is never the distribution.

        Read-only, no sudo: both commands query the target's own databases.
        """
        paths = sorted(f"{directory}/{name}" for directory, digests in target_keys.dirs() for name in digests)
        if not paths:
            return frozenset()
        result = await self.target_run(f"dpkg --search {' '.join(shlex.quote(path) for path in paths)}")
        owners: dict[str, tuple[str, ...]] = {}
        for line in result.stdout.splitlines():
            packages, separator, path = line.rpartition(": ")
            if separator and path.startswith("/"):
                owners[path.strip()] = tuple(name.strip() for name in packages.split(",") if name.strip())

        differing = {
            path: packages
            for path, packages in owners.items()
            if packages and source_keys.digest_of(path) != target_keys.digest_of(path)
        }
        if not differing:
            return frozenset()

        names = sorted({name for packages in differing.values() for name in packages})
        command = policy_command(names)
        policy = await self.target_run(command)
        # Exit code only (ADR-022). An unanswered probe leaves every owner without an origin
        # and so exempts nothing, which refreshes a key the distribution owns — a wrong write
        # rather than a stale key, so it fails the job instead of degrading.
        require_apt_answer(command, policy, self._machines.target)
        origins = installed_origins_by_package(policy.stdout)
        distribution = {name for name in names if (uris := origins.get(name)) and uris <= distribution_origins}
        return frozenset(
            path for path, packages in differing.items() if all(name in distribution for name in packages)
        )

    async def packages_by_source_file(
        self, filenames: frozenset[str], names: Sequence[str], target_refs: SourceFileRefs
    ) -> dict[str, list[str]]:
        """`{filename: the `names` the target installs from it}`, for the files in
        `filenames` that feed at least one — the shared computation behind both `/etc/apt`
        follow-ups (`02-SPEC-package-review-model.md` §4.1).

        Two callers, two questions, one read. A repository the source no longer has is
        withheld from the review entirely while anything the target keeps still comes from
        it (`PKG-FR-REPO-DELETE`); a repository whose two copies differ becomes the conflict
        screen instead of a silent overwrite (`PKG-FR-REPO-CONFLICT`). Both turn on which of
        the target's packages this file feeds, so `names` carries the union of the two
        populations and each caller reads its own subset back out.

        The withholding caller counts everything installed on the target plus its
        machine-specific marks; the conflict caller counts the marks alone. Marks are in both
        because a machine-specific package is structurally invisible — `filter_inert` drops
        it from the target manifest before anything is diffed, so it can never produce an
        `ItemDiff` of its own — and the user's explicit "this machine keeps this" is exactly
        the promise a silent deletion or repoint breaks. Automatically-installed packages
        count as usage: `commands.remove_args` runs `apt-get remove`, never `autoremove`, so
        nothing in this job takes one away when its reason goes, and a manual package the
        user keeps can require it regardless.

        There is no key counterpart: a signing key is never offered for deletion or change,
        so there is no review text for one to carry. The user approves the REPOSITORY;
        whichever keyring that leaves unused is collected afterwards with no decision.

        Costs one batched `apt-cache policy` over `names` (never one per package, the
        `collect_target_policy` shape), gated on `filenames` being non-empty so an ordinary
        run pays nothing; the source-file scan it also needs was already captured for keyring
        correctness, so this adds no second scan.

        Guarded on the exit code (ADR-022): an unanswered probe answers "this repository
        feeds nothing", which is the answer that offers a repository still in use for
        deletion and lets one feeding machine-specific packages be overwritten silently —
        the exact two failures this method exists to prevent. Without the `blocks` half,
        because a name may come from a decision FILE and a package recorded there may since
        have been removed, so an empty answer is reachable without anything being broken.
        """
        if not filenames or not names:
            return {}

        names = sorted(set(names))
        command = policy_command(names)
        policy = await self.target_run(command)
        require_apt_answer(command, policy, self._machines.target)
        origins_by_package = installed_origins_by_package(policy.stdout)
        packages_by_origin: dict[str, list[str]] = {}
        for name in names:
            for origin in origins_by_package.get(name, frozenset()):
                packages_by_origin.setdefault(origin, []).append(name)

        by_file: dict[str, list[str]] = {}
        for filename in sorted(filenames):
            reached: set[str] = set()
            for uri in target_refs.uris_of(filename):
                reached.update(packages_by_origin.get(uri, ()))
            if reached:
                by_file[filename] = sorted(reached)
        return by_file

    async def capture_repo_conflicts(self, machine_specific: Mapping[str, list[str]]) -> dict[str, RepoConflict]:
        """Read both machines' copies of every conflicted repository file, so the review can
        show the two versions (ruling 6).

        Only for a file that differs AND feeds a machine-specific package — the whole point
        of the trigger is that this is rare, so paying two `cat`s per entry is cheaper than
        any scheme that avoids them. Every other differing file is overwritten silently.
        """
        conflicts: dict[str, RepoConflict] = {}
        for filename, packages in machine_specific.items():
            path = source_file_destination(filename)
            target_version = await read_file_content(self.target_run, path, self._machines.target)
            source_version = await read_file_content(self.source_run, path, self._machines.source)
            conflicts[filename] = RepoConflict(
                packages=tuple(packages), target_version=target_version, source_version=source_version
            )
        return conflicts

    async def target_pro_attached(self, status_command: str) -> CommandResult:
        """The raw `pro status` result. Parsing stays in `esm_gate` — the payload names the
        subscriber's account, so only the parsed boolean may ever leave it (`PKG-FR-DISTRO-FILES`).

        `withhold_output` is what keeps that true of the executor's own debug trace, which
        otherwise records every command's stdout verbatim (`PKG-FR-LOG-VERBATIM`): the
        subscriber's identity would reach the log file before `esm_gate` ever parsed the
        payload, and no downstream filter can help — by then it is already written
        (`PKG-FR-ESM-PRIVACY`).
        """
        return await self._target.run_command(
            status_command,
            login_shell=False,
            withhold_output="the Ubuntu Pro attachment payload names the subscriber (PKG-FR-ESM-PRIVACY)",
        )
