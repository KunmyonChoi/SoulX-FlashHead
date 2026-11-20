# WP7 화면: 메모

## 만든 것

| 파일 | 내용 |
|---|---|
| `web/index.html` | 대화 화면 (글, 음성, 아바타) |
| `web/monitor.html` | 모니터 (대화 목록, 턴 카드, 분석, 추세) |
| `web/studio.html` | 제작실 (버전, 배포 단계, 차이, 시험, 제작 에이전트) |
| `web/app.css`, `web/common.js` | 세 화면이 함께 쓰는 모양과 API 호출 |
| `tests/mock_api.py` | 시험용 가짜 서버 (REST, `/ws`, `/api/live`), 포트 7884 |
| `tests/test_web_support.py` | 시험 도우미와 실행기 |
| `tests/test_web_index.py`, `test_web_monitor.py`, `test_web_studio.py` | 헤드리스 Chromium 시험 27개 |
| `tests/screenshots/*.png` | 시험이 남기는 화면 35장 |

프레임워크, 빌드, CDN 스크립트 없음. 글꼴만 Google Fonts 스타일시트(IBM Plex Sans KR)를 화면을 막지 않는 방식으로 불러오고, 실패하면 시스템 글꼴을 쓴다.

## 시험 실행

시스템 파이썬에 pytest가 없어서 실행기를 따로 두었다. 시험 함수는 인자가 없는 `test_*`이므로 pytest가 있으면 그대로 돈다.

```
cd v2 && python3 tests/test_web_support.py            # 전부 (지금 환경에서 쓰는 방법)
cd v2 && python3 tests/test_web_support.py studio     # 이름에 studio가 든 것만
cd v2 && python3 -m pytest tests/test_web_*.py -q     # pytest가 설치되어 있을 때
```

실행기는 7884에 아무것도 없으면 가짜 서버를 `/data/miniforge3/envs/flashhead/bin/python`으로 띄우고 끝나면 내린다.

## 서버에 기대는 주소

화면은 서버가 주는 주소 `/`, `/monitor`, `/studio`, `/web/app.css`, `/web/common.js`를 쓴다(화면 사이 링크와 스타일 경로가 이 주소로 적혀 있다).

## 기대는 API 필드

실제 모듈(WP1, 2, 3, 4, 6)의 `api.py`가 생긴 뒤 읽어서 맞춘 것은 "확인함"으로, 문서만 보고 정한 것은 "가정"으로 적었다. 통합 서버에 직접 붙여 돌려 보지는 않았다.

### 공통

- 404에 `{"detail": "Not Found"}`(FastAPI가 경로를 못 찾았을 때의 답)이거나 본문이 JSON이 아니면 "기능이 아직 설치되지 않음"으로 본다. 다른 `detail`이 있는 404는 "그 대상이 없음"으로 본다. 405와 503도 "설치되지 않음"으로 본다(WP6이 필요한 모듈이 없을 때 503을 낸다, 확인함).
- 409는 거절이다. 사유는 `detail`(글자 또는 `{message}`), 없으면 `message`, `error`, `reason` 순으로 찾는다.
- 요청 제한 시간 20초. 분석 실행 180초, 제작 에이전트 300초.

### WP1 (확인함: wp1이 알려 준 형태)

