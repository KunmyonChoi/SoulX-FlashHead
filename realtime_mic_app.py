"""
Real-time talking-head streaming server with two live audio sources.

mic:  the browser sends 16 kHz mono PCM from the microphone over a WebSocket.
tts:  the browser sends text; the server has a Supertonic server synthesise it
      sentence by sentence and feeds the speech into a continuously running stream.
chat: like tts, but the text is a user message; a local LLM (Ollama) answers and
      its reply is spoken as it streams in.
voice: like chat, but the user speaks: the microphone stream is split into
      utterances (Silero VAD) and transcribed (faster-whisper).
auto: a test harness for conversational naturalness. A second LLM persona plays the
      human: it listens to the avatar through STT, decides to wait, backchannel,
      interrupt or ask the next thing, and speaks through TTS into a virtual
      microphone - so its voice goes through exactly the same path as the voice source.

Optional "speculative" replies (voice and auto sources): the reply is prepared as soon as
the speaker pauses for PAUSE_SILENCE, held back, and released if the pause turns out to be
the end of the utterance (END_SILENCE) or thrown away if the speaker goes on.

Optional live information (client setting "tools", chat / voice / auto sources): the current
time and date are put into the system prompt, and the LLM may call the tools in
realtime_tools.py (weather, news). While a lookup runs the avatar says a short pre-synthesised
line ("날씨 확인해 볼게요."), so the wait is not silence. An interruption cancels the lookup.
The tools only read, so a speculative reply may run them before it is committed; a tool with
side effects would have to wait for the commit.

Interrupting a reply has two selectable behaviours (client setting "interrupt_mode"):
basic:  the server drops speech it has not sent yet; whatever the browser already
        holds keeps playing, and the full reply stays in the chat history.
unmute: (after kyutai-labs/unmute) the browser is told to flush what it holds and
        reports how far playback got; the reply in the history is cut to what was heard.
unmute_gate: unmute, plus the avatar keeps talking through short listener noises
        ("네", "아 그렇군요"): speech over the avatar only interrupts once it has lasted
        BARGE_IN_SECONDS, or once it has ended and turned out not to be a backchannel.

Either way, for every chunk of audio (lite: 0.96 s, pro: 1.12 s) the server
generates the matching video frames and sends them back together with that
audio, so the browser can play both in sync.

Usage:
    supertonic serve --port 7788                 # needed for the tts and chat sources
    ollama serve                                 # needed for the chat and voice sources
    CUDA_VISIBLE_DEVICES=0 python realtime_mic_app.py
    # then open http://127.0.0.1:7861 in Chrome
"""
import argparse
import asyncio
import io
import json
import math
import os
import re
import struct
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import cv2
import httpx
import numpy as np
import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from loguru import logger
from PIL import Image
from scipy.signal import resample_poly

from realtime_tools import Toolbox

from flash_head.inference import (
    get_pipeline,
    get_base_data,
    get_infer_params,
    get_audio_embedding,
    run_pipeline,
)

MAGIC = b"FHRT"
GATE_FRAME = 320  # 20 ms at 16 kHz
GUEST_MAGIC = b"FHGA"  # binary message carrying the simulated user's voice (auto source)
# Rough delay between the server sending a chunk and the browser starting to play it.
CLIENT_PLAYBACK_DELAY = 0.3
# How many times one reply may go back to the LLM with tool results before it has to answer.
MAX_TOOL_ROUNDS = 2

# unmute_gate: voiced time after which speech over the avatar counts as an interruption.
BARGE_IN_SECONDS = 1.2
# Listener noises that should not take the turn, alone or strung together ("아 네 그렇군요").
BACKCHANNEL = re.compile(
    r"^(네|예|응|음|흠|어|아|오|와|아하|그래|그래요|그렇군요|그렇구나|그렇죠|그쵸|맞아|맞아요|"
    r"정말|정말요|진짜|진짜요|좋네요|좋아요|알겠어요|그러네요|그렇네요|그렇겠네요|그렇겠어요|"
    r"그러게요|그럼요|그죠|맞네요|우와|대박|아이고)+$"
)


def is_backchannel(text):
    return bool(BACKCHANNEL.match(re.sub(r"[\s.,!?~…]", "", text)))


# A short answer to a yes/no question. Said right after the question, it must not be lost as noise, as a
# listener's backchannel, or as the avatar's own echo.
SHORT_ANSWER = re.compile(r"^(네|예|응|어|좋아|좋아요|좋습니다|그래|그래요|그러죠|맞아|맞아요|오케이|알겠어요|알겠습니다|"
                          r"해줘|해주세요|아니|아니요|아뇨|싫어|싫어요|됐어|됐어요)+$")
# While the avatar waits for an answer, speech this short still counts (a spoken "네" lasts 0.15 to 0.3 s).
SHORT_ANSWER_VOICED = 0.12


def _bare(text):
    return re.sub(r"[\s.,!?~…'\"]", "", text or "")


# What Whisper writes for silence or faint noise in Korean (it learnt them from video subtitles). Heard with
# little speech in the audio or low confidence, such a line is not the user talking. Checked against 18
# recorded turns (2026-11-20): all 11 made-up "감사합니다." dropped, the one clearly spoken kept.
HALLUCINATION = re.compile(r"^(감사합니다|고맙습니다|시청해주셔서감사합니다|구독과좋아요.*|좋아요와구독.*|"
                           r"다음영상에서만나요|MBC뉴스.*|자막.*|thankyou|thanksforwatching)+$", re.I)
_speech_vad = None


def speech_seconds(audio, sample_rate=16000):
    """Seconds of speech Silero finds in a whole utterance (float32 samples)."""
    global _speech_vad
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad
    if _speech_vad is None:
        _speech_vad = load_silero_vad()
    stamps = get_speech_timestamps(torch.from_numpy(np.asarray(audio, dtype=np.float32)), _speech_vad,
                                   sampling_rate=sample_rate)
    return sum(s["end"] - s["start"] for s in stamps) / sample_rate


def is_hallucination(text, speech_s=None, avg_logprob=None):
    if not HALLUCINATION.match(_bare(text)):
        return False
    return (speech_s is not None and speech_s < 0.5) or (avg_logprob is not None and avg_logprob < -0.4)


def is_echo(text, agent_text):
    """The microphone heard the end of the avatar's own words (speakers, no headphones), not the user."""
    heard = _bare(text)
    return len(heard) >= 2 and not SHORT_ANSWER.match(heard) and heard in _bare(agent_text)[-40:]


# A sentence that reports or promises a lookup. Said in a reply that made no tool call, it is
# either invented ("확인해 보니 주말엔 비가...") or an empty promise ("확인해 볼게요").
LOOKUP_CLAIM = re.compile(
    r"(확인|찾아|알아|검색|조회)\S{0,2}\s?(보니|봤더니|봤는데|봤어요|봤습니다|본 결과|볼게요|보겠습니다|드릴게요)"
    r"|(날씨|뉴스)를\s?보니")
# A temperature stated as a forecast: "영상 8도", "기온은 3도", "최고 12도".
FORECAST_TEMPERATURE = re.compile(r"(?:영상|영하|기온\S*|최고|최저)\s?(\d+)\s?도")
# A statement about the weather of some day: "주말엔 비 소식이 있어요", "내일은 많이 추워요".
WEATHER_STATEMENT = re.compile(
    r"(오늘|내일|모레|주말|이번 주|[월화수목금토일]요일)[^.?!]{0,30}"
    r"(비가|비 소식|비 올|눈이|눈 소식|맑|흐리|흐려|춥|추워|추울|덥|더워|더울|쌀쌀|포근|선선|소나기|화창)")
# "토요일은 21일": a weekday tied to a day of the month, which can be checked against the calendar.
WEEKDAY_DATE = re.compile(r"([월화수목금토일])요일\S{0,2}\s?(?:\d+월\s?)?(\d+)일")
DATE_NUDGE = (
    "(시스템 안내: 방금 말하려던 요일과 날짜가 달력과 맞지 않습니다. 달력을 다시 확인하고 답하세요.)"
)


def wrong_date(text, today):
    """True if the text pairs a weekday with a day of the month that is not that weekday (within three weeks)."""
    for weekday, day in WEEKDAY_DATE.findall(text):
        index = "월화수목금토일".index(weekday)
        valid = {(today + timedelta(days=offset)).day for offset in range(-7, 15)
                 if (today + timedelta(days=offset)).weekday() == index}
        if int(day) not in valid:
            return True
    return False


# A transcript that reads like a finished sentence (Whisper punctuates what it takes to be complete).
SENTENCE_END = re.compile(r"([.?!]|요|니다|습니까|입니까|죠|네)\s*$")
# A turn that refers back to what was just said rather than naming a topic of its own.
BACK_REFERENCE = re.compile(
    r"(첫|두|세|네|다섯)\s?번째|[1-5]번|마지막\s?(거|것|글|소식|뉴스|기사)|방금|아까|"
    r"^(그거|그게|그건|그것|그 글|그 소식|그 뉴스|그 기사|그럼|그러면|그래서|왜)\b|"
    r"\b(the (first|second|third|last) one|that one|you just (said|mentioned))\b", re.I)
LIVE_TOPIC = re.compile(r"날씨|기온|예보|뉴스|소식|기사|속보")
LOOKUP_NUDGE = (
    "(시스템 안내: 방금 도구를 호출하지 않고 날씨나 뉴스를 확인한 것처럼 말하려 했습니다. "
    "확인이 필요하면 지금 도구를 호출하고, 필요 없으면 확인했다는 말 없이 답하세요.)"
)


def claims_lookup(text, known=""):
    """True if a sentence of a reply that looked nothing up presents looked-up information.

    known: the lookup results earlier in the conversation; repeating a temperature from them is fine.
    """
    if FORECAST_TEMPERATURE.search(text):
        # every figure of the sentence counts: "영상 9도에서 15도" is a range
        if any(not re.search(rf"(?<!\d){value}\s?도", known) for value in re.findall(r"(\d+)\s?도", text)):
            return True  # a forecast figure that no lookup ever returned
    if not known and WEATHER_STATEMENT.search(text) and not text.rstrip().endswith("?"):
        return True  # weather described before anything was ever looked up
    # "말씀해 주시면 찾아볼게요" is an offer, not a claim
    return bool(LOOKUP_CLAIM.search(text)) and not re.search(r"(시면|다면|으면)\s", text)


AUTO_AVATAR_PROMPT = (
    "당신은 화면 속 아바타로, '{topic}'에 대해 상대와 음성으로 대담하고 있습니다. "
    "한국어 구어체로 서너 문장 정도로 충분히 풀어서 답하세요. 상대가 말을 끊으면 그 말에 바로 반응하세요. "
    "답변은 그대로 음성으로 읽히므로 마크다운, 목록, 이모지, 괄호 설명, 영어 단어는 쓰지 마세요."
)
AUTO_GUEST_PROMPT = (
    "당신은 '{topic}'에 대해 상대와 음성으로 대화하는 사람입니다. 호기심이 많고 솔직합니다. "
    "한국어 구어체로 한 번에 한두 문장만 말하세요. 상대의 말을 받아서 질문하거나 의견을 말해 대화를 이어가세요. "
    "말은 그대로 음성으로 읽히므로 마크다운, 목록, 이모지, 괄호 설명, 영어 단어는 쓰지 마세요."
)
AUTO_GUEST_LIVE = (
    " 대화 중간중간 화제를 바꿔서 지금 시각이나 오늘 날짜, 내일이나 주말의 날씨, 요즘 뉴스를 한 번에 하나씩 물어보세요."
)
AUTO_DECIDE_PROMPT = (
    "상대가 지금 말하는 중이고, 아직 끝나지 않았습니다.\n"
    "내가 방금 한 말: {question}\n"
    "지금까지 들린 상대의 말: {heard}\n\n"
    "지금 어떻게 반응할지 하나만 고르세요.\n"
    "- wait: 계속 듣는다.\n"
    "- backchannel: 듣고 있다는 짧은 추임새를 넣는다. text는 '네', '아 그렇군요', '오 정말요' 같은 여덟 글자 이내.\n"
    "- interrupt: 상대의 말을 끊고 끼어든다. text는 되묻거나 반박하거나 화제를 바꾸는 한 문장.\n"
    "성향: {tendency}\n"
    "JSON으로만 답하세요."
)
AUTO_DECIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["wait", "backchannel", "interrupt"]},
        "text": {"type": "string"},
    },
    "required": ["action", "text"],
}
# How eagerly the simulated user reacts while the avatar is talking:
# seconds between decisions, minimum seconds between backchannels, seconds of speech heard first.
AUTO_LEVELS = {
    "low": (4.0, 9.0, 6.0, "대부분 조용히 듣는다. 꼭 필요할 때만 반응한다."),
    "medium": (2.5, 5.0, 4.0, "가끔 추임새를 넣고, 궁금한 점이 생기면 끼어든다."),
    "high": (2.0, 3.5, 3.0, "추임새를 자주 넣고, 조금이라도 궁금하거나 말이 길어지면 바로 끼어든다."),
}


