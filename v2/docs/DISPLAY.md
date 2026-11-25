# 화면 표시, 여는 말, 터치 입력 (v2 추가 설계, 2026-11-17)

## 왜 필요한가

키오스크 주문 도우미(`kiosk-order`)를 써 본 사람이 당황했다. 메뉴가 보이지 않았기 때문이다. 원인은 둘이다.

1. 그 에이전트는 뼈대였다. 도구도 메뉴 데이터도 없다.
2. 구조에 화면으로 무언가를 보여 줄 통로가 없다. 에이전트가 전할 수 있는 것은 말과 글뿐이다.

메뉴는 듣고 고르는 것이 아니라 보고 고르는 것이다. 이 문서는 2번을 푸는 설계이고, 키오스크를 두 번째 대표 시나리오로 끝까지 만든다.

## 원칙

- **화면은 시스템의 값으로 그린다.** 가격, 주문 내역, 합계는 모델의 말이 아니라 도구 결과에서 온다. 에이전트가 말로 틀려도 화면은 맞다.
- **화면 표시는 모델을 거치지 않는다.** 도구 계약서가 "이 결과를 어떤 화면으로 보여 줄지"를 적고, 게이트웨이가 화면 내용을 만들고, 엔진이 그대로 전달한다.
- **선택 항목이다.** 여는 말, 화면, 터치를 쓰지 않는 에이전트(`shop-support`, `tech-trend-anchor`)는 지금과 똑같이 동작한다.

## 사용자 시나리오 (S12. 키오스크에서 주문한다)

1. 고객이 대화를 시작하면 에이전트가 먼저 말한다. "어서 오세요. 화면의 메뉴를 보시고 말씀하시거나 눌러서 고르시면 됩니다." 화면 왼쪽에 메뉴가, 오른쪽에 빈 주문 내역이 보인다.
2. 고객이 "아이스 아메리카노 두 잔 주세요"라고 말한다. 주문 내역에 아이스 아메리카노 2잔과 합계가 나타난다. 에이전트가 "아이스 아메리카노 두 잔 담았어요. 더 필요하신 거 있으세요?"라고 말한다.
3. 고객이 "달달한 거 뭐 있어요?"라고 묻는다. 에이전트가 메뉴를 검색하고, 화면에서 해당 메뉴들이 강조된다. 에이전트는 두세 개만 말로 소개한다.
4. 고객이 화면에서 "바닐라 라떼"를 누른다. 주문 내역에 추가되고 에이전트가 짧게 확인한다.
5. 고객이 "아메리카노 하나는 빼 주세요"라고 한다. 주문 내역이 바뀐다.
6. 고객이 "결제할게요"라고 한다. 화면에 주문 확인 카드(내역과 합계)가 뜨고 에이전트가 읽어 준다. 고객이 "네"라고 하면 결제 요청이 실행되고 주문 번호가 적힌 영수증 화면이 나온다.
7. 품절 메뉴를 주문하면 담기지 않고, 에이전트가 품절임을 알리며 비슷한 메뉴를 권한다. 화면의 품절 메뉴는 흐리게 보인다.
8. 고객이 직원을 찾으면 직원 호출로 넘긴다.

## 타입 (정본은 `agentkit/contracts.py`, 이미 들어가 있다)

- `AgentDefinition.opening: Opening{say, tools[OpeningCall{name, arguments}]}`
- `AgentDefinition.screen: ScreenSettings{panels: ["menu", "cart"]}`
- `AgentDefinition.actions: [ClientAction{name, tool, description, say}]`
- `ToolContract.display: DisplaySpec{kind, path, refresh[]}`
- `ToolResult.display: [{"kind", "data", "source"}]`
- `ReplyEvent.type == "display"` (data: `{kind, data, source}`)
- `ReplyEngine.open()`, `ReplyEngine.act(name, arguments)`
- `ConversationDriver.opening()`, `.act()`, `AgentTurn.displays`
- `SpanKind`: `"display"`, `"action"`

정의서 파일: `opening`, `screen`, `actions`는 `agent.yaml`에 적는다.

