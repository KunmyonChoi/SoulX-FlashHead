# runtime (WP4): 대화 엔진과 서버 메모

파일: `agentkit/runtime/{__init__,engine,prompt,guards,knowledge,driver}.py`, `agentkit/server.py`,
`tests/test_runtime_{engine,server,llm}.py`, `tests/runtime_fakes.py`(시험용 가짜, 시험 아님), `pytest.ini`.

시험: `cd v2 && python -m pytest tests/test_runtime_*.py -q` → 45개 통과(2026-11-17, 다른 모듈이 모두 있는 상태).
`-m "not llm"`이면 Ollama 없이 41개. `llm` 4개 중 2개는 definition/tools/trace 모듈과 `shop-support`가 없으면 건너뛴다.

## 1. 계약과 달라진 점

| 무엇 | 계약 | 구현 | 이유 |
|---|---|---|---|
| 검사 뒤 다시 생성할 때의 안내 | "system note로 붙인다" (`Guard` 설명) | `user` 역할의 메시지로 대화 끝에 붙인다(이력에는 남기지 않음). 이미 말한 문장이 있으면 그것을 `assistant` 메시지로 먼저 넣고 "이어서 말하세요"를 덧붙인다 | v1과 같은 방식. 대화 중간의 두 번째 system 메시지는 Ollama 템플릿에서 믿을 수 없다 |
| 검사 단위 | "문장" | 말하기 조각(첫 조각 15자 이상, 이후 40자 이상) 안에서 걸린 **문장 하나**만 거둔다. 그 앞의 문장은 그대로 내보낸다 | 조각째 거두면 죄 없는 문장까지 사라진다 |
| `claims_lookup`의 `unless_tools`가 빈 목록 | 정해져 있지 않음 | "이번 답에서 도구를 하나도 부르지 않았을 때" 위반 | 빈 목록이면 항상 위반이 되는 것보다 쓸모 있다 |
| "이번 답에서 불렀다" | `called for this reply` | 그 턴에 부른 도구만 센다. 앞 턴에 조회한 것을 다시 말하며 "조회해 보니"라고 하면 걸린다 | 계약 그대로. v1은 앞선 조회 결과의 숫자를 봐줬지만 그건 날씨 전용 규칙이라 옮기지 않았다 |
| 도구 호출 시점 | v1은 스트림 중 바로 병렬 실행 | 그 생성이 끝난 뒤 **나온 순서대로 하나씩** 실행 | 한 번에 `verify_customer` → `get_order`가 나오면 순서가 지켜져야 한다 |
| 확인이 필요한 변경 도구 뒤 | 없음 | 그 답의 다음 생성에는 도구를 주지 않는다 | 모델이 같은 턴에 곧바로 다시 불러 `not_confirmed`를 받는 것을 막는다. 엔진은 스스로 확인하지 않는다 |
| 정의서에 없는 도구 호출 | 없음 | 게이트웨이를 부르지 않고 `invalid_input` 결과를 모델에 준다 | `tools` 목록은 권한이다 |
| 빈 답 / LLM 오류 | v1은 `notice` | 한 번 다시 묻고, 그래도 비면 사과 문장을 `piece`로 내고 `done.data.fallback=True` (오류면 `done.data.error`) | `ReplyEvent`에 notice가 없고, 음성에서 침묵보다 낫다 |
| 취소된 **일반** 답 | 언급 없음 | 만든 데까지 이력에 남기고 `TurnRecord(interrupted=True)`를 보낸다 | 끼어들기는 취소 뒤 `truncate_last_reply`로 이어진다 |
| `TurnRecord`를 다시 보냄 | "턴마다 하나" | `truncate_last_reply`, `done` 뒤에 온 `mark_output("reply")`가 같은 턴의 기록을 고쳐서 다시 보낸다 | 음성은 첫 소리가 `done`보다 늦을 수 있다. **WP3는 (session, turn)으로 덮어써야 한다** |
| 선제 응답의 구간 기록 | 언급 없음 | 끝날 때까지 엔진이 들고 있다가 완료되면 보낸다. 취소되면 버린다. 게이트웨이가 직접 쓰는 `tool` 구간은 남는다 | "흔적을 남기지 않는다" |
| 계약 밖 메서드 | - | `engine.discard_last_turn()`(끝난 선제 응답을 통째로 지움. 이미 나간 TurnRecord는 못 거둔다), `engine.set_options(language=, reply_length=)`(채널 설정), `engine.history`, `engine.closed` | WP5가 `discard_last_turn`을 쓴다 |
| `filler` WebSocket 메시지 | 6절 표에 없음 | 글 채널이 `{"type":"filler","id","text"}`를 보낸다 | 글 화면도 추임새를 보여 줄 수 있게 |
| `chat_done`, `chat_replace` | `id`, `text` | `chat_done`에 `turn`, `handoff`(, `fallback`, `error`), `chat_replace`에 `guard_id`, `action` 추가 | |
| `ready` | 표의 필드 | 글 채널은 `name`, `language`, `tools`도 넣는다 | |
| `serve_voice` 인자 | PLAN은 4개, 작업 지시는 3개 | 서명을 보고 `settings`를 받으면 질의 문자열 전체를 dict로 넘긴다 | 둘 다 맞게 |
| 서버 기본 host | 없음 | `127.0.0.1` (v1과 같음). 바꾸려면 `--host` | |
| `create_app(engine_factory=None)` | 인자 없음 | 시험이 가짜 엔진을 넣을 수 있게 선택 인자 하나 | |
| `/api/health` | 없음 | 붙은 라우터, 없는 모듈, 열린 세션 수 | 통합 확인용 |
| `ToolContext.test` | 시험·가상 세션 | `test or simulated` | 계약 설명대로 |
| 넘김 `rule_id` | 도구 인자에 없음 | 인자에 `rule_id`가 있으면 그것, 없으면 `reason`에 규칙 id가 들어 있거나 규칙이 하나뿐이거나 `when`과 낱말이 가장 많이 겹치는 규칙. 못 찾으면 `""` | 추정이다. 틀릴 수 있다 |

