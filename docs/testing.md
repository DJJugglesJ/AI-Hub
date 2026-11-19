# Stabilization checks

Use Python 3.11 or 3.12 for the tested development environment. CI runs both
versions on Windows and Ubuntu. These tests use synthetic data and local HTTP
servers; they do not install AI applications or download model weights.

## Windows

```powershell
. ./tools/windows.ps1
Invoke-AIHubSetup
Invoke-AIHubLint -SkipInstall
Invoke-AIHubTests -SkipInstall
```

Setup installs runtime and test dependencies into the repository's `.venv`.
Lint checks Python syntax and parses every PowerShell script. Tests include
PowerShell wrapper execution and failure propagation. POSIX installer tests are
explicitly skipped here and run on Linux CI; Windows does not emulate Linux.

The Web Launcher and CLI use the same action-command builder. On Windows they
invoke PowerShell wrappers, which prefer Git Bash and otherwise use WSL. WSL
requires an installed distribution. `AIHUB_BASH` can select a specific Bash
executable. This routing does not imply all upstream AI applications support
native Windows.

## Linux

Install Bash, Git, jq, curl/wget and Python with venv support, then run:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest
```

`pytest.ini` includes both `tests/` and runtime-local suites. Linux integration
tests exercise download integrity, mirror failover, offline bundles, headless
configuration, first-run launch, and self-update against temporary Git repos.

## Updated behavior

- Character IDs must start with an ASCII letter or digit and contain only
  letters, digits, dots, hyphens, or underscores. Trailing dots and Windows
  reserved device names are rejected. Storage lookups also reject symlinks
  escaping the configured root. Display names remain unrestricted.
- WebUI can launch without a saved prompt bundle or a successful pre-launch
  health check.
- Self-update follows the current branch's configured upstream, requires a clean
  checkout and a fast-forward, and archives the old revision before updating.
  Explicit `--headless`, `HEADLESS=1`, or `HEADLESS_UPDATE=1` requests do not
  prompt. Otherwise an interactive confirmation is required. Updates never
  reset local commits or automatically restart the running web server.

GPU inference and full upstream application installation still require separate
hardware acceptance testing.