## 화면 내용의 모양 (`display[].data`)

```jsonc
// kind "menu": 전체 메뉴. 메뉴 영역을 채운다.
{"categories": [{"id": "coffee", "name": "커피", "items": [
   {"id": "americano", "name": "아메리카노", "price": 4500, "description": "진한 에스프레소에 물을 더한 커피",
    "sold_out": false,
    "options": [{"id": "temperature", "name": "온도", "required": true,
                 "choices": [{"id": "hot", "name": "따뜻하게", "price_delta": 0}, {"id": "ice", "name": "차갑게", "price_delta": 0}]}]}]}]}

// kind "items": 검색·추천 결과. 메뉴 영역에서 이 항목들을 강조한다.
{"query": "달달한", "items": [ /* menu의 item과 같은 모양 */ ]}

// kind "cart": 주문 내역. 주문 내역 영역을 통째로 바꾼다.
{"lines": [{"line_id": "L1", "item_id": "americano", "name": "아메리카노", "options": ["차갑게"],
            "quantity": 2, "unit_price": 4500, "line_total": 9000}],
 "subtotal": 9000, "discount": 0, "total": 9000, "coupon": null}

// kind "confirm": 변경 도구의 확인 단계. 대화 위에 확인 카드로 뜬다.
{"text": "아메리카노 2잔, 합계 9000원 결제를 진행할까요?", "tool": "request_payment",
 "lines": [ /* cart의 lines */ ], "total": 9000}

// kind "receipt": 실행 결과. 영수증 카드.
{"order_no": "K-0012", "lines": [ /* ... */ ], "total": 9000, "message": "주문이 접수되었습니다. 번호가 불리면 받아 가세요."}

// kind "notice": 안내 한 줄.
{"level": "info", "text": "초코 케이크는 품절입니다."}
```

## 모듈별 동작

### 게이트웨이 (tools)

- 도구가 성공하면 계약서의 `display`로 `ToolResult.display`를 채운다. `path`가 있으면 `data`에서 그 경로의 값, 없으면 `data` 전체.
- `refresh`의 조회 도구들을 인자 없이 이어서 호출하고, 각각의 화면을 `display` 목록 뒤에 붙인다(같은 세션·턴 맥락으로, 구간도 기록). 실패하면 건너뛴다.
- 변경 도구의 제안 단계(`needs_confirmation`)에는 `{"kind": "confirm", "data": {"text": confirmation_text, "tool": name, ...미리보기 값}}`을 붙인다.
- 실패한 호출에는 화면을 붙이지 않는다. 단, 연결 방식이 실패와 함께 안내 화면을 주면(`data.display_notice`) `notice`로 붙인다.
- `GET /api/tools`의 각 항목에 `display`가 나온다.

### 더미 카페 (`agentkit/dummy/cafe.py`)

- 메뉴 4종류(커피, 논커피, 차, 디저트) 20개 안팎. 가격, 설명, 옵션(온도, 크기, 샷 추가), 품절 1~2개.
- 세션별 장바구니. 쿠폰 2종(`WELCOME10`: 10% 할인, `TEA1000`: 차 1000원 할인).
- 도구(이름 고정):

| 도구 | 종류 | 화면 | 설명 |
|---|---|---|---|
| `get_menu` | 조회 | `menu` | 전체 메뉴. 인자 없음 |
| `search_menu(query)` | 조회 | `items` | 이름, 설명, 태그(달달한, 카페인 없는, 따뜻한 등)로 찾기 |
| `get_cart` | 조회 | `cart` | 지금 주문 내역. 인자 없음 |
| `add_to_cart(item_id, quantity, options)` | 변경, 확인 없음 | refresh `get_cart` | 품절·필수 옵션 누락·없는 메뉴는 실패 유형과 안내 문장 |
| `remove_from_cart(line_id, quantity?)` | 변경, 확인 없음 | refresh `get_cart` | |
| `apply_coupon(code)` | 변경, 확인 없음 | refresh `get_cart` | |
| `request_payment()` | 변경, **확인 필요** | 제안 때 `confirm`, 실행 때 `receipt` | 빈 장바구니면 거절. 실행하면 주문 번호 발급, 장바구니 비움 |
| `handoff_to_human(reason)` | 기존 도구 재사용 | | 직원 호출 |

