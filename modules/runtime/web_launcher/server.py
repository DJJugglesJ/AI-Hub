"""Lightweight web launcher that wraps existing shell helpers and runtime modules.

- Purpose: expose HTTP endpoints for launcher/install commands, manifest browsing,
  and prompt compilation while serving the bundled web UI.
- Assumptions: repository layout matches the documented modules/ and manifests/
  directories; Python runtime can import the runtime packages.
- Side effects: writes prompt bundles to the cache path, spawns shell helper
  processes, and reads manifest/character data for display in the UI.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from modules.action_commands import shell_action_command
from modules.path_utils import get_data_root
from modules.runtime.common.jobs import JobManager, ACTIVE
from modules.config_service import config_service
from modules.runtime.character_studio.models import CharacterCard, CharacterStudioError
from modules.runtime.character_studio.registry import CharacterCardRegistry
from modules.runtime.character_studio import dataset as character_dataset
from modules.runtime.character_studio import services as character_services
from modules.runtime.character_studio import tagging as character_tagging
from modules.runtime.character_studio import trainer as character_trainer
from modules.runtime.hardware.gpu_diagnostics import collect_gpu_diagnostics
from modules.runtime.manifest_validation import (
    MANIFEST_UPDATE_SCHEMA,
    apply_manifest_updates,
    normalize_manifest_entry,
    slugify,
    validate_manifest_items,
)
from modules.runtime.prompt_builder import compiler
from modules.runtime.prompt_builder.history import PromptHistoryStore
from modules.runtime.prompt_builder.services import UIIntegrationHooks
from modules.runtime.registry import get_tool, list_tools, load_default_tools


logger = logging.getLogger(__name__)


def _slugify(value: str) -> str:
    return slugify(value)


PAIRING_SCHEMA: Dict[str, object] = {
    "type": "object",
    "properties": {
        "model": {"type": "string"},
        "loras": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


@dataclass
class ActionSpec:
    """Describe a launcher action that shells out to existing helpers."""

    id: str
    label: str
    description: str
    command: List[str]


class WebLauncherAPI:
    """Backend helpers for the web launcher HTTP surface."""

    def __init__(
        self,
        project_root: Path,
        config_path: Optional[Path] = None,
        log_dir: Optional[Path] = None,
        history_path: Optional[Path] = None,
        data_dir: Optional[Path] = None,
    ):
        self.project_root = project_root
        self.data_dir = data_dir or get_data_root()
        self.modules_dir = project_root / "modules"
        self.shell_dir = self.modules_dir / "shell"
        self.bundled_manifests = project_root / "manifests"
        self.manifest_dir = self.data_dir / "manifests"
        self.config_path = config_path or Path(config_service.DEFAULT_CONFIG_PATH)
        self._ui_hooks = UIIntegrationHooks()
        self._card_registry = CharacterCardRegistry()
        self._action_map: Dict[str, ActionSpec] = self._build_action_map()
        self._log_dir = log_dir or self.data_dir / "web_launcher/logs"
        self._history_path = history_path or self.data_dir / "web_launcher/selection_history.json"
        self._prompt_history = PromptHistoryStore()
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs = JobManager(self.data_dir / "jobs")
        self._lock = threading.RLock()
        load_default_tools()

    def _build_action_map(self) -> Dict[str, ActionSpec]:
        actions: Iterable[Tuple[str, str, str, str]] = (
            ("run_webui", "Run Stable Diffusion WebUI", "Launch the Stable Diffusion WebUI stack", "run_webui.sh"),
            ("run_kobold", "Launch KoboldAI", "Start KoboldAI with existing models", "run_kobold.sh"),
            ("run_sillytavern", "Launch SillyTavern", "Start SillyTavern with configured backends", "run_sillytavern.sh"),
            ("install_loras", "Install or Update LoRAs", "Download curated or CivitAI LoRAs", "install_loras.sh"),
            ("install_models", "Install or Update Models", "Install curated or Hugging Face models", "install_models.sh"),
            ("manifest_browser", "Browse Curated Manifests", "Run the legacy manifest browser flow", "manifest_browser.sh"),
            ("artifact_maintenance", "Artifact Maintenance", "Prune caches and verify model/LoRA links", "artifact_manager.sh"),
            ("self_update", "Update Installer", "Run the built-in self-update to refresh scripts", "self_update.sh"),
        )

        action_map: Dict[str, ActionSpec] = {}
        for action_id, label, description, script_name in actions:
            action_map[action_id] = ActionSpec(
                id=action_id,
                label=label,
                description=description,
                command=shell_action_command(self.project_root, script_name),
            )
        return action_map

    def list_actions(self) -> List[Dict[str, str]]:
        return [asdict(action) for action in self._action_map.values()]

    def trigger_action(self, action_id: str) -> Dict[str, object]:
        if action_id not in self._action_map:
            raise ValueError(f"Unknown action: {action_id}")

        action = self._action_map[action_id]
        return self.jobs.submit("action", action.command, self.project_root,
                                action=action.id, env=self._job_env())

    def _job_env(self):
        return {**os.environ, "HEADLESS": "1", "AIHUB_DATA_DIR": str(self.data_dir),
                "AIHUB_MANIFEST_DIR": str(self.manifest_dir)}

    def close(self):
        self.jobs.close()

    def _manifest_path(self, name):
        override = self.manifest_dir / f"{name}.json"
        return override if override.exists() else self.bundled_manifests / f"{name}.json"

    def _load_manifest(self, name: str) -> Dict[str, object]:
        manifest_path = self._manifest_path(name)
        base_payload = {"source": None, "items": [], "errors": [], "has_errors": False}
        if not manifest_path.exists():
            message = f"Manifest {manifest_path.name} not found"
            return {**base_payload, "errors": [message], "has_errors": True}

        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            message = f"Failed to parse {manifest_path.name}: {exc}"
            logger.warning(message)
            return {**base_payload, "errors": [message], "has_errors": True}

        if not isinstance(payload, dict):
            message = f"Manifest {manifest_path.name} must be a JSON object"
            logger.warning(message)
            return {**base_payload, "errors": [message], "has_errors": True}

        errors: List[str] = []
        source = payload.get("source")
        items = payload.get("items", [])

        if not isinstance(items, list):
            message = f"Manifest {manifest_path.name} items must be a list"
            logger.warning(message)
            return {**base_payload, "source": source, "errors": [message], "has_errors": True}

        validated_items, item_errors = validate_manifest_items(items, manifest_path.name)
        errors.extend(item_errors)

        return {
            "source": source,
            "items": validated_items,
            "errors": errors,
            "has_errors": bool(errors),
        }

    def get_manifests(self) -> Dict[str, object]:
        models_manifest = self._load_manifest("models")
        loras_manifest = self._load_manifest("loras")
        errors = models_manifest.get("errors", []) + loras_manifest.get("errors", [])
        return {
            "models": models_manifest,
            "loras": loras_manifest,
            "errors": errors,
            "has_errors": bool(errors),
        }

    def list_manifest(self, manifest_type: str) -> Dict[str, object]:
        if manifest_type not in {"models", "loras"}:
            raise ValueError("Manifest type must be 'models' or 'loras'")
        manifest = self._load_manifest(manifest_type)
        manifest["type"] = manifest_type
        return manifest

    def get_manifest_item(self, manifest_type: str, item_id: str) -> Dict[str, object]:
        manifest = self.list_manifest(manifest_type)
        index = {item.get("slug") or _slugify(item.get("name", "")): item for item in manifest.get("items", [])}
        normalized = _slugify(item_id)
        if normalized not in index:
            raise ValueError(f"Manifest item '{item_id}' not found in {manifest_type}")
        return {"item": index[normalized], "type": manifest_type, "source": manifest.get("source"), "errors": manifest.get("errors", [])}

    def validate_manifest(self, manifest_type: str) -> Dict[str, object]:
        manifest = self.list_manifest(manifest_type)
        return {
            "type": manifest_type,
            "items": manifest.get("items", []),
            "errors": manifest.get("errors", []),
            "has_errors": manifest.get("has_errors", False),
            "source": manifest.get("source"),
        }

    def update_manifest_item(self, manifest_type: str, item_id: str, payload: Dict[str, object]) -> Dict[str, object]:
        with self._lock:
            return self._update_manifest_item(manifest_type, item_id, payload)

    def _update_manifest_item(self, manifest_type, item_id, payload):
        if manifest_type not in {"models", "loras"}:
            raise ValueError("Manifest type must be 'models' or 'loras'")
        errors = config_service.validate_against_schema(payload, MANIFEST_UPDATE_SCHEMA)
        if errors:
            raise ValueError("; ".join(errors))
        if not payload:
            raise ValueError("At least one field must be provided for manifest updates")

        manifest_path = self._manifest_path(manifest_type)
        if not manifest_path.exists():
            raise ValueError(f"Manifest {manifest_path.name} not found")
        try:
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to parse {manifest_path.name}: {exc}") from exc
        if not isinstance(manifest_payload, dict):
            raise ValueError(f"Manifest {manifest_path.name} must be a JSON object")

        items = manifest_payload.get("items", [])
        if not isinstance(items, list):
            raise ValueError(f"Manifest {manifest_path.name} items must be a list")

        normalized_id = _slugify(item_id)
        target_index: Optional[int] = None
        for idx, entry in enumerate(items):
            if not isinstance(entry, dict):
                continue
            candidate = entry.get("slug") or _slugify(entry.get("name", ""))
            if candidate and _slugify(candidate) == normalized_id:
                target_index = idx
                break
        if target_index is None:
            raise ValueError(f"Manifest item '{item_id}' not found in {manifest_type}")

        updates = dict(payload)
        updated_entry = apply_manifest_updates(items[target_index], updates)
        normalized_entry, issues = normalize_manifest_entry(updated_entry)
        if issues:
            detail = "; ".join(issues)
            raise ValueError(f"Updated manifest entry is invalid: {detail}")

        items[target_index] = updated_entry
        manifest_payload["items"] = items
        from modules.user_storage import write_json_with_backup
        write_json_with_backup(self.manifest_dir / f"{manifest_type}.json", manifest_payload)

        detail = self.get_manifest_item(manifest_type, normalized_entry.get("slug", item_id))
        return {
            "item": detail.get("item"),
            "type": manifest_type,
            "errors": detail.get("errors", []),
            "has_errors": detail.get("errors", []) != [],
        }

    def _tail_log(self, log_path: Path, lines: int = 20) -> str:
        if not log_path.exists():
            return ""
        if lines <= 0:
            return ""
        tail_lines: deque[str] = deque(maxlen=lines)
        with log_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                tail_lines.append(line)
        return "".join(tail_lines)

    def _append_status_event(
        self, path: Path, level: str, event: str, message: str, detail: Optional[object] = None
    ) -> None:
        payload: Dict[str, object] = {
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": level,
            "event": event,
            "message": message,
        }
        if detail is not None:
            payload["detail"] = detail

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
            handle.write("\n")

    def _load_status_events(self, path: Path, limit: int = 50) -> List[Dict[str, object]]:
        if not path or not path.exists():
            return []
        if limit <= 0:
            return []

        recent_lines: deque[str] = deque(maxlen=limit)
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    recent_lines.append(line)

        events: List[Dict[str, object]] = []
        for line in recent_lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def _load_history(self) -> List[Dict[str, object]]:
        if not self._history_path.exists():
            return []
        try:
            return json.loads(self._history_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []

    def _start_job(self, *, models, loras, script_name):
        status_path = self._log_dir / f"{uuid.uuid4().hex}.status.jsonl"
        env = {**self._job_env(), "DOWNLOAD_STATUS_FILE": str(status_path),
               "AIHUB_LOG_PATH": str(status_path.with_suffix(".log"))}
        if models:
            env["CURATED_MODEL_NAMES"] = "\n".join(models)
        if loras:
            env["CURATED_LORA_NAMES"] = "\n".join(loras)
        return self.jobs.submit("installation", shell_action_command(self.project_root, script_name),
                                self.project_root, env=env, payload={"models": models, "loras": loras,
                                "status_path": str(status_path)})

    def start_installation(self, models=None, loras=None):
        models, loras = models or [], loras or []
        for selection in (models, loras):
            if not isinstance(selection, list) or any(not isinstance(name, str) or not name.strip() for name in selection):
                raise ValueError("Selections must be lists of non-empty names")
        if not models and not loras:
            raise ValueError("At least one model or LoRA name must be provided")
        jobs = []
        if models:
            jobs.append(self._start_job(models=models, loras=[], script_name="install_models.sh"))
        if loras:
            jobs.append(self._start_job(models=[], loras=loras, script_name="install_loras.sh"))
        return jobs

    def list_installations(self):
        rendered = []
        for job in self.jobs.list():
            if job["kind"] != "installation":
                continue
            job.update(job["payload"])
            events = self._load_status_events(Path(job["status_path"]))
            job.update(events=events, log_tail=self.jobs.log_tail(job["id"]),
                       last_error=next((ev for ev in reversed(events) if ev.get("level") == "error"), None),
                       last_mirror=next((ev for ev in reversed(events) if ev.get("event") in {"mirror_selected", "offline_used"}), None))
            rendered.append(job)
        return {"jobs": rendered, "history": [job for job in rendered if job["status"] not in ACTIVE] + self._load_history()}

    def _load_config(self) -> Dict[str, object]:
        loaded = config_service.load_config(str(self.config_path), env_prefix="", overrides=[])
        if loaded.migrated:
            config_service.save_config(loaded.data, str(self.config_path))
        return loaded.data

    def _save_selection(self, selection: Dict[str, object]) -> Dict[str, object]:
        config = self._load_config()
        config_service.deep_set(config, "selection", selection)
        config_service.save_config(config, str(self.config_path))
        return selection

    def get_pairings(self) -> Dict[str, object]:
        config = self._load_config()
        selection = config.get("selection", {})
        return {
            "selection": {
                "model": selection.get("model", ""),
                "loras": selection.get("loras", []),
            },
            "manifests": {
                "models": self.list_manifest("models"),
                "loras": self.list_manifest("loras"),
            },
        }

    def update_pairings(self, payload: Dict[str, object]) -> Dict[str, object]:
        errors = config_service.validate_against_schema(payload, PAIRING_SCHEMA)
        if errors:
            raise ValueError("; ".join(errors))

        selection_model = str(payload.get("model", "")).strip()
        selection_loras = payload.get("loras", []) or []
        if not isinstance(selection_loras, list):
            raise ValueError("loras must be a list of names")

        manifests = self.get_manifests()
        model_index = {item.get("name"): item for item in manifests.get("models", {}).get("items", [])}
        lora_index = {item.get("name"): item for item in manifests.get("loras", {}).get("items", [])}

        if selection_model and selection_model not in model_index:
            raise ValueError(f"Unknown model '{selection_model}'")

        invalid_loras = [name for name in selection_loras if name not in lora_index]
        if invalid_loras:
            raise ValueError(f"Unknown LoRA entries: {', '.join(invalid_loras)}")

        unique_loras = []
        for name in selection_loras:
            if name not in unique_loras:
                unique_loras.append(name)

        selection = {"model": selection_model, "loras": unique_loras}
        saved = self._save_selection(selection)
        return {"selection": saved}

    def list_characters(self) -> List[Dict[str, object]]:
        cards: List[Dict[str, object]] = []
        for card_id in self._card_registry.list_ids():
            card = self._card_registry.find(card_id)
            if card:
                cards.append(card.to_dict())
        return cards

    def get_character(self, card_id: str) -> Dict[str, object]:
        card = self._card_registry.find(card_id)
        if not card:
            raise ValueError(f"Unknown character: {card_id}")
        return card.to_dict()

    def upsert_character(self, payload: Dict[str, object]) -> Dict[str, object]:
        card = character_services.upsert_character_card(payload)
        self._card_registry._cache.pop(card.id, None)
        return card.to_dict()

    def get_dataset_summary(self, card_id: str) -> Dict[str, object]:
        self.get_character(card_id)
        return character_dataset.get_dataset_summary(card_id)

    def list_dataset_images(self, card_id: str, subset: str) -> Dict[str, object]:
        self.get_character(card_id)
        if not isinstance(subset, str) or not subset.strip():
            raise ValueError("subset must be a non-empty string")
        normalized_subset = character_dataset.validate_subset_name(subset)
        images = character_dataset.list_subset_images(card_id, normalized_subset)
        items = []
        for image_path in images:
            caption_path = image_path.with_suffix(".txt")
            items.append(
                {
                    "image_path": str(image_path),
                    "filename": image_path.name,
                    "caption_path": str(caption_path),
                    "caption_exists": caption_path.exists(),
                }
            )
        return {"subset": normalized_subset, "items": items, "count": len(items)}

    def get_dataset_caption(self, card_id: str, payload: Dict[str, object]) -> Dict[str, object]:
        image_path = payload.get("image_path")
        if not isinstance(image_path, str) or not image_path.strip():
            raise ValueError("image_path must be provided")
        resolved = self._resolve_dataset_image(card_id, image_path)
        caption_path = resolved.with_suffix(".txt")
        caption_text = caption_path.read_text(encoding="utf-8") if caption_path.exists() else ""
        tags = character_tagging.parse_tag_text(caption_text)
        return {
            "image_path": str(resolved),
            "caption_path": str(caption_path),
            "caption": caption_text,
            "tags": tags,
            "exists": caption_path.exists(),
        }

    def edit_dataset_tags(self, card_id: str, payload: Dict[str, object]) -> Dict[str, object]:
        image_path = payload.get("image_path")
        tags = payload.get("tags", [])
        if not isinstance(image_path, str) or not image_path.strip():
            raise ValueError("image_path must be provided")
        if not isinstance(tags, list) or not all(isinstance(item, str) for item in tags):
            raise ValueError("tags must be a list of strings")
        resolved = self._resolve_dataset_image(card_id, image_path)
        caption_path = character_tagging.edit_tags_for_image(str(resolved), new_tags=tags)
        caption_text = Path(caption_path).read_text(encoding="utf-8")
        return {
            "image_path": str(resolved),
            "caption_path": caption_path,
            "caption": caption_text,
            "tags": character_tagging.parse_tag_text(caption_text),
        }

    def bulk_edit_dataset_tags(self, card_id: str, payload: Dict[str, object]) -> Dict[str, object]:
        image_paths = payload.get("image_paths", [])
        append_tags = payload.get("append_tags")
        replace_with = payload.get("replace_with")
        if not isinstance(image_paths, list) or not all(isinstance(item, str) for item in image_paths):
            raise ValueError("image_paths must be a list of strings")
        if append_tags is not None and (
            not isinstance(append_tags, list) or not all(isinstance(item, str) for item in append_tags)
        ):
            raise ValueError("append_tags must be a list of strings when provided")
        if replace_with is not None and (
            not isinstance(replace_with, list) or not all(isinstance(item, str) for item in replace_with)
        ):
            raise ValueError("replace_with must be a list of strings when provided")
        resolved_paths = [str(self._resolve_dataset_image(card_id, path)) for path in image_paths]
        caption_paths = character_tagging.bulk_edit_tags(
            resolved_paths, append_tags=append_tags, replace_with=replace_with
        )
        return {"caption_paths": caption_paths, "count": len(caption_paths)}

    def _resolve_dataset_image(self, card_id: str, image_path: str) -> Path:
        self.get_character(card_id)
        resolved = Path(image_path).expanduser().resolve()
        dataset_root = character_dataset.get_character_dataset_dir(card_id).resolve()
        if resolved.suffix.lower() not in character_dataset.IMAGE_EXTENSIONS:
            raise ValueError("image_path must reference a dataset image")
        if not resolved.exists():
            raise ValueError("image_path does not exist")
        if dataset_root not in resolved.parents:
            raise ValueError("image_path is not within the dataset root")
        return resolved

    def init_dataset(self, card_id: str) -> Dict[str, object]:
        character_dataset.create_dataset_structure(card_id)
        return self.get_dataset_summary(card_id)

    def add_dataset_images(self, card_id: str, payload: Dict[str, object]) -> Dict[str, object]:
        subset = payload.get("subset", "base")
        images = payload.get("images", [])
        if not isinstance(subset, str) or not subset.strip():
            raise ValueError("subset must be a non-empty string")
        if not isinstance(images, list) or not all(isinstance(item, str) for item in images):
            raise ValueError("images must be a list of image paths")
        normalized_subset = character_dataset.validate_subset_name(subset)
        stored = character_dataset.add_images_to_dataset(card_id, images, normalized_subset)
        return {"stored": stored, "summary": self.get_dataset_summary(card_id)}

    def generate_dataset_captions(self, card_id: str, payload: Dict[str, object]) -> Dict[str, object]:
        subset = payload.get("subset", "base")
        if not isinstance(subset, str) or not subset.strip():
            raise ValueError("subset must be a non-empty string")
        normalized_subset = character_dataset.validate_subset_name(subset)
        captions = character_dataset.generate_captions_for_dataset(card_id, normalized_subset)
        return {"captions": captions, "summary": self.get_dataset_summary(card_id)}

    def auto_tag_dataset(self, card_id: str, payload: Dict[str, object]) -> Dict[str, object]:
        subset = payload.get("subset", "base")
        tagger_cmd = payload.get("tagger_cmd")
        extra_tags = payload.get("extra_tags")
        if not isinstance(subset, str) or not subset.strip():
            raise ValueError("subset must be a non-empty string")
        if tagger_cmd is not None and not isinstance(tagger_cmd, str):
            raise ValueError("tagger_cmd must be a string when provided")
        if extra_tags is not None:
            if not isinstance(extra_tags, list) or not all(isinstance(item, str) for item in extra_tags):
                raise ValueError("extra_tags must be a list of strings")
        normalized_subset = character_dataset.validate_subset_name(subset)
        captions = character_tagging.auto_tag_images(
            card_id,
            normalized_subset,
            tagger_cmd=tagger_cmd,
            extra_tags=extra_tags,
        )
        return {"captions": captions, "summary": self.get_dataset_summary(card_id)}

    def export_training_pack(self, card_id: str) -> Dict[str, object]:
        self.get_character(card_id)
        try:
            archive_path = character_trainer.export_training_pack(card_id)
        except Exception as exc:
            raise CharacterStudioError(
                f"Training pack export failed for {card_id}.",
                context={"errors": [str(exc)]},
            ) from exc

        pack_dir = character_dataset.get_character_dataset_dir(card_id) / "training_pack"
        config_path = pack_dir / "training_config.json"
        return {
            "status": "exported",
            "paths": {"archive": str(archive_path), "config": str(config_path)},
            "errors": [],
        }

    def run_lora_training(self, card_id: str) -> Dict[str, object]:
        self.get_character(card_id)
        dataset_dir = character_dataset.get_character_dataset_dir(card_id)
        config_path = dataset_dir / "training_config.json"
        trainer_cmd = os.getenv("CHAR_STUDIO_TRAINER_CMD")
        try:
            output_path = character_trainer.run_lora_training(card_id)
        except Exception as exc:
            raise CharacterStudioError(
                f"Trainer run failed for {card_id}.",
                context={"errors": [str(exc)]},
            ) from exc

        if not trainer_cmd:
            status = "config_written"
        elif output_path:
            status = "completed"
        else:
            status = "completed_no_output"

        return {
            "status": status,
            "paths": {"config": str(config_path), "output": output_path or ""},
            "errors": [],
        }

    def list_tools(self) -> Dict[str, object]:
        tools = [tool.to_dict() for tool in list_tools()]
        available = [tool for tool in tools if tool.get("available")]
        return {"items": tools, "available_count": len(available), "total": len(tools)}

    def create_task(self, tool_id: str, payload: Dict[str, object]) -> Dict[str, object]:
        tool = get_tool(tool_id)
        if not tool:
            raise ValueError(f"Unknown tool: {tool_id}")
        if not tool.available:
            raise ValueError(tool.availability_error or f"Tool {tool_id} is unavailable")

        if not isinstance(payload, dict):
            raise ValueError("Task payload must be an object")
        return self.jobs.submit(tool_id, [sys.executable, "-m", tool.entrypoint], self.project_root,
                                payload=payload, env=self._job_env(), parse_result=True)

    def list_tasks(self):
        return {"items": self.jobs.list()}

    def compile_prompt(self, scene_json: Dict[str, object], feedback: Optional[str] = None) -> Dict[str, object]:
        if not isinstance(scene_json, dict):
            raise ValueError("scene must be a JSON object")
        if feedback is not None and not isinstance(feedback, str):
            raise ValueError("feedback must be a string when provided")

        scene = compiler.parse_scene_description(scene_json)
        if feedback:
            scene = compiler.apply_feedback_to_scene(asdict(scene), feedback)
            scene = compiler.parse_scene_description(scene)
        assembly = compiler.build_prompt_from_scene(asdict(scene))
        assembly_payload = assembly.to_payload()
        published = self._ui_hooks.publish_prompt(assembly)
        history_entry = self._prompt_history.add_entry(
            asdict(scene),
            assembly_payload,
            metadata={"published": published},
        )
        return {"assembly": assembly_payload, "published": published, "history": history_entry.to_dict()}

    def compile_guided_prompt(self, scene_json: Dict[str, object]) -> Dict[str, object]:
        if not isinstance(scene_json, dict):
            raise ValueError("scene must be a JSON object")
        scene = compiler.parse_scene_description(scene_json)
        assembly = compiler.build_prompt_from_scene(asdict(scene))
        assembly_payload = assembly.to_payload()
        published = self._ui_hooks.publish_prompt(assembly)
        history_entry = self._prompt_history.add_entry(
            asdict(scene),
            assembly_payload,
            metadata={"published": published},
        )
        return {"assembly": assembly_payload, "published": published, "history": history_entry.to_dict()}

    def compile_quick_prompt(self, payload: Dict[str, object]) -> Dict[str, object]:
        scene = compiler.build_scene_from_quick_prompt(payload)
        assembly = compiler.build_prompt_from_scene(asdict(scene))
        assembly_payload = assembly.to_payload()
        published = self._ui_hooks.publish_prompt(assembly)
        history_entry = self._prompt_history.add_entry(
            asdict(scene),
            assembly_payload,
            metadata={"published": published},
        )
        return {"scene": asdict(scene), "assembly": assembly_payload, "published": published, "history": history_entry.to_dict()}

    def apply_feedback(self, scene_json: Dict[str, object], feedback: str) -> Dict[str, object]:
        if not isinstance(scene_json, dict):
            raise ValueError("scene must be a JSON object")
        if not isinstance(feedback, str):
            raise ValueError("feedback must be a string")
        scene = compiler.parse_scene_description(scene_json)
        updated = compiler.apply_feedback_to_scene(scene, feedback)
        if isinstance(updated, compiler.Error):
            raise ValueError(updated.error)
        return {"scene": asdict(updated)}

    def batch_tag_images(self, payload: Dict[str, object]) -> Dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        card_payload = payload.get("card")
        if not isinstance(card_payload, dict):
            raise ValueError("card must be a JSON object")
        image_contexts = payload.get("image_contexts", [])
        if not isinstance(image_contexts, list):
            raise ValueError("image_contexts must be a list")

        card = CharacterCard.from_dict(card_payload)
        card.normalize_triggers()
        card.validate()
        return character_tagging.batch_tag_images(card, image_contexts)

    def list_prompt_history(self) -> Dict[str, object]:
        entries = [entry.to_dict() for entry in self._prompt_history.list_entries()]
        favorites = [entry.to_dict() for entry in self._prompt_history.list_favorites()]
        return {"items": entries, "favorites": favorites}

    def set_prompt_favorite(self, entry_id: str, favorite: bool) -> Dict[str, object]:
        entry = self._prompt_history.set_favorite(entry_id, favorite)
        return {"item": entry.to_dict()}

    def status(self) -> Dict[str, object]:
        manifests = self.get_manifests()
        from modules.user_storage import legacy_locations
        pending = any(path.exists() and any(path.rglob("card.json" if name == "character_cards" else "*"))
                      for name, path in legacy_locations(self.project_root).items() if name != "manifests")
        return {
            "migration_required": pending and not (self.data_dir / "migration.json").exists(),
            "actions": self.list_actions(),
            "manifest_counts": {
                "models": len(manifests.get("models", {}).get("items", [])),
                "loras": len(manifests.get("loras", {}).get("items", [])),
            },
            "characters": len(self.list_characters()),
            "tools": self.list_tools(),
        }

    def gpu_diagnostics(self) -> Dict[str, object]:
        script_path = self.shell_dir / "gpu_diagnostics.sh"
        env = {**os.environ, "HEADLESS": "1"}
        if script_path.exists():
            try:
                result = subprocess.run(
                    ["bash", str(script_path)],
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=self.project_root,
                    env=env,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return json.loads(result.stdout)
                logger.warning("GPU diagnostics helper returned %s", result.returncode)
            except json.JSONDecodeError:
                logger.warning("Failed to decode GPU diagnostics JSON; falling back to runtime collector")
            except OSError as exc:
                logger.warning("GPU diagnostics helper invocation failed: %s", exc)
        return collect_gpu_diagnostics()


class LauncherRequestHandler(SimpleHTTPRequestHandler):
    """Serve static assets and JSON APIs for the web launcher."""

    def __init__(self, *args, api: WebLauncherAPI, static_dir: Path, auth_token: Optional[str], **kwargs):
        self.api = api
        self.auth_token = auth_token
        super().__init__(*args, directory=str(static_dir), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api_get(parsed.path)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api_post(parsed.path)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unsupported POST path")

    def _is_authorized(self) -> bool:
        if not self.auth_token:
            return True
        expected = f"Bearer {self.auth_token}"
        header = self.headers.get("Authorization", "")
        alt_header = self.headers.get("X-AIHUB-TOKEN", "")
        return header == expected or alt_header == self.auth_token

    def _require_auth(self) -> bool:
        if self._is_authorized():
            return True
        self._send_json({"error": "Unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
        return False

    def _read_json_body(self) -> Dict:
        length_header = self.headers.get("Content-Length", "0")
        content_length = int(length_header) if length_header.isdigit() else 0
        raw_body = self.rfile.read(content_length) if content_length else b""
        if not raw_body:
            return {}
        try:
            return json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON body") from exc

    def _send_json(self, payload: Dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        response = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def _handle_api_get(self, path: str) -> None:
        if not self._require_auth():
            return
        try:
            if path.startswith("/api/manifests/"):
                _, _, manifest_type, *rest = path.strip("/").split("/")
                if rest:
                    self._send_json(self.api.get_manifest_item(manifest_type, rest[0]))
                else:
                    self._send_json(self.api.list_manifest(manifest_type))
                return
            if path == "/api/status":
                self._send_json(self.api.status())
            elif path == "/api/manifests":
                self._send_json(self.api.get_manifests())
            elif path == "/api/characters":
                self._send_json({"items": self.api.list_characters()})
            elif path.startswith("/api/characters/") and "/dataset/images" in path:
                prefix = "/api/characters/"
                remainder = path[len(prefix) :]
                parts = remainder.split("/")
                if len(parts) < 3 or parts[1] != "dataset" or parts[2] != "images":
                    raise ValueError("Invalid dataset images endpoint")
                card_id = parts[0]
                subset = unquote(parts[3]) if len(parts) > 3 else "base"
                self._send_json(self.api.list_dataset_images(card_id, subset))
            elif path.startswith("/api/characters/") and path.endswith("/dataset"):
                card_id = path[len("/api/characters/") : -len("/dataset")].strip("/")
                if not card_id:
                    self.send_error(HTTPStatus.NOT_FOUND, "Character id required")
                    return
                self._send_json({"item": self.api.get_dataset_summary(card_id)})
            elif path.startswith("/api/characters/"):
                _, _, card_id = path.partition("/api/characters/")
                if not card_id:
                    self.send_error(HTTPStatus.NOT_FOUND, "Character id required")
                    return
                self._send_json({"item": self.api.get_character(card_id)})
            elif path == "/api/actions":
                self._send_json({"items": self.api.list_actions()})
            elif path == "/api/installations":
                self._send_json(self.api.list_installations())
            elif path == "/api/tools":
                self._send_json(self.api.list_tools())
            elif path == "/api/tasks":
                self._send_json(self.api.list_tasks())
            elif path.startswith("/api/jobs/"):
                job_id = path.split("/")[3]
                job = self.api.jobs.get(job_id)
                job["log_tail"] = self.api.jobs.log_tail(job_id)
                self._send_json({"job": job})
            elif path in {"/api/hardware/gpu", "/api/hardware/gpu/diagnostics"}:
                self._send_json(self.api.gpu_diagnostics())
            elif path == "/api/pairings":
                self._send_json(self.api.get_pairings())
            elif path == "/api/prompt/history":
                self._send_json(self.api.list_prompt_history())
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")
        except Exception as exc:  # pragma: no cover - defensive routing guard
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_api_post(self, path: str) -> None:
        if not self._require_auth():
            return
        try:
            if path == "/api/storage/migrate":
                from modules.user_storage import migrate_legacy_data
                self._send_json(migrate_legacy_data(self.api.project_root, self.api.data_dir))
            elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = path.split("/")[3]
                self._send_json({"job": self.api.jobs.cancel(job_id)})
            elif path == "/api/actions":
                payload = self._read_json_body()
                action_id = payload.get("action")
                result = self.api.trigger_action(action_id)
                self._send_json(result, status=HTTPStatus.ACCEPTED)
            elif path == "/api/prompt/compile":
                payload = self._read_json_body()
                scene_payload = payload.get("scene", payload)
                feedback = payload.get("feedback")
                result = self.api.compile_prompt(scene_payload, feedback)
                self._send_json(result)
            elif path == "/api/prompt/guided":
                payload = self._read_json_body()
                scene_payload = payload.get("scene", payload)
                result = self.api.compile_guided_prompt(scene_payload)
                self._send_json(result)
            elif path == "/api/prompt/quick":
                payload = self._read_json_body()
                result = self.api.compile_quick_prompt(payload)
                self._send_json(result)
            elif path == "/api/prompt/feedback":
                payload = self._read_json_body()
                scene_payload = payload.get("scene", payload)
                feedback = payload.get("feedback", "")
                result = self.api.apply_feedback(scene_payload, feedback)
                self._send_json(result)
            elif path == "/api/characters/tags/batch":
                payload = self._read_json_body()
                result = self.api.batch_tag_images(payload)
                self._send_json(result)
            elif path == "/api/prompt/history/favorite":
                payload = self._read_json_body()
                entry_id = payload.get("entry_id")
                favorite = payload.get("favorite", True)
                if not isinstance(entry_id, str) or not entry_id.strip():
                    raise ValueError("entry_id is required")
                if not isinstance(favorite, bool):
                    raise ValueError("favorite must be a boolean")
                result = self.api.set_prompt_favorite(entry_id, favorite)
                self._send_json(result)
            elif path == "/api/installations":
                payload = self._read_json_body()
                models = payload.get("models", [])
                loras = payload.get("loras", [])
                jobs = self.api.start_installation(models=models, loras=loras)
                self._send_json({"jobs": jobs}, status=HTTPStatus.ACCEPTED)
            elif path.startswith("/api/manifests/"):
                payload = self._read_json_body()
                _, _, manifest_type, *rest = path.strip("/").split("/")
                if not rest:
                    self.send_error(HTTPStatus.NOT_FOUND, "Manifest action required")
                    return
                if rest == ["validate"]:
                    result = self.api.validate_manifest(manifest_type)
                    self._send_json(result)
                    return
                if len(rest) >= 2 and rest[1] == "update":
                    item_id = rest[0]
                    result = self.api.update_manifest_item(manifest_type, item_id, payload)
                    self._send_json(result)
                    return
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown manifest action")
            elif path == "/api/tasks":
                payload = self._read_json_body()
                tool_id = payload.get("tool")
                task_payload = payload.get("payload", payload)
                task = self.api.create_task(tool_id, task_payload)
                self._send_json({"task": task}, status=HTTPStatus.ACCEPTED)
            elif path == "/api/pairings":
                payload = self._read_json_body()
                result = self.api.update_pairings(payload)
                self._send_json(result)
            elif path.startswith("/api/characters/") and "/dataset/" in path:
                prefix = "/api/characters/"
                remainder = path[len(prefix) :]
                parts = remainder.split("/")
                if len(parts) < 3 or parts[1] != "dataset":
                    raise ValueError("Invalid dataset endpoint")
                card_id = parts[0]
                action = parts[2]
                subaction = parts[3] if len(parts) > 3 else None
                payload = self._read_json_body()
                if action == "init":
                    result = self.api.init_dataset(card_id)
                    self._send_json({"item": result})
                elif action == "add-images":
                    result = self.api.add_dataset_images(card_id, payload)
                    self._send_json(result)
                elif action == "captions":
                    result = self.api.generate_dataset_captions(card_id, payload)
                    self._send_json(result)
                elif action == "auto-tag":
                    result = self.api.auto_tag_dataset(card_id, payload)
                    self._send_json(result)
                elif action == "caption":
                    result = self.api.get_dataset_caption(card_id, payload)
                    self._send_json(result)
                elif action == "tags" and subaction == "edit":
                    result = self.api.edit_dataset_tags(card_id, payload)
                    self._send_json(result)
                elif action == "tags" and subaction == "bulk":
                    result = self.api.bulk_edit_dataset_tags(card_id, payload)
                    self._send_json(result)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND, "Unknown dataset endpoint")
            elif path.startswith("/api/characters/") and "/training/" in path:
                prefix = "/api/characters/"
                remainder = path[len(prefix) :]
                parts = remainder.split("/")
                if len(parts) < 3 or parts[1] != "training":
                    raise ValueError("Invalid training endpoint")
                card_id = parts[0]
                action = parts[2]
                if action == "export":
                    result = self.api.export_training_pack(card_id)
                    self._send_json({"result": result})
                elif action == "run":
                    result = self.api.run_lora_training(card_id)
                    self._send_json({"result": result})
                else:
                    self.send_error(HTTPStatus.NOT_FOUND, "Unknown training endpoint")
            elif path == "/api/characters":
                payload = self._read_json_body()
                result = self.api.upsert_character(payload)
                self._send_json({"item": result})
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")
        except CharacterStudioError as exc:
            details = exc.context or {"errors": [str(exc)]}
            self._send_json({"error": str(exc), "details": details}, status=HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            if str(exc) == "Invalid JSON body":
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            if path == "/api/characters":
                self._send_json(
                    {"error": str(exc), "details": {"errors": [str(exc)]}},
                    status=HTTPStatus.BAD_REQUEST,
                )
            else:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - defensive routing guard
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Keep launcher output minimal; rely on action log files instead.
        return


def run_server(host: str = "127.0.0.1", port: int = 3939, auth_token: Optional[str] = None) -> None:
    """Start the threaded HTTP server for the web launcher."""

    project_root = Path(__file__).resolve().parents[3]
    static_dir = Path(__file__).parent / "static"
    api = WebLauncherAPI(project_root=project_root)

    def handler(*args, **kwargs):
        return LauncherRequestHandler(
            *args, api=api, static_dir=static_dir, auth_token=auth_token, **kwargs
        )

    try:
        server = ThreadingHTTPServer((host, port), handler)
    except Exception:
        api.close()
        raise
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token_note = " with bearer token auth" if auth_token else ""
    print(f"AI Hub web launcher running on http://{host}:{port}{token_note}")
    try:
        thread.join()
    except KeyboardInterrupt:
        print("Shutting down web launcher...")
        server.shutdown()
    finally:
        server.server_close()
        api.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Hub web launcher")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Run the web launcher server")
    serve_parser.add_argument("--host", default=os.environ.get("AIHUB_WEB_HOST", "127.0.0.1"), help="Bind host")
    serve_parser.add_argument("--port", type=int, default=int(os.environ.get("AIHUB_WEB_PORT", "3939")), help="Bind port")
    serve_parser.add_argument(
        "--auth-token",
        default=os.environ.get("AIHUB_WEB_TOKEN"),
        help="Optional bearer token required for API requests",
    )

    install_parser = subparsers.add_parser("install", help="Trigger curated installs without the UI")
    install_parser.add_argument("--models", nargs="*", default=[], help="Curated model names to install")
    install_parser.add_argument("--loras", nargs="*", default=[], help="Curated LoRA names to install")
    install_parser.add_argument("--wait", action="store_true", help="Block until installers complete")

    args = parser.parse_args()
    command = args.command or "serve"

    project_root = Path(__file__).resolve().parents[3]

    if command == "serve":
        run_server(
            host=getattr(args, "host", "127.0.0.1"),
            port=getattr(args, "port", 3939),
            auth_token=getattr(args, "auth_token", None),
        )
        return

    if command == "install":
        api = WebLauncherAPI(project_root=project_root)
        try:
            jobs = api.start_installation(models=args.models, loras=args.loras)
            print(json.dumps({"jobs": jobs}, indent=2))
            # This CLI owns the supervisor, so it must remain alive until completion.

            # Simple progress loop while blocking
            job_ids = {job["id"] for job in jobs}
            while True:
                current = api.list_installations()
                active = []
                for job in current.get("jobs", []):
                    if job["id"] not in job_ids:
                        continue
                    print(
                        f"[{job['status']}] {job['id']} models={job.get('models', [])} loras={job.get('loras', [])}\n"
                        f"Log tail:\n{job.get('log_tail', '').strip()}\n---"
                    )
                    if job["status"] in ACTIVE:
                        active.append(job)
                if not active:
                    break
                threading.Event().wait(3)
            if any(job["status"] != "succeeded" for job in current.get("jobs", []) if job["id"] in job_ids):
                raise SystemExit(1)
        finally:
            api.close()
        return

    parser.print_help()


if __name__ == "__main__":
    main()
