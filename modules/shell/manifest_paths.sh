#!/bin/bash
# Resolve a user override first, then the bundled catalog.
set -euo pipefail

resolve_manifest_path() {
  local name="$1" project_root="$2" user_dir
  user_dir="${AIHUB_MANIFEST_DIR:-}"
  if [[ -z "$user_dir" ]]; then
    user_dir=$(PYTHONPATH="$project_root${PYTHONPATH:+:$PYTHONPATH}" python3 -c 'from modules.path_utils import get_data_root; print(get_data_root() / "manifests")')
  fi
  if [[ -f "$user_dir/$name.json" ]]; then
    printf '%s\n' "$user_dir/$name.json"
  else
    printf '%s\n' "$project_root/manifests/$name.json"
  fi
}
