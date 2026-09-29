"""Windows DPAPI (CryptProtectData/CryptUnprotectData) via ctypes - no extra
pip dependency (avoids adding pywin32, a native-extension package, to the
cx_Freeze build pipeline untested). Ties the encrypted bytes to the current
Windows user account: readable only by that same user's own processes, not by
copying the file elsewhere or by another local account/process reading the
raw file. Used by account_manager/account_config.py to stop storing Riot
session cookies (equivalent to persistent login credentials) in plaintext on
disk (security report 2026-09-29)."""
import ctypes
import ctypes.wintypes as wintypes

_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_PDATA_BLOB = ctypes.POINTER(_DATA_BLOB)

# Explicit argtypes/restype (rather than relying on ctypes' default int/None
# marshaling) since this can't be exercised on a real Windows machine from
# this dev environment - spelling out the real Win32 signatures removes one
# whole class of "worked by ctypes coincidence" bugs.
_crypt32.CryptProtectData.argtypes = [
    _PDATA_BLOB, wintypes.LPCWSTR, _PDATA_BLOB, wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, _PDATA_BLOB,
]
_crypt32.CryptProtectData.restype = wintypes.BOOL
_crypt32.CryptUnprotectData.argtypes = [
    _PDATA_BLOB, ctypes.POINTER(wintypes.LPWSTR), _PDATA_BLOB, wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, _PDATA_BLOB,
]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
_kernel32.LocalFree.restype = wintypes.HLOCAL


def _make_blob(data: bytes):
    # Returns (blob, buffer) - the caller MUST keep `buffer` alive for the
    # duration of the API call, since `blob.pbData` points into it.
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _extract_and_free(blob: "_DATA_BLOB") -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        _kernel32.LocalFree(blob.pbData)


def protect(data: bytes) -> bytes:
    blob_in, _keepalive = _make_blob(data)
    blob_out = _DATA_BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return _extract_and_free(blob_out)


def unprotect(data: bytes) -> bytes:
    blob_in, _keepalive = _make_blob(data)
    blob_out = _DATA_BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return _extract_and_free(blob_out)
