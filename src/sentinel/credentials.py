"""Local credential store for camera passwords entered in the dashboard.

Config files never hold passwords. They reference `${secret:NAME}`, resolved at
runtime. On Windows each value is encrypted with DPAPI (bound to the service's
user account, so a copied file is useless elsewhere). On other systems the store
is a file readable only by its owner (0600); put the storage directory on an
encrypted volume, or use `${ENV_VAR}` from a service manager instead.

Nothing here logs values, and no API endpoint ever returns them."""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
from pathlib import Path

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,63}$")


class SecretStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    @property
    def backend(self) -> str:
        return "dpapi" if sys.platform == "win32" else "file-0600"

    def set(self, name: str, value: str) -> None:
        if not NAME_PATTERN.match(name):
            raise ValueError("invalid secret name")
        with self._lock:
            data = self._read()
            data[name] = base64.b64encode(_protect(value.encode("utf-8"))).decode("ascii")
            self._write(data)

    def get(self, name: str) -> str | None:
        with self._lock:
            token = self._read().get(name)
        if token is None:
            return None
        return _unprotect(base64.b64decode(token)).decode("utf-8")

    def delete(self, name: str) -> None:
        with self._lock:
            data = self._read()
            if data.pop(name, None) is not None:
                self._write(data)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._read())

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(temporary, 0o600)
        temporary.replace(self.path)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _ENTROPY = b"sentinel-camera-credentials"

    def _call(function, data: bytes) -> bytes:
        source = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
        entropy = _Blob(len(_ENTROPY), ctypes.cast(ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY)), ctypes.POINTER(ctypes.c_char)))
        result = _Blob()
        if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x01, ctypes.byref(result)):  # UI_FORBIDDEN
            raise OSError("DPAPI call failed")
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            _kernel32.LocalFree(result.pbData)

    def _protect(data: bytes) -> bytes:
        return _call(lambda s, _d, e, r, p, f, o: _crypt32.CryptProtectData(s, None, e, r, p, f, o), data)

    def _unprotect(data: bytes) -> bytes:
        return _call(lambda s, _d, e, r, p, f, o: _crypt32.CryptUnprotectData(s, None, e, r, p, f, o), data)

else:

    def _protect(data: bytes) -> bytes:
        return data

    def _unprotect(data: bytes) -> bytes:
        return data
