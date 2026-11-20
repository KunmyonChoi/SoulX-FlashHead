"""Integration tests of the voice and avatar channels with the real audio stack.

A small FastAPI app on port 7882 accepts /ws and calls serve_voice. The "customer" is speech
synthesised by Supertonic, sent as 16 kHz PCM in 0.1 s packets on a real-time clock and followed by
silence, exactly as the browser's microphone would send it. Real: Silero VAD, Whisper (GPU),
Supertonic, and for the avatar test v1's FlashHead Engine. The reply engine is a scripted fake unless
AGENTKIT_VOICE_TEST_AGENT names an agent (then agentkit.runtime.create_engine and the real LLM are used).

Run:  CUDA_VISIBLE_DEVICES=0 python -m pytest tests/test_voice_integration.py -q -s
"""
import asyncio
import io
import json
import math
import os
import socket
import struct
import threading
import time
import urllib.request

import numpy as np
import pytest

from agentkit import config
from agentkit.runtime import audio as audio_parts
from agentkit.runtime.voice import serve_voice
from voice_fakes import FakeEngine, parse_video_chunk, simple_script

pytestmark = [pytest.mark.gpu, pytest.mark.network]

PORT = 7882
URL = f"ws://127.0.0.1:{PORT}/ws"
QUESTION = "안녕하세요, 주문 조회를 하고 싶어요."
REPLY = ("네, 안녕하세요.", "주문번호를 말씀해 주시겠어요?")
PACKET = 1600   # samples: 0.1 s


def supertonic(text, voice="M1"):
    """The text as 16 kHz int16 speech, without leading or trailing silence."""
    import soundfile as sf
    from scipy.signal import resample_poly
    body = json.dumps({"text": text, "voice": voice, "lang": "ko", "response_format": "wav"}).encode()
    request = urllib.request.Request(config.TTS_URL.rstrip("/") + "/v1/tts", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        wav, rate = sf.read(io.BytesIO(response.read()), dtype="float32", always_2d=True)
    wav = audio_parts.trim_silence(wav.mean(axis=1), rate)
    divisor = math.gcd(rate, 16000)
    wav = resample_poly(wav, 16000 // divisor, rate // divisor)
    return (np.clip(wav, -1, 1) * 32767).astype("<i2")


@pytest.fixture(scope="module")
def question_pcm():
    try:
        return supertonic(QUESTION)
    except Exception as e:
        pytest.skip(f"Supertonic not reachable at {config.TTS_URL}: {e}")


@pytest.fixture(scope="module")
def server():
    import torch
    if not torch.cuda.is_available():
        pytest.skip("no GPU")
    import uvicorn
    from fastapi import FastAPI, WebSocket

    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            pytest.fail(f"port {PORT} is already in use")

    app = FastAPI()
    engines = []

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, channel: str = "voice"):
        await ws.accept()
        agent = os.environ.get("AGENTKIT_VOICE_TEST_AGENT")
        if agent:
            from agentkit.runtime import create_engine
            engine = await create_engine(agent, channel=channel, test=True)
        else:
            engine = FakeEngine(simple_script(*REPLY), session_id=f"it-{len(engines)}")
        engines.append(engine)
        try:
            await serve_voice(ws, engine, channel, dict(ws.query_params))
        finally:
            await engine.close()
            try:
                await ws.close()
            except Exception:
                pass

    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not uv.started:
        assert time.monotonic() < deadline, "test server did not start"
        time.sleep(0.05)
    yield engines
    uv.should_exit = True
    thread.join(timeout=15)
    assert not thread.is_alive(), "test server did not stop"


class Caller:
    """A browser stand-in: sends microphone audio on a real-time clock, records what comes back."""

    def __init__(self, query=""):
        self.url = URL + ("?" + query if query else "")
        self.json = []      # (time, message)
        self.binary = []    # (time, bytes)
        self.speech_end = None
        self.ws = None

    def of(self, type, **match):
        return [m for _, m in self.json if m.get("type") == type and all(m.get(k) == v for k, v in match.items())]

    async def __aenter__(self):
        import websockets
        self.ws = await websockets.connect(self.url, max_size=None, open_timeout=30)
        self.reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc):
        self.reader.cancel()
        await self.ws.close()

    async def _read(self):
        try:
            async for message in self.ws:
                now = time.monotonic()
                if isinstance(message, bytes):
                    self.binary.append((now, message))
                    continue
                data = json.loads(message)
                self.json.append((now, data))
                if data["type"] == "interrupt":
                    # like the page: report how far playback got (here: nothing was playing)
                    await self.ws.send(json.dumps({"type": "played", "seq": None, "offset": 0}))
        except Exception:
            pass

    async def wait(self, condition, timeout, what):
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline, f"timed out waiting for {what}; got {[m['type'] for _, m in self.json][-12:]}"
            await asyncio.sleep(0.02)

    async def microphone(self, pcm, until, timeout=40.0, lead=0.5):
        """Silence, then the speech, then silence until `until()` holds - all in 0.1 s packets."""
        speech = np.concatenate([np.zeros(int(lead * 16000), dtype="<i2"), pcm])
        speech = np.concatenate([speech, np.zeros(-len(speech) % PACKET, dtype="<i2")])
        silence = np.zeros(PACKET, dtype="<i2").tobytes()
        start = time.monotonic()
        index = 0
        while True:
            offset = index * PACKET
            packet = speech[offset:offset + PACKET].tobytes() if offset < len(speech) else silence
            await self.ws.send(packet)
            index += 1
            if self.speech_end is None and offset + PACKET >= int(lead * 16000) + len(pcm):
                self.speech_end = time.monotonic()   # the packet with the last voiced sample has left
            if self.speech_end is not None and until():
                return
            assert time.monotonic() - start < timeout, \
                f"no complete reply; got {[m['type'] for _, m in self.json][-12:]}, {len(self.binary)} binary"
            await asyncio.sleep(max(0.0, start + index * 0.1 - time.monotonic()))


