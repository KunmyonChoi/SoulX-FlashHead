# 음성 에이전트 프레임워크 v2: 구현 계획

무엇을 만드는지는 `PRD.md`, 구조와 인터페이스는 `ARCHITECTURE.md`, 타입의 정본은 `agentkit/contracts.py`.

## 1. 진행 방식

1. **0단계 (총괄, 끝남)**: v1 보존, 문서 세 개, 공통 계약(`contracts.py`), 공용 모듈(`config.py`, `llm.py`).
2. **1차 병렬 (작업 7개)**: 계약만 보고 서로 독립으로 만든다. 다른 모듈이 아직 없으면 계약에 맞는 가짜로 시험한다.
3. **통합 (총괄)**: 모듈을 붙여 서버를 띄우고, 어긋난 곳을 고친다.
4. **2차 (작업 1~2개)**: 제작 에이전트, 통합에서 드러난 빈 곳.
5. **검증 (총괄)**: 4절의 확인 절차를 전부 실행한다.

## 2. 작업 분장

| 작업 | 범위 | 담당 요구사항 | 만드는 파일 |
|---|---|---|---|
| WP1 정의서와 배포 | 정의서 읽기·검사, 버전 저장, 승인·배포 규칙, 정의서 6종, REST | R-DEF-1~6, R-REL-1~6, R-SHOP-5 | `agentkit/definition/`, `agents/`, `tests/test_definition_*.py` |
| WP2 도구와 더미 | 게이트웨이, 계약서, 연결 방식 4종, 더미 쇼핑몰, v1 도구 이전, REST | R-TOOL-1~8, R-SHOP-1~2 | `agentkit/tools/`, `agentkit/dummy/`, `tools/*.yaml`, `tests/test_tools_*.py` |
| WP3 기록과 가리기 | 기록기, 가리기, 저장소, 조회·집계·실시간 REST, 보관 기한 | R-OBS-1~7, R-MON-5(집계) | `agentkit/trace/`, `tests/test_trace_*.py` |
| WP4 대화 엔진과 서버 | ReplyEngine, 프롬프트, 검사, 지식 검색, 넘김, 글 채널, 서버 조립 | R-RT-1, 4~8, 10(언어·길이), R-DEF-5, R-SHOP-3~4 | `agentkit/runtime/{engine,prompt,guards,knowledge,driver,__init__}.py`, `agentkit/server.py`, `tests/test_runtime_*.py` |
| WP5 음성과 아바타 | VoiceSession, 음성 부품, 영상 옵션, 동시 세션 | R-RT-2~4, 9~11, R-OBS-5(음성 넘기기) | `agentkit/runtime/{voice,audio,avatar}.py`, `tests/test_voice_*.py` |
| WP6 평가 | 분석, 가상 고객, 사례, 시험 한 벌, 비교, REST | R-EVAL-1~8 | `agentkit/eval/`, `tests/test_eval_*.py` |
| WP7 화면 | 대화 화면, 모니터, 제작실 | R-MON-1~6, 화면 쪽 R-REL, R-EVAL, R-BLD | `web/`, `tests/test_web_*.py` |
| WP8 제작 에이전트 (2차) | 대화로 정의서 만들기·고치기, 시험·비교·승인 요청 | R-BLD-1~6 | `agentkit/builder/`, `tests/test_builder_*.py` |

### 모듈 사이의 약속

| 쓰는 쪽 | 제공하는 쪽 | 약속 |
|---|---|---|
| WP4 | WP1 | `DefinitionStore.load/resolve` |
| WP4 | WP2 | `ToolGateway` |
| WP4, WP2, WP5 | WP3 | `Recorder` |
| WP5 | WP4 | `ReplyEngine` (이벤트 `filler`, `piece` 등), 서버의 `/ws` 연결 지점 |
| WP6 | WP4 | `TextConversation` (`ConversationDriver`) |
| WP6 | WP3 | `store.get_session`, `put_document` |
| WP6 | WP1 | `store.load`, `attach_test_run` |
| WP7 | 전부 | REST와 WebSocket (ARCHITECTURE 6절) |
| WP8 | WP1, WP6 | `DefinitionStore` 쓰기, `run_suite`, `compare` |

