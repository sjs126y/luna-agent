"""Small Windows Job Object helper for built-in background processes.

The module is importable on every platform, but only performs Win32 calls on
native Windows.  Keeping the handle registry here lets the existing asyncio
process objects remain unchanged while still giving ``process_kill`` a
process-tree boundary.
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_jobs: dict[int, wintypes.HANDLE] = {}
_lock = threading.Lock()


def _kernel32():
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def available() -> bool:
    return os.name == "nt"


def attach(pid: int) -> bool:
    """Attach a running process to a kill-on-close Job Object."""
    if not available():
        return False
    kernel32 = _kernel32()
    process = kernel32.OpenProcess(
        _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        int(pid),
    )
    if not process:
        return False
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        kernel32.CloseHandle(process)
        return False
    try:
        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            return False
        if not kernel32.AssignProcessToJobObject(job, process):
            return False
        with _lock:
            old = _jobs.pop(int(pid), None)
            if old:
                kernel32.CloseHandle(old)
            _jobs[int(pid)] = wintypes.HANDLE(job)
        job = None
        return True
    finally:
        kernel32.CloseHandle(process)
        if job:
            kernel32.CloseHandle(job)


def terminate(pid: int) -> bool:
    """Close the Job Object, terminating the process tree."""
    if not available():
        return False
    with _lock:
        job = _jobs.pop(int(pid), None)
    if not job:
        return False
    _kernel32().CloseHandle(job)
    return True


def release(pid: int) -> None:
    """Release a completed process Job Object without a kill request."""
    if not available():
        return
    with _lock:
        job = _jobs.pop(int(pid), None)
    if job:
        _kernel32().CloseHandle(job)
