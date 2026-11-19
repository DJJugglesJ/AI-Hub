import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from modules.runtime.common.jobs import JobManager, ACTIVE
from modules.runtime.web_launcher.server import WebLauncherAPI
from modules.user_storage import migrate_legacy_data, write_json_with_backup

ROOT = Path(__file__).resolve().parents[1]


def wait_for(manager, job_id, states):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        record = manager.get(job_id)
        if record["status"] in states:
            return record
        time.sleep(0.02)
    pytest.fail(f"Timed out: {manager.get(job_id)}")


def test_jobs_record_success_failure_spawn_error_and_logs(tmp_path):
    manager = JobManager(tmp_path / "jobs")
    for command, expected in [([sys.executable, "-c", "print('actual output')"], "succeeded"),
                              ([sys.executable, "-c", "raise SystemExit(7)"], "failed"),
                              ([str(tmp_path / "missing-program")], "failed")]:
        job = manager.submit("test", command, ROOT)
        assert job["status"] == "queued"
        result = wait_for(manager, job["id"], {expected})
        assert result["completed_at"]
        if expected == "failed":
            assert result["error"]
        else:
            assert "actual output" in manager.log_tail(job["id"])


def test_cancel_stops_running_process_and_keeps_terminal_result(tmp_path):
    manager = JobManager(tmp_path / "jobs")
    job = manager.submit("test", [sys.executable, "-c", "import time; time.sleep(60)"], ROOT)
    wait_for(manager, job["id"], {"running"})
    process = manager.processes[job["id"]]
    manager.cancel(job["id"])
    result = wait_for(manager, job["id"], {"cancelled"})
    assert process.poll() is not None
    assert manager.cancel(job["id"]) == result


def test_restart_keeps_completed_jobs_and_marks_abandoned_jobs_interrupted(tmp_path):
    root = tmp_path / "jobs"
    manager = JobManager(root)
    job = manager.submit("test", [sys.executable, "-c", "print('saved')"], ROOT)
    wait_for(manager, job["id"], {"succeeded"})
    manager.close()
    # Model abrupt loss of the old supervisor without leaving a real orphan process.
    with sqlite3.connect(root / "jobs.sqlite3") as db:
        db.execute("INSERT INTO jobs VALUES (?, ?)", ("abandoned", json.dumps({"id": "abandoned", "status": "running"})))
    restarted = JobManager(root)
    assert restarted.get(job["id"])["status"] == "succeeded"
    assert restarted.get("abandoned")["status"] == "interrupted"
    assert "before retrying" in restarted.get("abandoned")["error"]


def test_second_supervisor_cannot_take_over_live_jobs(tmp_path):
    root = tmp_path / "jobs"
    JobManager(root)
    with pytest.raises(RuntimeError, match="Another launcher"):
        JobManager(root)


@pytest.mark.parametrize("tool", ["tts", "asr", "img2vid", "txt2vid"])
def test_unimplemented_media_is_unavailable_in_api_and_cli(tool, tmp_path):
    api = WebLauncherAPI(ROOT)
    spec = next(item for item in api.list_tools()["items"] if item["id"] == tool)
    assert not spec["available"]
    with pytest.raises(ValueError, match="No generation backend"):
        api.create_task(tool, {})
    payloads = {"tts": {"text": "hello"}, "asr": {"source_path": "sample.wav"},
                "img2vid": {"image_path": "sample.png"}, "txt2vid": {"prompt": "hello"}}
    result = subprocess.run([sys.executable, "-m", spec["entrypoint"], "--payload-json", json.dumps(payloads[tool])],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "No generation backend" in result.stdout
    assert not list(tmp_path.rglob("*.mp4"))


def test_manifest_edit_keeps_bundled_catalog_and_backs_up_override(tmp_path):
    api = WebLauncherAPI(ROOT)
    source = ROOT / "manifests/models.json"
    original = source.read_bytes()
    item = api.list_manifest("models")["items"][0]
    api.update_manifest_item("models", item["slug"], {"tags": ["first"]})
    api.update_manifest_item("models", item["slug"], {"tags": ["second"]})
    assert source.read_bytes() == original
    assert api.get_manifest_item("models", item["slug"])["item"]["tags"] == ["second"]
    backup = next((api.manifest_dir / "backups").glob("*.json"))
    assert json.loads(backup.read_text())["items"][0]["tags"] == ["first"]


def test_legacy_import_is_repeatable_preserves_sources_and_refuses_conflicts(tmp_path):
    project, destination = tmp_path / "old", tmp_path / "data"
    source = project / "modules/runtime/character_studio/character_cards/hero/card.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"id": "hero"}')
    first = migrate_legacy_data(project, destination)
    assert first["copied_files"] == 1
    assert source.exists()
    assert migrate_legacy_data(project, destination)["copied_files"] == 0
    target = destination / "character_cards/hero/card.json"
    target.write_text("newer user data")
    with pytest.raises(ValueError, match="conflict"):
        migrate_legacy_data(project, destination)
    assert target.read_text() == "newer user data"


def test_atomic_write_failure_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "data.json"
    write_json_with_backup(path, {"version": 1})
    import modules.user_storage as storage
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(storage.os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        write_json_with_backup(path, {"version": 2})
    assert json.loads(path.read_text()) == {"version": 1}
