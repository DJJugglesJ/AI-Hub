"""Durable asynchronous jobs. Only processes owned by this instance are stopped."""
import json
import os
import sqlite3
import subprocess
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from modules.process_utils import ProcessLease, process_options, stop_process_tree

ACTIVE = {"queued", "running", "cancelling"}


def now():
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.lease = ProcessLease(root / "supervisor.lock")
        self.lock = threading.RLock()
        self.processes = {}
        self.threads = []
        self.closed = False
        self.shutdown_complete = False
        self.db = sqlite3.connect(root / "jobs.sqlite3", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.commit()
        for record in self.list(limit=None):
            if record["status"] in ACTIVE:
                self._update(record["id"], status="interrupted", completed_at=now(),
                             error="Launcher restarted before completion was recorded. Check the external application before retrying.")

    def _save(self, record):
        self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?)", (record["id"], json.dumps(record)))
        self.db.commit()

    def _update(self, job_id, **changes):
        with self.lock:
            record = self.get(job_id)
            record.update(changes, updated_at=now())
            self._save(record)
            return record

    def get(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown job")
            return json.loads(row[0])

    def list(self, limit=200):
        with self.lock:
            query = "SELECT data FROM jobs ORDER BY rowid DESC"
            rows = self.db.execute(query if limit is None else query + " LIMIT ?", () if limit is None else (limit,)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def submit(self, kind, command, cwd, *, payload=None, env=None, action=None, parse_result=False):
        with self.lock:
            if self.closed:
                raise RuntimeError("Launcher is shutting down")
            self.threads = [thread for thread in self.threads if thread.is_alive()]
            job_id = uuid.uuid4().hex
            record = dict(id=job_id, kind=kind, action=action, status="queued", created_at=now(),
                          updated_at=now(), started_at=None, completed_at=None, pid=None,
                          command=list(command), payload=payload or {}, log_path=str(self.root / f"{job_id}.log"),
                          result=None, returncode=None, error=None)
            self._save(record)
            thread = threading.Thread(target=self._run, args=(job_id, cwd, env, parse_result), daemon=True)
            self.threads.append(thread)
            thread.start()
            return record

    def _run(self, job_id, cwd, env, parse_result):
        try:
            with self.lock:
                record = self.get(job_id)
                if record["status"] == "cancelling":
                    self._update(job_id, status="cancelled", completed_at=now())
                    return
                with open(record["log_path"], "w", encoding="utf-8") as log:
                    process = subprocess.Popen(record["command"], cwd=cwd, env=env or dict(os.environ),
                                               stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                               **process_options())
                self.processes[job_id] = process
                self._update(job_id, status="running", started_at=now(), pid=process.pid)
            code = process.wait()
            with self.lock:
                record = self.get(job_id)
                cancelled = record["status"] == "cancelling"
                result = None
                error = None if code == 0 else f"Process exited with code {code}. See job log."
                if parse_result and code == 0 and not cancelled:
                    try:
                        output = json.loads(Path(record["log_path"]).read_text(encoding="utf-8"))
                        if output.get("status") in {"failed", "unavailable"}:
                            error = output.get("error") or "Runtime tool failed"
                        else:
                            result = output.get("result", output)
                    except (ValueError, OSError) as exc:
                        error = f"Invalid runtime result: {exc}"
                self._update(job_id, status="cancelled" if cancelled else "failed" if error else "succeeded",
                             result=result, error=None if cancelled else error, returncode=code, completed_at=now())
                self.processes.pop(job_id, None)
        except Exception as exc:
            with self.lock:
                self.processes.pop(job_id, None)
                self._update(job_id, status="failed", error=str(exc), completed_at=now())

    def cancel(self, job_id):
        with self.lock:
            record = self.get(job_id)
            if record["status"] not in ACTIVE:
                return record
            self._update(job_id, status="cancelling")
            process = self.processes.get(job_id)
            if process:
                try:
                    stop_process_tree(process)
                except Exception as exc:
                    return self._update(job_id, status="running", error=f"Cancellation failed: {exc}")
            return self.get(job_id)

    def log_tail(self, job_id):
        record = self.get(job_id)
        try:
            with open(record["log_path"], encoding="utf-8", errors="replace") as stream:
                return "".join(deque(stream, maxlen=100))[-65536:]
        except FileNotFoundError:
            return ""

    def close(self):
        with self.lock:
            if self.shutdown_complete:
                return
            self.closed = True
            for record in self.list(limit=None):
                if record["status"] in ACTIVE:
                    self.cancel(record["id"])
        for thread in self.threads:
            thread.join(timeout=10)
        if any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("Some jobs could not be stopped; supervisor remains locked")
        self.db.close()
        self.lease.close()
        self.shutdown_complete = True
