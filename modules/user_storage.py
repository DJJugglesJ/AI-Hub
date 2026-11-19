"""Atomic user-file updates and explicit, non-destructive legacy imports."""
import argparse
import filecmp
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from modules.path_utils import get_data_root


def write_json_with_backup(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.parent / "backups" / f"{path.stem}-{uuid.uuid4().hex}.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
    descriptor, temporary = tempfile.mkstemp(prefix=".aihub-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def legacy_locations(project_root):
    studio = project_root / "modules/runtime/character_studio"
    return {"character_cards": studio / "character_cards", "datasets": studio / "datasets",
            "manifests": project_root / "manifests",
            "prompt_builder": Path.home() / ".cache/aihub/prompt_builder",
            "web_launcher": Path.home() / ".cache/aihub/web_launcher"}


def migrate_legacy_data(project_root: Path, destination: Path):
    """Copy missing files, refuse conflicts/symlinks, and retain every source file."""
    copies = []
    for name, source in legacy_locations(project_root).items():
        if source.is_symlink():
            raise ValueError(f"Legacy location is a symlink: {source}")
        if not source.exists():
            continue
        for file in source.rglob("*"):
            if file.is_symlink():
                raise ValueError(f"Legacy data contains a symlink: {file}")
            if not file.is_file():
                continue
            target = destination / name / file.relative_to(source)
            if destination.resolve() not in target.resolve().parents:
                raise ValueError("Migration destination escapes the data root")
            if target.exists():
                if not filecmp.cmp(file, target, shallow=False):
                    raise ValueError(f"Migration conflict; existing file preserved: {target}")
            else:
                copies.append((file, target))
    for source, target in copies:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    receipt = {"source": str(project_root), "copied_files": len(copies), "source_preserved": True}
    write_json_with_backup(destination / "migration.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description="Import legacy AI Hub user data without deleting originals")
    parser.add_argument("--from-project", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(migrate_legacy_data(args.from_project.resolve(), get_data_root()), indent=2))


if __name__ == "__main__":
    main()
