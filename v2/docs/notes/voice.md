# WP5 음성과 아바타: 메모

파일: `agentkit/runtime/voice.py`, `audio.py`, `avatar.py`, `tests/test_voice_session.py`(빠른 시험 27개), `tests/test_voice_integration.py`(GPU 시험 4개), `tests/voice_fakes.py`(시험용 가짜).

## 1. 서버와 엔진에 바라는 것 (호출 지점)

### 서버 → 음성 모듈

```python
from agentkit.runtime.voice import serve_voice
await serve_voice(ws, engine, channel, settings=None)   # channel: "voice" | "avatar"
```

- `ws`는 이미 `accept()`된 소켓. `serve_voice`가 `ready`를 보내고, 끊길 때까지 소켓을 읽고 쓴 뒤 돌아온다.
- **소켓도 엔진도 닫지 않는다.** 돌아온 뒤 `engine.close()`는 호출한 쪽이 한다(지금 `server.py`가 그렇게 한다). 아바타가 사용 중이거나 영상 엔진을 못 올리면 `{"type":"error","message":...}` 하나만 보내고 바로 돌아온다. 소켓은 호출한 쪽이 닫는다(엔드포인트가 끝나면 uvicorn이 닫는다).
- `settings`는 dict 또는 `VoiceSettings`. 지금 `server.py`는 쿼리 문자열 전체(`dict(query)`)를 넘긴다. 읽는 키: `voice`(또는 `tts_voice`), `stt`, `endpoint`, `interrupt_mode`, `barge_in`, `speculative`, `language`. 모르는 키와 틀린 값은 무시한다. 문자열 `"0"`, `"false"`는 꺼짐으로 읽는다. 같은 키를 클라이언트의 `config` 메시지로 대화 중에 바꿀 수 있다.
- 모델은 처음 쓸 때 올린다. 첫 음성 연결은 Whisper를 올리느라 `ready`까지 수 초, 첫 아바타 연결은 영상 모델을 올리고 예열하느라 더 오래 걸린다(시간을 따로 재지는 않았다. 아바타 통합 시험 전체가 약 1분).

### 음성 모듈 → 엔진

| 호출 | 언제 |
|---|---|
| `engine.reply(text, language=, speculative=, t_user_end=)` | 인식된 발화마다. `language`는 설정이 `ko`/`en`이면 그 값, `auto`면 인식기가 들은 언어. `t_user_end`는 말이 끝난 시각(epoch). 음성 채널로 온 `chat` 메시지도 같은 길로 간다 |
| 반복 취소 (`task.cancel()` 뒤 `aclose()`) | 끼어들기, 새 발화, `clear`, 선제 응답 폐기, 연결 종료 |
| `engine.truncate_last_reply(heard)` | `unmute`/`unmute_gate`에서 브라우저의 `played` 보고가 온 뒤, 답이 끝까지 들리지 않았을 때만. 하나도 안 들렸으면 `""` |
| `engine.mark_output("filler" \| "reply", time.time())` | 그 소리가 실린 첫 청크를 보내기 직전. 선제 응답은 확정된 뒤에만 |
| `engine.recorder` (없거나 `None`이어도 됨) | `recorder.audio(session_id, turn, "user"\|"agent", pcm16)`, `recorder.span(...)` (kind `endpoint`, `stt`, `tts`) |
| `engine.discard_last_turn()` (계약 밖, 있으면 씀) | 선제 응답이 이미 `done`까지 간 뒤에 사용자가 말을 이었을 때. 취소할 반복이 없어서 필요하다 |

엔진에 기대하는 것:

- `engine.turn`은 `reply()`의 첫 이벤트가 나오기 전에 올라가 있다(음성과 구간의 턴 번호를 첫 이벤트 때 읽는다).
- 취소된 선제 응답은 이력에 남지 않고 `turn`도 되돌아간다.
- `mark_output`은 같은 phase로 두 번 불려도 된다.
- `interrupt` 구간은 엔진이 `truncate_last_reply` 안에서 기록한다(WP4가 그렇게 한다). 음성 모듈은 기록하지 않는다. 두 번 남지 않게 하려는 것이다.
- `tool` 이벤트의 `data`에 `state`("start"/"done")가 있다. `effect == "write"`, `needs_confirmation`, `executed` 중 하나가 있으면 변경 도구로 본다(아래 R-RT-11).

## 2. 계약·문서와 다른 점

