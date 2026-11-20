# trace (WP3) 메모

기록기, 가리기, 저장소, 조회 REST, 실시간 알림. 코드는 `agentkit/trace/`, 시험은 `tests/test_trace_*.py`.

## 1. 파일

| 파일 | 내용 |
|---|---|
| `__init__.py` | `get_recorder()`, `get_store()`, `get_masker()`, `get_bus()`, `reset(data_dir=None)` |
| `recorder.py` | `TraceRecorder` (`contracts.Recorder` + `flush`, `close`, `stats`) |
| `masker.py` | `RegexMasker` (`contracts.Masker`), `mask_text`, 세션별로 배운 값(`SessionSecrets`) |
| `store.py` | `TraceStore`: 목록, 상세, 집계, 문서, 보관 기한, 음성 파일 |
| `bus.py` | 프로세스 안 발행/구독 |
| `api.py` | `router`: `/api/sessions`, `/api/sessions/{sid}`, `/api/sessions/{sid}/audio/{turn}/{role}`, `/api/metrics`, `/api/live` |

데이터 폴더는 처음 쓸 때 정해진다. 그 시점의 환경 변수 `AGENTKIT_DATA_DIR`이 있으면 그것, 없으면 `config.DATA_DIR`. 시험은 `trace.reset(tmp_path)`로 바꾸고 끝에 `trace.reset(None)`을 부른다. `reset`은 남은 기록을 쓰고 쓰기 스레드를 멈춘다.

## 2. 데이터베이스 (`<DATA_DIR>/trace.sqlite3`, WAL, `secure_delete=ON`)

```sql
sessions(session_id PK, agent_id, version, channel, test, simulated, started_at, ended_at,
         status DEFAULT 'active', label, settings JSON)
turns(session_id, turn, user_text, agent_text, t_user_end, t_agent_start, t_agent_end,
      interrupted, handoff, PRIMARY KEY(session_id, turn))        -- 같은 턴이 다시 오면 덮어쓴다
spans(id PK AUTOINCREMENT, session_id, turn, kind, name, t_start, t_end, ok,
      input JSON, output JSON, attrs JSON)
documents(kind, key, session_id, value JSON, created_at, updated_at, PRIMARY KEY(kind, key))
```

- 시각은 epoch 초(REAL). JSON은 UTF-8 그대로(`ensure_ascii=False`).
- `documents.session_id`: 값에 `session_id`가 있으면 그 값, 없고 `key`가 저장된 세션 id면 `key`. `purge`가 세션과 함께 지우는 기준이다.
- 음성: `<DATA_DIR>/audio/<session>/<turn>-<role>.wav` (16kHz 모노 16비트). 같은 턴·역할로 다시 오면 뒤에 이어 붙인다. 세션 id의 `[A-Za-z0-9._-]` 밖 글자는 폴더 이름에서 `_`로 바뀐다.
- 연결은 작업마다 새로 연다. 파일이 지워지면 다음 쓰기 때 다시 만든다.

## 3. 기록기 동작

- 호출은 `model_dump()`로 복사해 큐(최대 20000건)에 넣고 돌아온다. 쓰기 스레드가 최대 200건씩 묶어 가리고 한 트랜잭션으로 쓴다.
- 큐가 차면 그 기록을 버린다. 가리기나 쓰기가 실패하면 그 묶음을 버린다. 경고는 5초에 한 번(`agentkit.trace` 로거). `recorder.stats()["dropped"]`에 버린 수가 쌓인다. 호출은 인자가 틀려도 예외를 내지 않는다.
- 음성: 그 프로세스에서 `start_session`으로 본 세션이 시험·가상이 아니면 큐에도 넣지 않는다. 모르는 세션이면 쓰기 스레드가 저장소의 세션 행을 보고 정한다(없으면 버림). 대기 중인 음성은 64MB까지.
- `end_session`은 호출 시각을 `ended_at`으로 남긴다.
- 프로세스가 끝날 때 `atexit`으로 2초까지 남은 기록을 쓴다.
- 쓰기 스레드가 시작 30초 뒤와 이후 6시간마다 `store.purge()`를 부른다(`config.RETENTION_DAYS`).