`options`는 `{옵션 id: 선택 id}` 객체. 모델이 자주 틀리는 입력(이름으로 준 item_id, 한국어로 준 옵션)은 연결 방식이 너그럽게 받아 맞는 id로 바꾼다. 바꿀 수 없으면 `invalid_input`과 무엇이 가능한지 알려 주는 문장.

장바구니를 바꾸는 도구에 확인 단계를 두지 않는 이유: 세션 안에서만 유효하고 되돌릴 수 있다. 돈이 움직이는 `request_payment`만 두 단계다.

### 엔진 (runtime)

- `open()`: `opening.tools`를 게이트웨이로 호출(턴 0, `speculative=False`). `tool`, `display` 이벤트를 내고, 호출과 결과를 이력에 넣는다(모델이 메뉴의 id와 가격을 알게 된다. 결과가 길면 모델용 글은 도구의 `text`를 쓴다). 이어서 `opening.say`를 `piece`와 `done`으로 낸다. 턴 0으로 기록.
- `reply()`: 도구 결과에 `display`가 있으면 `tool` 이벤트 뒤에 항목마다 `display` 이벤트를 낸다. `Span(kind="display")` 기록.
- `act(name, arguments)`: `definition.actions`에서 찾는다. 대응 도구가 확인이 필요한 변경 도구면 거절. 아니면 새 턴으로 치고 게이트웨이를 호출해 `tool`, `display` 이벤트를 낸 뒤, 터치 내용을 적은 사용자 말("(화면에서 바닐라 라떼를 담았습니다)", `ClientAction.say`를 인자로 채운 것)과 도구 결과를 넣어 모델이 한 번 짧게 반응하게 한다. `Span(kind="action")` 기록.
- 프롬프트: `screen.panels`가 있으면 "고객은 화면으로 메뉴와 주문 내역을 보고 있다. 메뉴 전체나 주문 내역 전체를 말로 읽지 말고, 바뀐 것과 다음에 할 일만 짧게 말하라"를 넣는다.

### 서버와 음성

- `ready`에 `screen: {panels}`, `actions: [{name, tool, description}]`, `opening: bool` 추가.
- 연결 직후 `opening`이 있으면 `engine.open()`을 돌린다. 여는 말은 `chat_delta`/`chat_done`(id `"opening"`)으로, 음성·아바타 채널에서는 말로도 한다.
- 서버 → 클라이언트 `{"type": "display", "kind", "data", "source"}`.
- 클라이언트 → 서버 `{"type": "action", "id", "name", "arguments"}`. 응답은 일반 답과 같은 메시지들(`tool`, `display`, `chat_delta`, `chat_done`).
- 음성 채널에서 `action`이 오면 진행 중인 답을 끊고(끼어들기와 같게) 처리한다.

### 화면 (web/index.html)

