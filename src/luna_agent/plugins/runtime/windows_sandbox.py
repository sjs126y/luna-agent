"""Native Windows AppContainer process launcher for external plugin workers."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import logging
import os
from pathlib import Path
import subprocess
import threading
from typing import Any, Sequence


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

logger = logging.getLogger(__name__)


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
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


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
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _AppContainerProcess:
    """Small Popen-compatible adapter around a process and owning Job Object."""

    def __init__(self, *, process: int, job: int, pid: int, stdin, stdout, stderr) -> None:
        self._process = wintypes.HANDLE(process)
        self._job = wintypes.HANDLE(job)
        self.pid = int(pid)
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr

    def poll(self) -> int | None:
        code = wintypes.DWORD()
        if not ctypes.windll.kernel32.GetExitCodeProcess(self._process, ctypes.byref(code)):
            raise ctypes.WinError()
        return None if code.value == _STILL_ACTIVE else int(code.value)

    def wait(self, timeout: float | None = None) -> int:
        milliseconds = 0xFFFFFFFF if timeout is None else max(0, int(float(timeout) * 1000))
        result = ctypes.windll.kernel32.WaitForSingleObject(self._process, milliseconds)
        if result == _WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(["appcontainer-worker"], timeout)
        return int(self.poll() or 0)

    def terminate(self) -> None:
        if self.poll() is None and not ctypes.windll.kernel32.TerminateProcess(self._process, 1):
            raise ctypes.WinError()

    def kill(self) -> None:
        self.terminate()

    def close(self) -> None:
        kernel32 = ctypes.windll.kernel32
        for stream in (self.stdin, self.stdout, self.stderr):
            try:
                stream.close()
            except Exception:
                pass
        if self._process:
            kernel32.CloseHandle(self._process)
            self._process = wintypes.HANDLE()
        if self._job:
            kernel32.CloseHandle(self._job)
            self._job = wintypes.HANDLE()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def appcontainer_launch(
    *,
    python: Path,
    plugin_root: Path,
    environment_root: Path,
    data_root: Path,
    allow_network: bool,
    plugin_key: str,
    runtime_instance_id: str,
):
    if os.name != "nt":
        raise RuntimeError("AppContainer is available only on native Windows")

    from luna_agent.plugins.runtime.sandbox import PluginWorkerLaunch

    plugin_root = plugin_root.resolve()
    environment_root = environment_root.resolve()
    data_root = data_root.resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    profile_name = _profile_name(plugin_key, runtime_instance_id)
    profile = _AppContainerProfileLease(
        profile_name=profile_name,
        roots=(plugin_root, environment_root, data_root),
    )

    def process_factory(command: Sequence[str], cwd: Path, env: dict[str, str]):
        return profile.spawn(
            command=command,
            cwd=cwd,
            env=env,
            readable_roots=(plugin_root, environment_root),
            writable_root=data_root,
            allow_network=allow_network,
        )

    return PluginWorkerLaunch(
        argv=(str(python.absolute()), "-m", "luna_agent_plugin_sdk.worker"),
        cwd=data_root,
        backend="appcontainer",
        filesystem_isolated=True,
        network_isolated=not allow_network,
        process_factory=process_factory,
        cleanup=profile.close,
    )


def _profile_name(plugin_key: str, runtime_instance_id: str = "") -> str:
    identity = f"{plugin_key}\0{runtime_instance_id or 'legacy'}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"LunaAgent.Plugin.{digest}"


class _AppContainerProfileLease:
    def __init__(self, *, profile_name: str, roots: tuple[Path, ...]) -> None:
        self.profile_name = profile_name
        self.roots = tuple(Path(root).resolve() for root in roots)
        self._closed = False
        self._lock = threading.Lock()

    def spawn(self, **kwargs) -> _AppContainerProcess:
        with self._lock:
            if self._closed:
                raise RuntimeError("AppContainer profile lease is closed")
        return _spawn_appcontainer(profile_name=self.profile_name, **kwargs)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            _remove_appcontainer_profile(self.profile_name, self.roots)
        except Exception:
            logger.exception("Failed to remove AppContainer profile: %s", self.profile_name)


def _spawn_appcontainer(
    *,
    command: Sequence[str],
    cwd: Path,
    env: dict[str, str],
    profile_name: str,
    readable_roots: tuple[Path, ...],
    writable_root: Path,
    allow_network: bool,
) -> _AppContainerProcess:
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
            "Luna Agent isolated plugin worker",
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
                raise OSError(f"DeriveAppContainerSidFromAppContainerName failed: {int(result)}")

        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(app_sid, ctypes.byref(sid_text)):
            raise ctypes.WinError()
        local_allocations.append(ctypes.cast(sid_text, ctypes.c_void_p).value or 0)
        for root in readable_roots:
            _grant_appcontainer_access(root, sid_text.value, write=False)
        _grant_appcontainer_access(writable_root, sid_text.value, write=True)

        capabilities: Any = None
        capability_count = 0
        if allow_network:
            network_sid = ctypes.c_void_p()
            if not advapi32.ConvertStringSidToSidW("S-1-15-3-1", ctypes.byref(network_sid)):
                raise ctypes.WinError()
            local_allocations.append(network_sid.value or 0)
            capabilities = (_SID_AND_ATTRIBUTES * 1)(
                _SID_AND_ATTRIBUTES(network_sid, 0)
            )
            capability_count = 1
        security_capabilities = _SECURITY_CAPABILITIES(
            app_sid,
            ctypes.cast(capabilities, ctypes.POINTER(_SID_AND_ATTRIBUTES)) if capabilities else None,
            capability_count,
            0,
        )

        child_stdin, parent_stdin = _pipe(parent_reads=False)
        parent_stdout, child_stdout = _pipe(parent_reads=True)
        parent_stderr, child_stderr = _pipe(parent_reads=True)
        handles.extend((child_stdin, parent_stdin, parent_stdout, child_stdout, parent_stderr, child_stderr))

        size = ctypes.c_size_t()
        kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        attribute_buffer = ctypes.create_string_buffer(size.value)
        attribute_list = ctypes.cast(attribute_buffer, ctypes.c_void_p)
        if not kernel32.InitializeProcThreadAttributeList(attribute_list, 2, 0, ctypes.byref(size)):
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
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(command)))
        environment = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(env.items())) + "\0\0"
        )
        flags = _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT | _CREATE_SUSPENDED
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
        limits.BasicLimitInformation.ActiveProcessLimit = 1
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
        stdin, stdout, stderr = _parent_streams(parent_stdin, parent_stdout, parent_stderr)
        for handle in (parent_stdin, parent_stdout, parent_stderr):
            handles.remove(handle)
        result_process = _AppContainerProcess(
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
    kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
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
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
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
    permission = "(OI)(CI)(M)" if write else "(OI)(CI)(RX)"
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


def _remove_appcontainer_profile(profile_name: str, roots: tuple[Path, ...]) -> None:
    kernel32 = ctypes.windll.kernel32
    userenv = ctypes.windll.userenv
    advapi32 = ctypes.windll.advapi32
    _configure_winapi(kernel32, userenv, advapi32)
    app_sid = ctypes.c_void_p()
    result = userenv.DeriveAppContainerSidFromAppContainerName(
        profile_name,
        ctypes.byref(app_sid),
    )
    if int(result) != 0:
        return
    sid_text = wintypes.LPWSTR()
    try:
        if not advapi32.ConvertSidToStringSidW(app_sid, ctypes.byref(sid_text)):
            raise ctypes.WinError()
        for root in roots:
            _revoke_appcontainer_access(root, sid_text.value)
        result = userenv.DeleteAppContainerProfile(profile_name)
        if int(result) != 0:
            raise OSError(f"DeleteAppContainerProfile failed: HRESULT {int(result)}")
    finally:
        if sid_text:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        if app_sid:
            advapi32.FreeSid(app_sid)


def _revoke_appcontainer_access(path: Path, sid: str) -> None:
    completed = subprocess.run(
        ["icacls", str(path), "/remove:g", f"*{sid}", "/C"],
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