v1에서 옮기지 **않은** 것: 요일·날짜 검사(`wrong_date`), 예보 기온·날씨 문장 검사, `list_knowledge_by_date` 안내문,
`TOOL_GUIDE`(날씨·뉴스·긱뉴스·위키 안내). 모두 특정 에이전트의 내용이라 정의서(`persona`, `flow`, `guards`)에 있어야 한다.
정규식으로 표현되지 않는 요일 검사는 지금의 `Guard` 종류로는 담을 수 없다.

## 2. 못 끝냈거나 알고 있는 한계

- 넘김은 `handoff_to_human` 도구가 실행될 때만 일어난다. 모델이 도구를 부르지 않으면 엔진이 대신 넘기지 않는다.
- 도구 호출 중 취소되면 `tool` 이벤트의 "cancelled" 상태는 나가지 않는다(반복자가 취소되는 중이라 낼 수 없다).
- 이력은 메시지 40개쯤에서 자른다. 요약은 없다.
- `live_clock`의 지금 시각은 그 턴의 사용자 말에만 붙는다(v1과 같음). 영어 턴의 달력은 영어로 쓴다.
- 지식 검색 제한 시간은 1.5초(v1 값). 첫 검색이 시간 초과로 빠지는 것을 한 번 봤다(답은 자료 없이 나간다).
- 추임새는 그 답에서 아직 아무 말도 안 했을 때만, 첫 도구 호출 앞에 한 번 낸다. 계약서의 `filler`가 그 턴의 언어와 다르면 기본 문장을 쓴다.
- 글 채널의 `config` 메시지는 `language`, `length`/`reply_length`만 받는다.
- 서버 시험의 "모듈 없이 뜬다"는 `sys.modules`에서 막아서 확인했다. 실제로 폴더를 지운 상태로는 돌려 보지 않았다.

## 3. 다른 작업에 바라는 것 (부르는 곳)

**WP1** (`runtime/__init__.py: create_engine`)
- `from agentkit.definition import DefinitionStore`; `DefinitionStore()`; `store.resolve(agent_id, session_key)`(`version`이 없을 때, `session_key`가 없으면 세션 id), `store.load(agent_id, version)` → `AgentDefinition`. 예외는 그대로 올리고 서버가 `error` 메시지로 바꾼다.
- 저장소 객체는 프로세스에 하나를 두고 계속 쓴다. `load`/`resolve`가 매번 파일을 읽어야 배포 변경이 반영된다.
- `server.py`: `agentkit.definition.api.router`.

