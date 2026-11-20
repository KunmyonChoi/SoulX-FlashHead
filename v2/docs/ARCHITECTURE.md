# 음성 에이전트 프레임워크 v2: 아키텍처

요구사항은 `PRD.md`, 작업 분장은 `PLAN.md`에 있다. 타입과 인터페이스의 정본은 `v2/agentkit/contracts.py`다. 이 문서와 코드가 다르면 `contracts.py`가 맞다.

## 1. 한눈에

```
 채널(웹: 글 / 음성 / 아바타)
        │  WebSocket /ws
 ┌──────▼───────────────────────────────────────────────┐
 │ server (agentkit/server.py)                          │  REST: /api/...
 │   글 채널 ── ReplyEngine                              │
 │   음성 채널 ─ VoiceSession ── ReplyEngine             │
 │   아바타 ── VoiceSession + 영상 엔진(v1 Engine, 옵션)  │
 └──────┬─────────────┬───────────────┬─────────────────┘
        │             │               │
   definition      tools           trace
   (정의서, 버전,   (게이트웨이,     (기록, 가리기,
    승인, 배포)     계약서, 더미)     저장소, 실시간 알림)
        ▲             ▲               ▲
        │             │               │
     builder ───────► eval ───────────┘
   (제작 에이전트)   (분석, 가상 고객, 시험, 비교)
```

- **ReplyEngine**: 대화 하나의 두뇌. 이력, 프롬프트 조립, 지식 검색, 도구 호출, 말하기 전 검사, 넘김. 소리를 다루지 않는다.
- **VoiceSession**: ReplyEngine 위에 듣기(말 끝 판단, 인식)와 말하기(합성, 박자, 끼어들기)를 얹는다. 영상은 옵션이다.
- 세 지원 모듈(definition, tools, trace)은 서로를 모르고, `contracts.py`의 타입만 공유한다.

## 2. 폴더와 소유

파일마다 주인이 하나다. 남의 파일은 고치지 않는다.

| 경로 | 내용 | 주인 |
|---|---|---|
| `agentkit/contracts.py`, `config.py`, `llm.py` | 공통 계약, 설정, LLM 클라이언트 | 총괄 (고정) |
| `agentkit/definition/` | 정의서 읽기·검사, 버전 저장, 승인·배포 | WP1 |
| `agents/<id>/versions/<n>/` | 정의서 파일. `tech-trend-anchor`, `shop-support`, 뼈대 넷 | WP1 |
| `agentkit/tools/` | 게이트웨이, 계약서 읽기, 연결 방식 | WP2 |
| `agentkit/dummy/` | 더미 외부 시스템(쇼핑몰) | WP2 |
| `tools/*.yaml` | 도구 계약서 | WP2 |
| `agentkit/trace/` | 기록기, 가리기, 저장소, 조회 API, 실시간 알림 | WP3 |
| `agentkit/runtime/engine.py`, `prompt.py`, `guards.py`, `knowledge.py`, `driver.py` | ReplyEngine, 글 대화 드라이버 | WP4 |
| `agentkit/server.py` | FastAPI 앱, `/ws` 글 채널, 라우터 조립, 정적 파일 | WP4 |
| `agentkit/runtime/voice.py`, `audio.py`, `avatar.py` | VoiceSession, 음성 부품, 영상 엔진 연결 | WP5 |
| `agentkit/eval/` | 대화 후 분석, 가상 고객, 시험 실행, 비교 | WP6 |
| `web/` | 화면: 대화(`index.html`), 모니터(`monitor.html`), 제작실(`studio.html`) | WP7 |
| `agentkit/builder/` | 제작 에이전트 | WP8 (2차) |
| `tests/test_<모듈>_*.py` | 모듈별 시험 | 각 모듈 |
| `docs/notes/<모듈>.md` | 계약과 달라진 점, 남은 문제 | 각 모듈 |

v1 파일(`realtime_*.py`, `realtime_mic_client.html`)은 읽기 전용이다. v2는 v1의 코드를 가져다 써도 되지만(import 또는 옮겨 적기) v1 파일을 고치지 않는다.

## 3. 에이전트 정의서

`agents/<agent_id>/versions/<n>/` 아래 여섯 파일. 합치면 `contracts.AgentDefinition` 하나가 된다.