- `ready.screen.panels`에 따라 메뉴 영역과 주문 내역 영역을 연다. 없으면 지금과 같은 화면.
- 메뉴: 종류별 묶음, 이름·가격·설명, 품절은 흐리게. `items`가 오면 해당 항목 강조. 항목을 누르면 옵션 고르기(필수 옵션)가 나오고 `add_item` 동작을 보낸다(정의서에 그 동작이 있을 때만 누를 수 있다).
- 주문 내역: 줄마다 수량, 옵션, 금액, 빼기 버튼(`remove_item` 동작), 합계.
- 확인 카드와 영수증 카드: 대화 흐름 안이 아니라 눈에 띄는 자리에. 확인 카드는 고객이 말로 동의하거나 거절하면 사라진다(다음 `tool` 메시지의 `executed` 또는 `not_confirmed`).
- (2026-11-17 보완) 확인 카드에는 “아니요 / 네” 단추가 있다. 단추는 도구를 직접 실행하지 않고 “네, 그렇게 해 주세요.” / “아니요, 하지 않을게요.”를 고객의 말(chat)로 보내므로, 동의 판정은 말로 답한 경우와 같은 경로(게이트웨이의 두 단계 확인)를 탄다. `act`가 확인이 필요한 변경 도구를 거절하는 규칙은 그대로다.
- (2026-11-17 보완) “처음부터” 단추는 위 막대에 늘 보이고, 영수증 카드에도 “처음으로”가 있다. 누르면 지금 세션을 닫고 새 세션을 열어 여는 말부터 다시 시작한다(주문 내역은 버려진다).
- (2026-11-17 보완) 음성 쪽: 에이전트가 답을 기다리는 동안(동의 대기 중이거나 마지막 말이 물음)에는 짧은 “네”를 추임새로 버리지 않는다. 합성 직전에는 “1잔 → 한 잔”처럼 고유어 수로 읽는 단위를 바꾼다(`audio.spoken_form`, 화면과 기록의 글자는 그대로).
- 뼈대 에이전트(도구가 등록되지 않음)는 시작을 막고 이유를 보여 준다.
- 모니터: 턴 상세에서 `display`, `action` 구간을 읽기 쉽게 보여 준다(메뉴는 요약, 주문 내역은 줄 목록).

### 정의서와 평가 (definition, eval)

- 읽기: `agent.yaml`의 `opening`, `screen`, `actions`. 검사: `actions[].tool`이 그 에이전트의 `tools`에 있어야 한다.
- `kiosk-order` 정의서 완성: 위 도구들, 여는 말, 화면, 동작(`add_item` → `add_to_cart`, `remove_item` → `remove_from_cart`), 흐름 규칙, 넘김, 금지, 검사, 성공 기준(주문 정확도는 안전 기준), 추출 항목, 가상 고객 6명 이상, 사례 2개 이상. 음성 설정은 `speculative: false`.
- 분석: 판정 모델에게 턴별 도구 호출과 함께, `cart`·`receipt`·`confirm` 화면의 내용을 요약해 준다. "고객이 말한 주문과 결제된 내역이 일치한다"를 판정할 수 있어야 한다.
- 가상 고객: 여는 화면의 메뉴를 글로 받아 보고 주문한다(`driver.opening()`의 `displays`). 일부 가상 고객은 터치(`driver.act`)를 섞는다.
- 수치 지표 추가: `cart_changes`(담기·빼기 횟수), `payment_executed`(0/1).

## 파일 소유 (이번 작업)

| 작업 | 파일 |
|---|---|
| A. 도구와 더미 카페 | `agentkit/tools/*`, `agentkit/dummy/cafe.py`, `tools/get_menu.yaml` 등 7개, `tests/test_tools_*.py`, `tests/test_cafe_*.py` |
| B. 엔진, 서버, 음성 | `agentkit/runtime/engine.py`, `prompt.py`, `driver.py`, `__init__.py`, `voice.py`, `agentkit/server.py`, `tests/test_runtime_*.py`, `tests/test_voice_*.py`, `tests/runtime_fakes.py`, `tests/voice_fakes.py` |
| C. 화면 | `web/*`, `tests/mock_api.py`, `tests/test_web_*.py`, `tests/screenshots/` |
| D. 정의서와 평가 | `agentkit/definition/*`, `agents/kiosk-order/`, `agentkit/eval/*`, `tests/test_definition_*.py`, `tests/test_eval_*.py`, `tests/eval_fakes.py` |

`agentkit/contracts.py`는 고정이다. 고치지 않는다.

## 덧붙임 (2026-11-25): 말로 시키고 눈으로 확인하기 — 업무 비서 화면 PoC

오른쪽 화면을 "비서가 사이트를 열어 주는 곳"이 아니라 "말로 시킨 일을 눈으로 확인하는 곳"으로 쓴다. 화면이 값어치를 하는 경우는 길거나, 여러 개이거나, 글자 하나까지 정확해야 하거나, 되돌릴 수 없는 일을 확인할 때다. 시나리오 둘을 `office-assistant` v14(초안)와 `/office`에 넣었다.

### 메일 한 통씩 처리하기

