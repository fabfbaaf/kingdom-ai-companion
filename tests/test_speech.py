"""Speech tests use fake processes only: no speakers, microphones or installs."""

from __future__ import annotations

import asyncio
import json

import pytest

from companion import speech
from companion.speech import (
    MAX_QUEUE,
    MAX_SENTENCE_CHARS,
    MAX_SENTENCES,
    MAX_TEXT,
    SpeechError,
    SpeechService,
)

VOICES = [{"name": "Fake Chinese", "culture": "zh-CN", "enabled": True},
          {"name": "Fake English", "culture": "en-US", "enabled": True}]


class FakeProcess:
    def __init__(self, *, blocked=False, output=b'{"ok":true}', returncode=0):
        self.pid = 123456
        self.returncode = None
        self.completed = asyncio.Event()
        self.started = asyncio.Event()
        self.payloads = []
        self.blocked = blocked
        self.output = output
        self.exit_code = returncode
        self.killed = False

    async def communicate(self, payload):
        self.payloads.append(json.loads(payload))
        self.started.set()
        if self.blocked:
            await self.completed.wait()
        if self.returncode is None:
            self.returncode = self.exit_code
        self.completed.set()
        return self.output, None

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.completed.set()

    async def wait(self):
        await self.completed.wait()
        return self.returncode


def make_service(*, process_type=FakeProcess, voices=None, listener_active=None, **options):
    processes, commands = [], []

    async def factory(*command, **kwargs):
        commands.append((command, kwargs))
        process = process_type()
        processes.append(process)
        return process

    instance = SpeechService(process_factory=factory,
                             voice_loader=lambda: VOICES if voices is None else voices,
                             listener_active=listener_active, **options)
    return instance, processes, commands


async def until(predicate):
    for _ in range(150):
        if predicate():
            return
        await asyncio.sleep(0.005)
    pytest.fail("fake speech service did not reach expected state")


def reply(message_id=1, content="我在这里。一起向右探索吧！", **fields):
    return {"id": message_id, "content": content, "source": "text", **fields}


def test_construct_status_and_prepare_never_speak():
    async def scenario():
        loaded = []

        async def loader():
            loaded.append(True)
            return [dict(VOICES[0], registry_id="private", device="private-device")]

        instance, processes, _ = make_service()
        instance._voice_loader = loader
        assert instance.status()["enabled"] is False
        assert instance.status()["available_voices"] == []
        assert loaded == [] and processes == []
        await asyncio.gather(instance.prepare(), instance.prepare())
        assert loaded == [True]
        assert instance.status()["available_voices"] == [VOICES[0]]
        assert instance.status()["enabled"] is False
        assert processes == []
        status = instance.status()
        status["available_voices"][0]["name"] = "mutated"
        assert instance.status()["available_voices"][0]["name"] == "Fake Chinese"
        await instance.say(reply())
        assert processes == []
        await instance.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("voices", [[], [VOICES[1]], [dict(VOICES[0], enabled=False)]])
def test_no_chinese_voice_is_explicitly_unavailable(voices):
    async def scenario():
        instance, processes, _ = make_service(voices=voices)
        status = await instance.configure(True)
        assert status["enabled"] is False
        assert status["voice_name"] is None
        assert status["error"] == "chinese_voice_unavailable"
        assert "不可用" in status["message"]
        await instance.say(reply())
        assert processes == []
        await instance.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("voice_name", ["missing", "Fake English"])
def test_configure_rejects_uninstalled_or_non_chinese_voice(voice_name):
    async def scenario():
        instance, processes, _ = make_service()
        result = await instance.configure(True, voice_name)
        assert result["enabled"] is False
        assert result["error"] == "voice_unavailable"
        assert processes == []
        await instance.close()

    asyncio.run(scenario())