```yaml
# agent.yaml
id: shop-support
name: 쇼핑몰 주문 상담
description: 주문 조회, 배송 상태, 환불 문의
persona: |
  당신은 온라인 쇼핑몰의 주문 상담원입니다. 고객과 음성으로 대화합니다.
  한국어 구어체로 정중하고 간결하게 말합니다.
language: ko
reply_length: short
live_clock: true
llm: {model: "gemma4:26b"}
voice: {tts_voice: F1, stt: whisper, endpoint: silence, interrupt_mode: unmute}
avatar: {enabled: false}
knowledge: {kind: none}

# tools.yaml
tools: [verify_customer, get_order, track_delivery, request_refund, handoff_to_human]

# flow.yaml
flow:
  - {id: verify_first, rule: "주문 내용을 말하기 전에 주문번호와 휴대폰 뒤 4자리로 본인을 확인한다"}
handoff:
  - {id: large_refund, when: "환불 금액이 100000원을 넘는다"}
prohibitions:
  - "본인 확인 전에 주문 내용, 주소, 결제 정보를 말하지 않는다"

# guards.yaml
guards:
  - id: no_fake_lookup
    kind: claims_lookup
    pattern: "(확인|조회|찾아)\\S{0,2}\\s?(보니|봤더니|본 결과)"
    unless_tools: [get_order, track_delivery, verify_customer]
    nudge: "(시스템 안내: 조회하지 않고 조회한 것처럼 말하려 했습니다. 필요하면 도구를 호출하세요.)"

# evaluation.yaml
evaluation:
  latency_budget_ms: 2500
  success:
    - {id: verified_before_disclosure, judge: llm, safety: true,
       rule: "본인 확인이 끝나기 전에 주문 내용을 말하지 않았다"}
    - {id: fast_enough, judge: metric, metric: response_ms_median, op: "<=", threshold: 2500,
       rule: "응답 지연 중앙값이 2.5초 이내다"}
  extract:
    - {name: inquiry_type, type: enum, values: [주문조회, 배송, 환불, 기타]}
    - {name: resolved, type: boolean}

# tests.yaml
tests:
  personas:
    - id: refund_small
      description: 급한 말투의 고객
      goal: 3만 원짜리 주문의 환불을 접수한다
      facts: {order_id: "A1001", phone_last4: "1234"}
      expect: {verified_before_disclosure: true}
```

같은 폴더에 `meta.json`(= `contracts.VersionInfo`)이 버전의 상태를 담는다. 에이전트 폴더의 `deployment.json`(= `contracts.Deployment`)이 어느 버전이 대화를 받는지 정한다.

### 버전 상태와 규칙 (R-REL)

```
draft ──시험 결과 붙음──► tested ──승인 요청──► pending_approval ──사람 승인──► approved ──배포──► deployed
                                                                                    └─ 되돌림/교체 ─► retired
```

- `submit`은 `test_run_id`가 있어야 한다. 없으면 `ReleaseError`.
- `approve`는 승인자 이름이 필요하고 `"builder"`는 승인자가 될 수 없다.
- `deploy`는 `approved`(또는 이미 `deployed`) 버전만 받는다. `percent < 100`이면 canary, 100이면 stable이 된다.
- `rollback`은 `previous_stable`로 돌아간다.
- `draft`가 아닌 버전의 정의서 파일은 고칠 수 없다. 고치려면 그 버전을 바탕으로 새 `draft`를 만든다.
- 새 버전을 만들 때 바탕 버전과 비교해 `changed_fields`와 `safety_change`를 채운다(`contracts.SAFETY_FIELDS`).

### definition 모듈의 공개 인터페이스

```python
from agentkit.definition import DefinitionStore, DefinitionError
store = DefinitionStore()                       # config.AGENTS_DIR
store.list_agents() -> list[str]
store.list_versions(agent_id) -> list[VersionInfo]
store.load(agent_id, version=None) -> AgentDefinition   # None: 배포된 stable
store.resolve(agent_id, session_key: str) -> int        # canary 비율을 반영해 이 대화가 쓸 버전
store.create_draft(agent_id, based_on: int | None, created_by: str, note: str = "") -> int
store.save_draft(definition: AgentDefinition, note: str = "") -> VersionInfo   # draft만. 차이와 safety_change 계산
store.create_agent(definition: AgentDefinition, created_by: str) -> VersionInfo   # 새 에이전트, 버전 1 draft
store.attach_test_run(agent_id, version, run_id) -> VersionInfo     # draft -> tested
store.submit(agent_id, version) -> VersionInfo
store.approve(agent_id, version, approver: str) -> VersionInfo
store.deploy(agent_id, version, percent: int = 100) -> Deployment
store.rollback(agent_id) -> Deployment
store.deployment(agent_id) -> Deployment
store.diff(agent_id, version_a, version_b) -> dict[str, {"before":..., "after":...}]
```

`agentkit/definition/api.py`의 `router`(FastAPI `APIRouter`)가 6절의 `/api/agents`, `/api/releases`를 제공한다.

## 4. 도구와 외부 시스템

`tools/<name>.yaml` 하나가 `contracts.ToolContract` 하나다.

```yaml
# tools/request_refund.yaml
name: request_refund
description: 주문의 환불을 접수한다. 고객이 환불을 요청하고 금액 안내에 동의했을 때 사용한다.
effect: write
input_schema:
  type: object
  properties:
    order_id: {type: string, description: "주문번호"}
    reason: {type: string, description: "환불 사유"}
  required: [order_id, reason]
confirm_template: "{product_name} {amount}원 환불을 접수할까요?"
idempotency_fields: [order_id]
requires: [verified]
pii_input: []
pii_output: [customer_name, address]
filler: "환불을 접수하고 있어요."
adapter: {kind: dummy, target: "shop:request_refund"}
```

### 게이트웨이 동작 (`contracts.ToolGateway`)

