from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[2]))

from modules.runtime.web_launcher import server  # noqa: E402


def test_installation_status_events_exposed(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    api = server.WebLauncherAPI(
        project_root=project_root,
        config_path=tmp_path / "config.yaml",
        log_dir=tmp_path / "logs",
        history_path=tmp_path / "history.json",
    )

    status_path = tmp_path / "logs" / "job.status.jsonl"
    log_path = tmp_path / "logs" / "job.log"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("line1\nline2\n", encoding="utf-8")

    api._append_status_event(status_path, "info", "mirror_healthy", "Mirror ready", {"mirror": "primary"})
    api._append_status_event(status_path, "error", "checksum_failed", "Checksum mismatch", "deadbeef")

    api.jobs._save({"id": "job-1", "kind": "installation", "status": "succeeded",
                    "log_path": str(log_path), "payload": {"models": ["alpha"], "loras": ["beta"], "status_path": str(status_path)}})

    payload = api.list_installations()
    events = payload["jobs"][0]["events"]

    assert events[-1]["event"] == "checksum_failed"
    assert {evt["event"] for evt in events} >= {"mirror_healthy", "checksum_failed"}
    assert "line2" in payload["jobs"][0]["log_tail"]
