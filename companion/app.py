"""Loopback UI/API with a per-process same-origin session credential."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from companion.bridge import BridgeClient, BridgeError
from companion.config import ConfigError, ExecutionSettings, ModelConfigStore, ModelSettings
from companion.contracts import Decision, StartRequest
from companion.controller import ActionJournal, ControlError, ControlManager
from companion.conversation import Conversation, quick_intent
from companion.dialogue_overlay import DialogueOverlay
from companion.game_installation import (
    detect_games,
    installed_bridge_config,
    launch_game,
    validate_game_directory,
)
from companion.installation_store import InstallationStore
from companion.model import ModelClient, ModelError
from companion.notices import NoticeService
from companion.speech import SpeechError, SpeechService
from companion.voice import VoiceError, VoiceService

STATIC = Path(__file__).parent / "static"


class AcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirmed: bool


class DirectoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, max_length=2000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=1000)

    @field_validator("text")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty message")
        return value.strip()


class OllamaDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    endpoint: str = Field(default="http://127.0.0.1:11434", max_length=2000)

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value):
        return ExecutionSettings(endpoint=value).endpoint


class SpeechSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    voice_name: str | None = Field(default=None, max_length=200)
    headphones: bool = False


class NoticeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class VoiceDeviceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    device: int | None = Field(default=None, ge=0)


def create_app(*, bridge_config: Path | None = None, store: ModelConfigStore | None = None,
               bridge: BridgeClient | None = None, model: ModelClient | None = None,
               journal: ActionJournal | None = None, voice: VoiceService | None = None,
               speech: SpeechService | None = None) -> FastAPI:
    store = store or ModelConfigStore()
    bridge = bridge or BridgeClient(bridge_config)
    model = model or ModelClient(store)
    manager = ControlManager(bridge, model, store, journal)
    voice = voice or VoiceService(settings_path=store.path.with_name("voice.local.json"))
    speech = speech or SpeechService(listener_active=lambda: voice.status()["state"] == "listening")
    overlay = DialogueOverlay(bridge, state_reader=lambda: manager.latest_state)

    async def on_reply(message: dict) -> None:
        if message.get("source") != "proactive":
            notices.dialogue_activity()
        await overlay.publish(message)
        await speech.say(message, listening=voice.status()["state"] == "listening")

    conversation = Conversation(manager, voice, on_reply=on_reply, on_voice_activity=speech.stop)
    notices = NoticeService(manager, conversation)
    installation = InstallationStore(store.path.with_name("installation.local.json"))
    if bridge_config is not None:
        try:
            installation.game = validate_game_directory(bridge_config.parents[2])
        except (OSError, ValueError, IndexError):
            pass
    elif installation.game and bridge.config_path is None:
        bridge.config_path = installed_bridge_config(installation.game)
    session_token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            await speech.prepare()
            yield
        finally:
            await manager.prepare_shutdown()
            await notices.close()
            await speech.close()
            await conversation.close()
            await overlay.close()
            await manager.close()

    app = FastAPI(title="王国 AI 陪玩", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.manager, app.state.store = manager, store
    app.state.conversation, app.state.voice = conversation, voice
    app.state.speech, app.state.notices = speech, notices
    app.state.overlay = overlay
    app.state.request_shutdown = None

    def boundary(request: Request) -> None:
        parsed = urlsplit(str(request.url))
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise HTTPException(403, "仅允许本机访问")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "禁止其他网页调用本机控制接口")
        origin = request.headers.get("origin")
        if origin and origin != f"{parsed.scheme}://{parsed.netloc}":
            raise HTTPException(403, "页面来源不匹配")

    def authenticated(request: Request) -> None:
        boundary(request)
        value = request.headers.get("x-kingdom-session", "")
        if not value.isascii() or not secrets.compare_digest(value, session_token):
            raise HTTPException(403, "页面会话失效，请刷新页面")
        if (request.method not in {"GET", "HEAD", "OPTIONS"}
                and request.url.path not in {"/api/control/stop", "/api/voice/stop",
                                             "/api/speech/stop", "/api/shutdown"}):
            manager.ensure_accepting()

    @app.middleware("http")
    async def secure_response(request: Request, call_next):
        try:
            boundary(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        size = request.headers.get("content-length")
        if size and (not size.isdigit() or int(size) > 16_000):
            return JSONResponse({"detail": "请求过大"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        # Pydantic's default response contains input values, including rejected API keys.
        return JSONResponse({"detail": "请求格式无效，请核对字段与数值范围"}, status_code=422)

    @app.exception_handler(ControlError)
    @app.exception_handler(BridgeError)
    async def control_error(_request: Request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ModelError)
    @app.exception_handler(ConfigError)
    @app.exception_handler(VoiceError)
    @app.exception_handler(SpeechError)
    async def model_error(_request: Request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/app.js")
    async def javascript():
        return FileResponse(STATIC / "app.js", media_type="text/javascript")

    @app.get("/conversation.js")
    async def conversation_javascript():
        return FileResponse(STATIC / "conversation.js", media_type="text/javascript")

    @app.get("/style.css")
    async def stylesheet():
        return FileResponse(STATIC / "style.css", media_type="text/css")

    @app.get("/api/session")
    async def session():
        return {"token": session_token}

    protected = [Depends(authenticated)]

    @app.get("/api/status", dependencies=protected)
    async def status():
        state, error = None, None
        try:
            state = (await manager._state()).model_dump(mode="json")
        except BridgeError as exc:
            error = str(exc)
        return {"bridge": {"connected": state is not None, "state": state, "error": error},
                "control": manager.status(), "model": {**store.public(),
                    "usage": model.usage_status() if hasattr(model, "usage_status") else None},
                "validation": {"real_game_verified": False,
                               "notice": "开发预览：尚未完成真实存档与另一台电脑的陪玩验收"}}

    @app.get("/api/model", dependencies=protected)
    async def model_config():
        return store.public()

    @app.put("/api/model", dependencies=protected)
    async def update_model(value: ModelSettings):
        if manager.status()["running"]:
            raise ControlError("请先停止游玩再修改模型配置")
        return store.update(value)

    @app.post("/api/model/test", dependencies=protected)
    async def test_model():
        if manager.status()["running"]:
            raise ControlError("请先停止游玩再测试模型连接")
        await model.test()
        return {"connected": True, "message": "模型已返回合法 stop 测试指令；未执行游戏操作"}

    @app.get("/api/execution", dependencies=protected)
    async def execution_config():
        return store.public()["execution"]

    @app.put("/api/execution", dependencies=protected)
    async def update_execution(value: ExecutionSettings):
        if manager.status()["running"]:
            raise ControlError("请先停止游玩再切换执行模型")
        return store.update_execution(value)

    @app.post("/api/execution/models", dependencies=protected)
    async def execution_models(value: OllamaDiscoveryRequest):
        return await model.list_execution_models(value.endpoint)

    @app.post("/api/execution/test", dependencies=protected)
    async def test_execution():
        if manager.status()["running"]:
            raise ControlError("请先停止游玩再测试执行模型")
        await model.test_execution()
        return {"connected": True, "message": "执行模型已返回合法 stop 指令；未操作游戏"}

    @app.post("/api/control/start", dependencies=protected)
    async def start(value: StartRequest):
        conversation.cancel_actions()
        conversation.cancel_voice()
        return await manager.start(value)

    @app.post("/api/control/stop", dependencies=protected)
    async def stop():
        conversation.cancel_actions()
        conversation.cancel_voice()
        result = await manager.stop()
        await speech.stop()
        return result

    @app.post("/api/control/manual", dependencies=protected)
    async def manual(value: Decision):
        conversation.cancel_actions()
        conversation.cancel_voice()
        return await manager.manual(value)

    @app.get("/api/chat", dependencies=protected)
    async def chat_history():
        return conversation.public()

    @app.get("/api/dialogue/status", dependencies=protected)
    async def dialogue_status():
        return overlay.status()

    @app.post("/api/chat", dependencies=protected)
    async def chat(value: ChatRequest):
        notices.dialogue_activity()
        if quick_intent(value.text) == "stop":
            # Releasing game input has priority over waiting for an audio process to exit.
            result = await conversation.send(value.text)
            await speech.stop()
            return result
        await speech.stop()
        return await conversation.send(value.text)

    @app.get("/api/speech/status", dependencies=protected)
    async def speech_status():
        return speech.status()

    @app.put("/api/speech/settings", dependencies=protected)
    async def speech_settings(value: SpeechSettings):
        return await speech.configure(value.enabled, value.voice_name, value.headphones)

    @app.post("/api/speech/stop", dependencies=protected)
    async def speech_stop():
        return await speech.stop()

    @app.get("/api/notices/status", dependencies=protected)
    async def notice_status():
        return notices.status()

    @app.put("/api/notices/settings", dependencies=protected)
    async def notice_settings(value: NoticeSettings):
        return await notices.configure(value.enabled)

    @app.get("/api/voice/status", dependencies=protected)
    async def voice_status():
        return voice.status()

    @app.get("/api/voice/devices", dependencies=protected)
    async def voice_devices():
        return await voice.devices()

    @app.put("/api/voice/device", dependencies=protected)
    async def voice_device(value: VoiceDeviceSettings):
        return await voice.select_device(value.device)

    @app.get("/api/voice/events", dependencies=protected)
    async def voice_events(after: int = 0):
        if after < 0:
            raise HTTPException(422, "语音游标无效")
        return voice.events(after)

    @app.post("/api/voice/prepare", dependencies=protected)
    async def prepare_voice():
        return await voice.prepare_model()

    @app.post("/api/voice/start", dependencies=protected)
    async def start_voice():
        if not speech.status()["headphones"]:
            await speech.stop()
        return await conversation.start_voice()

    @app.post("/api/voice/stop", dependencies=protected)
    async def stop_voice():
        conversation.cancel_voice()
        return await voice.stop()

    @app.post("/api/control/acknowledge", dependencies=protected)
    async def acknowledge(value: AcknowledgeRequest):
        if not value.confirmed:
            raise ControlError("需要明确确认已在游戏中核对")
        return await manager.acknowledge()

    @app.get("/api/installation", dependencies=protected)
    async def installation_status():
        return installation.public()

    @app.post("/api/installation/detect", dependencies=protected)
    async def detect_installation():
        games = await asyncio.to_thread(detect_games)
        return {"candidates": [{"path": str(game), "bridge_installed": bool(installed_bridge_config(game))}
                               for game in games], **installation.public()}

    @app.put("/api/installation", dependencies=protected)
    async def select_installation(value: DirectoryRequest):
        if manager.status()["running"]:
            raise ControlError("请先停止游玩再修改游戏目录")
        async with manager.pause_control():
            try:
                game = await asyncio.to_thread(installation.select, value.path)
            except (OSError, ValueError) as exc:
                raise ControlError("游戏目录无效，请选择包含 KingdomTwoCrowns.exe 的完整目录") from exc
            bridge.config_path = installed_bridge_config(game)
        return {**installation.public(), "message": "游戏目录已保存" if bridge.config_path
                else "游戏目录已保存，尚未找到桥接配置；请先运行模组安装脚本"}

    @app.post("/api/game/launch", dependencies=protected)
    async def launch_selected_game():
        if manager.status()["running"]:
            raise ControlError("请先停止游玩再启动游戏")
        if installation.game is None:
            raise ControlError("请先检测或选择游戏目录")
        async with manager.pause_control():
            try:
                await bridge.state()
                return {"launched": False, "message": "游戏桥接已连接，请在现有游戏中进入本地合作存档"}
            except BridgeError:
                pass
            manager.ensure_accepting()
            try:
                process = await asyncio.to_thread(launch_game, installation.game)
            except ValueError as exc:
                raise ControlError(str(exc)) from exc
            except OSError as exc:
                raise ControlError("游戏启动失败，请从 Steam 启动并检查目录") from exc
        via_steam = Path(process.args[0]).stem.casefold() == "steam"
        message = ("已通过 Steam 请求启动游戏，请确认客户端已登录且账户拥有该游戏；等待桥接连接后进入本地合作存档。"
                   if via_steam else "已请求启动独立版游戏，请进入本地合作存档。")
        return {"launched": True, "launch_method": "steam" if via_steam else "direct",
                "message": message + " AI 未自动开启；启动请求成功不代表游戏已就绪。"}

    @app.post("/api/game/coop", dependencies=protected)
    async def open_local_coop():
        async with manager.pause_control():
            await bridge.open_coop()
        return {"message": "已请求打开本地合作提示，请在游戏中完成官方确认和外观选择；AI 未自动开启"}

    @app.post("/api/shutdown", dependencies=protected)
    async def shutdown():
        callback = app.state.request_shutdown
        if not callable(callback):
            raise ControlError("此启动方式请在终端按 Ctrl+C 退出后台")
        conversation.cancel_actions()
        conversation.cancel_voice()
        await manager.prepare_shutdown()
        await notices.close()
        await speech.close()
        await voice.stop()
        await overlay.close()
        asyncio.get_running_loop().call_later(0.2, callback)
        return {"message": "已停止 AI，正在退出后台"}

    return app