class Engine:
    """Owns the pipeline. Every method must be called from the single worker thread."""

    def __init__(self, args):
        self.args = args
        self.pipeline = get_pipeline(
            world_size=1,
            ckpt_dir=args.ckpt_dir,
            model_type=args.model_type,
            wav2vec_dir=args.wav2vec_dir,
        )
        params = get_infer_params()
        self.sample_rate = params["sample_rate"]
        self.fps = params["tgt_fps"]
        self.width, self.height = params["width"], params["height"]
        self.frame_num = params["frame_num"]
        self.motion_frames_num = params["motion_frames_num"]
        self.slice_frames = self.frame_num - self.motion_frames_num
        self.slice_samples = self.slice_frames * self.sample_rate // self.fps
        self.window_samples = self.sample_rate * params["cached_audio_duration"]
        self.audio_end_idx = params["cached_audio_duration"] * self.fps
        self.audio_start_idx = self.audio_end_idx - self.frame_num
        self.gate_threshold = None if args.gate_db is None else 10 ** (args.gate_db / 20)
        self.window = np.zeros(self.window_samples, dtype=np.float32)

        self.set_image(args.cond_image, use_face_crop=False)

        logger.info("Warming up (the first chunks trigger model compilation)...")
        silence = np.zeros(self.slice_samples, dtype=np.float32)
        for _ in range(3):
            self.step(silence)
        self.reset()
        logger.info("Warm-up done.")

    def set_image(self, path, use_face_crop):
        get_base_data(
            self.pipeline,
            cond_image_path_or_dir=path,
            base_seed=self.args.seed,
            use_face_crop=use_face_crop,
        )
        self.reset()

    def reset(self):
        """Start a new utterance: forget past audio and restart from the reference image."""
        self.pipeline.reset_person_name()
        self.window = np.zeros(self.window_samples, dtype=np.float32)

    def reference_jpeg(self):
        tensor = self.pipeline.cond_image_tensor_dict[self.pipeline.person_name]  # 1 C 1 H W, [-1, 1]
        image = ((tensor[0, :, 0].float().permute(1, 2, 0) + 1) * 127.5).clamp(0, 255)
        image = image.to(torch.uint8).cpu().numpy()
        return cv2.imencode(".jpg", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))[1].tobytes()

    def _gate(self, audio):
        """Zero out 20 ms frames below the threshold.

        Optional: for microphones whose background noise makes the mouth move.
        """
        if self.gate_threshold is None:
            return audio
        frames = audio.reshape(-1, GATE_FRAME)
        active = np.sqrt((frames ** 2).mean(axis=1)) > self.gate_threshold
        # keep 100 ms around active frames so word onsets and tails are not clipped
        active = np.convolve(active, np.ones(11), mode="same") > 0
        return (frames * active[:, None]).reshape(-1)

    def step(self, audio):
        """audio: float32 [-1, 1], exactly slice_samples long. Returns one JPEG per frame."""
        self.window = np.concatenate([self.window[len(audio):], self._gate(audio)])
        audio_embedding = get_audio_embedding(
            self.pipeline, self.window, self.audio_start_idx, self.audio_end_idx
        )
        video = run_pipeline(self.pipeline, audio_embedding)[self.motion_frames_num:]
        frames = video.clamp(0, 255).to(torch.uint8).cpu().numpy()  # T H W C, RGB
        quality = [cv2.IMWRITE_JPEG_QUALITY, self.args.jpeg_quality]
        return [
            cv2.imencode(".jpg", cv2.cvtColor(frame, cv2.COLOR_RGB2BGR), quality)[1].tobytes()
            for frame in frames
        ]


def split_for_tts(text, min_chars=40):
    """Split text into sentence groups that are synthesised one request at a time.

    Short sentences are merged with the next one: a group must play for longer than
    the following group takes to synthesise, or the avatar pauses between them.
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


class TTSClient:
    """Minimal client for `supertonic serve`."""

    def __init__(self, url, sample_rate):
        self.url = url.rstrip("/")
        self.sample_rate = sample_rate

    def voices(self):
        with urllib.request.urlopen(self.url + "/v1/styles", timeout=3) as response:
            return [style["name"] for style in json.load(response)["styles"]]

    def synthesize(self, text, voice, lang, speed):
        """Returns 16-bit mono PCM at the pipeline's sample rate."""
        body = {"text": text, "voice": voice, "lang": lang, "response_format": "wav"}
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
        return (np.clip(wav, -1, 1) * 32767).astype("<i2").tobytes()


DEFAULT_SYSTEM_PROMPT = (
    "당신은 화면 속 아바타로, 사용자와 음성으로 대화합니다. "
    "한국어 구어체로 한두 문장, 길어도 세 문장 이내로 짧게 답하세요. "
    "답변은 그대로 음성으로 읽히므로 마크다운, 목록, 이모지, 괄호 설명, 영어 단어는 쓰지 마세요."
)

DEFAULT_SYSTEM_PROMPT_EN = (
    "You are an avatar on a screen, talking with the user by voice. "
    "Reply only in natural spoken English, in one or two sentences, three at most, "
    "even when notes or reference material you are given are written in Korean. "
    "Your reply is read aloud as it is, so use no markdown, lists, emoji or parenthetical asides."
)
# Appended to a system prompt that is not the default one (the auto source's), to switch it to English.
ENGLISH_OVERRIDE = (
    "\nIMPORTANT: this conversation is in English. Disregard the instructions above about Korean and "
    "about avoiding English words: reply only in natural spoken English."
)
# Reply length (client setting "length"). "short" is what the system prompts ask for by themselves;
# the others are appended last, so they win over every "두세 문장" further up.
# Each entry: Korean instruction, English instruction, LLM output cap (tokens), LLM context (tokens).
REPLY_LENGTHS = {
    "short": (None, None, 1500, 8192),
    "normal": (
        "답변 길이: 위에 적힌 문장 수 제한 대신, 서너 문장으로 답하세요.",
        "Reply length: instead of the sentence limits above, answer in three or four sentences.",
        1500, 8192),
    "long": (
        "답변 길이: 위에 적힌 문장 수 제한 대신, 예닐곱 문장으로 이유나 예시를 들어 충분히 답하세요. "
        "목록처럼 나열하지 말고 말하듯 이어서 설명하세요.",
        "Reply length: instead of the sentence limits above, answer fully in six or seven sentences, with a "
        "reason or an example. Do not enumerate like a list; explain it the way you would say it.",
        1500, 8192),
    # a context this long is only asked for with this setting: Ollama reloads the model when it changes
    "explain": (
        "답변 길이: 위에 적힌 문장 수 제한 대신, 열 문장 이상으로 차근차근 자세히 설명하세요. "
        "배경, 핵심 내용, 예시 순서로 풀어 가되, 목록이나 번호 없이 말하듯 이어서 설명하세요.",
        "Reply length: instead of the sentence limits above, explain step by step in ten sentences or more: "
        "background, the main points, then an example. No lists or numbering; explain it the way you would say it.",
        2500, 16384),
}
# Said in a turn, these change the length of that one reply: "자세히 설명해줘", "짧게 말해줘".
ASK_LONGER = re.compile(r"(자세히|자세하게|상세히|상세하게|길게|구체적으로|깊이)|\b(in (more )?detail|at length|elaborate)\b", re.I)
ASK_SHORTER = re.compile(r"(짧게|간단히|간단하게|간략히|간략하게|한마디로|요약해)|\b(briefly|in short|keep it short)\b", re.I)


def reply_length(text, setting):
    """The length for one reply: the setting, unless the user asked for more or less in this turn."""
    setting = setting if setting in REPLY_LENGTHS else "short"
    if ASK_SHORTER.search(text):
        return "short"
    if ASK_LONGER.search(text):
        # one step up, but never into "explain" from below: that would reload the model mid-conversation
        return "explain" if setting == "explain" else "long"
    return setting


LANGUAGES = ("ko", "en")
HANGUL = re.compile("[\uac00-\ud7a3]")


def text_language(text):
    """"ko" if the text has any Hangul, else "en"."""
    return "ko" if HANGUL.search(text) else "en"


# Characters a TTS would read aloud or stumble on: markdown markers and emoji.
UNSPEAKABLE = re.compile("[*#`_~>|\\[\\]\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")
PIECE_END = re.compile(r"[.!?。！？](?=\s)|\n")


CLAUSE_END = re.compile(r"[,，](?=\s)")
# Where a long first sentence with no comma may still be cut: after a connecting ending ("가서", "내리쬐고",
# "대화하며", "좋은데"). Measured first sentences ran to 30-45 characters without a comma (2026-11-23).
LINK_END = re.compile(r"(?<=[가-힣])(고|서|며|면|면서|는데|은데|인데|지만|니까|어서|아서|해서|라서|도록)(?=\s)")
FIRST_SENTENCE_CHARS = 5     # the first piece may end at a sentence end this early ("안녕하세요!") ...
FIRST_CLAUSE_CHARS = 10      # ... or at a comma: synthesis takes about 0.45 s + 0.02 s a character (2026-11-23)


def take_piece(buffer, min_chars, first=False):
    """Cut a speakable piece off the front of streamed LLM text, ending at a sentence boundary.

    first: the reply's first piece, which the avatar waits for. It may be shorter, and may end at a comma: it is
    synthesised sooner, and the rest is ready before it has been said."""
    if first:
        for match in PIECE_END.finditer(buffer):
            if len(buffer[:match.end()].strip()) >= FIRST_SENTENCE_CHARS:
                return buffer[:match.end()], buffer[match.end():]
        for match in CLAUSE_END.finditer(buffer):
            if len(buffer[:match.end()].strip()) >= FIRST_CLAUSE_CHARS:
                return buffer[:match.end()], buffer[match.end():]
        for match in LINK_END.finditer(buffer):
            if len(buffer[:match.end()].strip()) >= FIRST_CLAUSE_CHARS:
                return buffer[:match.end()], buffer[match.end():]
        return None, buffer
    for match in PIECE_END.finditer(buffer):
        if len(buffer[:match.end()].strip()) >= min_chars:
            return buffer[:match.end()], buffer[match.end():]
    return None, buffer


class SmartTurn:
    """End-of-turn detection from the sound of the speech (pipecat-ai/smart-turn v3.2, ONNX on the CPU).

    Given the last 8 seconds of an utterance it returns the probability that the speaker has
    finished, judged from intonation and phrasing rather than from how long the silence is."""

    SECONDS = 8

    def __init__(self, path, sample_rate):
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 2
        self.session = ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])
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


