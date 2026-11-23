"""
The voice and avatar channels: listening (end of turn, recognition) and speaking (synthesis, pacing,
interruptions) on top of a ReplyEngine.

This is the audio half of v1's `run_tts` (realtime_mic_app.py). What produced the reply there
(`chat()`: prompt, LLM, tools, guards) is now the engine; this module turns the engine's "filler"
and "piece" events into speech and passes the other events on to the client.

    serve_voice(ws, engine, channel)        entry point for the server's /ws branch
    VoiceSession(engine, send_json, send_bytes, settings, avatar=None)

Wire formats (server -> client, binary):
    avatar: "FHRT" + <IIIIfI>(seq, start_sample, frame_count, pcm_bytes, infer_ms, dropped) + PCM
            + frame_count x (<I> length + JPEG)                      one per slice, also when silent
    voice:  "FHAU" + <II>(seq, pcm_bytes) + PCM                      only while there is speech
PCM is 16 kHz 16-bit mono in both directions.
"""
from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
import re
import struct
import time

import numpy as np

from .. import contracts
from . import audio as audio_parts
from . import client_features
from ..tools.consent import read_consent
from .audio import (HALLUCINATION, SAMPLE_RATE, SENTENCE_END, Endpointer, bare, is_backchannel, is_echo,
                    is_hallucination, is_short_answer, speech_seconds, split_for_tts, text_language)

logger = logging.getLogger("agentkit.voice")

MAGIC_VIDEO = b"FHRT"
MAGIC_AUDIO = b"FHAU"
# While the agent waits for a one-word answer, speech this short still counts as an utterance
# (a spoken "네" lasts 0.15 to 0.3 s; the usual threshold of 0.25 s threw many of them away as noise).
SHORT_ANSWER_VOICED = 0.12
OPENING_ID = "opening"          # the message id of the agent's opening words and screens
AUDIO_CHUNK_SECONDS = 0.48      # pace of the voice-only channel
SENTENCE_GAP_SECONDS = 0.3
# Rough delay between the server sending a chunk and the browser starting to play it.
CLIENT_PLAYBACK_DELAY = 0.3
# unmute_gate: voiced time after which speech over the agent counts as an interruption.
BARGE_IN_SECONDS = 1.2
# Characters a TTS would read aloud or stumble on: markdown markers and emoji.
UNSPEAKABLE = re.compile("[*#`_~>|\\[\\]\U0001F000-\U0001FAFF☀-➿️]")

SETTING_CHOICES = {
    "stt": ("whisper", "qwen"),
    "endpoint": ("silence", "smart"),
    "interrupt_mode": ("basic", "unmute", "unmute_gate"),
    "language": ("ko", "en", "auto"),
}


def voice_settings(definition, overrides=None):
    """The session's settings: the definition's `voice` block and language, then client overrides."""
    voice = getattr(definition, "voice", None) or contracts.VoiceSettings()
    settings = {
        "voice": voice.tts_voice,
        "stt": voice.stt,
        "endpoint": voice.endpoint,
        "interrupt_mode": voice.interrupt_mode,
        "barge_in": voice.barge_in,
        "speculative": voice.speculative,
        "language": getattr(definition, "language", "ko"),
        "speed": None,
    }
    apply_settings(settings, overrides or {})
    return settings


def apply_settings(settings, request):
    """Applies a client "config" message. Unknown keys and invalid values are ignored.
    Returns the names that changed."""
    if hasattr(request, "model_dump"):      # a contracts.VoiceSettings
        request = request.model_dump()
    request = dict(request)
    if "tts_voice" in request and "voice" not in request:
        request["voice"] = request["tts_voice"]
    changed = []
    for key in settings:
        if key not in request:
            continue
        value = request[key]
        if key in SETTING_CHOICES and value not in SETTING_CHOICES[key]:
            continue
        if key in ("barge_in", "speculative"):
            # query-string values arrive as text: "0" and "false" are off
            value = value.strip().lower() in ("1", "true", "yes", "on") if isinstance(value, str) else bool(value)
        if key == "voice" and not value:
            continue
        if settings[key] != value:
            settings[key] = value
            changed.append(key)
    return changed


def pack_audio_chunk(seq, pcm):
    return MAGIC_AUDIO + struct.pack("<II", seq, len(pcm)) + pcm


def pack_video_chunk(seq, start_sample, pcm, jpegs, infer_ms, dropped_samples=0):
    header = MAGIC_VIDEO + struct.pack("<IIIIfI", seq, start_sample, len(jpegs), len(pcm), infer_ms, dropped_samples)
    return header + pcm + b"".join(struct.pack("<I", len(j)) + j for j in jpegs)


