"""Placeholder ASR implementation."""
from __future__ import annotations

from pathlib import Path

from .models import ASRRequest, ASRResult


def transcribe(request: ASRRequest) -> ASRResult:
    raise RuntimeError("No generation backend is implemented for this tool.")