- `GET /api/agents` → 배열 또는 `{agents: []}`. 항목의 `id`, `name`, `description`, `deployment.stable`.
- `GET /api/agents/{id}` → `versions[]`(VersionInfo 전부: `version`, `state`, `created_at`, `created_by`, `based_on`, `note`, `safety_change`, `changed_fields`, `test_run_id`, `approved_by`, `approved_at`), `deployment`(`stable`, `canary`, `canary_percent`, `previous_stable`).
- `GET /api/agents/{id}/versions/{n}` → AgentDefinition. 쓰는 필드: `version`, `tools`, `knowledge.kind`, `knowledge.name`, `avatar.enabled`, `voice.{stt, endpoint, interrupt_mode, barge_in, speculative}`, `evaluation.latency_budget_ms`, `evaluation.success[].{id, rule, judge}`, `tests.personas`, `tests.cases`(개수만).
- `GET /api/agents/{id}/diff?a=&b=` → `changes: {필드: {before, after}}`, `safety_fields[]`. `safety_fields`가 없으면 화면이 직접 판단한다(`tools`, `handoff`, `prohibitions`, `guards`, 그리고 `evaluation` 안의 `safety`/`pii` 항목 변화).
- `POST /api/releases/{id}/{n}/submit|approve|deploy`, `POST /api/releases/{id}/rollback`. 본문 `{approver}` / `{percent}` / `{}`. 응답 본문은 쓰지 않고 성공하면 `GET /api/agents/{id}`를 다시 받는다.

### WP2 (확인함)

- `GET /api/tools` → `{tools: []}` 또는 배열. 항목의 `name`, `effect`, `description`. 이 주소가 없어도 대화 화면의 갈래는 정의서의 도구 이름으로 그려진다.

### WP3 (확인함: `agentkit/trace/api.py`, `store.py`)

- `GET /api/sessions?agent=&status=&limit=100` → `{sessions: []}` 또는 배열. 행의 `session_id`, `agent_id`, `version`, `channel`, `test`, `simulated`, `label`, `started_at`, `status`, `turns`(또는 `turn_count`), `response_ms_median`, `analysis.{total, failed, unjudged, safety_failed}`. 목록의 통과 표시는 `failed > 0`이면 실패, 아니고 `total > 0`이면 통과, `analysis`가 없으면 "분석 없음"이다. 채널과 종류(운영, 시험, 가상) 거르기는 화면에서 한다.
- `GET /api/sessions/{sid}` → `meta`(SessionMeta), `status`, `turns[]`(TurnRecord), `spans[]`(Span), `audio[]`(`{turn, role}`; 있으면 그 턴에만 재생기를 둔다).
- 구간별로 읽는 값: `endpoint.attrs.{mode, silence_s, probability}`, `stt.output.text`, `stt.attrs.{engine, language}`, `retrieval.input.query`, `retrieval.output.{sources, top_score}`, `retrieval.attrs.used`, `llm.output.{text, tool_calls[].{name, arguments}}`, `llm.attrs.{model, round, first_token_ms}`, `tool.input.arguments`, `tool.output.{text, needs_confirmation, confirmation_text, executed, error_type}`, `tool.attrs.{effect, executed, error_type}`, `guard.input.sentence`, `guard.attrs.{guard_id, action}`, `tts.input.chars`, `tts.attrs.seconds`, `output.attrs.phase`, `interrupt.attrs.heard_chars`, `handoff.attrs.{rule_id, reason}`. 모르는 값은 "저장된 원문 보기"에 JSON 그대로 나온다.
- `GET /api/sessions/{sid}/audio/{turn}/{role}` → WAV. `meta.test` 또는 `meta.simulated`이고 채널이 글이 아닐 때만 재생기를 둔다.
- `GET /api/metrics?agent=&days=` → `by_version[]`(또는 `groups`, `rows`)의 `agent_id`, `version`, `sessions`, `response_ms_median`, `span_ms.{kind}.median`, `tool_failure_rate`, `criteria_pass_rate`, `handoff_rate`. `by_day[]`(또는 `daily`)의 `day`(또는 `date`), `response_ms_median`, `sessions`.
- `GET /api/live` → `data: {type, session_id, ...}`. `session_id`가 있으면 목록을 다시 받고, 열려 있는 대화면 상세도 다시 받는다. `type: "analysis"`는 `key`를 세션으로 본다. `hello`, `audio`는 무시한다. 내용은 쓰지 않고 "바뀌었다"는 신호로만 쓴다.

### WP6 (확인함: `agentkit/eval/api.py`)

