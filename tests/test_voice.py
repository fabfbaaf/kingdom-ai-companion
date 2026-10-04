import asyncio
import hashlib
import io
import json
import threading
import zipfile

import httpx
import pytest

from companion import voice
from companion.voice import MODEL_FILES, MODEL_NAME, VoiceError, VoiceService


def model_files(path):
    for name in MODEL_FILES:
        item = path / name
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_bytes(b"test-model")


class FakeStream:
    def __init__(self, **options):
        self.options = options
        self.started = False
        self.aborted = False
        self.closed = False

    def start(self):
        self.started = True

    def abort(self):
        self.aborted = True

    def close(self):
        self.closed = True

    def feed(self, data=b"pcm", status=False):
        self.options["callback"](data, 4000, None, status)


class FakeRecognizer:
    def __init__(self):
        self.final = False
        self.malformed = False

    def AcceptWaveform(self, data):
        self.final = data != b"partial"
        return self.final

    def Result(self):
        return "invalid" if self.malformed else json.dumps({"text": "往 左 走"})

    def PartialResult(self):
        return json.dumps({"partial": "往 左"})


def service(tmp_path, *, recognizer=None, loader=None, stream_type=FakeStream):
    path = tmp_path / MODEL_NAME
    model_files(path)
    streams = []
    recognizer = recognizer or FakeRecognizer()

    def factory(**options):
        streams.append(stream_type(**options))
        return streams[-1]

    instance = VoiceService(
        path,
        model_loader=loader or (lambda path: object()),
        recognizer_factory=lambda model, rate: recognizer,
        stream_factory=factory,
        device_query=lambda: {"name": "fake-microphone", "default_samplerate": 48000},
    )
    return instance, streams, recognizer


async def until(check):
    for _ in range(100):
        if check():
            return
        await asyncio.sleep(0.01)
    pytest.fail("voice worker did not produce the expected state")


def test_construction_and_polling_do_not_open_microphone(tmp_path):
    instance, streams, _ = service(tmp_path)
    assert instance.status()["state"] == "ready"
    assert instance.events()["events"] == []
    assert streams == []


def test_listening_partials_finals_and_stop_are_offline(tmp_path):
    async def scenario():
        instance, streams, _ = service(tmp_path)
        assert (await instance.start())["state"] == "listening"
        stream = streams[0]
        assert stream.options["samplerate"] == 48000
        assert stream.options["dtype"] == "int16"
        assert stream.options["channels"] == 1
        stream.feed(b"partial")
        await until(lambda: instance.status()["partial"] == "往左")
        stream.feed()
        await until(lambda: instance.events()["cursor"] == 1)
        assert instance.events()["events"][0]["text"] == "往左走"
        assert instance.events(after=1)["events"] == []
        assert (await instance.stop())["state"] == "ready"
        assert stream.aborted and stream.closed
        stream.feed()
        await asyncio.sleep(0.03)
        assert instance.events()["cursor"] == 1
        await instance.close()

    asyncio.run(scenario())


def test_stop_during_model_loading_never_opens_microphone(tmp_path):
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        def loader(path):
            entered.set()
            release.wait(timeout=2)
            return object()

        instance, streams, _ = service(tmp_path, loader=loader)
        starting = asyncio.create_task(instance.start())
        await until(entered.is_set)
        await instance.stop()
        release.set()
        await starting
        assert streams == []
        assert instance.status()["state"] == "ready"
        await instance.close()

    asyncio.run(scenario())


def test_cancel_during_stream_open_closes_microphone(tmp_path):
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        class DelayedStream(FakeStream):
            def start(self):
                entered.set()
                release.wait(timeout=2)
                super().start()

        instance, streams, _ = service(tmp_path, stream_type=DelayedStream)
        starting = asyncio.create_task(instance.start())
        await until(entered.is_set)
        starting.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert streams[0].closed
        assert instance.status()["state"] == "ready"
        await instance.close()

    asyncio.run(scenario())