서버의 `/ws` 음성·아바타 분기: WP4의 `server.py`가 `channel`이 `voice`나 `avatar`면 `agentkit.runtime.voice.serve_voice(ws, engine, channel, settings)`를 부른다(없으면 "음성 채널이 설치되지 않았습니다" 오류). WP5가 이 함수를 만든다.

## 3. 각 작업의 완료 조건

모든 작업 공통:

- 자기 파일만 만든다. `contracts.py`, `config.py`, `llm.py`, v1 파일, 남의 폴더는 고치지 않는다.
- `tests/test_<모듈>_*.py`가 `cd v2 && python -m pytest tests/test_<모듈>_*.py -q`로 통과한다.
- 계약과 다르게 만든 것, 못 끝낸 것, 다른 모듈에 바라는 것을 `docs/notes/<모듈>.md`에 적는다.
- 공용 서비스(Ollama, Supertonic, Qwen3-ASR, WeKnora)와 v1(7861)을 끄지 않는다. 띄운 시험 서버는 끝나면 내린다.

### WP1
- 정의서 6종: `tech-trend-anchor`(v1과 같은 지시, 도구 7개, 지식 검색, 아바타), `shop-support`(PRD S11, R-SHOP 전부, 가상 고객 6명 이상, 사례 2개 이상), 뼈대 `drive-thru-order`, `kiosk-order`, `flight-rebooking`, `clinic-intake`(형식이 담을 수 있는지 확인용, 버전 1 draft).
- 두 주력 에이전트는 버전 1이 `deployed`(stable) 상태로 들어 있다.
- 틀린 정의서는 파일과 항목을 짚는 `DefinitionError`를 낸다.
- 승인·배포 규칙 위반은 모두 `ReleaseError`. 시험으로 하나씩 확인한다.
- canary 비율이 대화 수에 대략 맞게 나뉜다(같은 `session_key`는 늘 같은 버전).

### WP2
- 쇼핑몰 도구 5개와 v1 도구 7개의 계약서.
- 두 단계 실행, 중복 방지, `requires`/`grants`, 실패 유형 6가지를 시험으로 확인한다. 특히: 같은 턴의 재호출은 실행되지 않는다. 동의가 아닌 말 뒤에는 실행되지 않는다. `speculative`면 실행되지 않는다. 실행된 요청의 재호출은 다시 실행되지 않는다.
- `http` 연결을 더미 쇼핑몰을 HTTP로 띄워(`python -m agentkit.dummy.shop_server --port 7895`) 확인한다.
- `replay` 연결: 기록해 둔 응답으로 같은 결과를 낸다.

### WP3
- 기록기 호출이 1ms 안에 돌아온다. 저장소를 지워 쓰기가 실패해도 예외가 나오지 않는다.
- 가리기: ARCHITECTURE 8절의 표 전부에 대한 시험. 금액과 날짜는 남는다.
- 시험·가상 세션만 음성이 저장된다.
- `purge`가 기한 지난 세션과 음성을 지운다.
- `/api/live`가 새 기록을 SSE로 내보낸다.
- `metrics`: 에이전트·버전별 응답 지연 중앙값(`t_agent_start - t_user_end`), 구간별 중앙값, 도구 실패율, 넘김 비율, 분석 통과율(`documents(kind="analysis")`에서).

### WP4
- `shop-support`로 글 대화: 본인 확인 → 주문 조회 → 환불 제안 → 동의 → 접수. 실제 Ollama로 돌리는 시험 1개 이상(느려도 됨, `-m llm` 표시).
- 가짜 LLM으로 돌리는 빠른 시험: 프롬프트 조립, 도구 왕복, 검사 규칙(다시 생성, 문장 삭제), 넘김, 취소된 선제 응답이 이력을 남기지 않음, `truncate_last_reply`.
- 서버가 definition/tools/trace/eval/builder 모듈 없이도 뜨고, 있으면 라우터를 붙인다.
- 글 채널로 동시에 대화 3개가 된다.
- 대화가 끝나면(`close`) `agentkit.eval.analyze_session`이 있으면 백그라운드로 부른다.