**WP2** (`create_engine`, `engine.py`)
- `from agentkit.tools import build_gateway`; `build_gateway(recorder=recorder)` 한 번(프로세스 공용).
- `gateway.names()`, `gateway.contract(name)`(`effect`, `confirm_template`, `filler`를 읽는다), `gateway.specs(names)`, `await gateway.call(ctx, name, arguments)`, `gateway.end_session(session_id)`(엔진 `close`에서).
- `tool` 구간은 게이트웨이가 쓴다. 엔진은 쓰지 않는다.
- `needs_confirmation=True`일 때 `confirmation_text`가 있어야 한다. 엔진이 도구 결과 뒤에 "그대로 읽고 동의를 구하라"는 안내를 붙인다.
- 실패 `text`는 고객에게 전할 문장. 엔진이 앞에 `[실패: <error_type>]`을 붙인다.
- `handoff_to_human`은 `ok=True`이고 `needs_confirmation`이 아니면 넘김으로 본다.
- `server.py`: `agentkit.tools.api.router`.

**WP3** (`create_engine`, `engine.py`, `server.py`)
- `from agentkit.trace import get_recorder`. `start_session(SessionMeta)`(엔진 생성자), `span(Span)`, `turn(TurnRecord)`, `end_session(session_id, status)`.
- **`turn()`은 같은 (session_id, turn)이 다시 오면 덮어써야 한다**(위 표).
- 엔진이 쓰는 구간: `retrieval`, `llm`(input에 메시지 전체), `guard`, `output`(attrs에 `phase`, `ms`), `handoff`, `interrupt`(`truncate_last_reply`에서).
- 세션이 끝나면 서버가 분석 전에 `get_recorder().flush()`를 실행기에서 부른다(있으면).
- `server.py`: `agentkit.trace.api.router`.

**WP5** (`server.py: ws_endpoint`)
- `from agentkit.runtime import voice`; `await voice.serve_voice(ws, engine, channel, settings)`(지금 WP5의 서명). 소켓은 이미 `accept`됐고, `ready`는 WP5가 보낸다. 돌아오거나 예외가 나면 서버가 `engine.close()`와 분석을 맡는다. 엔진을 닫지 않아도 된다(두 번 닫아도 괜찮다).
- `reply()`는 한 번에 하나만. 새로 부르면 끝나지 않은 앞의 답을 엔진이 취소 처리한다.
- `mark_output("filler"|"reply", t)`는 턴마다 처음 것만 센다. `done` 뒤에 와도 된다.

**WP6** (`server.py: _analyze`, `driver.py`)
- `from agentkit.eval import analyze_session`; `analyze_session(session_id)`. 코루틴이면 그대로, 아니면 실행기에서 돌린다. 턴이 하나도 없는 세션은 부르지 않는다. 실패는 로그만 남긴다.
- `TextConversation.open(agent_id, version=None, test=True, simulated=True, label="")`, `say(text) -> AgentTurn`, `close(status)`. `tool_calls` 항목에는 계약의 다섯 키에 `needs_confirmation`, `effect`가 더 있다. `driver.events`에 마지막 턴의 이벤트가 있다.
- `server.py`: `agentkit.eval.api.router`. (`agentkit.builder.api.router`도 있으면 붙인다.)

## 4. 로컬 모델(gemma4:26b)이 규칙을 따르게 하려고 프롬프트에 넣은 것

- **확인 문장은 도구 결과에 넣는다.** 시스템 지시의 두 단계 규칙만으로는 부족해서, `needs_confirmation` 결과 바로 뒤에 "(시스템 안내: 아직 아무것도 실행되지 않았습니다. 다음 확인 문장을 그대로 읽어 주고… 지금은 다시 호출하지 마세요.)"를 붙인다. 그리고 그 다음 생성에는 도구를 아예 주지 않는다.
- **넘김도 같다.** `handoff_to_human`이 성공하면 결과 뒤에 "한 문장으로 알리라"는 안내를 붙이고 도구를 뺀다.
- 변경 도구 규칙에 도구 이름을 적는다(`변경 도구(request_refund)는…`). `confirm_template`이 있는 변경 도구만.
- 도구 규칙 세 줄: 추측 금지, 값이 없으면 지어내지 말고 묻기, 결과에 없는 말 금지.
- 넘김 조건 뒤에 "도구를 호출하지 않고 연결했다고 말하지 마세요".
- `short`도 길이 지시를 낸다(v1은 기본 지시문에 들어 있어 없었다).
- 언어가 `auto`인데 그 턴이 영어면, 한국어로 쓴 persona 바로 뒤에 "이 대화는 영어" 덮어쓰기 문장을 넣고 나머지 틀은 영어로 쓴다. 정의서의 규칙 문장 자체는 번역하지 않는다.
- 지금 시각은 시스템 지시가 아니라 사용자 말 앞에 둔다(프롬프트 캐시). 참고 자료도 같은 자리, 그 턴에만.

