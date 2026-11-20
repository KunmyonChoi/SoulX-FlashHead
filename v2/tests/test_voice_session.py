"""Fast tests of the voice channel: scripted engine, fake TTS / STT / endpointer, no model, no network."""
import asyncio
import json
import struct
import time

import numpy as np
import pytest

from agentkit.contracts import VoiceSettings
from agentkit.runtime import audio as audio_parts
from agentkit.runtime import avatar as avatar_module
from agentkit.runtime.voice import (VoiceSession, apply_settings, pack_audio_chunk, pack_video_chunk, serve_voice,
                                    voice_settings)
from voice_fakes import (BYTES_PER_CHAR, Client, FakeEngine, FakeParts, FakeSTT, FakeTTS, ev, fake_pcm,
                         parse_video_chunk, silence_packet, simple_script, until)

CHUNK = 0.5                       # seconds per chunk in these tests
CHUNK_BYTES = int(CHUNK * 16000) * 2
GAP_BYTES = int(0.3 * 16000) * 2  # the pause the session puts after each sentence


class Rig:
    """A VoiceSession wired to fakes, with its output loop running."""

    def __init__(self, script, parts=None, session_id="s1", chunk_seconds=CHUNK, **overrides):
        self.engine = FakeEngine(script, session_id=session_id)
        self.client = Client()
        self.parts = parts or FakeParts()
        self.session = VoiceSession(self.engine, self.client.send_json, self.client.send_bytes,
                                    voice_settings(self.engine.definition, overrides), parts=self.parts,
                                    chunk_seconds=chunk_seconds)

    async def __aenter__(self):
        await self.session.prepare()
        self.endpointer = self.session.endpointer
        self.task = asyncio.create_task(self.session.run())
        return self

    async def __aexit__(self, *exc):
        await self.session.close()
        self.task.cancel()
        await asyncio.wait({self.task}, timeout=2)

    async def mic(self, *events, in_speech=None, voiced=None):
        """One microphone packet for which the (fake) endpointer reports these events."""
        self.endpointer.script.append(list(events))
        if in_speech is not None:
            self.endpointer.in_speech = in_speech
        if voiced is not None:
            self.endpointer.voiced = voiced
        await self.session.on_audio(silence_packet())

    async def say(self, text="안녕하세요"):
        await self.session.on_message({"type": "chat", "id": "c1", "text": text})


AUDIO = np.zeros(16000, dtype=np.float32)   # stands for an utterance; the fake STT ignores its content


# ---- piece -> audio ---------------------------------------------------------------------------------

def test_piece_becomes_paced_fhau_chunks():
    sentence = "안녕하세요. 반갑습니다."          # 13 characters -> 1.3 s of fake speech

    async def main():
        async with Rig(simple_script(sentence)) as rig:
            expected = fake_pcm(sentence) + b"\x00" * GAP_BYTES
            await rig.say()
            await until(lambda: len(rig.client.pcm()) >= len(expected))
            await asyncio.sleep(0.8)          # silence: nothing more may arrive
            return rig

    rig = asyncio.run(main())
    client, engine = rig.client, rig.engine
    expected = fake_pcm(sentence) + b"\x00" * GAP_BYTES
    chunks = client.chunks()
    # framing: "FHAU" + <II>(seq, bytes) + PCM, seq counting from 0, at most one slice each
    assert [seq for seq, _ in chunks] == list(range(len(chunks)))
    assert all(0 < len(pcm) <= CHUNK_BYTES for _, pcm in chunks)
    assert all(len(pcm) == CHUNK_BYTES for _, pcm in chunks[:-1])
    assert client.pcm() == expected          # all of the speech, nothing else (no silent chunks)
    raw = client.binary[0][1]
    assert raw[:4] == b"FHAU" and struct.unpack("<II", raw[4:12]) == (0, CHUNK_BYTES)
    # pacing: one chunk per 0.5 s
    times = [t for t, _ in client.binary]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(0.35 < gap < 0.7 for gap in gaps), gaps
    # the events of the engine reach the client under v1's message names
    assert client.of("llm", state="start")
    assert "".join(m["text"] for m in client.of("chat_delta", id="c1")).strip() == sentence
    assert client.of("chat_done", id="c1")[0]["text"] == sentence
    assert [m["state"] for m in client.of("tts")] == ["start", "done"]
    speech = client.of("speech")
    assert len(speech) == 1 and speech[0]["phase"] == "reply" and speech[0]["seq"] == 0 and speech[0]["id"] == "c1"
    # the engine was told when the first sound left, once
    assert [phase for phase, _ in engine.outputs] == ["reply"]
    assert abs(engine.outputs[0][1] - time.time()) < 10
    assert engine.calls[0]["text"] == "안녕하세요" and engine.calls[0]["speculative"] is False
    # the agent's audio of the turn went to the recorder (which keeps it only for test sessions)
    assert [(sid, turn, role) for sid, turn, role, _ in engine.recorder.audios] == [("s1", 1, "agent")]
    assert engine.recorder.audios[0][3] == expected
    assert [s.kind for s in engine.recorder.spans] == ["tts"] and engine.recorder.spans[0].turn == 1


