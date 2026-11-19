# Runtime reliability

The launcher requires Python 3.11 or later; CI tests 3.11 and 3.12 on Windows
and Ubuntu. Start scripts prefer the repository `.venv`, or `AIHUB_PYTHON`
when explicitly set. Install `requirements-dev.txt` for the pinned test stack.
These pins cover AI Hub, not the separately installed upstream AI applications.

## Persistent jobs

Actions, model installations and available runtime tools return a queued job
immediately. The Jobs panel shows running, succeeded, failed, cancelled or
interrupted states, errors and log tails. Success means the owned process exited
successfully; it is not a health guarantee for an external AI service.

Jobs and logs are stored under the data directory in `jobs/`. Only one launcher
may supervise that directory at a time. A second launcher fails clearly rather
than marking another launcher's jobs interrupted. Cancel stops the process tree
owned by this launcher. Normal shutdown cancels active jobs. Detached services
and processes inside WSL may require separate shutdown in their own environment.

After an abrupt launcher exit, previously active jobs become interrupted on the
next startup. No stale PID is killed and no job is automatically retried. Check
the external application before retrying. The install CLI stays alive until its
jobs finish; `--wait` remains accepted for compatibility.

## User storage and migration

The default data directory is `%LOCALAPPDATA%/AIHub/data` on Windows and
`${XDG_DATA_HOME:-~/.local/share}/aihub` on Linux. `AIHUB_DATA_DIR` overrides it.
Use an absolute path when sharing it between launcher processes. Character cards,
datasets, prompt history, prompt bundles, job records and manifest overrides use
this directory. Existing installer configuration and log overrides still apply.

Manifest edits create user overrides; bundled catalogs remain unchanged. Shell
installers use those overrides too. `AIHUB_MANIFEST_DIR` can select an explicit
catalog directory. JSON card and manifest updates keep backups and replace files
atomically.

Legacy files are not moved automatically. Use the import banner in the Web
Launcher, or run from the new checkout:

```bash
python -m modules.user_storage --from-project /path/to/old/AI-Hub
```

The importer copies old character cards, datasets, catalogs, and the user's old
prompt/launcher cache. Originals remain in place. It refuses symlinks and
conflicting files before copying, and identical files make repeat imports safe.
Resolve conflicts manually after comparing both copies. `migration.json` records
the import. Keep the old files until you have checked the imported content.

## Remaining P1 work

- Select and hardware-test pinned upstream application revisions and dependency
  profiles before claiming reproducible GPU installations.
- Add real media backends before enabling TTS, ASR or video generation. Their
  API and CLI currently return explicit unavailable errors without fake output.
- Validate the complete UI and GPU workflows on supported hardware, including
  cancellation across WSL and external services.
