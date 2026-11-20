# WP2 도구와 더미: 메모

## 만든 것

- `agentkit/tools/`: `loader.py`(계약서 읽기·검사), `validate.py`(입력 검사), `consent.py`(동의 판정), `adapters.py`(연결 방식 4종), `gateway.py`(게이트웨이, `build_gateway`), `v1_lookup.py`(v1 조회 도구 7개), `api.py`(`router`), `testing.py`(다른 모듈도 쓸 수 있는 `FakeRecorder`, `make_ctx`, `ScriptedAdapter`).
- `agentkit/dummy/`: `shop.py`(더미 쇼핑몰, 주문 22건), `shop_server.py`(같은 것을 HTTP로).
- `tools/*.yaml`: 쇼핑몰 5개, v1 7개.
- `tests/test_tools_{gateway,shop,contracts,adapters}.py`.

시험: `cd v2 && python -m pytest tests/test_tools_*.py -q` → 216 통과, 3 건너뜀(인터넷·WeKnora).
건너뛴 3개는 `AGENTKIT_NETWORK_TESTS=1`을 주면 돈다(2026-11-17에 3개 모두 통과: 날씨, 위키백과, 뉴스, 긱뉴스, WeKnora 목록). `-m "not network"`로도 뺄 수 있다.
`test_tools_adapters.py`는 7895 포트에 더미 쇼핑몰 서버를 띄웠다가 내린다. 포트가 이미 쓰이고 있으면 그 시험 4개를 건너뛴다.

## 계약과 다르게 만든 것

