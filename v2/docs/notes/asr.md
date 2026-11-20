# 음성 인식 품질 (뒤쪽): 메모

설계: `docs/ASR_QUALITY.md`. 이 메모는 표의 "A. 뒤쪽" 작업이 실제로 한 일, 설계와 다른 점, 남은 일이다.

파일: `agentkit/runtime/audio.py`, `voice.py`, `agentkit/trace/flags.py`(신규), `store.py`, `recorder.py`, `api.py`, `agentkit/eval/asr.py`(신규), `eval/api.py`, `agentkit/server.py`, `tests/test_asr_signals.py`, `test_asr_api.py`, `test_asr_voice.py`. 기존 시험 한 줄 수정: `tests/test_trace_store.py`(세션 목록 행에 `asr` 키가 생겨서 통째 비교하는 기대값에 추가).

## 1. 한 일

### 인식 신뢰도
- `audio.Transcript`: `tuple`을 상속해 `(text, language)`로 그대로 풀린다. `avg_logprob`, `no_speech_prob`, `compression_ratio`가 속성으로 붙는다. 기존 호출부와 가짜 인식기(그냥 튜플을 돌려주는 것)는 그대로 동작한다.
- Whisper: 세그먼트 전체(무음 확률 0.6 이상이라 글에서 뺀 세그먼트 포함)에 대해 로그 확률은 세그먼트 길이로 가중 평균, 나머지 둘은 최댓값. 세그먼트가 없으면 셋 다 null.
- Qwen: 셋 다 null. `low_confidence`는 `audio_s < 0.4`로만 정해진다.
- `audio.stt_quality(result, audio)`가 stt 구간 attrs에 넣을 값(`avg_logprob`, `no_speech_prob`, `compression_ratio`, `audio_s`, `rms_db`, `low_confidence`)을 만든다. `rms_db`는 발화 전체의 RMS(dBFS), 무음은 -120.
- 선제 응답: 신뢰도 값은 쉼 시점에 한 인식의 것이고, `audio_s`/`rms_db`는 확정된 발화 전체의 것이다.

### 음성과 글의 짝 (턴 번호)
- 고친 버그: 선제 응답이 확정될 때 stt/endpoint 구간을 `engine.turn + 1`에 기록했는데, 선제 응답이 이미 돌고 있으면 엔진은 그 턴을 이미 센 뒤라 구간이 **다음 턴 번호**에 붙었다(음성과 TurnRecord는 N, stt 구간은 N+1). 이제 `_reply`가 엔진 반복을 시작하기 직전의 `engine.turn`을 `request["turn_base"]`에 적어 두고, `_record_heard`는 `request["turn"]`(첫 이벤트 때 읽은 값) → `turn_base + 1` → `engine.turn + 1` 순으로 쓴다.
- 확인 방법: `tests/test_asr_voice.py::test_stored_audio_and_transcript_of_each_turn_belong_together`. 진짜 `Engine`(대본 LLM) + 진짜 recorder/store(임시 폴더) + `VoiceSession`(가짜 인식기)으로 말한 턴, 글로 보낸 턴, 인식 결과가 빈 소음, 말한 턴, 선제 응답으로 확정된 턴을 섞어 돌린다. 발화마다 길이와 표본 값이 다른 음성을 넣고, 끝난 뒤 저장소에서 턴 N의 `user_text`, 턴 N stt 구간의 `output.text`와 `audio_s`, `audio/pair/N-user.wav`의 표본 수와 값이 모두 같은 발화의 것인지 본다. 고치기 전 코드로 돌리면 이 시험과 `test_committed_speculative_turn_...`가 실패한다(구간 턴 [1,3,5] ≠ [1,3,4]).
- 음성 채널의 `chat_done`에 `turn`을 넣었다(화면이 턴 번호를 알 수 있게. 화면 쪽 요청).

### 턴 표시
- `trace/flags.py`에 신호 계산(`auto_signals`)과 문서 읽기/쓰기를 두었다. `eval/asr.py`는 이를 다시 내보낸다(설계는 신호 계산을 asr.py에 두라고 했지만 지표를 내는 trace 저장소가 써야 해서 trace 쪽에 두었다. trace → eval 의존을 만들지 않으려는 것).
- 세션이 끝날 때: recorder의 쓰기 스레드가 `end` 항목을 쓴 뒤 `flags.refresh`를 부른다. `flush()`가 돌아오면 표시도 저장돼 있다.
- 조회 시: `GET /api/sessions/{sid}`는 매번 신호를 다시 계산한다. 끝난 세션이면 저장된 문서의 `auto`가 없거나 다를 때 저장한다. 진행 중인 세션은 계산만 하고 저장하지 않는다.
- 신호가 하나도 없고 사람이 표시하지 않은 턴은 문서를 만들지 않는다(`{"auto": [], "misheard": null}`로 응답).
- `repeat`의 "글자 겹침 비율": 공백·문장부호를 뺀 글자에 대해 `difflib.SequenceMatcher.ratio()` ≥ 0.6.
- `empty_wait`: `runtime.guards.is_empty_wait(agent_text)`이고 그 턴에 `tool` 구간이 없을 때.
- 신호는 저장된(가려진) 글로 계산한다.

### 인식 사례, 다시 돌리기
- 사례 문서 kind `asr_case`, key = id = `<세션 id>-<턴>`. `mask=False`. `heard`는 그 턴 stt 구간의 `output.text`(저장소에 있는 것이라 가려진 글).
- 다시 돌리기 문서 kind `asr_bench`, key = run_id. 사례 하나를 끝낼 때마다 저장하므로 진행 중에도 `GET`으로 중간 결과가 보인다. 실행은 데몬 스레드.
- Whisper는 `shared().stt_pool`(음성 세션과 같은 한 줄짜리 풀)에서 올리고 부른다. 대화 중인 세션의 인식과 번갈아 돈다. Qwen은 `/health`가 답하지 않으면 `available: false`.