def test_read_only_voice_scan_uses_json_and_no_audio_request(monkeypatch):
    async def scenario():
        monkeypatch.setattr(speech, "WINDOWS", True)
        payload = json.dumps({"ok": True, "voices": VOICES}).encode()
        instance, processes, commands = make_service(
            process_type=lambda: FakeProcess(output=payload)
        )
        instance._voice_loader = None
        await instance.prepare()
        assert processes[0].payloads == [{"op": "voices"}]
        assert instance.status()["enabled"] is False
        assert commands[0][0][-2:] == (
            "-Command", speech.HELPER_PATH.read_text(encoding="utf-8")
        )
        assert "-ExecutionPolicy" not in commands[0][0]
        await instance.close()

    asyncio.run(scenario())


def test_enabled_replies_split_and_text_never_enters_command():
    async def scenario():
        instance, processes, commands = make_service()
        await instance.configure(True)
        malicious_text = '向右走。$(Remove-Item *) "quoted" <speak>仅是文字</speak>！'
        await instance.say(reply(content=malicious_text))
        await until(lambda: instance._worker is None)
        payloads = [process.payloads[0] for process in processes]
        assert len(payloads) == 2
        assert "".join(payload["text"] for payload in payloads) == malicious_text
        assert all(payload["op"] == "say" for payload in payloads)
        assert all(payload["voice_name"] == "Fake Chinese" for payload in payloads)
        assert all("Remove-Item" not in str(command) for command, _ in commands)
        assert all("-ExecutionPolicy" not in command for command, _ in commands)
        assert all(kwargs["stdin"] == asyncio.subprocess.PIPE for _, kwargs in commands)
        assert all("shell" not in kwargs for _, kwargs in commands)
        await instance.say(reply(content=malicious_text))
        await asyncio.sleep(0)
        assert len(processes) == 2  # The same message cannot play twice.
        await instance.close()

    asyncio.run(scenario())


def test_system_errors_users_and_malformed_messages_are_silent():
    async def scenario():
        instance, processes, _ = make_service()
        await instance.configure(True)
        for message in [reply(source="system"), reply(role="user"), reply(content=None),
                        reply(id=None), reply(id=True), {}, None]:
            await instance.say(message)
        await asyncio.sleep(0)
        assert processes == []
        await instance.close()

    asyncio.run(scenario())


def test_queue_text_and_sentence_limits():
    async def scenario():
        instance, processes, _ = make_service(process_type=lambda: FakeProcess(blocked=True))
        await instance.configure(True)
        await instance.say(reply(content="长" * (MAX_TEXT + 1000)))
        await until(lambda: bool(processes and processes[0].payloads))
        for message_id in range(2, MAX_QUEUE + 4):
            await instance.say(reply(message_id))
        assert instance.status()["queued"] == MAX_QUEUE
        assert len(instance._seen) == MAX_QUEUE + 1
        first = processes[0].payloads[0]
        assert len(first["text"]) == MAX_SENTENCE_CHARS
        chunks, truncated = speech._sentences("短。" * 1000)
        assert truncated and len(chunks) == MAX_SENTENCES
        chunks, truncated = speech._sentences("长" * (MAX_TEXT + 1000))
        assert truncated and sum(map(len, chunks)) == MAX_TEXT
        assert all(len(chunk) <= MAX_SENTENCE_CHARS for chunk in chunks)
        await instance.stop()
        assert processes[0].killed
        assert instance.status()["queued"] == 0
        await instance.close()

    asyncio.run(scenario())


def test_listening_suppresses_default_output_and_headphones_are_explicit():
    async def scenario():
        listening = True
        instance, processes, _ = make_service(listener_active=lambda: listening)
        await instance.configure(True)
        await instance.say(reply())
        assert processes == []
        assert "暂停" in instance.status()["message"]
        listening = False
        await instance.say(reply(2), listening=True)
        assert processes == []
        listening = True
        result = await instance.configure(True, headphones=True)
        assert result["headphones"] is True
        assert "不提供回声消除" in result["message"]
        await instance.say(reply(3), listening=True)
        await until(lambda: instance._worker is None)
        assert processes
        await instance.close()

    asyncio.run(scenario())