### WP5
- 음성 채널: 16kHz PCM을 넣으면 인식 → 답변 → `FHAU` 음성이 나온다. Supertonic으로 만든 음성을 넣는 통합 시험.
- 끼어들기(`unmute`): 재생 위치 보고 뒤 `engine.truncate_last_reply`가 들린 데까지로 불린다.
- 아바타 채널: v1 `Engine`을 늦게 올리고 `FHRT`를 보낸다. 두 번째 아바타 요청은 거절된다. GPU는 0번을 쓴다(`CUDA_VISIBLE_DEVICES=0`).
- 음성 채널 2개가 동시에 된다.
- WP4의 엔진이 아직 없으면 `ReplyEngine` 가짜로 시험한다.

### WP6
- 분석: 요약, 추출(`pii` 항목은 채워졌는지만), 모델 판정, 수치 판정. 판정마다 근거 턴.
- 가상 고객이 목표를 이루면 스스로 끝낸다.
- `run_suite` → `TestRun` 저장 → 버전에 붙임. `compare`가 좋아진·나빠진 항목을 가른다.
- WP4, WP3이 아직 없으면 `ConversationDriver`와 저장소 가짜로 시험한다. 있으면 실제로 `shop-support` 한 벌을 돌린다.

### WP7
- 세 화면이 프레임워크 없이 순수 HTML/CSS/JS다. 서버가 없을 때는 "서버에 연결할 수 없습니다"를 보여 준다.
- 대화 화면: 에이전트와 채널 선택, 글·음성·아바타. v1 화면의 "무대 뒤" 흐름 표시를 가져오되 도구 목록은 서버에서 받는다. 변경 도구의 확인 대기, 넘김 상태를 표시한다.
- 모니터: 대화 목록, 턴 카드와 지연 막대(구간별 색), 기준 초과 강조, 턴 상세, 분석 결과, 추세, `/api/live`로 갱신.
- 제작실: 버전 목록과 상태, 차이 보기, 시험 실행과 결과·비교표, 승인·배포·되돌림 버튼, 제작 에이전트 대화창.
- 가짜 REST 서버(작업 안에서 만든 시험용)로 화면을 띄워 헤드리스 브라우저 스크린샷으로 확인한다. 브라우저 시험은 시스템 파이썬(`python3`, playwright 설치됨)으로 돌린다.

### WP8 (2차)
- "환불 금액이 5만 원을 넘으면 사람에게 넘겨"라고 하면 새 초안이 생기고 넘김 규칙이 바뀌며 `safety_change`가 참이다.
- "말투를 더 짧게"는 `safety_change`가 거짓이다.
- 새 에이전트를 대화로 만들 때 빠진 결정(본인 확인 방법 등)을 되묻는다.
- 승인과 배포를 시도할 방법이 없다(도구에 없음). 시험으로 확인한다.

## 4. 최종 확인 절차 (PRD 7절)

통합 서버 `cd v2 && python -m agentkit.server --port 7871`에서 실행한다.

| 번호 | 확인 | 방법 |
|---|---|---|
| V1 | 두 에이전트가 정의서만으로 뜬다 | `/api/agents`, 각각 글 대화 한 턴 |
| V2 | S11을 끝까지 수행 | 글 채널로 소액 환불(접수)과 고액 환불(넘김). 더미의 환불 기록 확인 |
| V3 | 음성·아바타 채널 | 합성 음성을 넣어 한 턴. 아바타는 프레임 수신 확인 |
| V4 | 기록과 분석이 저장되고 화면에 보인다 | `/api/sessions/{sid}`, `/analysis`, 모니터 화면 스크린샷 |
| V5 | 개인정보 원문이 없다 | `trace.sqlite3`에서 시험에 쓴 주문번호, 전화번호 뒷자리, 이름을 검색해 0건 |
| V6 | 가상 고객 시험 한 벌 | `/api/tests/run` → 결과표 |
| V7 | 말로 수정 → 시험 → 승인 → 배포 → 되돌림. 승인 없는 배포는 거절 | `/api/builder/chat`, `/api/releases/...` |
| V8 | 변경 도구 안전 | 동의 없는 실행 0, 중복 실행 0 (V2, V6의 기록에서 집계) |
| V9 | v1이 그대로 실행된다 | 7861에 글 대화 한 턴 |
| V10 | 동시 대화 | 글 3개, 음성 2개 동시 |

