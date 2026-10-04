"""Local model configuration; API keys are protected with Windows user DPAPI."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConfigError(ValueError):
    pass


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    endpoint: str = Field(default="https://api.openai.com/v1", max_length=2000)
    model: str = Field(default="", max_length=200)
    api_key: str | None = Field(default=None, max_length=4096, repr=False, exclude=True)

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("模型地址需为 HTTP(S) 地址，不能包含账号、查询参数或片段")
        return value

    @field_validator("model")
    @classmethod
    def valid_model(cls, value: str) -> str:
        return value.strip()


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise ConfigError("密钥持久化当前只支持 Windows DPAPI")
    backing = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _Blob(len(data), ctypes.cast(backing, ctypes.POINTER(ctypes.c_ubyte)))
    result = _Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        operation = library.CryptUnprotectData
        operation.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(_Blob)]
        success = operation(ctypes.byref(source), None, None, None, None, 1,
                            ctypes.byref(result))
    else:
        operation = library.CryptProtectData
        operation.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(_Blob)]
        success = operation(ctypes.byref(source), "Kingdom AI model key", None, None, None,
                            1, ctypes.byref(result))
    operation.restype = wintypes.BOOL
    if not success:
        raise ConfigError("Windows 无法保护或恢复模型密钥，请重新配置")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def default_config_path() -> Path:
    if os.name == "nt":
        return Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "KingdomAICompanion/model.local.json"
    return Path.home() / ".local/share/kingdom-ai-companion/model.local.json"


class ModelConfigStore:
    def __init__(self, path: Path | None = None, *,
                 protect: Callable[[bytes], bytes] | None = None,
                 unprotect: Callable[[bytes], bytes] | None = None) -> None:
        self.path = path or default_config_path()
        self.protect = protect or (lambda value: _dpapi(value, decrypt=False))
        self.unprotect = unprotect or (lambda value: _dpapi(value, decrypt=True))
        self.settings = ModelSettings()
        self.load_error: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            if self.path.stat().st_size > 32_000:
                raise ValueError("invalid settings")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError("invalid settings")
            key = data.pop("api_key_dpapi", None)
            settings = ModelSettings.model_validate(data)
            if key:
                settings.api_key = self.unprotect(base64.b64decode(key, validate=True)).decode()
            self.settings = settings
        except (OSError, ValueError, TypeError, KeyError):
            self.load_error = "模型配置未能恢复，请重新保存配置。"

    def public(self) -> dict:
        return {"endpoint": self.settings.endpoint, "model": self.settings.model,
                "key_present": bool(self.settings.api_key),
                "configured": bool(self.settings.model), "error": self.load_error}

    def update(self, value: ModelSettings) -> dict:
        key = self.settings.api_key if value.api_key is None else value.api_key.strip()
        protected = self.protect(key.encode()) if key else None
        encoded = value.model_dump()
        encoded["api_key_dpapi"] = base64.b64encode(protected).decode() if protected else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".model-{secrets.token_hex(8)}.tmp")
        try:
            temporary.write_text(json.dumps(encoded, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        value.api_key = key
        self.settings = value
        self.load_error = None
        return self.public()