def test_filler_is_spoken_first_and_marked():
    def script(text):
        return [ev("tool", state="start", name="get_order", arguments={"order_id": "A1001"}),
                ev("filler", "주문을 확인해 볼게요."), 0.3,
                ev("tool", state="done", name="get_order", ok=True, effect="read"),
                ev("filler", "잠시만요."),            # a second filler after sound was queued is not spoken
                ev("delta", "배송 중입니다."), ev("piece", "배송 중입니다."), ev("done", "배송 중입니다.")]

    async def main():
        async with Rig(script) as rig:
            await rig.say("주문 어디쯤이에요")
            await until(lambda: len(rig.client.of("speech")) == 2)
            await until(lambda: rig.session._reply_finished())
            return rig

    rig = asyncio.run(main())
    phases = [m["phase"] for m in rig.client.of("speech")]
    assert phases == ["filler", "answer"]            # "answer": the reply that followed a lookup, as in v1
    assert rig.client.of("speech")[1]["tool_ms"] >= 250
    assert [phase for phase, _ in rig.engine.outputs] == ["filler", "reply"]
    assert [text for text, _, _ in rig.parts.tts.calls] == ["주문을 확인해 볼게요.", "배송 중입니다."]
    assert [m["state"] for m in rig.client.of("tool")] == ["start", "done"]
    assert rig.client.of("tool")[0]["arguments"] == {"order_id": "A1001"}
    assert rig.client.pcm() == (fake_pcm("주문을 확인해 볼게요.") + b"\x00" * GAP_BYTES
                                + fake_pcm("배송 중입니다.") + b"\x00" * GAP_BYTES)
    # the filler's audio is cached per voice for the next time
    assert ("F1", "ko", "주문을 확인해 볼게요.") in rig.parts.filler_cache


def test_handoff_and_knowledge_events_are_forwarded():
    def script(text):
        return [ev("knowledge", state="start", query="q"), ev("handoff", rule_id="large_refund", reason="고액"),
                ev("replace", "상담원에게 연결할게요."), ev("piece", "상담원에게 연결할게요."),
                ev("done", "상담원에게 연결할게요.")]

    async def main():
        async with Rig(script) as rig:
            await rig.say()
            await until(lambda: rig.client.of("chat_done"))
            return rig

    rig = asyncio.run(main())
    assert rig.client.of("knowledge")[0]["query"] == "q"
    assert rig.client.of("handoff")[0] == {"type": "handoff", "id": "c1", "rule_id": "large_refund", "reason": "고액"}
    assert rig.client.of("chat_replace")[0]["text"] == "상담원에게 연결할게요."


# ---- interruption -----------------------------------------------------------------------------------

FIRST = "하나 둘 셋 넷 다섯."            # 12 characters: stream bytes 0 .. 38400, then a 9600 byte gap
SECOND = "ab cd ef gh ij kl mn"         # 20 characters: stream bytes 48000 .. 112000
assert len(FIRST) * BYTES_PER_CHAR + GAP_BYTES == 3 * CHUNK_BYTES


@pytest.mark.parametrize("chunks_before, played, expected", [
    # chunk 3 starts at byte 48000 = the first byte of SECOND; 0.45 s in is 22.5 % of it: "ab c" -> "ab"
    (4, {"seq": 3, "offset": 0.45}, FIRST + " ab"),
    # chunk 1 starts at byte 16000; 0.2 s in is byte 22400 of FIRST's 38400: 7 characters -> "하나 둘 셋"
    (2, {"seq": 1, "offset": 0.2}, "하나 둘 셋"),
    # all of FIRST and the gap, nothing of SECOND
    (4, {"seq": 2, "offset": 0.5}, FIRST),
    # the browser had not started any chunk
    (1, {"seq": None, "offset": 0}, ""),
])
def test_interruption_cuts_the_reply_to_what_was_heard(chunks_before, played, expected):
    async def main():
        async with Rig(simple_script(FIRST, SECOND, tail=30), interrupt_mode="unmute", barge_in=True) as rig:
            await rig.say()
            await until(lambda: len(rig.client.binary) >= chunks_before)
            sent = len(rig.client.binary)
            await rig.mic(("start", None), in_speech=True)      # the user starts talking over the agent
            interrupts = rig.client.of("interrupt")
            assert len(interrupts) == 1 and interrupts[0]["seq"] in (sent, sent + 1)
            assert rig.engine.truncated == []                   # not before the browser has reported
            await rig.session.on_message({"type": "played", **played})
            await asyncio.sleep(0.7)
            assert len(rig.client.binary) <= sent + 1           # the rest of the speech was dropped
            return rig

    rig = asyncio.run(main())
    assert rig.engine.truncated == [expected]
    assert rig.client.of("chat_truncate") == [{"type": "chat_truncate", "id": "c1", "text": expected}]
    assert rig.engine.calls[0]["cancelled"] is True              # the engine's iteration was cancelled
    assert not rig.client.of("chat_done")                        # a cancelled reply is not reported as done


def test_reply_heard_to_the_end_is_not_cut():
    async def main():
        async with Rig(simple_script("네."), interrupt_mode="unmute", barge_in=True) as rig:
            await rig.say()
            await until(lambda: rig.session._reply_finished())
            # 1) interrupted right after the last chunk left: the browser reports all of it as played
            await rig.mic(("start", None), in_speech=True)
            assert len(rig.client.of("interrupt")) == 1
            await rig.session.on_message({"type": "played", "seq": 0, "offset": 0.5})
            assert rig.engine.truncated == [] and not rig.client.of("chat_truncate")
            # 2) long after: nothing is playing, so the browser is not even asked
            rig.session.play_end = rig.session.listen["busy_until"] = 0.0
            await rig.session.cancel_speech()
            assert len(rig.client.of("interrupt")) == 1
            return rig

    asyncio.run(main())