## 5. 요구사항 추적

| 요구사항 | 작업 | 확인 |
|---|---|---|
| R-DEF-1~3, 6 | WP1 | WP1 시험 |
| R-DEF-4~5 | WP1, WP4 | V1 |
| R-RT-1, 5~8 | WP4 | V1, V2 |
| R-RT-2~3, 9~11 | WP5 | V3 |
| R-RT-4 | WP4, WP5 | V10 |
| R-RT-10 | WP4(언어·길이), WP5(말 끝·인식) | V3 |
| R-TOOL-1~3, 6~8 | WP2 | WP2 시험 |
| R-TOOL-4~5 | WP2, WP4 | V2, V8 |
| R-OBS-1~2, 6~7 | WP3 | V4 |
| R-OBS-3~4 | WP3, WP2 | V5 |
| R-OBS-5 | WP3, WP5 | WP3 시험 |
| R-MON-1~4, 6 | WP7, WP3 | V4 |
| R-MON-5 | WP3, WP7 | V4 |
| R-EVAL-1~3 | WP6 | V4 |
| R-EVAL-4~8 | WP6 | V6 |
| R-BLD-1~6 | WP8 | V7 |
| R-REL-1~6 | WP1, WP7 | V7 |
| R-SHOP-1~2 | WP2 | V2 |
| R-SHOP-3~4 | WP1(정의), WP2(강제), WP4(지시) | V2, V8 |
| R-SHOP-5 | WP1 | V6 |

## 6. 위험

| 위험 | 대응 |
|---|---|
| 모듈이 계약을 다르게 읽는다 | 타입을 `contracts.py`에 코드로 고정. 다르게 만든 곳은 메모에 적고 통합에서 맞춘다 |
| 모델이 변경 도구를 동의 없이 부른다 | 게이트웨이가 턴 순서와 동의를 강제한다. 모델을 믿지 않는다 |
| 모델이 본인 확인을 건너뛴다 | `requires: [verified]`로 조회 도구 자체가 거절한다 |
| 가리기가 놓친 개인정보 | 계약서의 필드 표시와 글 패턴 두 겹. V5로 확인. 놓친 유형은 메모에 남긴다 |
| GPU 메모리 | 영상 엔진은 아바타 요청 때만 올린다. 시험 중 0번 GPU에 v1과 v2가 함께 올라간다(여유 약 30GB) |
| 여러 작업이 Ollama를 함께 써서 느리다 | LLM을 쓰는 시험은 최소로, 나머지는 가짜 LLM |

## 7. 진행 상태 (2026-11-20)

WP1~WP8과 확인 절차 V1~V10은 11/17에 끝났다(`RESULT.md`). 그 뒤 추가 작업(음성 인식 품질, 화면 표시와 키오스크, 뼈대 세 개 완성, 비서, 도구 묶음, 엔진 준비와 내림)도 들어갔다. 목록과 시험 결과는 `notes/followups-2026-11-18.md`.

남은 일:
- 시험 완료된 네 에이전트(키오스크, 드라이브스루, 항공, 의원)의 승인과 배포 (사람)
- 비서 에이전트의 가상 고객 시험 한 벌
- 새로 완성된 세 에이전트의 시험 항목 보강(지금 4~5개, 저장된 사례 없음)
- 모듈별 메모(`notes/*.md`)를 11/18 변경에 맞게 갱신
