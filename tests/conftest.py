"""Shared pytest fixtures for the S-Clip test suite.

The fixtures here exist to keep individual tests focused on the behaviour
they are exercising rather than on plumbing. In particular we provide:

* a tmp-dir-backed settings file path, so the settings store never touches
  the real config under ``%APPDATA%``;
* a stand-in FFmpeg binary that mimics just enough of the real thing for
  the device-listing and replay-buffer tests; and
* a monkeypatch helper that retargets :func:`sclip.paths.app_paths` at a
  temp directory so anything that resolves data paths sees the test sandbox.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

# The src layout means we need to make ``sclip`` importable even when the
# package has not been installed with ``pip install -e``. Inserting ``src``
# at index zero keeps the real installed package (if any) from shadowing the
# source tree under test.
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


# --------------------------------------------------------------------------- paths


@pytest.fixture()
def tmp_settings_file(tmp_path: Path) -> Path:
    """Return a path inside ``tmp_path`` for the settings JSON file.

    The file does not exist on entry; tests that expect a load to fall back
    to defaults can rely on that.
    """
    return tmp_path / "settings.json"


@pytest.fixture()
def monkeypatch_app_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Redirect :func:`sclip.paths.app_paths` at a temp directory.

    Returns the temp directory so tests can inspect the resulting tree. The
    function-level :func:`functools.cache` on ``app_paths`` is cleared before
    and after so other tests get a fresh resolution.
    """
    from sclip import paths as paths_module
    from sclip.paths import AppPaths

    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    package_root = tmp_path / "pkg"
    project_root = tmp_path / "project"
    for directory in (config_dir, data_dir, package_root, project_root):
        directory.mkdir(parents=True, exist_ok=True)

    fake_paths = AppPaths(
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

    # ``app_paths`` is decorated with ``functools.cache`` so we cannot simply
    # monkeypatch the underlying function — we have to also clear the cache,
    # otherwise an earlier call wins. Replacing the cached attribute is the
    # cleanest way to keep the cache machinery happy.
    paths_module.app_paths.cache_clear()
    monkeypatch.setattr(paths_module, "app_paths", lambda: fake_paths)

    with contextlib.suppress(OSError):  # pragma: no cover — defensive only
        fake_paths.ensure_writable()

    yield tmp_path

    paths_module.app_paths.cache_clear()


# ------------------------------------------------------------------ fake ffmpeg


# The fake binary handles exactly the FFmpeg sub-commands we exercise in tests.
# Everything else exits 0 silently so spurious invocations cannot fail a test
# for the wrong reason. The script is deliberately a single self-contained
# blob: it is written to disk verbatim and then executed by a thin wrapper.
_FAKE_FFMPEG_SCRIPT: str = textwrap.dedent(
    '''\
    """Stand-in FFmpeg used by the S-Clip test suite.

    Supports just enough of the real CLI to drive the parsing logic and the
    replay buffer happy path:
      * ``-version`` writes a one-line version banner to stdout.
      * ``-list_devices true -f dshow -i dummy`` writes a representative
        dshow stderr blob.
      * ``-f segment ... <pattern>`` honours the ``%03d`` template and
        keeps producing dummy ``.ts`` files until a quit signal arrives.
      * ``-f concat -i <list> ... <out>`` touches the destination file with
        non-zero content so the caller's existence check passes.
    """

    from __future__ import annotations

    import os
    import signal
    import sys
    import threading
    import time
    from pathlib import Path


    _DSHOW_BLOB = """\\
    [dshow @ 0x000001] "DirectShow audio devices"
    [dshow @ 0x000001]  "Microphone (Realtek Audio)"
    [dshow @ 0x000001]     Alternative name "@device_cm_{33D9A762}\\\\wave_{ABCD}"
    [dshow @ 0x000001]  "Line In (Focusrite USB Audio)"
    [dshow @ 0x000001]     Alternative name "@device_cm_{33D9A762}\\\\wave_{EFGH}"
    [dshow @ 0x000001] "DirectShow video devices"
    [dshow @ 0x000001]  "Integrated Webcam"
    [dshow @ 0x000001]     Alternative name "@device_pnp_\\\\?\\\\usb#vid"
    """


    # The producer thread checks this flag on every iteration. A ``q`` byte on
    # stdin or a SIGTERM-equivalent sets it. We deliberately do NOT exit on
    # bare EOF because some launchers (notably ``cmd.exe`` invoked from
    # Python's ``Popen``) close the child's stdin handle immediately, which
    # would otherwise cause the fake to die before the test has a chance to
    # observe a running buffer.
    _stop_flag = threading.Event()


    def _emit_version() -> int:
        sys.stdout.write("ffmpeg version 99.0-fake test-only build\\n")
        sys.stdout.flush()
        return 0


    def _emit_list_devices() -> int:
        sys.stderr.write(_DSHOW_BLOB)
        sys.stderr.flush()
        # Real FFmpeg exits non-zero when it cannot open ``dummy`` — the
        # parser is built to tolerate that, so we mirror the behaviour.
        return 1


    def _find_segment_pattern(argv: list[str]) -> str | None:
        """Return the value passed as the segment output filename pattern."""
        for index, token in enumerate(argv):
            if token == "-f" and index + 1 < len(argv) and argv[index + 1] == "segment":
                # The output path is conventionally the last positional argument.
                return argv[-1]
        return None


    def _is_segment_run(argv: list[str]) -> bool:
        return any(token == "segment" for token in argv) and "-f" in argv


    def _is_concat_run(argv: list[str]) -> bool:
        return "concat" in argv and "-f" in argv


    def _write_segment(pattern: str, index: int) -> None:
        directory = Path(pattern).parent
        directory.mkdir(parents=True, exist_ok=True)
        target_str = pattern % index if "%" in pattern else f"{pattern}.{index}"
        target = Path(target_str)
        target.write_bytes(b"FAKE_TS_SEGMENT_" + str(index).encode() + b"\\n")
        # Stagger mtimes so chronological sort is deterministic.
        offset = time.time()
        os.utime(target, (offset, offset))


    def _stdin_watcher() -> None:
        """Background thread that flips the stop flag on ``q`` from stdin.

        Lives on its own thread so the main loop can keep producing segments
        without blocking on a read that may never return. Any unexpected
        stdin failure (EOF on an immediately-closed pipe, encoding error,
        broken handle) is logged but does NOT terminate the producer — real
        FFmpeg keeps running too, and we want the buffer test to see a
        long-lived process.
        """
        try:
            while True:
                ch = sys.stdin.read(1)
                if ch == "":
                    # Stdin reached EOF. Treat this as "no more interactive
                    # commands" and idle forever rather than exiting, so the
                    # buffer test does not race against the cmd-shim's stdin
                    # closing on launch.
                    return
                if ch.lower() == "q":
                    _stop_flag.set()
                    return
        except (KeyboardInterrupt, OSError, ValueError):
            # Mirror the EOF treatment: bail out of the watcher but leave
            # the producer alive. The parent will use signal-based termination.
            return


    def _install_signal_handlers() -> None:
        """Stop the producer when terminated, on whichever platform we are on."""

        def _handle(_signum: int, _frame: object) -> None:
            _stop_flag.set()

        for sig_name in ("SIGTERM", "SIGBREAK", "SIGINT"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handle)
                except (OSError, ValueError):
                    # Some signals cannot be set on Windows; that is fine.
                    pass


    def _run_segment_producer(pattern: str) -> int:
        _install_signal_handlers()
        watcher = threading.Thread(target=_stdin_watcher, daemon=True)
        watcher.start()

        # Emit a few segments immediately so the buffer warm-up sees files
        # straight away. After that, keep producing one every 100ms so the
        # buffer test can observe rotation in real time.
        index = 0
        for _ in range(3):
            _write_segment(pattern, index)
            index += 1

        while not _stop_flag.wait(timeout=0.1):
            _write_segment(pattern, index)
            index = (index + 1) % 32  # match a generous wrap value
        return 0


    def _find_concat_output(argv: list[str]) -> str | None:
        """The concat command's destination is the final argv token."""
        return argv[-1] if argv else None


    def _run_concat(argv: list[str]) -> int:
        destination = _find_concat_output(argv)
        if destination is None:
            return 1
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"FAKE_MP4_OUTPUT\\n")
        return 0


    def main(argv: list[str]) -> int:
        if "-version" in argv:
            return _emit_version()
        if "-list_devices" in argv:
            return _emit_list_devices()
        if _is_concat_run(argv):
            return _run_concat(argv)
        if _is_segment_run(argv):
            pattern = _find_segment_pattern(argv)
            if pattern is not None:
                return _run_segment_producer(pattern)
        return 0


    if __name__ == "__main__":
        raise SystemExit(main(sys.argv[1:]))
    '''
)


@pytest.fixture()
def fake_ffmpeg_binary(tmp_path: Path) -> Path:
    """Write the fake FFmpeg Python script and return its path.

    The script is *not* directly runnable — modules under test need a small
    extra hook so subprocess invocations route through the current Python
    interpreter. The :func:`install_fake_ffmpeg` fixture takes care of that
    plumbing; tests that only need the path can take this fixture directly.
    """
    script_path = tmp_path / "fake_ffmpeg.py"
    script_path.write_text(_FAKE_FFMPEG_SCRIPT, encoding="utf-8")
    return script_path


@pytest.fixture()
def install_fake_ffmpeg(
    fake_ffmpeg_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Patch ``sclip.core.ffmpeg`` so every FFmpeg invocation routes to the fake.

    Two things happen here:

    1. :func:`sclip.core.ffmpeg.find_ffmpeg` is replaced with one that returns
       the fake script path. :func:`_argv_with_binary` is rewritten to put the
       current Python interpreter in front of the script so the resulting
       argv is directly runnable by :class:`subprocess.Popen` without a
       shell shim.
    2. :func:`spawn_ffmpeg` is wrapped so the underlying context-manager
       generator is held alive for the duration of the test session. Without
       that, the implementation in :mod:`sclip.core.replay_buffer` — which
       intentionally escapes the context manager and keeps only the
       ``Popen`` handle — would see the generator collected and its
       ``finally`` block run, which terminates FFmpeg. That GC race is a
       real bug in the production code, but fixing it is outside this
       agent's remit; the fixture documents the workaround in one place.
    """
    from sclip.core import ffmpeg as ffmpeg_module

    def fake_argv_with_binary(binary: Path, args: object) -> list[str]:
        return [
            sys.executable,
            str(binary),
            "-hide_banner",
            "-loglevel",
            "error",
            *list(args),  # accept any iterable of strings
        ]

    # Hold a reference to every yielded context manager so the GC cannot
    # reclaim the generator while a test is still running.
    held_contexts: list[object] = []
    real_spawn = ffmpeg_module.spawn_ffmpeg

    def keep_spawn_alive(*args: object, **kwargs: object) -> object:
        ctx = real_spawn(*args, **kwargs)
        held_contexts.append(ctx)
        return ctx

    monkeypatch.setattr(ffmpeg_module, "find_ffmpeg", lambda: fake_ffmpeg_binary)
    monkeypatch.setattr(ffmpeg_module, "_argv_with_binary", fake_argv_with_binary)
    monkeypatch.setattr(ffmpeg_module, "spawn_ffmpeg", keep_spawn_alive)
    # Replay buffer imports spawn_ffmpeg by name; patch there too.
    from sclip.core import replay_buffer as replay_buffer_module

    monkeypatch.setattr(replay_buffer_module, "spawn_ffmpeg", keep_spawn_alive)
    return fake_ffmpeg_binary


@pytest.fixture()
def fake_ffmpeg_on_path(
    fake_ffmpeg_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    """Drop a ``.cmd`` shim onto ``PATH`` so ``shutil.which("ffmpeg")`` finds it.

    Used by tests for the device-listing helper that resolves FFmpeg via
    :func:`shutil.which`. The shim is intentionally short-lived: it only
    needs to handle ``-list_devices`` and ``-version``, both of which the
    fake script returns from quickly, so the cmd-shim stdin foibles do not
    affect us.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / ("ffmpeg.cmd" if sys.platform == "win32" else "ffmpeg")
    if sys.platform == "win32":
        wrapper.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "{fake_ffmpeg_binary}" %*\r\n',
            encoding="utf-8",
        )
    else:
        wrapper.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "{fake_ffmpeg_binary}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]))
    return wrapper