- `GET /api/sessions/{sid}/analysis` → AnalysisResult(`summary`, `extracted`, `criteria[].{id, passed, reason, evidence_turns, safety}`, `metrics`, `created_at`). 분석이 없을 때의 404는 `detail`이 "Not Found"가 아니어야 "아직 분석 없음"으로 보인다(지금 그렇다).
- `POST /api/sessions/{sid}/analyze` 본문 `{}`. 끝나면 `GET .../analysis`를 다시 받는다.
- `POST /api/sessions/{sid}/save_case` 본문 `{case_id, expect: {기준 id: bool}}`. `facts`, `description`은 보내지 않는다.
- `POST /api/tests/run` 본문 `{agent, version}` → `run_id`.
- `GET /api/tests/runs?agent=` → `{runs: []}` 또는 배열. `GET /api/tests/runs/{run_id}` → TestRun + `status`(`running`, `finished`, `error`, `interrupted`), `total`, `skipped`, `error`. `status`가 없으면 `finished_at`으로 끝났는지 본다. 1.2초마다 다시 받는다.
- `GET /api/tests/compare?base=&new=` → Comparison. `metric_changes`는 `{이름: [기준, 새]}`.

### WP4, WP5: `/ws?agent=&channel=&version=&test=0|1`

- 받는 메시지: `ready`(`session_id`, `version`, 음성 `sample_rate`, 아바타 `fps`, `width`, `height`), `chat_delta`, `chat_replace`, `chat_done`(`id`, `text`), `filler`(`id`, `text`; 문서 표에는 없지만 WP4 서버가 보낸다), `llm`(`state`, `model`, `round`, `tools`, `ms`, `chars`), `knowledge`(`state`, `query`, `ok`, `sources`, `top`, `threshold`, `ms`, `has`, `message`), `tool`(`state`, `name`, `arguments`, `ok`, `ms`, `result`, `items`, `effect`, `executed`, `needs_confirmation`, `confirmation_text`, `error_type`), `tts`(`state`, `chars`, `filler`, `seconds`, `ms`), `speech`(`id`, `seq`, `offset`, `phase`, `since_utterance_ms`, `stt_ms`, `llm_ms`, `synth_ms`, `tool_ms`, `speculative`), `transcript`, `listen_state`, `metric`(`name: "turn_end"`의 `probability`, `silence`), `interrupt`(`seq`), `chat_truncate`, `handoff`(`rule_id`, `reason`), `notice`(`message`), `error`(`message`, `id`).
- `error`에 `id`가 있으면 그 답만 실패로 보고 대화를 이어 간다. `id`가 없으면 안내만 띄우고, 이어서 연결이 닫히면 그 글을 닫힌 사유로 보여 준다.
- 보내는 메시지: `{type:"chat", id, text}`, `{type:"config", barge_in, interrupt_mode, speculative, endpoint, stt}`(음성과 아바타, `ready` 직후와 설정을 바꿀 때), `{type:"played", seq, offset}`, `{type:"clear"}`, 16kHz 16비트 PCM 0.1초 묶음.
- 이진: 앞 4바이트가 `FHAU`면 `<II>`(seq, PCM 바이트 수) + PCM, `ready.sample_rate`로 재생. 아니면 v1의 `FHRT`(28바이트 머리, PCM 16kHz, JPEG 프레임).

### WP8: `POST /api/builder/chat` (가정, 모듈이 아직 없음)

- 보냄: `{message, agent?, conversation_id?}`. "새 에이전트를 만드는 대화"를 켜면 `agent`를 뺀다.
- 받음(가정): `reply`(없으면 `message`, `text`), `conversation_id`, `agent_id`(또는 `agent`), `version`, `safety_change`, `changed_fields`. `version`이 오면 그 버전을 고르고, 지금과 다른 `agent_id`가 오면 그 에이전트로 옮긴다. `reply`만 와도 동작한다.

## 문서에 없어서 정한 것

