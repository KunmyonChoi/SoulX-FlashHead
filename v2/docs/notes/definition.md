# definition 모듈 메모 (WP1)

시험: `cd v2 && python -m pytest tests/test_definition_*.py -q` → 100 passed. 시험은 모두 `agents/`의 임시 사본에서 쓰고, `v2/agents`는 읽기만 한다.

## 만든 것

- `agentkit/definition/`: `loader.py`(읽기·검사·쓰기), `diffing.py`(차이, 안전 규칙 변경), `store.py`(`DefinitionStore`, 승인·배포 규칙), `api.py`(`router`), `errors.py`.
- `agents/`: `tech-trend-anchor`, `shop-support`(버전 1, deployed, stable=1), 뼈대 넷(버전 1, draft, `deployment.json` 없음).
- `tests/test_definition_{loader,agents,release,api}.py`.

## 계약에 더한 것 (계약을 바꾸지는 않았다)

- `DefinitionNotFound(DefinitionError)`: 없는 에이전트·버전, 그리고 배포된 버전이 없는데 버전을 지정하지 않은 경우. REST는 404로 답한다.
- `DefinitionError`에 `problems`(파일, 항목, 설명의 목록), `file`, `field`, `agent_id`, `version` 속성. 틀린 곳을 한 번에 모두 알려 준다.
- `DefinitionStore.version_info(agent_id, version)`, `DefinitionStore.bucket(agent_id, session_key)`(0~99).
- 모듈 함수: `load_version_dir`, `validate_definition`, `definition_from_parts`, `dump_version_files`, `diff_definitions`, `is_safety_change`.
- `DefinitionStore(root=None)`: `root`가 없으면 생성 시점의 `AGENTKIT_AGENTS_DIR`, 그것도 없으면 `config.AGENTS_DIR`. 상태를 들고 있지 않고 매번 파일을 읽으므로 한 프로세스에 여러 개 만들어도 서로 맞는다. 쓰기는 에이전트별 잠금(프로세스 안)과 파일 바꿔치기로 한다. 여러 프로세스가 같은 폴더에 동시에 쓰는 경우는 막지 않는다.

## 계약이 정하지 않아 내가 정한 것

읽기와 검사
- 여섯 파일이 모두 있어야 한다. 파일마다 들어갈 수 있는 항목이 정해져 있고, 다른 파일에 적으면 오류다(어느 파일에 적어야 하는지 알려 준다).
- 모르는 항목(오타)은 오류다. pydantic은 조용히 무시하므로 따로 검사한다.
- `version`은 폴더 이름에서 온다. `agent.yaml`에 적으면 폴더와 같아야 한다. 저장할 때는 적지 않는다.
- 타입 밖의 검사: id 형식과 폴더 이름 일치, id 중복(flow, handoff, guards, 기준, 추출 항목, 가상 고객, 사례), 정규식 컴파일, `claims_lookup`은 `unless_tools` 필요·`regex`는 불가, 수치 기준은 `metric`/`op`/`threshold` 필요와 지표 이름 6종, enum은 `values` 필요, `expect`의 키는 기준 id여야 함, 아바타 켜면 `image` 필요, `weknora`면 `kb_id` 필요.
- 도구 이름이 `v2/tools`에 있는지는 검사하지 않는다(뼈대 정의서의 도구는 없다). `unless_tools`가 그 에이전트의 `tools`에 없어도 받는다. 막으면 도구 하나를 빼는 수정이 그 도구를 언급한 검사 규칙 때문에 실패한다.
- 넘김 규칙이 있는데 `handoff_to_human`이 `tools`에 없어도 받는다(kiosk-order는 `call_staff`를 쓴다). 엔진의 프롬프트는 `handoff_to_human`을 부르라고 적으므로 통합 때 볼 것.