def test_listening_started_after_queueing_stops_current_and_remaining_sentences():
    async def scenario():
        listening = False
        instance, processes, _ = make_service(
            process_type=lambda: FakeProcess(blocked=True), listener_active=lambda: listening
        )
        await instance.configure(True)
        await instance.say(reply())
        await until(lambda: bool(processes and processes[0].payloads))
        listening = True
        await until(lambda: instance._worker is None)
        assert len(processes) == 1 and processes[0].killed
        assert instance.status()["speaking"] is False
        assert instance.status()["queued"] == 0
        assert instance.status()["enabled"] is True
        await instance.close()

    asyncio.run(scenario())


def test_listener_state_failure_safely_suppresses_output():
    async def scenario():
        def broken_listener():
            raise RuntimeError("private-device-name")

        instance, processes, _ = make_service(listener_active=broken_listener)
        await instance.configure(True)
        await instance.say(reply())
        assert processes == []
        assert "private-device-name" not in str(instance.status())
        await instance.close()

    asyncio.run(scenario())


def test_stop_kills_active_speech_and_clears_queue_without_replay():
    async def scenario():
        instance, processes, _ = make_service(process_type=lambda: FakeProcess(blocked=True))
        await instance.configure(True)
        await instance.say(reply())
        await until(lambda: bool(processes and processes[0].payloads))
        await instance.say(reply(2))
        status = await instance.stop()
        assert processes[0].killed
        assert status["speaking"] is False and status["queued"] == 0
        assert status["enabled"] is True
        await asyncio.sleep(0.03)
        assert len(processes) == 1
        await instance.configure(False)
        await instance.say(reply(3))
        assert len(processes) == 1
        await instance.close()

    asyncio.run(scenario())


def test_stop_during_delayed_spawn_never_sends_text_to_late_process():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        process = FakeProcess(blocked=True)

        async def delayed_factory(*args, **kwargs):
            entered.set()
            await release.wait()
            return process

        instance = SpeechService(process_factory=delayed_factory, voice_loader=lambda: VOICES)
        await instance.configure(True)
        await instance.say(reply())
        await entered.wait()
        await instance.stop()
        assert process.payloads == []
        release.set()
        await until(lambda: process.killed)
        assert process.payloads == []
        await instance.close()

    asyncio.run(scenario())


def test_stop_during_configuration_cannot_late_enable_output():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def loader():
            entered.set()
            await release.wait()
            return VOICES

        instance, processes, _ = make_service()
        instance._voice_loader = loader
        configuring = asyncio.create_task(instance.configure(True))
        await entered.wait()
        await instance.stop()
        release.set()
        await configuring
        assert instance.status()["enabled"] is False
        await instance.say(reply())
        assert processes == []
        await instance.close()

    asyncio.run(scenario())


def test_timeout_terminates_speech_and_disables_failing_service():
    async def scenario():
        instance, processes, _ = make_service(
            process_type=lambda: FakeProcess(blocked=True), speech_timeout=0.01
        )
        await instance.configure(True)
        await instance.say(reply())
        await until(lambda: instance._worker is None)
        status = instance.status()
        assert len(processes) == 1 and processes[0].killed
        assert status["enabled"] is False and status["speaking"] is False
        assert status["error"] == "speech_timeout"
        assert status["queued"] == 0
        await instance.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("output, returncode", [
    (b"not json private-device-path", 0), (b'{"ok":false}', 0), (b'{"ok":true}', 1),
])
def test_process_failure_is_safe_and_does_not_replay(output, returncode):
    async def scenario():
        instance, processes, _ = make_service(
            process_type=lambda: FakeProcess(output=output, returncode=returncode)
        )
        await instance.configure(True)
        await instance.say(reply())
        await until(lambda: instance._worker is None)
        assert len(processes) == 1
        assert instance.status()["error"] == "speech_failed"
        assert "private-device-path" not in str(instance.status())
        await instance.close()

    asyncio.run(scenario())


def test_close_kills_speech_and_is_idempotent():
    async def scenario():
        instance, processes, _ = make_service(process_type=lambda: FakeProcess(blocked=True))
        await instance.configure(True)
        await instance.say(reply())
        await until(lambda: bool(processes and processes[0].payloads))
        await instance.close()
        await instance.close()
        assert processes[0].killed
        assert instance.status()["enabled"] is False
        assert instance.status()["speaking"] is False
        await instance.say(reply(2))
        assert len(processes) == 1
        with pytest.raises(SpeechError, match="关闭"):
            await instance.configure(True)

    asyncio.run(scenario())


