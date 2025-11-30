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
    source $HOME/.cargo/env
fi

# Install pc-switcher
echo "Installing pc-switcher package..."
# Assuming we are in the repo or installing from git
# For now, install editable from current dir
uv tool install --force .

# Create config
CONFIG_DIR="$HOME/.config/pc-switcher"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    echo "Creating default config..."
    cat > "$CONFIG_DIR/config.yaml" <<EOF
global_settings:
  log_file_level: FULL
  log_cli_level: INFO
  sync_jobs:
    user_data: true
    packages: true

btrfs_snapshots:
  subvolumes:
    - "@"
    - "@home"
EOF
fi

echo "Installation complete!"