| 항목 | 계약·문서 | 구현 | 이유 |
|---|---|---|---|
| 동의가 와야 하는 때 | "LATER turn" | **바로 다음 턴**만 (`consent_window=1`). 더 늦게 오면 실행하지 않고 확인 문장을 다시 돌려준다(`needs_confirmation=True`) | R-TOOL-4의 "다음 말에서". 두 턴 뒤의 "네"는 다른 질문에 대한 답일 수 있다. `build_gateway(consent_window=n)`으로 넓힐 수 있다 |
| 동의가 분명하지 않을 때 | `not_confirmed` | `not_confirmed` + `confirmation_text`를 채워 돌려주고, 그 턴을 "다시 물은 턴"으로 삼는다(다음 턴의 동의가 유효) | 고객이 되물었을 때 한 번 더 묻고 진행할 수 있어야 한다 |
| 거절일 때 | `not_confirmed` | `not_confirmed`이고 제안을 지운다. 뒤의 "네"는 새 제안부터 다시 시작한다 | 거절한 요청이 나중의 "네"로 살아나면 안 된다 |
| 선제(`speculative`) 변경 호출 | "실행하지 않는다" | 실행도 **제안도** 하지 않고 `ok=False`, `not_confirmed`. 확인 없는 변경 도구(`handoff_to_human`)도 같다 | 버려질 수 있는 턴이 제안을 남기면, 고객이 확인 문장을 듣지 못했는데 다음 "네"로 실행될 수 있다. 엔진은 턴이 확정된 뒤 다시 불러야 한다 |
| 미리보기가 거절될 때 | 언급 없음 | 첫 호출에서 바로 실패를 돌려준다(확인을 묻지 않는다). 예: 10만 원 초과 → `precondition_failed` | 접수가 안 될 것을 고객에게 확인받게 하지 않는다 |
| 쇼핑몰 규정 거절의 유형 | 정해져 있지 않음 | `precondition_failed`, `data.reason`은 `over_limit` / `not_delivered` / `period_over` / `already_refunded`, `data.handoff_recommended` | 실패 유형 7개 중 가장 가깝다. `requires` 부족과 같은 유형이므로 구분은 `data`로 한다 |
| 확인 문장에 들어간 인자가 바뀐 재호출 | 같은 `idempotency_fields`면 같은 요청 | 확인 문장의 자리표시자에 쓰인 인자가 바뀌면 새 제안으로 본다. 그 밖의 인자(`reason`)는 바뀌어도 같은 요청 | 고객이 들은 내용과 다른 것을 실행하지 않는다 |
| 결과를 모르는 변경 실행 | "다시 시도하지 않는다" | 실행 중 시간 초과, 보낸 뒤 끊김, 실행 중 취소는 "결과 모름"으로 두고 같은 요청을 그 세션에서 다시 보내지 않는다(상담원 연결을 안내하는 문장). 연결 자체가 안 된 경우(연결 거부, 503)는 아무것도 안 바뀐 것으로 보고 다시 요청할 수 있다 | 중복 실행 0건 |
| 제한 시간 | `timeout_s` | 재시도를 포함한 **전체** 예산 하나. 시간 초과는 다시 시도하지 않는다 | v1과 같다. 응답 지연이 `timeout_s`를 넘지 않는다 |
| 재시도 대상 | "연결 오류" | 시스템에 닿지 못한 경우만(`AdapterUnavailable`: 연결 거부, 끊김, 502/503/504). 시스템이 답한 거절(`ok: false`)은 다시 시도하지 않는다 | |
| 입력 검사 | 계약서의 JSON Schema | 손실 없는 형 변환을 한다(`"3"`→3, `1234`→`"1234"`, `2.0`→2). 계약서에 없는 인자는 버린다(`additionalProperties: false`면 오류). `pattern`, `minimum`, `maximum`, `minLength`, `maxLength`도 본다 | 모델이 숫자와 글자를 섞어 보낸다 |
| 없는 도구 호출 | 정해져 있지 않음 | `invalid_input` | |
| 기록의 문장 | `pii_paths`를 넘긴다 | `pii_paths`를 넘기고, 더해서 구간의 `output.text`, `output.confirmation_text` 안에 든 표시 필드의 값(이름, 주소, 뒤 4자리)을 `[가려짐]`으로 바꿔서 넘긴다. 모델이 받는 `ToolResult.text`는 그대로다 | 결과 문장이 "김민수 고객님의 주문"처럼 표시 필드의 값을 되풀이한다. 가리기 한 겹 더 |
| 구간 `attrs` | `effect`, `executed`, `error_type` | 더해서 `needs_confirmation`, `adapter`, `phase`, `attempts`, `speculative`, `call_id`, 있으면 `consent`, `proposed_turn`, `reproposed`, `error` | V8 집계용. `phase`: `read`, `proposed`, `executed`, `duplicate`, `same_turn_refused`, `declined`, `unclear_refused`, `speculative_refused`, `preview_refused`, `preview_failed`, `execute_refused`, `execute_failed`, `outcome_unknown`, `blocked_unknown`, `refused`, `cancelled` |
| `GET /api/tools` | "계약서 목록" | `{"tools": [...], "errors": [...]}`. 항목은 계약서 필드 + `needs_confirmation`. 계약서가 깨져 있으면 200에 `errors` | 배열이 아니라 객체다. WP7에 알렸다 |
| REST 추가 | 없음 | `GET /api/tools/{name}`, `GET /api/dummy/shop/records?session_id=&kind=` | 끝난 대화의 환불·넘김 기록 확인(V2) |
| `build_gateway` 인자 | `recorder` | 더해서 `tools_dir`, `adapter_overrides`, `consent_window` | 아래 "연결 방식 전환" |
| `http` 연결의 미리보기 | 언급 없음 | `adapter.options.preview_url`로 같은 본문을 POST. 없으면 미리보기 없음 | |
| `http` 변경 도구와 시험 세션 | "adapters must not touch real systems" | `ctx.test`이고 `options.allow_in_test`가 참이 아니면 보내지 않고 `system_error` | 조회 도구는 시험 세션에서도 나간다(v1 조회 도구가 그렇다) |
| `get_weather`의 `day_offset` 범위 | - | 계약서에 `maximum`을 넣지 않았다 | v1의 안내 문장("7일 뒤까지만")을 그대로 쓰려고 |

## 게이트웨이 규칙과 시험

`tests/test_tools_gateway.py`.

