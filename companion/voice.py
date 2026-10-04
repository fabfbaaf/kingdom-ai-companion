"""Explicit, offline microphone transcription with bounded in-memory audio.

The model is fetched only after prepare_model() is requested. No microphone is
opened by construction, status polling, or model download. Audio is never saved.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import math
import os
import queue
import re
import shutil
import stat
import tempfile
import threading
import zipfile
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

MODEL_NAME = "vosk-model-small-cn-0.22"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
# SHA-256 of the archive retrieved from the official HTTPS URL on 2026-10-03.
# The upstream model page does not publish a separate signed checksum.
MODEL_SHA256 = "3af8b0e7e0f835ae9d414ce5df580237a3cfb08d586c9fbbb0f7ff29ad5b14ba"
MODEL_ARCHIVE_BYTES = 43_898_754
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 96 * 1024 * 1024
MAX_EVENTS = 64
MAX_TEXT = 2000
MODEL_FILES = (
    "am/final.mdl",
    "conf/mfcc.conf",
    "conf/model.conf",
    "graph/Gr.fst",
    "graph/HCLr.fst",
)


class VoiceError(RuntimeError):
    pass


def default_model_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / "KingdomAICompanion" / "speech" / MODEL_NAME


def _model_ready(path: Path) -> bool:
    try:
        return all(
            (path / name).is_file() and (path / name).stat().st_size > 0 for name in MODEL_FILES
        )
    except OSError:
        return False


def _extract_archive(archive: Path, staging: Path) -> Path:
    """Validate every ZIP entry before writing any file under staging."""
    root = staging.resolve()
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if not entries or len(entries) > 256:
            raise VoiceError("语音模型压缩包内容不符合预期。")
        if sum(info.file_size for info in entries) > MAX_EXPANDED_BYTES:
            raise VoiceError("语音模型解压大小超出限制。")
        seen = set()
        for info in entries:
            # ZipInfo normalizes backslashes on Windows; inspect the original too.
            name = info.orig_filename
            parts = PurePosixPath(name).parts
            mode = info.external_attr >> 16
            if (
                not parts
                or parts[0] != MODEL_NAME
                or "\\" in name
                or "\x00" in name
                or ":" in name
                or any(part in {".", ".."} for part in name.split("/"))
                or any(part.endswith((" ", ".")) for part in parts)
                or PurePosixPath(name).is_absolute()
                or stat.S_ISLNK(mode)
                or info.flag_bits & 1
                or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            ):
                raise VoiceError("语音模型压缩包包含不安全的路径。")
            target = (root / Path(*parts)).resolve()
            if not target.is_relative_to(root) or str(target).casefold() in seen:
                raise VoiceError("语音模型压缩包包含重复或越界的路径。")
            seen.add(str(target).casefold())
        for info in entries:
            target = root.joinpath(*PurePosixPath(info.filename).parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(info) as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination, length=64 * 1024)
            if target.stat().st_size != info.file_size:
                raise VoiceError("语音模型解压校验失败。")
    model = root / MODEL_NAME
    if not _model_ready(model):
        raise VoiceError("语音模型缺少必要文件。")
    return model


def _clean_text(payload: str, field: str) -> str:
    if not isinstance(payload, str) or len(payload) > 20_000:
        raise VoiceError("语音识别结果超出限制。")
    value = json.loads(payload).get(field, "")
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise VoiceError("语音识别结果格式异常。")
    # Vosk CN inserts spaces between characters; retain spaces around Latin words.
    value = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", value)
    return value.strip()


class VoiceService:
    def __init__(
        self,
        model_dir: Path | str | None = None,
        *,
        model_loader: Callable[[str], Any] | None = None,
        recognizer_factory: Callable[[Any, float], Any] | None = None,
        stream_factory: Callable[..., Any] | None = None,
        device_query: Callable[..., dict[str, Any]] | None = None,
        device_list: Callable[[], list[dict[str, Any]]] | None = None,
        settings_path: Path | None = None,
        download_client: httpx.AsyncClient | None = None,
    ):
        self.model_dir = Path(model_dir) if model_dir is not None else default_model_dir()
        self._model_loader = model_loader
        self._recognizer_factory = recognizer_factory
        self._stream_factory = stream_factory
        self._device_query = device_query
        self._device_list = device_list
        self._settings_path = settings_path or (
            default_model_dir().parent.parent / "voice.local.json" if model_dir is None
            else self.model_dir.parent / "voice.local.json")
        self._device_preference: dict | None = None
        self._device_missing = False
        try:
            if self._settings_path.is_file() and self._settings_path.stat().st_size < 4096:
                setting = json.loads(self._settings_path.read_text(encoding="utf-8"))
                if (isinstance(setting, dict) and isinstance(setting.get("name"), str)
                        and isinstance(setting.get("host_api"), str)):
                    self._device_preference = {"name": setting["name"][:200],
                                               "host_api": setting["host_api"][:100]}
        except (OSError, ValueError):
            pass
        self._selected_device: int | None = None
        self._devices: list[dict] = []
        self._device_error: str | None = None
        self._error_code: str | None = None
        self._level = 0.0
        self._peak = 0.0
        self._overflows = 0
        self._last_audio_at: str | None = None
        self._download_client = download_client
        self._lock = threading.RLock()
        self._start_lock = asyncio.Lock()
        self._generation = 0
        self._stopping = 0
        self._closed = False
        self._stream = None
        self._worker: threading.Thread | None = None
        self._capture_stop = threading.Event()
        self._model = None
        self._download_task: asyncio.Task | None = None
        self._partial = ""
        self._device = None
        self._progress: float | None = None
        self._seq = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        available = all(
            factory is not None or importlib.util.find_spec(module) is not None
            for factory, module in ((model_loader, "vosk"), (stream_factory, "sounddevice"))
        )
        self._available = available
        self._state = "ready" if _model_ready(self.model_dir) else "needs_model"
        self._message = "中文语音模型已就绪，麦克风未开启。" if self._state == "ready" else (
            "请先下载中文语音模型（约 42 MB）；安装后可离线识别。"
        )
        if not available:
            self._state = "unavailable"
            self._message = "缺少本地语音依赖，请重新运行项目安装脚本。"

    def status(self) -> dict[str, Any]:
        with self._lock:
            model_ready = _model_ready(self.model_dir)
            if model_ready and self._state == "needs_model":
                self._state = "ready"
                self._message = "中文语音模型已就绪，麦克风未开启。"
            return {
                "state": self._state,
                "message": self._message,
                "partial": self._partial,
                "model_ready": model_ready,
                "device": self._device,
                "progress": self._progress,
                "cursor": self._seq,
                "stopping": self._stopping > 0,
                "selected_device": self._selected_device,
                "devices": list(self._devices),
                "device_error": self._device_error,
                "error_code": self._error_code,
                "level": self._level,
                "peak": self._peak,
                "overflows": self._overflows,
                "last_audio_at": self._last_audio_at,
            }

    def _query_devices(self) -> list[dict]:
        if self._device_list is not None:
            return self._device_list()
        import sounddevice

        hosts = sounddevice.query_hostapis()
        default = sounddevice.default.device[0]
        return [{"index": index, "name": str(device["name"])[:200],
                 "host_api": str(hosts[device["hostapi"]]["name"]),
                 "sample_rate": float(device["default_samplerate"]),
                 "channels": int(device["max_input_channels"]), "default": index == default}
                for index, device in enumerate(sounddevice.query_devices())
                if device["max_input_channels"] > 0]

    async def devices(self) -> dict:
        try:
            devices = await asyncio.to_thread(self._query_devices)
            with self._lock:
                self._devices = devices
                if self._device_preference and self._stream is None and not self._stopping:
                    selected = next((d for d in devices if all(d.get(k) == v for k, v in self._device_preference.items())), None)
                    self._selected_device = selected["index"] if selected else None
                    self._device_missing = selected is None
                selected_available = any(d.get("index") == self._selected_device for d in devices)
                self._device_error = ("上次选择的麦克风已不可用，请重新选择输入设备。" if self._device_missing else
                                     None if selected_available or any(d.get("default") for d in devices) else
                                      "Windows 默认麦克风不可用，请选择输入设备；必要时检查系统声音设置。")
        except Exception:  # noqa: BLE001 -- device enumeration never records audio
            with self._lock:
                self._devices = []
                self._device_error = "无法读取输入设备，请检查音频服务、驱动与 Windows 麦克风权限。"
        return self.status()

    async def select_device(self, device: int | None) -> dict:
        async with self._start_lock:
            with self._lock:
                if self._closed or self._stream is not None or self._stopping:
                    raise VoiceError("请先停止监听，再选择麦克风。")
            await self.devices()
            with self._lock:
                if device is not None and not any(d["index"] == device for d in self._devices):
                    raise VoiceError("所选输入设备已不可用，请刷新设备列表。")
                self._selected_device = device
                self._device_missing = False
                chosen = next((d for d in self._devices if d["index"] == device), None)
                self._device_error = (None if chosen or any(d.get("default") for d in self._devices) else
                                      "Windows 默认麦克风不可用，请选择输入设备；必要时检查系统声音设置。")
                self._device_preference = ({"name": chosen["name"], "host_api": chosen.get("host_api", "")}
                                           if chosen else None)
                self._device = None
                self._error_code = None
                self._message = "输入设备已选择，点击开启监听后收音。"
            payload = json.dumps(self._device_preference, ensure_ascii=False)
            def save():
                self._settings_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self._settings_path.with_name(".voice-settings.tmp")
                try:
                    temporary.write_text(payload, encoding="utf-8")
                    os.replace(temporary, self._settings_path)
                finally:
                    temporary.unlink(missing_ok=True)
            try:
                await asyncio.to_thread(save)
            except OSError:
                with self._lock:
                    self._message = "输入设备已选择，但无法保存偏好；重启后需要重新选择。"
            return self.status()

    def events(self, after: int = 0) -> dict[str, Any]:
        with self._lock:
            first = self._events[0]["seq"] if self._events else self._seq + 1
            return {
                "events": [dict(event) for event in self._events if event["seq"] > after],
                "cursor": self._seq,
                "dropped": max(0, first - max(0, after) - 1),
            }

    async def prepare_model(self) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise VoiceError("语音服务正在关闭。")
            if not self._available:
                raise VoiceError(self._message)
            if _model_ready(self.model_dir):
                return self.status()
            if self._download_task is None or self._download_task.done():
                self._state = "downloading"
                self._message = "正在从 Vosk 官方下载中文模型；麦克风未开启。"
                self._progress = 0.0
                self._download_task = asyncio.create_task(self._download_model())
        return self.status()

    async def _download_model(self) -> None:
        client = self._download_client or httpx.AsyncClient(
            timeout=httpx.Timeout(45, connect=15), follow_redirects=False
        )
        temporary = None
        try:
            self.model_dir.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix=".vosk-download-", suffix=".zip", dir=self.model_dir.parent, delete=False
            ) as handle:
                temporary = Path(handle.name)
            digest = hashlib.sha256()
            count = 0
            async with client.stream("GET", MODEL_URL) as response:
                response.raise_for_status()
                if response.status_code != 200:
                    raise VoiceError("语音模型下载返回异常状态。")
                length = response.headers.get("content-length")
                if length is not None and int(length) > MAX_ARCHIVE_BYTES:
                    raise VoiceError("语音模型下载大小超出限制。")
                # Writes are bounded to 64 KiB and do not record any microphone data.
                with temporary.open("wb") as output:  # noqa: ASYNC230
                    async for block in response.aiter_bytes(chunk_size=64 * 1024):
                        count += len(block)
                        if count > MAX_ARCHIVE_BYTES:
                            raise VoiceError("语音模型下载大小超出限制。")
                        output.write(block)
                        digest.update(block)
                        with self._lock:
                            self._progress = min(0.99, count / MODEL_ARCHIVE_BYTES)
            if digest.hexdigest() != MODEL_SHA256 or count != MODEL_ARCHIVE_BYTES:
                raise VoiceError("语音模型 SHA-256 校验失败，请稍后重试。")
            extraction = asyncio.create_task(asyncio.to_thread(self._install_archive, temporary))
            try:
                await asyncio.shield(extraction)
            except asyncio.CancelledError:
                # Keep temporary files alive until the bounded extraction finishes.
                await asyncio.shield(extraction)
                raise
            with self._lock:
                if not self._closed:
                    self._state = "ready"
                    self._message = "中文语音模型已就绪，麦克风未开启。"
                    self._progress = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 -- download failures stay in service status
            with self._lock:
                if not self._closed:
                    self._state = "error"
                    self._message = str(error) if isinstance(error, VoiceError) else (
                        "中文语音模型下载失败，请检查网络后重试。"
                    )
                    self._progress = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if self._download_client is None:
                await client.aclose()

    def _install_archive(self, archive: Path) -> None:
        with tempfile.TemporaryDirectory(prefix=".vosk-stage-", dir=self.model_dir.parent) as temp:
            candidate = _extract_archive(archive, Path(temp))
            with self._lock:
                if self._closed:
                    return
                if _model_ready(self.model_dir):
                    return
                if self.model_dir.exists():
                    raise VoiceError("语音模型目录已有不完整文件，请移走后重新下载。")
                os.replace(candidate, self.model_dir)

    def _load_capture(self) -> tuple[Any, Callable[..., Any], dict[str, Any]]:
        if self._device_missing:
            raise VoiceError("上次选择的麦克风已不可用，请刷新设备列表并重新选择。")
        model_loader = self._model_loader
        recognizer_factory = self._recognizer_factory
        stream_factory = self._stream_factory
        device_query = self._device_query
        if model_loader is None or recognizer_factory is None:
            import vosk

            vosk.SetLogLevel(-1)
            model_loader = model_loader or vosk.Model
            recognizer_factory = recognizer_factory or vosk.KaldiRecognizer
        if stream_factory is None or device_query is None:
            import sounddevice

            stream_factory = stream_factory or sounddevice.RawInputStream
            device_query = device_query or (lambda: sounddevice.query_devices(kind="input"))
        try:
            if self._selected_device is None:
                info = device_query()
            else:
                if self._device_query is not None:
                    info = self._device_query(self._selected_device)
                else:
                    import sounddevice
                    info = sounddevice.query_devices(self._selected_device, kind="input")
        except Exception as exc:
            raise VoiceError("无法读取所选麦克风。默认设备可能不可用，请刷新并选择输入设备。") from exc
        sample_rate = float(info.get("default_samplerate", 16000))
        if not 8000 <= sample_rate <= 192000 or info.get("max_input_channels", 1) < 1:
            raise VoiceError("系统默认设备不是可用的麦克风。")
        if self._model is None:
            self._model = model_loader(str(self.model_dir))
        recognizer = recognizer_factory(self._model, sample_rate)
        return recognizer, stream_factory, {**info, "sample_rate": sample_rate}

    async def start(self) -> dict[str, Any]:
        # A start already queued before stop() cannot reopen the microphone later.
        with self._lock:
            requested_generation = self._generation
        async with self._start_lock:
            with self._lock:
                if self._closed:
                    raise VoiceError("语音服务正在关闭。")
                if requested_generation != self._generation:
                    return self.status()
                if self._stopping:
                    raise VoiceError("麦克风正在关闭，请稍后重新开启监听。")
                if self._stream is not None:
                    return self.status()
                if not self._available or not _model_ready(self.model_dir):
                    raise VoiceError("请先完成中文语音模型安装。")
                generation = self._generation
                self._partial = ""
                self._level = self._peak = 0.0
                self._overflows = 0
                self._last_audio_at = None
                self._error_code = None
                self._message = "正在打开系统默认麦克风。"
            stream = None
            try:
                if self._device_preference:
                    await self.devices()
                recognizer, stream_factory, info = await asyncio.to_thread(self._load_capture)
                with self._lock:
                    if self._closed or generation != self._generation:
                        return self.status()
                audio: queue.Queue[bytes | None] = queue.Queue(maxsize=32)
                capture_stop = threading.Event()

                def callback(data, frames, time, status):
                    if capture_stop.is_set():
                        return
                    if status:
                        if getattr(status, "input_overflow", False):
                            # Reset the incomplete sentence across lost samples, then keep listening.
                            with self._lock:
                                if generation == self._generation:
                                    self._overflows += 1
                                    self._message = "音频曾短暂溢出，已重新开始当前句；若频繁出现请检查设备或负载。"
                            try:
                                audio.put_nowait(None)
                            except queue.Full:
                                self._capture_fault(generation, capture_stop, "语音识别积压，请重新开启。", "audio_backlog")
                                return
                        else:
                            self._capture_fault(generation, capture_stop, "麦克风音频中断，请检查设备后重新开启。", "audio_device_fault")
                            return
                    if len(data) > 384_000:
                        self._capture_fault(generation, capture_stop, "麦克风音频块异常，已停止监听。")
                        return
                    block = bytes(data)
                    try:
                        samples = memoryview(block[:len(block) - len(block) % 2]).cast("h")
                        rms = math.sqrt(sum(value * value for value in samples) / len(samples)) if samples else 0
                        peak = max((abs(value) for value in samples), default=0)
                        with self._lock:
                            if generation == self._generation:
                                self._level = round(min(1.0, rms / 32768), 4)
                                self._peak = round(min(1.0, peak / 32768), 4)
                                self._last_audio_at = datetime.now(UTC).isoformat()
                        audio.put_nowait(block)
                    except queue.Full:
                        self._capture_fault(generation, capture_stop, "语音识别积压，已停止监听，请重新开启。", "audio_backlog")

                stream = stream_factory(
                    **({"device": self._selected_device} if self._selected_device is not None else {}),
                    samplerate=info["sample_rate"],
                    blocksize=max(400, int(info["sample_rate"] / 5)),
                    channels=1,
                    dtype="int16",
                    callback=callback,
                )
                with self._lock:
                    stale = (
                        self._closed or generation != self._generation or capture_stop.is_set()
                    )
                    if not stale:
                        self._stream = stream
                        self._capture_stop = capture_stop
                        self._device = str(info.get("name", "系统默认麦克风"))[:200]
                if stale:
                    with self._lock:
                        if self._stream is stream:
                            self._stream = None
                    await asyncio.to_thread(self._close_stream, stream)
                    return self.status()
                opening = asyncio.create_task(asyncio.to_thread(stream.start))
                try:
                    await asyncio.shield(opening)
                except asyncio.CancelledError:
                    await asyncio.shield(opening)
                    await self.stop()
                    raise
                with self._lock:
                    stale = (
                        self._closed or generation != self._generation or capture_stop.is_set()
                    )
                    if not stale:
                        self._state = "listening"
                        self._message = "正在本地监听中文语音；停止监听可立即关闭麦克风。"
                        self._worker = threading.Thread(
                            target=self._recognize,
                            args=(generation, stream, recognizer, audio, capture_stop),
                            name="kingdom-offline-speech",
                            daemon=True,
                        )
                        self._worker.start()
                if stale:
                    with self._lock:
                        if self._stream is stream:
                            self._stream = None
                    await asyncio.to_thread(self._close_stream, stream)
                return self.status()
            except Exception as error:  # noqa: BLE001 -- native audio failures must close stream
                with self._lock:
                    if self._stream is stream:
                        self._stream = None
                    if generation == self._generation and not self._closed:
                        self._state = "error"
                        self._error_code = "input_open_failed"
                        self._message = str(error) if isinstance(error, VoiceError) else (
                            "无法打开默认麦克风，请检查 Windows 麦克风权限、设备或占用情况。"
                        )
                if stream is not None:
                    await asyncio.to_thread(self._close_stream, stream)
                raise VoiceError(self._message) from None

    def _capture_fault(self, generation: int, capture_stop: threading.Event, message: str,
                       code: str = "audio_fault") -> None:
        with self._lock:
            if generation == self._generation:
                self._state = "error"
                self._message = message
                self._partial = ""
                self._error_code = code
                self._level = self._peak = 0.0
        capture_stop.set()

    def _recognize(self, generation, stream, recognizer, audio, capture_stop) -> None:
        try:
            while not capture_stop.is_set():
                try:
                    block = audio.get(timeout=0.1)
                except queue.Empty:
                    continue
                if block is None:
                    recognizer.Reset()
                    with self._lock:
                        if generation == self._generation:
                            self._partial = ""
                    continue
                final = recognizer.AcceptWaveform(block)
                text = _clean_text(
                    recognizer.Result() if final else recognizer.PartialResult(),
                    "text" if final else "partial",
                )
                with self._lock:
                    if capture_stop.is_set() or generation != self._generation:
                        break
                    self._partial = "" if final else text
                    if final and text:
                        self._seq += 1
                        self._events.append({
                            "seq": self._seq,
                            "text": text,
                            "captured_at": datetime.now(UTC).isoformat(),
                        })
        except Exception:  # noqa: BLE001 -- exceptions must never escape an audio worker
            self._capture_fault(generation, capture_stop, "本地语音识别失败，已关闭麦克风，请重新开启。", "recognition_failed")
        finally:
            with self._lock:
                owns_stream = self._stream is stream and generation == self._generation
                if owns_stream:
                    self._stream = None
            if owns_stream and not self._close_stream(stream):
                with self._lock:
                    if generation == self._generation and self._stream is None:
                        self._stream = stream
                        self._state = "error"
                        self._message = "未能确认麦克风关闭，请重试停止监听或退出应用。"

    @staticmethod
    def _close_stream(stream) -> bool:
        try:
            stream.abort()
        except Exception:  # noqa: BLE001 -- close must still be attempted after abort fails
            logging.getLogger(__name__).debug("Microphone abort failed; trying close.")
        try:
            stream.close()
            return True
        except Exception:  # noqa: BLE001 -- a native close failure cannot crash the audio worker
            logging.getLogger(__name__).warning("Microphone close failed.")
            return False

    async def stop(self) -> dict[str, Any]:
        with self._lock:
            self._generation += 1
            self._stopping += 1
            self._capture_stop.set()
            stream, self._stream = self._stream, None
            worker, self._worker = self._worker, None
            self._partial = ""
            self._level = self._peak = 0.0
            if self._state != "downloading":
                self._state = "ready" if _model_ready(self.model_dir) else "needs_model"
                if not self._available:
                    self._state = "unavailable"
                self._message = "麦克风已关闭。"
        try:
            if stream is not None:
                closing = asyncio.create_task(asyncio.to_thread(self._close_stream, stream))
                try:
                    closed = await asyncio.shield(closing)
                except asyncio.CancelledError:
                    # The underlying native close must finish even if its caller leaves.
                    closed = await asyncio.shield(closing)
                    if not closed:
                        with self._lock:
                            self._stream = stream
                            self._state = "error"
                            self._message = "未能确认麦克风关闭，请重试停止监听或退出应用。"
                    raise
                if not closed:
                    with self._lock:
                        if self._stream is None:
                            self._stream = stream
                            self._state = "error"
                            self._message = "未能确认麦克风关闭，请重试停止监听或退出应用。"
            if worker is not None:
                await asyncio.to_thread(worker.join, 1.0)
        finally:
            with self._lock:
                self._stopping -= 1
        return self.status()

    async def close(self) -> None:
        with self._lock:
            self._closed = True
            download = self._download_task
        await self.stop()
        if download is not None and not download.done():
            download.cancel()
            try:
                await download
            except asyncio.CancelledError:
                pass