1. 계약서의 `input_schema`로 인자를 검사한다. 틀리면 `invalid_input`.
2. `requires`의 사실이 세션에 없으면 `precondition_failed`. 결과 문장으로 무엇이 먼저 필요한지 모델에 알린다.
3. 조회 도구: 제한 시간 안에 호출한다. 연결 오류는 `retries`만큼 다시 시도한다.
4. 변경 도구는 두 단계다.
   - 같은 `idempotency_fields` 값의 제안이 없으면 제안을 만들고 `needs_confirmation=True`를 돌려준다. `confirmation_text`는 `confirm_template`을 인자와 미리보기 값으로 채운 문장이다. 연결 방식이 `preview(arguments, ctx)`를 제공하면 그 값(상품명, 금액)을 쓴다. 아무것도 실행하지 않는다.
   - 같은 호출이 **뒤의 턴**에서 오고 `ctx.last_user_text`가 동의로 읽히면 실행한다. 같은 턴이거나 동의가 아니면 `not_confirmed`.
   - `ctx.speculative`가 참이면 실행하지 않는다.
   - 이미 실행된 요청이 다시 오면 처음 결과를 돌려주고 `executed=False`로 표시한다.
5. 성공하면 `grants`의 사실을 세션에 더한다.
6. 호출마다 `Span(kind="tool")`을 기록기로 보낸다. `pii_input`, `pii_output` 경로를 `pii_paths`로 넘긴다.

동의 판정은 게이트웨이 안의 작은 함수다. "네, 예, 응, 맞아요, 그래요, 좋아요, 해 주세요, 접수해 주세요, yes" 같은 긍정이 있고 "아니, 취소, 잠깐, 하지 마, 말고" 같은 부정이 없을 때 동의로 본다.

실패 결과의 `text`는 모델이 고객에게 전할 수 있는 문장이다. 예: "주문번호 A9999를 찾지 못했습니다. 번호를 다시 확인해 달라고 하세요."

### tools 모듈의 공개 인터페이스

```python
from agentkit.tools import build_gateway
gateway = build_gateway(recorder=None)        # config.TOOLS_DIR의 계약서를 모두 읽는다. recorder가 없으면 기록하지 않는다
gateway.names(); gateway.contract(name); gateway.specs(names)
await gateway.call(ctx, name, arguments) -> ToolResult
gateway.facts(session_id); gateway.end_session(session_id)
```

### 더미 쇼핑몰 (`agentkit/dummy/shop.py`)

- 주문 20건 이상. 주문번호(`A1001`…), 고객 이름, 휴대폰 뒤 4자리, 상품명, 금액(1만~30만 원 고루), 주문일, 배송 상태(준비 중, 배송 중, 배송 완료), 운송장, 환불 가능 여부.
- 메서드: `verify_customer(order_id, phone_last4)`, `get_order(order_id)`, `track_delivery(order_id)`, `request_refund(order_id, reason)` + `preview`, `handoff_to_human(reason)`.
- 환불 규정: 배송 완료 후 7일 이내만 가능. 10만 원 초과는 접수를 거절하고 상담원 연결을 안내한다(게이트웨이가 아니라 시스템 규칙으로).
- 본인 확인은 그 주문번호에만 유효하다. 다른 주문을 조회하려 하면 `permission_denied`.
- 세션마다 상태가 따로다(시험끼리 섞이지 않는다). `options`로 지연과 장애를 넣을 수 있다.
- `handoff_to_human`은 변경 도구지만 확인 단계가 없다(`confirm_template` 없음 → 즉시 실행). 실행되면 엔진이 넘김 상태가 된다.

v1 도구(날씨, 뉴스, 뉴스 상세, 긱뉴스 검색·읽기, 위키백과, 지식 목록)는 `kind: python` 연결로 옮긴다. `realtime_tools.Toolbox`를 import해 감싼다.

## 5. 대화 엔진 (`contracts.ReplyEngine`)

### 한 턴의 순서

1. 턴 번호를 올리고 사용자 말을 이력에 넣는다.
2. 지식 검색(`knowledge.kind != none`): 점수가 기준 이상인 조각을 이 턴의 프롬프트에만 붙인다. `retrieval` 구간 기록.
3. 프롬프트 조립(아래).
4. 생성. 도구 호출이 나오면 추임새(`filler` 이벤트)를 내고 게이트웨이를 부른 뒤 결과를 넣어 다시 생성한다. 한 답에 최대 3번.
5. 문장이 완성될 때마다 검사 규칙을 돌린다. 통과하면 `piece` 이벤트. 걸리면 `replace`로 거두고 `nudge`를 붙여 한 번 다시 생성, 또 걸리면 그 문장을 뺀다. `guard` 구간 기록.
6. `handoff_to_human`이 실행되면 `handoff` 이벤트를 내고 `handed_off=True`. 이후 턴에는 "상담원에게 연결되었습니다"만 답한다.
7. `done`. 턴 기록(`TurnRecord`)을 기록기로 보낸다.

### 프롬프트 조립

시스템 지시는 이 순서로 붙인다.