관찰: 실제 대화에서 모델이 `request_refund`를 부르기 전에 스스로 "환불해 드릴까요?"라고 한 번 더 묻는 턴이 생긴다
(확인이 두 번). 동의 없는 실행은 없었다. 표본은 대화 4개뿐이다.

## 5. 화면 표시, 여는 말, 터치 (DISPLAY.md의 B 작업, 2026-11-17)

파일: `engine.py`, `prompt.py`, `driver.py`, `__init__.py`, `server.py`, `tests/test_runtime_display.py`(새 시험 14개),
`tests/test_runtime_server.py`(시험 2개 추가), `tests/runtime_fakes.py`(`MiniCafe`, `make_cafe_definition`).
시험: `python -m pytest tests/test_runtime_*.py -q -m "not llm"` → 70개 통과.

### 동작

- **화면(`display`)**: 도구 결과의 `ToolResult.display` 항목마다 그 도구의 `tool`(done) 이벤트 바로 뒤에
  `ReplyEvent(type="display", data={kind, data, source})`를 내고 `Span(kind="display", name=kind, output={"data"},
  attrs={"source"})`를 기록한다. 내용은 게이트웨이가 준 그대로이고 모델을 거치지 않는다. 선제 응답의 `display` 구간은
  다른 구간과 같이 끝날 때까지 들고 있다가, 취소되면 버린다.
- **여는 말(`engine.open()`)**: `opening.tools`를 순서대로 게이트웨이로 부른다(`ToolContext.turn=0`, `speculative=False`).
  `tool`(start, done), `display` 이벤트를 내고, `opening.say`를 `piece` 하나와 `done`으로 낸다. `delta`는 내지 않는다
  (계약 문구대로). 채널이 `piece`를 글로도 보여 준다. `TurnRecord(turn=0, user_text="", agent_text=say)`를 보낸다.
  `engine.turn`은 0 그대로다. 여는 말이 없는 정의서, 두 번째 호출, 이미 대화가 시작된 뒤의 호출은 `done`만 낸다.
- **여는 말의 이력**: 이력 맨 앞에 `user "(대화가 시작되었습니다. 고객이 방금 도착했습니다.)"`, `assistant(tool_calls)`,
  `tool`(도구의 `text`), `assistant(say)` 순서로 들어간다. 이 묶음은 **이력 자르기(40개)에서 빠진다**. 대화가 길어져도
  모델이 메뉴의 id와 가격을 잃지 않게 하려는 것이다.
- **터치(`engine.act(name, arguments)`)**: `definition.actions`에서 찾고, 대응 도구의 계약서를 게이트웨이에서 본다.
  모르는 동작, 그 에이전트의 `tools`에 없는 도구, `confirm_template`이 있는 도구는 `done`(`data.error`)만 내고 아무것도
  바꾸지 않는다. 받아들인 터치는 고객 턴으로 센다: `Span(kind="action", name=동작, input={"arguments"}, attrs={"tool"})`,
  게이트웨이 호출(`tool`, `display` 이벤트), 그리고 **도구 없이** 모델이 한 번 답한다. 그 턴의 `user` 말은
  `ClientAction.say`를 채운 문장이고(괄호가 없으면 붙인다) `TurnRecord.user_text`도 그 문장이다.
- **프롬프트**: `screen.panels`가 있으면 말하기 규칙 다음에 "화면: 고객은 화면으로 메뉴와 주문 내역을 보고 있습니다 …
  전체를 말로 읽지 말고 바뀐 것과 다음에 할 일만" 문장이 들어간다(한국어, 영어). `actions`가 있으면 "화면을 눌러 직접
  처리할 수도 있으며, 괄호 문장으로 전달되고 이미 처리된 것" 문장이 붙는다.
