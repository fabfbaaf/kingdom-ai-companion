"""Local API integration for parallel dialogue, optional speech and notices."""

import asyncio

import httpx
from test_companion import FakeBridge, store_for
from test_conversation import ChatModel, FakeVoice

from companion.app import create_app
from companion.controller import ActionJournal


class Speech:
    def __init__(self):
        self.enabled = False
        self.headphones = False
        self.messages = []
        self.stops = 0
        self.closed = False
        self.gate = None
        self.entered = asyncio.Event()

    def status(self):
        return {"enabled": self.enabled, "headphones": self.headphones,
                "speaking": False, "voice_name": "Synthetic Chinese",
                "available_voices": [{"name": "Synthetic Chinese", "culture": "zh-CN",
                                      "enabled": True}], "error": None, "queued": 0}

    async def prepare(self):
        return self.status()

    async def configure(self, enabled, voice_name=None, headphones=False):
        self.enabled, self.headphones = enabled, headphones
        return self.status()

    async def say(self, message, listening=False):
        if self.enabled and (self.headphones or not listening):
            self.messages.append(message)
        return self.status()

    async def stop(self):
        self.stops += 1
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        return self.status()

    async def close(self):
        self.closed = True
        self.enabled = False


def make_app(tmp_path, speech):
    return create_app(store=store_for(tmp_path), bridge=FakeBridge(), voice=FakeVoice(),
                      model=ChatModel(), speech=speech,
                      journal=ActionJournal(tmp_path / "session.local.json"))


def test_speech_and_notices_are_protected_opt_in_and_dialogue_only(tmp_path):
    async def scenario():
        speech = Speech()
        app = make_app(tmp_path, speech)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:48860") as client:
            for path in ("/api/speech/status", "/api/notices/status"):
                assert (await client.get(path)).status_code == 403
            client.headers["X-Kingdom-Session"] = (await client.get("/api/session")).json()["token"]
            assert (await client.get("/api/speech/status")).json()["enabled"] is False
            assert (await client.get("/api/notices/status")).json()["enabled"] is False
            assert (await client.put("/api/speech/settings", json={"enabled": "true"})).status_code == 422
            result = await client.put("/api/speech/settings", json={"enabled": True})
            assert result.status_code == 200
            assert (await client.post("/api/chat", json={"text": "你好"})).status_code == 200
            assert len(speech.messages) == 1
            assert not app.state.manager.bridge.commands
            assert (await client.post("/api/speech/stop")).status_code == 200
            assert app.state.manager.mode == "idle"
            history = (await client.get("/api/chat")).json()
            assert history["busy"] is False and history["queued"] == 0
            assert "current_action" in history["context"]
        await app.state.conversation.close()

    asyncio.run(scenario())


def test_text_stop_releases_game_before_waiting_for_audio_exit(tmp_path):
    async def scenario():
        speech = Speech()
        app = make_app(tmp_path, speech)
        speech.gate = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:48860") as client:
            client.headers["X-Kingdom-Session"] = (await client.get("/api/session")).json()["token"]
            pending = asyncio.create_task(client.post("/api/chat", json={"text": "停下"}))
            await asyncio.wait_for(speech.entered.wait(), 0.5)
            assert app.state.manager.bridge.stops > 0
            assert app.state.manager.mode == "idle"
            speech.gate.set()
            assert (await pending).status_code == 200
        await app.state.conversation.close()

    asyncio.run(scenario())


def test_lifespan_releases_control_before_speech_close(tmp_path):
    async def scenario():
        speech = Speech()
        app = make_app(tmp_path, speech)
        manager = app.state.manager

        async def close():
            assert manager.accepting_control is False and manager.mode == "idle"
            assert manager.bridge.stops > 0
            speech.closed = True

        speech.close = close
        async with app.router.lifespan_context(app):
            pass
        assert speech.closed and manager.bridge.closed

    asyncio.run(scenario())


def test_chat_forwards_display_without_controlling_game_or_enabling_speech(tmp_path):
    class DisplayBridge(FakeBridge):
        def __init__(self):
            super().__init__()
            self.data["capabilities"].append("dialogue_bubble")
            self.display = []
            self.sent = asyncio.Event()

        async def dialogue(self, **body):
            self.display.append(body)
            self.sent.set()
            return {"status": "accepted"}

    async def scenario():
        bridge, speech = DisplayBridge(), Speech()
        app = create_app(store=store_for(tmp_path), bridge=bridge, model=ChatModel(),
                         voice=FakeVoice(), speech=speech,
                         journal=ActionJournal(tmp_path / "session.local.json"))
        async with (app.router.lifespan_context(app),
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                      base_url="http://127.0.0.1:48860") as client):
            assert (await client.get("/api/dialogue/status")).status_code == 403
            client.headers["X-Kingdom-Session"] = (
                await client.get("/api/session")).json()["token"]
            response = await client.post("/api/chat", json={"text": "你好"})
            assert response.status_code == 200
            await asyncio.wait_for(bridge.sent.wait(), 0.5)
            display = bridge.display[0]
            assert display["session_id"] == "test-session" and display["text"]
            assert set(display) == {"message_id", "session_id", "text"}
            status = (await client.get("/api/dialogue/status")).json()
            assert status["available"] and status["sent_count"] == 1
            assert status["shown"] is None and not bridge.commands and not bridge.heartbeats
            assert not speech.messages and app.state.manager.mode == "idle"
        assert app.state.overlay.status()["reason"] == "closed"
    asyncio.run(scenario())