### 측정한 호출 시간

이 장비, 메시지 21개짜리 `llm` 구간, 쓰기 스레드가 동시에 가리고 쓰는 중, 10000회 × 3:

- 중앙값 10~11µs, p99 21~31µs.
- 1ms를 넘은 호출 13~14회/10000. 모두 5.1ms였다. 쓰기 스레드가 정규식을 돌리며 인터프리터 락(GIL)을 쥐고 있을 때 기다리는 시간이다(전환 간격 5ms). 쓰기 스레드가 놀고 있으면 생기지 않는다.
- 시험 안의 측정(1650회)에서는 최대 0.2ms~33ms가 섞여 나왔다. 10ms 이상은 측정 루프가 구간 1500개를 메모리에 쥐고 있을 때의 가비지 컬렉션이었다(GC를 끄면 5.1ms로 내려감).
- 결론: "보통 0.01ms, 드물게 5ms". 1ms 보장은 아니다. 가리기를 다른 프로세스로 옮기지 않는 한 GIL 대기는 남는다.

## 4. 가리기

세 겹이다. 모두 쓰기 스레드에서, 디스크에 쓰기 전에 한다.

1. **경로**: 구간과 함께 온 `pii_paths`(`input.arguments.phone_last4`, `output.data.address`). 목록을 만나면 원소마다 적용, `*`는 아무 키. 끝 키에 `name`/이름/성함이 들어 있으면 `[이름]`, 아니면 `[가려짐]`. `input.`/`output.`으로 시작하지 않는 경로는 `input.X`, `output.X`, `input.arguments.X`, `output.data.X`를 모두 시도한다(계약서의 필드 이름을 그대로 넘겨도 된다).
2. **키 이름**: 경로가 없어도 dict 키가 개인정보를 뜻하면 값(문자열, 숫자, 그 목록)을 바꾼다. `phone*`, `mobile*`, `tel`, `email*`, `address*`, `addr`, `ssn`, `rrn`, `birth*`, `card_no/num/number`, `customer_name`, `user_name`, `full_name`, `recipient*`, `receiver*`, `last4`, 앞에 `xxx_`가 붙은 꼴, 한국어 키(이름, 성함, 고객명, 주문자, 수령인, 전화번호, 휴대폰, 연락처, 주소, 이메일, 생년월일). `name` 단독은 대상이 아니다(도구 이름, 에이전트 이름과 겹침). 글 속의 `"customer_name": "김민수"`, `이름: 최영희` 꼴도 같은 키 목록으로 가린다(모델에 넣은 도구 결과 JSON 대비).
3. **배운 값**: 1, 2에서 가려진 원래 값(2~200자)을 세션별로 메모리에만 기억해, 그 세션의 다른 기록에 같은 글자가 나오면 `[가려짐]`으로 바꾼다. 이미 저장된 그 세션의 구간과 턴도 다시 써서 바꾼다. 숫자만으로 된 값은 앞뒤가 숫자가 아닐 때만, 뒤에 `원`이 없을 때만 바꾼다. `end_session` 때 잊는다.
4. **글 패턴** (`mask_text`): 아래 표.

### 잡는 것 (전부 `tests/test_trace_masker.py`의 `CAUGHT`에 있다)