class VoiceSession:
    """One voice or avatar conversation.

    engine:      contracts.ReplyEngine
    send_json:   async (dict) -> None;  send_bytes: async (bytes) -> None
    settings:    dict from voice_settings() (or None: taken from engine.definition)
    avatar:      avatar.AvatarSession for the avatar channel, None for voice only
    parts:       the shared audio models (audio.SharedAudio); tests pass a fake

    The owner calls `await prepare()`, runs `run()` as a task, feeds `on_audio(bytes)` and
    `on_message(dict)` from the socket, and finally `await close()`.
    """

    def __init__(self, engine, send_json, send_bytes, settings=None, avatar=None, *, parts=None,
                 chunk_seconds=AUDIO_CHUNK_SECONDS):
        self.engine = engine
        self._send_json_raw = send_json
        self._send_bytes_raw = send_bytes
        self.settings = settings if settings is not None else voice_settings(engine.definition)
        self.avatar = avatar
        self.parts = parts or audio_parts.shared()
        self.recorder = getattr(engine, "recorder", None)
        self.sample_rate = SAMPLE_RATE
        if avatar is not None:
            self.slice_samples = avatar.slice_samples
            self.duration = avatar.slice_frames / avatar.fps
        else:
            self.slice_samples = int(chunk_seconds * SAMPLE_RATE)
            self.duration = chunk_seconds
        self.slice_bytes = self.slice_samples * 2
        self.sentence_gap = b"\x00" * (int(SENTENCE_GAP_SECONDS * SAMPLE_RATE) * 2)

        self.speech = bytearray()   # synthesised PCM not yet sent
        self.marks = []             # where each request's first sound starts inside `speech`
        self.requests = asyncio.Queue()
        self.generation = 0         # bumped to cancel queued and in-flight speech
        self.chat_task = None
        self.synth_busy = False
        # A speculative reply: prepared during a pause, not audible until committed.
        self.hold = False           # while on, synthesised speech stays in `speech`
        self.spec = {"task": None, "text": None, "request": None, "outbox": [], "committed": False}
        # Bookkeeping for "unmute" interrupts. Positions are byte offsets into the whole speech
        # stream of this session (sent or dropped), so they survive `speech` being cut.
        self.seq = 0
        self.stream_base = 0
        self.in_flight = False
        self.chunk_speech = {}      # chunk seq -> (stream position of its first speech byte, speech bytes in it)
        # pieces: spoken text + stream range; queued/synthesised/done tell whether more was coming
        self.last_reply = {"token": None, "id": None, "pieces": [], "queued": 0, "synthesised": 0, "done": False}
        # pending: asyncio.Event set once the browser has reported playback
        # reply: the interrupted reply as it stood when it was cut off; task: its (cancelled) task
        self.truncation = {"pending": None, "reply": None, "task": None}
        self.kick = asyncio.Event()   # wakes the main loop right after a flush
        # the avatar is resting (the last chunk had no sound and none is waiting): speech that arrives now starts
        # at once instead of on the next 0.96 s beat (as in v1, 2026-11-23: the beat wait was ~0.4 s of 1.7 s)
        self.idle = True
        self.wake = asyncio.Event()   # voice only: there is speech to send
        self.play_end = 0.0           # when the audio sent so far has finished playing (estimate)
        self.endpointer = None
        self.judge = None
        self.listen = {"transcribing": False, "busy_until": 0.0, "state": None, "turn": 0, "over_avatar": False}
        self.closed = False
        self.notices = deque()         # news for the customer (engine.notify), waiting for a quiet moment
        self._synth_task = None
        self._background = set()
        self._agent_audio = {"turn": None, "pcm": bytearray()}
        self._audio_handed = set()    # (turn, role) already given to the recorder
        self.first_audio_ms = []      # since_utterance_ms of each reply's first sound (for tests / logs)

    # ---- plumbing ---------------------------------------------------------------------------

    async def send_json(self, payload):
        try:
            await self._send_json_raw(payload)
        except Exception:
            pass  # the socket closed while a task was still winding down

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _in_pool(self, pool, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(pool, fn, *args)

    def _span(self, kind, turn, t_start, t_end, *, name="", ok=True, input=None, output=None, attrs=None):
        if self.recorder is None:
            return
        try:
            self.recorder.span(contracts.Span(
                session_id=self.engine.session_id, turn=turn, kind=kind, name=name, t_start=t_start,
                t_end=t_end, ok=ok, input=input, output=output, attrs=attrs or {}))
        except Exception as e:  # recording never disturbs the conversation
            logger.debug("span not recorded: %r", e)

    def _hand_audio(self, turn, role, pcm):
        if self.recorder is None or not pcm or turn is None or (turn, role) in self._audio_handed:
            return
        self._audio_handed.add((turn, role))
        try:
            self.recorder.audio(self.engine.session_id, turn, role, bytes(pcm))
        except Exception as e:
            logger.debug("audio not recorded: %r", e)

    def _flush_agent_audio(self):
        held = self._agent_audio
        if held["pcm"]:
            self._hand_audio(held["turn"], "agent", held["pcm"])
        self._agent_audio = {"turn": held["turn"], "pcm": bytearray()}

    def _hand_user_audio(self, request):
        """Gives the user's utterance to the recorder once its turn number is known and the turn is real."""
        if request.get("audio") is None or request.get("turn") is None:
            return
        if request.get("speculative") and not request.get("committed"):
            return
        self._hand_audio(request["turn"], "user", audio_parts.float_to_pcm16(request.pop("audio")))

    async def prepare(self):
        """Loads what the current settings need (Whisper, VAD, Smart Turn). Problems become notices."""
        loop = asyncio.get_running_loop()
        if self.endpointer is None:
            try:
                self.endpointer = await loop.run_in_executor(None, self.parts.new_endpointer)
            except Exception as e:
                logger.warning("VAD unavailable: %r", e)
                await self.send_json({"type": "notice", "message": f"음성 구간 감지를 쓸 수 없습니다: {e}"})
        if self.settings["stt"] == "whisper":
            try:
                await self._in_pool(self.parts.stt_pool, self.parts.recognizer, "whisper")
            except Exception as e:
                logger.warning("Speech recognition unavailable: %r", e)
                await self.send_json({"type": "notice", "message": f"음성 인식을 쓸 수 없습니다: {e}"})
        self.judge = None
        if self.settings["endpoint"] == "smart":
            smart = await loop.run_in_executor(None, self.parts.smart_turn)
            if smart is None:
                await self.send_json({"type": "notice", "message": "Smart Turn 모델이 없어 고정 침묵으로 말 끝을 판단합니다."})
            else:
                self.judge = smart.probability

    def stt_language(self):
        return self.settings["language"]

    def speech_language(self, text):
        """The TTS language for one sentence."""
        if self.settings["language"] in ("ko", "en"):
            return self.settings["language"]
        return text_language(text)

    # ---- speaking: synthesis queue ------------------------------------------------------------

    async def _synthesizer(self):
        loop = asyncio.get_running_loop()
        while True:
            gen, request = await self.requests.get()
            self.synth_busy = True
            first = True
            for sentence in split_for_tts(str(request.get("text", ""))):
                if gen != self.generation:
                    break
                started, started_at = time.perf_counter(), time.time()
                voice = request.get("voice") or self.settings["voice"]
                lang = request.get("lang") or self.speech_language(sentence)
                # a reply prepared in secret stays off the page until committed: its messages wait
                # with the reply's other messages (they used to be dropped, so a committed reply
                # showed no synthesis for the sentences that were ready early)
                tell = self._spec_out if self.hold and request.get("speculative") else self.send_json
                shown = not self.hold or tell is not self.send_json
                if shown:
                    await tell({"type": "tts", "id": request.get("id"), "state": "start",
                                "chars": len(sentence), "filler": bool(request.get("filler"))})
                cache_key = (voice, lang, sentence)
                try:
                    pcm = self.parts.filler_cache.get(cache_key) if request.get("filler") else None
                    if pcm is None:
                        pcm = await loop.run_in_executor(
                            self.parts.tts_pool, self.parts.tts.synthesize, sentence, voice, lang,
                            self.settings.get("speed"))
                        if request.get("filler"):
                            self.parts.filler_cache[cache_key] = pcm
                except Exception as e:
                    logger.warning("TTS failed: %r", e)
                    self._span("tts", request.get("turn_ref", {}).get("turn") or self.engine.turn, started_at,
                               time.time(), ok=False, input={"chars": len(sentence)}, attrs={"error": str(e)[:200]})
                    await self.send_json({"type": "notice", "message": f"TTS 실패: {e}"})
                    break
                if gen != self.generation:
                    break
                seconds = len(pcm) / 2 / self.sample_rate
                synth_ms = (time.perf_counter() - started) * 1000
                self._span("tts", request.get("turn_ref", {}).get("turn") or self.engine.turn, started_at,
                           time.time(), input={"chars": len(sentence)},
                           attrs={"seconds": seconds, "filler": bool(request.get("filler")), "voice": voice})
                if shown:
                    await tell({"type": "tts", "id": request.get("id"), "state": "done", "ms": synth_ms,
                                "seconds": seconds, "filler": bool(request.get("filler"))})
                if gen != self.generation:
                    break
                if first and request.get("phase"):
                    self.marks.append({
                        "phase": request["phase"],
                        "pos": len(self.speech),
                        "id": request.get("id"),
                        "synth_ms": synth_ms,
                        "llm_ms": request.get("llm_ms"),
                        "stt_ms": request.get("stt_ms"),
                        "utterance_end": request.get("utterance_end"),
                        "speculative": bool(request.get("speculative")),
                        "tool_ms": request.get("tool_ms"),
                    })
                first = False
                mine = request.get("reply") is not None and request.get("reply") is self.last_reply["token"]
                if mine:
                    start = self.stream_base + len(self.speech)
                    self.last_reply["pieces"].append({"text": sentence, "start": start, "end": start + len(pcm)})
                self.speech.extend(pcm)
                self.speech.extend(self.sentence_gap)
                if self.avatar and self.idle and not self.hold:
                    self.idle = False
                    self.kick.set()          # make the next chunk now; the page cuts the resting one it plays
                self.wake.set()
            if request.get("reply") is not None and request.get("reply") is self.last_reply["token"]:
                self.last_reply["synthesised"] += 1
            self.synth_busy = self.requests.qsize() > 0

    # ---- speaking: one reply --------------------------------------------------------------------

    async def _reply(self, gen, request, out=None):
        """Runs the engine for one user turn: forwards its events and queues its speech.

        out: where the client messages go (a speculative reply buffers them until committed)."""
        out = out or self.send_json
        loop = asyncio.get_running_loop()
        request_id = request.get("id")
        token = object()
        turn_ref = request.setdefault("turn_ref", {"turn": None})
        self.last_reply = {"token": token, "id": request_id, "pieces": [], "queued": 0, "synthesised": 0,
                           "done": False}
        started = time.perf_counter()
        sound_queued = False    # anything at all, filler included
        answer_queued = False
        tool_started = None
        tool_ms = None
        done_sent = False
        events = None

        def queue_speech(text, **extra):
            nonlocal sound_queued
            self.requests.put_nowait((gen, {
                "text": text,
                "id": request_id,
                # only the first sound carries the timings, so the client can show the delay
                "llm_ms": None if sound_queued else (time.perf_counter() - started) * 1000,
                "stt_ms": None if sound_queued else request.get("stt_ms"),
                "utterance_end": request.get("utterance_end"),
                "speculative": request.get("speculative"),
                "turn_ref": turn_ref,
                **extra,
            }))
            sound_queued = True

        try:
            # the reply this one replaces must be out of the engine before the next turn starts
            previous = self.truncation["task"]
            if previous is not None and not previous.done():
                await asyncio.wait({previous}, timeout=2.0)
            if self.truncation["pending"]:
                # let the browser's playback report fix up the interrupted reply first
                pending = self.truncation["pending"]
                try:
                    await asyncio.wait_for(pending.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass
                if self.truncation["pending"] is pending:
                    self.truncation["pending"] = None
            # a report that comes after this point could only cut the wrong reply (the engine's
            # "last reply" is about to be this one), so it is ignored
            self.truncation["reply"] = None
            self.truncation["task"] = None
            self._flush_agent_audio()  # what was said so far belongs to the turn before

            language = self.settings["language"]
            if language not in ("ko", "en"):
                language = request.get("language") or text_language(str(request.get("text", "")))
            started = time.perf_counter()
            if request.get("opening"):
                events = self.engine.open()          # the agent speaks first (turn 0)
            elif request.get("notice"):
                events = self.engine.notify(request["text"])   # the agent speaks up about a finished task
            elif request.get("action") is not None:
                events = self.engine.act(request["action"]["name"], request["action"]["arguments"])
            else:
                events = self.engine.reply(
                    str(request.get("text", "")).strip(), language=language,
                    speculative=bool(request.get("speculative")), t_user_end=request.get("t_user_end"))
            request["turn_base"] = self.engine.turn  # the engine counts the turn when the iteration starts
            async for event in events:
                if turn_ref["turn"] is None:
                    turn_ref["turn"] = request["turn"] = self.engine.turn
                    self._agent_audio["turn"] = turn_ref["turn"]  # sound sent from here on belongs to this turn
                    self._hand_user_audio(request)
                kind = event.type
                if kind == "delta":
                    await out({"type": "chat_delta", "id": request_id, "text": event.text})
                elif kind == "replace":
                    await out({"type": "chat_replace", "id": request_id, "text": event.text})
                elif kind == "piece":
                    text = UNSPEAKABLE.sub("", event.text).strip()
                    if not text:
                        continue
                    if request.get("opening"):
                        # the opening words are not streamed as deltas: show them as they are queued
                        await out({"type": "chat_delta", "id": request_id, "text": event.text})
                    self.last_reply["queued"] += 1
                    phase = None if answer_queued else ("answer" if tool_ms is not None else "reply")
                    queue_speech(text, reply=token, phase=phase, tool_ms=tool_ms)
                    answer_queued = True
                elif kind == "filler":
                    # only as the first sound: once the reply is being spoken a "one moment" is noise
                    if event.text.strip() and not sound_queued:
                        queue_speech(event.text.strip(), filler=True, phase="filler")
                elif kind == "tool":
                    data = event.data
                    if data.get("state") == "start" and tool_started is None:
                        tool_started = time.perf_counter()
                    elif data.get("state") == "done" and tool_started is not None:
                        tool_ms = (time.perf_counter() - tool_started) * 1000
                    if data.get("effect") == "write" or data.get("needs_confirmation") or data.get("executed"):
                        request["write_seen"] = True
                    await out({"type": "tool", "id": request_id, **data})
                elif kind in ("knowledge", "llm", "handoff"):
                    await out({"type": kind, "id": request_id, **event.data})
                elif kind == "display":
                    # a screen for the customer, never spoken; a speculative reply holds it back with
                    # its other messages until it is committed (`out`)
                    await out({"type": "display", "id": request_id, "kind": event.data.get("kind"),
                               "data": event.data.get("data"), "source": event.data.get("source", "")})
                elif kind == "done":
                    request["finished"] = True
                    self.last_reply["done"] = self.last_reply["token"] is token
                    if gen == self.generation:
                        done_sent = True
                        await out({"type": "chat_done", "id": request_id, "text": event.text,
                                   "turn": turn_ref["turn"],
                                   **({"error": event.data["error"]} if event.data.get("error") else {})})
            if self.last_reply["token"] is token:
                self.last_reply["done"] = True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Reply failed: %r", e)
            await out({"type": "notice", "message": f"답변 생성 실패: {e}"})
        finally:
            if events is not None and hasattr(events, "aclose"):
                try:
                    await events.aclose()  # cancels the engine's turn if it was still running
                except BaseException as e:
                    logger.debug("closing the reply iterator: %r", e)
            if gen == self.generation and not done_sent:
                await out({"type": "chat_done", "id": request_id})

    def _start_reply(self, request):
        self.chat_task = asyncio.create_task(self._reply(self.generation, request))
        return self.chat_task

    def start_opening(self):
        """The agent speaks first (definition.opening): its screens go to the client and its words
        through the speech queue like any reply, so the customer can talk over them. Call once,
        after "ready". Returns the task, or None when the agent has no opening."""
        if not client_features(self.engine.definition)["opening"] or not hasattr(self.engine, "open"):
            return None
        return self._start_reply({"id": OPENING_ID, "opening": True, "text": "", "t_user_end": time.time(),
                                  "utterance_end": None})

    # ---- interruptions --------------------------------------------------------------------------

    async def cancel_speech(self):
        """Stops what is being said or prepared. In the unmute modes the browser is told to flush
        and to report how far playback got; the report cuts the reply (see apply_playback)."""
        loop = asyncio.get_running_loop()
        if self.spec["task"] is not None:
            await self.discard_speculation()  # a reply still being prepared in secret goes too
        reply = self.last_reply
        task = self.chat_task
        # Nothing is audible or on its way if every reply so far was complete and its last chunk
        # has played. Then there is nothing to flush or cut, and the browser is not asked (v1 asked
        # anyway; here that would cost a client that does not answer half a second per turn).
        quiet = ((reply["token"] is None or (reply["done"] and reply["synthesised"] == reply["queued"]))
                 and not self.speech and not self.synth_busy and self.requests.empty()
                 and (task is None or task.done()) and loop.time() >= self.play_end + 0.5)
        self.generation += 1
        self.stream_base += len(self.speech)
        self.speech.clear()
        self.marks.clear()
        while not self.requests.empty():
            self.requests.get_nowait()
        if task is not None:
            task.cancel()
            self.chat_task = None
        if self.settings["interrupt_mode"].startswith("unmute") and not quiet:
            # Everything up to the chunk being generated right now is stale for the browser.
            first_fresh = self.seq + (1 if self.in_flight else 0)
            if reply["token"] is not None:
                self.truncation["reply"] = dict(reply)
                self.truncation["task"] = task
                reply["token"] = None
            self.truncation["pending"] = asyncio.Event()
            await self.send_json({"type": "interrupt", "seq": first_fresh})
            self.listen["busy_until"] = loop.time() + 0.3
            self.play_end = loop.time()  # the browser drops what it had buffered
            self.kick.set()
            self.wake.set()
        self._flush_agent_audio()

    def apply_playback(self, seq, offset):
        """Works out what of the interrupted reply the browser actually played.

        Returns (request id, text heard), or (None, None) if there is nothing to cut."""
        cut, self.truncation["reply"] = self.truncation["reply"], None
        if cut is None:
            return None, None
        pieces = cut["pieces"]
        if seq in self.chunk_speech:
            start, length = self.chunk_speech[seq]
            played = start + min(int(offset * self.sample_rate) * 2, length)
        else:
            played = -1  # nothing of this reply reached the speakers
        logger.debug("playback report: seq=%s offset=%.2f chunk=%s played=%s pieces=%s queued=%s synthesised=%s "
                     "done=%s", seq, offset, self.chunk_speech.get(seq), played,
                     [(p["start"], p["end"]) for p in pieces], cut["queued"], cut["synthesised"], cut["done"])
        heard = []
        # complete = the engine finished, every piece was synthesised, and (below) all of it was played
        complete = cut["done"] and cut["synthesised"] == cut["queued"]
        for piece in pieces:
            if piece["end"] <= played:
                heard.append(piece["text"])
                continue
            complete = False
            if piece["start"] < played:
                # no word timings from the TTS: assume an even speaking rate within the piece
                ratio = (played - piece["start"]) / (piece["end"] - piece["start"])
                partial = piece["text"][:int(len(piece["text"]) * ratio)]
                heard.append(partial[:partial.rfind(" ")] if " " in partial else "")
            break
        if complete:
            return None, None  # the whole reply was heard
        return cut["id"], " ".join(part for part in heard if part).strip()

    async def on_played(self, seq, offset):
        """The browser's answer to "interrupt": the latest chunk that had started and how far into it."""
        task = self.truncation["task"]
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=1.0)  # the cancelled turn must be out of the engine first
        interrupted_id, heard = self.apply_playback(seq, offset)
        if heard is not None:
            loggable = getattr(self.engine, "_loggable", None)
            logger.info("Interrupted reply cut to what was heard: %r", loggable(heard) if loggable else heard)
            try:
                self.engine.truncate_last_reply(heard)
            except Exception as e:
                logger.warning("truncate_last_reply failed: %r", e)
            # (the "interrupt" span is the engine's to record: it knows which turn was cut)
            await self.send_json({"type": "chat_truncate", "id": interrupted_id, "text": heard})
        self.truncation["task"] = None
        if self.truncation["pending"]:
            self.truncation["pending"].set()

    # ---- listening ------------------------------------------------------------------------------

    def avatar_busy(self):
        """True while a reply is being prepared or (as far as the server can tell) still playing."""
        if self.hold:
            return False  # only a speculative reply exists, and nobody can hear it yet
        task = self.chat_task
        return bool(
            self.speech or self.synth_busy or not self.requests.empty()
            or (task and not task.done()) or asyncio.get_running_loop().time() < self.listen["busy_until"]
        )

    def only_tail(self):
        """The agent has nothing more to say: all that keeps it "busy" is the allowance for the
        browser still playing the last chunk."""
        task = self.chat_task
        return not (self.speech or self.synth_busy or not self.requests.empty() or (task and not task.done()))

    def expecting_answer(self):
        """True while the customer owes the agent a short answer: a write tool waits for consent, or
        the agent's last words were a question. "네" is then an answer, not a listener's noise."""
        engine = self.engine
        try:
            gateway = getattr(engine, "gateway", None)
            if gateway is not None and gateway.pending(engine.session_id):
                return True
        except Exception:
            pass
        for message in reversed(getattr(engine, "history", None) or []):
            if message.get("role") == "assistant" and message.get("content"):
                return str(message["content"]).rstrip().endswith(("?", "？"))
        return False

    async def update_listen_state(self):
        if self.listen["transcribing"]:
            state = "transcribing"
        elif self.avatar_busy():
            state = "speaking"
        elif self.endpointer is not None and self.endpointer.in_speech:
            state = "hearing"
        else:
            state = "listening"
        if state != self.listen["state"]:
            self.listen["state"] = state
            await self.send_json({"type": "listen_state", "state": state})

    async def _transcribe(self, audio):
        name = self.settings["stt"]
        recognizer = await self._in_pool(self.parts.stt_pool, self.parts.recognizer, name)
        pool = self.parts.stt_pool if name == "whisper" else None  # the remote recogniser needs no GPU turn
        return await self._in_pool(pool, recognizer.transcribe, audio, self.stt_language())

    def _new_request(self, text, heard_in, stt_ms, utterance_end, audio, **extra):
        loop = asyncio.get_running_loop()
        self.listen["turn"] += 1
        return {
            "id": f"voice-{self.listen['turn']}",
            "text": text,
            "stt_ms": stt_ms,
            "utterance_end": utterance_end,                                  # loop clock, for the client's delay
            "t_user_end": time.time() - (loop.time() - utterance_end),       # epoch, for the record
            "language": heard_in,
            "audio": audio,
            **extra,
        }

    def _record_heard(self, request, closed):
        """Spans of a turn that is going ahead: how its end was decided and what was recognised."""
        # The turn this utterance becomes in the engine, which is also the turn its audio is stored
        # under. A committed speculative reply is already running, so the engine may have counted
        # it: then the number is the one read when that reply started (see _reply).
        turn = request.get("turn")
        if turn is None:
            turn = request["turn_base"] + 1 if "turn_base" in request else self.engine.turn + 1
        end = request["t_user_end"]
        self._span("endpoint", turn, end, end + closed["silence"], attrs={
            "mode": "smart" if closed.get("probability") is not None else "silence",
            "silence_s": closed["silence"], "probability": closed.get("probability")})
        self._span("stt", turn, request["stt_at"], request["stt_at"] + request["stt_ms"] / 1000,
                   output={"text": request["text"]},
                   attrs={"engine": request["engine"], "language": request["language"],
                          **audio_parts.stt_quality(request.get("heard"), request.get("audio"))})

    async def _spec_out(self, payload):
        if self.spec["committed"]:
            await self.send_json(payload)
        else:
            self.spec["outbox"].append(payload)

    async def _speculate(self, audio, utterance_end):
        """Prepares a reply to what has been said so far, in case the speaker is done."""
        started, started_at = time.perf_counter(), time.time()
        engine_name = self.settings["stt"]
        heard = await self._transcribe(audio)
        text, heard_in = heard
        if len(text) < 2 or not SENTENCE_END.search(text) or HALLUCINATION.match(bare(text)):
            # mid-sentence ("그러니까 내일..."): the speaker will go on, a reply now would be thrown away.
            # "감사합니다." may be made up from noise: the whole utterance is checked in on_utterance instead.
            self.spec["text"] = ""
            return
        request = self._new_request(text, heard_in, (time.perf_counter() - started) * 1000, utterance_end, None,
                                    speculative=True, stt_at=started_at, engine=engine_name, heard=heard)
        self.spec["outbox"].append({"type": "transcript", "id": request["id"], "text": text,
                                    "stt_ms": request["stt_ms"], "language": heard_in, "engine": engine_name,
                                    "speculative": True})
        self.spec["request"] = request
        self.hold = True
        self.spec["text"] = text
        await self._reply(self.generation, request, out=self._spec_out)

    async def discard_speculation(self):
        """The speaker went on: forget the reply that was being prepared. Cancelling the task
        cancels the engine's iteration, which (by contract) leaves no trace in the history."""
        task = self.spec["task"]
        if task is None:
            return
        self.spec["task"] = None
        task.cancel()
        self.generation += 1
        self.stream_base += len(self.speech)
        self.speech.clear()
        self.marks.clear()
        while not self.requests.empty():
            self.requests.get_nowait()
        self.last_reply["token"] = None
        self.hold = False
        await asyncio.wait({task}, timeout=2.0)  # the engine must have let go before the next turn
        request = self.spec["request"] or {}
        forget = getattr(self.engine, "discard_last_turn", None)
        if request.get("finished") and forget is not None and request.get("turn") == self.engine.turn:
            # The prepared reply was already complete, so there was no iteration left to cancel and
            # the turn is in the engine's history. WP4's engine offers this (outside the contract).
            try:
                forget()
            except Exception as e:
                logger.warning("discard_last_turn failed: %r", e)
        if self.spec["text"]:  # otherwise nothing but a transcription was spent
            await self.send_json({"type": "metric", "name": "speculation_discarded", "text": self.spec["text"]})

    async def begin_speculation(self, audio):
        await self.discard_speculation()
        self.spec.update({"text": None, "request": None, "outbox": [], "committed": False})
        loop = asyncio.get_running_loop()
        self.spec["task"] = asyncio.create_task(self._speculate(audio, loop.time() - Endpointer.PAUSE_SILENCE))

    async def commit_speculation(self, audio, utterance_end, closed):
        """The pause was the end of the utterance: let the prepared reply out."""
        task = self.spec["task"]
        while self.spec["text"] is None and not task.done():
            await asyncio.sleep(0.01)  # speech recognition has not finished yet
        if self.spec["task"] is not task:
            return  # discarded meanwhile
        request = self.spec["request"]
        if not self.spec["text"]:
            # nothing was prepared (the pause did not look like an ending): answer the normal way
            self.spec["task"] = None
            self.hold = False
            await self.on_utterance(audio, utterance_end, closed=closed)
            return
        if request.get("write_seen"):
            # R-RT-11: the prepared reply reached for a tool that changes something, which may only
            # run on words that are final. Throw the preparation away and answer the normal way.
            text = self.spec["text"]
            self.spec["text"] = None  # not a discarded guess as far as the page is concerned
            await self.discard_speculation()
            await self._answer(self._new_request(
                text, request["language"], request["stt_ms"], utterance_end, audio,
                stt_at=request["stt_at"], engine=request["engine"], heard=request.get("heard")), closed)
            return
        request["committed"] = True
        # the words are final now: write tools the reply reaches from here on must really run
        commit = getattr(self.engine, "commit_speculation", None)
        if commit is not None:
            try:
                commit()
            except Exception as e:
                logger.warning("commit_speculation failed: %r", e)
        request["audio"] = audio
        self._record_heard(request, closed)
        self._hand_user_audio(request)
        while self.spec["outbox"]:
            await self.send_json(self.spec["outbox"].pop(0))
        self.spec["committed"] = True
        self.hold = False
        self.chat_task = task
        self.spec["task"] = None
        self.wake.set()

    async def _answer(self, request, closed):
        self._record_heard(request, closed)
        await self.send_json({"type": "transcript", "id": request["id"], "text": request["text"],
                              "stt_ms": request["stt_ms"], "language": request["language"],
                              "engine": request["engine"]})
        await self.cancel_speech()
        self._start_reply(request)

    def last_agent_text(self):
        for message in reversed(getattr(self.engine, "history", None) or []):
            if message.get("role") == "assistant" and message.get("content"):
                return str(message["content"])
        return ""

    async def on_utterance(self, audio, utterance_end, over_avatar=False, closed=None, in_tail=False):
        """over_avatar: the utterance began while the agent was talking and has not interrupted it yet.
        in_tail: it began after the agent's last words, while only the playback allowance was left."""
        closed = closed or {"silence": Endpointer.END_SILENCE, "probability": None}
        self.listen["transcribing"] = True
        await self.update_listen_state()
        started, started_at = time.perf_counter(), time.time()
        engine_name = self.settings["stt"]
        heard_in = heard = None
        try:
            heard = await self._transcribe(audio)
            text, heard_in = heard
        except Exception as e:
            logger.warning("STT failed: %r", e)
            await self.send_json({"type": "notice", "message": f"음성 인식 실패: {e}"})
            text = ""
        finally:
            self.listen["transcribing"] = False
        # a lone "네" or "예" is dropped as too short to trust, unless the agent is waiting for exactly that
        if text and HALLUCINATION.match(bare(text)):
            # "감사합니다." heard in silence or faint noise: Whisper's subtitle habit, not the customer
            try:
                speech_s = await asyncio.to_thread(speech_seconds, audio, self.sample_rate)
            except Exception:
                speech_s = None
            if is_hallucination(text, speech_s, getattr(heard, "avg_logprob", None)):
                logger.info("Ignored a made-up transcript %r (speech %s s)", text, speech_s)
                await self.send_json({"type": "metric", "name": "hallucination_ignored", "text": text,
                                      "speech_s": speech_s})
                await self.update_listen_state()
                return
        answering = self.expecting_answer() and (self.only_tail() or not self.avatar_busy())
        one_word_answer = answering and (is_short_answer(text) or (len(text) == 1 and read_consent(text) != "unclear"))
        if in_tail and is_echo(text, self.last_agent_text()):
            # the microphone heard the agent's own last words
            await self.send_json({"type": "metric", "name": "echo_ignored", "text": text})
        elif over_avatar and is_backchannel(text) and not one_word_answer:
            # the listener was only signalling attention: keep talking, say nothing about it
            await self.send_json({"type": "metric", "name": "backchannel_ignored", "text": text})
        elif len(text) >= 2 or one_word_answer:
            await self._answer(self._new_request(
                text, heard_in, (time.perf_counter() - started) * 1000, utterance_end, audio,
                stt_at=started_at, engine=engine_name, heard=heard), closed)
        await self.update_listen_state()

    async def on_audio(self, data):
        """Microphone audio: 16 kHz 16-bit mono PCM, any packet size (the browser sends 0.1 s)."""
        if self.endpointer is None or self.listen["transcribing"] or self.closed:
            return
        loop = asyncio.get_running_loop()
        endpointer = self.endpointer
        # After the agent's last words it still counts as busy for a while (the browser may still be playing).
        # Wiping the microphone then threw away a quick "좋아" or "그래": listen in that tail, and drop only
        # what turns out to be the agent's own echo.
        tail = self.avatar_busy() and self.only_tail()
        if self.avatar_busy() and not self.settings["barge_in"] and not tail:
            # Half-duplex: without headphones the microphone hears the agent's own voice.
            endpointer.reset()
        else:
            gated = self.settings["interrupt_mode"] == "unmute_gate"
            endpointer.judge = self.judge if self.settings["endpoint"] == "smart" else None
            expecting = self.expecting_answer()
            endpointer.min_voiced = SHORT_ANSWER_VOICED if expecting else Endpointer.MIN_VOICED
            endpointer.max_close = Endpointer.END_SILENCE if expecting else None
            for kind, audio in endpointer.feed(np.frombuffer(data[:len(data) // 2 * 2], dtype="<i2")):
                if kind == "start":
                    # a start that never became an utterance (too short) must not leave its mark on the next one
                    self.listen["over_avatar"] = False
                    self.listen["in_tail"] = tail
                if kind == "start" and self.avatar_busy():
                    if tail and not self.settings["barge_in"]:
                        self.listen["over_avatar"] = False   # half-duplex, the agent is done: the customer's turn
                    elif gated and expecting and self.only_tail():
                        # the question has been asked in full; what starts now is the answer to it,
                        # even if it is only "네" and the last syllable is still playing
                        self.listen["over_avatar"] = False
                    elif gated:
                        self.listen["over_avatar"] = True  # wait and see whether this is a real interruption
                    else:
                        await self.cancel_speech()  # the user started talking over the agent
                elif kind == "pause":
                    if self.settings["speculative"] and not self.listen["over_avatar"] and not self.avatar_busy():
                        await self.begin_speculation(audio)
                elif kind == "resume":
                    await self.discard_speculation()
                elif kind == "utterance":
                    closed = dict(endpointer.last)
                    utterance_end = loop.time() - closed["silence"]
                    if closed["probability"] is not None:
                        logger.info("Turn closed after %.2f s of silence (finished-probability %.2f)",
                                    closed["silence"], closed["probability"])
                        await self.send_json({"type": "metric", "name": "turn_end", **closed})
                    if self.spec["task"] is not None:
                        self._spawn(self.commit_speculation(audio, utterance_end, closed))
                    else:
                        self._spawn(self.on_utterance(audio, utterance_end, self.listen["over_avatar"], closed,
                                                      self.listen.get("in_tail", False)))
                    self.listen["over_avatar"] = False
                    self.listen["in_tail"] = False
            voiced = endpointer.voiced * Endpointer.WINDOW / self.sample_rate
            if self.listen["over_avatar"] and endpointer.in_speech and voiced >= BARGE_IN_SECONDS:
                # too long for a backchannel: the user is taking the turn
                self.listen["over_avatar"] = False
                if self.avatar_busy():
                    await self.cancel_speech()
        await self.update_listen_state()

    def notify(self, text):
        """Called from outside the session (the server): the agent has something to tell the customer.
        It is said at the next moment when neither side is talking."""
        text = str(text or "").strip()
        if text and not self.closed:
            self.notices.append(text)

    def _quiet(self):
        task = self.chat_task
        hearing = self.endpointer is not None and self.endpointer.in_speech
        return not (self.avatar_busy() or hearing or self.listen["transcribing"] or self.spec["task"] is not None
                    or (task and not task.done()))

    async def _notice_pump(self):
        """Starts a reply for each waiting notice, one at a time, without talking over anyone."""
        while not self.closed:
            await asyncio.sleep(0.2)
            if self.notices and self._quiet():
                text = self.notices.popleft()
                self.listen["turn"] += 1
                self._start_reply({"id": f"notice-{self.listen['turn']}", "notice": True, "text": text,
                                   "t_user_end": time.time(), "utterance_end": None})

    async def on_message(self, request):
        """A JSON message from the client: config, played, chat, action, clear."""
        kind = request.get("type")
        if kind == "config":
            changed = apply_settings(self.settings, request)
            if {"stt", "endpoint"} & set(changed):
                self._spawn(self.prepare())
        elif kind == "played":
            try:
                offset = float(request.get("offset") or 0)
            except (TypeError, ValueError):
                offset = 0.0
            await self.on_played(request.get("seq"), offset)
        elif kind == "chat":
            # typed text on the voice channel: answered aloud, like a spoken turn
            text = str(request.get("text", "")).strip()
            if text:
                await self.cancel_speech()  # a new message interrupts whatever is being said
                self.listen["turn"] += 1
                self._start_reply({"id": request.get("id") or f"chat-{self.listen['turn']}", "text": text,
                                   "t_user_end": time.time(), "utterance_end": asyncio.get_running_loop().time()})
        elif kind == "action":
            # a touch on the screen: takes the turn the way an interruption does, then the engine
            # carries it out and its short reply is spoken
            name, arguments = str(request.get("name") or ""), request.get("arguments")
            self.listen["turn"] += 1
            request_id = request.get("id") or f"action-{self.listen['turn']}"
            known = {a.name for a in getattr(self.engine.definition, "actions", None) or []}
            if name not in known or not hasattr(self.engine, "act"):
                # nothing to carry out: the agent is not cut off for it
                await self.send_json({"type": "chat_done", "id": request_id, "text": "",
                                      "error": f"unknown action: {name}"})
                return
            await self.cancel_speech()
            self._start_reply({"id": request_id, "text": "",
                               "action": {"name": name, "arguments": arguments if isinstance(arguments, dict) else {}},
                               "t_user_end": time.time(), "utterance_end": asyncio.get_running_loop().time()})
        elif kind == "clear":
            await self.cancel_speech()

    # ---- the paced output loop ------------------------------------------------------------------

    async def _wait_kick(self, timeout):
        """Sleeps up to `timeout`; returns early (True) if the stream was flushed or closed."""
        try:
            await asyncio.wait_for(self.kick.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _send_chunk(self, pcm, start_sample):
        """Sends one chunk: `pcm` is the speech in it (may be empty on the avatar channel)."""
        loop = asyncio.get_running_loop()
        seq = self.seq
        self.chunk_speech[seq] = (self.stream_base, len(pcm))
        self.chunk_speech.pop(seq - 200, None)
        self.stream_base += len(pcm)
        if pcm:
            seconds = len(pcm) / 2 / self.sample_rate
            # the browser plays this chunk a moment later; allow for that and for room echo
            self.listen["busy_until"] = loop.time() + max(seconds, self.duration if self.avatar else 0) + 1.0
            self.play_end = max(self.play_end, loop.time() + CLIENT_PLAYBACK_DELAY) + seconds
            self._agent_audio["pcm"].extend(pcm)
        for mark in list(self.marks):
            if mark["pos"] < len(pcm):
                self.marks.remove(mark)
                since = None if mark["utterance_end"] is None else (loop.time() - mark["utterance_end"]) * 1000
                if mark["phase"] != "filler" and since is not None:
                    self.first_audio_ms.append(since)
                try:
                    self.engine.mark_output("filler" if mark["phase"] == "filler" else "reply", time.time())
                except Exception as e:
                    logger.debug("mark_output failed: %r", e)
                await self.send_json({
                    "type": "speech",
                    "seq": seq,
                    "id": mark["id"],
                    "phase": mark["phase"],
                    "tool_ms": mark["tool_ms"],
                    "offset": mark["pos"] / 2 / self.sample_rate,
                    "synth_ms": mark["synth_ms"],
                    "llm_ms": mark["llm_ms"],
                    "stt_ms": mark["stt_ms"],
                    "speculative": mark["speculative"],
                    "since_utterance_ms": since,
                })
            else:
                mark["pos"] -= len(pcm)
        self.in_flight = True
        try:
            if self.avatar is None:
                await self._send_bytes_raw(pack_audio_chunk(seq, pcm))
            else:
                padded = pcm + b"\x00" * (self.slice_bytes - len(pcm))
                started = time.perf_counter()
                jpegs = await self.avatar.step(np.frombuffer(padded, dtype="<i2").astype(np.float32) / 32768.0)
                infer_ms = (time.perf_counter() - started) * 1000
                await self._send_bytes_raw(pack_video_chunk(seq, start_sample, padded, jpegs, infer_ms))
        finally:
            self.in_flight = False
        self.seq += 1

    def _reply_finished(self):
        reply = self.last_reply
        return (reply["done"] and reply["synthesised"] == reply["queued"] and not self.speech
                and not self.synth_busy and self.requests.empty())

    async def run(self):
        """Runs until close(): sends speech on a real-time clock.

        Avatar: a chunk every slice, silence (idle motion) unless speech is queued, as in v1.
        Voice only: chunks only while there is speech; the clock restarts when speech arrives."""
        loop = asyncio.get_running_loop()
        self._synth_task = asyncio.create_task(self._synthesizer())
        self._spawn(self._notice_pump())
        try:
            if self.avatar is not None:
                await self._run_video(loop)
            else:
                await self._run_audio(loop)
        finally:
            self._synth_task.cancel()

    async def _run_video(self, loop):
        clock_start = loop.time()
        index = 0       # chunks since the clock was last (re)started
        sent = 0        # chunks since the session started
        while not self.closed:
            wait = clock_start + index * self.duration - loop.time()
            if wait > 0 and not self.kick.is_set():
                await self._wait_kick(wait)
            if self.closed:
                break
            if self.kick.is_set():
                # the browser just threw away its buffer: send the next chunk now, not on the old beat
                self.kick.clear()
                clock_start, index = loop.time(), 0
            pcm = b"" if self.hold else bytes(self.speech[:self.slice_bytes])
            del self.speech[:len(pcm)]
            self.idle = not pcm and not self.speech
            await self._send_chunk(pcm, sent * self.slice_samples)
            index += 1
            sent += 1
            if self._reply_finished():
                self._flush_agent_audio()
            await self.update_listen_state()

    async def _run_audio(self, loop):
        next_time = loop.time()     # when the next chunk is due (the previous one has played out)
        sent_samples = 0
        while not self.closed:
            if self.kick.is_set():
                # the browser just threw away its buffer: whatever comes next goes out at once
                self.kick.clear()
                next_time = loop.time()
            if self.hold or not self.speech:
                self.wake.clear()
                if self._reply_finished():
                    self._flush_agent_audio()
                await self.update_listen_state()
                if not self.closed and (self.hold or not self.speech):
                    await self.wake.wait()
                continue
            wait = next_time - loop.time()
            if wait > 0 and await self._wait_kick(wait):
                continue
            if self.kick.is_set() or self.hold:
                continue
            pcm = bytes(self.speech[:self.slice_bytes])
            del self.speech[:len(pcm)]
            if not pcm:
                continue
            send_time = loop.time()
            await self._send_chunk(pcm, sent_samples)
            sent_samples += len(pcm) // 2
            # The next chunk is due when this one has played, however long it was. Up to 0.1 s of
            # lateness is caught up; after a silence the clock simply restarts.
            next_time = max(next_time, send_time - 0.1) + len(pcm) / 2 / self.sample_rate
            await self.update_listen_state()

    async def close(self):
        """Stops everything. Safe to call more than once."""
        if self.closed:
            return
        self.closed = True
        self.kick.set()
        self.wake.set()
        tasks = [t for t in (self.chat_task, self.spec["task"], self._synth_task, *self._background) if t]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=3.0)
        self._flush_agent_audio()


async def serve_voice(ws, engine, channel="voice", settings=None, *, parts=None, avatar_holder=None, on_session=None):
    """Serves one voice or avatar conversation on an accepted WebSocket until it disconnects.

    ws:       accepted FastAPI/Starlette WebSocket
    engine:   contracts.ReplyEngine (the caller closes it afterwards)
    channel:  "voice" (audio only, FHAU) or "avatar" (audio + video, FHRT)
    settings: optional overrides of engine.definition.voice (dict or contracts.VoiceSettings)

    Sends "ready" first, or "error" and returns if the avatar is taken or cannot be loaded.
    Returns when the client disconnects; does not close the socket or the engine.
    """
    send_lock = asyncio.Lock()  # several tasks write to this socket

    async def send_json(payload):
        async with send_lock:
            await ws.send_text(json.dumps(payload, ensure_ascii=False, default=str))

    async def send_bytes(payload):
        async with send_lock:
            await ws.send_bytes(payload)

    definition = engine.definition
    avatar = None
    if channel == "avatar":
        from . import avatar as avatar_module
        holder = avatar_holder or avatar_module.holder()
        image = getattr(getattr(definition, "avatar", None), "image", None)
        try:
            avatar = await holder.acquire(image)
        except (avatar_module.AvatarBusy, avatar_module.AvatarUnavailable) as e:
            try:
                await send_json({"type": "error", "message": str(e)})
            except Exception:
                pass
            return
    session = VoiceSession(engine, send_json, send_bytes, voice_settings(definition, settings), avatar, parts=parts)
    if on_session is not None:
        on_session(session)     # the server keeps session.notify, for news that arrives from outside
    run_task = None
    try:
        await session.prepare()
        ready = {
            "type": "ready",
            "session_id": engine.session_id,
            "agent": getattr(definition, "id", None),
            "version": getattr(definition, "version", None),
            "channel": channel,
            "sample_rate": session.sample_rate,
            "chunk_seconds": session.duration,
            "settings": {k: v for k, v in session.settings.items() if k != "speed"},
            **client_features(definition),
        }
        if avatar is not None:
            ready.update(fps=avatar.fps, width=avatar.width, height=avatar.height,
                         slice_frames=avatar.slice_frames, slice_samples=avatar.slice_samples)
        await send_json(ready)
        run_task = asyncio.create_task(session.run())
        session.start_opening()
        while True:
            receive = asyncio.ensure_future(ws.receive())
            done, _ = await asyncio.wait({receive, run_task}, return_when=asyncio.FIRST_COMPLETED)
            if receive not in done:
                receive.cancel()
                run_task.result()  # the output loop ended by itself: raise what stopped it
                break
            message = receive.result()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes"):
                await session.on_audio(message["bytes"])
            elif message.get("text"):
                try:
                    request = json.loads(message["text"])
                except ValueError:
                    continue
                if isinstance(request, dict):
                    await session.on_message(request)
    except Exception as e:
        # a socket that went away mid-send ends the session the same way a disconnect does
        logger.info("Voice session ended: %s: %s", type(e).__name__, e)
    finally:
        await session.close()
        if run_task is not None:
            run_task.cancel()
            await asyncio.wait({run_task}, timeout=3.0)
        if avatar is not None:
            avatar.release()