def test_basic_mode_drops_unsent_speech_without_asking_the_browser():
    async def main():
        async with Rig(simple_script(FIRST, SECOND, tail=30), interrupt_mode="basic", barge_in=True) as rig:
            await rig.say()
            await until(lambda: len(rig.client.binary) >= 2)
            sent = len(rig.client.binary)
            await rig.mic(("start", None), in_speech=True)
            await asyncio.sleep(0.7)
            assert len(rig.client.binary) <= sent + 1
            await rig.session.on_message({"type": "played", "seq": 0, "offset": 0.1})
            return rig

    rig = asyncio.run(main())
    assert not rig.client.of("interrupt") and rig.engine.truncated == []
    assert rig.engine.calls[0]["cancelled"] is True


def test_half_duplex_ignores_the_microphone_while_speaking():
    async def main():
        async with Rig(simple_script(FIRST, SECOND), barge_in=False) as rig:
            await rig.say()
            await until(lambda: len(rig.client.binary) >= 1)
            resets = rig.endpointer.resets
            await rig.mic(("start", None), in_speech=True)       # would interrupt, if it were listened to
            assert rig.endpointer.resets == resets + 1 and len(rig.endpointer.script) == 1
            assert not rig.client.of("interrupt")
            return rig

    asyncio.run(main())


# ---- unmute_gate ------------------------------------------------------------------------------------

def test_unmute_gate_keeps_talking_through_a_backchannel():
    async def main():
        parts = FakeParts(stt=FakeSTT(["네 네.", "잠깐만요, 그게 아니라요."]))
        async with Rig(simple_script(FIRST, SECOND, SECOND), parts=parts, interrupt_mode="unmute_gate",
                       barge_in=True) as rig:
            await rig.say()
            await until(lambda: len(rig.client.binary) >= 1)
            # a short noise over the agent: no interruption when it starts...
            await rig.mic(("start", None), in_speech=True, voiced=5)
            assert rig.session.listen["over_avatar"] is True and not rig.client.of("interrupt")
            # ...and none when it turns out to be "네 네"
            await rig.mic(("utterance", AUDIO), in_speech=False, voiced=0)
            await until(lambda: rig.client.of("metric", name="backchannel_ignored"))
            sent = len(rig.client.binary)
            await until(lambda: len(rig.client.binary) > sent)   # still talking
            assert not rig.client.of("interrupt") and not rig.client.of("transcript")
            assert len(rig.engine.calls) == 1 and rig.engine.calls[0]["cancelled"] is False

            # the same, but what was said is a real objection: it takes the turn
            await rig.mic(("start", None), in_speech=True, voiced=5)
            await rig.mic(("utterance", AUDIO), in_speech=False, voiced=0)
            await until(lambda: len(rig.engine.calls) == 2)
            assert rig.client.of("transcript")[0]["text"] == "잠깐만요, 그게 아니라요."
            assert len(rig.client.of("interrupt")) == 1
            return rig

    rig = asyncio.run(main())
    assert rig.engine.calls[1]["text"] == "잠깐만요, 그게 아니라요."


def test_unmute_gate_interrupts_once_the_speech_is_too_long_for_a_backchannel():
    async def main():
        async with Rig(simple_script(FIRST, SECOND, tail=30), interrupt_mode="unmute_gate", barge_in=True) as rig:
            await rig.say()
            await until(lambda: len(rig.client.binary) >= 1)
            await rig.mic(("start", None), in_speech=True, voiced=5)
            await rig.mic(in_speech=True, voiced=20)             # 0.64 s voiced: still waiting
            assert not rig.client.of("interrupt")
            await rig.mic(in_speech=True, voiced=40)             # 1.28 s voiced: past BARGE_IN_SECONDS
            assert len(rig.client.of("interrupt")) == 1
            assert rig.session.listen["over_avatar"] is False
            await asyncio.sleep(0.05)
            return rig

    rig = asyncio.run(main())
    assert rig.engine.calls[0]["cancelled"] is True


def test_a_one_word_answer_to_a_question_is_an_answer_not_a_backchannel():
    """After "결제를 진행할까요?" the customer says "네" while the last chunk is still playing."""
    async def main():
        parts = FakeParts(stt=FakeSTT(["네.", "네", "예"]))
        async with Rig(simple_script("결제를 진행할까요?"), parts=parts, interrupt_mode="unmute_gate", barge_in=True) as rig:
            await rig.say("결제할게요")
            await until(lambda: rig.engine.calls and rig.engine.calls[0]["finished"] and rig.session.only_tail())
            rig.engine.history = [{"role": "assistant", "content": "결제를 진행할까요?"}]
            assert rig.session.avatar_busy() and rig.session.expecting_answer()   # only the playback allowance is left
            await rig.mic(("start", None), in_speech=True, voiced=4)
            assert rig.session.listen["over_avatar"] is False
            # while an answer is awaited the endpointer accepts shorter speech and does not wait 2 s for more
            assert rig.endpointer.min_voiced < audio_parts.Endpointer.MIN_VOICED
            assert rig.endpointer.max_close == audio_parts.Endpointer.END_SILENCE
            await rig.mic(("utterance", AUDIO), in_speech=False, voiced=0)
            await until(lambda: len(rig.engine.calls) == 2)
            assert rig.engine.calls[1]["text"] == "네." and not rig.client.of("metric", name="backchannel_ignored")

            # recognised as a single character, without the full stop: still the answer
            await until(lambda: rig.engine.calls[1]["finished"] and rig.session.only_tail())
            await rig.mic(("start", None), in_speech=True, voiced=4)
            await rig.mic(("utterance", AUDIO), in_speech=False, voiced=0)
            await until(lambda: len(rig.engine.calls) == 3)
            assert rig.engine.calls[2]["text"] == "네"

            # nothing was asked: a lone "예" stays too short to act on, and the thresholds are back
            rig.engine.history = [{"role": "assistant", "content": "주문이 접수되었습니다."}]
            await until(lambda: rig.engine.calls[2]["finished"] and not rig.session.avatar_busy(), timeout=8)
            await rig.mic(("start", None), in_speech=True, voiced=4)
            assert rig.endpointer.min_voiced == audio_parts.Endpointer.MIN_VOICED and rig.endpointer.max_close is None
            await rig.mic(("utterance", AUDIO), in_speech=False, voiced=0)
            await asyncio.sleep(0.2)
            assert len(rig.engine.calls) == 3
            return rig

    asyncio.run(main())