| 무엇 | 문서 | 실제 | 이유 |
|---|---|---|---|
| `serve_voice` 인자 | PLAN은 `(ws, engine, channel, settings)`, 작업 지시는 `(ws, engine, channel)` | `settings`는 선택. 시험용 키워드 `parts=`, `avatar_holder=` 추가 | 두 형태를 다 받는다 |
| `VoiceSession` 생성자 | `(engine, send_json, send_bytes, settings, avatar=None)` | 뒤에 키워드 `parts=`, `chunk_seconds=` 추가 | 가짜 음성 부품 주입 |
| VAD | "모델 하나, 세션별 상태" | Silero의 **ONNX** 모델 하나를 나눠 쓰고, 세션마다 순환 상태와 64샘플 문맥만 따로 둔다(`audio.Vad`). v1은 JIT 모델 | JIT 모델은 상태가 모델 안에 있어 나눠 쓸 수 없다. 임계값과 판정 규칙은 v1 그대로 |
| 무음 때 `interrupt` | v1은 새 발화마다 `interrupt`를 보내고 `played`를 기다림 | 들릴 것도 준비 중인 것도 없으면(답이 끝났고 마지막 청크가 재생된 뒤 0.5초가 지남) 보내지 않는다 | 음성 채널은 무음 때 청크가 없어 브라우저가 보고할 것이 없고, `played`를 안 보내는 클라이언트는 턴마다 0.5초를 잃는다 |
| 늦은 `played` | v1은 언제 와도 잘랐다 | 다음 답이 시작된 뒤(0.5초 대기 후)에 온 보고는 버린다 | `truncate_last_reply`는 "마지막 답"만 가리키므로 늦은 보고는 엉뚱한 답을 자르게 된다. 이 경우 끊긴 답은 엔진이 취소 때 남긴 그대로다 |
| `FHAU` 청크 길이 | "박자는 0.48초 단위" | 말이 이어지는 동안은 0.48초(15360바이트). 말의 마지막 청크는 남은 만큼만(채우지 않음) 보내고, 다음 청크는 그 길이만큼 뒤에 보낸다 | 무음을 보내지 않기 위해서. 문장 사이 0.3초 쉼은 음성에 들어 있다 |
| `ready` | 6절의 필드 | `chunk_seconds`, `settings`, 아바타는 `slice_samples`도 보낸다 | 화면이 박자와 현재 설정을 알 수 있게 |
| `speech`의 `phase` | "v1과 같음" | `filler`, `reply`, `answer`(조회 뒤의 답). 엔진에는 `answer`도 `"reply"`로 알린다 | v1 화면 표시를 유지 |
| `transcript` | `id, text, stt_ms, engine, language` | 선제 응답으로 나간 것에는 `speculative: true`가 붙는다 | v1과 같음 |
| 추임새 | `filler` 이벤트를 말한다 | 그 답에서 아직 아무 소리도 큐에 없을 때만 말한다 | v1 규칙. 답을 말하는 중의 "잠시만요"는 방해다 |
| `metric` 메시지 | 6절 표에 없음 | `backchannel_ignored`, `speculation_discarded`, `turn_end`를 v1처럼 보낸다 | v1 화면이 쓰던 것. 무시해도 된다 |
| 음성 넘기기 | `recorder.audio()` | (턴, 역할)마다 한 번만 부른다. 에이전트 음성은 **보낸** 음성이다(끊겨서 안 들린 꼬리도 그 청크까지는 들어 있다). 사용자 음성은 잘린 발화(앞 0.3초 포함) | 기록기가 같은 턴에 두 번 불렸을 때 덮는지 잇는지 계약에 없다 |
| `avatar.enabled` | 정의서 항목 | 보지 않는다. 채널이 `avatar`면 올린다. `avatar.image`만 쓴다(없으면 `examples/girl.png`) | D2: 채널이 요청할 때 붙인다. 막으려면 서버가 `enabled`를 보고 거절하면 된다 |

## 3. 못 끝낸 것, 알려진 문제

