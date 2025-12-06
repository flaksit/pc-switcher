# Testing Framework Contracts

**Feature**: 002-testing-framework

## Overview

This feature does not expose HTTP/REST/GraphQL APIs. The "contracts" for this feature are:

1. **pytest marker contract**: Tests marked with `@pytest.mark.integration` are excluded by default
2. **Environment variable contract**: Integration tests require specific environment variables
3. **Lock format**: Hetzner Server Labels on pc-switcher-pc1

These contracts are documented in:
- [data-model.md](../data-model.md) - Entity definitions and validation rules
- [research.md](../research.md) - Configuration decisions and formats

## pytest Configuration Contract

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = ["--strict-markers", "-v", "-m", "not integration"]
markers = [
    "integration: Integration tests (require VM infrastructure)",
]
```

## Environment Variable Contract

| Variable | Required For | Format |
|----------|-------------|--------|
| PC_SWITCHER_TEST_PC1_HOST | Integration tests | IPv4 address or hostname |
| PC_SWITCHER_TEST_PC2_HOST | Integration tests | IPv4 address or hostname |
| PC_SWITCHER_TEST_USER | Integration tests | Unix username (default: testuser) |
| HCLOUD_TOKEN | Infrastructure management | Hetzner API token |
| SSH_PUBLIC_KEY | VM provisioning (local) | Path to .pub file |

## CI Secrets Contract

| Secret | Required For | Description |
|--------|-------------|-------------|
| HCLOUD_TOKEN | CI integration tests | Hetzner Cloud API token |
| HETZNER_SSH_PRIVATE_KEY | CI integration tests | SSH private key for VM access |

## Lock Contract

The lock is stored as Hetzner Server Labels on the `pc-switcher-pc1` server object, which survives VM reboots and btrfs snapshot rollbacks.

**Labels**:
| Label | Value |
|-------|-------|
| `lock_holder` | CI job ID or username |
| `lock_acquired` | ISO 8601 datetime |

**Operations**:
```bash
# Check lock status
./tests/infrastructure/scripts/lock.sh "" status

# Acquire lock
./tests/infrastructure/scripts/lock.sh "$CI_JOB_ID" acquire

# Release lock
./tests/infrastructure/scripts/lock.sh "$CI_JOB_ID" release

# Manual cleanup (for stuck locks)
hcloud server remove-label pc-switcher-pc1 lock_holder
hcloud server remove-label pc-switcher-pc1 lock_acquired
```