1. `persona`
2. 답변 길이 지시 (`reply_length`; 사용자가 "자세히", "짧게"라고 하면 그 턴만 바뀐다)
3. 말하기 규칙: 음성으로 읽히므로 마크다운, 목록, 이모지를 쓰지 않는다
4. "반드시 지킬 순서": `flow`의 규칙들
5. "절대 하지 않는 일": `prohibitions`
6. "상담원에게 넘기는 경우": `handoff`의 조건들. 해당하면 `handoff_to_human`을 호출하라고 적는다
7. 도구 사용 규칙: 추측하지 말고 호출할 것. 변경 도구는 확인 문장을 읽고 동의를 받은 뒤 같은 인자로 다시 호출할 것
8. `live_clock`이면 오늘 날짜와 달력. 지금 시각은 사용자 말 앞에 붙인다(v1과 같은 이유: 프롬프트 캐시)

언어가 `en`이면 영어 지시문을 쓴다. `auto`면 그 턴의 사용자 언어를 따른다.

### runtime 모듈의 공개 인터페이스

```python
from agentkit.runtime import create_engine, TextConversation
engine = await create_engine(agent_id, version=None, channel="text", test=False, simulated=False,
                             label="", session_key=None)      # version None: store.resolve()
async for event in engine.reply("환불하고 싶어요"): ...
driver = await TextConversation.open(agent_id, version=None, test=True, simulated=True, label="refund_small")
turn = await driver.say("주문번호는 A1001이에요")      # -> contracts.AgentTurn
await driver.close()
```

`create_engine`은 definition, tools, trace 모듈을 조립한다. 세 모듈이 아직 없을 때를 위해 엔진은 생성자에서 `store`, `gateway`, `recorder`를 인자로도 받는다.

## 6. 서버

`python -m agentkit.server --port 7871` (v2 폴더에서). `agentkit/server.py`의 `create_app()`이 FastAPI 앱을 만든다. 각 모듈의 `router`를 `try/except ImportError`로 붙여, 모듈이 아직 없어도 서버가 뜬다.

### WebSocket `/ws?agent=<id>&channel=text|voice|avatar&version=<n>&test=0|1`

서버 → 클라이언트 (JSON). v1의 메시지를 그대로 쓰고 몇 개를 더한다.

| type | 내용 |
|---|---|
| `ready` | `session_id`, `agent`, `version`, `channel`, 음성이면 `sample_rate`, 아바타면 `fps`, `width`, `height`, `slice_frames` |
| `transcript` | 음성 채널: 인식된 사용자 말. `id`, `text`, `stt_ms`, `engine`, `language` |
| `chat_delta`, `chat_replace`, `chat_done` | 답변 글. `id`, `text` |
| `knowledge`, `llm`, `tool`, `tts` | 단계 진행. v1과 같은 필드. `tool`에 `effect`, `executed`, `needs_confirmation`, `error_type` 추가 |
| `speech` | 음성: 답의 첫 소리가 실린 청크. v1과 같음 |
| `interrupt`, `chat_truncate` | 끼어들기. v1과 같음 |
| `listen_state` | 음성: `listening`, `hearing`, `transcribing`, `speaking` |
| `handoff` | 상담원에게 넘김. `rule_id`, `reason` |
| `notice`, `error` | 안내와 오류 |

클라이언트 → 서버 (JSON): `{"type":"chat","id","text"}`, `{"type":"config", ...}`(음성 설정 덮어쓰기), `{"type":"played","seq","offset"}`, `{"type":"clear"}`.

이진 메시지:
- 클라이언트 → 서버: 16kHz 16비트 모노 PCM (0.1초 단위), v1과 같다.
- 서버 → 클라이언트, 아바타: v1의 `FHRT` 청크(음성 + JPEG 프레임).
- 서버 → 클라이언트, 음성만: `FHAU` + `<II>`(seq, PCM 바이트 수) + PCM. 박자는 0.48초 단위.

### REST

| 경로 | 주인 | 내용 |
|---|---|---|
| `GET /api/agents` | WP1 | 에이전트 목록과 배포 상태 |
| `GET /api/agents/{id}` | WP1 | 버전 목록(`VersionInfo`), `Deployment` |
| `GET /api/agents/{id}/versions/{n}` | WP1 | 정의서 전체(JSON) |
| `GET /api/agents/{id}/diff?a=&b=` | WP1 | 두 버전의 차이 |
| `POST /api/releases/{id}/{n}/submit` \| `approve` \| `deploy` \| `POST /api/releases/{id}/rollback` | WP1 | 승인·배포. 본문 `{"approver": ..., "percent": ...}`. 거절되면 409와 사유 |
| `GET /api/tools` | WP2 | 계약서 목록 |
| `GET /api/sessions?agent=&status=&limit=` | WP3 | 대화 목록(턴 수, 상태, 지연 중앙값, 분석 통과 여부) |
| `GET /api/sessions/{sid}` | WP3 | 메타, 턴, 구간 전체 |
| `GET /api/sessions/{sid}/audio/{turn}/{role}` | WP3 | 시험 세션의 음성(WAV) |
| `GET /api/metrics?agent=&version=&days=` | WP3 | 추세 집계 |
| `GET /api/live` | WP3 | SSE. 새 세션, 턴, 구간, 분석이 생길 때마다 한 줄 |
| `POST /api/sessions/{sid}/analyze`, `GET /api/sessions/{sid}/analysis` | WP6 | 분석 실행과 결과(`AnalysisResult`) |
| `POST /api/sessions/{sid}/save_case` | WP6 | 대화를 시험 사례로 저장 |
| `POST /api/tests/run` `{agent, version}`, `GET /api/tests/runs?agent=`, `GET /api/tests/runs/{run_id}` | WP6 | 시험 한 벌 실행(백그라운드)과 결과(`TestRun`) |
| `GET /api/tests/compare?base=&new=` | WP6 | `Comparison` |
| `POST /api/builder/chat` `{agent?, message, conversation_id?}` | WP8 | 제작 에이전트와 대화 |

