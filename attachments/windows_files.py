"""Open a checked Windows path, validating the actual handle before any read."""

import ctypes
from ctypes import wintypes
import msvcrt
import os
from pathlib import Path
import stat


def open_regular_file(target: Path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    final_path = kernel.GetFinalPathNameByHandleW
    final_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    final_path.restype = wintypes.DWORD
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    # No write/delete sharing while the descriptor is used. Open the final
    # component itself (not a newly substituted reparse point).
    handle = create(str(target), 0x80000000, 1, None, 3, 0x00200000 | 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = final_path(handle, None, 0, 0)
        if not size:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(size + 1)
        length = final_path(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise PermissionError("无法验证文件实际路径")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        if Path(value) != target:
            raise PermissionError("文件路径在打开期间发生变化")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None  # Descriptor now owns the handle.
        try:
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise PermissionError("仅支持普通文件，不支持目录、特殊文件或硬链接")
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
    finally:
        if handle is not None:
            close(handle)
