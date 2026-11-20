"""ASR quality in the voice channel: what the stt span records about a recognition, and that the
audio stored for a turn is the audio of the utterance whose transcript is that turn's user text.

The fast tests use fake speech parts; the pairing test runs the real engine (scripted LLM) into the
real trace store in a temporary directory. The `gpu` tests run the real Whisper on speech
synthesised by Supertonic."""
import asyncio
import json
import urllib.request
import wave

import numpy as np
import pytest

from agentkit import config, trace
from agentkit.eval import asr
from agentkit.runtime import audio as audio_parts
from agentkit.runtime.engine import Engine
from agentkit.runtime.voice import VoiceSession, voice_settings
from runtime_fakes import ScriptedLLM, make_definition, text
from voice_fakes import Client, FakeEngine, FakeParts, FakeSTT, silence_packet, simple_script, until


class Rig:
    def __init__(self, engine, parts, **overrides):
        self.engine, self.parts, self.client = engine, parts, Client()
        self.session = VoiceSession(engine, self.client.send_json, self.client.send_bytes,
                                    voice_settings(engine.definition, overrides), parts=parts, chunk_seconds=0.5)

    async def __aenter__(self):
        await self.session.prepare()
        self.task = asyncio.create_task(self.session.run())
        return self

    async def __aexit__(self, *exc):
        await self.session.close()
        self.task.cancel()
        await asyncio.wait({self.task}, timeout=2)

    async def mic(self, *events, in_speech=None):
        self.session.endpointer.script.append(list(events))
        if in_speech is not None:
            self.session.endpointer.in_speech = in_speech
        await self.session.on_audio(silence_packet())

    async def quiet(self):
        """The agent has finished speaking: until then the microphone is ignored (half-duplex)."""
        await until(lambda: not self.session.avatar_busy(), timeout=10)

    async def utter(self, audio, done=1):
        await self.quiet()
        await self.mic(("start", None), in_speech=True)
        await self.mic(("utterance", audio), in_speech=False)
        await until(lambda: len(self.client.of("chat_done")) >= done and self.session._reply_finished())


def tone(seconds, level):
    """An utterance that can be told from the others: its length and its (constant) sample value."""
    return np.full(int(16000 * seconds), level, dtype=np.float32)


class ConfidentSTT(FakeSTT):
    """Returns what the real Whisper recogniser returns: a Transcript with confidence values."""

    def __init__(self, results):
        super().__init__()
        self.results = list(results)

    def transcribe(self, audio, language=None):
        self.heard.append(audio)
        return self.results.pop(0)


def stt_spans(recorder):
    return [s for s in recorder.spans if s.kind == "stt"]


def test_stt_span_carries_confidence_length_and_level():
    results = [audio_parts.Transcript("주문 조회해 주세요.", "ko", -0.21, 0.03, 1.25),
               audio_parts.Transcript("으으윽 슬머세요.", "ko", -1.31, 0.2, 1.0)]

    async def main():
        engine = FakeEngine(simple_script("네."))
        async with Rig(engine, FakeParts(stt=ConfidentSTT(results))) as rig:
            await rig.utter(tone(1.5, 0.1), done=1)
            await rig.utter(tone(0.8, 0.01), done=2)
        return engine

    engine = asyncio.run(main())
    good, bad = [s.attrs for s in stt_spans(engine.recorder)]
    assert good == {"engine": "whisper", "language": "ko", "avg_logprob": -0.21, "no_speech_prob": 0.03,
                    "compression_ratio": 1.25, "audio_s": 1.5, "rms_db": -20.0, "low_confidence": False}
    assert bad["avg_logprob"] == -1.31 and bad["low_confidence"] is True
    assert bad["audio_s"] == 0.8 and bad["rms_db"] == -40.0