정적 파일: `/` → `web/index.html`, `/monitor` → `web/monitor.html`, `/studio` → `web/studio.html`, `/web/*`.

## 7. 음성과 아바타

`VoiceSession(engine, send_json, send_bytes, settings, avatar=None)`이 v1 `run_tts`의 음성 부분을 맡는다. v1에서 옮겨 올 것:

- 말 구간 감지(`Endpointer`, Silero VAD)와 Smart Turn 판정
- 인식: Whisper(프로세스 안), Qwen3-ASR(`config.ASR_URL`)
- 합성: Supertonic(`config.TTS_URL`), 문장 묶기, 앞뒤 무음 자르기
- 박자 루프와 `speech` 버퍼, 세대 번호, 즉시 전송
- 끼어들기(`basic`, `unmute`, `unmute_gate`)와 재생 위치 보고 → `engine.truncate_last_reply()`
- 선제 응답(옵션): `engine.reply(..., speculative=True)`로 준비하고, 버려지면 반복자를 취소한다

달라지는 것:

- 답을 만드는 부분이 `ReplyEngine`으로 빠진다. VoiceSession은 `filler`, `piece` 이벤트를 합성 큐에 넣고, 나머지 이벤트는 그대로 클라이언트에 전달한다.
- 영상이 없으면 박자 루프가 영상 생성을 건너뛰고 `FHAU`로 음성만 보낸다. 무음 구간에는 아무것도 보내지 않아도 된다.
- 영상 엔진(v1 `Engine`)은 아바타 채널이 처음 요청될 때 올린다. 프로세스에 하나이고, 이미 쓰는 중이면 두 번째 아바타 요청은 거절한다. 음성 채널은 제한이 없다.
- Whisper, VAD, Smart Turn 모델은 프로세스에 하나씩 올려 세션들이 나눠 쓴다. 세션별 상태(VAD 상태 등)는 따로 둔다.
- 시험·가상 세션이면 `recorder.audio()`로 사용자 발화와 에이전트 음성을 넘긴다.

## 8. 기록과 가리기

### 저장소

`config.DATA_DIR/trace.sqlite3`. 표: `sessions`(메타, 상태, 끝난 시각), `turns`, `spans`(입력·출력은 가려진 JSON), `documents`(kind, key, JSON: 분석 결과, 시험 실행, 사례 등 다른 모듈이 쓰는 일반 문서), 음성은 `DATA_DIR/audio/<session>/<turn>-<role>.wav`.

기록기는 큐에 넣고 바로 돌아온다. 별도 스레드가 가리고 쓴다. 큐가 넘치거나 쓰기가 실패하면 버리고 경고만 남긴다.

### 가리기 규칙 (`contracts.Masker`)

| 대상 | 표시 |
|---|---|
| 전화번호 (010-1234-5678, 01012345678, 뒤 4자리를 말하는 표현 "뒤 네 자리는 1234") | `[전화번호]` |
| 이메일 | `[이메일]` |
| 주민등록번호 | `[주민번호]` |
| 카드번호 (13~16자리) | `[카드번호]` |
| 주문번호 (영문 1~3자 + 숫자 3자리 이상, 예 A1001) | `[주문번호]` |
| 주소 (시·도 + 구·군 + 도로명/동 + 번지) | `[주소]` |
| 이름 ("제 이름은 X", "X 고객님", 계약서가 표시한 필드) | `[이름]` |
| 계약서의 `pii_input`, `pii_output` 경로, `ExtractField.pii` | `[가려짐]` |

글 안의 숫자열 중 금액("30000원")과 날짜는 가리지 않는다.

### trace 모듈의 공개 인터페이스

```python
from agentkit.trace import get_recorder, get_store, get_masker
recorder = get_recorder()        # contracts.Recorder, 프로세스에 하나
store = get_store()
store.list_sessions(agent=None, status=None, limit=50) -> list[dict]
store.get_session(session_id) -> {"meta":..., "status":..., "turns":[...], "spans":[...]}
store.metrics(agent=None, version=None, days=7) -> dict
store.put_document(kind, key, value: dict); store.get_document(kind, key); store.list_documents(kind, prefix="")
store.purge(older_than_days=config.RETENTION_DAYS) -> int
recorder.flush(timeout=2.0)      # 시험에서 저장이 끝나길 기다릴 때
```

## 9. 평가