| 규칙 | 시험 |
|---|---|
| 입력 검사 → `invalid_input` | `test_invalid_input_is_refused_before_the_adapter`, `test_unknown_tool_is_invalid_input`, `test_scalar_arguments_are_converted_and_json_strings_accepted` |
| `requires` 없음 → `precondition_failed`, 무엇이 먼저인지 알림 | `test_requires_missing_is_precondition_failed_and_names_what_comes_first` |
| `grants`는 성공했을 때만, 세션별 | `test_grants_set_the_fact_only_on_success_and_per_session`, `test_end_session_forgets_facts_proposals_and_shop_state` |
| 변경 도구 첫 호출은 제안만 | `test_first_call_of_a_write_tool_only_proposes` |
| 같은 턴의 재호출은 실행되지 않는다 | `test_same_turn_repeat_does_not_execute` |
| 뒤 턴 + 동의 → 실행 | `test_later_turn_with_consent_executes` |
| 뒤 턴 + 거절 → 실행 안 됨, 제안 삭제 | `test_later_turn_with_refusal_does_not_execute_and_drops_the_proposal` |
| 뒤 턴 + 동의가 아닌 말 → 실행 안 됨 | `test_later_turn_without_clear_consent_does_not_execute` |
| 오래된 제안은 다시 묻는다 | `test_stale_proposal_is_asked_again_instead_of_executed`, `test_consent_window_can_be_widened` |
| 확인받은 인자가 바뀌면 다시 묻는다 | `test_changed_confirmed_argument_needs_a_new_confirmation` |
| 미리보기 거절, 확인 문장을 못 만들 때 | `test_preview_refusal_is_reported_at_once_without_a_proposal`, `test_missing_template_value_refuses_instead_of_reading_a_broken_sentence` |
| `confirm_template` 없는 변경 도구는 바로 실행 | `test_write_tool_without_confirm_template_executes_at_once` |
| `speculative`면 변경 도구를 실행하지 않는다 | `test_speculative_never_executes_a_write_tool`, `test_speculative_does_not_execute_a_write_tool_without_confirmation`, `test_speculative_read_tools_run` |
| 실행된 요청의 재호출은 처음 결과 + `executed=False` | `test_repeated_executed_request_returns_first_result_with_executed_false`, `test_concurrent_identical_writes_execute_once`, `test_handoff_runs_once_per_conversation_even_with_another_reason` |
| 실패 유형 7가지 | `test_error_types_from_the_system`, `test_every_error_type_reaches_the_model_with_a_korean_sentence`, `test_adapter_bug_becomes_system_error_not_an_exception` (+ 위의 `invalid_input`, `precondition_failed`, `not_confirmed` 시험) |
| 제한 시간 | `test_timeout`, `test_shop_delay_injection_runs_into_the_timeout` |
| 재시도는 조회 도구만 | `test_read_tool_is_retried_when_the_system_is_not_reached`, `test_read_tool_gives_up_after_retries_with_system_error`, `test_a_refusal_by_the_system_is_not_retried`, `test_shop_outage_injection_is_retried_for_read_tools`, `test_write_tool_is_never_retried_even_if_the_contract_asks` |
| 결과를 모르는 변경은 다시 보내지 않는다 | `test_write_with_unknown_outcome_is_not_offered_again`, `test_write_broken_off_midway_is_not_offered_again`, `test_system_refusal_at_execution_changes_nothing_and_grants_nothing` |
| 호출마다 `Span(kind="tool")`, `pii_paths` | `test_every_call_is_recorded_as_a_tool_span`, `test_span_carries_pii_paths_and_no_marked_value_in_its_sentences`, `test_result_text_for_the_model_is_not_masked`, `test_broken_or_missing_recorder_does_not_disturb_the_call`, `test_cancelled_call_is_recorded_and_stays_cancelled` |
| S11 전체 | `test_s11_small_refund_and_large_refund_handoff` |

`http`: `test_tools_adapters.py::test_http_adapter_runs_the_whole_scenario_against_the_shop_server` 등 5개. `replay`: `test_replay_gives_the_recorded_results_again` 등 3개.

## 동의 판정 (`agentkit/tools/consent.py`)

`read_consent(text)`는 `"yes"`, `"no"`, `"unclear"` 중 하나를 낸다. `"yes"`일 때만 실행한다. 순서: 부정이 하나라도 있으면 `no` → 물음표로 끝나면 `unclear` → 긍정이 있으면 `yes` → 나머지 `unclear`.

**동의로 읽는 말** (시험 목록 `YES`):
네, 네네, 넵, 예, 응, 그래(요), 그럼요, 그러세요, 그러죠, 맞아(요), 맞습니다, 좋아(요), 좋습니다, 오케이, 물론(이죠), 당연하죠, 알겠어요, 알겠습니다, 동의(합니다). 이 낱말들은 홀로 선 낱말일 때만 센다("네이버", "예약", "응답", "그렇네요"는 아니다).
"…해 주세요", "…해 줘", "부탁…", "진행해…", "접수해…", "처리해…", "그렇게 해…", "…하겠습니다", "…할게요".
영어: yes, yeah, yep, sure, ok, okay, please, go ahead, do it, correct, right, confirm, agree, absolutely.

