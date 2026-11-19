"""Platform-specific process commands shared by launcher surfaces."""
import platform
import shutil
from pathlib import Path
from typing import List


def shell_action_command(project_root: Path, script_name: str) -> List[str]:
    """Use the Windows wrapper or the POSIX helper without importing a UI."""
    if platform.system() == "Windows":
        wrapper = project_root / "launcher" / "windows" / (Path(script_name).stem + ".ps1")
        return [shutil.which("pwsh") or "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)]
    return ["bash", str(project_root / "modules" / "shell" / script_name)]