- "하나씩 보자" → 화면 목록의 첫 메일이 가운데에 열리고 비서가 요점을 말한다. 이후 "다음", "보관", "나중에", "답장해 줘"만 말한다.
- 도구: `next_mail`(조회, 목록 순서로 아직 열지 않은 다음 메일), `triage_mail`(변경, 확인 없음: 열린 한 통을 보관하거나 안 읽음으로 남기고 다음 메일을 연다). 한 통이고, 사용자가 보고 있고, `undo_cleanup`으로 되돌릴 수 있어 확인 단계를 두지 않았다. 스팸·휴지통·여러 통은 그대로 `cleanup_mail`(확인 필요).
- 화면 내용 `mail`에 `position {name, index, total, left}`, `done {ref, action, label, report_id}`, 목록을 다 보면 `{finished: true}`가 붙는다.
- **말로 하는 단계어** (`ClientAction.phrases`, `arguments`, `while_showing`): 사용자의 말 전체가 단계어("다음", "보관")이고 그 화면(`mail`)이 떠 있으면, 엔진이 모델보다 먼저 터치와 같은 경로로 실행한다. 모델이 도구를 고르는 데 기대지 않고, 모델 생성도 한 번으로 준다(실측 0.7~1.3초). 확인을 기다리는 요청이 있으면 단계어로 읽지 않는다. 확인 카드·알림·보내기 대기·정리 결과(`CARD_KINDS`)는 떠 있는 화면을 바꾸지 않는다.

### 답장 초안을 말로 고치기

- 확인 문장을 들은 뒤 "금요일 말고 목요일로", "끝에 인사도 넣어 줘"라고 하면 본문이 고쳐지고, 오른쪽 카드에 바뀐 곳(추가는 초록, 삭제는 줄 긋기)이 표시된다. 원문 메일은 가운데에 그대로 있다.
- 고친 메일의 확인 문장은 처음부터 다시 읽지 않고 바뀐 부분만 말한다: "김민수에게 보낼 메일에서 바뀐 부분은 '…'입니다. 이대로 보낼까요?"
- **고칠 수 있는 요청** (`ToolContract.revisable`, `send_mail`에 켬): 확인 질문에 동의도 거절도 아닌 답이 오면 엔진이 모델에게 "고친 인자로 다시 부르라"고 알리고, 새 확인이 기다리던 확인을 대신한다. 이전에는 엔진이 "도구를 다시 부르지 말라"고 해서, 비서가 고친 내용을 말로만 읽고 "응"에는 고치기 전 본문이 나갔다(v13까지).
- 받는 사람 없이 본문만 고쳐 부르면 쓰던 메일의 받는 사람과 제목을 잇는다. 같은 인자로 다시 부르면 "똑같다"고 알려 준다.
- 가드 `no_fake_revision`: `send_mail`을 부르지 않고 "고쳤습니다", "넣을게요"라고 말하면 한 번 다시 생성한다.

### 화면 (`web/office.html`)

- 메일은 옆 패널이 아니라 가운데 영역에 열린다. 옆 패널이 비므로 원문과 보낼 메일이 나란히 보인다.
- 읽기 화면 위 막대: 목록에서 몇 번째인지, `나중에`·`보관`·`다음` 단추(말로 하는 것과 같은 동작). 보관하면 되돌리기 단추가 있는 알림이 뜬다.

### 남은 것

- 고치기는 모델이 새 본문으로 다시 부르는 데 달려 있다. 마지막 실행 3회는 모두 맞았지만, 그 앞 실행들에서 한 번은 빈 답이 나와 사용자가 다시 말해야 했다.
- 단계어 목록은 정의서에 적은 말과 정확히 같을 때만 듣는다. 그 밖의 말("이건 됐고 다음 거")은 모델이 처리한다.
- 음성 채널에서는 시험하지 않았다(글 채널로만 확인). 짧은 "다음", "보관"이 추임새로 버려지지 않는지 확인이 필요하다.
- v14는 초안이다. 시험 한 벌(가상 고객 `one_by_one_triage`, `revise_reply` 추가)과 승인·배포가 남았다.