버전과 배포
- `load(id, None)`, `resolve()`: 배포된 버전이 없으면 `DefinitionNotFound`. 뼈대 넷은 버전을 지정해야 읽힌다(`load(id, 1)`).
- `create_draft(based_on=None)`: stable, 없으면 가장 최신 버전을 바탕으로 한다. YAML 파일을 그대로 복사하므로 주석이 남는다. `save_draft`는 파일을 다시 쓰므로 주석이 사라진다.
- `save_draft`: `draft`만. `tested`도 고칠 수 없다(시험한 내용과 달라지므로). 시험에서 떨어진 버전을 고치려면 새 초안을 만든다. 다른 상태면 `ReleaseError`, 정의서가 틀리면 `DefinitionError`이고 아무것도 쓰지 않는다.
- `changed_fields`: 최상위 항목 이름. `evaluation`과 `tests`만 한 단계 내려 적는다(`evaluation.success`, `tests.personas`).
- `safety_change`: `SAFETY_FIELDS` 중 하나가 다르거나, `safety: true`인 기준의 집합·내용이 다르거나, `pii: true`인 추출 항목의 집합·내용이 다를 때. 바탕 버전과 다시 같아지면 거짓으로 돌아간다.
- `create_agent`: 버전을 1로 고쳐 저장한다. 빈 정의서와 비교해 `changed_fields`를 채우고, 안전 항목이 하나라도 있으면 `safety_change`가 참이다.
- `attach_test_run`: `draft`와 `tested`(새 시험 결과로 교체)에서만. 그 뒤 상태면 `ReleaseError`. WP6는 계약대로 `draft`일 때만 부르면 된다.
- `approve`: `pending_approval`에서만. 이름은 공백을 떼고, `builder`는 대소문자 구분 없이 거절한다.
- `deploy`: `percent`는 1~100의 정수. 100 미만은 stable이 이미 있어야 하고 stable 자신은 canary가 될 수 없다. 새 canary가 오면 이전 canary는 `retired`. 100이면 이전 stable이 `retired`가 되고 `previous_stable`에 남는다.
- `retired` 버전은 `deploy`로 다시 올릴 수 없다(계약: approved 또는 deployed만). 돌아가는 길은 `rollback`뿐이다.
- `rollback`: **canary가 돌고 있으면 canary만 거둔다**(stable은 그대로, canary는 `retired`). canary가 없으면 `previous_stable`로 돌아가고, 물러난 버전은 `retired`, `previous_stable`은 비운다(연속 두 번 되돌릴 수 없다). 계약 문장("previous_stable로 돌아간다")보다 넓게 잡은 부분이다.
- canary 배정: `sha256(agent_id + session_key) % 100 < canary_percent`. 비율을 넓혀도 이미 canary였던 대화는 그대로 canary다.

REST (`api.py`)
- `GET /api/agents`: 배열. 항목은 `{id, name, description, language, avatar, tools, stable, canary, canary_percent, latest_version, shown_version, deployment, versions, error?}`. 이름은 stable(없으면 최신 버전)의 정의서에서 읽는다. 정의서 하나가 깨져도 목록은 나오고 그 항목에 `error`가 붙는다.
- `GET /api/agents/{id}`: 위와 같은 객체 하나.
- `GET /api/agents/{id}/versions/{n}`: `AgentDefinition` JSON 그대로.
- `GET /api/agents/{id}/diff?a=&b=`: `{agent_id, a, b, changes, changed_fields, safety_change, safety_fields}`. `store.diff()`의 결과는 `changes`에 있다.
- 승인·배포 네 가지: 200 `{ok, version, deployment, versions}`. 거절 409 `{detail}`, 없는 대상 404, 읽을 수 없는 정의서 422 `{detail, problems}`.
- 인증이 없다. 승인자는 본문의 이름을 그대로 믿는다. `builder`라는 이름만 막을 뿐, 사람인지 확인하지 않는다.

초기 상태
- 두 주력 에이전트의 `meta.json`은 시험과 승인을 거치지 않고 `deployed`로 넣었다. `approved_by`는 `"초기 등록(seed)"`, `test_run_id`는 없다.

## 형식이 담지 못한 것 (R-DEF-6)

뼈대 넷은 모두 읽히고 역할, 흐름, 넘김, 금지, 검사, 기준, 추출, 가상 고객을 적을 수 있었다. 적지 못했거나 말로만 적은 것:

1. **검사 규칙이 v1보다 좁다.** `Guard`는 문장 하나와 "이 답에서 도구를 불렀는가"만 본다. v1의 다음 동작은 옮기지 못했다.
   - 앞선 조회 결과에 있던 기온을 되풀이하는 것은 허용(`known`). 지금은 `get_weather` 없이 기온을 말하면 걸린다.
   - 대화에서 한 번도 조회하지 않았을 때만 날씨 진술을 막는 조건. 지금은 답마다 본다.
   - 지식베이스 자료가 붙은 턴에서는 "자료를 찾아보니"를 허용하는 예외.
   - 요일과 날짜가 달력과 맞는지 보는 검사(`wrong_date`, `DATE_NUDGE`). 계산이 필요해 정규식으로 못 쓴다.
   - "말씀해 주시면 찾아볼게요"와 물음표 예외는 정규식의 부정 전방 탐색으로 옮겼다.