def test_tree_termination_uses_only_owned_pid_and_hidden_window(monkeypatch):
    async def scenario():
        monkeypatch.setattr(speech, "WINDOWS", True)
        calls = []
        process = FakeProcess(blocked=True)

        async def fake_taskkill(*args, **kwargs):
            calls.append((args, kwargs))
            process.kill()
            killer = FakeProcess()
            killer.returncode = 0
            killer.completed.set()
            return killer

        monkeypatch.setattr(speech.asyncio, "create_subprocess_exec", fake_taskkill)
        instance, _, _ = make_service()
        instance._real_processes = True
        await instance._terminate(process)
        args, kwargs = calls[0]
        assert args[1:] == ("/PID", str(process.pid), "/T", "/F")
        assert "/IM" not in args
        assert kwargs["creationflags"] == speech.CREATE_NO_WINDOW
        assert process.killed
        await instance.close()

    asyncio.run(scenario())


def test_spawn_timeout_reaps_late_process_without_sending_text():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        process = FakeProcess(blocked=True)

        async def delayed_factory(*args, **kwargs):
            entered.set()
            await release.wait()
            return process

        instance = SpeechService(process_factory=delayed_factory, voice_loader=lambda: VOICES,
                                 start_timeout=0.01)
        await instance.configure(True)
        await instance.say(reply())
        await entered.wait()
        await until(lambda: instance._worker is None)
        assert instance.status()["error"] == "speech_timeout"
        release.set()
        await until(lambda: process.killed)
        assert process.payloads == []
        await instance.close()

    asyncio.run(scenario())


def test_cancelled_stop_finishes_killing_owned_process():
    async def scenario():
        instance, processes, _ = make_service(process_type=lambda: FakeProcess(blocked=True))
        await instance.configure(True)
        await instance.say(reply())
        await until(lambda: bool(processes and processes[0].payloads))
        entered, release = asyncio.Event(), asyncio.Event()
        original_kill_tree = instance._kill_tree

        async def delayed_kill_tree(process):
            entered.set()
            await release.wait()
            await original_kill_tree(process)

        instance._kill_tree = delayed_kill_tree
        stopping = asyncio.create_task(instance.stop())
        await entered.wait()
        stopping.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert processes[0].killed
        assert instance.status()["speaking"] is False
        await instance.close()

    asyncio.run(scenario())


def test_concurrent_configure_cannot_overwrite_newer_disable():
    async def scenario():
        instance, processes, _ = make_service(process_type=lambda: FakeProcess(blocked=True))
        await instance.configure(True)
        await instance.say(reply())
        await until(lambda: bool(processes and processes[0].payloads))
        entered, release = asyncio.Event(), asyncio.Event()
        original_kill_tree = instance._kill_tree

        async def delayed_kill_tree(process):
            entered.set()
            await release.wait()
            await original_kill_tree(process)

        instance._kill_tree = delayed_kill_tree
        enabling = asyncio.create_task(instance.configure(True, headphones=True))
        await entered.wait()
        disabling = asyncio.create_task(instance.configure(False, headphones=False))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(enabling, disabling)
        assert instance.status()["enabled"] is False
        assert instance.status()["headphones"] is False
        assert processes[0].killed
        await instance.close()

    asyncio.run(scenario())


def test_voice_discovery_timeout_is_bounded_and_sanitized():
    async def scenario():
        async def slow_loader():
            await asyncio.Event().wait()

        instance, processes, _ = make_service(voice_timeout=0.01)
        instance._voice_loader = slow_loader
        await asyncio.wait_for(instance.prepare(), 0.2)
        assert instance.status()["error"] == "voice_discovery_failed"
        assert instance.status()["enabled"] is False
        assert processes == []
        await instance.close()

    asyncio.run(scenario())
