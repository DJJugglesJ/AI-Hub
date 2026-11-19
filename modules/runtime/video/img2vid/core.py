"""Placeholder img2vid logic."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .models import ImageToVideoRequest, ImageToVideoResult


OUTPUT_DIR = Path.home() / ".cache/aihub/video/img2vid"


def generate_video(request: ImageToVideoRequest, task_id: Optional[str] = None) -> ImageToVideoResult:
    raise RuntimeError("No generation backend is implemented for this tool.")
