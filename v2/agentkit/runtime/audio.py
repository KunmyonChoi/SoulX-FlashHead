"""
Audio parts of the voice channel: end-of-turn detection, speech recognition, speech synthesis.

Ported from v1 (realtime_mic_app.py: Endpointer, SmartTurn, SpeechToText, RemoteSpeechToText,
TTSClient, split_for_tts, trim_silence). What changed is ownership: v1 served one session, so each
class owned its model. Here the models are loaded once per process (`shared()`), and whatever
changes while a conversation runs lives in per-session objects (`Endpointer`, `Vad`).
"""
from __future__ import annotations

import io
import json
import logging
import math
import re
import threading
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .. import config

logger = logging.getLogger("agentkit.voice")

SAMPLE_RATE = 16000          # microphone, recogniser, synthesised speech and recorder all use this
LANGUAGES = ("ko", "en")
HANGUL = re.compile("[가-힣]")

# Listener noises that should not take the turn, alone or strung together ("아 네 그렇군요").
BACKCHANNEL = re.compile(
    r"^(네|예|응|음|흠|어|아|오|와|아하|그래|그래요|그렇군요|그렇구나|그렇죠|그쵸|맞아|맞아요|"
    r"정말|정말요|진짜|진짜요|좋네요|좋아요|알겠어요|그러네요|그렇네요|그렇겠네요|그렇겠어요|"
    r"그러게요|그럼요|그죠|맞네요|우와|대박|아이고)+$"
)
# A transcript that reads like a finished sentence (Whisper punctuates what it takes to be complete).
SENTENCE_END = re.compile(r"([.?!]|요|니다|습니까|입니까|죠|네)\s*$")


