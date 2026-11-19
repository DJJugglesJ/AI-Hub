import time
from pathlib import Path
from modules.runtime.web_launcher import server


def test_voice_profiles_tool_wired(tmp_path):
    api = server.WebLauncherAPI(project_root=Path(__file__).resolve().parents[2])
    result = api.create_task("voice_profiles", {})
    assert result["status"] == "queued"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = api.jobs.get(result["id"])
        if job["status"] not in {"queued", "running"}:
            break
        time.sleep(0.02)
    assert job["status"] == "succeeded", job
    assert job["result"]["profiles"]
