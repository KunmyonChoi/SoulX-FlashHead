"""Fakes for the voice tests: a scripted ReplyEngine, and audio parts that need no model."""
import asyncio
import struct
import time
from collections import deque

import numpy as np

from agentkit.contracts import AgentDefinition, ReplyEvent, VoiceSettings

BYTES_PER_CHAR = 3200          # FakeTTS: 0.1 s of 16 kHz PCM per character


def ev(type, text="", **data):
    return ReplyEvent(type=type, text=text, data=data)


class FakeRecorder:
    def __init__(self):
        self.audios = []       # (session_id, turn, role, bytes)
        self.spans = []

    def start_session(self, meta): pass

    def span(self, span, pii_paths=None):
        self.spans.append(span)

    def turn(self, turn): pass

    def audio(self, session_id, turn, role, pcm16):
        self.audios.append((session_id, turn, role, pcm16))

    def end_session(self, session_id, status): pass


class FakeEngine:
    """script: user_text -> list of ReplyEvent and numbers (seconds to wait before the next event)."""

    def __init__(self, script, session_id="s1", voice=None, recorder=True, **definition):
        self.session_id = session_id
        self.definition = AgentDefinition(id="fake", name="가짜", persona="시험용", voice=voice or VoiceSettings(),
                                          **definition)
        self.turn = 0
        self.handed_off = False
        self.script = script
        self.opening_script = None   # steps of open(); None: nothing but "done"
        self.act_script = None       # (name, arguments) -> steps of act()
        self.opened = 0
        self.acts = []               # (name, arguments, turn)
        self.calls = []
        self.truncated = []
        self.outputs = []
        self.closed = None
        self.recorder = FakeRecorder() if recorder else None

    async def reply(self, user_text, *, language=None, speculative=False, t_user_end=None):
        self.turn += 1
        call = {"text": user_text, "language": language, "speculative": speculative, "t_user_end": t_user_end,
                "turn": self.turn, "finished": False, "cancelled": False}
        self.calls.append(call)
        try:
            for step in self.script(user_text):
                if isinstance(step, (int, float)):
                    await asyncio.sleep(step)
                else:
                    yield step
            call["finished"] = True
        finally:
            if not call["finished"]:
                call["cancelled"] = True
                if speculative:
                    self.turn -= 1   # a cancelled speculative reply leaves no trace

    async def open(self):
        """The agent's opening: turn 0, no customer turn is counted."""
        self.opened += 1
        for step in self.opening_script or [ev("done", "")]:
            if isinstance(step, (int, float)):
                await asyncio.sleep(step)
            else:
                yield step

    async def act(self, name, arguments):
        """A touch: a customer turn whose events come from act_script."""
        if self.act_script is None:
            yield ev("done", "", error=f"unknown action: {name}")
            return
        self.turn += 1
        self.acts.append((name, dict(arguments), self.turn))
        for step in self.act_script(name, arguments):
            if isinstance(step, (int, float)):
                await asyncio.sleep(step)
            else:
                yield step

    def truncate_last_reply(self, heard_text):
        self.truncated.append(heard_text)

    def mark_output(self, phase, t):
        self.outputs.append((phase, t))

    async def close(self, status="completed"):
        self.closed = status


def simple_script(*pieces, wait=0.0, tail=0.0):
    """A reply that streams and speaks the given sentences."""
    def script(user_text):
        steps = [ev("llm", state="start", round=1)]
        if wait:
            steps.append(wait)
        for piece in pieces:
            steps += [ev("delta", piece + " "), ev("piece", piece)]
        if tail:
            steps.append(tail)
        steps.append(ev("done", " ".join(pieces)))
        return steps
    return script


class FakeTTS:
    """0.1 s per character; the samples spell the text, so tests can tell whose audio is whose."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = []

    def synthesize(self, text, voice, lang, speed=None):
        self.calls.append((text, voice, lang))
        if self.delay:
            time.sleep(self.delay)
        return fake_pcm(text)


def fake_pcm(text):
    return b"".join(struct.pack("<h", 1000 + (ord(c) % 20000)) * (BYTES_PER_CHAR // 2) for c in text)


class FakeSTT:
    def __init__(self, texts=()):
        self.texts = deque(texts)
        self.heard = []

    def transcribe(self, audio, language=None):
        self.heard.append(audio)
        return (self.texts.popleft() if self.texts else ""), (language if language in ("ko", "en") else "ko")


class FakeEndpointer:
    """feed() returns the next scripted list of events instead of looking at the audio."""
    WINDOW = 512

    def __init__(self):
        self.script = deque()
        self.in_speech = False
        self.voiced = 0
        self.judge = None
        self.last = {"silence": 0.7, "probability": None}
        self.resets = 0

    def reset(self):
        self.resets += 1
        self.in_speech = False
        self.voiced = 0

    def feed(self, pcm):
        return self.script.popleft() if self.script else []


class FakeParts:
    """Stands in for audio.SharedAudio."""

    def __init__(self, tts=None, stt=None):
        self.tts = tts or FakeTTS()
        self.stt = stt or FakeSTT()
        self.tts_pool = None       # default executor
        self.stt_pool = None
        self.filler_cache = {}
        self.endpointers = []

    def recognizer(self, name):
        return self.stt

    def smart_turn(self):
        return None

    def new_endpointer(self):
        endpointer = FakeEndpointer()
        self.endpointers.append(endpointer)
        return endpointer


class Client:
    """Collects what a session sends."""

    def __init__(self):
        self.json = []
        self.binary = []           # (time received, bytes)

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, payload):
        self.binary.append((time.monotonic(), payload))

    def of(self, type, **match):
        return [m for m in self.json if m.get("type") == type and all(m.get(k) == v for k, v in match.items())]

    def chunks(self):
        """FHAU chunks as (seq, pcm)."""
        out = []
        for _, data in self.binary:
            assert data[:4] == b"FHAU", data[:4]
            seq, size = struct.unpack("<II", data[4:12])
            assert len(data) == 12 + size
            out.append((seq, data[12:]))
        return out

    def pcm(self):
        return b"".join(pcm for _, pcm in self.chunks())


def parse_video_chunk(data):
    """An FHRT chunk as (seq, start_sample, pcm, [jpeg, ...], infer_ms)."""
    assert data[:4] == b"FHRT", data[:4]
    seq, start_sample, frames, pcm_bytes, infer_ms, dropped = struct.unpack("<IIIIfI", data[4:28])
    offset = 28
    pcm = data[offset:offset + pcm_bytes]
    offset += pcm_bytes
    jpegs = []
    for _ in range(frames):
        (length,) = struct.unpack("<I", data[offset:offset + 4])
        offset += 4
        jpegs.append(data[offset:offset + length])
        offset += length
    assert offset == len(data)
    return seq, start_sample, pcm, jpegs, infer_ms


async def until(condition, timeout=5.0, step=0.01):
    """Waits until condition() is true; fails the test if it never is."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(step)


def silence_packet(seconds=0.1):
    return np.zeros(int(16000 * seconds), dtype="<i2").tobytes()