1. 변경 도구의 동의 대기와 실행을 **도구 이름**으로 짝짓는다. 한 대화에서 같은 변경 도구의 제안 둘이 동시에 기다리는 경우는 구분하지 못한다(뒤의 것이 앞 카드를 대신한다). `tool` 메시지에 `call_id`가 있지만 두 단계가 같은 값을 쓰는지 몰라 쓰지 않았다.
2. 동의 대기 카드에 "동의한다고 답하기"("네, 그렇게 해 주세요."), "하지 않는다고 답하기"("아니요, 하지 않을게요.") 버튼이 있다. 고객의 말로 글을 보내는 것이고 서버의 동의 판정을 그대로 탄다.
3. 넘김 뒤에는 입력창만 막는다. 연결과 마이크는 "정지"를 누를 때까지 그대로다.
4. 정의서의 `avatar.enabled`가 거짓이면 아바타 채널을 고를 수 없게 했다. 정의서를 못 받으면 막지 않는다.
5. 버전 선택이 비어 있으면 `version` 없이 연결한다(서버가 배포 버전을 고른다). 배포된 버전이 없는 에이전트는 가장 새 버전이 골라진다.
6. 지연 기준은 그 대화의 에이전트·버전 정의서에서 읽는다. 못 받으면 2.5초를 쓰고 그렇게 표시한다.
7. 턴의 지연은 `t_agent_start - t_user_end`. 지연 막대는 그 턴의 구간을 시각대로 놓은 것이고, 한 대화 안에서는 모든 턴이 같은 눈금을 쓴다. 합성(`tts`)은 아랫줄, 첫 출력·끼어들기·넘김은 세로선이다.
8. 추세의 "앞 버전과 비교"는 같은 에이전트의 바로 앞 행과 비교한다. 지연과 도구 실패율은 낮을수록, 통과율은 높을수록 좋게 칠하고, 넘김 비율은 좋고 나쁨을 칠하지 않는다.
9. 제작실의 단계 버튼(시험 실행, 승인 요청, 승인, 배포)은 상태와 상관없이 누를 수 있고, 되는지는 서버가 정한다. 화면은 승인자 이름이 비었는지와 비율이 1~100인지만 먼저 본다.
10. 비교 기준 시험은 고른 버전의 `based_on` 버전 시험, 없으면 운영 버전의 시험이 처음에 골라진다.
11. 시험 사례 저장의 기대 판정은 기준마다 "통과해야 함"이 기본이다.

## 못 끝낸 것, 알려 둘 것