def test_a_recogniser_without_confidence_gives_nulls_and_judges_by_length_only():
    async def main():
        engine = FakeEngine(simple_script("네."))
        parts = FakeParts(stt=FakeSTT(["환불해 주세요.", "네 맞아요."]))        # plain (text, language), like before
        async with Rig(engine, parts, stt="qwen") as rig:
            await rig.utter(tone(1.0, 0.1), done=1)
            await rig.utter(tone(0.3, 0.1), done=2)
        return engine

    engine = asyncio.run(main())
    first, second = [s.attrs for s in stt_spans(engine.recorder)]
    assert first["engine"] == "qwen" and first["avg_logprob"] is None and first["no_speech_prob"] is None
    assert first["compression_ratio"] is None and first["audio_s"] == 1.0 and first["low_confidence"] is False
    assert second["audio_s"] == 0.3 and second["low_confidence"] is True


def test_voice_chat_done_names_the_turn():
    async def main():
        engine = FakeEngine(simple_script("네."))
        async with Rig(engine, FakeParts(stt=FakeSTT(["환불해 주세요.", "네 맞아요."]))) as rig:
            await rig.utter(tone(1.0, 0.1), done=1)
            await rig.utter(tone(1.0, 0.1), done=2)
            return [m["turn"] for m in rig.client.of("chat_done")]

    assert asyncio.run(main()) == [1, 2]


