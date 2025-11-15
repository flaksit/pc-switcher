# Using GitHub Releases to Define Python Package Version with uv

Me:
> I'm trying to find a smooth way to organize versioning for my Python program (a small tool) that is managed with uv. The repo is on GitHub and I love GitHubs release functionality. Is it possible to just use the release functionality of GitHub to update/define the version of my python package?

Gemini 2.5 Pro:

This is a great workflow goal. The "Release → Version" flow (where the Git tag *is* the source of truth) is cleaner than manually bumping files, but `uv`'s default build backend is currently optimized for static versioning (performance-first).

To achieve this with `uv`, you have two main paths. **Path A** is the "True Dynamic" approach (smoothest, requires a specific backend config), and **Path B** is the "Action Automation" approach (keeps `uv` defaults, but adds a commit).

### Path A: The "True Dynamic" Way (Recommended)

This is the smoothest option. You don't store the version in `pyproject.toml` at all. Instead, when you create a GitHub Release (e.g., `v1.0.0`), the build tool automatically detects that tag and injects it into your package during the build.

Since `uv`'s native build backend doesn't support this yet, you simply tell `uv` to use a backend that does (like `hatchling` or `setuptools`). This doesn't stop you from using `uv` for everything else (installing, locking, running).

**1. Update `pyproject.toml`**
Replace your version and build-system sections with this configuration. I recommend `hatchling` with the `uv-dynamic-versioning` plugin as it's very compatible with `uv`.

```toml
[project]
name = "my-tool"
dynamic = ["version"] # <--- Crucial: tells uv version is dynamic
# version = "0.1.0"   # <--- Remove this line!

[build-system]
requires = ["hatchling", "uv-dynamic-versioning"]
build-backend = "hatchling.build"

[tool.uv-dynamic-versioning]
enable = true
vcs = "git"
style = "pep440"
```

**2. The Workflow**

1.  Commit your code changes (no need to touch `pyproject.toml`).
2.  Go to GitHub, draft a new Release, and tag it `v1.2.0`.
3.  GitHub Actions picks this up. When it runs `uv build`, the package version becomes `1.2.0` automatically.

**3. GitHub Action Example**
Here is a clean Action that builds and releases only when you publish a GitHub Release.

```yaml
name: Publish
on:
  release:
    types: [published]

jobs:
  pypi_publish:
    name: Upload to PyPI
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # Required to fetch tags for versioning!

      - uses: astral-sh/setup-uv@v3

      - name: Build package
        run: uv build  # uv will use hatchling to pull version from the tag

      - name: Publish package
        run: uv publish
```

-----

### Path B: The "Action Automation" Way (If you prefer static files)

If you absolutely want to keep the version number written inside `pyproject.toml` (some people prefer this for explicit clarity), you can use a GitHub Action to do the "dirty work" of updating the file for you when you release.

**1. The Workflow**

1.  You create a Release `v1.2.0` on GitHub.
2.  The Action triggers, reads the tag `v1.2.0`.
3.  It uses `uv version` to update the file.
4.  It builds and publishes.
5.  *(Optional)* It commits the changed `pyproject.toml` back to your repo (this can get messy with permissions, so I often skip this and just let the release artifact have the correct version).

**2. GitHub Action Step**
You would add this step *before* your build step:

```yaml
      - name: Sync version with Release Tag
        run: |
          # Strip 'v' prefix if present (e.g., v1.0.0 -> 1.0.0)
          VERSION=${GITHUB_REF#refs/tags/v} 
          echo "Setting version to $VERSION"
          uv version $VERSION
```

### Which one should you choose?

  * **Choose Path A (Dynamic)** if you want the modern, zero-friction experience. You never have to worry about "bumping" a file again. The tag is the only source of truth.
  * **Choose Path B (Static)** only if you have a strict requirement that `pyproject.toml` must always contain the hardcoded version number in your source code.

### Next Step

Would you like me to help you configure the `uv-dynamic-versioning` for your specific project structure (e.g., do you store your code in a `/src` folder or the root)?