- **실제 서버로는 돌려 보지 않았다.** 모든 시험은 가짜 서버 기준이다. 가짜 서버의 응답 형태는 위 "확인함" 항목의 실제 코드에 맞췄지만, 통합 서버(7871)에 화면을 붙여 확인하는 일은 남았다.
- **실제 마이크 음성으로 확인하지 않았다.** 음성 채널 시험은 Chromium의 가짜 마이크로 PCM이 나가는 것과, 가짜 서버가 보낸 `FHAU` 3청크가 재생 일정에 올라가는 것까지 본다. 아바타 시험은 `FHRT` 프레임이 캔버스에 그려지는 것까지 본다. 소리가 실제로 들리는지, 박자가 맞는지는 사람이 들어야 한다.
- **끼어들기(`interrupt` → `played` 보고, `chat_truncate`)는 v1 코드를 옮겼지만 시험이 없다.** 가짜 서버가 끼어들기를 흉내 내지 않는다.
- `interrupt` 처리에서 v1과 달라진 점 하나: 재생 중인 청크를 찾을 때 이미 끝난 청크는 빼도록 했다(v1은 마지막으로 시작한 청크를 그대로 보고했다). 아무것도 재생 중이 아니면 `seq: null, offset: 0`을 보낸다.
- 음성만 받는 `FHAU`는 밀려도 건너뛰지 않고 이어 붙인다. v1의 "두 청크 넘게 밀리면 건너뛰기"는 영상(`FHRT`)에만 남겼다.
- v1의 자율 대화, 텍스트 읽기, 마이크 따라 말하기, 기준 이미지 올리기, 음성·모델·언어·답변 길이 고르기는 옮기지 않았다. v2 API에 해당 주소가 없다. 아바타 화면은 시작 전에 검은 칸이다.
- 음성 설정 서랍은 인식 모델, 말 끝 판단, 끼어들기 방식, 말하는 중 듣기, 선제 응답만 있다. 서버가 `config`의 이 키들을 받는지는 확인하지 않았다(WP5의 `serve_voice`).
- 모니터의 날짜별 그래프는 WP3의 `by_day`가 고른 조건 전체의 하루 값만 주므로 선이 하나다. 버전별 선을 그리는 코드는 있지만 지금 API로는 쓰이지 않는다.
- 실패한 대화를 유형별로 묶어 보기(R-BLD-6, 권장)는 없다.
- 모니터 목록은 최근 100건만 받는다. 더 불러오기는 없다.
- `/api/live`가 404면 10초마다 다시 받는 방식으로 바뀐다. 연결이 끊긴 경우는 브라우저가 다시 연결하고, 다시 붙으면 목록을 새로 받는다.
- 전체 화면 스크린샷에서 모니터의 대화 목록이 세로 가운데쯤 찍힌 것은 화면에 붙어 따라오는 칸을 전체 높이로 찍을 때 생기는 모습이고, 실제 화면에서는 위에 붙어 있다.
- 접근성은 키보드 초점 표시(시험으로 확인), 레이블, `aria-live` 정도까지다. 화면 읽기 프로그램으로 확인하지 않았다.

## 음성 인식 품질 화면 (2026-11-17, `docs/ASR_QUALITY.md`의 B)

### 만든 것

- 대화 화면: 녹음 토글 이름을 "녹음해서 품질 개선에 쓰기"로 바꿨다. 처음 값은 주소의 `test=1` > 이 브라우저에서 마지막으로 고른 값(`localStorage`의 `agentkit.record`) > `GET /api/health`의 `internal_testing` 순으로 정한다. `/api/health`가 없거나 필드가 없으면 꺼진 채로 시작한다.
- 대화 화면: 음성·아바타 채널에서 음성 인식으로 생긴 고객 말풍선 아래(고객 쪽, 오른쪽 정렬)에 "잘못 알아들음"이 붙는다. 누르면 바로 `POST /api/sessions/{sid}/turns/{turn}/flag {misheard: true}`를 보내고 한 줄 입력칸을 연다. 저장하면 `{misheard: true, actual_text}`를 다시 보낸다. 초점을 옮기지 않고, 대화가 끝난 뒤에도 쓸 수 있다. 녹음하지 않는 대화면 음성이 남지 않는다고 알린다. 글로 입력한 말풍선에는 붙지 않는다.
- 모니터 턴 카드: 자동 신호 표시(점선 호박색 "?"), 사람이 표시한 오인식(빨간 채움), 고객 줄의 "잘못 알아들음"과 입력칸, 인식된 글 옆 재생기, 그 아래 한 줄 요약(인식 모델, 확신도, 소리 크기, 길이, 말 끝 판단). 단계별 보기의 음성 인식 구간에 다섯 값을 풀어 쓰고 기준을 벗어난 줄을 빨갛게 표시한다.
- 모니터 목록: 의심·표시된 턴 수 표시와 "인식" 거르기.
- 모니터 "인식 품질" 보기(`/monitor?view=asr`): 사례 목록(재생, 실제 한 말, 그때 인식, 실제 한 말 고치기, 대화에서 보기, 두 번 묻는 삭제), "다시 돌려 비교", 모델별 표, 사례별 표(다른 글자와 빠진 글자 표시), 지난 비교. 들어오면 가장 최근 비교를 보여 준다.
- 추세: 인식 품질 칸(전체 수치, 버전별 표, 인식 모델별 표)과 기다리라고만 한 턴.
- 사용자가 실제 화면을 보고 알려 준 것 둘도 고쳤다. (1) 무대 뒤의 조회·도구 줄 묶음이 "답변 생성" 점에서 내려오도록 점의 위치를 재서 붙인다(`placeLanes`, 창 크기가 바뀌면 다시 잰다). 오른쪽에 380px 이상 남으면 묶음 전체가 그 점 아래에 오고, 좁으면 묶음은 전체 폭을 쓰고 가로선으로 잇는다. (2) 아바타 채널의 "영상 생성" 점은 영상 청크가 오는 동안 호박색("만드는 중 · 청크당 Nms", `FHRT` 머리의 `infer_ms`), 소리가 든 청크가 재생되는 동안 빨간색이다.
- `app.css`, `common.js` 주소에 `?v=asr4`를 붙였다. 예전 스타일이 브라우저에 남아 새 버튼이 기본 버튼 모양(흰 알약)으로 보이는 일을 막으려는 것이다.