def test_a_backchannel_during_the_question_itself_is_still_ignored():
    """ "네" while the agent is still reading the confirmation out is not consent to it."""
    async def main():
        parts = FakeParts(stt=FakeSTT(["네."]))
        script = simple_script("딸기 라떼 한 잔, 합계 6000원입니다.", "결제를 진행할까요?", tail=30)
        async with Rig(script, parts=parts, interrupt_mode="unmute_gate", barge_in=True) as rig:
            rig.engine.history = [{"role": "assistant", "content": "더 필요한 것이 있으신가요?"}]
            await rig.say("결제할게요")
            await until(lambda: len(rig.client.binary) >= 1)
            assert not rig.session.only_tail()
            await rig.mic(("start", None), in_speech=True, voiced=4)
            assert rig.session.listen["over_avatar"] is True
            await rig.mic(("utterance", AUDIO), in_speech=False, voiced=0)
            await until(lambda: rig.client.of("metric", name="backchannel_ignored"))
            assert len(rig.engine.calls) == 1 and not rig.client.of("interrupt")

    asyncio.run(main())


def test_pending_consent_counts_as_expecting_an_answer():
    class Gateway:
        def __init__(self): self.waiting = [{"tool": "request_payment"}]
        def pending(self, session_id): return self.waiting

    async def main():
        async with Rig(simple_script("네.")) as rig:
            assert rig.session.expecting_answer() is False
            rig.engine.gateway = Gateway()
            assert rig.session.expecting_answer() is True
            rig.engine.gateway.waiting = []
            assert rig.session.expecting_answer() is False

    asyncio.run(main())


def test_a_notice_is_spoken_at_a_quiet_moment_and_not_over_a_reply():
    async def main():
        async with Rig(simple_script("내일은 회의가 두 건 있습니다.", tail=0.6)) as rig:
            calls = []

            async def notify(text):
                calls.append(text)
                for step in simple_script("맡기신 일정 등록이 끝났습니다.")(text):
                    yield step
            rig.engine.notify = notify
            await rig.say("내일 일정 알려 줘")
            rig.session.notify("웹 작업 결과: 일정을 등록했습니다.")
            await asyncio.sleep(0.3)
            assert calls == [] and rig.session.notices              # the reply is still going: the news waits
            await until(lambda: calls, timeout=10)
            assert calls == ["웹 작업 결과: 일정을 등록했습니다."] and not rig.session.notices
            await until(lambda: [m for m in rig.client.of("chat_done") if str(m.get("id", "")).startswith("notice-")])
            spoken = [call[0] for call in rig.parts.tts.calls]
            assert "맡기신 일정 등록이 끝났습니다." in spoken
            rig.session.notify("   ")                                 # nothing to say
            assert not rig.session.notices

    asyncio.run(main())


# ---- speculative replies ----------------------------------------------------------------------------

def test_speculative_reply_is_discarded_on_resume_and_committed_on_end():
    async def main():
        parts = FakeParts(stt=FakeSTT(["내일 날씨 알려줘요.", "내일 날씨 알려줘요. 그리고 모레도요."]))
        async with Rig(simple_script("내일은 맑아요.", wait=0.2), parts=parts, speculative=True) as rig:
            engine, client = rig.engine, rig.client
            # the speaker pauses: a reply is prepared in secret
            await rig.mic(("start", None), in_speech=True)
            await rig.mic(("pause", AUDIO), in_speech=True)
            await until(lambda: len(engine.calls) == 1)
            assert engine.calls[0]["speculative"] is True
            assert not client.of("transcript") and not client.of("llm") and not client.binary
            # ...and goes on: the iteration is cancelled and nothing of it remains
            await rig.mic(("resume", None), in_speech=True)
            assert engine.calls[0]["cancelled"] is True and engine.turn == 0
            assert client.of("metric", name="speculation_discarded")[0]["text"] == "내일 날씨 알려줘요."
            assert not client.of("transcript") and not client.of("chat_delta") and not client.binary

            # second pause, and this time the utterance ends there
            await rig.mic(("pause", AUDIO), in_speech=True)
            await until(lambda: len(engine.calls) == 2)
            await until(lambda: len(rig.session.speech) > 0)     # synthesised, but held back
            await asyncio.sleep(0.6)
            assert not client.binary and not client.of("chat_delta") and not client.of("tts")
            assert engine.outputs == []
            await rig.mic(("utterance", AUDIO), in_speech=False)
            await until(lambda: client.binary and client.of("chat_done"))
            await until(lambda: rig.session._reply_finished())
            return rig

    rig = asyncio.run(main())
    engine, client = rig.engine, rig.client
    assert len(engine.calls) == 2                                # the committed reply was not generated again
    assert engine.calls[1]["speculative"] is True and engine.calls[1]["cancelled"] is False
    assert client.of("transcript")[0]["text"] == "내일 날씨 알려줘요. 그리고 모레도요."
    assert client.of("speech")[0]["speculative"] is True
    assert client.pcm() == fake_pcm("내일은 맑아요.") + b"\x00" * GAP_BYTES
    assert [phase for phase, _ in engine.outputs] == ["reply"]
    # both sides of the committed turn went to the recorder, the discarded guess did not
    assert sorted((turn, role) for _, turn, role, _ in engine.recorder.audios) == [(1, "agent"), (1, "user")]
    assert [s.kind for s in engine.recorder.spans if s.kind in ("endpoint", "stt")] == ["endpoint", "stt"]