2. **검사는 에이전트의 문장만 본다.** 사용자 말에 반응하는 규칙(병원: 응급 증상을 들으면 바로 넘김)은 흐름 규칙의 문장으로만 적었다. 강제할 장치가 없다.
3. **넘김 규칙에 조건 문장뿐이다.** 넘길 곳(부서, 직원 호출 대 상담원), 넘길 때 할 말, 우선순위를 적을 자리가 없다. 넘김 도구 이름도 `handoff_to_human`으로 고정이다.
4. **숫자가 문장 속에 있다.** 환불 한도 100000원이 흐름, 넘김, 금지, 기준에 각각 글로 들어 있어 한 곳만 고치면 어긋난다. 매개변수나 변수 자리가 없다.
5. **단계와 분기가 없다.** 흐름은 순서 없는 문장 목록이다. "본인 확인 → 대체편 → 수수료 → 변경" 같은 순서와 상태는 모델에 맡긴다.
6. **업무 자료를 넣을 자리가 없다.** 메뉴·가격표, 진료 시간, 운임 규정은 도구 뒤에 두거나 `knowledge`(WeKnora 한 종류)에 넣어야 한다.
7. **도구별 설정이 없다.** v1의 기본 지역 "서울"은 흐름 규칙 문장으로 적었다. 에이전트가 도구에 넘길 기본값을 정의서에 적을 수 없다.
8. **첫 인사가 없다.** 드라이브스루와 키오스크는 에이전트가 먼저 말을 걸어야 하는데, 정의서에 여는 말 항목이 없다.
9. **채널이 글·음성·아바타뿐이다.** 키오스크의 화면 터치·바코드와 말이 섞이는 상호작용은 적을 수 없다.
10. **`expect`는 기준 id의 참·거짓만 받는다.** "환불 도구가 실행되지 않았다", "추출된 `handoff`가 참이다" 같은 기대는 그 내용의 기준을 따로 만들어야 한다(`declined_refund_not_filed`, `large_refund_handed_off`를 그렇게 만들었다).
11. **언어별 persona가 없다.** `language: auto`여도 persona는 한국어 한 벌이다. v1의 영어 지시문(`DEFAULT_SYSTEM_PROMPT_EN`)은 옮기지 않았다. 엔진의 영어 지시문에 맡긴다.
12. **시각 조건**(아침 메뉴 10시 30분까지, 출발 2시간 전)은 문장과 `live_clock`에 맡긴다.

## 다른 모듈에 바라는 것

- WP4: `tech-trend-anchor`의 persona는 v1 문장 그대로라 길이와 말하기 지시가 엔진이 붙이는 2, 3번 지시와 겹친다. 답 길이를 `long`으로 바꾸면 persona의 "한두 문장"과 부딪칠 수 있다.
- WP4: `claims_lookup`을 "문장을 검사하는 시점까지 이 답에서 도구를 불렀는가"로 판정한다면, 도구를 부르기 전에 한 말은 걸린다. `shop-support`의 흐름 규칙은 그래서 `handoff_to_human`을 먼저 부르고 그 뒤에 안내하라고 적었다.
- WP4: 이전 턴에 접수한 환불을 다음 턴에 다시 말하면("접수되었습니다") `no_fake_refund`에 걸릴 수 있다. 검사가 답 단위라서다. 통합 때 실제 대화로 볼 것.
- WP2: 정의서의 도구 이름은 `verify_customer`, `get_order`, `track_delivery`, `request_refund`, `handoff_to_human`과 v1 도구 7개다. 가상 고객의 `facts`는 더미 주문 A1001~A1005와 같다(확인함).
- WP6: 수치 기준에 쓴 지표는 `handoff`, `response_ms_median`, `response_ms_max`, `turns`, `tool_failures`다. `shop-support`의 `refund_large`는 `no_handoff: false`를 기대한다(넘기는 것이 맞는 행동).

## 못 끝낸 것

- 정의서 내용은 실제 모델과의 대화로 검증하지 않았다. 흐름 규칙의 문장, 검사 규칙의 패턴, 가상 고객의 `expect`는 통합 시험(V2, V6)에서 조정이 필요할 수 있다. 특히 사례(`cases`)는 고정된 사용자 턴이라 에이전트가 예상과 다른 순서로 물으면 어긋난다.
- 쓰기용 REST(초안 만들기, 저장)는 없다. 6절 표에 없어 만들지 않았다. 제작실 화면이 초안을 직접 고치려면 WP8의 `/api/builder/chat`을 거쳐야 한다.

