"""Core text-to-speech routines (placeholder implementation)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .models import TextToSpeechRequest, TextToSpeechResult


OUTPUT_DIR = Path.home() / ".cache/aihub/audio/tts"


def synthesize_speech(request: TextToSpeechRequest, task_id: Optional[str] = None) -> TextToSpeechResult:
    raise RuntimeError("No generation backend is implemented for this tool.")