def audio_chunks(caller):
    out = []
    for at, data in caller.binary:
        assert data[:4] == b"FHAU"
        seq, size = struct.unpack("<II", data[4:12])
        assert len(data) == 12 + size and 0 < size <= 15360 and size % 2 == 0
        out.append((at, seq, np.frombuffer(data[12:], dtype="<i2")))
    return out


async def one_voice_turn(pcm, query=""):
    async with Caller(query) as caller:
        await caller.wait(lambda: caller.of("ready") or caller.of("error"), 120, "ready")
        assert not caller.of("error"), caller.of("error")

        def replied():
            # the reply is over: chat_done seen and no audio for 1.5 s
            return bool(caller.of("chat_done") and caller.binary
                        and time.monotonic() - caller.binary[-1][0] > 1.5)

        await caller.microphone(pcm, replied)
        return caller


def check_voice_turn(caller):
    ready = caller.of("ready")[0]
    assert ready["channel"] == "voice" and ready["sample_rate"] == 16000 and ready["chunk_seconds"] == 0.48
    transcript = caller.of("transcript")
    assert len(transcript) == 1 and transcript[0]["engine"] == "whisper"
    assert "주문" in transcript[0]["text"], transcript[0]["text"]
    if not os.environ.get("AGENTKIT_VOICE_TEST_AGENT"):
        assert "".join(m["text"] for m in caller.of("chat_delta")).strip() == " ".join(REPLY)
        assert caller.of("chat_done")[0]["text"] == " ".join(REPLY)
    assert caller.of("tts", state="done") and caller.of("speech")
    chunks = audio_chunks(caller)
    assert [seq for _, seq, _ in chunks] == list(range(len(chunks)))
    pcm = np.concatenate([samples for _, _, samples in chunks])
    seconds = len(pcm) / 16000
    spoken = sum(m["seconds"] for m in caller.of("tts", state="done"))
    assert seconds == pytest.approx(spoken + 0.3 * len(caller.of("tts", state="done")), abs=0.01)
    assert np.sqrt((pcm.astype(np.float32) ** 2).mean()) > 300          # speech, not silence
    # paced in real time: the audio took about as long to arrive as it takes to play
    assert chunks[-1][0] - chunks[0][0] > seconds - 0.48 - 0.3
    assert chunks[0][0] > caller.speech_end                             # nothing before the turn ended
    return (chunks[0][0] - caller.speech_end) * 1000, caller.of("speech")[0]


def test_voice_channel_one_turn(server, question_pcm):
    first = asyncio.run(one_voice_turn(question_pcm))
    delay, speech = check_voice_turn(first)
    second = asyncio.run(one_voice_turn(question_pcm))       # models warm
    delay2, speech2 = check_voice_turn(second)
    for name, d, s in (("first", delay, speech), ("second", delay2, speech2)):
        print(f"\n[voice] {name} session: end of speech -> first FHAU chunk {d:.0f} ms "
              f"(server: since_utterance {s['since_utterance_ms']:.0f} ms, stt {s['stt_ms']:.0f} ms, "
              f"synth {s['synth_ms']:.0f} ms)")
    assert delay2 < 3000        # PRD: answer audio within 3 s of the end of speech
    if not os.environ.get("AGENTKIT_VOICE_TEST_AGENT"):
        # serve_voice returned on the disconnect and the app closed the engine (a moment after the client left)
        deadline = time.monotonic() + 5
        while server[-1].closed is None and time.monotonic() < deadline:
            time.sleep(0.05)
        # (the reply may have been prepared speculatively during the closing pause: with a fast
        # recogniser it is, which is the default voice setting at work; what matters is that the
        # reply that was spoken ran to its end)
        assert server[-1].closed == "completed" and server[-1].calls[-1]["finished"] is True


def test_two_voice_sessions_at_once(server, question_pcm):
    async def main():
        return await asyncio.gather(one_voice_turn(question_pcm), one_voice_turn(question_pcm))

    callers = asyncio.run(main())
    delays = [check_voice_turn(caller)[0] for caller in callers]
    assert callers[0].of("ready")[0]["session_id"] != callers[1].of("ready")[0]["session_id"]
    print(f"\n[voice] two sessions at once: end of speech -> first audio {delays[0]:.0f} ms and {delays[1]:.0f} ms")


