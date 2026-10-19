"""Encrypt saved API keys with the current Windows user's DPAPI identity."""
import base64
import ctypes
from ctypes import wintypes
import os

PREFIX = "dpapi:v1:"


def _crypt(value: bytes, decrypt=False) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("无法使用当前 Windows 账户保护模型密钥。")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def protect_secret(value: str | None) -> str | None:
    if not value or os.name != "nt":
        return value
    return PREFIX + base64.b64encode(_crypt(value.encode())).decode("ascii")


def reveal_secret(value: str | None) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("Invalid stored secret")
    if not value or not value.startswith(PREFIX):
        return value
    if os.name != "nt":
        raise ValueError("Windows 加密密钥只能由原 Windows 账户读取，请重新配置。")
    return _crypt(base64.b64decode(value[len(PREFIX):], validate=True), decrypt=True).decode()