def test_speculation_is_skipped_mid_sentence():
    async def main():
        parts = FakeParts(stt=FakeSTT(["그러니까 내일", "그러니까 내일 날씨 알려줘요."]))
        async with Rig(simple_script("내일은 맑아요."), parts=parts, speculative=True) as rig:
            await rig.mic(("start", None), in_speech=True)
            await rig.mic(("pause", AUDIO), in_speech=True)
            await until(lambda: rig.session.spec["text"] == "")
            assert rig.engine.calls == []                        # "그러니까 내일" does not read as finished
            await rig.mic(("utterance", AUDIO), in_speech=False)
            await until(lambda: rig.client.of("chat_done"))
            return rig

    rig = asyncio.run(main())
    assert [(c["text"], c["speculative"]) for c in rig.engine.calls] == [("그러니까 내일 날씨 알려줘요.", False)]


def test_speculative_reply_that_reached_for_a_write_tool_is_redone_when_the_turn_is_final():
    def script(text):
        return [ev("tool", state="start", name="request_refund", arguments={}, effect="write"),
                ev("tool", state="done", name="request_refund", ok=False, effect="write", executed=False),
                ev("piece", "환불을 접수할까요?"), ev("done", "환불을 접수할까요?")]

    async def main():
        parts = FakeParts(stt=FakeSTT(["환불해 주세요."]))
        async with Rig(script, parts=parts, speculative=True) as rig:
            await rig.mic(("start", None), in_speech=True)
            await rig.mic(("pause", AUDIO), in_speech=True)
            await until(lambda: len(rig.engine.calls) == 1 and rig.engine.calls[0]["finished"])
            await rig.mic(("utterance", AUDIO), in_speech=False)
            await until(lambda: len(rig.engine.calls) == 2 and rig.client.of("chat_done"))
            return rig

    rig = asyncio.run(main())
    assert [c["speculative"] for c in rig.engine.calls] == [True, False]
    assert rig.engine.calls[1]["text"] == "환불해 주세요."
    assert len(rig.client.of("transcript")) == 1 and len(rig.client.of("tool")) == 2


# ---- normal spoken turn, recorder, settings ---------------------------------------------------------

def test_spoken_turn_end_to_end_with_fakes():
    async def main():
        parts = FakeParts(stt=FakeSTT(["주문 조회해 주세요."]))
        async with Rig(simple_script("주문번호를 알려 주세요."), parts=parts, language="auto") as rig:
            await rig.mic(("start", None), in_speech=True)
            assert rig.client.of("listen_state")[-1]["state"] == "hearing"
            await rig.mic(("utterance", AUDIO), in_speech=False)
            await until(lambda: rig.client.of("chat_done") and rig.session._reply_finished())
            return rig

    rig = asyncio.run(main())
    transcript = rig.client.of("transcript")[0]
    assert transcript["text"] == "주문 조회해 주세요." and transcript["engine"] == "whisper" and transcript["id"] == "voice-1"
    call = rig.engine.calls[0]
    assert call["language"] == "ko" and abs(call["t_user_end"] - time.time()) < 10
    states = [m["state"] for m in rig.client.of("listen_state")]
    assert states[0] == "hearing" and states.index("transcribing") < states.index("speaking")
    user = [a for a in rig.engine.recorder.audios if a[2] == "user"]
    assert len(user) == 1 and user[0][1] == 1 and len(user[0][3]) == len(AUDIO) * 2
    kinds = [s.kind for s in rig.engine.recorder.spans]
    assert kinds[:2] == ["endpoint", "stt"] and "tts" in kinds
    assert rig.engine.recorder.spans[1].output == {"text": "주문 조회해 주세요."}
    assert rig.client.of("speech")[0]["since_utterance_ms"] is not None


def test_settings_come_from_the_definition_and_the_client():
    engine = FakeEngine(simple_script("네."), voice=VoiceSettings(tts_voice="M1", stt="qwen", endpoint="smart",
                                                                  interrupt_mode="unmute_gate", barge_in=True,
                                                                  speculative=False),
                        language="en")
    settings = voice_settings(engine.definition)
    assert settings == {"voice": "M1", "stt": "qwen", "endpoint": "smart", "interrupt_mode": "unmute_gate",
                        "barge_in": True, "speculative": False, "language": "en", "speed": None}
    # a "config" message, with one invalid value and one unknown key
    changed = apply_settings(settings, {"type": "config", "stt": "whisper", "endpoint": "sometimes",
                                        "tts_voice": "F2", "tools": True, "speculative": True})
    assert sorted(changed) == ["speculative", "stt", "voice"]
    assert settings["stt"] == "whisper" and settings["endpoint"] == "smart" and settings["voice"] == "F2"
    # the server hands over the query string: values are text
    settings = voice_settings(engine.definition, {"agent": "x", "barge_in": "0", "speculative": "true", "language": "ko"})
    assert settings["barge_in"] is False and settings["speculative"] is True and settings["language"] == "ko"