def test_smart_turn_and_qwen_settings(server, question_pcm):
    """R-RT-10: the other end-of-turn judge and the other recogniser, chosen per connection."""
    try:
        with urllib.request.urlopen(config.ASR_URL.rstrip("/") + "/health", timeout=2):
            pass
    except Exception as e:
        pytest.skip(f"Qwen3-ASR server not reachable at {config.ASR_URL}: {e}")
    if not os.path.exists(config.SMART_TURN_MODEL):
        pytest.skip("Smart Turn model file missing")
    caller = asyncio.run(one_voice_turn(question_pcm, "stt=qwen&endpoint=smart"))
    transcript = caller.of("transcript")[0]
    assert transcript["engine"] == "qwen" and "주문" in transcript["text"]
    turn_end = caller.of("metric", name="turn_end")
    assert turn_end and 0.0 <= turn_end[0]["probability"] <= 1.0
    assert audio_chunks(caller)
    print(f"\n[voice] qwen + smart turn: closed after {turn_end[0]['silence']:.2f} s of silence "
          f"(probability {turn_end[0]['probability']:.2f}), first audio "
          f"{(audio_chunks(caller)[0][0] - caller.speech_end) * 1000:.0f} ms after the end of speech")


def test_avatar_channel_frames_and_single_seat(server, question_pcm):
    async def main():
        async with Caller("channel=avatar") as caller:
            # the first avatar session loads and warms up the video model
            await caller.wait(lambda: caller.of("ready") or caller.of("error"), 600, "ready (video engine load)")
            assert not caller.of("error"), caller.of("error")
            await caller.wait(lambda: len(caller.binary) >= 3, 30, "idle video chunks")

            # a second avatar connection while this one is open is refused
            async with Caller("channel=avatar") as second:
                await second.wait(lambda: second.of("ready") or second.of("error"), 20, "refusal")
                refused = (second.of("error"), second.of("ready"), len(second.binary))
            # ...while a voice-only connection is welcome
            async with Caller() as voice:
                await voice.wait(lambda: voice.of("ready") or voice.of("error"), 60, "voice ready")
                voice_ready = voice.of("ready")

            def replied():
                speech = caller.of("speech")
                return bool(caller.of("chat_done") and speech
                            and time.monotonic() - next(t for t, m in caller.json if m["type"] == "speech") > 5.0)

            await caller.microphone(question_pcm, replied, timeout=60)
            return caller, refused, voice_ready

    caller, refused, voice_ready = asyncio.run(main())
    errors, readies, binaries = refused
    assert len(errors) == 1 and "아바타" in errors[0]["message"] and not readies and binaries == 0
    assert voice_ready and voice_ready[0]["channel"] == "voice"

    ready = caller.of("ready")[0]
    assert ready["channel"] == "avatar" and ready["sample_rate"] == 16000
    for key in ("fps", "width", "height", "slice_frames"):
        assert ready[key] > 0
    chunks = [(at, *parse_video_chunk(data)) for at, data in caller.binary]
    assert [c[1] for c in chunks] == list(range(len(chunks)))
    slice_samples = ready["slice_frames"] * 16000 // ready["fps"]
    for _, seq, start_sample, pcm, jpegs, infer_ms in chunks:
        assert start_sample == seq * slice_samples and len(pcm) == slice_samples * 2
        assert len(jpegs) == ready["slice_frames"]
        assert all(j[:3] == b"\xff\xd8\xff" and j[-2:] == b"\xff\xd9" for j in jpegs)   # whole JPEG images
    import cv2
    image = cv2.imdecode(np.frombuffer(chunks[-1][4][0], dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image.shape[:2] == (ready["height"], ready["width"])

    assert "주문" in caller.of("transcript")[0]["text"]
    assert caller.of("chat_done")
    speech = next(m for m in caller.of("speech") if m["phase"] == "reply")
    by_seq = {c[1]: c for c in chunks}
    assert not any(by_seq[0][3].strip(b"\x00"))                                   # idle: silent audio
    voiced = np.frombuffer(by_seq[speech["seq"]][3], dtype="<i2").astype(np.float32)
    assert np.sqrt((voiced ** 2).mean()) > 100                                    # the reply is in this chunk
    delay = (by_seq[speech["seq"]][0] - caller.speech_end) * 1000
    infer = [c[5] for c in chunks[3:]]
    print(f"\n[avatar] end of speech -> chunk carrying the first reply sound {delay:.0f} ms "
          f"(server: since_utterance {speech['since_utterance_ms']:.0f} ms); "
          f"{len(chunks)} FHRT chunks, video inference median {np.median(infer):.0f} ms "
          f"per {1000 * ready['slice_frames'] / ready['fps']:.0f} ms slice")
    # the seat is free again after the disconnect
    from agentkit.runtime import avatar as avatar_module
    deadline = time.monotonic() + 5
    while avatar_module.holder().busy and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not avatar_module.holder().busy and avatar_module.holder().loaded