**거절로 읽는 말** (시험 목록 `NO`; 긍정과 함께 있어도 거절이 이긴다. "네, 아니 잠깐만요"):
아니, 아뇨, 안 돼, 안 됩, 안 할, 안 해, 않, 취소, 잠깐, 잠시만, 기다려, 하지 마, 하지 말, 말고, 말아, 싫, 그만, 됐어(요), 됐습니다, 나중에, 다음에, 보류, 필요 없, 생각해 볼, 생각 좀, 다시 생각, 몰라, 모르겠, 글쎄.
영어: no, nope, nah, not, don't, do not, never, cancel, wait, stop, hold on, later.

**동의도 거절도 아닌 말** (시험 목록 `UNCLEAR`; 실행하지 않고 다시 묻게 한다):
빈 말, 물음표로 끝나는 말("네?", "얼마라고요?"), 관계없는 말, "음", "어".
"…해 주세요"라도 다른 것을 청하는 말: 말/말씀/설명/얘기/이야기/확인/안내/조회/검색/연결/교환/변경/수정/반복/전달 + "해 주…" ("금액을 다시 말해 주세요", "상담원 연결해 주세요").

알려진 한계:
- 음성 인식이 물음표를 붙이지 않으면 "네?"가 "네"로 읽힌다(동의로 실행된다). 억양은 여기서 알 수 없다.
- "네, 확인해 주세요"는 "네" 때문에 동의로 읽힌다.
- "편안해요"처럼 "안해"가 든 낱말은 거절로 읽힌다(안전한 쪽의 오판).
- "그럼"은 뺐다("그럼 얼마예요"). "어"도 뺐다.
- 판정은 낱말 규칙이다. 뜻을 읽지 않는다. "네, 근데 그 주문 말고 다른 주문이요"는 "말고" 때문에 거절이 되지만, 비슷한 말을 다 잡지는 못한다.

## 더미 쇼핑몰

- 주문 22건(A1001~A1022). 앞의 5건은 지시받은 값 그대로다. 날짜는 "며칠 전"으로 두고 세션이 처음 쇼핑몰에 닿을 때 `config.TIMEZONE`의 오늘로 계산한다.
- 환불 규정 검사 순서: 이미 접수됨 → 배송 완료 전 → 7일 지남 → 10만 원 초과. 7일째 되는 날까지 가능하다(A1020).
- 본인 확인은 주문 하나에 묶인다. 다른 주문번호는 있든 없든 `permission_denied`. 다른 주문으로 다시 확인하면 권한이 그 주문으로 옮겨 간다.
- 뒤 4자리를 3번 틀리면 그 세션은 더 확인하지 못하고 `handoff_to_human`을 안내받는다(지시에 없던 규칙).
- 주문번호는 `a-1001`, `A 1001`도 `A1001`로 읽는다.
- 상태는 세션별이다. `gateway.end_session()`은 그 세션의 주문·확인 상태를 지우지만, 환불·넘김 기록은 남는다: `agentkit.dummy.shop.get_shop().records(session_id=None, kind=None)`, `.refunds(session_id)`. 프로세스 안에만 있고 서버를 다시 띄우면 사라진다. 최근 2000건.
- 지연과 장애(R-TOOL-8): 계약서의 `adapter.options`, `get_shop().inject(...)`, 환경 변수(`AGENTKIT_SHOP_DELAY_S`, `AGENTKIT_SHOP_FAIL`, `AGENTKIT_SHOP_FAIL_TIMES`, `AGENTKIT_SHOP_FAIL_RATE`), 서버는 `--delay-s`, `--fail`, `--fail-times`와 `POST /shop/inject`. 항목: `delay_s`, `fail`(`unavailable` 닿지 않음 / `system_error` / `timeout` 응답 없음 / 그 밖의 실패 유형 이름), `fail_times`(세션·기능별 처음 n번만), `fail_rate`, `fail_methods`, `fail_preview`(미리보기에도 적용).
- `handoff_to_human`은 대화마다 접수 하나다. 다시 부르면 `ok=True`, `executed=False`.

## 연결 방식 전환 (R-TOOL-3)

계약서 파일의 `adapter`를 고치거나, 파일을 두고 덮어쓴다.

```python
build_gateway(adapter_overrides={"get_order": {"kind": "http", "target": "http://127.0.0.1:7895/shop/get_order"}})
```

환경 변수 `AGENTKIT_TOOL_OVERRIDES=<yaml 파일>`도 같은 내용을 받는다(`도구 이름: {kind, target, options}`). 에이전트 정의서는 바뀌지 않는다.