def test_config_message_changes_the_voice_of_the_next_reply():
    async def main():
        async with Rig(simple_script("네.")) as rig:
            await rig.session.on_message({"type": "config", "voice": "M2", "language": "en"})
            await rig.say("hello")
            await until(lambda: rig.session._reply_finished() and rig.client.binary)
            return rig

    rig = asyncio.run(main())
    assert rig.parts.tts.calls == [("네.", "M2", "en")] and rig.engine.calls[0]["language"] == "en"


def test_tts_failure_is_a_notice_not_a_crash():
    class Broken(FakeTTS):
        def synthesize(self, text, voice, lang, speed=None):
            raise RuntimeError("connection refused")

    async def main():
        async with Rig(simple_script("네."), parts=FakeParts(tts=Broken())) as rig:
            await rig.say()
            await until(lambda: rig.client.of("notice"))
            assert not rig.task.done()
            return rig

    rig = asyncio.run(main())
    assert "TTS" in rig.client.of("notice")[0]["message"]
    assert rig.engine.recorder.spans[0].kind == "tts" and rig.engine.recorder.spans[0].ok is False


# ---- two sessions -----------------------------------------------------------------------------------

def test_two_sessions_at_once_do_not_share_state():
    async def main():
        parts = FakeParts(tts=FakeTTS(delay=0.05))               # the same shared parts for both
        a = Rig(simple_script(FIRST, SECOND, tail=30), parts=parts, session_id="a", interrupt_mode="unmute",
                barge_in=True)
        b = Rig(simple_script("둘째 대화의 답입니다."), parts=parts, session_id="b", interrupt_mode="unmute",
                barge_in=True)
        async with a, b:
            assert a.endpointer is not b.endpointer
            await asyncio.gather(a.say("첫째"), b.say("둘째"))
            await until(lambda: len(a.client.binary) >= 2 and len(b.client.binary) >= 2)
            # interrupting one conversation leaves the other talking
            await a.mic(("start", None), in_speech=True)
            await a.session.on_message({"type": "played", "seq": 1, "offset": 0.2})
            await until(lambda: b.session._reply_finished())
            return a, b

    a, b = asyncio.run(main())
    assert a.engine.truncated == ["하나 둘 셋"] and b.engine.truncated == []
    assert len(a.client.of("interrupt")) == 1 and not b.client.of("interrupt")
    assert b.client.pcm() == fake_pcm("둘째 대화의 답입니다.") + b"\x00" * GAP_BYTES
    assert a.client.pcm() == (fake_pcm(FIRST) + b"\x00" * GAP_BYTES + fake_pcm(SECOND))[:len(a.client.pcm())]
    assert [seq for seq, _ in b.client.chunks()] == list(range(len(b.client.chunks())))
    assert {sid for sid, *_ in a.engine.recorder.audios} == {"a"}
    assert {sid for sid, *_ in b.engine.recorder.audios} == {"b"}
    assert b.engine.calls[0]["text"] == "둘째" and a.engine.calls[0]["text"] == "첫째"


def test_endpointers_share_the_vad_model_but_not_its_state():
    shared = audio_parts.SharedAudio()
    one, two = shared.new_endpointer(), shared.new_endpointer()
    assert one.vad.session is two.vad.session                    # one model in the process
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(16000) * 3000).astype(np.int16)
    one.feed(noise)
    assert np.abs(one.vad.state).sum() > 0 and np.abs(two.vad.state).sum() == 0
    assert len(one.tail) == 16000 % 512 and len(two.tail) == 0
    # the second one answers as a fresh endpointer would, whatever the first has heard
    fresh = shared.new_endpointer()
    window = noise[:512].astype(np.float32) / 32768.0
    assert two.vad(window) == pytest.approx(fresh.vad(window), abs=1e-6)
    one.reset()
    assert np.abs(one.vad.state).sum() == 0


def test_endpointer_events_with_a_scripted_vad():
    class ScriptedVad:
        """Speech probability taken from the first sample of each window."""
        def reset_states(self): pass
        def __call__(self, window): return float(window[0] * 32768.0 / 1000.0)

    def windows(value, count):
        return np.full(count * 512, int(value * 1000), dtype=np.int16)

    endpointer = audio_parts.Endpointer(ScriptedVad())
    events = endpointer.feed(windows(0.0, 5))
    events += endpointer.feed(windows(0.9, 20))                  # 0.64 s of speech
    assert [kind for kind, _ in events] == ["start"]
    events = endpointer.feed(windows(0.0, 6))                    # 0.19 s of silence: a pause
    assert [kind for kind, _ in events] == ["pause"]
    assert [kind for kind, _ in endpointer.feed(windows(0.9, 3))] == ["resume"]
    events = endpointer.feed(windows(0.0, 25))                   # 0.8 s of silence: over
    assert [kind for kind, _ in events] == ["pause", "utterance"]
    assert events[-1][1].dtype == np.float32 and not endpointer.in_speech
    assert endpointer.last["silence"] == pytest.approx(21 * 512 / 16000)
    # with a judge that hears an unfinished phrase the wait is 2 s instead of 0.7 s
    endpointer.judge = lambda audio: 0.1
    endpointer.feed(windows(0.9, 20))
    assert [kind for kind, _ in endpointer.feed(windows(0.0, 40))] == ["pause"]
    assert [kind for kind, _ in endpointer.feed(windows(0.0, 30))] == ["utterance"]
    assert endpointer.last["probability"] == 0.1
    # a session waiting for a one-word answer caps that wait...
    endpointer.max_close = audio_parts.Endpointer.END_SILENCE
    endpointer.feed(windows(0.9, 20))
    assert [kind for kind, _ in endpointer.feed(windows(0.0, 25))] == ["pause", "utterance"]
    # ...and takes speech too short for the usual threshold (5 windows = 0.16 s)
    endpointer.max_close, endpointer.judge = None, None
    endpointer.feed(windows(0.9, 5))
    assert [kind for kind, _ in endpointer.feed(windows(0.0, 25))] == []
    endpointer.min_voiced = 0.12
    endpointer.feed(windows(0.9, 5))
    assert [kind for kind, _ in endpointer.feed(windows(0.0, 25))] == ["pause", "utterance"]