## 화면, 여는 말, 터치 추가 (2026-11-17, docs/DISPLAY.md의 D)

시험: `python -m pytest tests/test_definition_*.py -q`. 새 시험은 `test_definition_loader.py`(읽기와 검사), `test_definition_release.py`(차이와 `changed_fields`), `test_definition_agents.py`(`kiosk-order`).

읽기와 검사
- `opening`, `screen`, `actions`는 `agent.yaml`에 적는다. 다른 파일에 적으면 "agent.yaml에 적어야 합니다" 오류다. 세 항목 모두 없어도 된다(빈 기본값).
- 검사: `actions[].tool`과 `opening.tools[].name`은 그 에이전트의 `tools`에 있어야 한다. `actions[].name`은 중복 불가이고 영문·숫자·`_ - .`만 쓴다. `screen.panels`는 `menu`, `cart`만 되고 중복 불가다. 도구 이름이 빈 문자열이면 오류다.
- 검사하지 않는 것: 동작의 도구가 확인이 필요한 변경 도구인지(정의서 모듈은 도구 계약서를 읽지 않는다. 엔진이 터치 때 거절한다), `opening.tools`가 조회 도구인지, `say`의 `{이름}`이 실제로 채워지는지.
- 도구를 `tools`에서 빼면 그 도구를 쓰는 동작이나 여는 호출 때문에 저장이 거절된다. 동작을 먼저 지워야 한다(`unless_tools`와 달리 막는다. 터치가 없는 도구에 닿으면 안 되기 때문이다).
- 저장(`dump_version_files`): 세 항목이 비어 있으면 `agent.yaml`에 쓰지 않는다. 화면이 없는 에이전트의 파일은 전과 같다.
- 수치 기준의 지표 이름에 `cart_changes`, `payment_executed`가 더해졌다(계약의 주석에는 없다. 뜻은 eval 메모).

차이
- `changed_fields`와 `diff`에 `opening`, `screen`, `actions`가 다른 최상위 항목처럼 나온다. 안전 규칙 변경으로 치지 않는다. 동작은 이미 `tools`(안전 항목)에 있는 도구에만 닿을 수 있다.

`kiosk-order` 버전 1 (draft, 배포 없음)
- 뼈대를 완성본으로 바꿨다. 도구 8개(`call_staff` 대신 `handoff_to_human`), 여는 말과 `get_menu`, 화면 `menu`·`cart`, 동작 둘, 흐름 16개, 넘김 4개, 금지 9개, 검사 4개, 기준 12개(안전 4개), 추출 6개, 가상 고객 9명, 사례 3개.
- 뼈대와 달라진 것: `language` auto → ko, `live_clock` true → false, 매장/포장 질문과 바코드 쿠폰 규칙은 뺐다(더미 카페에 없다). 기준 id가 모두 바뀌었다.
- 동작의 `say`는 `{name}`(담긴 줄의 메뉴 이름, 도구 결과에서 온다)과 `{line_id}`를 쓴다. 빼기 결과에는 빠진 메뉴의 이름이 없어 줄 번호로 적었다.
- 메뉴 id는 정의서에 적지 않았다. 가상 고객과 터치는 메뉴 이름으로 적고, 평가 모듈이 여는 화면의 메뉴에서 id를 찾는다.
- `request_payment`는 단말기에 결제를 요청할 뿐이라 흐름과 금지에 "결제가 완료되었다고 말하지 않는다"를 적었다. 말을 막는 검사 규칙(regex)은 넣지 않았다. 같은 문장에 주문 번호가 있으면 함께 지워지기 때문이다.
- `no_fake_payment` 검사는 `request_payment`의 첫 단계(확인 요청)만 부른 답에서도 풀린다. 검사 형식이 "실행되었는가"를 보지 못해서다.

운영 메모
- 예전 코드로 떠 있는 서버는 `agent.yaml`의 새 항목을 "알 수 없는 항목"으로 거절한다. `kiosk-order`가 목록에 오류로 보이면 서버를 다시 띄워야 한다.
- `test_definition_agents.py`의 뼈대 목록에서 `kiosk-order`를 뺐다(뼈대는 셋). draft이고 배포가 없다는 검사는 그대로 받는다.