### 턴 번호

`transcript`나 `chat_done`에 숫자 `turn`이 있으면 그 값을 쓴다(`chat_done`이 나중에 오면 그 값으로 고친다). 없으면 이 세션의 고객 말(음성과 글 입력 모두)을 1부터 센 값이다. 뒤쪽 작업이 음성 경로의 `chat_done`에 `turn`을 넣었다고 알려 왔으므로 실제로는 답이 끝나기 전에 누른 표시만 센 값으로 나간다. 그때 서버가 404를 주면 그 답의 `chat_done`이 온 뒤 한 번 다시 보낸다.

### 기대는 필드 (뒤쪽 작업이 알려 준 형태, 실제 서버에 붙여 보지는 않음)

- `GET /api/sessions` 행의 `asr: {suspect_turns, misheard_turns}` (문서에 없음, 뒤쪽 작업과 맞춤). 없으면 표시가 안 보일 뿐이다.
- `turn.flags.{auto[], misheard, actual_text}`, flag 응답의 `{flags, case}`.
- 비교: `engines[name].{n, cer, exact(건수), exact_rate, ms_median, available}`, `items[].results[name].{text, cer, ms}` 또는 `{error}`, 실행의 `started_at, total, done, skipped[], error`. `GET /api/asr/bench`의 `runs[].{run_id, status, started_at, total, engines}`.
- 지표: `asr.{voice_turns, suspect_turns, suspect_rate, misheard_turns, misheard_rate, reask_rate, repeat_rate, low_confidence_rate}`, `empty_wait_turns`, `empty_wait_rate`, `by_version[]`의 같은 값, `by_stt_engine.{whisper, qwen}`.

### 정한 것, 남은 것

- 다른 글자 표시는 화면에서 계산한다(글자와 숫자만 최장 공통 부분열로 맞춤). 서버의 오류율과 같은 기준이지만 편집 거리의 정렬과 꼭 같지는 않다.
- "소리가 든 청크"는 PCM 크기로 판단한다(절댓값 300 초과 표본이 있으면). 실제 아바타 서버가 쉬는 동안 보내는 청크의 소리가 완전한 무음인지 확인하지 않았다.
- 센 턴 번호로 이미 표시를 보낸 뒤 `chat_done`이 다른 번호를 알려 주면 앞의 표시는 옮기지 않는다.
- 사례의 "실제 한 말 고치기"는 그 세션·턴에 flag를 다시 보낸다. 세션이 지워진 사례에서는 서버가 404를 줄 수 있다.
- 인식 품질 보기는 실시간 알림을 듣지 않는다. 다른 곳에서 만든 사례는 보기를 다시 열어야 나온다.
- 시험은 가짜 서버 기준이다. 가짜 서버에 `/mock/set`(internal_testing, qwen, idle_video), `/mock/say`(말로 한 고객 턴)를 더했다. 새 시험은 `test_web_index.py`와 `test_web_monitor.py`에 넣었다(빠른 묶음이 이 두 파일을 이미 빼기 때문). 대화 목록 고정 자료가 7건에서 8건이 되어 기존 시험의 개수 두 곳을 8로 바꿨다.