# ---- text helpers and framing -----------------------------------------------------------------------

def test_text_helpers():
    assert audio_parts.split_for_tts("네. 알겠습니다. 주문번호를 확인하고 바로 배송 상태를 알려 드리겠습니다, 잠시만 기다려 주세요. 감사합니다.") == [
        "네. 알겠습니다. 주문번호를 확인하고 바로 배송 상태를 알려 드리겠습니다, 잠시만 기다려 주세요.", "감사합니다."]
    assert audio_parts.is_backchannel("아 네, 그렇군요.") and not audio_parts.is_backchannel("네, 환불해 주세요.")
    # counts are synthesised the way they are said; the text shown and recorded keeps its digits
    assert audio_parts.spoken_form("딸기 라떼 1잔, 크루아상 2개, 합계 14700원 결제를 진행할까요?") == \
        "딸기 라떼 한 잔, 크루아상 두 개, 합계 14700원 결제를 진행할까요?"
    assert audio_parts.spoken_form("20잔") == "스무 잔" and audio_parts.spoken_form("21명") == "스물한 명"
    for untouched in ("주문 번호는 K-0012번입니다", "3개월 할부", "10달러", "100개", "1.5잔", "30분", "2026년 11월 17일"):
        assert audio_parts.spoken_form(untouched) == untouched
    assert audio_parts.spoken_form("1잔", "en") == "1잔"
    assert audio_parts.text_language("hello") == "en" and audio_parts.text_language("hello 네") == "ko"
    wav = np.concatenate([np.zeros(8000), np.full(1600, 0.5), np.zeros(8000)]).astype(np.float32)
    assert len(audio_parts.trim_silence(wav, 16000)) == 1600 + 2 * 640


def test_chunk_framing():
    assert pack_audio_chunk(7, b"\x01\x02\x03\x04") == b"FHAU" + struct.pack("<II", 7, 4) + b"\x01\x02\x03\x04"
    data = pack_video_chunk(3, 4800, b"\x00" * 8, [b"\xff\xd8a", b"\xff\xd8bc"], 12.5)
    assert parse_video_chunk(data) == (3, 4800, b"\x00" * 8, [b"\xff\xd8a", b"\xff\xd8bc"], 12.5)


# ---- serve_voice over a (fake) socket, and the avatar's single seat ---------------------------------

class FakeSocket:
    def __init__(self):
        self.inbox = asyncio.Queue()
        self.json = []
        self.binary = []

    async def receive(self):
        return await self.inbox.get()

    async def send_text(self, text):
        self.json.append(json.loads(text))

    async def send_bytes(self, data):
        self.binary.append(data)

    def text(self, **payload):
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(payload)})

    def disconnect(self):
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})


class FakeVideoEngine:
    """The surface of v1's Engine that the avatar channel uses. 5 frames (0.2 s) per slice."""
    sample_rate, fps, width, height = 16000, 25, 64, 64
    slice_frames, slice_samples = 5, 3200

    def __init__(self):
        self.steps = 0
        self.resets = 0
        self.images = []

    def reset(self):
        self.resets += 1

    def set_image(self, path, use_face_crop):
        self.images.append(path)

    def step(self, audio):
        assert audio.dtype == np.float32 and len(audio) == self.slice_samples
        self.steps += 1
        return [b"\xff\xd8frame%d" % i for i in range(self.slice_frames)]


def test_serve_voice_sends_ready_and_returns_on_disconnect():
    async def main():
        ws, engine = FakeSocket(), FakeEngine(simple_script("네, 말씀하세요."))
        task = asyncio.create_task(serve_voice(ws, engine, "voice", {"interrupt_mode": "basic"}, parts=FakeParts()))
        await until(lambda: ws.json)
        ready = ws.json[0]
        ws.inbox.put_nowait({"type": "websocket.receive", "text": "not json"})
        ws.inbox.put_nowait({"type": "websocket.receive", "bytes": silence_packet()})
        ws.text(type="chat", id="t1", text="여보세요")
        await until(lambda: ws.binary and any(m["type"] == "chat_done" for m in ws.json))
        ws.disconnect()
        await asyncio.wait_for(task, 3)
        return ws, engine, ready

    ws, engine, ready = asyncio.run(main())
    assert ready["type"] == "ready" and ready["channel"] == "voice" and ready["sample_rate"] == 16000
    assert ready["session_id"] == "s1" and ready["agent"] == "fake" and ready["version"] == 1
    assert ready["chunk_seconds"] == 0.48 and ready["settings"]["interrupt_mode"] == "basic"
    assert "fps" not in ready
    assert ws.binary[0][:4] == b"FHAU" and len(ws.binary[0]) <= 12 + 15360
    assert engine.closed is None                                 # closing the engine is the caller's job


