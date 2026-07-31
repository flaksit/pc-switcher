"""Unit tests for the passwordless-sudo remediation text, and the static audit that keeps
the two sudo/lock preconditions inside `validate()`.

A validation error that only says "passwordless sudo is not available" leaves the user
to research sudoers syntax and the safe way to edit it, so the actionable parts of this
message are behaviour worth pinning: the drop-in path, the visudo invocation, the grant
line, and the verification command.

The audit below pins where those preconditions are checked rather than what they say.
It lives here because it is about the same two probes as the hint: the grant the hint
teaches, and the dpkg lock a run cannot proceed through.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pcswitcher
from pcswitcher.sudoers import SUDOERS_DROPIN_PATH, passwordless_sudo_hint


class TestPasswordlessSudoHint:
    """K65 — the copy-paste remediation every sudo validation failure carries."""

    def test_names_every_required_binary(self) -> None:
        """K65."""
        hint = passwordless_sudo_hint(("/usr/bin/apt-get", "/usr/bin/install"))

        assert "/usr/bin/apt-get" in hint
        assert "/usr/bin/install" in hint

    def test_uses_the_drop_in_not_etc_sudoers(self) -> None:
        """Editing /etc/sudoers directly would fight the distribution's own file."""
        hint = passwordless_sudo_hint(("/usr/bin/rsync",))

        assert SUDOERS_DROPIN_PATH in hint
        assert "/etc/sudoers " not in hint

    def test_directs_the_user_through_visudo(self) -> None:
        """A syntax error written by a plain editor can lock the user out of sudo."""
        hint = passwordless_sudo_hint(("/usr/bin/rsync",))

        assert f"visudo --file={SUDOERS_DROPIN_PATH}" in hint
        assert "visudo" in hint

    def test_includes_a_verification_command(self) -> None:
        hint = passwordless_sudo_hint(("/usr/bin/rsync",))

        assert "sudo --non-interactive true" in hint

    def test_substitutes_a_known_user_into_the_grant_line(self) -> None:
        hint = passwordless_sudo_hint(("/usr/bin/snap",), user="alice")

        assert "alice ALL=(ALL) NOPASSWD: /usr/bin/snap" in hint
        assert "YOUR_USER" not in hint

    def test_flags_the_placeholder_when_the_user_is_unknown(self) -> None:
        hint = passwordless_sudo_hint(("/usr/bin/snap",))

        assert "YOUR_USER ALL=(ALL) NOPASSWD: /usr/bin/snap" in hint
        assert "replacing YOUR_USER" in hint

    def test_says_a_broader_grant_is_acceptable(self) -> None:
        """ADR-013's entry is a lower bound; a machine may grant wider rights."""
        hint = passwordless_sudo_hint(("/usr/bin/rsync",))

        assert "broader" in hint.lower()


_SRC = Path(pcswitcher.__file__).parent

# The two environment preconditions a package job cannot recover from mid-run: the
# passwordless grant, and the dpkg frontend lock. Matched as text rather than as call
# sites, so a probe hoisted into a constant or built into a helper is caught too.
_ENVIRONMENT_PROBES = ("sudo --non-interactive", "fuser /var/lib/dpkg/lock-frontend")

# Occurrences of that text which are not a run issuing the probe, keyed by
# `<relpath>::<enclosing qualname>`. One entry today: the hint prints the verification
# command for the user to run themselves.
_NOT_A_PROBE: dict[str, str] = {
    "sudoers.py::passwordless_sudo_hint": "the verification command the hint tells the user to run",
}

# The modules that must each keep at least one probe, so the audit cannot pass by
# matching nothing after a rename or a move.
_MODULES_WITH_PROBES = frozenset(
    {
        "jobs/apt_sync/job.py",
        "jobs/flatpak_sync.py",
        "jobs/snap_sync.py",
    }
)


@dataclass(frozen=True)
class _ProbeSite:
    """One place the source mentions an environment probe, and the function it sits in."""

    relpath: str
    qualname: str
    lineno: int
    text: str

    @property
    def key(self) -> str:
        return f"{self.relpath}::{self.qualname}"


def _collect_probe_sites() -> list[_ProbeSite]:
    """Every string in `src/pcswitcher/` naming one of the probes, by enclosing function.

    The enclosing qualname is the whole point: the requirement is about WHERE the
    environment is checked, not how often.
    """
    sites: list[_ProbeSite] = []

    def visit(node: ast.AST, qualname: str, relpath: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                visit(child, f"{qualname}.{child.name}" if qualname else child.name, relpath)
                continue
            if (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and any(probe in child.value for probe in _ENVIRONMENT_PROBES)
            ):
                sites.append(_ProbeSite(relpath, qualname, child.lineno, child.value.strip()))
            visit(child, qualname, relpath)

    for path in sorted(_SRC.rglob("*.py")):
        visit(ast.parse(path.read_text(encoding="utf-8")), "", path.relative_to(_SRC).as_posix())
    return sites


class TestEnvironmentPreconditionsStayInValidate:
    """K67 — a precondition is discovered in the validation step, never mid-execute.

    A run that re-probes sudo or the dpkg lock while applying has already changed
    something by the time it finds out it cannot proceed, and a job that degrades to a
    reduced capture when a probe fails hides the missing grant instead of reporting it.
    Both are shapes of code, not observable outcomes, so this is a static audit rather
    than a behavioural test: no fixture can prove the absence of a call site.
    """

    def test_the_audit_sees_the_probes_in_every_job_that_needs_them(self) -> None:
        """Guard against a rubber stamp: an audit that matches nothing passes vacuously."""
        found = {site.relpath for site in _collect_probe_sites()}

        assert found >= _MODULES_WITH_PROBES, f"probes missing from {sorted(_MODULES_WITH_PROBES - found)}"

    def test_no_environment_probe_sits_outside_a_validate_body(self) -> None:
        """The requirement: every sudo and dpkg-lock probe is inside a `validate()`."""
        misplaced = [
            f"    {site.relpath}:{site.lineno}  ({site.qualname})  {site.text}"
            for site in _collect_probe_sites()
            if site.qualname.rpartition(".")[2] != "validate" and site.key not in _NOT_A_PROBE
        ]

        assert not misplaced, (
            "environment probes outside validate():\n"
            + "\n".join(misplaced)
            + "\n    Move the check into the job's validate(), so the run refuses before it "
            "changes anything, or record it in _NOT_A_PROBE if it issues no probe."
        )
