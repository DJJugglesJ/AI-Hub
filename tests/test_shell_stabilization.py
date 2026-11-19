import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.linux
ROOT = Path(__file__).resolve().parents[1]


def run(command, cwd, env=None, check=True):
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=check, timeout=30)


@pytest.mark.parametrize("health_exit", [0, 1])
def test_webui_launch_without_prompt_bundle(tmp_path, health_exit):
    shell = tmp_path / "shell"
    shell.mkdir()
    shutil.copyfile(ROOT / "modules/shell/run_webui.sh", shell / "run_webui.sh")
    (shell / "logging.sh").write_text('log_msg() { :; }\nlog_event() { :; }\nmetrics_record_start() { :; }\n')
    health = shell / "health_webui.sh"
    health.write_text(f'#!/bin/bash\nexit {health_exit}\n')
    webui = tmp_path / "AI/WebUI"
    venv = webui / "venv/bin"
    venv.mkdir(parents=True)
    (venv / "activate").write_text('export PATH="$HOME/AI/WebUI/venv/bin:$PATH"\n')
    python = venv / "python"
    python.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$HOME/launched.txt"\n')
    python.chmod(0o755)
    result = run(["bash", str(shell / "run_webui.sh")], tmp_path, env={**os.environ, "HOME": str(tmp_path), "HEADLESS": "1", "PROMPT_BUNDLE_PATH": str(tmp_path / "missing.json")}, check=False)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "launched.txt").read_text().splitlines()[0] == "launch.py"


@pytest.fixture
def update_checkout(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    run(["git", "init", "-b", "main"], upstream)
    run(["git", "config", "user.email", "test@example.invalid"], upstream)
    run(["git", "config", "user.name", "Test"], upstream)
    shell = upstream / "modules/shell"
    shell.mkdir(parents=True)
    for name in ("self_update.sh", "logging.sh"):
        shutil.copyfile(ROOT / "modules/shell" / name, shell / name)
    (upstream / "README.md").write_text("before\n")
    run(["git", "add", "."], upstream)
    run(["git", "commit", "-m", "initial"], upstream)
    checkout = tmp_path / "checkout"
    run(["git", "clone", str(upstream), str(checkout)], tmp_path)
    (upstream / "README.md").write_text("after\n")
    run(["git", "commit", "-am", "update"], upstream)
    env = {**os.environ, "HEADLESS": "1", "AIHUB_CONFIG_DIR": str(tmp_path / "config"), "AIHUB_LOG_PATH": str(tmp_path / "update.log"), "AIHUB_LOG_DIR": str(tmp_path / "logs"), "AIHUB_BACKUP_DIR": str(tmp_path / "backups")}
    return checkout, upstream, env


def test_update_fast_forwards_with_backup_and_no_stdin(update_checkout, tmp_path):
    checkout, upstream, env = update_checkout
    result = run(["bash", "modules/shell/self_update.sh"], checkout, env=env, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (checkout / "README.md").read_text() == "after\n"
    assert list((tmp_path / "backups").glob("*.tar.gz"))
    assert run(["git", "rev-parse", "HEAD"], checkout).stdout == run(["git", "rev-parse", "HEAD"], upstream).stdout


def test_update_refuses_dirty_worktree_without_destroying_changes(update_checkout):
    checkout, _, env = update_checkout
    (checkout / "README.md").write_text("my changes\n")
    before = run(["git", "rev-parse", "HEAD"], checkout).stdout
    result = run(["bash", "modules/shell/self_update.sh", "--headless"], checkout, env=env, check=False)
    assert result.returncode != 0
    assert "local changes" in result.stderr
    assert (checkout / "README.md").read_text() == "my changes\n"
    assert run(["git", "rev-parse", "HEAD"], checkout).stdout == before


def test_update_without_arguments_or_headless_env_has_clear_error(update_checkout):
    checkout, _, env = update_checkout
    env.pop("HEADLESS", None)
    env.pop("HEADLESS_UPDATE", None)
    env.pop("DISPLAY", None)
    result = run(["bash", "modules/shell/self_update.sh"], checkout, env=env, check=False)
    assert result.returncode != 0
    assert "Noninteractive updates require" in result.stderr
    assert "unbound variable" not in result.stderr


def test_update_refuses_divergence_without_reset(update_checkout):
    checkout, _, env = update_checkout
    run(["git", "config", "user.email", "test@example.invalid"], checkout)
    run(["git", "config", "user.name", "Test"], checkout)
    (checkout / "local.txt").write_text("keep me\n")
    run(["git", "add", "local.txt"], checkout)
    run(["git", "commit", "-m", "local work"], checkout)
    before = run(["git", "rev-parse", "HEAD"], checkout).stdout
    result = run(["bash", "modules/shell/self_update.sh"], checkout, env=env, check=False)
    assert result.returncode != 0
    assert "not a fast-forward" in result.stderr
    assert run(["git", "rev-parse", "HEAD"], checkout).stdout == before
    assert (checkout / "local.txt").read_text() == "keep me\n"
