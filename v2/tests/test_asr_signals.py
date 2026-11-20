"""ASR quality, the pure parts: confidence of a recognition, automatic turn signals, character error rate."""
import numpy as np

from agentkit.eval import asr
from agentkit.runtime import audio as audio_parts
from agentkit.trace import flags


def turn(n, user, agent):
    return {"turn": n, "user_text": user, "agent_text": agent}


def stt(n, low):
    return {"turn": n, "kind": "stt", "attrs": {"engine": "whisper", "low_confidence": low}}


def test_transcript_unpacks_as_text_and_language_and_carries_confidence():
    result = audio_parts.Transcript("안녕하세요", "ko", -0.31, 0.02, 1.1)
    text, language = result
    assert (text, language) == ("안녕하세요", "ko") and result == ("안녕하세요", "ko")
    assert result.text == "안녕하세요" and result.language == "ko"
    assert result.confidence() == {"avg_logprob": -0.31, "no_speech_prob": 0.02, "compression_ratio": 1.1}
    bare = audio_parts.Transcript("hi", "en")
    assert bare.confidence() == {"avg_logprob": None, "no_speech_prob": None, "compression_ratio": None}


def test_low_confidence_thresholds():
    low = audio_parts.is_low_confidence
    assert low(-0.3, 0.1, 1.2, 2.0) is False
    assert low(-0.81, 0.1, 1.2, 2.0) is True            # unsure of the words
    assert low(-0.3, 0.51, 1.2, 2.0) is True            # probably not speech
    assert low(-0.3, 0.1, 2.41, 2.0) is True            # repetitive, made-up text
    assert low(-0.3, 0.1, 1.2, 0.39) is True            # too short to carry a sentence
    assert low(-0.8, 0.5, 2.4, 0.4) is False            # the limits themselves pass
    assert low(None, None, None, 1.0) is False          # Qwen: nothing but the length to go on
    assert low(None, None, None, 0.3) is True


def test_stt_quality_from_a_transcript_and_from_a_plain_tuple():
    tone = (0.1 * np.sin(np.arange(16000) * 0.1)).astype(np.float32)        # 1 s, about -23 dBFS
    attrs = audio_parts.stt_quality(audio_parts.Transcript("네", "ko", -0.9, 0.01, 1.0), tone)
    assert attrs["avg_logprob"] == -0.9 and attrs["audio_s"] == 1.0 and attrs["low_confidence"] is True
    assert -24 < attrs["rms_db"] < -22
    plain = audio_parts.stt_quality(("네", "ko"), tone[:4800])              # a fake or a remote recogniser
    assert plain == {"avg_logprob": None, "no_speech_prob": None, "compression_ratio": None,
                     "audio_s": 0.3, "rms_db": plain["rms_db"], "low_confidence": True}
    assert audio_parts.rms_db(np.zeros(100, dtype=np.float32)) == -120.0


def test_auto_signals_on_the_conversation_from_the_spec():
    turns = [
        turn(1, "가져서 배달됐어요.", "죄송합니다, 다시 한번 말씀해 주시겠어요?"),
        turn(2, "뺏어져서 배달됐다고요.", "확인을 위해 잠시만 기다려 주세요."),
        turn(3, "언제까지 기다려요? 아까 말했잖아요.", "주문을 확인했습니다. 파손 접수를 도와드릴게요."),
        turn(4, "네", "배송 상태를 확인해 드릴게요. 주문번호를 말씀해 주시겠어요?"),
        turn(5, "네", "잠시만 기다려 주세요."),
    ]
    spans = [stt(1, True), stt(2, False), stt(3, False), {"turn": 5, "kind": "tool", "attrs": {}}]
    assert flags.auto_signals(turns, spans) == {
        1: ["low_confidence", "reask"],
        2: ["repeat", "empty_wait"],              # says nearly the same again; the agent only says "wait"
        3: ["complaint"],
        4: [],                                    # a promise that ends in a question hands the turn back
        5: [],                                    # "네" twice is too short to be a repeat; a tool was called
    }


def test_repeat_needs_four_characters_and_sixty_percent():
    assert flags.similar("환불해 주세요", "환불 해주세요!") is True
    assert flags.similar("네네", "네네") is False                      # under four characters
    assert flags.similar("환불해 주세요", "배송지를 바꾸고 싶어요") is False


def test_cer_ignores_spaces_and_punctuation():
    assert asr.cer("떨어진 상태로 배달됐다고요", "떨어진 상태로 배달됐다고요.") == 0.0
    assert asr.cer("안녕하세요", "안녕하세요") == 0.0
    assert asr.cer("안녕하세요", "안녕하새요") == 0.2                   # one of five characters
    assert asr.cer("안녕", "") == 1.0
    assert asr.cer("네", "네 그렇습니다") == 5.0                        # insertions can push it past 1
    assert asr.cer("", "뭔가") is None and asr.cer(" .,!", "뭔가") is None
    assert asr.cer("Hello, World", "hello world") == 0.0
    assert asr.edit_distance("kitten", "sitting") == 3


def test_bench_summary_is_over_all_characters():
    items = [
        {"expected": "안녕하세요", "results": {"whisper": {"text": "안녕하세요", "cer": 0.0, "ms": 100.0},
                                            "qwen": {"error": "boom"}}},
        {"expected": "주문번호는 A1001입니다", "results": {"whisper": {"text": "주문번호는 A1001", "cer": 0.25, "ms": 300.0}}},
    ]
    summary = asr.summarize(items, ["whisper", "qwen"], {"whisper": True, "qwen": False})
    # 5 + 13 expected characters, 3 edits ("입니다" missing)
    assert summary["whisper"] == {"n": 2, "cer": round(3 / 18, 4), "exact": 1, "exact_rate": 0.5,
                                  "ms_median": 200.0, "available": True}
    assert summary["qwen"] == {"n": 0, "cer": None, "exact": 0, "exact_rate": None, "ms_median": None,
                               "available": False}
