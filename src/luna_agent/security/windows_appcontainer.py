"""Generic native Windows AppContainer process primitives.

This module owns the OS boundary used by both external plugin workers and the
built-in shell broker.  It deliberately contains no plugin or shell policy:
callers decide which executable, roots, environment, and network capability
are allowed, while this module enforces the token, ACL, stdio, and Job Object
boundary.

The module is importable on Linux/WSL for capability checks and tests.  Any
operation that actually creates an AppContainer process fails explicitly when
the host is not native Windows.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
from typing import Any, Sequence

logger = logging.getLogger(__name__)

_ERROR_ALREADY_EXISTS_HRESULT = -2147024713
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_SUSPENDED = 0x00000004
_STARTF_USESTDHANDLES = 0x00000100
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_HANDLE_FLAG_INHERIT = 0x00000001
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WAIT_TIMEOUT = 258
_STILL_ACTIVE = 259


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.POINTER(_SID_AND_ATTRIBUTES)),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
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
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
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


@dataclass(frozen=True, slots=True)
class AppContainerLaunchInfo:
    """Description of the security boundary selected for a process."""

    profile_name: str
    active_process_limit: int
    filesystem_isolated: bool = True
    network_isolated: bool = True
    security_level: str = "os-isolated"


class AppContainerProcess:
    """Small Popen-compatible adapter owning the process Job Object."""

    def __init__(self, *, process: int, job: int, pid: int, stdin, stdout, stderr):
        self._process = wintypes.HANDLE(process)
        self._job = wintypes.HANDLE(job)
        self.pid = int(pid)
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._returncode: int | None = None
        self._closed = False

    @property
    def returncode(self) -> int | None:
        return self.poll()

    def poll(self) -> int | None:
        if not self._process:
            return self._returncode
        code = wintypes.DWORD()
        if not ctypes.windll.kernel32.GetExitCodeProcess(self._process, ctypes.byref(code)):
            raise ctypes.WinError()
        if code.value == _STILL_ACTIVE:
            return None
        self._returncode = int(code.value)
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._process:
            return int(self._returncode or 0)
        milliseconds = (
            0xFFFFFFFF
            if timeout is None
            else max(0, int(float(timeout) * 1000))
        )
        result = ctypes.windll.kernel32.WaitForSingleObject(
            self._process, milliseconds
        )
        if result == _WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(["appcontainer"], timeout)
        return int(self.poll() or 0)

    def terminate(self) -> None:
        if self.poll() is None and not ctypes.windll.kernel32.TerminateProcess(
            self._process, 1
        ):
            raise ctypes.WinError()

    def kill(self) -> None:
        # Closing the Job Object is the preferred tree-kill path.  Terminate
        # remains a fallback for callers that need immediate process status.
        self.terminate()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        kernel32 = ctypes.windll.kernel32
        for stream in (self.stdin, self.stdout, self.stderr):
            try:
                stream.close()
            except Exception:
                pass
        if self._job:
            kernel32.CloseHandle(self._job)
            self._job = wintypes.HANDLE()
        if self._process:
            try:
                self.poll()
            except Exception:
                pass
            kernel32.CloseHandle(self._process)
            self._process = wintypes.HANDLE()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class AppContainerLease:
    """Profile/ACL lease shared by one logical process generation."""

    def __init__(
        self,
        *,
        profile_name: str,
        roots: Sequence[Path],
        active_process_limit: int = 1,
        lease_root: Path | None = None,
    ) -> None:
        self.profile_name = profile_name
        normalized_roots = tuple(Path(root).resolve() for root in roots)
        self.active_process_limit = max(1, int(active_process_limit))
        self.lease_root = Path(lease_root).resolve() if lease_root else None
        self.roots = tuple(
            dict.fromkeys(
                (*normalized_roots, self.lease_root) if self.lease_root else normalized_roots
            )
        )
        self.info = AppContainerLaunchInfo(
            profile_name=profile_name,
            active_process_limit=self.active_process_limit,
        )
        self._closed = False
        self._lock = threading.Lock()
        self._marker: Path | None = None
        if self.lease_root is not None and os.name == "nt":
            self._marker = _create_lease_marker(
                self.lease_root,
                profile_name=self.profile_name,
                roots=self.roots,
            )

    def spawn(
        self,
        *,
        command: Sequence[str],
        cwd: Path,
        env: dict[str, str],
        readable_roots: Sequence[Path] = (),
        writable_roots: Sequence[Path] = (),
        denied_roots: Sequence[Path] = (),
        allow_network: bool = False,
    ) -> AppContainerProcess:
        if os.name != "nt":
            raise RuntimeError("AppContainer is available only on native Windows")
        with self._lock:
            if self._closed:
                raise RuntimeError("AppContainer profile lease is closed")
        return _spawn_appcontainer(
            command=command,
            cwd=Path(cwd),
            env=dict(env),
            profile_name=self.profile_name,
            readable_roots=tuple(Path(root).resolve() for root in readable_roots),
            writable_roots=tuple(Path(root).resolve() for root in writable_roots),
            denied_roots=tuple(Path(root).resolve() for root in denied_roots),
            allow_network=allow_network,
            active_process_limit=self.active_process_limit,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        cleaned = False
        try:
            cleanup_appcontainer_profile(self.profile_name, self.roots)
            cleaned = True
        except Exception:
            logger.exception("Failed to remove AppContainer profile: %s", self.profile_name)
        finally:
            if cleaned and self._marker is not None:
                try:
                    self._marker.unlink(missing_ok=True)
                except OSError:
                    logger.debug("Failed to remove AppContainer lease marker: %s", self._marker)


def profile_name(namespace: str, identity: str) -> str:
    """Return a stable, bounded profile name for a lease identity."""
    digest = hashlib.sha256(f"{namespace}\0{identity}".encode("utf-8")).hexdigest()[:24]
    return f"LunaAgent.{namespace}.{digest}"


def create_lease(
    *,
    namespace: str,
    identity: str,
    roots: Sequence[Path],
    active_process_limit: int = 1,
    lease_root: Path | None = None,
) -> AppContainerLease:
    return AppContainerLease(
        profile_name=profile_name(namespace, identity),
        roots=roots,
        active_process_limit=active_process_limit,
        lease_root=lease_root,
    )


def sweep_orphan_leases(lease_root: Path) -> int:
    """Remove markers whose owning host process no longer exists.

    The sweep is deliberately conservative: malformed markers are left for a
    later manual inspection, and only profiles with a dead owner PID are
    removed.  It is safe to call on non-Windows hosts and returns the number
    of cleaned leases.
    """
    if os.name != "nt":
        return 0
    root = Path(lease_root).resolve()
    if not root.is_dir():
        return 0
    cleaned = 0
    for marker in root.glob("*.json"):
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            profile = payload.get("profile")
            owner_pid = int(payload.get("owner_pid", 0))
            roots = tuple(Path(item) for item in payload.get("roots", ()))
            if not isinstance(profile, str) or not _profile_is_safe(profile):
                continue
            if _process_is_alive(owner_pid):
                continue
            cleanup_appcontainer_profile(profile, roots)
            marker.unlink(missing_ok=True)
            cleaned += 1
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.debug("Ignoring malformed AppContainer lease marker: %s", marker)
        except Exception:
            logger.exception("Failed to sweep AppContainer lease marker: %s", marker)
    return cleaned


def _create_lease_marker(lease_root: Path, *, profile_name: str, roots: Sequence[Path]) -> Path:
    lease_root.mkdir(parents=True, exist_ok=True)
    sweep_orphan_leases(lease_root)
    marker = lease_root / f"{profile_name}.{os.getpid()}.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": profile_name,
                "owner_pid": os.getpid(),
                "roots": [str(Path(root).resolve()) for root in roots],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return marker


def _profile_is_safe(profile: str) -> bool:
    return bool(profile.startswith("LunaAgent.") and len(profile) <= 128 and "/" not in profile and "\\" not in profile)


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    kernel32 = ctypes.windll.kernel32
    _configure_winapi(kernel32, ctypes.windll.userenv, ctypes.windll.advapi32)
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def cleanup_appcontainer_profile(profile: str, roots: Sequence[Path]) -> None:
    """Revoke per-lease ACLs and remove an AppContainer profile idempotently."""
    if os.name != "nt":
        return
    kernel32 = ctypes.windll.kernel32
    userenv = ctypes.windll.userenv
    advapi32 = ctypes.windll.advapi32
    _configure_winapi(kernel32, userenv, advapi32)
    app_sid = ctypes.c_void_p()
    result = userenv.DeriveAppContainerSidFromAppContainerName(
        profile, ctypes.byref(app_sid)
    )
    if int(result) != 0:
        return
    sid_text = wintypes.LPWSTR()
    try:
        if not advapi32.ConvertSidToStringSidW(app_sid, ctypes.byref(sid_text)):
            raise ctypes.WinError()
        revoke_errors: list[str] = []
        for root in roots:
            try:
                _revoke_appcontainer_access(Path(root), sid_text.value)
            except Exception as exc:
                revoke_errors.append(f"{root}: {exc}")
        if revoke_errors:
            raise RuntimeError(
                "Failed to revoke one or more AppContainer ACLs: "
                + "; ".join(revoke_errors[:5])
            )
        result = userenv.DeleteAppContainerProfile(profile)
        if int(result) != 0:
            raise OSError(f"DeleteAppContainerProfile failed: HRESULT {int(result)}")
    finally:
        if sid_text:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        if app_sid:
            advapi32.FreeSid(app_sid)


def _spawn_appcontainer(
    *,
    command: Sequence[str],
    cwd: Path,
    env: dict[str, str],
    profile_name: str,
    readable_roots: Sequence[Path],
    writable_roots: Sequence[Path],
    denied_roots: Sequence[Path],
    allow_network: bool,
    active_process_limit: int,
) -> AppContainerProcess:
    kernel32 = ctypes.windll.kernel32
    userenv = ctypes.windll.userenv
    advapi32 = ctypes.windll.advapi32
    _configure_winapi(kernel32, userenv, advapi32)
    app_sid = ctypes.c_void_p()
    local_allocations: list[int] = []
    handles: list[int] = []
    attribute_buffer: Any | None = None
    attribute_list = ctypes.c_void_p()
    process_info = _PROCESS_INFORMATION()
    job = 0
    try:
        result = userenv.CreateAppContainerProfile(
            profile_name,
            profile_name,
            "Luna Agent isolated process",
            None,
            0,
            ctypes.byref(app_sid),
        )
        if int(result) not in (0, _ERROR_ALREADY_EXISTS_HRESULT):
            raise OSError(f"CreateAppContainerProfile failed: HRESULT {int(result)}")
        if int(result) == _ERROR_ALREADY_EXISTS_HRESULT:
            result = userenv.DeriveAppContainerSidFromAppContainerName(
                profile_name, ctypes.byref(app_sid)
            )
            if int(result) != 0:
                raise OSError(
                    "DeriveAppContainerSidFromAppContainerName failed: "
                    f"{int(result)}"
                )

        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(app_sid, ctypes.byref(sid_text)):
            raise ctypes.WinError()
        local_allocations.append(ctypes.cast(sid_text, ctypes.c_void_p).value or 0)
        for root in readable_roots:
            _grant_appcontainer_access(Path(root), sid_text.value, write=False)
        for root in writable_roots:
            _grant_appcontainer_access(Path(root), sid_text.value, write=True)
        for root in denied_roots:
            _deny_appcontainer_access(Path(root), sid_text.value)

        capabilities: Any = None
        capability_count = 0
        if allow_network:
            network_sid = ctypes.c_void_p()
            if not advapi32.ConvertStringSidToSidW(
                "S-1-15-3-1", ctypes.byref(network_sid)
            ):
                raise ctypes.WinError()
            local_allocations.append(network_sid.value or 0)
            capabilities = (_SID_AND_ATTRIBUTES * 1)(
                _SID_AND_ATTRIBUTES(network_sid, 0)
            )
            capability_count = 1
        security_capabilities = _SECURITY_CAPABILITIES(
            app_sid,
            ctypes.cast(capabilities, ctypes.POINTER(_SID_AND_ATTRIBUTES))
            if capabilities
            else None,
            capability_count,
            0,
        )

        child_stdin, parent_stdin = _pipe(parent_reads=False)
        parent_stdout, child_stdout = _pipe(parent_reads=True)
        parent_stderr, child_stderr = _pipe(parent_reads=True)
        handles.extend(
            (child_stdin, parent_stdin, parent_stdout, child_stdout, parent_stderr, child_stderr)
        )

        size = ctypes.c_size_t()
        kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        attribute_buffer = ctypes.create_string_buffer(size.value)
        attribute_list = ctypes.cast(attribute_buffer, ctypes.c_void_p)
        if not kernel32.InitializeProcThreadAttributeList(
            attribute_list, 2, 0, ctypes.byref(size)
        ):
            raise ctypes.WinError()
        if not kernel32.UpdateProcThreadAttribute(
            attribute_list,
            0,
            _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(security_capabilities),
            ctypes.sizeof(security_capabilities),
            None,
            None,
        ):
            raise ctypes.WinError()
        inherited = (wintypes.HANDLE * 3)(child_stdin, child_stdout, child_stderr)
        if not kernel32.UpdateProcThreadAttribute(
            attribute_list,
            0,
            _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            ctypes.byref(inherited),
            ctypes.sizeof(inherited),
            None,
            None,
        ):
            raise ctypes.WinError()

        startup = _STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = child_stdin
        startup.StartupInfo.hStdOutput = child_stdout
        startup.StartupInfo.hStdError = child_stderr
        startup.lpAttributeList = attribute_list
        command_line = ctypes.create_unicode_buffer(
            subprocess.list2cmdline([str(item) for item in command])
        )
        clean_env = {str(key): str(value) for key, value in env.items()}
        environment = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(clean_env.items()))
            + "\0\0"
        )
        flags = (
            _EXTENDED_STARTUPINFO_PRESENT
            | _CREATE_UNICODE_ENVIRONMENT
            | _CREATE_SUSPENDED
        )
        if not kernel32.CreateProcessW(
            None,
            command_line,
            None,
            None,
            True,
            flags,
            environment,
            str(Path(cwd).resolve()),
            ctypes.byref(startup),
            ctypes.byref(process_info),
        ):
            raise ctypes.WinError()

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError()
        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_ACTIVE_PROCESS | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        limits.BasicLimitInformation.ActiveProcessLimit = max(1, int(active_process_limit))
        if not kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError()
        if not kernel32.AssignProcessToJobObject(job, process_info.hProcess):
            raise ctypes.WinError()
        if kernel32.ResumeThread(process_info.hThread) == 0xFFFFFFFF:
            raise ctypes.WinError()

        kernel32.CloseHandle(process_info.hThread)
        process_info.hThread = None
        for handle in (child_stdin, child_stdout, child_stderr):
            kernel32.CloseHandle(handle)
            handles.remove(handle)
        stdin, stdout, stderr = _parent_streams(
            parent_stdin, parent_stdout, parent_stderr
        )
        for handle in (parent_stdin, parent_stdout, parent_stderr):
            handles.remove(handle)
        result_process = AppContainerProcess(
            process=process_info.hProcess,
            job=job,
            pid=process_info.dwProcessId,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        process_info.hProcess = None
        job = 0
        return result_process
    except Exception:
        if process_info.hProcess:
            kernel32.TerminateProcess(process_info.hProcess, 1)
        raise
    finally:
        if attribute_list:
            kernel32.DeleteProcThreadAttributeList(attribute_list)
        for handle in handles:
            if handle:
                kernel32.CloseHandle(handle)
        if process_info.hThread:
            kernel32.CloseHandle(process_info.hThread)
        if process_info.hProcess:
            kernel32.CloseHandle(process_info.hProcess)
        if job:
            kernel32.CloseHandle(job)
        for allocation in local_allocations:
            if allocation:
                kernel32.LocalFree(allocation)
        if app_sid:
            advapi32.FreeSid(app_sid)


def _configure_winapi(kernel32, userenv, advapi32) -> None:
    userenv.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wintypes.DWORD,
    ]
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.SetHandleInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.DeleteProcThreadAttributeList.restype = None
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOEXW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [wintypes.HANDLE]
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.FreeSid.argtypes = [ctypes.c_void_p]
    advapi32.FreeSid.restype = ctypes.c_void_p


def _grant_appcontainer_access(path: Path, sid: str, *, write: bool) -> None:
    if not path.exists():
        raise RuntimeError(f"AppContainer resource root does not exist: {path}")
    if path.is_dir():
        permission = "(OI)(CI)(M)" if write else "(OI)(CI)(RX)"
    else:
        permission = "(M)" if write else "(RX)"
    completed = subprocess.run(
        ["icacls", str(path), "/grant", f"*{sid}:{permission}", "/C"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Failed to grant AppContainer access to {path}: "
            f"{(completed.stdout or '')[-2000:]}"
        )


def _revoke_appcontainer_access(path: Path, sid: str) -> None:
    if not path.exists():
        return
    completed = subprocess.run(
        ["icacls", str(path), "/remove", f"*{sid}", "/C"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Failed to revoke AppContainer access from {path}: "
            f"{(completed.stdout or '')[-2000:]}"
        )


def _deny_appcontainer_access(path: Path, sid: str) -> None:
    if not path.exists():
        return
    permission = "(OI)(CI)(F)" if path.is_dir() else "(F)"
    completed = subprocess.run(
        ["icacls", str(path), "/deny", f"*{sid}:{permission}", "/C"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Failed to deny AppContainer access to {path}: "
            f"{(completed.stdout or '')[-2000:]}"
        )


def _pipe(*, parent_reads: bool) -> tuple[int, int]:
    kernel32 = ctypes.windll.kernel32
    read_handle = wintypes.HANDLE()
    write_handle = wintypes.HANDLE()
    attributes = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)
    if not kernel32.CreatePipe(
        ctypes.byref(read_handle), ctypes.byref(write_handle), ctypes.byref(attributes), 0
    ):
        raise ctypes.WinError()
    parent = read_handle if parent_reads else write_handle
    if not kernel32.SetHandleInformation(parent, _HANDLE_FLAG_INHERIT, 0):
        kernel32.CloseHandle(read_handle)
        kernel32.CloseHandle(write_handle)
        raise ctypes.WinError()
    return int(read_handle.value), int(write_handle.value)


def _parent_streams(stdin_handle: int, stdout_handle: int, stderr_handle: int):
    import msvcrt

    stdin_fd = msvcrt.open_osfhandle(stdin_handle, os.O_WRONLY | os.O_BINARY)
    stdout_fd = msvcrt.open_osfhandle(stdout_handle, os.O_RDONLY | os.O_BINARY)
    stderr_fd = msvcrt.open_osfhandle(stderr_handle, os.O_RDONLY | os.O_BINARY)
    return (
        os.fdopen(stdin_fd, "wb", buffering=0),
        os.fdopen(stdout_fd, "rb", buffering=0),
        os.fdopen(stderr_fd, "rb", buffering=0),
    )