| 대상 | 잡는 꼴 | 표시 |
|---|---|---|
| 휴대폰 | `010-1234-5678`, `01012345678`, `010 1234 5678`, `010.1234.5678`, `011-234-5678`, `+82 10-1234-5678`, "공일공 일이삼사 오육칠팔" | `[전화번호]` |
| 일반 전화 | `02-345-6789`, `031-123-4567`, `070-1234-5678`, `0507-1234-5678` | `[전화번호]` |
| 뒤 4자리 | "뒤 네 자리는 1234", "뒷자리 1234", "뒷번호가 5678", "뒤 4자리는 9012", "끝자리는 4321", "마지막 네 자리 7788", "1234가 뒷자리예요", "뒷자리는 일이삼사", "last four digits are 4321". 숫자만 바뀌고 앞말은 남는다 | `[전화번호]` |
| 이메일 | `a.b+c@example.co.kr` | `[이메일]` |
| 주민등록번호 | `900101-1234567`, 붙여 쓴 13자리(앞 6자리가 날짜, 7번째가 1~8) | `[주민번호]` |
| 카드번호 | 13~16자리 연속, 4자리씩 띄어쓰기/하이픈, 4-6-5 | `[카드번호]` |
| 주문번호 | 영문 1~3자 + 숫자 3자리 이상(`A1001`, `ab12345`, `ORD20261`), "주문번호(는) " 뒤에 오는 영숫자 4자 이상(`20261105-0001`) | `[주문번호]` |
| 주소 | (시·도) + 시/군/구 0~2개 + 로/길/동/읍/면/리/가 + 번지수 (+ 건물, 동, 호, 층). 시·도 없이 "강남구 테헤란로 152"도 | `[주소]` |
| 이름 | "(제) 이름은/성함은 X입니다·예요·…", "저는 X(이)라고 합니다", "X 고객님", "X 회원님", "성씨+1~2자+님"(홍길동님), "my name is John Smith" | `[이름]` |

남는 것(시험 `SURVIVES`): `30000원`, `1,250,000원`, `2026-11-17`, `2026년 11월 3일 15시 30분`, `20261117`, "뒤 네 자리는 5000원", "역삼동 20도"(날씨), uuid, `gemma4:26b`, "안녕하세요 고객님".

### 못 잡는 것 (시험 `MISSED`에 고정해 둔 것과 그 밖에 아는 것)

- **단서 없는 이름**: "김민수입니다", "저는 김민수입니다", "민수라고 불러 주세요", 답변 속 "김민수 씨". 성과 이름을 띄어 쓴 "김 민수 고객님"은 "민수"만 가려진다. 2~4자가 아닌 이름, 외국 이름(영어는 "my name is"만).
- **단서 없는 숫자**: "1234요" 같은 뒤 4자리. 도구 계약서가 그 인자를 `pii_input`으로 표시했다면 3번(배운 값)으로 가려지지만, 도구 구간이 도착하기 전에는 원문이 잠깐 디스크에 있었다가 덮어쓰인다. 아래 5절의 첫 항목.
- 조사가 낀 숫자 읽기: "공일공에 일이삼사에 오육칠팔". 한글로 읽은 주문번호("에이 천일"), 주민번호, 카드번호.
- 구/군/시가 없는 주소("테헤란로 152 3층"), 번지수 없는 주소("서울 강남구 역삼동"), 건물 이름만("○○아파트 101동 1203호").
- 풀어 쓴 이메일("kim at example dot com"), 하이픈이 낀 주문번호("A-1001"), 숫자만으로 된 주문번호("주문번호" 말이 앞에 없을 때).
- 대표번호(1588-1234)는 개인정보가 아니라고 보고 두었다.
- 계좌번호, 여권번호, 운전면허번호, 차량번호, 생년월일(날짜와 구분 못 함)은 규칙이 없다.
- 음성 파일 안의 말은 가릴 수 없다. 그래서 시험·가상 세션만 저장한다.

### 잘못 가리는 것 (아는 것)

- 영문+숫자 제품명이 주문번호로 가려진다: `RTX4090`, `A380`, `H264` (시험에 고정). `tech-trend-anchor` 기록에서 눈에 띌 수 있다.
- 4자리 수 네 개가 나란히 있으면 카드번호("2024 2025 2026 2027"), 13자리 연속 숫자(ms 단위 시각)는 주민번호나 카드번호가 된다.
- "X 고객님"의 X가 이름이 아닌 2~4자 낱말이면 가려진다(자주 나오는 말 수십 개와 요/다/까/죠/님으로 끝나는 말은 뺐다. 그래도 "단골 고객님"은 가려진다). "성씨+님"도 같다(직함 수십 개 제외).
- 글 속 `전화번호: …`, `주소: …`는 줄 끝이나 쉼표까지 통째로 가린다.
- 배운 값이 짧으면(2자 이름 등) 같은 글자가 든 다른 낱말도 가려진다.
- 값으로 들어 있는 짧은 식별자가 주문번호 패턴에 걸릴 수 있다: 영문 1~3자+숫자 3자리 이상이고 앞뒤가 `-`, `_`, 영숫자가 아닐 때(`s1001`, `p001`). 세션 id는 열에 따로 저장하므로 영향이 없지만, 구간 안의 `call_id` 같은 값은 바뀔 수 있다.

