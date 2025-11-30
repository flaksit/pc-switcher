#!/bin/bash
set -e

echo "Installing pc-switcher..."

# Check for btrfs
if ! mount | grep "on / type btrfs" > /dev/null; then
    echo "Error: Root filesystem is not btrfs. pc-switcher requires btrfs."
    exit 1
fi

# Install dependencies
if ! command -v uv &> /dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    source "$HOME/.local/bin/env" || true
fi

# Install pc-switcher
echo "Installing pc-switcher package..."
# Install from git using uv tool
# We default to the main branch, but this can be overridden by setting PC_SWITCHER_VERSION
VERSION="${PC_SWITCHER_VERSION:-main}"
REPO_URL="https://github.com/flaksit/pc-switcher-antigravity"

echo "Installing version: $VERSION from $REPO_URL"
uv tool install --force "git+$REPO_URL@$VERSION"

# Create config
CONFIG_DIR="$HOME/.config/pc-switcher"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    echo "Creating default config..."
    # Download default config from repo
    # We use the raw content from GitHub
    curl -LsSf "https://raw.githubusercontent.com/flaksit/pc-switcher-antigravity/$VERSION/default-config.yaml" -o "$CONFIG_DIR/config.yaml"
fi

echo "Installation complete!"
