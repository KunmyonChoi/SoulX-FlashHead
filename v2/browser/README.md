# 브라우저 조작 서비스 (비서 시나리오의 "뒤쪽 두뇌")

음성 에이전트(`office-assistant`)가 말로 받은 일을 웹 사이트에서 대신 처리한다. 화면은 그림이 아니라 접근성 트리(역할, 이름, 요소 번호)로 읽는다.

```bash
cd v2/browser
/data/miniforge3/bin/python3 service.py          # 7895: 서비스, 7890: 더미 오피스(로그인·일정·주소록·메일)
/data/miniforge3/bin/python3 run_tasks.py gemma4:26b 2    # 과제 10개 시험 (자체 더미 오피스를 7891에 띄움)
```

- `agent.py`: 조작기. 모델 바깥에서 강제하는 규칙 네 가지 — 금고(로그인은 조작기가 채움), 허용 주소, 되돌릴 수 없는 단추 앞에서 멈춤, 절차 기록과 재생.
- `service.py`: 세션마다 작업 하나. `/task`, `/answer`, `/confirm`(+`/confirm_preview`). 도구 계약은 `v2/tools/web_task*.yaml`.
- `office_site.py`: 시험용 더미 사이트. API가 아니라 화면으로만 쓴다.
- 모델은 `BROWSER_MODEL`, 문맥 크기는 `BROWSER_NUM_CTX`(음성 에이전트와 같아야 모델을 다시 올리지 않는다).
- Playwright가 필요해 음성 서버(flashhead 환경)와 다른 파이썬에서 돈다.

## 맡겨 두고 대화 이어 가기

- 도구 호출은 `BROWSER_WAIT`초(기본 6)만 기다린다. 그 안에 끝나지 않으면 "진행 중"으로 돌아와 대화가 풀리고,
  작업이 다음 멈춤(결과, 되물음, 실행 전 확인)에 닿으면 서비스가 음성 서버의 `POST /api/sessions/{id}/notify`로 알린다.
- 음성 서버는 그 소식을 `engine.notify(text)`로 넘겨 비서가 먼저 말하게 한다. 음성 채널에서는 양쪽 모두 말하지 않는
  순간까지 기다린다(`VoiceSession.notify`).
- 알림은 사용자의 말이 아니다. 그 턴에서는 게이트웨이에 사용자 발화를 넘기지 않으므로, 알림 속 문장으로는 동의가 성립하지 않는다.
- 음성 서버 주소는 `AGENT_URLS`(쉼표로 구분, 기본 7871과 7872)에서 찾는다.
