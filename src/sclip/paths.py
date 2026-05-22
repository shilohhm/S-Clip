"""Resolved filesystem paths for the running application.

Anything that needs to know where the package lives, where to drop clips, or
where the user-writable config lives should pull from here rather than calling
``os.path.dirname(__file__)`` ad hoc. Centralising it keeps the app working
regardless of where the user launches it from.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

from platformdirs import user_config_path, user_data_path

_APP_NAME = "S-Clip"
_APP_AUTHOR = "S-Clip"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Bundle of every directory the app needs to read or write."""

    package_root: Path
    project_root: Path
    assets_dir: Path
    styles_qss: Path
    config_dir: Path
    settings_file: Path
    clips_dir: Path
    replay_buffer_dir: Path
    log_file: Path

    def ensure_writable(self) -> None:
        """Create any directories the user will write into.

        We never auto-create read-only paths (package_root, assets_dir) — those
        ship with the app and missing entries indicate a packaging fault.
        """
        for path in (
            self.config_dir,
            self.clips_dir,
            self.replay_buffer_dir,
            self.log_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)


@cache
def app_paths() -> AppPaths:
    """Resolve every app path exactly once per process."""
    package_root = Path(__file__).resolve().parent
    project_root = package_root.parent.parent  # src/sclip/.. -> repo root

    config_dir = user_config_path(_APP_NAME, _APP_AUTHOR, roaming=True)
    data_dir = user_data_path(_APP_NAME, _APP_AUTHOR, roaming=True)

    return AppPaths(
        package_root=package_root,
        project_root=project_root,
        assets_dir=package_root / "ui" / "assets",
        styles_qss=package_root / "ui" / "styles.qss",
        config_dir=config_dir,
        settings_file=config_dir / "settings.json",
        clips_dir=data_dir / "clips",
        replay_buffer_dir=data_dir / "replay_buffer",
        log_file=data_dir / "logs" / "sclip.log",
    )
