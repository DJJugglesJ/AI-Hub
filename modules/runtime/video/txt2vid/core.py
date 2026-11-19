"""Placeholder txt2vid logic."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .models import TextToVideoRequest, TextToVideoResult


OUTPUT_DIR = Path.home() / ".cache/aihub/video/txt2vid"


def generate_video(request: TextToVideoRequest, task_id: Optional[str] = None) -> TextToVideoResult:
    raise RuntimeError("No generation backend is implemented for this tool.")