## 5. 계약과 달라진 점, 알아 둘 것

1. **"저장 전에 가린다"의 예외 하나.** 패턴이 못 잡는 값(단서 없는 "1234요")이 도구 구간보다 먼저 저장되면, 도구 구간이 올 때까지 원문이 디스크에 있다. 도구 구간이 오면 그 세션의 기존 행을 다시 쓴다(`secure_delete`로 옛 내용은 0으로 덮임). 시험(`test_nothing_unmasked_reaches_the_disk`)은 이 경우를 포함해 파일 바이트에 원문이 없음을 확인한다. 다만 그 사이에 다른 연결이 열려 있어 WAL 파일이 남아 있는 동안에는 옛 프레임이 `trace.sqlite3-wal`에 남을 수 있다(확인하지 않았다). 도구가 호출되지 않으면 그 값은 그대로 남는다.
2. **`put_document(kind, key, value, mask=True)`**: 계약에 없는 `mask` 인자. 기본으로 문서의 문자열도 가린다(V5 대비). 식별자 키(`session_id`, `agent_id`, `run_id`, `item_id`, `id`, `key`, `case_id`, `base_run`, `new_run`, `test_run_id`, `kind`, `judge`, `metric`, `op`, `type`, `status`, `channel`, `state`, `improved`, `regressed`, `unchanged_pass`, `unchanged_fail`, `mismatches`, `evidence_turns`)의 값은 그대로 둔다. 그래서 사례 문서의 `facts: {order_id: "A1001", phone_last4: "1234"}`는 `[주문번호]`, `[가려짐]`으로 저장된다. 원문이 필요하면 `mask=False`(그 값은 V5 검색에 걸린다). **WP6 확인 필요**: 가상 고객 id가 `p001` 꼴이고 식별자 키가 아닌 곳(예: 문서의 `label`)에 들어가면 가려진다.
3. **`put_document`는 쓰기가 실패하면 예외를 낸다.** 대화 경로가 아니어서다. 기록기 호출만 예외가 없다.
4. **`list_documents`의 반환**: 값 dict의 목록(키 순). 각 원소는 dict의 하위 클래스라 `d["run_id"]`처럼 쓰고, `d.key`, `d.kind`, `d.created_at`, `d.updated_at`도 있다. `get_document`도 같고 없으면 `None`. `delete_document(kind, key)`를 더했다.
5. **`get_session`**: 없으면 `None`. 계약의 `meta`, `status`, `turns`, `spans`에 `ended_at`, `audio`(`[{turn, role, bytes}]`), `analysis`(문서가 있으면 그 dict)를 더했다. 턴에 `response_ms`, 구간에 `id`, `ms`가 붙는다. 구간은 `t_start` 순.
6. **REST 응답 꼴** (ARCHITECTURE에 정해져 있지 않아 정했다. WP7과 맞춰야 한다):
   - `GET /api/sessions` → `{"sessions": [행...]}` (배열이 아니라 객체). 추가 필터 `version=`, `test=0|1`.
   - 행: `session_id`, `agent_id`(= `agent`), `version`, `channel`, `label`, `test`, `simulated`, `started_at`, `ended_at`, `status`, `turns`(= `turn_count`), `response_ms_median`, `response_ms_max`, `handoff`, `interrupted`(횟수), `analysis`(`null` 또는 `{passed, failed, unjudged, total, safety_failed, all_passed, failed_ids, created_at}`).
   - `GET /api/sessions/{sid}` → `get_session` 그대로, 없으면 404.
   - `GET /api/metrics` → 아래 7번. 추가 필터 `test=0|1`.
   - `GET /api/live` → `data: {json}\n\n` 한 줄씩(`event:` 필드 없음, `EventSource.onmessage`로 받는다). 처음에 `retry: 2000`과 `{"type":"hello"}`. 15초 동안 조용하면 `: ping`. 종류: `session`(`meta`), `turn`(`record`), `span`(`span`; 입력·출력이 4000자를 넘으면 빼고 `truncated: true`), `session_end`(`status`, `ended_at`), `audio`(`turn`, `role`), `analysis`(`key`, `summary`), `document`(그 밖의 문서: `kind`, `key`). 모두 `session_id`를 갖는다(문서는 없을 수 있음). 클라이언트당 1000건까지 쌓이고 넘치면 그 클라이언트 몫만 버린다.