- `python`: `"패키지.모듈:함수"`, `await fn(arguments, ctx)`. 돌려주는 값은 글, `{"ok", "text", "data", "error_type"}`, `ToolResult` 중 하나. 실패는 `ToolFailure(error_type, text)`, 닿지 못함은 `AdapterUnavailable`. 미리보기는 `options.preview: "모듈:함수"`.
- `dummy`: `"shop:<기능>"`.
- `http`: POST `{"arguments", "context"}` → `{"ok", "text", "data", "error_type", "executed"?}`. JSON 없이 4xx면 상태 코드로 유형을 정한다(404 `not_found`, 401·403 `permission_denied`, 400·422 `invalid_input`).
- `replay`: JSONL, 줄마다 `{"tool"?, "phase"?: "call"|"preview", "arguments", "result"}`. 인자가 같은 기록을 순서대로 내고 마지막 것을 되풀이한다. `options.match_fields`로 비교할 인자를 줄일 수 있다. 기록은 `agentkit.tools.record_call(path, tool, arguments, result, phase)`로 만든다. 경로는 절대 경로이거나 `tools/` 또는 `v2/` 기준.

## v1 조회 도구

- 6개는 `realtime_tools.Toolbox`의 메서드를 직접 부른다(`Toolbox.run`을 거치지 않는다). `run`은 실패를 글로만 알려 유형을 가릴 수 없어서다. 그 대신 `ToolError`의 문장으로 유형을 가른다: "조회 실패/요청을 받지 않습니다/가져오지 못했습니다/응답하지 않습니다" → `system_error`, "다시 호출하세요/알 수 없습니다/검색어가 없습니다/까지만 조회할 수" → `invalid_input`, 나머지 → `not_found`. v1의 문장이 바뀌면 이 분류가 어긋난다.
- v1의 "한 번 더 시도"는 게이트웨이의 `retries: 1`이 맡는다. 제한 시간은 v1과 같은 4초(전체).
- `Toolbox`는 이벤트 루프마다 하나를 만들어 나눠 쓴다(긱뉴스 목록 캐시, 지명 캐시). 기본 지역은 환경 변수 `AGENTKIT_DEFAULT_LOCATION`(기본 서울).
- `list_knowledge_by_date`는 v1 `KnowledgeClient.list_by_date`를 옮겨 적었다(문장, 쪽 넘김, 소수 초 처리 같음). 지식베이스 id는 환경 변수 `AGENTKIT_WEKNORA_KB`, 없으면 `29f24b56-…206c`.
- v1의 `TOOL_GUIDE`와 지식 목록 안내문(`KnowledgeClient.guide`)은 옮기지 않았다. 지시문은 정의서의 몫이다.

## 못 끝낸 것, 남은 문제

- **`list_knowledge_by_date`가 에이전트의 `knowledge.kb_id`를 모른다.** `ToolContext`에 정의서 정보가 없어서 지식베이스가 프로세스 전체에 하나다(환경 변수). 에이전트마다 다른 지식베이스를 쓰려면 `ToolContext`에 항목이 필요하다.
- **선제 응답과 변경 도구.** 선제 호출은 제안도 남기지 않으므로, 선제로 만든 답이 그대로 확정되면 그 답은 "실행하지 않았다"는 결과를 바탕으로 한 것이다. 엔진이 확정 뒤에 변경 도구를 다시 불러야 한다(WP4에 알렸다).
- **게이트웨이 상태는 프로세스 안에만 있다.** 사실, 제안, 실행 결과가 메모리에 있어 서버를 다시 띄우면 사라진다. 실제 시스템을 붙일 때는 중복 방지 키를 시스템에도 넘겨야 한다(지금 `http` 본문에는 `arguments`와 `context`만 간다).
- **`end_session`을 부르지 않은 세션의 상태는 남는다.** 기한으로 지우는 장치가 없다.
- **`http` 연결은 호출마다 새 연결을 연다.** 지연이 문제 되면 클라이언트를 나눠 써야 한다.
- **`output_schema`는 적기만 하고 검사하지 않는다.**
- **언어.** 결과 문장은 모두 한국어다. `ctx.language="en"`이어도 같다(위키백과 판만 바뀐다).
- `shop_server`에는 인증이 없다. 127.0.0.1에만 묶는다.

## 다른 모듈에 바라는 것

