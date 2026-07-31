"""Notice when a newer S-Clip has been released, and say so.

S-Clip 2.0.0 shipped with a software-encoder default that dropped one frame in
five, and saves that took seven seconds. Both were fixed within days. Anyone
who installed the earlier build had no way to learn any of that had happened,
which is a poor reward for having installed it early.

This module answers one question - is there a newer release than the one
running - and nothing more. Deliberately:

*This is not an updater.* It downloads no code and runs none. The user is shown
a link and installs the new version themselves, exactly as they installed this
one. That restraint is the point rather than a missing feature. S-Clip's
installer is not code-signed, so an automatic update would have to download an
unsigned executable and run it, which trains the user to click through the
SmartScreen warning that is currently their only protection, and turns the
project's release pipeline into a code-execution channel on every machine that
has ever run it. Doing that properly needs a signed update manifest and a key
to sign it with. Until then, linking to a release page is the honest option.

This is also S-Clip's only outbound network connection. The check is off a
single unauthenticated GitHub API call, it is throttled to once a day, it can
be turned off in Settings, and it says so on the About page. Nothing about the
user or their machine is transmitted beyond what any HTTP request discloses.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The releases endpoint for this project. ``/latest`` deliberately excludes
# pre-releases and drafts, so tagging a beta never prompts anybody.
_RELEASES_API: str = "https://api.github.com/repos/shilohhm/S-Clip/releases/latest"

# Where a human is sent. The API URL is not usable in a browser.
_RELEASES_PAGE: str = "https://github.com/shilohhm/S-Clip/releases/latest"

# GitHub rejects requests without a User-Agent, and asks that it identify the
# caller. Naming the project is more courteous than impersonating a browser.
_USER_AGENT: str = "S-Clip-update-check"

# Short enough that a black-holed connection cannot hold up the worker for
# long. The check is best-effort; missing one costs nothing.
_TIMEOUT_SECONDS: float = 6.0

# One check a day. Frequent enough that a fix is noticed within a day of
# release, rare enough to stay far below GitHub's unauthenticated rate limit
# and to be defensible as a background network call.
_CHECK_INTERVAL_SECONDS: float = 24 * 60 * 60

# Guards against a hostile or broken endpoint returning something enormous.
_MAX_RESPONSE_BYTES: int = 64 * 1024

# A leading "v" is conventional on tags and carries no meaning.
_VERSION_PATTERN = re.compile(r"^v?(\d+(?:\.\d+)*)")


@dataclass(frozen=True, slots=True)
class Release:
    """A published release newer than the one running."""

    version: str
    url: str

    @property
    def headline(self) -> str:
        """The one line shown to the user."""
        return f"S-Clip {self.version} is available."


def parse_version(text: str) -> tuple[int, ...] | None:
    """Turn ``v2.1.0`` into ``(2, 1, 0)``, or ``None`` if it is not a version.

    Anything after the numeric run is ignored, so a pre-release suffix compares
    equal to its base version. That is the conservative direction: it can only
    ever suppress a prompt, never invent one.
    """
    match = _VERSION_PATTERN.match(text.strip())
    if match is None:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:  # pragma: no cover - the pattern admits digits only
        return None


def is_newer(candidate: str, current: str) -> bool:
    """Return ``True`` when ``candidate`` is a later version than ``current``.

    Unparseable input on either side returns ``False``. A version check that
    cannot be trusted should stay quiet rather than nag on a guess.
    """
    new = parse_version(candidate)
    old = parse_version(current)
    if new is None or old is None:
        return False
    # Compare on equal length so 2.1 and 2.1.0 are the same release.
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


def _read_state(path: Path) -> dict[str, Any]:
    """Read the throttle record, treating any problem as "never checked"."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("Update-check state file is not valid JSON; ignoring it")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_state(path: Path, checked_at: float) -> None:
    """Record when the last check happened. Best-effort by design."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked_at": checked_at}), encoding="utf-8")
    except OSError as exc:
        # A read-only config directory should not turn into a visible error;
        # the cost is only that the next launch checks again.
        logger.debug("Could not record the update-check time: %s", exc)


def _is_due(state: dict[str, Any], now: float) -> bool:
    """Return ``True`` when enough time has passed since the last check."""
    last = state.get("checked_at")
    if not isinstance(last, int | float):
        return True
    # A clock that moved backwards (timezone change, NTP correction) would
    # otherwise park the next check arbitrarily far in the future.
    if last > now:
        return True
    return (now - last) >= _CHECK_INTERVAL_SECONDS


def _fetch_latest_tag() -> str | None:
    """Ask GitHub for the latest release tag, or ``None`` if anything fails."""
    request = urllib.request.Request(
        _RELEASES_API,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = response.read(_MAX_RESPONSE_BYTES)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # No network, DNS failure, proxy, rate limit, GitHub outage. All of
        # these are ordinary and none is worth telling the user about.
        logger.debug("Update check could not reach GitHub: %s", exc)
        return None

    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.debug("Update check got an unreadable response: %s", exc)
        return None

    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name")
    return tag if isinstance(tag, str) and tag else None


def check_for_update(
    current_version: str,
    *,
    state_file: Path,
    enabled: bool = True,
    now: float | None = None,
) -> Release | None:
    """Return a newer release, or ``None``.

    ``None`` covers every uninteresting case together: the check is switched
    off, it ran recently, the network is unavailable, GitHub said something
    unexpected, or the running version is already current. Callers want to
    treat all of those identically, so they are not distinguished.

    Never raises. This runs on a background thread at start-up, where an
    exception would be both invisible and pointless.
    """
    if not enabled:
        return None

    moment = time.time() if now is None else now
    if not _is_due(_read_state(state_file), moment):
        return None

    # Recorded before the result is known, so a persistently failing check
    # backs off for a day rather than retrying on every single launch.
    _write_state(state_file, moment)

    tag = _fetch_latest_tag()
    if tag is None:
        return None
    if not is_newer(tag, current_version):
        logger.debug("Update check: %s is current", current_version)
        return None

    version = tag.lstrip("v")
    logger.info("Update available: %s (running %s)", version, current_version)
    return Release(version=version, url=_RELEASES_PAGE)


__all__ = [
    "Release",
    "check_for_update",
    "is_newer",
    "parse_version",
]
