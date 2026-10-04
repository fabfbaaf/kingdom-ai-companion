"""Opt-in Windows speech output. Construction and status polling are silent.

Only the fixed helper script interprets JSON read from stdin. Text is passed to
SpeechSynthesizer.Speak (plain text), never to a shell or the game controller.
Microsoft API reference: https://learn.microsoft.com/en-us/dotnet/api/
system.speech.synthesis.speechsynthesizer
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import subprocess
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_TEXT = 1200
MAX_SENTENCE_CHARS = 120
MAX_SENTENCES = 32
MAX_QUEUE = 4
MAX_VOICES = 64
MAX_OUTPUT = 32_768
HELPER_PATH = Path(__file__).with_name("speech-helper.ps1")
WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


class SpeechError(RuntimeError):
    pass


class _Interrupted(Exception):
    """A stopped or microphone-suppressed generation must never be replayed."""


def _sentences(content: str) -> tuple[list[str], bool]:
    truncated = len(content) > MAX_TEXT
    text = content[:MAX_TEXT].replace("\x00", "").strip()
    chunks = []
    for sentence in re.findall(r".+?(?:[。！？!?；;\n]+|$)", text, flags=re.DOTALL):
        sentence = re.sub(r"\s+", " ", sentence).strip()
        for start in range(0, len(sentence), MAX_SENTENCE_CHARS):
            if len(chunks) >= MAX_SENTENCES:
                return chunks, True
            chunks.append(sentence[start:start + MAX_SENTENCE_CHARS])
    return chunks, truncated


def _chinese(voice: dict[str, Any]) -> bool:
    culture = voice["culture"].casefold()
    return voice["enabled"] and (culture == "zh" or culture.startswith("zh-"))


def _clean_voices(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > MAX_VOICES:
        raise SpeechError("本机音色列表格式异常。")
    voices = []
    seen = set()
    for value in values:
        if not isinstance(value, dict):
            raise SpeechError("本机音色列表格式异常。")
        name, culture = value.get("name"), value.get("culture")
        if (not isinstance(name, str) or not 1 <= len(name) <= 200
                or not isinstance(culture, str) or not 1 <= len(culture) <= 24
                or not isinstance(value.get("enabled"), bool)):
            raise SpeechError("本机音色列表格式异常。")
        if name not in seen:
            # Device identifiers, registry IDs, descriptions and paths stay private.
            voices.append({"name": name, "culture": culture, "enabled": value["enabled"]})
            seen.add(name)
    return voices


class SpeechService:
    def __init__(
        self,
        *,
        process_factory: Callable[..., Any] | None = None,
        voice_loader: Callable[[], Any] | None = None,
        listener_active: Callable[[], bool] | None = None,
        speech_timeout: float = 30.0,
        voice_timeout: float = 3.0,
        start_timeout: float = 2.0,
    ):
        if min(speech_timeout, voice_timeout, start_timeout) <= 0:
            raise ValueError("Speech timeouts must be positive.")
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._real_processes = process_factory is None
        self._voice_loader = voice_loader
        self._listener_active = listener_active
        self._speech_timeout = speech_timeout
        self._voice_timeout = voice_timeout
        self._start_timeout = start_timeout
        self._enabled = False
        self._headphones = False
        self._speaking = False
        self._voice_name: str | None = None
        self._voices: list[dict[str, Any]] = []
        self._prepared = False
        self._error: str | None = None
        self._message = "语音播报默认关闭；可手动开启 Windows 本地中文音色。"
        self._generation = 0
        self._closed = False
        self._stopping = 0
        self._queue: deque[tuple[int, list[str]]] = deque()
        self._seen: deque[str | int] = deque(maxlen=128)
        self._worker: asyncio.Task | None = None
        self._prepare_task: asyncio.Task | None = None
        self._processes: dict[int, Any] = {}
        self._terminating: dict[int, asyncio.Task] = {}
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._spawn_tasks: set[asyncio.Task] = set()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "headphones": self._headphones,
            "speaking": self._speaking,
            "voice_name": self._voice_name,
            "available_voices": [dict(voice) for voice in self._voices],
            "error": self._error,
            "message": self._message,
            "queued": len(self._queue),
        }

    async def prepare(self) -> dict[str, Any]:
        """Read installed voice metadata; this operation cannot produce audio."""
        if self._closed:
            return self.status()
        if not self._prepared and (self._prepare_task is None or self._prepare_task.done()):
            self._prepare_task = asyncio.create_task(self._load_voices())
        if self._prepare_task is not None:
            await asyncio.shield(self._prepare_task)
        return self.status()

    async def _load_voices(self) -> None:
        try:
            if self._voice_loader is not None:
                if inspect.iscoroutinefunction(self._voice_loader):
                    values = await asyncio.wait_for(self._voice_loader(), self._voice_timeout)
                else:
                    values = await asyncio.wait_for(
                        asyncio.to_thread(self._voice_loader), self._voice_timeout
                    )
                    if inspect.isawaitable(values):
                        values = await asyncio.wait_for(values, self._voice_timeout)
            else:
                if not WINDOWS:
                    raise SpeechError("本地语音播报仅支持 Windows。")
                result = await self._request({"op": "voices"}, self._voice_timeout)
                values = result.get("voices")
            voices = _clean_voices(values)
            if self._closed:
                return
            self._voices = voices
            self._prepared = True
            chinese = [voice for voice in voices if _chinese(voice)]
            if chinese:
                preferred = next((v for v in chinese if v["culture"].casefold() == "zh-cn"),
                                 chinese[0])
                self._voice_name = preferred["name"]
                self._error = None
                self._message = "本地中文音色已就绪；语音播报关闭。"
            else:
                self._voice_name = None
                self._enabled = False
                self._error = "chinese_voice_unavailable"
                self._message = "Windows System.Speech 未发现可用中文音色；本地中文播报不可用。"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- no raw OS paths or exception text in public state
            if not self._closed:
                self._enabled = False
                self._error = "voice_discovery_failed"
                self._message = "无法读取 Windows 本地音色；语音播报不可用。"

    async def configure(self, enabled: bool, voice_name: str | None = None,
                        headphones: bool = False) -> dict[str, Any]:
        if not isinstance(enabled, bool) or not isinstance(headphones, bool):
            raise SpeechError("语音播报设置格式不正确。")
        if voice_name is not None and (not isinstance(voice_name, str)
                                       or not 1 <= len(voice_name) <= 200):
            raise SpeechError("请选择列表中的本地中文音色。")
        if self._closed:
            raise SpeechError("语音播报服务已关闭。")
        # Every configuration invalidates pending work, including delayed startup.
        self._enabled = False
        generation = self._generation + 1
        await self.stop()
        if self._closed or generation != self._generation:
            return self.status()
        self._headphones = headphones
        if not enabled:
            self._message = "语音播报已关闭。"
            return self.status()
        await self.prepare()
        if self._closed or generation != self._generation:
            return self.status()
        chosen = voice_name or self._voice_name
        voice = next((v for v in self._voices if v["name"] == chosen and _chinese(v)), None)
        if voice is None:
            self._error = ("voice_unavailable" if voice_name else
                           self._error or "chinese_voice_unavailable")
            self._message = "所选中文音色不可用，请选择 Windows 实际安装且可用的中文音色。"
            return self.status()
        self._voice_name = voice["name"]
        self._enabled = True
        self._error = None
        self._message = ("本地语音播报已开启；耳机模式允许边听边播，不提供回声消除。"
                         if headphones else "本地语音播报已开启；麦克风监听期间暂停播报。")
        return self.status()

    def _listening(self, hint: bool = False) -> bool:
        try:
            return bool(hint or (self._listener_active is not None and self._listener_active()))
        except Exception:  # noqa: BLE001 -- uncertain microphone state suppresses audio
            return True

    async def say(self, message: dict, listening: bool = False) -> dict[str, Any]:
        """Queue a bounded assistant reply, without executing its text as an action."""
        if (self._closed or self._stopping or not self._enabled or not isinstance(message, dict)
                or message.get("source") == "system"
                or message.get("role", "assistant") != "assistant"):
            return self.status()
        content, message_id = message.get("content"), message.get("id")
        if (not isinstance(content, str) or isinstance(message_id, bool)
                or not isinstance(message_id, (str, int))
                or (isinstance(message_id, str) and len(message_id) > 128)
                or message_id in self._seen):
            return self.status()
        if not self._headphones and self._listening(listening):
            await self.stop()
            self._message = "麦克风监听期间暂停播报；关闭监听后播报新的回复。"
            return self.status()
        sentences, truncated = _sentences(content)
        if not sentences:
            return self.status()
        if len(self._queue) >= MAX_QUEUE:
            self._message = "播报队列已满，已跳过这条回复。"
            return self.status()
        self._seen.append(message_id)
        self._queue.append((self._generation, sentences))
        self._message = "回复较长，仅播报有界的开头部分。" if truncated else "回复已加入本地播报队列。"
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())
        return self.status()

    def _allowed(self, generation: int) -> bool:
        return (not self._closed and self._enabled and generation == self._generation
                and (self._headphones or not self._listening()))

    async def _work(self) -> None:
        task = asyncio.current_task()
        try:
            while self._queue:
                generation, sentences = self._queue.popleft()
                for text in sentences:
                    if not self._allowed(generation):
                        raise _Interrupted()
                    self._speaking = True
                    await self._request(
                        {"op": "say", "voice_name": self._voice_name, "text": text},
                        self._speech_timeout, generation=generation,
                    )
                self._speaking = False
        except _Interrupted:
            self._queue.clear()
            if not self._closed:
                self._message = "播报已停止；麦克风监听期间暂停播报。"
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 -- do not expose user text or OS details
            self._queue.clear()
            self._enabled = False
            self._generation += 1
            self._error = "speech_timeout" if isinstance(error, TimeoutError) else "speech_failed"
            self._message = "本地播报超时，已停止。" if isinstance(error, TimeoutError) else (
                "Windows 本地播报失败，已关闭；可检查本地音色后重新开启。"
            )
        finally:
            if self._worker is task:
                self._speaking = False
                self._worker = None

    def _process_options(self) -> dict[str, Any]:
        options = {"stdin": asyncio.subprocess.PIPE, "stdout": asyncio.subprocess.PIPE,
                   "stderr": asyncio.subprocess.DEVNULL, "limit": MAX_OUTPUT + 1}
        if WINDOWS:
            options["creationflags"] = CREATE_NO_WINDOW
        return options

    async def _spawn(self) -> Any:
        system_root = Path(os.environ.get("SystemRoot", "C:/Windows"))
        executable = system_root / "System32/WindowsPowerShell/v1.0/powershell.exe"
        # This is bundled, fixed source, never generated from a message or a
        # voice name. -Command avoids changing the user's script execution policy.
        helper_source = HELPER_PATH.read_text(encoding="utf-8")
        async def create():
            result = self._process_factory(
                str(executable), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-Command", helper_source,
                **self._process_options()
            )
            return await result if inspect.isawaitable(result) else result

        spawning = asyncio.create_task(create())
        self._spawn_tasks.add(spawning)
        spawning.add_done_callback(self._spawn_tasks.discard)
        try:
            return await asyncio.wait_for(asyncio.shield(spawning), self._start_timeout)
        except (asyncio.CancelledError, TimeoutError):
            # A late process receives no JSON, hence cannot start speaking. Reap it
            # when it appears, even after the queued generation has been stopped.
            def discard(future):
                if future.cancelled() or future.exception() is not None:
                    return
                cleanup = asyncio.create_task(self._terminate(future.result()))
                self._cleanup_tasks.add(cleanup)
                cleanup.add_done_callback(self._cleanup_tasks.discard)
            spawning.add_done_callback(discard)
            raise

    async def _request(self, payload: dict, timeout: float,
                       generation: int | None = None) -> dict[str, Any]:
        process = await self._spawn()
        self._processes[id(process)] = process
        communication = None
        try:
            if self._closed or (generation is not None and not self._allowed(generation)):
                raise _Interrupted()
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            communication = asyncio.create_task(process.communicate(encoded))
            deadline = asyncio.get_running_loop().time() + timeout
            while not communication.done():
                if self._closed or (generation is not None and not self._allowed(generation)):
                    raise _Interrupted()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError()
                await asyncio.wait({communication}, timeout=min(0.05, remaining))
            output, _stderr = communication.result()
            if process.returncode != 0 or len(output) > MAX_OUTPUT:
                raise SpeechError("本地播报进程失败。")
            result = json.loads(output.decode("utf-8-sig"))
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise SpeechError("本地播报进程返回异常。")
            return result
        finally:
            if process.returncode is None:
                await self._terminate(process)
            if communication is not None and not communication.done():
                communication.cancel()
                await asyncio.gather(communication, return_exceptions=True)
            self._processes.pop(id(process), None)

    async def _terminate(self, process: Any) -> None:
        key = id(process)
        if key not in self._terminating:
            self._terminating[key] = asyncio.create_task(self._kill_tree(process))
        task = self._terminating[key]
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise
        finally:
            if task.done() and self._terminating.get(key) is task:
                self._terminating.pop(key, None)

    async def _kill_tree(self, process: Any) -> None:
        if process.returncode is not None:
            return
        if self._real_processes and WINDOWS:
            # /T targets only this owned PID and its descendants. Never kill by
            # image name: unrelated PowerShell processes must remain untouched.
            taskkill = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe"
            killer = None
            try:
                killer = await asyncio.wait_for(asyncio.create_subprocess_exec(
                    str(taskkill), "/PID", str(process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                ), 2.0)
                await asyncio.wait_for(killer.wait(), 2.0)
            except (OSError, TimeoutError):
                if killer is not None and killer.returncode is None:
                    killer.kill()
                    await killer.wait()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), 2.0)
        except TimeoutError:
            pass

    async def stop(self) -> dict[str, Any]:
        self._generation += 1
        self._queue.clear()
        self._stopping += 1
        worker = self._worker
        try:
            if worker is not None and not worker.done():
                worker.cancel()
                cleanup = asyncio.ensure_future(asyncio.gather(worker, return_exceptions=True))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await asyncio.shield(cleanup)
                    raise
            self._speaking = False
            self._message = "播报已停止；队列已清空。"
        finally:
            self._stopping -= 1
        return self.status()

    async def close(self) -> None:
        self._closed = True
        self._enabled = False
        await self.stop()
        if self._prepare_task is not None and not self._prepare_task.done():
            self._prepare_task.cancel()
            await asyncio.gather(self._prepare_task, return_exceptions=True)
        for process in list(self._processes.values()):
            await self._terminate(process)
        if self._spawn_tasks:
            await asyncio.wait(self._spawn_tasks, timeout=self._start_timeout)
            # Let late-spawn discard callbacks register their process cleanup.
            await asyncio.sleep(0)
        if self._cleanup_tasks:
            await asyncio.gather(*self._cleanup_tasks, return_exceptions=True)
        self._message = "语音播报服务已关闭。"