- **WP4**: 변경 도구는 턴이 확정된 뒤(`speculative=False`)에 불러야 한다. `needs_confirmation` 뒤 같은 턴에 다시 부르지 않는 것(이미 그렇게 한다고 메모에 있다). 엔진이 닫힐 때 `gateway.end_session()`.
- **WP3**: `recorder.span(span, pii_paths=[...])`의 경로는 `input.arguments.<인자>`, `output.data.<경로>`다. `output.data`가 `None`이거나 경로가 없을 수 있다(실패 결과). 주문번호(`A1001`)와 접수번호(`R1001`)는 계약서에 개인정보로 표시하지 않았으므로 글 패턴으로 가려야 한다. `output.text`에는 운송장 번호(12자리 숫자)도 나온다.
- **WP1**: `shop-support`의 흐름 규칙에 "도구가 상담원 연결을 안내하면 `handoff_to_human`을 호출한다"가 있으면 10만 원 초과 거절 문장과 맞는다.
- **WP7**: `/api/tools`는 `{"tools": [...], "errors": [...]}`.
- **총괄**: `ToolContext`에 에이전트의 `knowledge.kb_id`(또는 도구별 설정)를 넘길 자리. `ToolErrorType`에 업무 규정 거절을 뜻하는 유형이 없어 `precondition_failed`를 함께 쓴다.

## 화면 표시와 더미 카페 (2026-11-17 추가, `docs/DISPLAY.md`의 작업 A)

### 만든 것

- `gateway.py`: `ToolResult.display`를 채운다. `loader.py`: 계약서의 `display` 검사(`check_refresh`). `validate.py`: 인자 종류 `object`. `adapters.py`: 더미 시스템 `cafe`, `Reply.repeatable`. `api.py`: `GET /api/dummy/cafe/records`.
- `agentkit/dummy/cafe.py`: 더미 카페(`get_cafe()`, `CafeSystem`).
- `tools/`: `get_menu`, `search_menu`, `get_cart`, `add_to_cart`, `remove_from_cart`, `apply_coupon`, `request_payment`. `handoff_to_human`은 그대로 쓴다(쇼핑몰 더미가 세션을 처음 보면 만들어 주므로 카페만 쓴 세션에서도 된다. `order_id`는 `null`).
- 시험: `tests/test_cafe_system.py`(카페만), `tests/test_cafe_gateway.py`(계약서와 게이트웨이를 거쳐), `test_tools_gateway.py`의 "screens" 묶음, `test_tools_contracts.py`의 `display` 검사.

### 게이트웨이가 화면을 만드는 규칙

| 경우 | `display` |
|---|---|
| 성공, 계약서에 `display` 있음 | `{"kind": 계약서의 kind, "data": data 또는 path의 값, "source": 도구 이름}`. `path`의 값이 없으면 항목을 만들지 않는다(오류 아님) |
| 성공, `refresh` 있음 | 위 항목 뒤에, `refresh`의 조회 도구를 `{}`로 불러 얻은 화면들. `source`는 그 조회 도구 이름 |
| 변경 도구의 제안 | `{"kind": "confirm", "data": {미리보기 값..., "text": 확인 문장, "tool": 도구 이름}}`. 계약서에 `display`가 없어도 붙는다(`request_refund`도 확인 카드가 나온다). `refresh`는 돌지 않는다 |
| 실패 | 없음. `data.display_notice`(문장 또는 `{"level", "text"}`)가 있으면 `{"kind": "notice", "data": {"level": "info", "text"}}` |
| 이미 처리된 요청의 재호출(`executed=False`) | 성공과 같다(처음 결과의 화면 + 지금 상태로 `refresh`) |

정해 둔 것:

- **`refresh`로 얻은 화면과 종류가 같은 자기 화면은 뺀다.** `add_to_cart`의 계약서는 `display: {kind: cart, path: cart, refresh: [get_cart]}`이고 결과는 `get_cart`의 `cart` 하나다. `refresh`가 실패하면 자기 결과의 `cart`가 대신 나온다. `DisplaySpec.kind`가 필수라서 "화면 없이 refresh만"은 적을 수 없다.
- **`refresh` 호출은 한 단계만.** `refresh`로 불린 조회 도구의 `refresh`는 따라가지 않는다.
- **`refresh` 호출도 구간이다.** `Span(kind="tool")`, `attrs.refresh_of`(부른 도구 이름), `attrs.parent_call_id`. 부른 도구의 구간보다 먼저 기록된다(`t_start`는 뒤). 모델이 부른 호출만 세려면 `refresh_of`가 없는 구간만 센다.
- **도구 구간의 `output.display`에는 `[{"kind", "source"}]`만 넣는다.** 내용은 `output.data`와 엔진의 `display` 구간에 있다. 같은 내용을 두 번 저장하지 않고, 가려지지 않은 사본이 남지 않게 하려는 것이다. 호출한 쪽이 받는 `ToolResult.display`에는 `data`가 다 들어 있다.
- `ToolResult.ms`는 자기 호출만 잰다(`refresh` 시간 제외).
- 화면 내용은 `data`의 깊은 복사다. 화면을 만들다 오류가 나도 호출 결과는 그대로 나간다(`display`만 빈다).
- 계약서 검사: `display.kind`는 `DisplayKind`, `display.refresh`의 이름은 등록된 도구이고 `effect: read`, `display`가 있고, `required` 인자가 없어야 한다. 자기 자신은 안 된다. `load_contracts`가 모든 파일을 읽은 뒤 검사하고, `Gateway(...)`를 직접 만들 때도 검사한다.