1. **R-RT-11, 확정 뒤의 변경 도구.** 선제 응답이 확정 전에 변경 도구를 건드렸으면(`tool` 이벤트로 확인) 준비한 답을 버리고 `speculative=False`로 다시 만든다(시험 있음). 그러나 확정된 **뒤에도** 생성이 계속되는 답은 엔진 안에서 여전히 `speculative=True`이므로, 그때 나온 변경 도구 호출은 게이트웨이가 거절한다. 실행되지는 않으니 안전하지만 고객은 한 번 더 말해야 한다. 고치려면 계약에 "확정" 호출(예: `engine.commit()`)이 필요하다.
2. **끝난 선제 응답의 턴 기록.** 선제 응답이 `done`까지 간 뒤 버려지면 `discard_last_turn()`으로 이력은 지워지지만, 이미 나간 `TurnRecord`는 남는다(WP4 메모와 같은 문제).
3. **아바타 첫 import의 `chdir`.** `flash_head/inference.py`가 import될 때 상대 경로로 설정 파일을 읽어서, v1 `Engine`을 처음 import하는 동안만 작업 폴더를 저장소 루트로 바꿨다가 되돌린다. 프로세스 전체에 걸리므로 그 짧은 순간 다른 스레드의 상대 경로가 어긋날 수 있다. v1을 고치지 않고는 다른 방법이 없다.
4. **기본은 반이중.** `barge_in`이 꺼져 있으면(기본) 에이전트가 말하는 동안 마이크를 듣지 않는다(v1과 같음: 스피커 소리를 자기 말로 듣지 않게). 말로 끼어들려면 `barge_in=1`(헤드폰).
5. **음성 채널의 가상 고객(R-EVAL-5 권장)**은 만들지 않았다. v1의 `auto` 소스를 옮기지 않았다.
6. 음성만 쓰는 채널도 16kHz로 내보낸다(인식, 기록기, 영상 모델과 같은 표본율). 음질을 올리려면 `FHAU`에 표본율을 싣는 계약 변경이 필요하다.
7. 대화 중 `config`로 `stt`를 `whisper`로 처음 바꾸면 모델을 올리는 동안 그 사이의 발화 인식이 늦어진다.
8. 영상 생성이 실시간보다 느려지면(GPU를 다른 일이 쓸 때) v1처럼 청크가 밀린다. 따로 대응하지 않았다. 시험에서는 0.96초 조각당 영상 생성 중앙값 0.25 ~ 0.38초였다(v1이 같은 GPU에 떠 있는 상태).

## 4. v1에서 옮기지 않은 것

- `auto` 소스 전체(가상 사용자, `FHGA`, `report_interrupt` 지표), `mic` 소스(`run_mic`: 마이크 음성으로 바로 입 모양), `speak` 메시지(글을 그대로 읽기).
- `chat()`의 내용 전부(프롬프트, LLM, 도구, 지식 검색, 검사, 길이·언어 지시): 엔진(WP4)의 일이다.
- 추임새 미리 합성(`warm_fillers`): 대신 처음 말할 때 합성하고 (목소리, 언어, 문장)별로 프로세스에 캐시한다. 지식 검색 예열(`keep_knowledge_warm`)도 엔진 쪽 일이라 없다.
- REST: `/api/image`(얼굴 사진 바꾸기), `/api/image/current`, `/api/tts`(목소리 목록), `/api/llm`. `audio.TTSClient.voices()`와 `AvatarHolder`가 `engine.set_image`를 부를 수 있게는 해 두었다.
- 잡음 게이트(`--gate_db`), `--max_backlog`.

v1에서 그대로 옮긴 것(동작 동일): `Endpointer`의 창·임계값·판정 표, `SmartTurn`, Whisper/Qwen 인식 호출, `TTSClient`, `split_for_tts`, `trim_silence`, 추임새 판정 정규식, 합성 큐와 세대 번호, `speech` 버퍼와 표시(`marks`), 청크별 음성 위치 장부(`chunk_speech`), `cancel_speech`, `apply_playback`(들린 글 계산), 선제 응답(`hold`, 준비·폐기·확정), `unmute_gate`와 `BARGE_IN_SECONDS`, `FHRT` 형식과 무음 청크, 박자와 즉시 전송(`kick`).

## 5. 설정과 자원

- 모델은 프로세스에 하나: Whisper(`audio.shared().whisper()`, GPU, 못 쓰면 CPU로 내려감), Silero VAD(ONNX, CPU), Smart Turn(ONNX, CPU), 영상 엔진(`avatar.holder()`). 인식은 한 줄로(스레드 1개), 합성 요청은 동시에 4개까지.
- 아바타 환경 변수(선택): `AGENTKIT_AVATAR_CKPT`, `AGENTKIT_AVATAR_WAV2VEC`, `AGENTKIT_AVATAR_MODEL_TYPE`(기본 `lite`), `AGENTKIT_AVATAR_IMAGE`. `config.py`를 고칠 수 없어 `avatar.py`가 직접 읽는다.
- 새로 설치한 패키지 없음.

## 6. 시험

```
cd v2 && CUDA_VISIBLE_DEVICES=0 python -m pytest tests/test_voice_*.py -q        # 31개, 약 2분
python -m pytest tests/test_voice_session.py -q                                  # 빠른 것만 27개, 약 20초, GPU 불필요
AGENTKIT_DATA_DIR=$(mktemp -d) AGENTKIT_VOICE_TEST_AGENT=shop-support CUDA_VISIBLE_DEVICES=0 \
  python -m pytest tests/test_voice_integration.py -q -s -k "one_turn or two_voice"   # 실제 엔진과 LLM으로
```

통합 시험은 7882 포트에 자기 앱을 띄우고 끝나면 내린다. Supertonic이 만든 음성을 0.1초 단위로 실시간 박자에 맞춰 보낸다.

