# Versioning and Release Strategy of Python project on GitHub

This is a conversation with Grok 4.1 on Perplexity.

## Initial User Prompt

What is the standard or best practice for releasing and versioning a python tool on github, with the following constraints:
- I do not want to publish the package on pypi
- I want users to be able to install it by curl'ing a bash script and executing it
- I want dynamic versioning. The version is set when I create a new github release

## Versioning Strategy

Use semantic versioning (SemVer) with GitHub tags like `v1.2.3`. Create annotated tags (`git tag -a v1.2.3 -m "Release 1.2.3"`) and push them to trigger releases. For dynamic versioning in the Python package, configure `pyproject.toml` with `dynamic = ["version"]` and a tool like `setuptools_scm`, `versioneer`, or `uv-dynamic-versioning` to derive the version from the nearest Git tag during builds.[^1_1][^1_2][^1_3]

## Release Process

Push a SemVer tag to GitHub, which can automatically create a release via GitHub Actions workflows. Build distributions (wheels or sdist) using `python -m build` or `uv build`, then upload them as release assets named like `tool-v1.2.3-py3-none-any.whl`. Validate that the package metadata version matches the tag in CI to prevent mismatches.[^1_4][^1_2][^1_5]

## Installation Script

Host an `install.sh` script in the repo root, invoked via `curl -sL https://github.com/OWNER/REPO/releases/latest/download/install.sh | bash`. The script queries the GitHub API for the latest release tag (`curl -s https://api.github.com/repos/OWNER/REPO/releases/latest | jq -r .tag_name`), downloads the matching wheel (`curl -L https://github.com/OWNER/REPO/releases/download/v${VERSION}/tool-${VERSION}-py3-none-any.whl -o /tmp/tool.whl`), and installs it (`pip install /tmp/tool.whl --user`). Include checks for prerequisites like `curl`, `jq`, `pip`, and handle errors with `set -e`.[^1_6][^1_7][^1_8]

## Best Practices

- Automate builds and uploads in `.github/workflows/release.yml` triggered on tag push.
- Warn users to review scripts before `curl | bash` and provide checksums.
- For CLI tools, use entry points in `pyproject.toml` (e.g., `[project.scripts] tool = "tool:main"`). Test installs on Ubuntu and other distros.[^1_9][^1_10][^1_11][^1_12][^1_13][^1_14][^1_15][^1_16][^1_17][^1_18][^1_19][^1_20]

<div align="center">⁂</div>

[^1_1]: https://pypi.org/project/dynamic-versioning/

[^1_2]: https://dev.to/tosin2013/how-to-keep-your-python-package-metadata-in-sync-with-github-release-tags-fkb

[^1_3]: https://pydevtools.com/handbook/how-to/how-to-add-dynamic-versioning-to-uv-projects/

[^1_4]: https://discuss.python.org/t/how-to-single-source-a-packages-version-when-publishing-to-pypi-using-github-action/50982

[^1_5]: https://dev.to/glasnt/automating-python-package-releases-by-extending-git-2ncd

[^1_6]: https://stackoverflow.com/questions/49062932/install-script-curled-from-github

[^1_7]: https://gist.github.com/brettjrea/540629e1f21f8d28f4124a20ce201666

[^1_8]: https://github.com/dropseed/barrel

[^1_9]: https://github.com/dssg/install-cli

[^1_10]: https://www.linkedin.com/pulse/versioning-python-code-tagging-software-releases-github-messemer-rxpff

[^1_11]: https://github.com/oracle/oci-cli/issues/424

[^1_12]: https://github.com/curl/curl

[^1_13]: https://www.reddit.com/r/linuxquestions/comments/16z440g/how_does_curl_whatever_scriptsh_bash_work/

[^1_14]: https://docs.rockylinux.org/10/gemstones/git/00-gh_cli_installation/

[^1_15]: https://python.plainenglish.io/automatic-versioning-and-release-python-package-ba15dfb8adf0

[^1_16]: https://www.reddit.com/r/learnpython/comments/uiz3p9/inhouse_python_package_release_best_practices/

[^1_17]: https://github.blog/engineering/engineering-principles/scripting-with-github-cli/

[^1_18]: https://github.com/pypa/setuptools/discussions/3630

[^1_19]: https://github.com/spacetelescope/style-guides/blob/master/guides/release-workflow.md

[^1_20]: https://github.com/PackeTsar/Install-Python


---

# I want the install script to not use pip, but to install the package as an uv tool. Show me a concrete install.sh script with all the necessary code.