def test_avatar_channel_sends_fhrt_and_takes_one_session_at_a_time():
    loads = []

    def loader(image):
        loads.append(image)
        return FakeVideoEngine()

    async def main():
        holder = avatar_module.AvatarHolder(loader=loader)
        first, second, third = FakeSocket(), FakeSocket(), FakeSocket()
        engine = FakeEngine(simple_script("안녕하세요."))
        task = asyncio.create_task(serve_voice(first, engine, "avatar", parts=FakeParts(), avatar_holder=holder))
        await until(lambda: len(first.binary) >= 2)              # idle chunks flow without any speech
        # a second avatar conversation is refused while the first one runs
        await asyncio.wait_for(serve_voice(second, FakeEngine(simple_script("네.")), "avatar", parts=FakeParts(),
                                           avatar_holder=holder), 2)
        idle = len(first.binary)
        first.text(type="chat", id="t1", text="안녕")
        await until(lambda: any(m["type"] == "speech" for m in first.json))
        await until(lambda: len(first.binary) >= idle + 8)
        first.disconnect()
        await asyncio.wait_for(task, 3)
        # ...and accepted once the seat is free; the model is not loaded again
        task = asyncio.create_task(serve_voice(third, FakeEngine(simple_script("네.")), "avatar", parts=FakeParts(),
                                               avatar_holder=holder))
        await until(lambda: third.json and third.binary)
        third.disconnect()
        await asyncio.wait_for(task, 3)
        return holder, first, second, third

    holder, first, second, third = asyncio.run(main())
    assert len(loads) == 1 and loads[0].endswith("examples/girl.png")
    assert holder.engine.resets == 2 and not holder.busy
    ready = first.json[0]
    assert (ready["type"], ready["channel"], ready["fps"], ready["slice_frames"]) == ("ready", "avatar", 25, 5)
    assert ready["width"] == 64 and ready["chunk_seconds"] == 0.2
    assert [m["type"] for m in second.json] == ["error"] and "아바타" in second.json[0]["message"]
    assert second.binary == []
    assert third.json[0]["type"] == "ready"
    chunks = [parse_video_chunk(data) for data in first.binary]
    assert [c[0] for c in chunks] == list(range(len(chunks)))                 # seq
    assert [c[1] for c in chunks] == [i * 3200 for i in range(len(chunks))]   # start_sample
    assert all(len(c[2]) == 6400 and len(c[3]) == 5 and c[3][0].startswith(b"\xff\xd8") for c in chunks)
    assert not any(chunks[0][2].strip(b"\x00"))                               # silent before the reply
    speech_seq = next(m for m in first.json if m["type"] == "speech")["seq"]
    spoken = b"".join(c[2] for c in chunks[speech_seq:])
    expected = fake_pcm("안녕하세요.") + b"\x00" * GAP_BYTES
    assert spoken[:len(expected)] == expected and not any(spoken[len(expected):])


def test_video_engine_can_be_prepared_ahead_shared_with_a_session_and_unloaded():
    """warm() loads without taking the seat; a session that starts meanwhile waits for that same load;
    unload() gives the engine up unless it is in use; an idle engine is unloaded after the limit."""
    loads = []

    def loader(image):
        loads.append(image)
        time.sleep(0.3)
        return FakeVideoEngine()

    async def main():
        holder = avatar_module.AvatarHolder(loader=loader)
        assert holder.status()["state"] == "unloaded" and holder.unload() is False
        assert holder.warm()["state"] == "loading" and holder.warm()["state"] == "loading"      # pressed twice: one load
        assert holder.unload() is False                                                          # not while loading
        session = await holder.acquire()                                                         # waits for the load in progress
        assert len(loads) == 1 and holder.status()["state"] == "in_use"
        assert holder.unload() is False and holder.unload_if_idle(0.01) is False                 # not while a session holds it
        session.release()
        status = holder.status()
        assert status["state"] == "ready" and status["idle_s"] == 0 and status["load_s"] >= 0.3
        assert holder.unload_if_idle(60) is False and holder.unload_if_idle(0) is False          # not idle long enough; 0 = never
        await asyncio.sleep(0.05)
        assert holder.unload_if_idle(0.01) is True and holder.status()["state"] == "unloaded" and holder.engine is None
        again = await holder.acquire()                                                           # loads again on demand
        assert len(loads) == 2 and holder.status()["state"] == "in_use"
        again.release()
        assert holder.unload() is True and holder.status()["state"] == "unloaded"
        with pytest.raises(avatar_module.AvatarUnavailable):
            holder.warm("no/such/image.png")

    asyncio.run(main())


def test_avatar_engine_that_fails_to_load_is_reported_and_frees_the_seat():
    def loader(image):
        raise RuntimeError("CUDA out of memory")

    async def main():
        holder = avatar_module.AvatarHolder(loader=loader)
        ws = FakeSocket()
        await asyncio.wait_for(serve_voice(ws, FakeEngine(simple_script("네.")), "avatar", parts=FakeParts(),
                                           avatar_holder=holder), 2)
        return holder, ws

    holder, ws = asyncio.run(main())
    assert [m["type"] for m in ws.json] == ["error"] and "CUDA out of memory" in ws.json[0]["message"]
    assert not holder.busy and not holder.loaded
