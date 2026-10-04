"""Best-effort, latest-reply display without authority to operate the game.

publish() only reads a cached state and replaces one in-memory pending message.
The worker checks a fresh game session before one authenticated display request.
HTTP acceptance does not prove the game rendered a bubble; unknown results are
never retried. No game command, heartbeat, control lease or audio is involved.
"""

from __future__ import annotations

import asyncio
import unicodedata
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from companion.bridge import DIALOGUE_CAPABILITY, DIALOGUE_TEXT_LIMIT
from companion.contracts import GameState
from companion.gameplay_context import fresh

MAX_SEEN = 128


def _bubble_text(content: str) -> str:
    # Inspect only a small prefix even if a caller supplies unbounded content.
    prefix = content[:DIALOGUE_TEXT_LIMIT + 1].replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "".join(character for character in prefix
                      if unicodedata.category(character) not in {"Cc", "Cs"}
                      or character in "\n\t").strip()
    units = sum(2 if ord(character) > 0xFFFF else 1 for character in cleaned)
    truncated = len(content) > DIALOGUE_TEXT_LIMIT + 1 or units > DIALOGUE_TEXT_LIMIT
    budget = DIALOGUE_TEXT_LIMIT - 1 if truncated else DIALOGUE_TEXT_LIMIT
    result, size = [], 0
    for character in cleaned:
        width = 2 if ord(character) > 0xFFFF else 1
        if size + width > budget:
            truncated = True
            break
        result.append(character)
        size += width
    text = "".join(result).rstrip()
    return text + "…" if text and truncated else text


@dataclass(frozen=True)
class _Message:
    message_id: str | int
    wire_id: str
    text: str
    session_id: str


class DialogueOverlay:
    def __init__(self, bridge, state_reader: Callable[[], GameState | None],
                 *, request_timeout: float = 3.0):
        if request_timeout <= 0:
            raise ValueError("Dialogue request timeout must be positive.")
        self.bridge = bridge
        self._state_reader = state_reader
        self._request_timeout = request_timeout
        self._prefix = uuid.uuid4().hex + ":"
        self._pending: _Message | None = None
        self._worker: asyncio.Task | None = None
        self._seen: deque[tuple[str, str]] = deque(maxlen=MAX_SEEN)
        self._closed = False
        self._sending = False
        self._error: str | None = None
        self._message = "游戏内聊天展示等待桥接提供 dialogue_bubble 能力。"
        self._sent_message_id: str | int | None = None
        self._sent_count = 0

    def _cached_state(self) -> GameState | None:
        try:
            state = self._state_reader()
            return state if isinstance(state, GameState) else None
        except Exception:  # noqa: BLE001 -- cached display failures cannot affect a reply
            return None

    @staticmethod
    def _supports(state: GameState | None) -> bool:
        return bool(state is not None and state.ready and state.coop
                    and state.controlled_player_id == 1 and state.player(1) is not None
                    and DIALOGUE_CAPABILITY in state.capabilities)

    @classmethod
    def _available(cls, state: GameState | None) -> bool:
        return cls._supports(state) and fresh(state)

    def status(self) -> dict:
        state = self._cached_state()
        message = self._message
        if self._closed:
            reason = "closed"
        elif state is None:
            reason = "disconnected"
            message = "尚未读取游戏会话；完整回复保留在聊天页面。"
        elif DIALOGUE_CAPABILITY not in state.capabilities:
            reason = "unsupported"
            message = "当前游戏桥接未提供聊天气泡能力；请更新模组后使用。"
        elif not fresh(state):
            reason = "stale"
            message = "游戏状态已过期；等待当前游戏会话后转发新的回复。"
        elif not self._available(state):
            reason = "not_ready"
            message = "游戏中的 P2 尚未就绪；完整回复保留在聊天页面。"
        else:
            reason = "available"
        return {"available": not self._closed and self._available(state),
                "reason": reason,
                "queued": self._pending is not None, "sending": self._sending,
                "sent_message_id": self._sent_message_id, "sent_count": self._sent_count,
                "shown": None, "error": self._error,
                "last_error": self._message if self._error else None,
                "message": message}

    async def publish(self, message: dict) -> dict:
        """Replace one pending reply; never await a game or network operation."""
        if (self._closed or not isinstance(message, dict)
                or message.get("role") != "assistant" or message.get("playable") is not True
                or message.get("source") == "system"):
            return self.status()
        message_id, content = message.get("id"), message.get("content")
        if (not isinstance(content, str) or isinstance(message_id, bool)
                or not isinstance(message_id, (str, int))):
            return self.status()
        if isinstance(message_id, int):
            if not 0 <= message_id <= 2**63 - 1:
                return self.status()
            identity = str(message_id)
        else:
            message_id = message_id.strip()
            try:
                size = len(message_id.encode("utf-16-le")) // 2
            except UnicodeEncodeError:
                return self.status()
            if not 1 <= size <= 64 or any(unicodedata.category(char) == "Cc"
                                          for char in message_id):
                return self.status()
            identity = message_id
        state = self._cached_state()
        # A slow model reply can outlive the cached observation. Bind that known
        # session here; the worker must fetch a fresh observation of the same
        # session before sending. No unobserved session is invented at publish.
        if not self._supports(state):
            self._message = "游戏状态尚未就绪；本条回复保留在聊天页面。"
            return self.status()
        key = state.session_id, identity
        if key in self._seen:
            return self.status()
        text = _bubble_text(content)
        if not text:
            return self.status()
        self._seen.append(key)
        self._pending = _Message(message_id, self._prefix + identity, text, state.session_id)
        self._error = None
        self._message = "最新回复已排队等待转发；尚未确认游戏显示。"
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())
        return self.status()

    async def _run(self) -> None:
        worker = asyncio.current_task()
        try:
            while not self._closed and self._pending is not None:
                message, self._pending = self._pending, None
                self._sending = True
                try:
                    state = await asyncio.wait_for(self.bridge.state(), self._request_timeout)
                    if self._closed:
                        return
                    # A newer reply replaces work still waiting for an observation.
                    if self._pending is not None:
                        continue
                    if not self._available(state) or state.session_id != message.session_id:
                        self._message = "游戏会话变化或状态不可用；已跳过旧回复。"
                        continue
                    result = await asyncio.wait_for(self.bridge.dialogue(
                        message_id=message.wire_id, text=message.text,
                        session_id=message.session_id,
                    ), self._request_timeout)
                    if self._closed:
                        return
                    if not isinstance(result, dict) or result.get("status") not in {
                        "accepted", "duplicate"
                    }:
                        raise ValueError("Unconfirmed display request.")
                    self._sent_message_id = message.message_id
                    self._sent_count += 1
                    self._error = None
                    self._message = "游戏桥接已接受文字；实际气泡显示仍需在游戏中核对。"
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 -- no retries or raw config/game details
                    self._error = "send_unconfirmed"
                    self._message = "聊天展示转发未确认；本条不重发，聊天页面保留完整回复。"
                finally:
                    self._sending = False
        finally:
            if self._worker is worker:
                self._worker = None
                self._sending = False

    async def close(self) -> None:
        self._closed = True
        self._pending = None
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self._sending = False
        self._message = "游戏内聊天转发服务已关闭。"