class Endpointer:
    """Splits a 16 kHz PCM stream into utterances with Silero VAD.

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

    def __init__(self, sample_rate):
        from silero_vad import load_silero_vad
        self.sample_rate = sample_rate
        self.min_voiced = self.MIN_VOICED   # lowered by the session while it waits for a short answer
        self.vad = load_silero_vad()
        self.end_windows = int(self.END_SILENCE * sample_rate / self.WINDOW)
        self.pause_windows = int(self.PAUSE_SILENCE * sample_rate / self.WINDOW)
        self.max_windows = int(self.MAX_LENGTH * sample_rate / self.WINDOW)
        self.judge = None          # set by the session: audio -> probability that the turn is over
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
            with torch.no_grad():
                prob = float(self.vad(torch.from_numpy(window.astype(np.float32) / 32768.0), self.sample_rate))
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
                        self.close_windows = int(seconds * self.sample_rate / self.WINDOW)
                    except Exception as e:  # never let the judge stop the microphone
                        logger.warning(f"End-of-turn model failed: {e}")
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


class SpeechToText:
    def __init__(self, model_name, language):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(model_name, device="cuda", compute_type="float16")
        self.language = language

    def transcribe(self, audio, language=None):
        """Returns (text, language). language: "ko" / "en", or "auto" to pick between the two."""
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
        kept = [s for s in segments if s.no_speech_prob < 0.6]
        weights = [max(len(s.text.strip()), 1) for s in kept]
        # read by is_hallucination(): "감사합니다." with low confidence is Whisper's, not the user's
        self.last_logprob = (sum(float(s.avg_logprob) * w for s, w in zip(kept, weights)) / sum(weights)
                             if kept else None)
        return "".join(s.text for s in kept).strip(), language


class RemoteSpeechToText:
    """Speech recognition by realtime_asr_server.py (Qwen3-ASR), behind the same call as SpeechToText."""

    def __init__(self, url):
        self.url = url.rstrip("/")

    def name(self):
        """The model the server runs, or None if it is not reachable."""
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=1.5) as response:
                return json.load(response).get("model")
        except Exception:
            return None

    def transcribe(self, audio, language=None):
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        request = urllib.request.Request(
            f"{self.url}/transcribe?language={language or 'ko'}", data=pcm,
            headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
        return result["text"], result["language"]


class OllamaClient:
    def __init__(self, url, model):
        self.url = url.rstrip("/")
        self.model = model
        self._capabilities = {}  # model name -> what Ollama says it can do

    async def supports(self, model, capability):
        """capability: "thinking" (has a thinking mode) or "tools" (can call functions)."""
        model = model or self.model
        if model not in self._capabilities:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    response = await client.post(self.url + "/api/show", json={"model": model})
                    self._capabilities[model] = response.json().get("capabilities", [])
            except Exception:
                return False
        return capability in self._capabilities[model]

    async def has_thinking(self, model):
        return await self.supports(model, "thinking")

    async def models(self):
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(self.url + "/api/tags")
            response.raise_for_status()
            return [m["name"] for m in response.json()["models"]]

    async def complete(self, messages, model=None, schema=None):
        """Returns a whole reply at once; with `schema`, the reply is JSON matching it."""
        model = model or self.model
        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": "30m",
            "options": {"num_predict": 300, "num_ctx": 8192},
        }
        if schema:
            body["format"] = schema
        if await self.has_thinking(model):
            body["think"] = False
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5)) as client:
            response = await client.post(self.url + "/api/chat", json=body)
            if response.status_code != 200:
                raise RuntimeError(response.text[:200])
            return response.json().get("message", {}).get("content", "")

    async def stream(self, messages, model=None, tools=None, num_predict=1500, num_ctx=8192):
        """Yields ("text", delta) as the reply is generated (thinking output is skipped) and
        ("tool", {"name": ..., "arguments": {...}}) for each function the model wants called."""
        model = model or self.model
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": "30m",
            # num_predict: generous, thinking models spend several hundred tokens before answering.
            # num_ctx: Ollama otherwise reserves the model's full context (262k for Gemma 4),
            # which costs tens of GB of VRAM; a spoken chat of 20 short messages needs far less.
            "options": {"num_predict": num_predict, "num_ctx": num_ctx},
        }
        if tools:
            body["tools"] = tools
        if await self.has_thinking(model):
            body["think"] = False  # a spoken reply cannot wait for a thinking phase
        timeout = httpx.Timeout(120, connect=5)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", self.url + "/api/chat", json=body) as response:
                if response.status_code != 200:
                    raise RuntimeError((await response.aread()).decode(errors="replace")[:200])
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    message = data.get("message", {})
                    if message.get("content"):
                        yield "text", message["content"]
                    for call in message.get("tool_calls") or []:
                        function = call.get("function") or {}
                        if function.get("name"):
                            yield "tool", {"name": function["name"], "arguments": function.get("arguments") or {}}
                    if data.get("done"):
                        total = data.get("total_duration", 0) / 1e9
                        if total > 2.5:
                            # say where a slow reply spent its time: (re)loading, reading the prompt, or writing
                            logger.info(
                                f"Slow LLM pass {total:.1f}s: load {data.get('load_duration', 0) / 1e9:.1f}s, "
                                f"prompt {data.get('prompt_eval_count', 0)} tok "
                                f"{data.get('prompt_eval_duration', 0) / 1e9:.1f}s, "
                                f"output {data.get('eval_count', 0)} tok {data.get('eval_duration', 0) / 1e9:.1f}s")
                        break


KNOWLEDGE_TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "list_knowledge_by_date",
        "description": "지식베이스에 특정 날짜에 추가된 자료의 목록(제목과 요약)을 가져온다. "
                       "'오늘 들어온 자료', '어제 추가된 것', '11월 13일에 넣은 자료', '최근에 추가된 자료'처럼 "
                       "추가된 날짜나 최신 여부로 자료를 물으면 사용한다. 내용에 대한 질문에는 쓰지 않는다.",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string",
                         "description": "YYYY-MM-DD 형식의 날짜. 예: 2026-11-16. "
                                        "날짜 없이 가장 최근에 추가된 자료를 원하면 빈 문자열."},
            },
            "required": ["date"],
        },
    }},
]
# knowledge_mode "tool": the model searches the knowledge base itself, with a query of its own, when it is what the
# user wants (2026-11-20: searching the user's words before every reply answered "자동차 관련 뉴스" from a
# stored document and searched "자세히 알아봐 줘" as it was)
SEARCH_KNOWLEDGE_SPEC = {"type": "function", "function": {
    "name": "search_knowledge",
    "description": "지식베이스에 저장된 기술 동향 자료를 내용으로 찾는다. 사용자가 지식베이스나 저장된 자료를 찾아 달라고 하거나, "
                   "앞서 지식베이스에서 소개한 자료를 더 묻거나, 뉴스·인터넷이 아니라 정리된 기술 자료가 필요한 질문일 때 쓴다. "
                   "최신 뉴스, 인터넷 검색, 날씨를 원하면 쓰지 않는다. 날짜로 자료 목록을 볼 때는 list_knowledge_by_date를 쓴다.",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "찾을 내용을 담은 검색어. 사용자의 말을 그대로 넣지 말고 대화의 맥락에서 "
                                                   "주제를 정한다. 예: \"LongCat-Video 영상 생성 모델\""}},
        "required": ["query"]}}}
KNOWLEDGE_TOOLS = {spec["function"]["name"] for spec in KNOWLEDGE_TOOL_SPECS} | {"search_knowledge"}
# The user names the knowledge base: search it before the reply in either mode.
ASKS_KNOWLEDGE = re.compile(r"지식\s?베이스|지식에서|자료에서|저장(된|해 둔|한)\s?(글|자료)|등록(된|한)\s?(글|자료)|"
                            r"(추가|등록|저장)(된|한)\s?(지식|자료)|(새로|오늘|어제|최근)\s?(들어온|추가된|올라온|넣은)\s?(지식|자료)|"
                            r"knowledge base", re.I)
# The user asks for another source: no knowledge-base material, and no search_knowledge for that turn.
ASKS_ELSEWHERE = re.compile(r"인터넷|웹에서|온라인|구글|뉴스|기사|위키|긱\s?뉴스|날씨|\bweb\b|internet|news|wikipedia",
                            re.I)


def knowledge_first(text, mode):
    """Whether this turn searches the knowledge base before the reply (v2: runtime/knowledge.py)."""
    if ASKS_KNOWLEDGE.search(text or ""):
        return True
    if mode == "tool" or ASKS_ELSEWHERE.search(text or ""):
        return False
    return True


SITUATION_TURNS_BACK = 4
SITUATION_ITEM = re.compile(r"^\s*(?:\(?\d{1,2}[.)]|[-•·]|\[\d{1,2}\])\s*(.+)$")


def situation_note(history):
    """Where the conversation stands, from the history (v2: runtime/situation.py): the last lookup with its
    first items and the question last asked. Goes in front of the user's words, so that "자세히 알아봐 줘",
    "그렇게 해줘" or "좋아" are read as continuing it."""
    lookup, results, turns_back, asked, seen_reply = None, [], 0, "", False
    for index in range(len(history) - 1, 0, -1):
        message = history[index]
        role = message.get("role")
        if role == "user" and not str(message.get("content") or "").lstrip().startswith("("):
            turns_back += 1
            if turns_back > SITUATION_TURNS_BACK:
                break
        elif role == "assistant" and message.get("content") and not seen_reply:
            seen_reply = True
            parts = [p for p in re.split(r"(?<=[.?!])\s+", str(message["content"]).strip()) if p]
            if parts and parts[-1].endswith(("?", "？")):
                asked = parts[-1][:120]
        elif role == "assistant" and message.get("tool_calls") and lookup is None:
            calls = [c.get("function", c) for c in message["tool_calls"]]
            if calls:
                lookup = calls
                results = [m.get("content") for m in history[index + 1:index + 1 + len(calls)] if m.get("role") == "tool"]
        if lookup is not None and seen_reply:
            break
    if not lookup and not asked:
        return ""
    lines = []
    if lookup:
        def args(a):
            return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in (a or {}).items() if v not in (None, ""))
        items = []
        for result in results:
            for raw in str(result or "").splitlines():
                found = SITUATION_ITEM.match(raw)
                if found and len(items) < 5:
                    items.append(re.sub(r"\s+", " ", found.group(1)).strip()[:70])
        done = "; ".join(f"{c.get('name')}({args(c.get('arguments'))})" for c in lookup)
        lines.append(f"- 최근에 한 조회: {done}" + (" → 결과: " + " / ".join(items) if items else ""))
    if asked:
        lines.append(f"- 직전에 사용자에게 물은 것: \"{asked}\"")
    return ("(지금 대화의 흐름입니다. 사용자의 말을 이 흐름 안에서 이해하세요. '자세히', '그거', '그렇게 해줘', '좋아'처럼 "
            "짧은 말은 대개 이 흐름을 잇는 말입니다.)\n" + "\n".join(lines))
KNOWLEDGE_FILLER = "자료를 확인해 볼게요."
WEEKDAY_NAMES = "월화수목금토일"


class KnowledgeClient:
    """Retrieval from a WeKnora knowledge base: POST /knowledge-search returns the best-matching
    chunks (vector + keyword recall, reranked) without any LLM pass, so it is fast enough to run
    before every reply. The chunks go into the prompt of the avatar's own LLM."""

    MAX_HITS = 3          # chunks put into the prompt
    SUMMARY_CHARS = 600   # WeKnora's per-document summaries are about 300 characters
    TEXT_CHARS = 900      # body chunks run to 4000 characters; the 8192-token context cannot take many

    def __init__(self, url, key_file, kb_id, name, timeout):
        self.url = url.rstrip("/")
        self.kb_id = kb_id
        self.name = name
        self.timeout = timeout
        self.key = None
        self.error = None
        try:
            with open(os.path.expanduser(key_file)) as f:
                self.key = f.read().strip() or None
            if not self.key:
                self.error = f"{key_file} is empty"
        except OSError as e:
            self.error = str(e)
        self._client = None

    @property
    def available(self):
        return bool(self.key and self.kb_id)

    async def search(self, query, timeout=None):
        """Returns (hits, ms); hits are dicts with score, title, content and kind, best first."""
        if self._client is None:
            self._client = httpx.AsyncClient(headers={"X-API-Key": self.key})
        started = time.perf_counter()
        response = await self._client.post(
            self.url + "/knowledge-search",
            json={"query": query, "knowledge_base_id": self.kb_id, "match_count": 5},
            timeout=timeout or self.timeout)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:120]}")
        hits = [{"score": float(item.get("score") or 0), "title": item.get("knowledge_title") or "",
                 "content": item.get("content") or "", "kind": item.get("chunk_type") or "text"}
                for item in response.json().get("data") or []]
        hits.sort(key=lambda hit: -hit["score"])
        return hits, (time.perf_counter() - started) * 1000

    LIST_ITEMS = 5         # documents described in one tool result
    DESCRIPTION_CHARS = 260

    def guide(self, now, with_date):
        """System prompt text for the list tool. with_date: nothing else has told the model the date."""
        days = ", ".join(
            f"{(now - timedelta(days=i)).strftime('%Y-%m-%d')}"
            f"({WEEKDAY_NAMES[(now - timedelta(days=i)).weekday()]}요일{', ' + label if label else ''})"
            for i, label in enumerate(["오늘", "어제", "그저께", "", "", "", "", ""]))
        return (
            (f"오늘은 {now.year}년 {now.month}월 {now.day}일입니다. " if with_date else "")
            + f"'{self.name}' 지식베이스에 자료가 추가된 날짜로 묻는 말(오늘, 어제, 특정 날짜, 최근에 추가된 자료)에는 "
            "추측하지 말고 가장 먼저 list_knowledge_by_date 도구를 호출하세요. "
            f"최근 날짜: {days}. "
            "결과를 말할 때는 몇 건인지 먼저 말하고, 두세 건만 골라 무엇에 관한 자료인지 한 문장씩 풀어서 소개하세요. "
            "제목과 주소는 그대로 읽지 마세요. 더 있으면 더 들을지 물어보세요."
        )

    async def list_by_date(self, date, tz):
        """Tool body. Returns the text for the LLM; raises ValueError with a message it should relay."""
        date = str(date or "").strip()
        day = None
        if date:
            try:
                day = datetime.strptime(date[:10], "%Y-%m-%d").date()
            except ValueError:
                raise ValueError(f"날짜 '{date}'를 이해하지 못했습니다. YYYY-MM-DD 형식으로 다시 호출하세요.")
        if self._client is None:
            self._client = httpx.AsyncClient(headers={"X-API-Key": self.key})
        items, page = [], 1
        while True:
            response = await self._client.get(
                f"{self.url}/knowledge-bases/{self.kb_id}/knowledge",
                params={"page": page, "page_size": 100, "sort_by": "created_at", "sort_order": "desc"},
                timeout=self.timeout)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            body = response.json()
            batch = body.get("data") or []
            for item in batch:
                try:
                    # Python 3.10 only reads fractions of exactly 3 or 6 digits; WeKnora drops trailing zeros
                    stamp = re.sub(r"\.\d+", "", str(item.get("created_at"))).replace("Z", "+00:00")
                    created = datetime.fromisoformat(stamp).astimezone(tz)
                except ValueError:
                    continue
                items.append((created, item.get("title") or item.get("file_name") or "제목 없음",
                              re.sub(r"\s+", " ", item.get("description") or "").strip()))
            # newest first: once a page ends before the wanted day, nothing older is needed
            if not batch or len(items) >= int(body.get("total") or 0) or page >= 5 \
                    or (day and items and items[-1][0].date() < day):
                break
            page += 1
        if not items:
            return "지식베이스에 자료가 없습니다."

        def spoken(d):
            return f"{d.month}월 {d.day}일 {WEEKDAY_NAMES[d.weekday()]}요일"

        def describe(chosen):
            return "\n".join(
                f"{i}. 제목: {title}\n   추가 시각: {created.month}월 {created.day}일 {created.hour}시 {created.minute}분\n"
                f"   요약: {summary[:self.DESCRIPTION_CHARS] or '요약 없음'}"
                for i, (created, title, summary) in enumerate(chosen, 1))

        if day is None:
            chosen = items[:self.LIST_ITEMS]
            return f"가장 최근에 추가된 자료 {len(chosen)}건 (전체 {len(items)}건 중):\n" + describe(chosen)
        matched = [item for item in items if item[0].date() == day]
        if matched:
            more = (f" 이 중 최근 {self.LIST_ITEMS}건만 적습니다." if len(matched) > self.LIST_ITEMS else "")
            return f"{spoken(day)}에 추가된 자료는 {len(matched)}건입니다.{more}\n" + describe(matched[:self.LIST_ITEMS])
        counts = {}
        for created, _, _ in items:
            counts[created.date()] = counts.get(created.date(), 0) + 1
        near = sorted(counts, key=lambda d: abs((d - day).days))[:3]
        return (f"{spoken(day)}에 추가된 자료는 없습니다. 자료가 추가된 가까운 날짜: "
                + ", ".join(f"{spoken(d)} {counts[d]}건" for d in sorted(near)) + ".")

    async def run(self, name, arguments, tz):
        """Same contract as Toolbox.run: (text for the LLM, succeeded, milliseconds)."""
        started = time.perf_counter()
        arguments = arguments if isinstance(arguments, dict) else {}
        try:
            if name == "search_knowledge":
                text, ok = await asyncio.wait_for(self.search_text(arguments.get("query")), 4.0), True
            else:
                text, ok = await asyncio.wait_for(self.list_by_date(arguments.get("date"), tz), 4.0), True
        except ValueError as e:
            text, ok = str(e), False
        except asyncio.TimeoutError:
            text, ok = "조회 실패: 시간이 너무 오래 걸려 중단했습니다.", False
        except Exception as e:
            text, ok = f"조회 실패: {type(e).__name__}", False
        return text, ok, (time.perf_counter() - started) * 1000

    async def search_text(self, query, threshold=0.4):
        """search_knowledge: the reference text for the model's own query, or a line saying there is none."""
        query = str(query or "").strip()
        if not query:
            raise ValueError("검색어가 없습니다. 무엇을 찾을지 검색어를 정해 다시 호출하세요.")
        hits, _ = await self.search(query)
        block, _ = self.context(hits, threshold)
        return block or (f"지식베이스에서 '{query}'와 관련된 자료를 찾지 못했습니다. 지식베이스에 없다고 말하고, "
                         "필요하면 다른 도구를 쓰세요.")

    def context(self, hits, threshold):
        """Returns (prompt text, titles used) for the hits that score at least `threshold`."""
        used = [hit for hit in hits if hit["score"] >= threshold][:self.MAX_HITS]
        if not used:
            return "", []
        parts, titles = [], []
        for index, hit in enumerate(used, 1):
            limit = self.SUMMARY_CHARS if hit["kind"] == "summary" else self.TEXT_CHARS
            body = re.sub(r"\s+", " ", hit["content"]).strip()[:limit]
            parts.append(f"({index}) 문서: {hit['title']}\n{body}")
            if hit["title"] not in titles:
                titles.append(hit["title"])
        text = (
            f"[참고 자료 시작: '{self.name}' 지식베이스에서 찾은 내용입니다. 질문과 관련이 있으면 이 내용을 근거로 답하고, "
            "관련이 없으면 무시하세요. 날씨나 뉴스처럼 지금의 소식을 묻는 말이면 이 자료가 아니라 도구로 조회하세요. "
            "다른 곳(긱뉴스, 위키백과 등)을 찾아봤는데 없었다면, 없었다고만 하지 말고 이 지식베이스에는 "
            "이런 자료가 있다고 내용을 함께 알려 주세요. "
            "자료에 없는 사실은 지어내지 마세요. 링크나 문서 번호는 읽지 말고 "
            "내용을 말로 풀어서 설명하세요.]\n" + "\n\n".join(parts) + "\n[참고 자료 끝]")
        return text, titles