Here is a concrete `install.sh` script that dynamically fetches the latest GitHub release tag, installs `uv` if missing (via its official installer), and installs your Python tool via `uv tool install` from the specific Git tag—no `pip` involved. Users run it with `curl -sSL https://github.com/OWNER/REPO/releases/latest/download/install.sh | bash` (host this raw script as a release asset too, or in repo root).[^2_1][^2_2]

```bash
#!/usr/bin/env bash
set -euo pipefail

# Customize these
REPO_OWNER="yourorg"
REPO_NAME="yourrepo"
TOOL_ENTRYPOINT="your-tool"  # Optional: e.g., the [project.scripts] CLI name; omit if uv infers from pyproject.toml

# Get latest release tag (strip 'v' prefix if present)
LATEST_TAG=$(curl -sSL "https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/releases/latest" \
  | grep -oE '"tag_name": "[^"]*"' \
  | head -1 \
  | cut -d '"' -f 4)
VERSION="${LATEST_TAG#v}"  # Remove leading 'v' for cleaner use

echo "Installing ${REPO_NAME} v${VERSION} via uv..."

# Install uv if not found (use jq-free parsing; install jq if preferred)
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # Source the new PATH (uv installer sets ~/.cargo/env or ~/.local/bin)
  if [[ -f ~/.cargo/env ]]; then
    source ~/.cargo/env
  elif [[ -d ~/.local/bin ]]; then
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi

# Install tool from Git tag (uv resolves pyproject.toml entrypoints automatically)
INSTALL_URL="git+https://github.com/${REPO_OWNER}/${REPO_NAME}.git@v${LATEST_TAG}"
if [[ -n "${TOOL_ENTRYPOINT}" ]]; then
  uv tool install "${INSTALL_URL}" --no-cache --force-reinstall
  uv tool run "${TOOL_ENTRYPOINT}" --version  # Verify
else
  uv tool install "${INSTALL_URL}" --no-cache --force-reinstall
fi

echo "Installed ${REPO_NAME} v${VERSION}. Run 'uv tool list' to confirm."
echo "Add ~/.local/bin or ~/.cargo/bin to PATH if needed (e.g., via uv tool update-shell)."
```


## Key Notes

- Replace placeholders (`REPO_OWNER`, etc.). The script assumes `pyproject.toml` defines `[project.scripts]` for your CLI entrypoint; `uv` installs all of them.[^2_1][^2_3]
- Robust parsing without `jq` (common on Ubuntu); add `apt install jq` check if desired.
- `--no-cache --force-reinstall` ensures clean dynamic install; remove for production.
- Post-install: Users may need `source ~/.bashrc` or `uv tool update-shell` for PATH (script handles basics).[^2_2]
- Test on clean Ubuntu 24.04 VM: builds wheel-free, uses ~100ms for installs.[^2_4]
<span style="display:none">[^2_10][^2_11][^2_12][^2_13][^2_14][^2_15][^2_16][^2_17][^2_18][^2_19][^2_20][^2_5][^2_6][^2_7][^2_8][^2_9]</span>

<div align="center">⁂</div>

[^2_1]: https://github.com/astral-sh/uv/blob/main/docs/guides/tools.md

[^2_2]: https://mac.install.guide/python/install-uv

[^2_3]: https://docs.astral.sh/uv/concepts/tools/

[^2_4]: https://github.com/astral-sh/uv

[^2_5]: https://stackoverflow.com/questions/79699897/how-to-uv-add-git-repo-with-subpackages

[^2_6]: https://www.reddit.com/r/learnpython/comments/1lmpbtu/question_uv_uvx_install_tool_from_github_repo_and/

[^2_7]: https://github.com/astral-sh/setup-uv

[^2_8]: https://pydevtools.com/handbook/how-to/how-to-add-dynamic-versioning-to-uv-projects/

[^2_9]: https://docs.s3it.uzh.ch/general/uv/

[^2_10]: https://github.com/astral-sh/uv/releases

[^2_11]: https://realpython.com/python-uv/

[^2_12]: https://blog.appsignal.com/2025/09/24/switching-from-pip-to-uv-in-python-a-comprehensive-guide.html

[^2_13]: https://thisdavej.com/packaging-python-command-line-apps-the-modern-way-with-uv/

[^2_14]: https://github.com/astral-sh/uv/issues/6533

[^2_15]: https://realpython.com/uv-vs-pip/

[^2_16]: https://github.com/astral-sh/uv/issues/6381

[^2_17]: https://docs.astral.sh/uv/concepts/projects/dependencies/

[^2_18]: https://stackoverflow.com/questions/20082935/how-to-install-pip-for-python-3-on-mac-os-x

[^2_19]: https://docs.astral.sh/uv/guides/integration/github/

[^2_20]: https://docs.astral.sh/uv/getting-started/installation/

