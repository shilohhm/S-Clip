"""Tie child processes to the lifetime of this one.

S-Clip's capture is an FFmpeg process it spawns and later stops. The stop path
is careful, but it only runs when S-Clip gets the chance to run it. If the
application is killed outright — Task Manager's "End task", a crash, an
installer force-closing it during an upgrade — nothing sends FFmpeg the ``q``,
and it inherits none of the parent's mortality. The result observed in testing
was a capture that carried on recording the screen for thirty-four minutes
after the application had gone, writing segments into the buffer directory the
whole time, with no window and no tray icon to reveal it.

Two things make that worse than a stray process. It is a privacy problem: the
screen is still being recorded and nothing on screen says so. And the segments
it leaves behind are indistinguishable from a live session's, so the *next*
capture stitches them into its clip — a saved clip that silently contains
footage from before.

Windows has a purpose-built answer. A job object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` terminates every process assigned to it
once the last handle to the job closes, and the handle closes when this process
exits, however it exits. The kernel enforces it, so there is no cleanup code to
skip.

On any other platform this degrades to doing nothing, which is honest: the
guarantee is Windows-specific and S-Clip is a Windows application.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any, cast

logger = logging.getLogger(__name__)

# JobObjectExtendedLimitInformation, from Windows' JOBOBJECTINFOCLASS.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: kill every assigned process when the last
# handle to the job goes away.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# Rights needed to put an already-running process into a job.
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


def _kernel32() -> Any:  # pragma: no cover - Windows-only
    """Return kernel32 with the signatures we use declared.

    Declaring these is not optional housekeeping. ctypes defaults an
    undeclared return value to C ``int``, which is 32 bits, so a 64-bit HANDLE
    comes back truncated. A truncated job handle still looks truthy, so the
    guard appears to work while the real handle goes unreferenced and closes --
    and because the job carries KILL_ON_JOB_CLOSE, closing it kills the very
    process we just enrolled. The observed symptom was a capture that armed
    successfully and then produced no segments at all.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]

    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]

    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    return kernel32


def _build_job() -> object | None:  # pragma: no cover - Windows-only, exercised at runtime
    """Create the kill-on-close job object, or ``None`` if unavailable."""
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),  # ULONG_PTR: pointer-sized
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = _kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        logger.warning(
            "Could not create a job object (error %s); a crash could orphan FFmpeg",
            ctypes.get_last_error(),
        )
        return None

    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        logger.warning(
            "Could not configure the job object (error %s); a crash could orphan FFmpeg",
            ctypes.get_last_error(),
        )
        kernel32.CloseHandle(job)
        return None

    logger.debug("Child-process job object created")
    # Returned as the HANDLE object, not an int: the guard holds this for the
    # life of the process, and the job must stay open for the guarantee to
    # hold -- closing it would terminate every child it protects.
    return cast(object, job)


class _ProcessGuard:
    """Holds the job object and enrols children in it."""

    def __init__(self) -> None:
        self._job: object | None = None
        self._resolved = False

    def _job_handle(self) -> object | None:
        # Built on first use rather than at import, so importing this module
        # stays free on platforms and test runs that never spawn anything.
        if not self._resolved:
            self._resolved = True
            if sys.platform == "win32":
                self._job = _build_job()
        return self._job

    def guard(self, process: subprocess.Popen[str]) -> bool:
        """Assign ``process`` to the job. Returns whether it was enrolled.

        A failure here is logged and swallowed. Losing the guarantee is bad,
        but it is not a reason to refuse to capture: the ordinary stop path
        still works, and the orphan only appears if the application dies
        abnormally.
        """
        job = self._job_handle()
        if job is None:
            return False

        import ctypes

        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, process.pid)
        if not handle:
            logger.warning(
                "Could not open FFmpeg process %s to guard it (error %s)",
                process.pid,
                ctypes.get_last_error(),
            )
            return False
        try:
            if not kernel32.AssignProcessToJobObject(job, handle):
                logger.warning(
                    "Could not guard FFmpeg process %s (error %s); it may outlive a crash",
                    process.pid,
                    ctypes.get_last_error(),
                )
                return False
        finally:
            kernel32.CloseHandle(handle)

        logger.debug("FFmpeg process %s enrolled in the child job", process.pid)
        return True


_GUARD = _ProcessGuard()


def guard_child(process: subprocess.Popen[str]) -> bool:
    """Tie ``process`` to this application's lifetime where the OS allows it."""
    try:
        return _GUARD.guard(process)
    except Exception:
        logger.exception("Guarding the FFmpeg process failed")
        return False


__all__ = ["guard_child"]
