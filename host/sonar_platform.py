"""Operating-system differences (Windows, macOS, Linux) kept in one place."""
import os
from pathlib import Path
import subprocess
import sys


def default_output_folder():
    # D: is the lab laptop's data drive; other machines record under the home folder.
    if os.name == "nt" and Path("D:\\").exists():
        return Path("D:\\SonarCaptures")
    return Path.home() / "SonarCaptures"


def default_adapter():
    """Windows needs the adapter name; elsewhere the wired link is found automatically."""
    return "Ethernet" if os.name == "nt" else "auto"


def ui_fonts():
    """(proportional, monospace) families that exist on the running system."""
    if sys.platform == "darwin":
        return "Helvetica Neue", "Menlo"
    if os.name == "nt":
        return "Segoe UI", "Consolas"
    return "DejaVu Sans", "DejaVu Sans Mono"


def open_folder(folder):
    """Show a folder in Explorer / Finder / the desktop file manager."""
    path = str(folder)
    if os.name == "nt":
        os.startfile(path)  # noqa: S606 - local folder chosen by the user
        return
    result = subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", path],
                            capture_output=True, timeout=15, check=False)
    if result.returncode:
        raise OSError(f"Could not open {path}")