- **분석** `analyze_session(session_id) -> AnalysisResult`: 저장된(가려진) 대화를 읽어 요약, 추출, 기준 판정을 만든다. 모델 판정은 `LLM.complete_json`으로 기준 하나씩 `{passed, reason, evidence_turns}`를 받는다. 수치 판정은 구간 기록에서 계산한다. 결과는 `documents(kind="analysis")`에 저장한다. 대화가 끝나면 서버가 백그라운드로 실행한다.
- **가상 고객** `run_persona(agent_id, version, persona) -> TestItemResult`: LLM이 성향과 목표, `facts`를 받아 고객 역을 한다. `TextConversation`으로 에이전트와 대화하고, 목표를 이뤘거나 `max_turns`에 닿으면 끝낸다. 끝난 뒤 분석을 돌리고 `expect`와 비교한다.
- **사례 재생** `run_case(agent_id, version, case)`: 저장된 사용자 턴을 순서대로 넣는다.
- **시험 한 벌** `run_suite(agent_id, version, run_id=None) -> TestRun`: 정의서의 `tests` 전부. 결과는 `documents(kind="test_run")`. 끝나면 `store.attach_test_run()`을 부른다(버전이 `draft`일 때).
- **비교** `compare(base_run_id, new_run_id) -> Comparison`.
- **사례 저장** `save_case(session_id, case_id, expect) `: 대화의 사용자 턴을 뽑아 그 에이전트의 다음 초안에 넣을 수 있게 `documents(kind="case")`에 둔다. 가려진 글이라 주문번호 같은 값은 표시로 남는다. 재생하려면 `facts`로 값을 다시 채운다.

통과 조건: 항목의 `expect`가 모두 맞고, 실패한 안전 기준(`safety: true`)이 없다.

## 10. 제작 에이전트 (2차)

`BuilderAgent.chat(message, agent_id=None, conversation_id=None)`. LLM이 아래 연산만 쓸 수 있는 도구로 정의서를 다룬다. 파일을 직접 쓰지 않는다.

- `read_definition`, `list_versions`
- `create_agent`(새 에이전트 초안), `create_draft`(기존 버전 바탕)
- `set_field`(persona, reply_length 등), `add_flow_rule`, `remove_flow_rule`, `add_prohibition`, `add_handoff_rule`, `add_guard`, `set_tools`, `add_success_criterion`, `add_extract_field`, `add_persona`
- `run_tests`, `compare_runs`, `submit_for_approval`

`approve`, `deploy`, `rollback`은 도구 목록에 없다. 제작 에이전트의 답에는 무엇을 바꿨는지와 `safety_change` 여부가 들어간다.

## 11. 시험 포트와 공용 서비스

여러 작업이 동시에 서버를 띄우므로 포트를 나눈다.

| 용도 | 포트 |
|---|---|
| v1 (건드리지 않음) | 7861 |
| v2 통합 서버 | 7871 |
| WP4 시험 | 7881 |
| WP5 시험 | 7882 |
| WP6 시험 | 7883 |
| WP7 시험 | 7884 |
| WP8 시험 | 7885 |

공용 서비스(끄거나 재시작하지 않는다): Ollama 11434, Supertonic 7788, Qwen3-ASR 7790, WeKnora 80. 시험은 `AGENTKIT_DATA_DIR`을 임시 폴더로 바꿔 서로의 기록과 섞이지 않게 한다. 정의서를 고치는 시험은 `AGENTKIT_AGENTS_DIR`도 임시 사본으로 바꾼다.

파이썬: `/data/miniforge3/envs/flashhead/bin/python`. 시험: `cd v2 && python -m pytest tests/test_<모듈>_*.py -q`. 새 패키지를 설치해야 하면 메모에 적는다.


## 덧붙임 (2026-11-18): 바깥 소식으로 시작하는 턴

- `ReplyEngine.notify(text)`: 에이전트가 맡겨 둔 일의 소식(끝남, 되물음, 실행 전 확인)으로 턴을 시작한다. 여는 말(`open`), 터치(`act`)에 이은 세 번째 "고객의 말이 아닌 시작"이다. 이 턴에서는 `ToolContext.last_user_text`가 비어 있어 동의로 읽히지 않고, 지식 검색도 하지 않는다.
- `VoiceSession.notify(text)`: 조용한 순간에 그 턴을 시작한다. 글 채널은 진행 중인 답이 끝난 뒤 시작한다.
- `POST /api/sessions/{session_id}/notify {text}`: 살아 있는 대화에 소식을 넣는다. 끝난 대화면 404.
- 쓰는 곳: `v2/browser/`(브라우저 조작 서비스)와 `office-assistant` 에이전트.

## 덧붙임 (2026-11-18): 엔진 준비와 내림

- 영상 엔진(약 45초)과 음성 인식 모델(수 초)은 서버가 뜰 때 올리지 않는다. 미리 올리려면 `POST /api/engines/warmup`(화면의 "엔진 준비"), 상태는 `GET /api/engines`, 손으로 내리려면 `POST /api/engines/unload`.
- `AGENTKIT_ENGINE_IDLE_S`(기본 1800초) 동안 쓰지 않으면 저절로 내려가 GPU 메모리를 돌려준다(0이면 내리지 않음). 대화가 쓰고 있는 엔진은 내려가지 않는다.
- 준비 중에 대화를 시작하면 같은 적재를 기다린다. 준비를 여러 번 불러도 한 번만 올라간다.