def test_start_waiting_for_another_start_cannot_reopen_after_stop(tmp_path):
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        def loader(path):
            entered.set()
            release.wait(timeout=2)
            return object()

        instance, streams, _ = service(tmp_path, loader=loader)
        first = asyncio.create_task(instance.start())
        await until(entered.is_set)
        queued = asyncio.create_task(instance.start())
        await asyncio.sleep(0)
        await instance.stop()
        release.set()
        await asyncio.gather(first, queued)
        assert streams == []
        assert instance.status()["state"] == "ready"
        await instance.close()

    asyncio.run(scenario())


def test_input_fault_closes_microphone_and_does_not_emit_command(tmp_path):
    async def scenario():
        instance, streams, _ = service(tmp_path)
        await instance.start()
        streams[0].feed(status=True)
        await until(lambda: streams[0].closed)
        assert instance.status()["state"] == "error"
        assert instance.events()["events"] == []
        await instance.close()

    asyncio.run(scenario())


def test_invalid_recognition_result_closes_microphone(tmp_path):
    async def scenario():
        instance, streams, recognizer = service(tmp_path)
        recognizer.malformed = True
        await instance.start()
        streams[0].feed()
        await until(lambda: streams[0].closed)
        assert instance.status()["state"] == "error"
        assert instance.events()["events"] == []
        await instance.close()

    asyncio.run(scenario())


def test_input_fault_during_stream_open_is_not_overwritten_as_listening(tmp_path):
    async def scenario():
        class FaultStream(FakeStream):
            def start(self):
                super().start()
                self.feed(status=True)

        instance, streams, _ = service(tmp_path, stream_type=FaultStream)
        assert (await instance.start())["state"] == "error"
        assert streams[0].closed
        assert instance._stream is None
        await instance.close()

    asyncio.run(scenario())


def test_bounded_audio_queue_stops_instead_of_silently_dropping_speech(tmp_path):
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        class BlockedRecognizer(FakeRecognizer):
            def AcceptWaveform(self, data):
                entered.set()
                release.wait(timeout=2)
                return super().AcceptWaveform(data)

        instance, streams, _ = service(tmp_path, recognizer=BlockedRecognizer())
        await instance.start()
        streams[0].feed()
        await until(entered.is_set)
        for _ in range(33):
            streams[0].feed()
        assert instance.status()["state"] == "error"
        assert "积压" in instance.status()["message"]
        release.set()
        await until(lambda: streams[0].closed)
        assert instance.events()["events"] == []
        await instance.close()

    asyncio.run(scenario())


def test_failed_close_is_reported_and_can_be_retried(tmp_path):
    async def scenario():
        class FailingStream(FakeStream):
            def close(self):
                if self.started:
                    self.started = False
                    raise RuntimeError("native close failed")
                super().close()

        instance, streams, _ = service(tmp_path, stream_type=FailingStream)
        await instance.start()
        assert (await instance.stop())["state"] == "error"
        assert "未能确认" in instance.status()["message"]
        assert instance._stream is streams[0]
        assert (await instance.stop())["state"] == "ready"
        assert streams[0].closed
        await instance.close()

    asyncio.run(scenario())


def test_new_start_does_not_overlap_native_microphone_close(tmp_path):
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        class DelayedClose(FakeStream):
            def close(self):
                entered.set()
                release.wait(timeout=2)
                super().close()

        instance, streams, _ = service(tmp_path, stream_type=DelayedClose)
        await instance.start()
        stopping = asyncio.create_task(instance.stop())
        await until(entered.is_set)
        assert instance.status()["stopping"]
        with pytest.raises(VoiceError, match="正在关闭"):
            await instance.start()
        release.set()
        await stopping
        assert streams[0].closed
        assert not instance.status()["stopping"]
        await instance.close()

    asyncio.run(scenario())