def create_app(args):
    app = FastAPI()
    # One thread owns the GPU pipeline, so inference and image changes never overlap.
    pool = ThreadPoolExecutor(max_workers=1)
    engine = pool.submit(Engine, args).result()
    state = {"busy": False}
    tts = TTSClient(args.tts_url, engine.sample_rate)
    tts_pool = ThreadPoolExecutor(max_workers=1)
    llm = OllamaClient(args.llm_url, args.llm_model)
    toolbox = Toolbox(args.location, args.timezone, args.tool_timeout)
    knowledge = KnowledgeClient(args.weknora_url, args.weknora_key_file, args.weknora_kb,
                                args.knowledge_name, args.knowledge_timeout)
    # the names the user may call the knowledge base by, compared without spaces
    knowledge_aliases = {re.sub(r"\s+", "", name.lower()) for name in [args.knowledge_name, *args.knowledge_aliases.split(",")]
                         if name.strip()}
    if knowledge.available:
        logger.info(f"Knowledge search ready ({args.weknora_url}, '{args.knowledge_name}').")

        def warm_knowledge():
            # WeKnora unloads its embedding model after five idle minutes and then needs 3.5-5 s for the
            # next search, which the reply does not wait for. Keeping it loaded for as long as the app
            # runs (not only while a session has the knowledge setting on) avoids that "시간 초과".
            while True:
                try:
                    request = urllib.request.Request(
                        knowledge.url + "/knowledge-search",
                        headers={"X-API-Key": knowledge.key, "Content-Type": "application/json"},
                        data=json.dumps({"query": "warm up", "knowledge_base_id": knowledge.kb_id,
                                         "match_count": 1}).encode())
                    urllib.request.urlopen(request, timeout=30).read()
                except Exception as e:
                    logger.warning(f"Knowledge warm-up failed: {e!r}")
                time.sleep(240)

        import threading
        threading.Thread(target=warm_knowledge, name="knowledge-warmer", daemon=True).start()
    else:
        logger.info(f"Knowledge search off: {knowledge.error or 'no knowledge base id'}")
    filler_cache = {}  # (voice, text) -> PCM of a "one moment" line, ready to play at once
    stt_pool = ThreadPoolExecutor(max_workers=1)
    try:
        stt = stt_pool.submit(SpeechToText, args.stt_model, args.stt_lang).result()
        stt_error = None
        logger.info(f"Speech recognition ready ({args.stt_model}).")
    except Exception as e:
        stt, stt_error = None, str(e)
        logger.warning(f"Speech recognition unavailable: {e}")
    remote_stt = RemoteSpeechToText(args.asr_url)
    logger.info(f"Second speech recognizer at {args.asr_url}: {remote_stt.name() or 'not running'}")
    try:
        smart_turn = SmartTurn(args.smart_turn_model, engine.sample_rate)
        smart_turn.probability(np.zeros(engine.sample_rate, dtype=np.float32))  # first call loads the graph
        logger.info(f"End-of-turn model ready ({args.smart_turn_model}).")
    except Exception as e:
        smart_turn = None
        logger.info(f"End-of-turn model off: {e}")
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "realtime_mic_client.html")

    async def on_worker(fn, *a):
        return await asyncio.get_running_loop().run_in_executor(pool, fn, *a)

    @app.get("/")
    async def index():
        return FileResponse(html_path, media_type="text/html")

    @app.get("/api/image/current")
    async def current_image():
        return Response(await on_worker(engine.reference_jpeg), media_type="image/jpeg")

    @app.post("/api/image")
    async def set_image(file: UploadFile = File(...), face_crop: bool = Form(True)):
        try:
            image = Image.open(io.BytesIO(await file.read())).convert("RGB")
        except Exception as e:
            return JSONResponse({"ok": False, "message": f"Cannot read image: {e}"}, status_code=400)
        os.makedirs(args.upload_dir, exist_ok=True)
        path = os.path.join(args.upload_dir, f"ref_{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.png")
        image.save(path)
        await on_worker(engine.set_image, path, face_crop)
        logger.info(f"Reference image set to {path} (face_crop={face_crop})")
        return {"ok": True}

    @app.get("/api/tts")
    async def tts_info():
        try:
            voices = await asyncio.get_running_loop().run_in_executor(tts_pool, tts.voices)
            return {"available": True, "voices": voices, "default_voice": args.tts_voice}
        except Exception as e:
            return {"available": False, "message": f"TTS 서버({args.tts_url})에 연결할 수 없습니다: {e}"}

    @app.get("/api/llm")
    async def llm_info():
        try:
            return {"available": True, "models": await llm.models(), "default_model": args.llm_model,
                    "location": args.location,
                    "knowledge": {"available": knowledge.available, "name": knowledge.name},
                    "smart_turn": smart_turn is not None,
                    "stt_engines": {"whisper": args.stt_model.split("/")[-1], "qwen": remote_stt.name()}}
        except Exception as e:
            return {"available": False, "message": f"LLM 서버({args.llm_url})에 연결할 수 없습니다: {e}"}

    async def run_mic(incoming, send_chunk):
        slice_bytes = engine.slice_samples * 2
        pending = bytearray()
        start_sample = 0
        dropped_samples = 0
        closed = False
        while not closed:
            message = await incoming.get()
            if message is None:
                break
            pending.extend(message.get("bytes") or b"")
            while True:
                while not incoming.empty():
                    message = incoming.get_nowait()
                    if message is None:
                        closed = True
                        break
                    pending.extend(message.get("bytes") or b"")
                if closed or len(pending) < slice_bytes:
                    break

                # If generation cannot keep up, skip old audio so the delay stays bounded.
                backlog = len(pending) // slice_bytes
                if backlog > args.max_backlog:
                    drop = (backlog - 1) * slice_bytes
                    del pending[:drop]
                    start_sample += drop // 2
                    dropped_samples += drop // 2

                pcm = bytes(pending[:slice_bytes])
                del pending[:slice_bytes]
                await send_chunk(pcm, start_sample, dropped_samples)
                start_sample += engine.slice_samples

    async def run_tts(send_json, send_bytes, incoming, send_chunk, session, source):
        """Runs a continuous stream on a real-time clock: silence (idle motion) unless speech is queued.

        Speech comes from "speak" requests (text read as is) or "chat" requests (an LLM reply).
        """
        loop = asyncio.get_running_loop()
        slice_bytes = engine.slice_samples * 2
        duration = engine.slice_frames / engine.fps
        sentence_gap = b"\x00" * (int(0.3 * engine.sample_rate) * 2)
        # a piece cut inside a sentence (a first piece ending at a comma or a connecting ending) goes on almost at once
        clause_gap = b"\x00" * (int(0.06 * engine.sample_rate) * 2)
        comma_gap = b"\x00" * (int(0.15 * engine.sample_rate) * 2)
        speech = bytearray()   # synthesised PCM not yet sent
        marks = []             # where each request's speech starts inside `speech`
        requests = asyncio.Queue()
        generation = {"n": 0}  # bumped to cancel queued and in-flight speech
        history = [{"role": "system", "content": args.llm_system}]
        chat_state = {"task": None}
        synth_state = {"busy": False}
        settings = {"voice": None, "model": None, "barge_in": False, "interrupt_mode": "basic",
                    "speculative": False, "tools": False,
                    # knowledge: search the WeKnora knowledge base before each reply
                    # knowledge_threshold: rerank score a chunk needs to be put into the prompt
                    "knowledge": False, "knowledge_threshold": 0.4,
                    # tool: the model calls search_knowledge when it needs it; before_reply: v1's search of the
                    # user's words before every reply (a turn asking for the internet or news skips it in both)
                    "knowledge_mode": "tool",
                    # language: "ko", "en", or "auto" (each turn in the language the user used)
                    "language": "ko",
                    # length: how long replies are, see REPLY_LENGTHS
                    "length": "short",
                    # endpoint: "silence" (0.7 s of silence ends an utterance) or "smart" (SmartTurn decides
                    # whether to wait 0.35, 0.7 or 2 s)
                    "endpoint": "silence",
                    # stt: "whisper" (in this process) or "qwen" (realtime_asr_server.py)
                    "stt": "whisper"}

        def recognizer():
            return remote_stt if settings["stt"] == "qwen" else stt

        def stt_language():
            return settings["language"] if settings["language"] in (*LANGUAGES, "auto") else args.stt_lang

        def speech_language(text):
            """The TTS language for one sentence."""
            if settings["language"] in LANGUAGES:
                return settings["language"]
            return text_language(text) if settings["language"] == "auto" else args.tts_lang
        # A speculative reply: prepared during a pause, not audible until committed.
        hold = {"on": False}   # while on, synthesised speech stays in `speech`
        spec = {"task": None, "text": None, "request": None, "outbox": [], "committed": False}
        # Bookkeeping for "unmute" interrupts. Positions are byte offsets into the whole
        # speech stream of this session (sent or dropped), so they survive `speech` being cut.
        stream = {"base": 0, "in_flight": False, "idle": True}
        chunk_speech = {}      # chunk seq -> (stream position of its first speech byte, speech bytes in it)
        # pieces: spoken text + stream range; queued/synthesised/llm_done tell whether more was coming
        last_reply = {"message": None, "id": None, "pieces": [], "queued": 0, "synthesised": 0, "llm_done": False,
                      "tool_entries": []}
        # pending: asyncio.Event set once the browser has reported playback
        # reply: the interrupted reply as it stood when it was cut off (a later reply must not be touched)
        truncation = {"pending": None, "reply": None}
        kick = asyncio.Event()  # wakes the main loop to send an idle chunk right after a flush
        background = set()      # fire-and-forget tasks, referenced so they are not collected mid-run
        knowledge_warmer = {"task": None}
        knowledge_memory = {"reference": ""}  # the knowledge-base text given to the latest topical turn
        # voice source: microphone -> utterances -> text
        endpointer = Endpointer(engine.sample_rate) if source in ("voice", "auto") else None
        listen = {"transcribing": False, "busy_until": 0.0, "state": None, "turn": 0, "over_avatar": False}
        if endpointer and stt is None:
            await send_json({"type": "notice", "message": f"음성 인식을 쓸 수 없습니다: {stt_error}"})

        async def synthesizer():
            while True:
                gen, request = await requests.get()
                synth_state["busy"] = True
                first = True
                for sentence in split_for_tts(str(request.get("text", ""))):
                    if gen != generation["n"]:
                        break
                    started = time.perf_counter()
                    voice = request.get("voice") or args.tts_voice
                    shown = not hold["on"]  # a reply prepared in secret stays off the page until committed
                    if shown:
                        await send_json({"type": "tts", "id": request.get("id"), "state": "start",
                                         "chars": len(sentence), "filler": bool(request.get("filler"))})
                    try:
                        pcm = filler_cache.get((voice, sentence)) if request.get("filler") else None
                        if pcm is None:
                            pcm = await loop.run_in_executor(
                                tts_pool, tts.synthesize, sentence, voice,
                                request.get("lang") or speech_language(sentence), request.get("speed"),
                            )
                            if request.get("filler"):
                                filler_cache[(voice, sentence)] = pcm
                    except Exception as e:
                        logger.warning(f"TTS failed: {e}")
                        await send_json({"type": "notice", "message": f"TTS 실패: {e}"})
                        break
                    if gen != generation["n"]:
                        break
                    if shown:
                        await send_json({"type": "tts", "id": request.get("id"), "state": "done",
                                         "ms": (time.perf_counter() - started) * 1000,
                                         "seconds": len(pcm) / 2 / engine.sample_rate,
                                         "filler": bool(request.get("filler"))})
                    if first and request.get("phase"):
                        marks.append({
                            "phase": request["phase"],
                            "pos": len(speech),
                            "id": request.get("id"),
                            "synth_ms": (time.perf_counter() - started) * 1000,
                            "llm_ms": request.get("llm_ms"),
                            "stt_ms": request.get("stt_ms"),
                            "utterance_end": request.get("utterance_end"),
                            "speculative": bool(request.get("speculative")),
                            "tool_ms": request.get("tool_ms"),
                        })
                    first = False
                    if request.get("reply") is last_reply["message"] and last_reply["message"] is not None:
                        start = stream["base"] + len(speech)
                        last_reply["pieces"].append({"text": sentence, "start": start, "end": start + len(pcm)})
                    speech.extend(pcm)
                    speech.extend(sentence_gap if re.search(r"[.!?。！？…~]\s*$", sentence)
                                  else comma_gap if re.search(r"[,，]\s*$", sentence) else clause_gap)
                    if stream.get("idle") and not hold["on"]:
                        # The avatar is resting: make the next chunk now instead of on the next beat. The beat
                        # wait was 0.4 s of the 1.7 s from text on screen to sound (measured 2026-11-23); the
                        # page cuts the resting chunk it is playing when this one comes (realtime_mic_client).
                        stream["idle"] = False
                        kick.set()
                if request.get("reply") is last_reply["message"] and last_reply["message"] is not None:
                    last_reply["synthesised"] += 1
                synth_state["busy"] = requests.qsize() > 0

        async def cancel_speech():
            if spec["task"] is not None:
                await discard_speculation()  # a reply still being prepared in secret goes too
            generation["n"] += 1
            stream["base"] += len(speech)
            speech.clear()
            marks.clear()
            while not requests.empty():
                requests.get_nowait()
            if chat_state["task"]:
                chat_state["task"].cancel()
                chat_state["task"] = None
            if settings["interrupt_mode"].startswith("unmute"):
                # Everything up to the chunk being generated right now is stale for the browser.
                first_fresh = session["seq"] + (1 if stream["in_flight"] else 0)
                if last_reply["message"] is not None:
                    truncation["reply"] = dict(last_reply)
                    last_reply["message"] = None
                truncation["pending"] = asyncio.Event()
                await send_json({"type": "interrupt", "seq": first_fresh})
                listen["busy_until"] = loop.time() + 0.3
                auto["avatar_end_at"] = loop.time()  # the browser drops what it had buffered
                kick.set()

        def apply_playback(seq, offset):
            """Cut the interrupted reply down to what the browser actually played.

            Returns (request id, text heard), or (None, None) if there is nothing to cut."""
            cut, truncation["reply"] = truncation["reply"], None
            if cut is None:
                return None, None
            reply, pieces = cut["message"], cut["pieces"]
            if seq in chunk_speech:
                start, length = chunk_speech[seq]
                played = start + min(int(offset * engine.sample_rate) * 2, length)
            else:
                played = -1  # nothing of this reply reached the speakers
            logger.debug(f"playback report: seq={seq} offset={offset:.2f} chunk={chunk_speech.get(seq)} "
                         f"played={played} base={stream['base']} "
                         f"pieces={[(p['start'], p['end']) for p in pieces]} queued={cut['queued']} "
                         f"synthesised={cut['synthesised']} llm_done={cut['llm_done']}")
            heard = []
            # complete = the LLM finished, every piece was synthesised, and (below) all of it was played
            complete = cut["llm_done"] and cut["synthesised"] == cut["queued"]
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
            text = " ".join(part for part in heard if part).strip()
            if complete:
                return None, None  # the whole reply was heard
            reply["content"] = text
            if not text:
                # nothing of the answer was heard, so the lookup behind it never happened either
                for entry in [reply, *cut["tool_entries"]]:
                    forget(entry)
            return cut["id"], text

        def forget(entry):
            """Removes one message object from the history, if it is still there."""
            for i, message in enumerate(history):
                if message is entry:
                    del history[i]
                    return

        def trim_history():
            """Keeps the system prompt and about the last 20 messages, never starting mid-exchange."""
            del history[1:-20]
            while len(history) > 1 and history[1]["role"] != "user":
                del history[1]

        async def warm_fillers():
            """Synthesises the "one moment" lines for the current voice ahead of time."""
            try:
                await toolbox.geeknews_posts()  # nine page loads: better now than inside the first question
            except Exception as e:
                logger.info(f"GeekNews listing not loaded yet: {e!r}")
            voice = settings["voice"] or args.tts_voice
            for text in toolbox.fillers():
                if (voice, text) not in filler_cache:
                    try:
                        filler_cache[(voice, text)] = await loop.run_in_executor(
                            tts_pool, tts.synthesize, text, voice, args.tts_lang, None)
                    except Exception as e:
                        logger.warning(f"Could not prepare filler speech: {e}")
                        return

        async def keep_knowledge_warm():
            """WeKnora's embedding model is unloaded after five idle minutes and then takes
            seconds to come back; a search every few minutes keeps the next real one fast."""
            while settings["knowledge"] and knowledge.available:
                try:
                    _, ms = await knowledge.search("warm up", timeout=30)
                    if ms > 1000:
                        logger.info(f"Knowledge search warmed up in {ms:.0f} ms")
                except Exception as e:
                    logger.warning(f"Knowledge warm-up failed: {e}")
                await asyncio.sleep(240)

        async def find_knowledge(text, request_id, out):
            """Looks the question up; returns the text to put before it in the prompt ("" if nothing fits)."""
            await out({"type": "knowledge", "id": request_id, "state": "start", "query": text[:80]})
            try:
                hits, ms = await knowledge.search(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Knowledge search failed: {e!r}")
                await out({"type": "knowledge", "id": request_id, "state": "done", "ok": False,
                           "message": f"지식베이스가 {knowledge.timeout:g}초 안에 답하지 않음"
                           if isinstance(e, httpx.TimeoutException) else str(e)[:80]})
                return ""
            threshold = float(settings["knowledge_threshold"] or 0)
            block, titles = knowledge.context(hits, threshold)
            top = hits[0]["score"] if hits else 0.0
            logger.info(f"Knowledge search {ms:.0f} ms, top score {top:.2f}, used {len(titles)} document(s): {text!r}")
            await out({"type": "knowledge", "id": request_id, "state": "done", "ok": True, "ms": ms, "top": top,
                       "threshold": threshold, "sources": titles, "found": len(hits)})
            return block

        async def chat(gen, request, out=None):
            """Streams an LLM reply and queues it for speech piece by piece.

            With the "tools" setting the LLM also knows the time and may ask for lookups; the reply
            then takes more than one pass: (tool calls -> results) up to MAX_TOOL_ROUNDS times, then text.

            out: where the client messages go (a speculative reply buffers them until committed).
            """
            out = out or send_json
            request_id = request.get("id")
            model = request.get("model") or settings["model"]
            user_message = {"role": "user", "content": str(request.get("text", "")).strip()}
            reply = {"role": "assistant", "content": ""}
            history.append(user_message)
            trim_history()
            history.append(reply)
            # everything below may be cancelled at any await, so what to undo is on record first
            entries = request["history_entries"] = [user_message, reply]
            tool_entries = []  # the tool exchange of this reply, kept in the history just before it
            last_reply.update({"message": reply, "id": request_id, "pieces": [],
                               "queued": 0, "synthesised": 0, "llm_done": False, "tool_entries": tool_entries})
            live = settings["tools"]
            # the knowledge base's own tools come with the knowledge setting, and whenever the user names the knowledge
            # base: "오늘 추가된 지식 베이스 정리해줘" with the setting off was answered "무슨 말인지 모르겠어요" (2026-11-23)
            said = user_message["content"] or ""
            # or calls it by its name ("테크 트렌드 뒤져서 알려줘": the knowledge base is "Tech Trend")
            named = bool(ASKS_KNOWLEDGE.search(said)) or any(
                alias and alias in re.sub(r"\s+", "", said.lower()) for alias in knowledge_aliases)
            knowledge_specs = KNOWLEDGE_TOOL_SPECS if (settings["knowledge"] or named) and knowledge.available else []
            if knowledge_specs and (named or settings.get("knowledge_mode") == "tool") and not (
                    ASKS_ELSEWHERE.search(user_message["content"] or "") and not named):
                knowledge_specs = knowledge_specs + [SEARCH_KNOWLEDGE_SPEC]
            # the language of this reply: fixed by the setting, or (auto) the one the user just used
            language = settings["language"]
            if language not in LANGUAGES:
                language = request.get("language") or text_language(user_message["content"])
            length = reply_length(user_message["content"], settings["length"])
            length_ko, length_en, num_predict, _ = REPLY_LENGTHS[length]
            # the context follows the setting, not this turn: changing it makes Ollama reload the model
            num_ctx = REPLY_LENGTHS[settings["length"] if settings["length"] in REPLY_LENGTHS else "short"][3]
            can_call = False
            started = time.perf_counter()
            requested = loop.time()
            buffer = ""
            sound_queued = False   # anything at all, filler included
            answer_queued = False
            tool_ms = None
            jobs = []
            reference = ""  # knowledge-base text for this question; in the prompt, not in the history

            today = datetime.now(toolbox.tz).date()
            clock = toolbox.clock()  # fixed for this reply, so its later passes reuse the prompt cache
            nudge = []  # a one-off correction, see LOOKUP_CLAIM; not kept in the history
            # where the conversation stands, from the history before this question (situation_note)
            cut = next((i for i, m in enumerate(history) if m is user_message), len(history))
            try:
                note = situation_note(history[:cut])
            except Exception as e:
                logger.warning(f"Situation note failed: {e!r}")
                note = ""

            def llm_messages():
                system = history[0]["content"]
                if language == "en":
                    system = DEFAULT_SYSTEM_PROMPT_EN if system == DEFAULT_SYSTEM_PROMPT else system + ENGLISH_OVERRIDE
                system += "\n" + toolbox.context(can_call) if live else ""
                if knowledge_specs and can_call:
                    system += "\n" + knowledge.guide(datetime.now(toolbox.tz), with_date=not live)
                    if any(spec["function"]["name"] == "search_knowledge" for spec in knowledge_specs):
                        system += (f" '{knowledge.name}' 지식베이스의 내용은 미리 찾아 주지 않습니다. 사용자가 지식베이스나 저장된 "
                                   "자료를 원할 때, 또는 앞서 지식베이스에서 소개한 자료를 더 물을 때 search_knowledge를 부르세요. "
                                   "검색어는 사용자의 말을 그대로 쓰지 말고 지금 이야기하는 주제로 정하세요. 뉴스, 인터넷, 최신 "
                                   "소식을 원하면 지식베이스를 쓰지 말고 그에 맞는 도구를 쓰세요.")
                if length_ko:
                    system += "\n" + (length_en if language == "en" else length_ko)
                messages = [{"role": "system", "content": system}]
                for message in history[1:]:
                    if message is user_message and (live or reference or note):
                        # the clock goes here, not in the system prompt: see Toolbox.context()
                        content = f"{reference}\n질문: {message['content']}" if reference else message["content"]
                        if note:
                            content = f"{note}\n{content}"
                        message = {**message, "content": f"{clock}\n{content}" if live else content}
                    if message is not reply:
                        messages.append(message)
                return messages + nudge

            async def take_back(claim, rest, correction):
                """Withdraws a sentence that was streamed to the page but not yet queued for speech.

                The first time, everything unspoken goes and the pass is run again with a correction
                (returns True). If the model does it again, only the offending sentence is dropped.
                """
                nonlocal buffer
                again = not nudge
                unspoken = claim + rest
                kept = "" if again else rest
                reply["content"] = reply["content"][:len(reply["content"]) - len(unspoken)] + kept
                buffer = kept
                logger.info(f"Held back an unsupported sentence "
                            f"({'asking again' if again else 'sentence dropped'}): {claim.strip()!r}")
                await out({"type": "chat_replace", "id": request_id, "text": reply["content"]})
                if again:
                    nudge.append({"role": "user", "content": correction})
                return again

            def objection(text, guarded, known):
                """The correction to send back if this sentence must not be spoken, else None."""
                if wrong_date(text, today):
                    return DATE_NUDGE
                # with knowledge-base text in the prompt "자료를 찾아보니" is true; a sentence about the
                # weather or the news still needs its own lookup ("사고 뉴스 있어?" once matched a document
                # and was answered with old headlines)
                if guarded and (not reference or LIVE_TOPIC.search(text)) and claims_lookup(text, known):
                    return LOOKUP_NUDGE
                return None

            def queue_speech(text, **extra):
                nonlocal sound_queued
                requests.put_nowait((gen, {
                    "text": text,
                    "voice": request.get("voice") or settings["voice"],
                    # only the first sound carries the timings, so the client can show the delay
                    "id": request_id,
                    "llm_ms": None if sound_queued else (time.perf_counter() - started) * 1000,
                    "stt_ms": None if sound_queued else request.get("stt_ms"),
                    "utterance_end": request.get("utterance_end"),
                    "speculative": request.get("speculative"),
                    **extra,
                }))
                sound_queued = True

            def queue_piece(piece):
                nonlocal answer_queued
                text = UNSPEAKABLE.sub("", piece).strip()
                if not text:
                    return
                last_reply["queued"] += 1
                phase = None if answer_queued else ("answer" if tool_ms is not None else "reply")
                queue_speech(text, reply=reply, phase=phase, tool_ms=tool_ms)
                answer_queued = True

            async def run_tool(call):
                """One lookup, reported to the client. Returns the message that carries its result."""
                await out({"type": "tool", "id": request_id, "state": "start",
                           "name": call["name"], "arguments": call["arguments"]})
                try:
                    if call["name"] in KNOWLEDGE_TOOLS:
                        text, ok, ms = await knowledge.run(call["name"], call["arguments"], toolbox.tz)
                    else:
                        text, ok, ms = await toolbox.run(call["name"], call["arguments"], language)
                except asyncio.CancelledError:
                    # interrupted: tell the page, so the lookup does not look as if it were still running
                    await asyncio.shield(out({"type": "tool", "id": request_id, "state": "cancelled",
                                              "name": call["name"], "arguments": call["arguments"]}))
                    raise
                logger.info(f"Tool {call['name']}({call['arguments']}) -> {'ok' if ok else 'failed'} in {ms:.0f} ms"
                            + ("" if ok else f": {text[:80]!r} [{getattr(toolbox, 'last_error', None)}]"))
                await out({"type": "tool", "id": request_id, "state": "done", "name": call["name"],
                           "arguments": call["arguments"], "ok": ok, "ms": ms, "result": text[:1200],
                           # addresses in the result, shown as links under the lookup: the reply does not read them out
                           "links": list(dict.fromkeys(re.findall(r"https?://[^\s)\]>'\"]+", text)))[:6],
                           # how many numbered entries came back (articles, posts, documents)
                           "items": len(re.findall(r"^\s*\d+\. ", text, re.M))})
                return {"role": "tool", "tool_name": call["name"], "content": text}

            try:
                if truncation["pending"]:
                    # let the browser's playback report fix up the interrupted reply first
                    pending = truncation["pending"]
                    try:
                        await asyncio.wait_for(pending.wait(), 0.5)
                    except asyncio.TimeoutError:
                        pass
                    if truncation["pending"] is pending:
                        truncation["pending"] = None
                can_call = bool(live or knowledge_specs) and await llm.supports(model, "tools")
                started = time.perf_counter()
                # "두 번째 글 더 자세히" points at the conversation, not at a topic: searched as it is, it
                # matched an unrelated document (score 0.53) and the reply lost the thread
                mode = settings.get("knowledge_mode") or "before_reply"
                if settings["knowledge"] and knowledge.available and user_message["content"] \
                        and not knowledge_first(user_message["content"], mode):
                    await out({"type": "knowledge", "id": request_id, "state": "skipped"})
                    knowledge_memory["reference"] = ""
                elif settings["knowledge"] and knowledge.available and user_message["content"]:
                    if BACK_REFERENCE.search(user_message["content"]) and mode == "before_reply":
                        # ...so a follow-up ("그거 누가 만들었어?") keeps the material of the turn before
                        reference = knowledge_memory["reference"]
                        await out({"type": "knowledge", "id": request_id, "state": "reused", "has": bool(reference)})
                    else:
                        reference = await find_knowledge(user_message["content"], request_id, out)
                        knowledge_memory["reference"] = reference
                empty_retry = False
                for round_index in range(MAX_TOOL_ROUNDS + 1):
                    # the last pass gets no tools, so the reply cannot loop on lookups
                    tools = None
                    if can_call and round_index < MAX_TOOL_ROUNDS:
                        tools = (toolbox.specs if live else []) + knowledge_specs
                    calls = []
                    lookup_started = None
                    pass_started = time.perf_counter()
                    first_token_ms = None
                    # only a reply that could have looked something up, and has not, is held to account
                    guarded = tools is not None and not tool_entries
                    known = " ".join(m["content"] for m in history if m["role"] == "tool")
                    retry = False
                    await out({"type": "llm", "id": request_id, "state": "start", "round": round_index + 1,
                               "model": model or args.llm_model, "tools": len(tools or []),
                               "language": language, "length": length})
                    async for kind, value in llm.stream(llm_messages(), model, tools, num_predict, num_ctx):
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - pass_started) * 1000
                            await out({"type": "llm", "id": request_id, "state": "first_token",
                                       "round": round_index + 1, "ms": first_token_ms})
                            if first_token_ms > 2500:
                                logger.info(f"Slow first token: {first_token_ms:.0f} ms in pass {round_index + 1} "
                                            f"(request arrived {loop.time() - requested:.1f}s ago)")
                        if kind == "tool":
                            if not sound_queued:
                                # say something right away; the lookup and the second pass take a while
                                queue_speech("One moment, let me check." if language == "en"
                                             else KNOWLEDGE_FILLER if value["name"] in KNOWLEDGE_TOOLS
                                             else toolbox.filler(value["name"]), filler=True, phase="filler")
                            lookup_started = lookup_started or time.perf_counter()
                            calls.append(value)
                            jobs.append(asyncio.create_task(run_tool(value)))
                            continue
                        if not reply["content"]:
                            value = value.lstrip()  # some passes open with a line break
                            if not value:
                                continue
                        reply["content"] += value
                        buffer += value
                        await out({"type": "chat_delta", "id": request_id, "text": value})
                        # a short first piece gets the avatar talking sooner
                        piece, buffer = take_piece(buffer, 40, first=not answer_queued)
                        correction = objection(piece, guarded and not calls, known) if piece and live else None
                        if correction:
                            if await take_back(piece, buffer, correction):
                                retry = True
                                break
                            continue
                        if piece:
                            if not answer_queued and round_index:
                                logger.info(
                                    f"Reply after lookup: pass {round_index + 1} began "
                                    f"{(pass_started - started) * 1000:.0f} ms in, first token after "
                                    f"{first_token_ms:.0f} ms, first sentence after "
                                    f"{(time.perf_counter() - pass_started) * 1000:.0f} ms")
                            queue_piece(piece)
                    if retry:
                        continue
                    correction = objection(buffer, guarded and not calls, known) if live else None
                    if correction and await take_back(buffer, "", correction):
                        continue
                    if not calls and tool_entries and not reply["content"].strip() and not empty_retry:
                        # now and then the model returns nothing at all after a lookup; ask once more
                        empty_retry = True
                        logger.info(f"Empty reply after a lookup in pass {round_index + 1}; asking again")
                        continue
                    if not calls:
                        break
                    # anything said along with the calls is a sentence of its own, not the start of the answer
                    queue_piece(buffer)
                    buffer = ""
                    if reply["content"].strip():
                        reply["content"] = reply["content"].rstrip() + " "
                    results = await asyncio.gather(*jobs)
                    jobs.clear()
                    tool_ms = (tool_ms or 0.0) + (time.perf_counter() - lookup_started) * 1000
                    exchange = [{"role": "assistant", "content": "",
                                 "tool_calls": [{"function": call} for call in calls]}, *results]
                    # in the history the lookup comes before the reply it led to
                    position = next((i for i, m in enumerate(history) if m is reply), len(history))
                    history[position:position] = exchange
                    tool_entries.extend(exchange)
                    entries.extend(exchange)
                queue_piece(buffer)
                last_reply["llm_done"] = True
                await out({"type": "llm", "id": request_id, "state": "done", "chars": len(reply["content"]),
                           "ms": (time.perf_counter() - started) * 1000})
                if not reply["content"].strip():
                    await out({"type": "notice", "message": "LLM이 답을 내지 못했습니다. 다시 시도해 주세요."})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"LLM failed: {e}")
                await out({"type": "notice", "message": f"LLM 실패: {e}"})
            finally:
                for job in jobs:
                    job.cancel()  # interrupted while looking something up
                if not reply["content"]:
                    forget(reply)  # may already be gone if a speculative reply was discarded
                    for entry in tool_entries:
                        forget(entry)
                if gen == generation["n"]:
                    await out({"type": "chat_done", "id": request_id})

        def avatar_busy():
            """True while a reply is being prepared or (as far as the server can tell) still playing."""
            if hold["on"]:
                return False  # only a speculative reply exists, and nobody can hear it yet
            task = chat_state["task"]
            return bool(
                speech or synth_state["busy"] or not requests.empty()
                or (task and not task.done()) or loop.time() < listen["busy_until"]
            )

        def only_tail():
            """The avatar has nothing more to say: all that keeps it "busy" is the allowance for the browser
            still playing the last chunk (up to about 1.7 s after the last audible word)."""
            task = chat_state["task"]
            return not (speech or synth_state["busy"] or not requests.empty() or (task and not task.done()))

        def last_agent_text():
            for message in reversed(history):
                if message.get("role") == "assistant" and message.get("content"):
                    return str(message["content"])
            return ""

        def expecting_answer():
            """The avatar's last words were a question: a short "좋아" or "네" is now an answer."""
            return last_agent_text().rstrip().endswith(("?", "？"))

        async def update_listen_state():
            if listen["transcribing"]:
                state = "transcribing"
            elif avatar_busy():
                state = "speaking"
            elif endpointer.in_speech:
                state = "hearing"
            else:
                state = "listening"
            if state != listen["state"]:
                listen["state"] = state
                await send_json({"type": "listen_state", "state": state})

        async def spec_out(payload):
            if spec["committed"]:
                await send_json(payload)
            else:
                spec["outbox"].append(payload)

        async def speculate(audio, utterance_end):
            """Prepares a reply to what has been said so far, in case the speaker is done."""
            started = time.perf_counter()
            text, heard_in = await loop.run_in_executor(stt_pool, recognizer().transcribe, audio, stt_language())
            if len(text) < 2 or not SENTENCE_END.search(text) or HALLUCINATION.match(_bare(text)):
                # mid-sentence ("그러니까 내일..."): the speaker will go on, a reply now would be thrown away.
                # "감사합니다." may be made up from noise: the whole utterance is checked in on_utterance instead.
                spec["text"] = ""
                return
            listen["turn"] += 1
            request = {
                "id": f"voice-{listen['turn']}",
                "text": text,
                "stt_ms": (time.perf_counter() - started) * 1000,
                "utterance_end": utterance_end,
                "speculative": True,
                "language": heard_in,
            }
            spec["outbox"].append({"type": "transcript", "id": request["id"], "text": text,
                                   "stt_ms": request["stt_ms"], "language": heard_in, "speculative": True})
            spec["request"] = request
            hold["on"] = True
            spec["text"] = text
            await chat(generation["n"], request, out=spec_out)

        async def discard_speculation():
            """The speaker went on: forget the reply that was being prepared."""
            task = spec["task"]
            if task is None:
                return
            spec["task"] = None
            task.cancel()
            generation["n"] += 1
            stream["base"] += len(speech)
            speech.clear()
            marks.clear()
            while not requests.empty():
                requests.get_nowait()
            for entry in (spec["request"] or {}).get("history_entries", ()):
                forget(entry)
            last_reply["message"] = None
            hold["on"] = False
            if spec["text"]:  # otherwise nothing but a transcription was spent
                await send_json({"type": "metric", "name": "speculation_discarded", "text": spec["text"]})

        async def begin_speculation(audio):
            await discard_speculation()
            spec.update({"text": None, "request": None, "outbox": [], "committed": False})
            spec["task"] = asyncio.create_task(speculate(audio, loop.time() - Endpointer.PAUSE_SILENCE))

        async def commit_speculation(audio, utterance_end):
            """The pause was the end of the utterance: let the prepared reply out."""
            task = spec["task"]
            while spec["text"] is None and not task.done():
                await asyncio.sleep(0.01)  # speech recognition has not finished yet
            if spec["task"] is not task:
                return  # discarded meanwhile
            if not spec["text"]:
                # nothing was prepared (the pause did not look like an ending): answer the normal way
                spec["task"] = None
                hold["on"] = False
                await on_utterance(audio, utterance_end)
                return
            while spec["outbox"]:
                await send_json(spec["outbox"].pop(0))
            spec["committed"] = True
            hold["on"] = False
            chat_state["task"] = task
            spec["task"] = None

        async def on_utterance(audio, utterance_end, over_avatar=False, in_tail=False):
            """over_avatar: the utterance began while the avatar was talking and has not interrupted it yet.
            in_tail: it began after the avatar's last words, while only the playback allowance was left."""
            listen["transcribing"] = True
            await update_listen_state()
            started = time.perf_counter()
            heard_in = None
            try:
                text, heard_in = await loop.run_in_executor(stt_pool, recognizer().transcribe, audio, stt_language())
            except Exception as e:
                logger.warning(f"STT failed: {e}")
                await send_json({"type": "notice", "message": f"음성 인식 실패: {e}"})
                text = ""
            finally:
                listen["transcribing"] = False
            if text and HALLUCINATION.match(_bare(text)):
                try:
                    speech_s = await asyncio.to_thread(speech_seconds, audio)
                except Exception:
                    speech_s = None
                if is_hallucination(text, speech_s, getattr(recognizer(), "last_logprob", None)):
                    logger.info(f"Ignored a made-up transcript {text!r} (speech {speech_s} s)")
                    await send_json({"type": "metric", "name": "hallucination_ignored", "text": text})
                    await update_listen_state()
                    return
            answering = expecting_answer() and (only_tail() or not avatar_busy())
            one_word_answer = answering and bool(SHORT_ANSWER.match(_bare(text)))
            if in_tail and is_echo(text, last_agent_text()):
                await send_json({"type": "metric", "name": "echo_ignored", "text": text})
            elif over_avatar and is_backchannel(text) and not one_word_answer:
                # the listener was only signalling attention: keep talking, say nothing about it
                await send_json({"type": "metric", "name": "backchannel_ignored", "text": text})
            elif len(text) >= 2 or one_word_answer:
                if over_avatar and auto["active"] and avatar_busy():
                    await report_interrupt()
                listen["turn"] += 1
                request = {
                    "id": f"voice-{listen['turn']}",
                    "text": text,
                    "stt_ms": (time.perf_counter() - started) * 1000,
                    "utterance_end": utterance_end,
                    "language": heard_in,
                }
                await send_json({"type": "transcript", "id": request["id"], "text": text,
                                 "stt_ms": request["stt_ms"], "language": heard_in, "engine": settings["stt"]})
                await cancel_speech()
                chat_state["task"] = asyncio.create_task(chat(generation["n"], request))
            await update_listen_state()

        async def on_microphone(data):
            if stt is None or listen["transcribing"]:
                return
            expecting = expecting_answer()
            endpointer.min_voiced = SHORT_ANSWER_VOICED if expecting else Endpointer.MIN_VOICED
            # After the avatar's last words the server still counts it busy for a while (the browser may still
            # be playing). Wiping the microphone then threw away a quick "좋아" or "그래": listen in that tail,
            # and drop only what turns out to be the avatar's own echo.
            tail = avatar_busy() and only_tail()
            if avatar_busy() and not settings["barge_in"] and not tail:
                # Half-duplex: without headphones the microphone hears the avatar's own voice.
                endpointer.reset()
            else:
                gated = settings["interrupt_mode"] == "unmute_gate"
                endpointer.judge = smart_turn.probability if smart_turn and settings["endpoint"] == "smart" else None
                for kind, audio in endpointer.feed(np.frombuffer(data, dtype="<i2")):
                    if kind == "start":
                        listen["over_avatar"] = False
                        listen["in_tail"] = tail
                    if kind == "start" and avatar_busy():
                        if tail:
                            pass   # the avatar has said everything: this is the user's turn, likely an answer
                        elif gated:
                            listen["over_avatar"] = True  # wait and see whether this is a real interruption
                        else:
                            if auto["active"]:
                                await report_interrupt()
                            await cancel_speech()  # the user started talking over the avatar
                    elif kind == "pause":
                        if settings["speculative"] and not listen["over_avatar"] and not avatar_busy():
                            await begin_speculation(audio)
                    elif kind == "resume":
                        await discard_speculation()
                    elif kind == "utterance":
                        utterance_end = loop.time() - endpointer.last["silence"]
                        if endpointer.last["probability"] is not None:
                            logger.info(f"Turn closed after {endpointer.last['silence']:.2f} s of silence "
                                        f"(finished-probability {endpointer.last['probability']:.2f})")
                            await send_json({"type": "metric", "name": "turn_end", **endpointer.last})
                        if spec["task"] is not None:
                            asyncio.create_task(commit_speculation(audio, utterance_end))
                        else:
                            asyncio.create_task(on_utterance(audio, utterance_end, listen["over_avatar"],
                                                             listen.get("in_tail", False)))
                        listen["over_avatar"] = False
                        listen["in_tail"] = False
                voiced = endpointer.voiced * Endpointer.WINDOW / engine.sample_rate
                if listen["over_avatar"] and endpointer.in_speech and voiced >= BARGE_IN_SECONDS:
                    # too long for a backchannel: the user is taking the turn
                    listen["over_avatar"] = False
                    if avatar_busy():
                        if auto["active"]:
                            await report_interrupt()
                        await cancel_speech()
            await update_listen_state()

        # ---- auto source: a simulated user (LLM persona + STT ears + TTS voice) --------------------
        auto = {
            "active": False, "task": None, "mic_task": None, "topic": "", "level": "medium",
            "guest_voice": "M1", "max_turns": 6, "ask_live": False,
            "dialogue": [],            # (speaker, text) as the simulated user experienced it
            "avatar_audio": bytearray(),  # avatar speech sent since the user's last full turn
            "avatar_end_at": 0.0,      # when the avatar speech sent so far finishes playing
            "guest_until": 0.0,        # when the simulated user's current utterance ends
            "guest_onset": 0.0, "guest_kind": None,
        }
        virtual_mic = bytearray()      # simulated user's speech not yet "picked up" by the microphone

        def avatar_speaking():
            return (bool(speech) and not hold["on"]) or loop.time() < auto["avatar_end_at"]

        def avatar_pending():
            """The avatar is still working on a response to something the user said."""
            task = chat_state["task"]
            return bool(
                endpointer.in_speech or listen["transcribing"] or synth_state["busy"] or hold["on"]
                or spec["task"] is not None or not requests.empty() or (task and not task.done())
            )

        async def report_interrupt():
            """Metric: how the system reacted to the simulated user talking over the avatar."""
            now = loop.time()
            unmute = settings["interrupt_mode"].startswith("unmute")
            await send_json({
                "type": "metric", "name": "interrupt", "kind": auto["guest_kind"],
                "mode": settings["interrupt_mode"],
                # from the user's first sound to the system deciding to stop
                "detect_ms": (now - auto["guest_onset"]) * 1000,
                # how long the avatar keeps talking after that decision
                "residual_ms": 30.0 if unmute else max(0.0, auto["avatar_end_at"] - now) * 1000,
            })

        async def virtual_microphone():
            """Feeds the simulated user's voice (or silence) to the voice pipeline in real time."""
            packet = engine.sample_rate // 10 * 2  # 100 ms
            started = loop.time()
            count = 0
            while True:
                count += 1
                delay = started + count * 0.1 - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                data = bytes(virtual_mic[:packet])
                del virtual_mic[:packet]
                await on_microphone(data + b"\x00" * (packet - len(data)))

        async def guest_hear(extra=b""):
            """What the simulated user has made out of the avatar's speech so far (through STT)."""
            audio = np.frombuffer(bytes(auto["avatar_audio"]) + extra, dtype="<i2")
            if len(audio) < engine.sample_rate // 2:
                return ""
            text, _ = await loop.run_in_executor(
                stt_pool, recognizer().transcribe, audio.astype(np.float32) / 32768.0, stt_language())
            return text

        def guest_messages(closing):
            messages = [
                {"role": "system", "content": AUTO_GUEST_PROMPT.format(topic=auto["topic"])
                 + (AUTO_GUEST_LIVE if auto["ask_live"] else "")},
                {"role": "user", "content": "(대화를 시작합니다. 먼저 말을 거세요.)"},
            ]
            for speaker, text in auto["dialogue"]:
                messages.append({"role": "assistant" if speaker == "guest" else "user", "content": text})
            if messages[-1]["role"] == "assistant":
                messages.append({"role": "user", "content": "(상대가 아무 말도 하지 않습니다. 대화를 이어가세요.)"})
            if closing:
                messages[-1]["content"] += "\n(이제 대화를 마무리하는 짧은 인사를 하세요.)"
            return messages

        async def guest_prepare(kind, text=None, closing=False, heard=None):
            """Decides what the simulated user says next (unless given) and synthesises it."""
            if text is None:
                if heard:
                    auto["dialogue"].append(("avatar", heard))
                try:
                    text = await llm.complete(guest_messages(closing), settings["model"])
                finally:
                    if heard:
                        auto["dialogue"].pop()
            text = UNSPEAKABLE.sub("", text).strip()
            if not text:
                return None
            pcm = await loop.run_in_executor(
                tts_pool, tts.synthesize, text, auto["guest_voice"], args.tts_lang, None
            )
            return {"kind": kind, "text": text, "pcm": pcm, "heard": heard}

        async def guest_say(utterance):
            """Plays the utterance to the viewer and into the virtual microphone at the same moment."""
            now = loop.time()
            length = len(utterance["pcm"]) / 2 / engine.sample_rate
            auto.update({"guest_onset": now, "guest_kind": utterance["kind"], "guest_until": now + length})
            if utterance["kind"] != "backchannel":
                # a full turn: note what was heard of the avatar, then start listening afresh
                if utterance.get("heard"):
                    auto["dialogue"].append(("avatar", utterance["heard"]))
                auto["dialogue"].append(("guest", utterance["text"]))
                auto["avatar_audio"].clear()
            await send_json({"type": "guest", "kind": utterance["kind"], "text": utterance["text"],
                             "duration": length})
            await send_bytes(GUEST_MAGIC + struct.pack("<I", len(utterance["pcm"])) + utterance["pcm"])
            virtual_mic.extend(utterance["pcm"])

        async def guest_loop():
            interval, backchannel_gap, min_heard, tendency = AUTO_LEVELS.get(auto["level"], AUTO_LEVELS["medium"])
            turns = 0
            prepared = None            # next turn, composed while the avatar finishes its sentence
            last_decision = last_backchannel = idle_since = 0.0
            try:
                await guest_say(await guest_prepare("next"))
                turns += 1
                while True:
                    await asyncio.sleep(0.1)
                    now = loop.time()
                    if now < auto["guest_until"]:
                        continue  # still talking
                    if avatar_speaking():
                        idle_since = 0.0
                        reply_done = (
                            last_reply["llm_done"] and last_reply["synthesised"] == last_reply["queued"]
                            and not avatar_pending()
                        )
                        if reply_done and len(speech) <= 2 * slice_bytes:
                            # the avatar is on its last sentence: think of what to say next
                            if prepared is None and turns < auto["max_turns"] + 1:
                                gen = generation["n"]
                                heard = await guest_hear(bytes(speech))
                                prepared = await guest_prepare("next", closing=turns >= auto["max_turns"], heard=heard)
                                if prepared:
                                    prepared["generation"] = gen
                            continue
                        heard_seconds = len(auto["avatar_audio"]) / 2 / engine.sample_rate
                        if heard_seconds < min_heard or now - last_decision < interval:
                            continue
                        last_decision = now
                        heard = await guest_hear()
                        question = next((t for who, t in reversed(auto["dialogue"]) if who == "guest"), "")
                        raw = await llm.complete(
                            [{"role": "system", "content": AUTO_GUEST_PROMPT.format(topic=auto["topic"])},
                             {"role": "user", "content": AUTO_DECIDE_PROMPT.format(
                                 question=question, heard=heard, tendency=tendency)}],
                            settings["model"], AUTO_DECIDE_SCHEMA,
                        )
                        try:
                            decision = json.loads(raw)
                        except ValueError:
                            continue
                        action = decision.get("action")
                        if action == "backchannel" and loop.time() - last_backchannel >= backchannel_gap:
                            utterance = await guest_prepare("backchannel", str(decision.get("text", ""))[:12])
                            if utterance and avatar_speaking():
                                last_backchannel = loop.time()
                                await guest_say(utterance)
                        elif action == "interrupt":
                            utterance = await guest_prepare("interrupt", str(decision.get("text", "")), heard=heard)
                            if utterance and avatar_speaking():
                                prepared = None
                                turns += 1
                                await guest_say(utterance)
                        continue
                    if avatar_pending():
                        idle_since = 0.0
                        continue
                    # Nobody is talking. Leave a natural beat after the avatar's last word, then speak.
                    idle_since = idle_since or now
                    if prepared and prepared.get("generation") != generation["n"]:
                        prepared = None  # the conversation moved on since this was composed
                    if prepared is None:
                        if turns >= auto["max_turns"] + 1:
                            break
                        if now - idle_since < 0.3:
                            continue
                        prepared = await guest_prepare(
                            "next", closing=turns >= auto["max_turns"], heard=await guest_hear())
                        if prepared is None:
                            continue
                        prepared["generation"] = generation["n"]
                    wait = auto["avatar_end_at"] + 0.3 - loop.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    await send_json({"type": "metric", "name": "user_gap",
                                     "gap_ms": (loop.time() - auto["avatar_end_at"]) * 1000})
                    await guest_say(prepared)
                    prepared = None
                    turns += 1
                await send_json({"type": "auto_done", "turns": turns})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Simulated user failed")
                await send_json({"type": "notice", "message": f"자율 대화 오류: {e}"})
            finally:
                auto["active"] = False

        async def auto_stop():
            auto["active"] = False
            for key in ("task", "mic_task"):
                if auto[key]:
                    auto[key].cancel()
                    auto[key] = None
            virtual_mic.clear()

        async def auto_start(request):
            await auto_stop()
            await cancel_speech()
            if stt is None:
                await send_json({"type": "notice", "message": f"음성 인식을 쓸 수 없습니다: {stt_error}"})
                return
            auto.update({
                "active": True,
                "topic": str(request.get("topic") or "요즘 관심사").strip(),
                "level": request.get("level") or "medium",
                "guest_voice": request.get("guest_voice") or "M1",
                "max_turns": max(1, min(30, int(request.get("max_turns") or 6))),
                "ask_live": bool(request.get("ask_live")),
                "dialogue": [], "avatar_end_at": 0.0, "guest_until": 0.0,
            })
            auto["avatar_audio"].clear()
            del history[1:]
            history[0]["content"] = AUTO_AVATAR_PROMPT.format(topic=auto["topic"])
            settings["barge_in"] = True  # the simulated user's microphone never hears the avatar
            endpointer.reset()
            auto["mic_task"] = asyncio.create_task(virtual_microphone())
            auto["task"] = asyncio.create_task(guest_loop())

        async def control():
            while True:
                message = await incoming.get()
                if message is None:
                    return
                if message.get("bytes"):
                    if endpointer and source == "voice":
                        await on_microphone(message["bytes"])
                    continue
                try:
                    request = json.loads(message.get("text") or "")
                except ValueError:
                    continue
                if request.get("type") == "config":
                    settings.update({k: request[k] for k in settings if k in request})
                    if settings["tools"]:
                        background.add(task := asyncio.create_task(warm_fillers()))
                        task.add_done_callback(background.discard)
                    if settings["knowledge"] and knowledge.available and not knowledge_warmer["task"]:
                        knowledge_warmer["task"] = asyncio.create_task(keep_knowledge_warm())
                        # the loop ends by itself once the setting is switched off
                        knowledge_warmer["task"].add_done_callback(lambda _: knowledge_warmer.update(task=None))
                    continue
                if request.get("type") == "played":
                    interrupted_id, heard = apply_playback(request.get("seq"), float(request.get("offset") or 0))
                    if heard is not None:
                        logger.info(f"Interrupted reply cut to what was heard: {heard!r}")
                        await send_json({"type": "chat_truncate", "id": interrupted_id, "text": heard})
                    if truncation["pending"]:
                        truncation["pending"].set()
                    continue
                if request.get("type") == "speak":
                    request["phase"] = "reply"
                    requests.put_nowait((generation["n"], request))
                elif request.get("type") == "chat":
                    await cancel_speech()  # a new message interrupts whatever is being said
                    chat_state["task"] = asyncio.create_task(chat(generation["n"], request))
                elif request.get("type") == "clear":
                    await auto_stop()
                    await cancel_speech()
                elif request.get("type") == "auto_start" and source == "auto":
                    await auto_start(request)

        synth_task = asyncio.create_task(synthesizer())
        control_task = asyncio.create_task(control())
        try:
            clock_start = loop.time()
            index = 0       # chunks since the clock was last (re)started
            sent = 0        # chunks since the session started
            while not control_task.done():
                wait = clock_start + index * duration - loop.time()
                if wait > 0 and not kick.is_set():
                    kicked = asyncio.create_task(kick.wait())
                    done, _ = await asyncio.wait(
                        {control_task, kicked}, timeout=wait, return_when=asyncio.FIRST_COMPLETED
                    )
                    kicked.cancel()
                    if control_task in done:
                        break
                if kick.is_set():
                    # the browser just threw away its buffer: send the next chunk now, not on the old beat
                    kick.clear()
                    clock_start, index = loop.time(), 0
                held = hold["on"]
                pcm = b"" if held else bytes(speech[:slice_bytes])
                del speech[:len(pcm)]
                stream["idle"] = not pcm and not speech   # nothing to say: the next speech may start at once
                chunk_speech[session["seq"]] = (stream["base"], len(pcm))
                chunk_speech.pop(session["seq"] - 200, None)
                stream["base"] += len(pcm)
                if pcm:
                    # the browser plays this chunk a moment later; allow for that and for room echo
                    listen["busy_until"] = loop.time() + duration + 1.0
                    if auto["active"]:
                        auto["avatar_audio"].extend(pcm)
                        auto["avatar_end_at"] = (
                            loop.time() + CLIENT_PLAYBACK_DELAY + len(pcm) / 2 / engine.sample_rate
                        )
                pcm += b"\x00" * (slice_bytes - len(pcm))
                for mark in ([] if held else list(marks)):
                    if mark["pos"] < slice_bytes:
                        marks.remove(mark)
                        await send_json({
                            "type": "speech",
                            "seq": session["seq"],
                            "id": mark["id"],
                            "phase": mark["phase"],
                            "tool_ms": mark["tool_ms"],
                            "offset": mark["pos"] / 2 / engine.sample_rate,
                            "synth_ms": mark["synth_ms"],
                            "llm_ms": mark["llm_ms"],
                            "stt_ms": mark["stt_ms"],
                            "speculative": mark["speculative"],
                            "since_utterance_ms": (
                                None if mark["utterance_end"] is None
                                else (loop.time() - mark["utterance_end"]) * 1000
                            ),
                        })
                    else:
                        mark["pos"] -= slice_bytes
                stream["in_flight"] = True
                try:
                    await send_chunk(pcm, sent * engine.slice_samples, 0)
                finally:
                    stream["in_flight"] = False
                index += 1
                sent += 1
                if endpointer:
                    await update_listen_state()
        finally:
            synth_task.cancel()
            control_task.cancel()
            await auto_stop()
            for task in (chat_state["task"], spec["task"], knowledge_warmer["task"], *background):
                if task:
                    task.cancel()

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, source: str = "mic"):
        await ws.accept()
        if state["busy"]:
            await ws.send_text(json.dumps({"type": "error", "message": "다른 세션이 사용 중입니다."}))
            await ws.close()
            return
        state["busy"] = True
        logger.info(f"Session started (source={source}).")
        try:
            await on_worker(engine.reset)
            await ws.send_text(json.dumps({
                "type": "ready",
                "sample_rate": engine.sample_rate,
                "fps": engine.fps,
                "slice_frames": engine.slice_frames,
                "slice_samples": engine.slice_samples,
                "width": engine.width,
                "height": engine.height,
                "model_type": args.model_type,
                "source": source,
            }))

            incoming = asyncio.Queue()
            session = {"seq": 0}
            send_lock = asyncio.Lock()  # several tasks write to this socket

            async def send_json(payload):
                async with send_lock:
                    try:
                        await ws.send_text(json.dumps(payload))
                    except RuntimeError:
                        pass  # the socket closed while a task was still winding down

            async def send_bytes(payload):
                async with send_lock:
                    await ws.send_bytes(payload)

            async def receiver():
                try:
                    while True:
                        message = await ws.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        incoming.put_nowait(message)
                finally:
                    incoming.put_nowait(None)

            async def send_chunk(pcm, start_sample, dropped_samples):
                audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                started = time.perf_counter()
                jpegs = await on_worker(engine.step, audio)
                infer_ms = (time.perf_counter() - started) * 1000
                header = MAGIC + struct.pack(
                    "<IIIIfI", session["seq"], start_sample, len(jpegs), len(pcm), infer_ms, dropped_samples
                )
                body = b"".join(struct.pack("<I", len(j)) + j for j in jpegs)
                async with send_lock:
                    await ws.send_bytes(header + pcm + body)
                session["seq"] += 1

            receiver_task = asyncio.create_task(receiver())
            try:
                if source in ("tts", "chat", "voice", "auto"):
                    await run_tts(send_json, send_bytes, incoming, send_chunk, session, source)
                else:
                    await run_mic(incoming, send_chunk)
            finally:
                receiver_task.cancel()
        except Exception as e:
            logger.info(f"Session ended: {type(e).__name__}: {e}")
        finally:
            state["busy"] = False
            logger.info("Session closed.")

    return app


