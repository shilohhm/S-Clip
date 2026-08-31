"""Signal sources. Every watcher is an async iterator of `Signal`."""

from .base import Watcher
from .replay import ReplayWatcher

__all__ = ["ReplayWatcher", "Watcher", "build_watcher"]


def build_watcher(config, on_error=None):  # type: ignore[no-untyped-def]
    """Pick a watcher from `config.twitter.mode`.

    Imported lazily so `replay` mode never needs the HTTP client or an API key.
    """
    mode = config.twitter.mode
    if mode == "replay":
        return ReplayWatcher(
            path=config.twitter.replay_path,
            speed=config.twitter.replay_speed,
        )
    if mode in {"poll", "stream"}:
        from .x_api import XApiWatcher

        return XApiWatcher(config.twitter, mode=mode, on_error=on_error)
    raise ValueError(f"unknown twitter.mode: {mode!r}")
