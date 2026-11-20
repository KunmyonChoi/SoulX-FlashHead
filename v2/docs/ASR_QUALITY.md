# 음성 인식 품질 개선 장치 (v2 추가 설계, 2026-11-17)

## 왜 필요한가

실제 음성 대화에서 인식이 자주 틀렸다("으으윽", "슬머세요.", "가져서 배달됐어요." → "뺏어져서 배달됐다고요." → "떨어진 상태로 배달됐다구요"). 그런데:

- 그 대화는 시험 세션이 아니어서 음성이 저장되지 않았다. 틀린 발화를 다시 들을 수도, 다른 인식 모델에 넣어 볼 수도 없다.
- 인식 신뢰도를 기록하지 않는다.
- "이 턴은 잘못 알아들었다"를 표시할 수단이 없고, 인식 품질 지표도 없다.

같은 대화에서 에이전트가 "잠시만 기다려 주세요"라고 하고 아무 일도 하지 않은 턴이 네 번 있었다. 엔진 쪽은 고쳤다(`runtime/guards.py`의 `is_empty_wait`, 답 끝에서 한 번 이어 가게 함). 이 현상도 지표로 잡아야 한다.

## 사용자 시나리오

1. 시험자가 대화 화면에서 음성 대화를 시작한다. "녹음해서 품질 개선에 쓰기"가 켜져 있고, 화면에 녹음 중임이 보인다.
2. 대화 중 에이전트가 잘못 알아들으면, 방금 자기 말풍선 옆의 "잘못 알아들음"을 누른다. 원하면 실제로 한 말을 적는다. 대화는 계속된다.
3. 대화가 끝난 뒤 모니터에서 그 대화를 연다. 턴마다 발화 음성을 들을 수 있고, 인식된 글, 인식 신뢰도, 소리 크기, 말 끝 판단이 보인다. 시스템이 의심한 턴(낮은 신뢰도, 같은 말 반복, 되물음, 불만 표현)에는 표시가 붙어 있다. 여기서도 "잘못 알아들음"과 실제 한 말을 적을 수 있다.
4. 표시한 턴은 인식 사례가 된다(음성 + 실제 한 말 + 당시 인식 결과).
5. 모니터의 "인식 품질" 보기에서 사례들을 인식 모델별(Whisper, Qwen3-ASR)로 다시 돌린다. 모델별 글자 오류율과 사례별 결과가 표로 나온다.
6. 추세에서 인식 의심 턴 비율, 사람이 표시한 오인식 비율, 되물은 비율, 같은 말 반복 비율, "기다리라고만 한 턴" 수를 에이전트·버전·인식 모델별로 본다.

## 녹음 정책 (PRD D5와의 관계)

- 운영 대화의 음성은 여전히 저장하지 않는다. 음성은 `test=1`(동의한 시험 세션)이나 가상 고객 대화에서만 저장된다. 이 규칙은 바꾸지 않는다.
- 달라지는 것은 기본값이다. 서버를 내부 시험용으로 띄우면(`AGENTKIT_INTERNAL_TESTING=1`, 기본값 1) 대화 화면의 녹음 토글이 켜진 상태로 시작한다. `/api/health`가 `internal_testing`을 알려 준다. 운영 배포에서는 0으로 둔다.
- 인식 사례의 "실제 한 말"과 음성은 가리지 않고 저장한다(재생과 비교에 원문이 필요하다). 시험 세션에서만 만들 수 있다.

## 데이터와 API

### 인식 구간(`Span kind="stt"`)의 `attrs`에 추가

| 키 | 뜻 |
|---|---|
| `avg_logprob` | Whisper 세그먼트 평균 로그 확률(낮을수록 불확실). Qwen은 없음(null) |
| `no_speech_prob` | 말이 아닐 확률(최댓값) |
| `compression_ratio` | 반복이 많은 지어낸 문장일수록 큼 |
| `audio_s` | 발화 길이(초) |
| `rms_db` | 발화의 평균 크기(dBFS) |
| `low_confidence` | 위 값으로 판단한 참/거짓. 기준: `avg_logprob < -0.8` 또는 `no_speech_prob > 0.5` 또는 `compression_ratio > 2.4` 또는 `audio_s < 0.4` |