측정값(2026-11-17, GPU 0에 v1이 함께 떠 있는 상태). "말 끝"은 마지막 유성 표본이 든 패킷을 보낸 시각이고, 고정 침묵 0.7초가 들어 있다.

| 경우 | 말 끝 → 첫 답변 음성 도착 |
|---|---|
| 음성, 가짜 엔진(LLM 없음) | 1.25 ~ 1.5초 (인식 0.06 ~ 0.17초, 첫 문장 합성 약 0.5초) |
| 음성 2개 동시, 가짜 엔진 | 1.3초와 1.9 ~ 2.0초 |
| 음성, Qwen3-ASR + Smart Turn | 1.0 ~ 1.1초 (침묵 0.32초에서 턴 종료) |
| 아바타, 가짜 엔진 | 2.2 ~ 2.4초 (첫 소리가 실린 `FHRT` 청크 도착) |
| 음성, 실제 `shop-support` 엔진(gemma4:26b) | 2.4초, 3.1초 (2회) |
| 음성 2개 동시, 실제 엔진 | 2.9초와 3.6초 |

실제 엔진으로는 PRD의 "중앙값 3초 이내"에 여유가 거의 없다. 표본이 몇 개뿐이라 중앙값이라 부를 수는 없다.

## 7. 화면 표시, 여는 말, 터치 (DISPLAY.md의 B 작업, 2026-11-17)

시험: `tests/test_voice_display.py`(8개, 가짜 엔진 7개와 실제 엔진+가짜 LLM 1개). `tests/voice_fakes.py`의 `FakeEngine`에
`open()`, `act()`(`opening_script`, `act_script`)가 생겼다. 기존 시험은 고치지 않았다.

- `ready`에 `screen`, `actions`, `opening`이 들어간다(글 채널과 같은 `runtime.client_features()`).
- 엔진의 `display` 이벤트는 `{"type":"display","id","kind","data","source"}`로 보낸다. 말로 하지 않는다. 선제 응답의
  것은 다른 메시지와 함께 확정될 때까지 붙잡아 둔다.
- **여는 말**: `serve_voice`가 `ready`를 보낸 직후 `session.start_opening()`을 부른다. 일반 답과 같은 경로(`_reply`,
  합성 큐)를 타므로 고객이 말을 걸면 끊기고, 끊기면 `truncate_last_reply`로 들은 데까지만 남는다. 메시지 id는
  `"opening"`, `chat_done.turn`은 0. 엔진이 `delta`를 내지 않으므로 `piece`를 `chat_delta`로도 보낸다. 여는 도구가
  있으면 첫 소리의 `speech.phase`는 `"answer"`(도구 뒤의 첫 소리)이고 `since_utterance_ms`는 `null`이다.
  `VoiceSession`을 직접 쓰는 쪽(시험)은 `start_opening()`을 스스로 불러야 한다.
- **터치**: 클라이언트의 `{"type":"action","id","name","arguments"}`는 `cancel_speech()`(끼어들기와 같은 처리: unmute
  방식이면 `interrupt`를 보내고 `played`로 자른다)를 한 뒤 `engine.act()`를 돌리고 그 답을 말한다. `id`가 없으면
  `action-N`을 붙인다. 정의서에 없는 동작 이름은 말을 끊지 않고 `chat_done`(`error`)만 보낸다. 엔진이 거절한
  터치(확인이 필요한 도구)는 말을 이미 끊은 뒤에 `chat_done`(`error`)이 간다.
- 알려진 문제: 여는 도구가 도는 짧은 동안(연결 직후 수십 ms) 끼어들기나 터치가 오면 여는 말 작업이 취소되어 남은
  여는 도구가 실행되지 않고 메뉴 화면이 빠질 수 있다. 글 채널은 여는 말을 끝까지 돌린 뒤 메시지를 읽으므로 해당 없다.
- 턴 0의 에이전트 음성은 `recorder.audio(session, 0, "agent", ...)`로 간다.
- **함께 고친 것(선제 응답의 `tts` 메시지)**: 붙잡아 둔 동안 합성된 문장의 `tts`(start, done) 메시지가 버려지고 있었다.
  이제 선제 응답의 다른 메시지와 함께 붙잡아 두었다가 확정되면 보낸다(버려지면 같이 사라진다).
  `test_voice_integration.py`의 두 시험이 이 때문에 실패하고 있었다(받은 음성 길이 ≠ `tts done`의 합). 인식이 빨라져
  (약 60 ms) 통합 시험의 턴이 선제 응답으로 처리되기 시작하면서 드러났다. 같은 이유로 `test_voice_channel_one_turn`의
  마지막 단정 `calls[0]["speculative"] is False`를 `calls[-1]["finished"] is True`로 바꿨다(기본 설정이 선제 응답 켬이다).