### 같은 인자로 다시 부르는 변경 도구 (`repeatable`)

게이트웨이는 실행된 변경 요청을 같은 인자로 다시 부르면 실행하지 않는다(R-TOOL-5). 장바구니에는 맞지 않는다("아메리카노 하나 더", 결제 뒤 두 번째 주문). 계약서 타입은 고정이라 시스템이 답에 `"repeatable": true`를 넣는 방식으로 했다(`Reply.repeatable`, `http`·`replay` 응답에도 쓸 수 있다). 그 답은 "처음 결과"로 기억하지 않는다. 카페의 변경 기능 4개가 모두 이렇게 답한다. 실행 결과를 모르는 경우(시간 초과)의 차단은 그대로다: 그 세션에서 같은 인자의 호출은 다시 보내지 않는다.

### 인자 종류 `object`

`add_to_cart`의 `options` 때문에 `validate.py`가 `type: object` 속성을 받는다. 묶음이면 그대로 넘기고, JSON 글이면 풀어서 넘긴다. 안쪽 속성(`properties`, `enum`)은 모델이 읽으라고 적는 것이고 검사하지 않는다(카페가 한국어 값도 받아야 해서). 묶음이 아닌 값(`"아이스"`, 목록)은 게이트웨이에서 `invalid_input`이다. 카페를 직접 부르면 글과 목록도 받는다.

### 더미 카페

메뉴 21개(품절 2개). 가격은 기본 크기 기준.

| 종류 | id | 이름 | 가격 | 옵션 |
|---|---|---|---|---|
| 커피 | `americano` | 아메리카노 | 4500 | 온도(필수), 크기, 샷 |
| | `decaf_americano` | 디카페인 아메리카노 | 5000 | 온도(필수), 크기 |
| | `cafe_latte` | 카페 라떼 | 5000 | 온도(필수), 크기, 샷 |
| | `vanilla_latte` | 바닐라 라떼 | 5500 | 온도(필수), 크기, 샷 |
| | `caramel_macchiato` | 카라멜 마키아토 | 5800 | 온도(필수), 크기, 샷 |
| | `cold_brew` | 콜드브루 | 5000 | 크기, 샷 (차갑게만) |
| 논커피 | `choco_latte` | 초코 라떼 | 5500 | 온도(필수), 크기 |
| | `matcha_latte` | 말차 라떼 | 5800 | 온도(필수), 크기 |
| | `strawberry_latte` | 딸기 라떼 | 6000 | 크기 (차갑게만) |
| | `lemon_ade` | 레몬 에이드 | 5500 | 크기 (차갑게만) |
| | `mango_smoothie` | 망고 스무디 | 6500 | 크기 (차갑게만). **품절** |
| 차 | `earl_grey` | 얼그레이 | 4800 | 온도(필수) |
| | `chamomile` | 캐모마일 | 4800 | 온도(필수) |
| | `peppermint` | 페퍼민트 | 4800 | 온도(필수) |
| | `yuzu_tea` | 유자차 | 5300 | 온도(필수) |
| | `peach_iced_tea` | 복숭아 아이스티 | 4800 | 크기 (차갑게만) |
| 디저트 | `cheesecake` | 치즈 케이크 | 6500 | 없음 |
| | `choco_cake` | 초코 케이크 | 6800 | 없음. **품절** |
| | `croissant` | 크루아상 | 4200 | 없음 |
| | `choco_cookie` | 초코칩 쿠키 | 3000 | 없음 |
| | `macaron` | 마카롱 | 3500 | 없음 |

