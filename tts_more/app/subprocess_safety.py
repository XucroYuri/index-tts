from __future__ import annotations

import ctypes
import subprocess
import sys


_SEM_FAILCRITICALERRORS = 0x0001
_SEM_NOGPFAULTERRORBOX = 0x0002


def noninteractive_subprocess_kwargs() -> dict[str, int]:
    if sys.platform != "win32":
        return {}
    _configure_noninteractive_error_mode()
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"creationflags": creation_flags} if creation_flags else {}


def _configure_noninteractive_error_mode() -> None:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetErrorMode.argtypes = []
        kernel32.GetErrorMode.restype = ctypes.c_uint
        kernel32.SetErrorMode.argtypes = [ctypes.c_uint]
        kernel32.SetErrorMode.restype = ctypes.c_uint
        current_mode = int(kernel32.GetErrorMode())
        kernel32.SetErrorMode(current_mode | _SEM_FAILCRITICALERRORS | _SEM_NOGPFAULTERRORBOX)
    except (AttributeError, OSError):
        return