def parse_args():
    parser = argparse.ArgumentParser(description="Real-time microphone / TTS streaming for FlashHead")
    parser.add_argument("--ckpt_dir", default="models/SoulX-FlashHead-1_3B")
    parser.add_argument("--wav2vec_dir", default="models/wav2vec2-base-960h")
    parser.add_argument("--model_type", default="lite", choices=["lite", "pro"],
                        help="Only lite is faster than real time on a single GPU.")
    parser.add_argument("--cond_image", default="examples/girl.png", help="Initial reference image.")
    parser.add_argument("--seed", type=int, default=9999)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7861)
    parser.add_argument("--jpeg_quality", type=int, default=85)
    parser.add_argument("--gate_db", type=float, default=None,
                        help="Optional noise gate threshold in dBFS (e.g. -50); quieter audio is treated "
                             "as silence. Off by default.")
    parser.add_argument("--max_backlog", type=int, default=3,
                        help="Skip old audio once this many chunks are waiting.")
    parser.add_argument("--upload_dir", default="gradio_results/mic_refs")
    parser.add_argument("--tts_url", default="http://127.0.0.1:7788", help="URL of `supertonic serve`.")
    parser.add_argument("--tts_voice", default="F1")
    parser.add_argument("--tts_lang", default="ko")
    parser.add_argument("--llm_url", default="http://127.0.0.1:11434", help="URL of the Ollama server.")
    parser.add_argument("--llm_model", default="gemma4:26b",
                        help="Ollama model. Thinking models answer several seconds later.")
    parser.add_argument("--stt_model", default="mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                        help="faster-whisper model for the voice source.")
    parser.add_argument("--stt_lang", default="ko")
    parser.add_argument("--llm_system", default=DEFAULT_SYSTEM_PROMPT, help="System prompt for the chat source.")
    parser.add_argument("--location", default="서울", help="Place assumed when a weather question names none.")
    parser.add_argument("--timezone", default="Asia/Seoul", help="Time zone of the clock the LLM is told.")
    parser.add_argument("--weknora_url", default="http://127.0.0.1/api/v1", help="WeKnora API base URL.")
    parser.add_argument("--weknora_key_file", default="~/.weknora_rt_key",
                        help="File holding a WeKnora API key with the retrieve capability. "
                             "Without it the knowledge search is off.")
    parser.add_argument("--weknora_kb", default="29f24b56-2379-4a82-96c7-e1b01963206c",
                        help="ID of the WeKnora knowledge base to search.")
    parser.add_argument("--knowledge_name", default="Tech Trend", help="Name of that knowledge base, as shown.")
    parser.add_argument("--knowledge_aliases", default="테크 트렌드,테크트렌드",
                        help="Other names the user may call the knowledge base by, comma-separated.")
    parser.add_argument("--knowledge_timeout", type=float, default=2.5,
                        help="Seconds a knowledge search may take before the reply goes ahead without it.")
    parser.add_argument("--asr_url", default="http://127.0.0.1:7790",
                        help="URL of realtime_asr_server.py (Qwen3-ASR), offered as a second speech recognizer.")
    parser.add_argument("--smart_turn_model", default="models/smart-turn/smart-turn-v3.2-cpu.onnx",
                        help="ONNX file of pipecat-ai/smart-turn; without it the end-of-turn model is off.")
    parser.add_argument("--tool_timeout", type=float, default=4.0,
                        help="Seconds a weather/news lookup may take before the avatar gives up on it.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    app = create_app(args)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