7. **`metrics` 반환** (최근 `days`일에 시작한 세션): `sessions`, `turns`, `status`(상태별 수), `response_ms {median, p90, count}`(+ `response_ms_median`, `response_ms_p90`), `span_ms {kind: {median, p90, count}}`, `tools {calls, failures, failure_rate, needs_confirmation, executed, error_types, by_name}`(+ `tool_failure_rate`), `handoff {sessions, rate}`(+ `handoff_rate`), `interruptions`, `criteria {sessions_analyzed, sessions_all_passed, judged, passed, pass_rate, safety_failures, by_id}`(+ `criteria_pass_rate`), `by_version`(에이전트·버전별로 같은 값), `by_day`(날짜별, `config.TIMEZONE`). 값이 없으면 비율과 중앙값은 `null`.
   - 도구 실패: `kind="tool"`이고 `ok=false`, 그리고 `needs_confirmation`이 아닌 것.
   - 넘김: 세션 상태가 `handoff`이거나, 턴의 `handoff`가 참이거나, `handoff` 구간이 있는 세션.
   - 통과율: 판정된 기준(`passed`가 `null`이 아닌 것) 중 통과 비율. 세션당 가장 최근 분석 문서 하나.
   - 시험·가상 세션도 기본으로 함께 센다(`test=0`으로 뺄 수 있다).
8. **분석 문서 찾기**: `kind="analysis"`이고 값의 `session_id`가 그 세션이거나 `key`가 세션 id인 것. WP6이 다른 키를 쓰더라도 값에 `session_id`가 있으면 된다.
9. **`purge`**: 끝난 시각(없으면 시작 시각)이 기한보다 오래된 세션과 그 턴, 구간, 세션에 딸린 문서, 음성 폴더를 지우고 세션 수를 돌려준다. 세션 행이 없는 오래된 구간·턴·음성 폴더도 지운다. 세션에 딸리지 않은 문서(시험 실행, 사례)는 지우지 않는다. 기한 정책이 필요하면 정해야 한다.
10. 계약의 `Masker.mask`에 키워드 인자 `protect_keys`를 더했다.

## 6. 못 끝낸 것, 남은 문제

- 끝나지 않고 죽은 세션은 `active`로 남는다. 정리하는 장치가 없다(보관 기한이 지나면 지워진다).
- 여러 프로세스가 같은 `trace.sqlite3`에 쓰는 경우는 SQLite 잠금(대기 5초)에 맡겼고 시험하지 않았다. 실시간 알림과 "배운 값"은 프로세스 안에서만 통한다. 다른 프로세스가 쓴 기록은 `/api/live`에 나오지 않는다.
- 쓰기가 계속 실패할 때 다시 시도하지 않는다. 그 묶음은 사라진다.
- 프로세스가 강제 종료되면 큐에 있던 기록은 사라진다.
- `/api/sessions`는 목록마다 턴을 읽어 중앙값을 계산한다. 세션이 수만 건이 되면 `metrics`와 함께 느려질 수 있다(재지 않았다).
- 새 패키지 설치 없음. SSE 시험은 시험 프로세스 안에서 uvicorn을 빈 포트에 잠깐 띄웠다 내린다.
