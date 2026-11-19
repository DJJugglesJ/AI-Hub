"""AI Hub runtime; Python 3.11+ (3.11 and 3.12 tested in CI)."""
import sys

if sys.version_info < (3, 11):
    raise RuntimeError("AI Hub requires Python 3.11 or newer.")