### 턴 표시 (documents `kind="turn_flag"`, key `<session_id>/<turn>`)

```json
{"session_id": "...", "turn": 3,
 "auto": ["low_confidence", "repeat", "reask", "complaint", "empty_wait"],
 "misheard": true, "actual_text": "떨어진 상태로 배달됐다고요", "note": "", "updated_at": 0}
```

`auto`는 세션이 끝날 때(그리고 조회 시 없으면) 계산한다.

- `low_confidence`: 그 턴의 stt 구간이 `low_confidence`
- `repeat`: 사용자 말이 바로 앞 사용자 말과 비슷하다(글자 겹침 비율 0.6 이상, 둘 다 4자 이상)
- `reask`: 에이전트 답이 되묻는 표현("다시 한번 말씀", "잘 전달되지", "못 알아", "다시 말씀")
- `complaint`: 사용자 말이 불만 표현("아까 말했", "말했잖", "몇 번을", "언제까지 기다")
- `empty_wait`: 에이전트 답이 기다리라는 말로 끝났고 그 턴에 도구 호출이 없다(`runtime.guards.is_empty_wait`)

`misheard`, `actual_text`, `note`는 사람이 넣는다.

### REST

| 경로 | 내용 |
|---|---|
| `GET /api/health` | `internal_testing: bool` 추가 (server.py) |
| `GET /api/sessions/{sid}` | 턴마다 `flags`(위 문서, 없으면 `{"auto": [], "misheard": null}`) 추가. 음성이 있으면 기존 `audio` 목록 그대로 |
| `POST /api/sessions/{sid}/turns/{turn}/flag` | 본문 `{misheard: bool, actual_text?: str, note?: str}` → `{ok, flags}`. `misheard`가 참이고 그 턴에 사용자 음성이 있으면 인식 사례를 만들거나 갱신한다 |
| `GET /api/metrics` | `asr: {voice_turns, suspect_rate, misheard_rate, reask_rate, repeat_rate, low_confidence_rate}`와 `empty_wait_turns`, `empty_wait_rate` 추가. `by_version`에도, 그리고 `by_stt_engine: {whisper: {...}, qwen: {...}}` |
| `GET /api/asr/cases?agent=` | `{cases: [{id, session_id, turn, agent_id, expected, heard, engine, audio_url, audio_s, created_at}]}` |
| `DELETE /api/asr/cases/{id}` | 사례 삭제 |
| `POST /api/asr/bench` | 본문 `{engines?: ["whisper", "qwen"], agent?: str}` → `{run_id}`. 백그라운드 실행 |
| `GET /api/asr/bench/{run_id}` | `{run_id, status: "running"|"finished"|"error", engines: {name: {n, cer, exact, ms_median, available}}, items: [{case_id, expected, heard_then, results: {engine: {text, cer, ms}}}]}` |
| `GET /api/asr/bench` | `{runs: [...최근 실행 요약]}` |

글자 오류율(CER)은 공백과 문장부호를 뺀 글자 기준 편집 거리 ÷ 기대 글자 수.

인식 다시 돌리기는 서버 프로세스가 이미 올린 인식기(`runtime.audio.shared()`)를 쓴다. Qwen 서버가 꺼져 있으면 `available: false`.

## 파일 소유 (이번 작업)

| 작업 | 파일 |
|---|---|
| A. 뒤쪽 | `agentkit/runtime/audio.py`, `voice.py`(stt attrs, 사용자 음성 넘기기), `agentkit/trace/`(턴 표시, 지표), `agentkit/eval/asr.py`(신규: 신호 계산, 사례, 다시 돌리기) + `agentkit/eval/api.py`에 라우트 추가, `agentkit/server.py`(health), `tests/test_asr_*.py` |
| B. 화면 | `web/index.html`, `web/monitor.html`, `web/common.js`, `web/app.css`, `tests/mock_api.py`, `tests/test_web_*.py` |

`agentkit/runtime/engine.py`, `guards.py`, `contracts.py`는 건드리지 않는다.