## 화면 표시, 여는 말, 터치 (2026-11-17, `docs/DISPLAY.md`의 C)

### 만든 것

- 대화 화면: `ready.screen.panels`가 비어 있지 않으면 주문용 배치로 바뀐다. 왼쪽 넓은 칸이 메뉴(종류 버튼, 이름·설명·가격, 품절은 흐리게), 오른쪽 위가 대화(아바타나 음성 상태 포함), 오른쪽 아래가 주문 내역(줄, 수량, 옵션, 금액, 빼기, 할인이 있으면 주문 금액과 할인, 합계). 에이전트를 고르면 정의서의 `screen.panels`로 자리를 미리 보여 주고, 연결되면 `ready`의 값을 따른다.
- 모든 내용은 `display` 메시지로만 그린다(`Screen.show`). 에이전트의 말은 화면을 바꾸지 않는다.
- 터치: `ready.actions`에 `add_item`이 있을 때만 메뉴가 버튼이다(없으면 `div`). 필수 옵션이 있으면 옵션 고르기가 먼저 뜬다. 필수 옵션은 비워 두고, 선택 옵션은 `default`를 미리 고른다. `{"type":"action","id","name":"add_item","arguments":{item_id, quantity, options}}`를 보낸다. 빼기는 `remove_item {line_id}`(`ready.actions`에 있을 때만 보인다).
- `items`: 해당 메뉴에 청록 테두리와 "찾은 메뉴" 표시, 첫 항목으로 스크롤, 위에 한 줄 설명. 다음 고객 턴(글, 음성 인식, 터치)에 지운다.
- 확인 카드와 영수증: 메뉴 칸 위에 뜬다(좁은 화면에서는 메뉴 목록 위에 끼워 넣는다). 확인 카드에는 버튼이 없고 "말이나 글로 네 또는 아니요"라고 적혀 있다. 사라지는 때: 그 도구가 실행됨, 같은 도구의 새 `confirm`이 옴(바꿔 그림), 뒤에 `cart`가 옴(주문이 바뀜). `not_confirmed`가 오면 흐리게 하고 "다시 확인 중"으로 바꾸며 메뉴를 다시 누를 수 있게 하고, 그 뒤 고객 턴이 그 도구를 부르지 않고 끝나면 지운다. 영수증은 "닫기"를 누르거나 새 확인 카드가 올 때까지 남는다(바로 뒤에 오는 빈 `cart`에는 지워지지 않는다).
- `notice`: 메뉴 칸 위의 한 줄, 7초 뒤 사라진다. 넘김(`handoff`)은 사라지지 않는 줄로 남고 터치를 막는다. 거절된 터치(`chat_done`의 `error`)도 한 줄로 보여 주고 빈 말풍선은 남기지 않는다.
- 개발자용 "무대 뒤" 칸과 시간 기록은 화면 에이전트에서는 닫혀 있고, 위쪽 막대의 "무대 뒤 보기"로 화면 아래에 붙여 연다. 대화 칸의 도구 줄(`.msg.tool`)도 화면 에이전트에서는 숨긴다. 시간 기록에는 `display`가 "화면에 표시 (종류): 요약"으로 남는다.
- 좁은 화면(980px 이하): 대화, 메뉴 순으로 쌓이고 주문 내역은 아래에 붙은 막대(개수와 합계)로 줄어든다. 막대를 누르면 주문 내역이 아래에서 올라온다. 옵션 고르기도 아래에서 올라온다.
- 화면이 없는 에이전트는 예전 그대로다(`body`와 `#stage`의 클래스가 그대로인지, 칸 위치가 같은지 시험한다). 그 에이전트에 오는 `confirm` 표시는 시간 기록에만 남고, 동의 카드는 예전처럼 대화 안에 뜬다.
- 뼈대 에이전트: `agentReadiness`가 `outline`이면 시작 버튼을 막고 "이 에이전트는 뼈대입니다. 도구 N개가 등록되지 않아 대화할 수 없습니다: …"를 안내 줄과 대화 칸에 보여 준다.
- 모니터: `display` 구간(메뉴는 종류별 개수, 찾은 메뉴는 이름과 가격, 주문 내역·확인·영수증은 줄과 합계), `action` 구간(무엇을 눌렀는지, 호출된 도구), 턴 카드의 "화면 터치"와 "화면 표시 N건" 표시. `attrs.refresh_of`가 있는 도구 구간은 들여 써서 "화면 갱신"으로 보여 주고 도구 횟수에서 뺀다. 0번 턴은 "시작" 카드다(턴 기록이 없고 구간만 있어도 만든다). 터치 턴과 시작 카드에는 "잘못 알아들음"이 없다.
- `common.js`: `won`, `DISPLAY_LABEL`, `displayText(kind, data)`, `KIND_LABEL`의 `display`·`action`. `app.css`: `--k-display`, `--k-action`과 화면 에이전트용 규칙. 주소의 `?v=`는 `screen7`.
- 에이전트 선택 칸은 380px(화면 에이전트에서는 230px)에서 줄여 쓴다. 준비 상태 글이 길어 좁은 화면이 옆으로 밀리던 것을 막는다.

