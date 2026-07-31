"""Tests for locating the FFmpeg binary.

Discovery matters more than it looks. A developer runs from a checkout with a
versioned extraction sitting in the tree; an installed user has FFmpeg on PATH
or dropped beside the executable. Getting the order wrong means a portable
install silently uses some other FFmpeg from the system.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sclip.core import ffmpeg as ffmpeg_module
from sclip.core.ffmpeg import FFmpegNotFoundError, find_ffmpeg

_BINARY = "ffmpeg.exe"


@pytest.fixture()
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Point discovery at empty project and package roots, and clear PATH."""
    project = tmp_path / "install"
    package = tmp_path / "install" / "_internal" / "sclip"
    project.mkdir(parents=True)
    package.mkdir(parents=True)

    fake = SimpleNamespace(project_root=project, package_root=package)
    monkeypatch.setattr(ffmpeg_module, "app_paths", lambda: fake, raising=False)
    import sclip.paths as paths_module

    monkeypatch.setattr(paths_module, "app_paths", lambda: fake)
    monkeypatch.setattr(ffmpeg_module.shutil, "which", lambda _name: None)
    return fake


def _place(directory: Path, name: str = _BINARY) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / name
    binary.write_bytes(b"MZ")  # enough to be a file
    return binary


def test_missing_ffmpeg_raises_a_clear_error(roots: SimpleNamespace) -> None:
    with pytest.raises(FFmpegNotFoundError):
        find_ffmpeg()


def test_path_is_used_when_nothing_is_bundled(
    roots: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on_path = _place(tmp_path / "system")
    monkeypatch.setattr(ffmpeg_module.shutil, "which", lambda _name: str(on_path))

    assert find_ffmpeg() == on_path


def test_a_sibling_ffmpeg_folder_wins_over_path(
    roots: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed copy beside the app must beat whatever the system has.

    Otherwise a portable install quietly runs a different FFmpeg than the one
    it shipped with, and its behaviour stops being reproducible.
    """
    monkeypatch.setattr(
        ffmpeg_module.shutil, "which", lambda _name: str(_place(tmp_path / "system"))
    )
    bundled = _place(roots.project_root / "ffmpeg" / "bin")

    assert find_ffmpeg() == bundled


def test_a_flat_sibling_folder_is_also_accepted(roots: SimpleNamespace) -> None:
    """Some FFmpeg zips have no ``bin`` directory; accept both shapes."""
    bundled = _place(roots.project_root / "ffmpeg")
    assert find_ffmpeg() == bundled


def test_a_versioned_extraction_is_found_whatever_its_version(
    roots: SimpleNamespace,
) -> None:
    """A checkout holds e.g. ``ffmpeg-7.1-essentials_build``.

    The version used to be hardcoded, so dropping in any other release left the
    app unable to see it.
    """
    bundled = _place(roots.project_root / "ffmpeg-8.0-essentials_build" / "bin")
    assert find_ffmpeg() == bundled


def test_the_newest_versioned_extraction_wins(roots: SimpleNamespace) -> None:
    _place(roots.project_root / "ffmpeg-6.1-essentials_build" / "bin")
    newest = _place(roots.project_root / "ffmpeg-7.1-essentials_build" / "bin")

    assert find_ffmpeg() == newest


def test_a_plain_sibling_folder_beats_a_versioned_extraction(
    roots: SimpleNamespace,
) -> None:
    """The installer lays FFmpeg down at ``ffmpeg/``; that is the deliberate one."""
    _place(roots.project_root / "ffmpeg-7.1-essentials_build" / "bin")
    deliberate = _place(roots.project_root / "ffmpeg" / "bin")

    assert find_ffmpeg() == deliberate


def test_the_package_root_is_searched_too(roots: SimpleNamespace) -> None:
    """A frozen build may sit the binary next to the package rather than above."""
    bundled = _place(roots.package_root / "ffmpeg" / "bin")
    assert find_ffmpeg() == bundled


def test_a_directory_named_like_the_binary_is_ignored(roots: SimpleNamespace) -> None:
    """Only files count; a stray directory must not be returned as the binary."""
    (roots.project_root / "ffmpeg" / "bin" / _BINARY).mkdir(parents=True)

    with pytest.raises(FFmpegNotFoundError):
        find_ffmpeg()
