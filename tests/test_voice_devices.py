"""Input selection and recovery with fake streams; never open real audio devices."""

import asyncio
import json
import struct

import httpx
import pytest
from test_companion import FakeBridge, store_for
from test_companion_features import Speech
from test_conversation import ChatModel
from test_voice import FakeRecognizer, FakeStream, model_files, until

from companion.app import create_app
from companion.controller import ActionJournal
from companion.voice import MODEL_NAME, VoiceError, VoiceService


def capture(tmp_path, *, device_list=None, query=None, recognizer=None):
    model = tmp_path / MODEL_NAME
    model_files(model)
    devices = [{"index": 5, "name": "USB mic", "host_api": "WASAPI", "default": False,
                "sample_rate": 48000, "channels": 1}]
    streams = []
    def factory(**options):
        streams.append(FakeStream(**options))
        return streams[-1]
    voice = VoiceService(model, model_loader=lambda _: object(),
                         recognizer_factory=lambda *_: recognizer or FakeRecognizer(),
                         device_query=query or (lambda *_: {"name": "USB mic", "default_samplerate": 48000}),
                         device_list=device_list or (lambda: devices), stream_factory=factory)
    return voice, streams


def test_selection_enumerates_without_capture_and_restores_after_index_changes(tmp_path):
    async def scenario():
        voice, streams = capture(tmp_path)
        result = await voice.devices()
        assert result["device_error"] and result["devices"][0]["index"] == 5
        assert streams == []
        assert (await voice.select_device(5))["device_error"] is None
        assert (await voice.devices())["device_error"] is None
        persisted = json.loads((tmp_path / "voice.local.json").read_text(encoding="utf-8"))
        assert persisted == {"name": "USB mic", "host_api": "WASAPI"}
        restarted, new_streams = capture(tmp_path, device_list=lambda: [{
            "index": 11, "name": "USB mic", "host_api": "WASAPI", "default": False}])
        restored = await restarted.devices()
        assert restored["selected_device"] == 11 and restored["device_error"] is None
        await restarted.start()
        assert new_streams[0].options["device"] == 11
        await restarted.close()
        await voice.close()
    asyncio.run(scenario())


def test_missing_preferred_microphone_never_silently_uses_another_input(tmp_path):
    async def scenario():
        voice, _ = capture(tmp_path)
        await voice.select_device(5)
        await voice.close()
        restarted, streams = capture(tmp_path, device_list=lambda: [{
            "index": 8, "name": "Stereo mix", "host_api": "WASAPI", "default": True}])
        with pytest.raises(VoiceError, match="不可用"):
            await restarted.start()
        assert not streams
        await restarted.select_device(None)
        assert (await restarted.start())["state"] == "listening"
        await restarted.close()
    asyncio.run(scenario())


def test_selection_is_rejected_while_capturing_and_unknown_index_is_rejected(tmp_path):
    async def scenario():
        voice, _ = capture(tmp_path)
        with pytest.raises(VoiceError, match="不可用"):
            await voice.select_device(999)
        await voice.start()
        with pytest.raises(VoiceError, match="停止监听"):
            await voice.select_device(5)
        await voice.close()
    asyncio.run(scenario())


def test_pcm_meter_reports_rms_peak_and_resets_on_stop(tmp_path):
    async def scenario():
        voice, streams = capture(tmp_path)
        await voice.start()
        streams[0].feed(struct.pack("<4h", 16384, -16384, 16384, -16384))
        assert voice.status()["level"] == 0.5 and voice.status()["peak"] == 0.5
        assert voice.status()["last_audio_at"]
        await voice.stop()
        assert voice.status()["level"] == 0 and voice.status()["peak"] == 0
        await voice.close()
    asyncio.run(scenario())


def test_short_overflow_resets_incomplete_sentence_and_keeps_listening(tmp_path):
    class Recognizer(FakeRecognizer):
        resets = 0
        def Reset(self):
            self.resets += 1
    class Overflow:
        input_overflow = True
    async def scenario():
        rec = Recognizer()
        voice, streams = capture(tmp_path, recognizer=rec)
        await voice.start()
        streams[0].feed(b"partial")
        await until(lambda: voice.status()["partial"])
        streams[0].feed(b"partial", status=Overflow())
        await until(lambda: rec.resets == 1)
        assert voice.status()["state"] == "listening" and voice.status()["overflows"] == 1
        streams[0].feed()
        await until(lambda: voice.events()["cursor"] == 1)
        await voice.close()
    asyncio.run(scenario())


def test_default_query_failure_has_actionable_error_and_no_capture(tmp_path):
    def unavailable():
        raise RuntimeError("default index -1")
    async def scenario():
        voice, streams = capture(tmp_path, query=unavailable)
        with pytest.raises(VoiceError, match="选择输入设备"):
            await voice.start()
        assert voice.status()["error_code"] == "input_open_failed" and not streams
        await voice.close()
    asyncio.run(scenario())


def test_device_api_is_authenticated_and_does_not_capture_until_start(tmp_path):
    async def scenario():
        voice, streams = capture(tmp_path)
        app = create_app(store=store_for(tmp_path), bridge=FakeBridge(), model=ChatModel(),
                         voice=voice, speech=Speech(), journal=ActionJournal(tmp_path / "session.local.json"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:48860") as client:
            assert (await client.get("/api/voice/devices")).status_code == 403
            client.headers["X-Kingdom-Session"] = (await client.get("/api/session")).json()["token"]
            assert (await client.get("/api/voice/devices")).json()["devices"][0]["index"] == 5
            assert not streams
            for invalid in (True, -1, "5"):
                assert (await client.put("/api/voice/device", json={"device": invalid})).status_code == 422
            assert (await client.put("/api/voice/device", json={"device": 5})).status_code == 200
            assert not streams
            assert (await client.post("/api/voice/start")).status_code == 200
            assert len(streams) == 1
            assert (await client.put("/api/voice/device", json={"device": None})).status_code == 502
            assert (await client.post("/api/voice/stop")).status_code == 200
            assert streams[0].closed and not app.state.manager.bridge.commands
        await app.state.conversation.close()
        await app.state.manager.close()
    asyncio.run(scenario())