- 옵션: `temperature`(온도, 필수: `hot` 따뜻하게 / `ice` 차갑게), `size`(크기: `regular` 기본 / `large` 크게 +500원), `shot`(샷 추가: `none` / `one` +500원 / `two` +1000원). 필수가 아닌 옵션은 주지 않으면 기본값이다. 화면의 줄 `options`에는 필수 옵션과 기본값이 아닌 선택만 나온다(`["차갑게", "크게"]`).
- 화면 내용에 명세보다 더 들어 있는 것: 메뉴 항목의 `category`, `tags`, 옵션의 `default`; 주문 줄의 `selected`(`{옵션 id: 선택 id}`); 주문 내역의 `coupon_name`, `count`(수량 합); 확인 카드의 `subtotal`, `discount`, `coupon`, `summary`.
- `coupon`은 쿠폰 코드 글자(`"WELCOME10"`) 또는 `null`.
- 쿠폰: `WELCOME10`(합계의 10%), `TEA1000`(차 메뉴가 담겨 있을 때 1000원. 차가 없으면 `precondition_failed`). 한 장만 쓸 수 있고 다른 쿠폰을 적용하면 바뀐다. 할인은 주문 내역이 바뀔 때마다 다시 계산한다(차를 빼면 `TEA1000` 할인은 0원이 되고 쿠폰은 남는다).
- 같은 메뉴·같은 옵션을 다시 담으면 같은 줄의 수량이 는다. 줄 번호는 `L1`부터, 세션별. 한 줄에 20개까지.
- 너그럽게 받는 입력: `item_id`에 한국어 이름·다른 이름("라떼", "아아", "아이스 아메리카노"는 `americano` + 차갑게), 옵션의 이름과 값을 한국어로("온도": "뜨겁게", "사이즈": "큰 거", "샷": 2, `{"아이스": true}`), 수량을 글로("2", "두 잔"), `line_id`를 숫자나 메뉴 이름으로("1", "치즈 케이크"). 그 메뉴에 없는 옵션이라도 원래 그런 값이면 넘어간다(콜드브루에 `ice`, 케이크에 `size: regular`).
- 실패 유형: 필수 옵션 누락·알 수 없는 옵션·수량 오류·여러 메뉴에 걸리는 이름은 `invalid_input`(가능한 선택을 문장에 적는다), 없는 메뉴·없는 줄·없는 쿠폰·검색 결과 없음은 `not_found`, 품절·빈 주문 결제·쿠폰 조건·확인 뒤 바뀐 주문은 `precondition_failed`(`data.reason`: `sold_out`, `empty_cart`, `no_tea`, `cart_changed`). 품절과 빈 주문 결제는 `display_notice`도 준다.
- 결제: 미리보기가 줄, 합계, `summary`("아이스 아메리카노 2잔, 합계 9000원". 줄이 4개를 넘으면 "… 외 n건")를 준다. 확인 문장은 `{summary} 결제를 진행할까요?`. 실행하면 주문 번호(`K-0001`부터, 프로세스 전체에서 이어짐)를 내고 주문 내역과 쿠폰을 비운다. **미리보기 뒤에 주문 내역이 바뀌었으면 실행을 거절한다**(`cart_changed`): 고객이 들은 내역과 다른 것을 결제하지 않는다. 다시 부르면 새 내역으로 제안부터 한다.
- 결제된 주문은 세션이 끝나도 남는다: `get_cafe().records(session_id=None)`, `GET /api/dummy/cafe/records?session_id=`. 시험용: `get_cafe().reset()`, `.set_sold_out(item_id, bool)`, `.cart(session_id)`.
- 지연 주입은 `adapter.options.delay_s`만 있다. 장애 주입은 만들지 않았다.

### 명세와 다르게 만든 것, 남은 문제

- **`request_payment`에 `refresh: [get_cart]`를 넣었다.** 명세의 표에는 `receipt`만 있다. 결제하면 주문 내역이 비워지는데 화면의 주문 내역 영역이 그대로 남지 않게 하려는 것이다. 실행 결과의 `display`는 `[receipt, cart(빈 것)]`이다. 빼려면 계약서의 `refresh`만 지우면 된다.
- 없는 메뉴는 `invalid_input`이 아니라 `not_found`다(모델의 입력 오류가 아니라 고객이 없는 것을 찾은 경우).
- 동의가 분명하지 않아 다시 묻는 경우(`not_confirmed` + `confirmation_text`)는 실패이므로 `confirm` 화면을 다시 붙이지 않는다. 화면이 `not_confirmed`에서 확인 카드를 닫으면, 에이전트가 다시 묻는 동안 카드가 없다.
- 쿠폰을 화면에서 넣는 길은 없다(말로만).
- 카페 문장은 한국어뿐이다.