def test_committed_speculative_turn_records_span_and_audio_under_the_engines_turn():
    """The prepared reply is already running when the utterance ends, so the engine has counted the
    turn: the stt span must not be filed under the turn after it."""
    async def main():
        engine = FakeEngine(simple_script("내일은 맑아요.", wait=0.3))
        parts = FakeParts(stt=FakeSTT(["먼저 인사부터요.", "내일 날씨 알려줘요.", "내일 날씨 알려줘요."]))
        async with Rig(engine, parts, speculative=True) as rig:
            await rig.utter(tone(1.0, 0.1), done=1)                      # turn 1, the ordinary way
            await rig.quiet()
            await rig.mic(("start", None), in_speech=True)
            await rig.mic(("pause", tone(0.6, 0.2)), in_speech=True)
            await until(lambda: engine.turn == 2)                        # counted; its first event is 0.3 s away
            await rig.mic(("utterance", tone(0.9, 0.2)), in_speech=False)
            await until(lambda: len(rig.client.of("chat_done")) == 2 and rig.session._reply_finished())
        return engine

    engine = asyncio.run(main())
    assert [c["turn"] for c in engine.calls] == [1, 2]
    assert [(s.turn, s.output["text"], s.attrs["audio_s"]) for s in stt_spans(engine.recorder)] == [
        (1, "먼저 인사부터요.", 1.0), (2, "내일 날씨 알려줘요.", 0.9)]
    assert [(s.turn) for s in engine.recorder.spans if s.kind == "endpoint"] == [1, 2]
    user = {turn: len(pcm) // 2 for _, turn, role, pcm in engine.recorder.audios if role == "user"}
    assert user == {1: 16000, 2: 14400}                                  # the whole utterance, not the pause


def wav_samples(path):
    with wave.open(str(path), "rb") as r:
        assert r.getframerate() == 16000 and r.getnchannels() == 1
        return np.frombuffer(r.readframes(r.getnframes()), dtype="<i2")


def test_stored_audio_and_transcript_of_each_turn_belong_together(tmp_path):
    """Real engine, real recorder and store. Spoken, typed, ignored and speculative turns are mixed so
    that a turn counter kept anywhere but in the engine would drift."""
    trace.reset(tmp_path / "data")
    said = {1: ("주문 조회해 주세요.", tone(1.0, 0.10)),
            3: ("가져서 배달됐어요.", tone(1.4, 0.20)),
            4: ("뺏어져서 배달됐다고요.", tone(0.7, 0.30))}

    async def main():
        engine = Engine(make_definition(tools=[]), None, trace.get_recorder(),
                        ScriptedLLM(responder=lambda messages, tools: [text("네, 알겠습니다.")], delay=0.05),
                        channel="voice", test=True, session_id="pair")
        stt = FakeSTT([said[1][0], "", said[3][0], said[4][0], said[4][0]])
        async with Rig(engine, FakeParts(stt=stt), speculative=True) as rig:
            await rig.utter(said[1][1], done=1)                                     # turn 1: spoken
            await rig.session.on_message({"type": "chat", "id": "c1", "text": "이건 글로 보냅니다"})
            await until(lambda: len(rig.client.of("chat_done")) == 2 and rig.session._reply_finished())   # turn 2: typed
            await rig.quiet()
            await rig.mic(("start", None), in_speech=True)                          # noise: recognised as nothing
            await rig.mic(("utterance", tone(0.5, 0.9)), in_speech=False)
            await rig.utter(said[3][1], done=3)                                     # turn 3: spoken
            await rig.quiet()
            await rig.mic(("start", None), in_speech=True)                          # turn 4: prepared at a pause
            await rig.mic(("pause", said[4][1][:8000]), in_speech=True)
            await until(lambda: engine.turn == 4)
            await rig.mic(("utterance", said[4][1]), in_speech=False)
            await until(lambda: len(rig.client.of("chat_done")) == 4 and rig.session._reply_finished())
        await engine.close()

    try:
        asyncio.run(main())
        assert trace.get_recorder().flush(5.0)
        store = trace.get_store()
        session = store.get_session("pair")
        assert [t["turn"] for t in session["turns"]] == [1, 2, 3, 4]
        assert [(a["turn"], a["role"]) for a in session["audio"] if a["role"] == "user"] == [
            (1, "user"), (3, "user"), (4, "user")]                                  # the typed turn has no audio
        spans = {s["turn"]: s for s in session["spans"] if s["kind"] == "stt"}
        assert sorted(spans) == [1, 3, 4]
        for number, (words, audio) in said.items():
            record = next(t for t in session["turns"] if t["turn"] == number)
            stored = wav_samples(store.audio_path("pair", number, "user"))
            assert record["user_text"] == words                                     # the turn's transcript
            assert spans[number]["output"] == {"text": words}                       # what the recogniser returned
            assert len(stored) == len(audio)                                        # this utterance's audio...
            assert set(stored.tolist()) == {int(audio[0] * 32767)}                  # ...sample for sample
            assert spans[number]["attrs"]["audio_s"] == round(len(audio) / 16000, 3)
        assert session["turns"][1]["user_text"] == "이건 글로 보냅니다"
        # and the flags computed at the end of the session sit on the right turn
        flags = {d["turn"]: d["auto"] for d in store.list_documents("turn_flag", "pair/")}
        assert flags == {4: ["repeat"]}
    finally:
        trace.reset(None)


# ---- the real recogniser ----------------------------------------------------------------------------

SENTENCES = ["주문한 상품이 떨어진 상태로 배달됐어요.", "환불 받으려면 어떻게 해야 하나요?"]


def synthesize(sentence):
    """16 kHz float32 speech from Supertonic (44.1 kHz WAV, resampled); skips when it is not running."""
    import io

    import soundfile as sf
    from scipy.signal import resample_poly
    body = json.dumps({"text": sentence, "voice": "M1", "lang": "ko", "response_format": "wav"}).encode()
    request = urllib.request.Request(config.TTS_URL.rstrip("/") + "/v1/tts", data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            wav, rate = sf.read(io.BytesIO(response.read()), dtype="float32", always_2d=True)
    except OSError as e:
        pytest.skip(f"Supertonic not reachable at {config.TTS_URL}: {e}")
    assert rate == 44100
    return resample_poly(wav.mean(axis=1), 160, 441).astype(np.float32)


@pytest.mark.gpu
@pytest.mark.network
def test_real_whisper_fills_the_confidence_fields():
    parts = audio_parts.shared()
    whisper = parts.stt_pool.submit(parts.whisper).result()
    speech = synthesize(SENTENCES[0])
    result = parts.stt_pool.submit(whisper.transcribe, speech, "ko").result()
    words, language = result                                      # still unpacks the old way
    attrs = audio_parts.stt_quality(result, speech)
    print(f"\nreal Whisper on synthesised speech: {words!r} {attrs}")
    assert language == "ko" and asr.cer(SENTENCES[0], words) <= 0.2
    assert isinstance(result.avg_logprob, float) and -1.0 < result.avg_logprob < 0.0
    assert isinstance(result.no_speech_prob, float) and 0.0 <= result.no_speech_prob < 0.5
    assert isinstance(result.compression_ratio, float) and 0.3 < result.compression_ratio < 2.4
    assert attrs["audio_s"] > 1.0 and -60 < attrs["rms_db"] < 0 and attrs["low_confidence"] is False

    # 0.3 s of faint noise: nothing a recognition should be trusted on
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(4800) * 0.003).astype(np.float32)
    quiet = parts.stt_pool.submit(whisper.transcribe, noise, "ko").result()
    noise_attrs = audio_parts.stt_quality(quiet, noise)
    print(f"real Whisper on 0.3 s of noise: {quiet[0]!r} {noise_attrs}")
    assert noise_attrs["low_confidence"] is True


@pytest.mark.gpu
@pytest.mark.network
def test_real_bench_over_two_hand_made_cases(tmp_path):
    from agentkit.contracts import SessionMeta, Span, TurnRecord
    import time
    speech = [synthesize(s) for s in SENTENCES]
    trace.reset(tmp_path / "data")
    try:
        rec, store = trace.get_recorder(), trace.get_store()
        t0 = time.time() - 30
        rec.start_session(SessionMeta(session_id="bench", agent_id="shop-support", version=1, channel="voice",
                                      test=True, started_at=t0))
        wrong = ["가져서 배달됐어요.", "한 불 받으려면요"]            # what a recogniser "heard" at the time
        for n, audio in enumerate(speech, 1):
            rec.span(Span(session_id="bench", turn=n, kind="stt", t_start=t0 + n, t_end=t0 + n + 0.2,
                          output={"text": wrong[n - 1]}, attrs={"engine": "whisper", "language": "ko"}))
            rec.turn(TurnRecord(session_id="bench", turn=n, user_text=wrong[n - 1], agent_text="네.",
                                t_user_end=t0 + n, t_agent_start=t0 + n + 1, t_agent_end=t0 + n + 2))
            rec.audio("bench", n, "user", audio_parts.float_to_pcm16(audio))
        rec.end_session("bench", "completed")
        assert rec.flush(5.0)
        for n, sentence in enumerate(SENTENCES, 1):
            _, case = asr.flag_turn(store, "bench", n, True, sentence)
            assert case["audio_s"] == round(len(speech[n - 1]) / 16000, 3) and case["heard"] == wrong[n - 1]

        doc = asr.run_bench(store, "asr-real", ["whisper", "qwen"])
        print("\nreal bench:", json.dumps({"engines": doc["engines"], "items": [
            {"expected": i["expected"], "results": i["results"]} for i in doc["items"]]}, ensure_ascii=False))
        assert doc["status"] == "finished" and doc["total"] == 2 and len(doc["items"]) == 2
        whisper = doc["engines"]["whisper"]
        assert whisper["available"] is True and whisper["n"] == 2 and whisper["cer"] <= 0.2
        assert whisper["ms_median"] > 0
        for item in doc["items"]:
            assert item["results"]["whisper"]["text"] and item["results"]["whisper"]["cer"] <= 0.3
        qwen = doc["engines"]["qwen"]
        if qwen["available"]:                                     # the Qwen3-ASR server is running
            assert qwen["n"] == 2 and qwen["cer"] <= 0.3
        else:
            assert qwen["n"] == 0 and all("qwen" not in item["results"] for item in doc["items"])
        assert store.get_document("asr_bench", "asr-real")["status"] == "finished"
    finally:
        trace.reset(None)