# ---- how a written count is said aloud ---------------------------------------------------------
# "1잔" is read "일잔" by the synthesiser; with these counters Korean counts in native numerals ("한 잔").
_NATIVE_UNITS = ["", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉"]
_NATIVE_TENS = ["", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔"]
# Counters that take native numerals. Left out on purpose: 번 ("주문 번호 12번"), 분 (minutes), 대 ("10대"),
# 층, 원, 년, 월, 일 and other Sino-Korean ones. "개월", "개국" and "달러" are excluded by the lookahead.
_NATIVE_COUNTER = re.compile(
    r"(?<![\d.,:/\-])(\d{1,2})\s?"
    r"(잔|개(?![월국년소])|명|병|장(?!소)|살|마리|권|컵|조각|봉지|그릇|접시|가지|군데|곳|판|벌|켤레|송이|시간|사람|달(?!러))"
    r"(?![0-9])")


def native_count(number):
    """1..99 as said before a counter: 1 -> 한, 20 -> 스무, 21 -> 스물한. None outside that range."""
    if not 1 <= number <= 99:
        return None
    tens, units = divmod(number, 10)
    if units == 0:
        return "스무" if tens == 2 else _NATIVE_TENS[tens]
    return _NATIVE_TENS[tens] + _NATIVE_UNITS[units]


def spoken_form(text, lang="ko"):
    """The text as it should be synthesised. Only speech uses this; what is shown and recorded keeps the digits."""
    if not str(lang or "").startswith("ko"):
        return text

    def say(match):
        word = native_count(int(match.group(1)))
        return match.group(0) if word is None else f"{word} {match.group(2)}"
    return _NATIVE_COUNTER.sub(say, text)


def is_backchannel(text):
    return bool(BACKCHANNEL.match(re.sub(r"[\s.,!?~…]", "", text)))


# A short answer to a yes/no question. Said right after the question, it must not be lost as noise, as a
# listener's backchannel, or as the agent's own echo.
SHORT_ANSWER = re.compile(r"^(네|예|응|어|좋아|좋아요|좋습니다|그래|그래요|그러죠|맞아|맞아요|오케이|알겠어요|알겠습니다|"
                          r"해줘|해주세요|아니|아니요|아뇨|싫어|싫어요|됐어|됐어요)+$")


def bare(text):
    return re.sub(r"[\s.,!?~…'\"]", "", text or "")


def is_short_answer(text):
    return bool(SHORT_ANSWER.match(bare(text)))


# What Whisper writes for silence or faint noise in Korean (it learnt them from video subtitles). Heard with
# little speech in the audio or low confidence, such a line is not the customer talking.
HALLUCINATION = re.compile(r"^(감사합니다|고맙습니다|시청해주셔서감사합니다|구독과좋아요.*|좋아요와구독.*|"
                           r"다음영상에서만나요|MBC뉴스.*|자막.*|thankyou|thanksforwatching)+$", re.I)
HALLUCINATION_MIN_SPEECH = 0.5     # seconds of speech the voice detector finds in the utterance
HALLUCINATION_MIN_LOGPROB = -0.4   # Whisper's average log-probability
_speech_vad = None


def speech_seconds(audio, sample_rate=16000):
    """Seconds of speech Silero finds in a whole utterance (float32 or int16 samples)."""
    global _speech_vad
    import numpy as np
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad
    if _speech_vad is None:
        _speech_vad = load_silero_vad()
    x = np.asarray(audio)
    if x.dtype != np.float32:
        x = x.astype(np.float32) / 32768.0
    stamps = get_speech_timestamps(torch.from_numpy(x), _speech_vad, sampling_rate=sample_rate)
    return sum(s["end"] - s["start"] for s in stamps) / sample_rate


def is_hallucination(text, speech_s=None, avg_logprob=None):
    """A subtitle line Whisper made up: "감사합니다." with almost no speech in it, or with low confidence."""
    if not HALLUCINATION.match(bare(text)):
        return False
    return (speech_s is not None and speech_s < HALLUCINATION_MIN_SPEECH) or \
        (avg_logprob is not None and avg_logprob < HALLUCINATION_MIN_LOGPROB)


def is_echo(text, agent_text):
    """The microphone heard the end of the agent's own words (speakers, no headphones), not the customer."""
    heard = bare(text)
    return len(heard) >= 2 and not SHORT_ANSWER.match(heard) and heard in bare(agent_text)[-40:]


def text_language(text):
    """"ko" if the text has any Hangul, else "en"."""
    return "ko" if HANGUL.search(text) else "en"


def split_for_tts(text, min_chars=40):
    """Split text into sentence groups that are synthesised one request at a time.

    Short sentences are merged with the next one: a group must play for longer than
    the following group takes to synthesise, or the speech pauses between them.
    """
    sentences = [p.strip() for p in re.split(r"(?<=[.!?。！？])\s+|\n+", text.strip()) if p.strip()]
    groups, current = [], ""
    for sentence in sentences:
        current = f"{current} {sentence}".strip()
        if len(current) >= min_chars:
            groups.append(current)
            current = ""
    if current:
        groups.append(current)
    return groups


def trim_silence(wav, sample_rate, threshold=0.005, margin=0.04):
    """Drop leading/trailing silence so pauses between groups are set by us, not by the TTS."""
    frame = sample_rate // 50
    count = len(wav) // frame
    if count == 0:
        return wav
    rms = np.sqrt((wav[:count * frame].reshape(count, frame) ** 2).mean(axis=1))
    active = np.flatnonzero(rms > threshold)
    if len(active) == 0:
        return wav
    start = max(0, active[0] * frame - int(margin * sample_rate))
    end = min(len(wav), (active[-1] + 1) * frame + int(margin * sample_rate))
    return wav[start:end]


def float_to_pcm16(audio):
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


class TTSClient:
    """Minimal client for `supertonic serve`. Stateless, so one instance serves every session."""

    def __init__(self, url=None, sample_rate=SAMPLE_RATE):
        self.url = (url or config.TTS_URL).rstrip("/")
        self.sample_rate = sample_rate

    def voices(self):
        with urllib.request.urlopen(self.url + "/v1/styles", timeout=3) as response:
            return [style["name"] for style in json.load(response)["styles"]]

    def synthesize(self, text, voice, lang, speed=None):
        """Blocking. Returns 16-bit mono PCM at `sample_rate`."""
        import soundfile as sf
        from scipy.signal import resample_poly
        body = {"text": spoken_form(text, lang), "voice": voice, "lang": lang, "response_format": "wav"}
        if speed:
            body["speed"] = speed
        request = urllib.request.Request(
            self.url + "/v1/tts",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            wav, sr = sf.read(io.BytesIO(response.read()), dtype="float32", always_2d=True)
        wav = trim_silence(wav.mean(axis=1), sr)
        if sr != self.sample_rate:
            divisor = math.gcd(sr, self.sample_rate)
            wav = resample_poly(wav, self.sample_rate // divisor, sr // divisor)
        return float_to_pcm16(wav)


class SmartTurn:
    """End-of-turn detection from the sound of the speech (pipecat-ai/smart-turn v3.2, ONNX on the CPU).

    Given the last 8 seconds of an utterance it returns the probability that the speaker has
    finished. Keeps no state between calls, so sessions share one instance."""

    SECONDS = 8

    def __init__(self, path, sample_rate=SAMPLE_RATE):
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 2
        self.session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self.features = WhisperFeatureExtractor(chunk_length=self.SECONDS)
        self.samples = self.SECONDS * sample_rate
        self.sample_rate = sample_rate

    def probability(self, audio):
        """audio: float32 [-1, 1]. Takes about 35 ms."""
        audio = audio[-self.samples:]
        audio = np.pad(audio, (self.samples - len(audio), 0))  # the model expects the speech at the end
        inputs = self.features(audio, sampling_rate=self.sample_rate, return_tensors="np", padding="max_length",
                               max_length=self.samples, truncation=True, do_normalize=True)
        return float(self.session.run(None, {"input_features": inputs.input_features.astype(np.float32)})[0].item())


class Vad:
    """One session's view of the shared Silero VAD model: the ONNX session is shared, the recurrent
    state and the 64-sample context (which Silero keeps between windows) belong to this object."""

    CONTEXT = 64

    def __init__(self, session, sample_rate=SAMPLE_RATE):
        self.session = session
        self.rate = np.array(sample_rate, dtype="int64")
        self.reset_states()

    def reset_states(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, self.CONTEXT), dtype=np.float32)

    def __call__(self, window):
        """window: 512 float32 samples in [-1, 1]. Returns the probability of speech."""
        x = np.concatenate([self.context, window[None, :].astype(np.float32)], axis=1)
        out, self.state = self.session.run(None, {"input": x, "state": self.state, "sr": self.rate})
        self.context = x[:, -self.CONTEXT:]
        return float(out[0, 0])


class Endpointer:
    """Splits a 16 kHz PCM stream into utterances. One per session.

    vad: a callable window -> speech probability with reset_states() (see Vad).
    With a `judge` (SmartTurn.probability) the silence that closes an utterance is no longer fixed:
    at each short pause the judge says how finished the speech sounds, and the wait is shortened
    (sounds finished), kept, or lengthened (sounds unfinished) accordingly."""

    WINDOW = 512            # samples per VAD decision (32 ms), fixed by the model
    START_WINDOWS = 3       # consecutive voiced windows that open an utterance
    END_SILENCE = 0.7       # seconds of silence that close it
    PAUSE_SILENCE = 0.2     # seconds of silence after which the utterance *may* be over
    PRE_ROLL_WINDOWS = 10   # audio kept from before the start, so the first syllable survives
    MIN_VOICED = 0.25       # seconds; shorter bursts are discarded as noise
    MAX_LENGTH = 20.0       # seconds
    # With a judge: (probability at least, seconds of silence that close the utterance), first match.
    # Not straight to the pause itself for "finished": unfinished phrases have scored 0.9 in tests.
    JUDGED_SILENCE = ((0.95, 0.35), (0.5, 0.7), (0.0, 2.0))

    def __init__(self, vad, sample_rate=SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.vad = vad
        self.end_windows = int(self.END_SILENCE * sample_rate / self.WINDOW)
        self.pause_windows = int(self.PAUSE_SILENCE * sample_rate / self.WINDOW)
        self.max_windows = int(self.MAX_LENGTH * sample_rate / self.WINDOW)
        self.judge = None          # set by the session: audio -> probability that the turn is over
        # Set by the session while the agent waits for a short answer ("네" to a yes/no question):
        self.min_voiced = self.MIN_VOICED   # a one-syllable answer is shorter than the usual noise threshold
        self.max_close = None               # seconds; caps the wait the judge asks for ("네" sounds unfinished to it)
        self.last = {"silence": self.END_SILENCE, "probability": None}  # how the latest utterance was closed
        self.reset()

    def reset(self):
        self.vad.reset_states()
        self.tail = np.zeros(0, dtype=np.int16)
        self.pre_roll = deque(maxlen=self.PRE_ROLL_WINDOWS)
        self.in_speech = False
        self.voiced_run = 0
        self.windows = []
        self.voiced = 0
        self.silence = 0
        self.paused = False
        self.close_windows = self.end_windows  # silence that closes the current utterance
        self.probability = None

    def feed(self, pcm):
        """pcm: int16 array. Returns events, in order:

        ("start", None)       speech began
        ("pause", audio)      a short silence: the utterance so far, which may turn out to be all of it
        ("resume", None)      the speaker went on after a pause
        ("utterance", audio)  the utterance is over
        """
        events = []
        data = np.concatenate([self.tail, pcm])
        count = len(data) // self.WINDOW
        self.tail = data[count * self.WINDOW:]
        for window in data[:count * self.WINDOW].reshape(count, self.WINDOW):
            prob = self.vad(window.astype(np.float32) / 32768.0)
            if not self.in_speech:
                self.pre_roll.append(window)
                self.voiced_run = self.voiced_run + 1 if prob > 0.5 else 0
                if self.voiced_run >= self.START_WINDOWS:
                    self.in_speech = True
                    self.windows = list(self.pre_roll)
                    self.voiced = self.voiced_run
                    self.silence = 0
                    events.append(("start", None))
                continue
            self.windows.append(window)
            if prob > 0.5:
                self.voiced += 1
            self.silence = 0 if prob > 0.35 else self.silence + 1
            long_enough = self.voiced * self.WINDOW / self.sample_rate >= self.min_voiced
            if self.silence == 0 and self.paused:
                self.paused = False
                self.close_windows, self.probability = self.end_windows, None
                events.append(("resume", None))
            elif self.silence == self.pause_windows and long_enough and not self.paused:
                self.paused = True
                so_far = np.concatenate(self.windows).astype(np.float32) / 32768.0
                if self.judge is not None:
                    try:
                        self.probability = self.judge(so_far)
                        seconds = next(s for p, s in self.JUDGED_SILENCE if self.probability >= p)
                        if self.max_close is not None:
                            seconds = min(seconds, self.max_close)
                        self.close_windows = int(seconds * self.sample_rate / self.WINDOW)
                    except Exception as e:  # never let the judge stop the microphone
                        logger.warning("End-of-turn model failed: %s", e)
                        self.close_windows, self.probability = self.end_windows, None
                events.append(("pause", so_far))
            if self.silence >= self.close_windows or len(self.windows) >= self.max_windows:
                if long_enough:
                    audio = np.concatenate(self.windows).astype(np.float32) / 32768.0
                    self.last = {"silence": self.silence * self.WINDOW / self.sample_rate,
                                 "probability": self.probability}
                    events.append(("utterance", audio))
                self.paused = False
                self.in_speech = False
                self.voiced_run = 0
                self.pre_roll.clear()
                self.windows = []
                self.close_windows, self.probability = self.end_windows, None
        return events


class Transcript(tuple):
    """What a recogniser returns. Unpacks as (text, language), as it always did; how sure the
    recogniser was rides along as attributes (None where the recogniser does not say)."""

    def __new__(cls, text, language, avg_logprob=None, no_speech_prob=None, compression_ratio=None):
        self = super().__new__(cls, (text, language))
        self.avg_logprob = avg_logprob
        self.no_speech_prob = no_speech_prob
        self.compression_ratio = compression_ratio
        return self

    @property
    def text(self):
        return self[0]

    @property
    def language(self):
        return self[1]

    def confidence(self):
        return {"avg_logprob": self.avg_logprob, "no_speech_prob": self.no_speech_prob,
                "compression_ratio": self.compression_ratio}


# Below (or above) these a recognition is not to be trusted (docs/ASR_QUALITY.md).
LOW_LOGPROB = -0.8
HIGH_NO_SPEECH = 0.5
HIGH_COMPRESSION = 2.4
SHORT_AUDIO_S = 0.4


def rms_db(audio):
    """Mean level of float32 [-1, 1] audio in dBFS (0 = full scale); -120 for silence or nothing."""
    if audio is None or len(audio) == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(np.asarray(audio, dtype=np.float64)))))
    return round(max(-120.0, 20.0 * math.log10(rms)), 1) if rms > 0 else -120.0


def is_low_confidence(avg_logprob=None, no_speech_prob=None, compression_ratio=None, audio_s=None):
    """The spec's rule; a value that is missing (Qwen has none) does not count against the turn."""
    return bool((avg_logprob is not None and avg_logprob < LOW_LOGPROB)
                or (no_speech_prob is not None and no_speech_prob > HIGH_NO_SPEECH)
                or (compression_ratio is not None and compression_ratio > HIGH_COMPRESSION)
                or (audio_s is not None and audio_s < SHORT_AUDIO_S))


def stt_quality(result, audio):
    """The attrs the stt span carries about one recognition: the recogniser's own confidence
    (from a Transcript; a plain (text, language) gives nulls), plus length and level of the audio."""
    seconds = None if audio is None else round(len(audio) / SAMPLE_RATE, 3)
    values = {name: getattr(result, name, None) for name in ("avg_logprob", "no_speech_prob", "compression_ratio")}
    return {**values, "audio_s": seconds, "rms_db": None if audio is None else rms_db(audio),
            "low_confidence": is_low_confidence(audio_s=seconds, **values)}


class SpeechToText:
    """faster-whisper in this process. One per process; calls go through SharedAudio.stt_pool."""

    name = "whisper"

    def __init__(self, model_name=None, language="ko"):
        from faster_whisper import WhisperModel
        model_name = model_name or config.WHISPER_MODEL
        try:
            self.model = WhisperModel(model_name, device="cuda", compute_type="float16")
        except Exception as e:
            logger.warning("Whisper could not use the GPU (%s); falling back to the CPU", e)
            self.model = WhisperModel(model_name, device="cpu", compute_type="int8")
        self.language = language

    def transcribe(self, audio, language=None):
        """Returns a Transcript: (text, language) plus avg_logprob (mean over the segments, weighted
        by their length), no_speech_prob and compression_ratio (the worst segment of each).
        language: "ko" / "en", or "auto" to pick between the two."""
        language = language or self.language
        if language == "auto":
            # Whisper's own guess, limited to the languages the rest of the pipeline can speak:
            # a short Korean utterance is otherwise now and then taken for Japanese or Chinese
            _, _, scores = self.model.detect_language(audio)
            scores = dict(scores)
            language = max(LANGUAGES, key=lambda code: scores.get(code, 0.0))
        segments, _ = self.model.transcribe(
            audio, language=language, beam_size=1, condition_on_previous_text=False
        )
        segments = list(segments)
        # Whisper invents phrases for non-speech; its own no-speech score filters most of them.
        text = "".join(s.text for s in segments if s.no_speech_prob < 0.6).strip()
        if not segments:
            return Transcript(text, language)
        # the confidence covers every segment, the dropped ones too: they are part of what was heard
        weights = [max(float(s.end) - float(s.start), 0.01) for s in segments]
        logprob = sum(float(s.avg_logprob) * w for s, w in zip(segments, weights)) / sum(weights)
        return Transcript(text, language, round(logprob, 4),
                          round(max(float(s.no_speech_prob) for s in segments), 4),
                          round(max(float(s.compression_ratio) for s in segments), 3))


class RemoteSpeechToText:
    """Speech recognition by realtime_asr_server.py (Qwen3-ASR), behind the same call as SpeechToText."""

    name = "qwen"

    def __init__(self, url=None):
        self.url = (url or config.ASR_URL).rstrip("/")

    def model_name(self):
        """The model the server runs, or None if it is not reachable."""
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=1.5) as response:
                return json.load(response).get("model")
        except Exception:
            return None

    def transcribe(self, audio, language=None):
        """Returns a Transcript without confidence values: the server does not report any."""
        request = urllib.request.Request(
            f"{self.url}/transcribe?language={language or 'ko'}", data=float_to_pcm16(audio),
            headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
        return Transcript(result["text"], result["language"])


class SharedAudio:
    """The models every voice session of this process shares. Each is loaded on first use,
    under a lock, so two sessions starting together load it once.

    whisper()      -> SpeechToText        (GPU; calls must run on `stt_pool`, one at a time)
    qwen()         -> RemoteSpeechToText  (HTTP, no model here)
    smart_turn()   -> SmartTurn or None   (None if the ONNX file is missing or fails to load)
    new_endpointer() -> Endpointer with its own VAD state over the shared VAD model
    tts            -> TTSClient
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._whisper = None
        self._whisper_error = None
        self._whisper_loading = None    # when a load began, while it is running
        self._whisper_used = None       # last time a session asked for the recogniser
        self.whisper_load_seconds = None
        self._smart_turn = None
        self._smart_turn_tried = False
        self._vad_session = None
        self._qwen = RemoteSpeechToText()
        self.tts = TTSClient()
        self.stt_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agentkit-stt")
        self.tts_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agentkit-tts")
        self.filler_cache = {}     # (voice, lang, text) -> PCM of a "one moment" line

    def whisper(self):
        """Blocking on first call (several seconds). Raises if the model cannot be loaded."""
        with self._lock:
            self._whisper_used = time.time()
            if self._whisper is None:
                if self._whisper_error is not None:
                    raise RuntimeError(self._whisper_error)
                self._whisper_loading = time.time()
                try:
                    self._whisper = SpeechToText()
                    # the first transcription pays for GPU initialisation; better now than in a turn
                    self._whisper.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), "ko")
                    self.whisper_load_seconds = time.time() - self._whisper_loading
                    logger.info("Speech recognition ready (%s).", config.WHISPER_MODEL)
                except Exception as e:
                    self._whisper_error = str(e)
                    raise
                finally:
                    self._whisper_loading = None
            return self._whisper

    def warm_whisper(self):
        """Loads the recogniser ahead of the first spoken turn, on the recogniser's own thread. Returns at once."""
        if self._whisper is None and self._whisper_loading is None:
            self._whisper_loading = time.time()     # shown as "loading" until whisper() takes over
            self.stt_pool.submit(self._warm_whisper)
        return self.whisper_status()

    def _warm_whisper(self):
        try:
            self.whisper()
        except Exception as e:
            self._whisper_loading = None
            logger.warning("Speech recognition could not be prepared: %s", e)

    def unload_whisper(self):
        """Drops the recogniser and gives its GPU memory back. The caller makes sure no session is listening."""
        with self._lock:
            if self._whisper is None:
                return False
            self._whisper, self._whisper_used, self._whisper_error = None, None, None
        import gc
        gc.collect()
        logger.info("Speech recognition unloaded.")
        return True

    def whisper_status(self):
        now = time.time()
        if self._whisper is None:
            if self._whisper_loading is not None:
                return {"state": "loading", "loading_s": round(now - self._whisper_loading, 1),
                        "expected_s": round(self.whisper_load_seconds or 8)}
            return {"state": "unloaded", "expected_s": round(self.whisper_load_seconds or 8), "error": self._whisper_error}
        return {"state": "ready", "load_s": round(self.whisper_load_seconds or 0, 1),
                "idle_s": None if self._whisper_used is None else round(now - self._whisper_used)}

    def qwen(self):
        return self._qwen

    def recognizer(self, name):
        return self.qwen() if name == "qwen" else self.whisper()

    def smart_turn(self):
        with self._lock:
            if not self._smart_turn_tried:
                self._smart_turn_tried = True
                try:
                    model = SmartTurn(config.SMART_TURN_MODEL)
                    model.probability(np.zeros(SAMPLE_RATE, dtype=np.float32))  # first call loads the graph
                    self._smart_turn = model
                    logger.info("End-of-turn model ready (%s).", config.SMART_TURN_MODEL)
                except Exception as e:
                    logger.info("End-of-turn model off: %s", e)
            return self._smart_turn

    def vad_session(self):
        with self._lock:
            if self._vad_session is None:
                from silero_vad import load_silero_vad
                self._vad_session = load_silero_vad(onnx=True).session
            return self._vad_session

    def new_endpointer(self):
        return Endpointer(Vad(self.vad_session()))


_shared = None
_shared_lock = threading.Lock()


def shared():
    """The process-wide SharedAudio."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = SharedAudio()
        return _shared