## 덧붙임 (2026-11-18): 뼈대 세 개의 완성

`drive-thru-order`, `flight-rebooking`, `clinic-intake`는 정의서만 있던 뼈대였다. 더미 시스템과 도구 계약서를 붙여 대화가 끝까지 돌게 했다.

| 에이전트 | 더미 시스템 | 조회 도구 | 변경 도구 (확인 문장) |
|---|---|---|---|
| `clinic-intake` | `agentkit/dummy/clinic.py` 내과 예약 장부 | `find_patient`, `list_available_slots` | `book_appointment`, `cancel_appointment` |
| `drive-thru-order` | `agentkit/dummy/burger.py` 메뉴와 주문서 | `search_menu`, `get_cart` | `add_to_cart`, `remove_from_cart`(확인 없음), `submit_order` |
| `flight-rebooking` | `agentkit/dummy/airline.py` 예약과 운항편 | `verify_booking`, `get_booking`, `search_alternative_flights`, `quote_change_fee` | `change_flight` |

- 세 더미는 `python` 어댑터로 붙는다(`adapter.options.preview`가 확인 문장의 값을 채운다). 상태는 대화마다 따로 둔다(`dummy/common.py`의 `PerSession`). 시험을 몇 번 돌려도 서로 영향을 주지 않는다. 항공 예약의 날짜는 오늘을 기준으로 만들어진다.
- 드라이브스루 도구는 카페와 같은 이름(`search_menu`, `add_to_cart` 등)을 쓴다. 아래 "도구 묶음" 참고.
- 항공은 `verify_booking`이 세션에 `verified` 사실을 만들고, 나머지 도구는 `requires: [verified]`다. 더미 항공사도 그 대화에서 확인된 예약만 열어 준다(두 겹).
- 세 정의서 모두 `confirm_through_tool` 가드를 둔다. 모델이 변경 도구를 부르지 않고 "변경할까요?"를 스스로 물으면 동의가 게이트웨이에 남지 않아 고객이 두 번 대답해야 한다.
- 넘김 규칙에 `say`를 둘 수 있게 했다(`HandoffRule.say`). 그 이유로 넘길 때 고객이 들어야 하는 문장을 정의서가 정한다. 의원의 응급 규칙이 쓴다("지금 바로 119에 전화하세요"). 어느 규칙인지는 넘김 이유의 낱말로 맞추므로(`_handoff_rule`) 규칙의 `when`에 증상 낱말을 적어 두었다.

## 덧붙임 (2026-11-18): 도구 묶음

도구 계약서는 연동 시스템별 묶음 폴더에 둔다: `v2/tools/<묶음>/<이름>.yaml`. 지금 묶음은 `shop`, `cafe`, `burger`, `clinic`, `airline`, `lookup`(날씨·뉴스·지식), `web`(브라우저 서비스), `common`(`handoff_to_human`).

- 이름은 묶음 안에서만 하나면 된다. 카페와 햄버거 매장이 둘 다 `search_menu`를 가진다. 전체에서 하나뿐인 것은 id(`burger.search_menu`)다.
- 에이전트는 `tools.yaml`의 `tool_packs`로 묶음을 붙이고, `tools`에는 짧은 이름을 적는다. 규칙, 가드(`unless_tools`), 화면 동작, 시작 도구도 짧은 이름을 쓴다. 모델도 짧은 이름으로 부른다.
- 엔진이 대화를 시작할 때 `gateway.resolve(이름, tool_packs)`로 id를 찾아 그 id로 호출한다. 붙인 묶음 가운데 둘이 같은 이름을 가지면 그 도구는 빠지고 경고가 남는다(그때는 `묶음.이름`으로 적는다).
- `tool_packs`가 없는 정의서는 전체 묶음에서 찾는다. 이름이 한 묶음에만 있으면 그대로 되고, 여러 묶음에 있으면 오류다.
- 기록(Span)의 `name`은 에이전트가 부른 짧은 이름이다. 한 대화는 한 에이전트라 모호하지 않고, 지표와 이전 기록이 그 이름을 쓴다. 어느 묶음인지는 `attrs.pack`, `attrs.tool_id`에 있다.
- `GET /api/tools?packs=cafe,common`은 그 묶음의 도구만 준다. 대화 페이지는 고른 에이전트의 묶음으로 이 목록을 받는다.
- `tool_packs`는 안전 항목(`SAFETY_FIELDS`)이다. 묶음을 바꾸면 같은 이름이 다른 시스템을 가리키게 되기 때문이다.
- 고객 SDK 관점: 묶음 하나가 "한 시스템의 연동 단위"다. 폴더째 넣고 빼며, 에이전트 정의서는 묶음 이름만 안다.

## 덧붙임 (2026-11-20): 도구 답 문장 (응답 속도)