- **드라이버**: `TextConversation.opening()`, `.act(name, arguments)`, `AgentTurn.displays`. 거절된 터치는 빈 턴이고
  이유는 `driver.events[-1].data["error"]`.
- **글 채널**: `ready`에 `screen`, `actions`, `opening` 추가(`runtime.client_features()`, 음성 채널도 같은 함수를 쓴다).
  `opening`이 참이면 `ready` 바로 뒤에 `engine.open()`을 돌려 `tool`, `display`, `chat_delta`, `chat_done`을 모두
  `id: "opening"`으로 보낸다. `{"type":"action"}`은 `chat`과 같게 처리한다(진행 중인 답을 끊고 새 턴).

### 계약·설계 문서와 다르거나 정해져 있지 않아 정한 것

| 무엇 | 문서 | 구현 | 이유 |
|---|---|---|---|
| 여는 말 이력의 첫 메시지 | 없음 | 괄호 안내문을 `user` 역할로 하나 넣는다 | 대화가 `assistant`의 도구 호출로 시작하면 채팅 템플릿(특히 gemma)이 어긋날 수 있다 |
| 여는 말 이력의 수명 | "이력에 남는다" | 이력 한도에서 제외(항상 남는다) | 메뉴를 잃으면 id를 지어낸다 |
| 터치 뒤 모델의 답 | "한 번 짧게 반응" | 그 턴의 생성에는 도구를 주지 않는다. 도구 결과 뒤에 "(시스템 안내: 고객이 화면을 눌러 직접 한 동작이며 이미 처리되었습니다. 같은 도구를 다시 호출하지 마세요 …)"를 붙인다. 실패한 터치는 "처리되지 않았습니다 … 위 안내를 전하세요" | 같은 터치에 같은 도구를 한 번 더 부르는 것을 구조로 막는다. 대신 터치에 대한 답에서는 다른 조회도 못 한다 |
| 터치 문장의 `{field}` | "인자에서" | 인자가 먼저이고, 없으면 도구 결과 `data`의 맨 위(와 한 단계 아래) 값에서 채운다. 없는 것은 빈칸 | 인자에는 `item_id`만 있고 이름은 결과에만 있다 |
| `say`가 비어 있는 동작 | 없음 | "(화면에서 {동작 이름} 동작을 했습니다)" | |
| 터치로 `handoff_to_human` | 없음 | 확인 문장이 없으면 허용되고, 음성 턴과 같게 넘김 처리된다 | 같은 코드 경로 |
| 넘김 뒤의 터치 | 없음 | 턴으로 세고 "상담원에게 연결되었습니다"만 답한다. 도구는 부르지 않는다 | `reply()`와 같게 |
| 터치의 지식 검색 | 없음 | 하지 않는다 | 사람이 한 말이 아니다 |
| 거절된 터치의 WebSocket 응답 | "일반 답과 같은 메시지들" | `{"type":"chat_done","id","text":"","error":"..."}` 하나 | |
| 글 채널에서 여는 말 실행 | "연결 직후" | 받기 루프에 들어가기 전에 끝까지 돌린다(취소되지 않는다). 그동안 온 메시지는 그 뒤에 처리된다 | 여는 도구가 취소되면 메뉴 화면이 없다 |
| `say`가 없는 여는 말 | 없음 | `chat_delta` 없이 `chat_done`(text `""`)만 간다 | 화면은 빈 말풍선을 만들지 않게 주의 |

### 알고 있는 한계

- **턴 0의 `TurnRecord`**: 저장소와 지표가 턴 0을 고객 턴처럼 셀 수 있다(턴 수, 응답 시간 중앙값). `user_text`가 빈
  문자열이고 `turn == 0`인 것으로 구분할 수 있다. trace/eval 쪽에서 확인이 필요하다.
- 서버는 `engine.turn == 0`이면 세션을 `abandoned`로 닫고 분석도 하지 않는다. 여는 말만 듣고 떠난 세션이 여기에 든다(의도한 것).
- 터치 문장에 `item_id`가 그대로 보일 수 있다("(화면에서 vanilla_latte 담았습니다)"). 도구 결과 `data`에 `name`이 있으면
  `say`에 `{name}`을 쓰면 된다.
- 도구를 못 쓰는 모델(`supports("tools")`가 거짓)에서도 여는 도구와 터치는 실행되고 이력에 도구 메시지가 남는다.