def test_event_history_is_bounded_and_reports_missed_finals(tmp_path):
    async def scenario():
        instance, streams, _ = service(tmp_path)
        await instance.start()
        for sequence in range(voice.MAX_EVENTS + 6):
            streams[0].feed()
            await until(lambda sequence=sequence: instance.events()["cursor"] == sequence + 1)
        result = instance.events()
        assert len(result["events"]) == voice.MAX_EVENTS
        assert result["dropped"] == 6
        assert result["cursor"] == voice.MAX_EVENTS + 6
        await instance.close()

    asyncio.run(scenario())


def test_close_disallows_restart_and_download(tmp_path):
    async def scenario():
        instance, _, _ = service(tmp_path)
        await instance.close()
        with pytest.raises(VoiceError, match="关闭"):
            await instance.start()
        with pytest.raises(VoiceError, match="关闭"):
            await instance.prepare_model()

    asyncio.run(scenario())


def make_archive(*names):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name in names:
            info = zipfile.ZipInfo()
            info.filename = name
            archive.writestr(info, b"model")
    return output.getvalue()


@pytest.mark.parametrize("name", [
    f"{MODEL_NAME}/../escape",
    f"{MODEL_NAME}/folder/./file",
    f"{MODEL_NAME}\\escape",
    "/absolute",
    "other-model/am/final.mdl",
    f"{MODEL_NAME}/C:/escape",
    f"{MODEL_NAME}/trailing./file",
])
def test_archive_rejects_unsafe_paths_before_writing(tmp_path, name):
    archive = tmp_path / "input.zip"
    archive.write_bytes(make_archive(name))
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(VoiceError):
        voice._extract_archive(archive, staging)
    assert list(staging.iterdir()) == []


def test_archive_rejects_case_collisions_and_symlinks(tmp_path):
    archive = tmp_path / "input.zip"
    archive.write_bytes(make_archive(f"{MODEL_NAME}/A", f"{MODEL_NAME}/a"))
    with pytest.raises(VoiceError):
        voice._extract_archive(archive, tmp_path / "staging")
    with zipfile.ZipFile(archive, "w") as zipped:
        entry = zipfile.ZipInfo(f"{MODEL_NAME}/link")
        entry.external_attr = 0o120777 << 16
        zipped.writestr(entry, "../../escape")
    with pytest.raises(VoiceError):
        voice._extract_archive(archive, tmp_path / "staging")


def test_download_hash_failure_does_not_install_or_open_microphone(tmp_path):
    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"corrupt-model")
        ))
        instance = VoiceService(tmp_path / MODEL_NAME, download_client=client)
        assert (await instance.prepare_model())["state"] == "downloading"
        await instance._download_task
        assert instance.status()["state"] == "error"
        assert "SHA-256" in instance.status()["message"]
        assert not instance.model_dir.exists()
        assert list(tmp_path.glob(".vosk-*")) == []
        await instance.close()
        await client.aclose()

    asyncio.run(scenario())


def test_verified_download_installs_atomically_without_microphone(tmp_path, monkeypatch):
    payload = make_archive(*(f"{MODEL_NAME}/{name}" for name in MODEL_FILES))
    monkeypatch.setattr(voice, "MODEL_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(voice, "MODEL_ARCHIVE_BYTES", len(payload))

    async def scenario():
        requests = []

        def transport(request):
            requests.append(request)
            return httpx.Response(200, content=payload)

        client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        instance = VoiceService(tmp_path / MODEL_NAME, download_client=client)
        await instance.prepare_model()
        task = instance._download_task
        await instance.prepare_model()
        assert instance._download_task is task
        await task
        assert instance.status()["state"] == "ready"
        assert instance.status()["model_ready"]
        assert instance._stream is None
        assert str(requests[0].url) == voice.MODEL_URL
        assert len(requests) == 1
        assert list(tmp_path.glob(".vosk-*")) == []
        await instance.close()
        await client.aclose()

    asyncio.run(scenario())
