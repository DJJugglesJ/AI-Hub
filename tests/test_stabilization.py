import json
import os
import shutil
import subprocess
import sys
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from modules import action_commands
from modules.runtime.character_studio import card_cli, dataset, models, services
from modules.runtime.character_studio.registry import CharacterCardRegistry
from modules.runtime.web_launcher import server

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("card_id", ["..", "../escaped", "a/b", "a\\b", "/absolute", "C:\\escape", "C:escape", "CON", "nul.txt", "trailing.", "", "a\x00b"])
def test_character_ids_cannot_escape_storage(card_id, tmp_path, monkeypatch):
    root = tmp_path / "cards"
    monkeypatch.setattr(models, "CARD_STORAGE_ROOT", root)
    monkeypatch.setattr(card_cli, "CARD_STORAGE_ROOT", root)
    monkeypatch.setattr(dataset, "DATASET_ROOT", tmp_path / "datasets")
    for operation in (
        lambda: services.upsert_character_card({"id": card_id, "name": "Test"}, storage_root=root),
        lambda: CharacterCardRegistry(root).find(card_id),
        lambda: models.CharacterCard(id=card_id, name="Test").save(),
        lambda: models.CharacterCard.load(card_id),
        lambda: card_cli._get_card_path(card_id),
        lambda: dataset.get_character_dataset_dir(card_id),
    ):
        with pytest.raises(models.SchemaValidationError):
            operation()
    assert not root.exists()


def test_character_symlink_cannot_escape_storage(tmp_path):
    root, outside = tmp_path / "cards", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Host cannot create symlinks; Linux CI covers containment")
    with pytest.raises(models.SchemaValidationError):
        services.upsert_character_card({"id": "linked", "name": "Test"}, storage_root=root)
    assert not (outside / "card.json").exists()


def test_character_http_rejects_traversal_and_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(services, "CARD_STORAGE_ROOT", tmp_path / "cards")
    api = server.WebLauncherAPI(ROOT, log_dir=tmp_path / "logs", history_path=tmp_path / "history.json")
    handler = partial(server.LauncherRequestHandler, api=api, static_dir=ROOT / "modules/runtime/web_launcher/static", auth_token="test-token")
    http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        for headers, expected in [({}, 401), ({"Authorization": "Bearer test-token"}, 400)]:
            request = Request(f"http://127.0.0.1:{http.server_port}/api/characters", data=json.dumps({"id": "../escaped", "name": "Test"}).encode(), headers=headers)
            with pytest.raises(HTTPError) as result:
                urlopen(request, timeout=5)
            assert result.value.code == expected
        assert not (tmp_path / "escaped").exists()
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("platform_name", ["Windows", "Linux"])
def test_web_actions_and_download_jobs_use_platform_command(platform_name, tmp_path, monkeypatch):
    monkeypatch.setattr(action_commands.platform, "system", lambda: platform_name)
    api = server.WebLauncherAPI(ROOT, log_dir=tmp_path / "logs", history_path=tmp_path / "history.json")
    calls = []

    class Process:
        pid = 123
        returncode = 0

        def poll(self):
            return self.returncode

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["stdout"].close()
        return Process()

    monkeypatch.setattr(server.subprocess, "Popen", popen)
    monkeypatch.setattr(server.WebLauncherAPI, "_monitor_job", lambda *args: None)
    api.trigger_action("run_webui")
    api.start_installation(models=["Example model"])
    for (command, kwargs), script in zip(calls, ["run_webui.sh", "install_models.sh"]):
        assert command == action_commands.shell_action_command(ROOT, script)
        assert command[-1].endswith(".ps1" if platform_name == "Windows" else ".sh")
        assert kwargs["env"]["HEADLESS"] == "1"
    assert calls[1][1]["env"]["CURATED_MODEL_NAMES"] == "Example model"
    assert "DOWNLOAD_STATUS_FILE" in calls[1][1]["env"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell integration")
def test_powershell_scripts_parse_and_wrapper_preserves_failure(tmp_path):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell, "PowerShell is required for Windows tests"
    fake_bash = tmp_path / "fake bash.cmd"
    fake_bash.write_text('@echo off\necho wrapper-probe\nexit /b 7\n')
    script = tmp_path / "probe.ps1"
    # Script paths are passed as arguments, not interpolated into executable text.
    script.write_text('''param([string]$ProjectRoot)
$ErrorActionPreference = 'Stop'
Get-ChildItem $ProjectRoot -Recurse -Filter *.ps1 | Where-Object { $_.FullName -notlike "$ProjectRoot\\.venv\\*" } | ForEach-Object {
  $tokens = $null; $errors = $null
  [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$errors) | Out-Null
  if ($errors) { throw "$($_.FullName): $errors" }
}
. "$ProjectRoot/launcher/windows/common.ps1"
$code = Invoke-AIHubShellAction -ActionName 'run_webui' -ScriptName 'run_webui.sh'
exit $code
''')
    result = subprocess.run([powershell, "-NoProfile", "-File", str(script), str(ROOT)], env={**os.environ, "AIHUB_BASH": str(fake_bash), "AIHUB_CONFIG_DIR": str(tmp_path / "config"), "AIHUB_LOG_PATH": str(tmp_path / "probe.log")}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 7, result.stdout + result.stderr
    assert "wrapper-probe" in result.stdout
    assert "exited with code 7" in (tmp_path / "probe.log").read_text()
