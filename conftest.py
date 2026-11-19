"""Isolate runtime defaults before test modules are imported."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(scope="session", autouse=True)
def isolated_runtime_defaults(tmp_path_factory):
    from modules.runtime.character_studio import models, services, dataset, trainer, card_cli
    from modules.runtime.prompt_builder import history, services as prompt_services
    root = tmp_path_factory.mktemp("runtime")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "home", classmethod(lambda cls: root))
        patch.setenv("AIHUB_CONFIG_DIR", str(root / "config"))
        patch.setenv("AIHUB_LOG_PATH", str(root / "logs" / "test.log"))
        for module in (models, services, dataset, trainer, card_cli):
            patch.setattr(module, "CARD_STORAGE_ROOT", root / "cards")
        patch.setattr(dataset, "DATASET_ROOT", root / "datasets")
        patch.setattr(history, "DEFAULT_HISTORY_PATH", root / "history.json")
        patch.setattr(prompt_services, "DEFAULT_BUNDLE_PATH", root / "prompt.json")
        yield


def pytest_collection_modifyitems(items):
    if sys.platform != "linux":
        skip = pytest.mark.skip(reason="POSIX installer integration runs on Linux CI")
        for item in items:
            if item.get_closest_marker("linux"):
                item.add_marker(skip)
