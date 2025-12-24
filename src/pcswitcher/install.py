from __future__ import annotations

from pcswitcher.version import Release, Version


def get_install_with_script_command_line(v: Release | Version | str | None = None, /) -> str:
    """Get the command line to install a specific version of pc-switcher using the install script.

    Args:
        v: Release, Version, or branch name to install.
    """
    set_version = ""
    set_ref = ""
    if isinstance(v, Release):
        url_ref = f"refs/tags/{v.tag}"
        if v < Version.parse("v0.1.0-alpha.4") and v.tag.startswith("v"):
            set_version = f"VERSION='{v.tag[1:]}'"
        else:
            set_version = f"VERSION='{v.tag}'"
    elif isinstance(v, Version):
        url_ref = f"refs/tags/v{v.semver_str()}"
        v_str = v.semver_str()
        if v < Version.parse("v0.1.0-alpha.4") and v_str.startswith("v"):
            set_version = f"VERSION='{v_str[1:]}'"
        else:
            set_version = f"VERSION='{v_str}'"
    elif isinstance(v, str):
        url_ref = f"refs/heads/{v}"
        set_ref = f"-s -- --ref '{v}'"
    else:
        url_ref = "refs/heads/main"

    script_url = f"https://raw.githubusercontent.com/flaksit/pc-switcher/{url_ref}/install.sh"

    return f"curl -sSL {script_url} | {set_version} bash {set_ref}"