### 가짜 서버와 시험

- `tests/mock_api.py`: `kiosk-order`(메뉴 13개, 품절 2개, 장바구니, 쿠폰, 확인 뒤 결제, 여는 말과 메뉴 표시, `action` 처리, 거절된 동작), 뼈대 `flight-rebooking`, 저장된 세션 `s-kiosk`(0번 턴, `display`·`action` 구간, `refresh_of`). `/mock/set?kiosk_actions=0`은 동작 없는 정의서로 바꾼다. 대화 목록 고정 자료가 9건이 되어 기존 시험의 개수 세 곳을 9로 바꿨다.
- `tests/test_web_screen.py` 24개. 전체 66개 통과(`python3 tests/test_web_support.py`).

### 정한 것, 남은 것

- 실제 서버(7871)로는 주문 흐름을 돌려 보지 못했다. 확인한 시점에 그 서버는 `GET /api/tools`가 `tools: []`와 `add_to_cart.yaml` 오류를 돌려주고 `kiosk-order` 정의서를 "알 수 없는 항목(opening, screen)"으로 거절했다(새 코드로 다시 띄우기 전으로 보인다). 그 상태에서는 모든 에이전트가 "도구 N개 미등록"으로 보이고 배포되지 않은 에이전트는 뼈대로 막힌다.
- 확인 카드에 "네/아니요" 버튼을 두지 않았다. 글 채널의 키오스크에서는 입력창에 직접 써야 한다.
- 선택 옵션만 있는 메뉴는 누르면 옵션 고르기 없이 `options: {}`로 바로 담는다(기본값은 서버가 채운다고 보았다).
- 정의서 오류(`broken`)인 에이전트는 시작을 막지 않는다. 서버가 거절하는 사유가 그대로 보인다.
- 패널이 `cart`뿐인 에이전트는 확인 카드와 영수증 자리가 없어서 확인은 예전 대화 안 카드로, 영수증과 안내는 위쪽 안내 줄로 나온다. 시험은 없다.
- 음성·아바타 채널에서 터치가 진행 중인 답을 끊는 것은 서버 일이고, 화면 쪽 시험은 없다.
- 화면 에이전트에서 음성·아바타 채널을 고르면 1440px에서 위쪽 막대가 두 줄이 된다.
- 모니터의 터치 턴에서 구간 시각이 "말 끝 +"로 적힌다.
