"""Authenticated HTTP bridge. Only loopback addresses and explicit config paths."""

from __future__ import annotations

import asyncio
import json
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from companion.contracts import GameState, Receipt

STOP_CONFIRM_TIMEOUT = 3.4
DIALOGUE_CAPABILITY = "dialogue_bubble"
DIALOGUE_TEXT_LIMIT = 600


def _dialogue_string(value: str, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise BridgeError(f"聊天展示的 {field} 必须是文字。")
    cleaned = value.strip()
    try:
        size = len(cleaned.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise BridgeError(f"聊天展示的 {field} 格式无效。") from exc
    if not 1 <= size <= limit or any(
        unicodedata.category(character) == "Cc" and character not in "\n\t"
        for character in cleaned
    ):
        raise BridgeError(f"聊天展示的 {field} 长度或格式无效。")
    return cleaned


class BridgeError(RuntimeError):
    pass


class BridgeClient:
    def __init__(self, config_path: Path | None, client: httpx.AsyncClient | None = None) -> None:
        self.config_path = config_path
        self.client = client or httpx.AsyncClient(timeout=3, follow_redirects=False, trust_env=False)

    def _config(self) -> tuple[str, str]:
        try:
            if self.config_path is None:
                raise BridgeError("尚未指定游戏桥接配置，请使用 --bridge-config")
            if self.config_path.stat().st_size > 16_000:
                raise ValueError("invalid config")
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            url, token = data.get("base_url", "http://127.0.0.1:48861"), data["token"]
            parsed = urlsplit(url)
            if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path not in {"", "/"} or not parsed.port):
                raise ValueError("invalid url")
            if (not isinstance(token, str) or not 20 <= len(token) <= 512
                    or not all(character.isascii() and (character.isalnum() or character in "_-")
                               for character in token)):
                raise ValueError("invalid token")
            return url.rstrip("/"), token
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise BridgeError("桥接配置缺失或无效，请重新运行安装配置程序") from exc

    async def request(self, method: str, path: str, body: dict | None = None) -> dict:
        base_url, token = self._config()
        try:
            response = await self.client.request(method, base_url + path, json=body,
                                                 headers={"Authorization": f"Bearer {token}"})
            if response.status_code >= 400:
                raise BridgeError(f"游戏桥接拒绝请求：HTTP {response.status_code}")
            if len(response.content) > 16_000_000:
                raise ValueError("large response")
            data = response.json()
            if not isinstance(data, dict):
                raise TypeError("invalid response")
            return data
        except httpx.HTTPError as exc:
            raise BridgeError("游戏桥接未连接或超时，请确认游戏及模组已启动") from exc
        except (ValueError, TypeError) as exc:
            raise BridgeError("桥接响应格式无效，请核对组件版本") from exc

    async def state(self) -> GameState:
        try:
            return GameState.model_validate(await self.request("GET", "/state"))
        except ValidationError as exc:
            raise BridgeError("桥接状态缺少必要事实或会话身份") from exc

    async def heartbeat(self, *, control_id: str, session_id: str, control_epoch: int) -> None:
        await self.request("POST", "/heartbeat", {"control_id": control_id,
                                                  "session_id": session_id,
                                                  "control_epoch": control_epoch})

    async def command(self, body: dict) -> Receipt:
        try:
            return Receipt.model_validate(await self.request("POST", "/command", body))
        except ValidationError as exc:
            raise BridgeError("桥接动作回执格式无效") from exc

    async def receipt(self, action_id: str) -> Receipt:
        try:
            return Receipt.model_validate(await self.request("GET", f"/commands/{action_id}"))
        except ValidationError as exc:
            raise BridgeError("桥接动作回执格式无效") from exc

    async def stop(self) -> None:
        try:
            async with asyncio.timeout(STOP_CONFIRM_TIMEOUT):
                await self.request("POST", "/stop", {})
                acknowledged_at = datetime.now(UTC)
                while True:
                    state = await self.state()
                    captured_at = state.captured_at.astimezone(UTC)
                    age = (datetime.now(UTC) - captured_at).total_seconds()
                    # HTTP acknowledgement only queues the main-thread release. A cached
                    # observation predating that acknowledgement cannot prove it completed.
                    if state.input_released and captured_at >= acknowledged_at and -2 <= age <= 2.5:
                        return
                    await asyncio.sleep(0.05)
        except TimeoutError as exc:
            raise BridgeError("停止已请求，但未能读取新的输入释放确认；请在游戏中按 F8 接管") from exc

    async def open_coop(self) -> dict:
        return await self.request("POST", "/coop/open", {})

    async def dialogue(self, *, message_id: str, text: str, session_id: str) -> dict:
        """Queue plain overlay text only; no action, heartbeat or control lease."""
        body = {"message_id": _dialogue_string(message_id, "message_id", 100),
                "text": _dialogue_string(text, "text", DIALOGUE_TEXT_LIMIT),
                "session_id": _dialogue_string(session_id, "session_id", 200)}
        result = await self.request("POST", "/dialogue", body)
        status = result.get("status")
        if not isinstance(status, str) or status not in {"accepted", "duplicate"}:
            raise BridgeError("聊天展示请求结果未确认；不会自动重发。")
        return result

    async def close(self) -> None:
        await self.client.aclose()