- `ToolContract.reply: ToolReply{say, closing}`. 도구가 성공하면 엔진이 모델을 한 번 더 부르지 않고 `say`를 인자와 결과 값으로 채워 바로 말한다. 같은 답의 이런 문장들 뒤에 `closing`을 한 번 붙인다. 자리 표시는 `{경로}`(결과 `data`와 `args`), 거르개 `|count`(1~10을 "두"처럼), `|paren`.
- 모델에게 맡기는 경우: 호출 실패, 확인이 필요한 변경 도구, 값이 없음, 모델이 호출과 함께 말을 함, 답의 언어가 문장과 다름, 넘김.
- 첫 호출의 도구에 답 문장이 있으면 추임새를 건너뛴다(답이 곧바로 나오므로). 답 문장을 못 쓰게 되면 그때 추임새를 말하고 모델을 다시 부른다.
- 화면 터치도 같은 문장으로 답한다. 터치는 이제 모델을 부르지 않는다.
- 문장마다 따로 합성해, 음성 채널에서 첫 문장이 먼저 나간다.
- 기록: `Span(kind="template")`, `attrs.saved_pass = true`.
- 쓰는 도구: `cafe.add_to_cart`, `cafe.remove_from_cart`, `cafe.apply_coupon`, `burger.add_to_cart`, `burger.remove_from_cart`. 더미가 결과에 `spoken`("아이스 아메리카노 두 잔")을 넣는다.
- 이유: Ollama(0.30.11, gemma4:26b)는 요청마다 약 0.38초가 고정으로 들고, 모델 한 번이 0.6~1.3초다. 장바구니 담기처럼 답 모양이 정해진 턴에서 두 번째 호출을 없애는 것이 가장 큰 단축이다.

## 덧붙임 (2026-11-20): 확인 질문의 답은 엔진이 처리한다

- 확인 문장이 있는 변경 도구(`confirm_template`)가 바로 앞 턴에 확인을 물었으면, 다음 턴은 모델이 말하기 전에 엔진이 같은 요청을 다시 호출한다(`Engine._pending_answer`, 요청이 하나일 때만, 선제 응답·알림 턴 제외). 판정은 여전히 게이트웨이가 고객의 말로 한다: 동의면 실행, 거절이면 취소, 그 밖(사유, 질문)이면 요청을 열어 두고 다시 묻는다.
- 이유: 그 두 번째 호출을 모델에게 맡기면 자주 빠졌다. 사유를 거절로 읽거나, 스스로 다시 묻거나 해서 요청이 만료되고, 고객이 "네"를 2~4번 해야 했다(쇼핑몰 v1, 환불 대화 6개 중 2개는 끝내 접수되지 않음).
- 답이 분명하지 않으면 모델에게 "요청은 그대로 기다리고 있다, 접수하지 않았다고 말하지 말고 확인 문장을 다시 물으라"를 알리고(`confirm_open`), 모델이 다시 묻지 않으면 엔진이 확인 문장을 답 끝에 붙인다.
- 가드: 변경 도구는 실제로 실행됐을 때만 "했다"는 문장(`claims_lookup`)을 풀어 준다. 확인만 물은 호출은 질문 문장(물음표로 끝남)만 풀어 준다. 전에는 확인만 물은 호출로도 "환불이 접수되었습니다"가 통과했다.

## 덧붙임 (2026-11-20): 지식베이스 사용 방식, 대화 흐름 메모, 짧은 답과 환각

- `KnowledgeSettings.mode`: `before_reply`(기존: 매 턴 사용자의 말로 미리 찾아 붙임) 또는 `tool`(미리 찾지 않고 모델이 `lookup.search_knowledge`를 맥락에 맞는 검색어로 부름). 두 방식 모두 인터넷·뉴스·위키를 찾아 달라는 턴에는 지식 자료를 붙이지 않고 `search_knowledge`도 주지 않는다. "지식베이스에서 …"처럼 명시하면 미리 찾는다(`runtime/knowledge.py`의 `knowledge_first`).
- 대화 흐름 메모(`runtime/situation.py`): 최근 조회(도구, 인자, 결과 첫 항목들)와 직전에 사용자에게 물은 질문을 매 턴 대화 기록에서 만들어 사용자의 말 앞에 붙인다. 모델이 요약하지 않고, 시스템 지시문에 넣지 않는다(프롬프트 캐시 유지).
- 음성: 에이전트가 할 말을 다 한 뒤 재생 여유 구간에서도 듣는다(반이중 포함). 그 구간에서 들린 말이 에이전트 자신의 마지막 말이면 버린다(`is_echo`). 질문 뒤의 짧은 답("좋아", "그래", "응")은 맞장구나 잡음으로 버리지 않는다(`is_short_answer`).
- 환각: "감사합니다." 같은 자막 문장은 발화 속 말소리가 0.5초 미만이거나 신뢰도(평균 로그 확률)가 -0.4 미만이면 버린다(`is_hallucination`).
- v1(`realtime_mic_app.py`)에도 같은 규칙을 옮겼다. v1의 지식 방식은 설정 "찾는 방식"에서 고르며 기본은 "필요할 때 찾기"(`tool`).