### 지표
- `asr`, `by_stt_engine`, `empty_wait_turns`, `empty_wait_rate`는 저장된 표시 문서가 아니라 턴과 구간에서 그 자리에서 계산한다(진행 중이거나 표시가 아직 저장되지 않은 옛 세션도 포함). `misheard`만 문서에서 읽는다.
- 분모: `asr`의 비율은 stt 구간이 있는 턴 수(`voice_turns`). `empty_wait_rate`는 전체 턴 수(글 대화 포함).
- `suspect`는 `low_confidence`, `repeat`, `reask`, `complaint` 중 하나라도 있는 턴. `empty_wait`는 에이전트 쪽 문제라 넣지 않았다.
- `by_version`, `by_day`의 항목마다 같은 키가 들어간다(`by_stt_engine` 포함).

## 2. 설계에 없는데 넣은 것 / 다른 점

| 항목 | 내용 |
|---|---|
| `GET /api/sessions` 행의 `asr` | `{suspect_turns, misheard_turns}`. 저장된 표시 문서만 센다(진행 중인 세션은 0). 화면 쪽 요청 |
| 음성 `chat_done`의 `turn` | 위 참고 |
| `POST .../flag` 응답의 `case` | 만든 사례 또는 null(그 턴에 사용자 음성이 없을 때) |
| `misheard: false` | 그 턴의 사례를 지운다 |
| `actual_text` 없이 `misheard: true` | 사례는 만들되 `expected`가 비어 있다. 다시 돌리기는 이런 사례를 건너뛰고 `skipped`에 id를 적는다. 이미 사례가 있으면 예전 `expected`를 유지한다 |
| 표시 문서의 `actual_text` | 다른 문서처럼 가려서 저장한다. 원문은 사례(`expected`)에만 남는다 |
| 사례의 추가 필드 | `language`, `updated_at` |
| 다시 돌리기 엔진 요약 | `exact`는 CER 0인 사례 **수**, `exact_rate`를 따로 둔다. `cer`는 (전체 편집 거리 ÷ 전체 기대 글자 수)이고 1을 넘을 수 있다 |
| 다시 돌리기 문서의 추가 필드 | `started_at`, `finished_at`, `total`, `done`, `agent`, `skipped`, `error`; 항목에 `session_id`, `turn`, `audio_url`; 엔진이 실패한 항목은 `results[engine] = {"error": ...}` |
| 프로세스가 죽어 멈춘 실행 | `status: "error"`로 응답한다(문서에는 `running`으로 남아 있다) |
| 지표의 `asr` | 설계의 여섯 키에 `suspect_turns`, `misheard_turns` 추가 |
| 모르는 엔진 이름 | 400 |

## 3. 실제로 본 값 (2026-11-17, Supertonic M1으로 합성한 한국어, Whisper large-v3-turbo)

- "주문한 상품이 떨어진 상태로 배달됐어요." → 그대로 인식. `avg_logprob -0.1262`, `no_speech_prob 0.0`, `compression_ratio 0.889`, `audio_s 3.622`, `rms_db -27.4`, `low_confidence false`.
- 0.3초짜리 약한 잡음 → "감사합니다."로 지어냄. `avg_logprob -0.3002`, `no_speech_prob 0.0`, `compression_ratio 0.64`, `rms_db -50.5`. **`low_confidence`가 참이 된 것은 길이(0.3초 < 0.4초) 때문뿐이다.** 신뢰도 값만 보면 정상 발화와 구분되지 않는다.
- 사례 두 개 다시 돌리기: Whisper CER 0.0(중앙값 131 ms), Qwen3-ASR CER 0.0(중앙값 126 ms). 합성음이라 둘 다 쉽다. 실제 오인식 사례로는 아직 돌려 보지 않았다.

## 4. 남은 일, 알려진 한계

- 위 잡음 결과처럼 설계의 기준값은 짧지 않은 잡음·중얼거림에서 Whisper가 지어낸 문장을 못 잡을 수 있다. `rms_db`를 기준에 넣을지는 실제 사례를 모은 뒤 정할 일이다(설계 기준을 바꾸지 않았다).
- 사례의 음성은 세션 녹음을 그대로 가리킨다. 보존 기간이 지나 세션이 지워지면(`purge`) 사례 문서도 같이 지워진다(문서의 `session_id`로). 사례를 오래 두려면 음성을 따로 복사해야 한다. 하지 않았다.
- 운영 세션(시험 아님)은 음성이 없어 표시는 되지만 사례가 되지 않는다(설계대로). `POST .../flag` 자체를 시험 세션으로 제한하지는 않았다.
- 턴 기록이 아직 없을 때(답이 끝나기 전) `POST .../flag`는 404다.
- 음성은 16 kHz로 저장된 것만 다시 돌린다(지금 저장되는 것은 모두 16 kHz).
- 다시 돌리기 실행을 취소하는 길은 없다. 동시에 두 실행을 막지도 않는다(Whisper는 풀이 한 줄이라 차례로 돈다).
- 포트 7871에서 돌고 있는 서버는 예전 코드다. 다시 띄워야 새 경로가 생긴다.
- 진짜 마이크 + 진짜 엔진으로 하는 음성 대화(브라우저)에서의 확인은 하지 않았다. 확인한 것은 가짜 인식기 + 진짜 엔진/저장소, 그리고 진짜 Whisper 단독이다.
