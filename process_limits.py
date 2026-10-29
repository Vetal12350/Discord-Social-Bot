"""Per-child limits shared by FFmpeg and the disposable extractor."""
import math
import os
from pathlib import Path
import psutil

RSS_LIMIT = 96 * 1024 * 1024


def under_pressure(*, reserve=0):
    """Refuse work on measured low headroom; inspect only our own Linux cgroup."""
    if psutil.virtual_memory().available < 96 * 1024 * 1024 + reserve:
        return True
    if os.name == 'nt':
        return False
    try:
        relative = next(line.split(':', 2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines()
                        if line.startswith('0::'))
        if '..' in Path(relative).parts:
            return False
        group = Path('/sys/fs/cgroup') / relative.lstrip('/')
        current = int((group / 'memory.current').read_text())
        maximum = (group / 'memory.max').read_text().strip()
        if maximum != 'max' and current + reserve > int(maximum) - 16 * 1024 * 1024:
            return True
        high = (group / 'memory.high').read_text().strip()
        if high != 'max':
            return current >= int(high) - 16 * 1024 * 1024
    except (OSError, ValueError, StopIteration):
        pass
    return False

def windows_job(pid: int, cpu_seconds=8):
    """Hard per-process private-memory/CPU limits; close kills any surviving child."""
    import ctypes
    from ctypes import wintypes as w
    size = ctypes.c_size_t
    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64), ('flags', w.DWORD),
                    ('min_ws', size), ('max_ws', size), ('active', w.DWORD), ('affinity', size),
                    ('priority', w.DWORD), ('scheduling', w.DWORD)]
    class Limits(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6), ('process_memory', size),
                    ('job_memory', size), ('peak_process', size), ('peak_job', size)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    process = None
    try:
        limits = Limits()
        limits.basic.flags = 0x2 | 0x100 | 0x2000
        limits.basic.process_time = math.ceil(cpu_seconds) * 10_000_000
        limits.process_memory = 128 * 1024 * 1024
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        process = kernel.OpenProcess(0x100 | 0x1, False, pid)
        if not process or not kernel.AssignProcessToJobObject(job, process):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.CloseHandle(job)
        raise
    finally:
        if process:
            kernel.CloseHandle(process)
    return lambda: kernel.CloseHandle(job)



def limit_process(pid, cpu_seconds=8):
    stats = psutil.Process(pid)
    if os.name == 'nt':
        return stats, windows_job(pid, cpu_seconds)
    stats.nice(10)
    stats.rlimit(psutil.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    stats.rlimit(psutil.RLIMIT_DATA, (128 * 1024 * 1024,) * 2)
    stats.rlimit(psutil.RLIMIT_CPU, (math.ceil(cpu_seconds),) * 2)
    return stats, None
