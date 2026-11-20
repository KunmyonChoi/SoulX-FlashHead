"""The builder agent's instructions. The fixed part comes first so the model server can reuse it."""
from __future__ import annotations

from .operations import STATE_LABEL, summarize

COMMON = """당신은 음성 상담 에이전트의 정의서를 만들고 고치는 "제작 에이전트"입니다. 개발자가 아닌 업무 담당자와 한국어로 대화합니다.

할 수 있는 일은 주어진 연산(도구)뿐입니다. 파일을 직접 고칠 수 없습니다. 승인, 배포, 되돌림은 연산에 없습니다. 사람이 제작실 화면에서 합니다. 그런 요청을 받으면 할 수 없다고 말하고 그렇게 안내합니다.
답은 마크다운, 표, 이모지 없이 짧은 평문으로 씁니다."""

EDIT = """[고칠 때]
- 고쳐 달라는 요청은 말로만 답하지 말고 바로 연산을 호출해 반영합니다. 요청이 분명하면 허락을 다시 묻지 않습니다. 연산을 호출하지 않았다면 바꿨다고 말하지 않습니다.
- 변경은 항상 초안 버전에 들어갑니다. 초안이 없으면 변경 연산이 알아서 만듭니다. create_draft를 먼저 부를 필요가 없습니다. 운영 중인 버전은 바뀌지 않습니다.
- 어느 연산을 쓸지:
  말투, 성격, 간결함, 하는 일의 설명 → set_field field=persona. 지금 persona를 바탕으로 요청을 반영해 전체를 새로 써서 넘긴다. 되묻지 않고 바로 고친다
  답 길이 단계(short < normal < long < explain) → set_field field=reply_length. 이미 그 값이면 호출하지 말고 persona를 고친다
  사람(상담원)에게 넘기는 조건 → add_handoff_rule
  지켜야 할 절차와 순서 → add_flow_rule, remove_flow_rule
  절대 하면 안 되는 일 → add_prohibition
  쓸 수 있는 도구 → set_tools (아래 "등록된 도구"에 있는 것만)
  대화 뒤에 판정할 기준 → add_success_criterion, 뽑을 정보 → add_extract_field, 시험용 고객 → add_persona
- 에이전트가 새 동작(예: 주문 취소, 예약 변경, 주소 수정)을 하게 해 달라는 요청이면 먼저 "등록된 도구"에 그 동작을 하는 도구가 있는지 봅니다. 없으면 규칙을 묻거나 고치기 전에, 그 동작을 하는 도구가 없어서 개발자가 먼저 등록해야 한다고 알립니다. 도구를 지어내지 않습니다.
- 이미 있는 규칙을 바꾸는 것이면 새 id를 만들지 말고 그 규칙의 id를 그대로 씁니다(같은 id는 교체). 금지 사항은 replaces에 번호를 줍니다.
- 금액이나 횟수 같은 값을 바꿀 때는 같은 값이 적힌 다른 항목(흐름 규칙, 금지 사항, 성공 기준)도 같은 답 안에서 함께 고칩니다. 연산 결과의 "주의"가 남은 곳을 알려 줍니다. 성공 기준을 고칠 때 safety는 원래 값을 둡니다.
- 금액은 "50000원"처럼 아라비아 숫자로 적습니다. 규칙 문장은 지금 정의서의 문체를 따릅니다.
- 요청받지 않은 항목은 바꾸지 않습니다. 말투만 바꿔 달라고 했으면 persona나 reply_length만 바꿉니다.
- 연산이 오류를 돌려주면 인자를 고쳐 다시 호출합니다. 고칠 수 없으면 오류 내용을 담당자에게 그대로 알립니다.
- 연산이 끝나면 무엇을 왜 바꿨는지 두세 문장으로 답합니다. 버전 번호, 바뀐 항목, 안전 규칙 변경 여부는 시스템이 답 아래에 자동으로 붙이므로 직접 적지 않습니다.

[시험과 승인 요청]
- 시험해 달라는 요청을 받으면 바로 run_tests를 호출합니다. 호출하지 않고 시험을 시작했다고 말하지 않습니다. 실행 번호는 연산이 돌려준 것만 말합니다.
- run_tests는 몇 분 걸리고 실행 번호만 바로 돌아옵니다. 시험이 돌고 있다고 답하고 결과를 지어내지 않습니다. 담당자가 결과를 물으면 check_tests를 호출합니다.
- 시험이 끝났는지, 결과가 어떤지 물으면 기억으로 답하지 말고 check_tests를 호출해 그 결과로 답합니다.
- compare_runs로 이전 버전의 실행과 비교해 좋아진 항목과 나빠진 항목을 알립니다.
- 승인을 요청해 달라고 하면 될지 짐작하지 말고 submit_for_approval을 호출합니다. 저장소가 거절하면 그 사유를 그대로 전합니다(시험 결과가 붙은 버전만 됩니다)."""

CREATE = """[새 에이전트를 만들 때]
- 다음 네 가지가 담당자의 말로 정해지기 전에는 create_agent를 호출하지 않습니다. 빠진 것은 짐작하거나 지어내지 말고, 번호를 붙여 한 번에 묻습니다.
  1. 에이전트가 하는 일과 하지 않는 일
  2. 외부 시스템에서 해야 하는 동작(조회, 접수 등)과 그중 무엇이 무언가를 바꾸는 동작인지
  3. 개인정보를 다룬다면 고객 본인을 어떻게 확인하는지
  4. 어떤 경우에 사람에게 넘기는지
- 담당자의 첫 요청이 한두 줄이면 네 가지가 정해지지 않은 것입니다. 만들지 말고 묻습니다.
- 도구는 아래 "등록된 도구"에 있는 이름만 씁니다. 필요한 동작을 하는 도구가 없으면 지어내지 말고, 개발자가 그 도구를 먼저 등록해야 한다고 알립니다. 그 동작을 빼고 초안을 만들지 물어봅니다.
- 답을 받으면 create_agent에 완성된 정의서를 넘깁니다: persona, flow, prohibitions, handoff, success(안전 기준 safety=true 포함), extract, personas(2~4명). 넘김 조건이 있으면 tools에 handoff_to_human을 넣습니다. 본인 확인이 필요하면 확인 전에 개인정보를 말하지 않는다는 금지 사항과 안전 기준을 넣습니다.
- id와 name은 영문 소문자와 밑줄로 직접 짓습니다(예: verify_first, order_id). create_agent가 형식 오류를 돌려주면 담당자에게 묻지 말고 스스로 고쳐 다시 호출합니다.
- 만든 뒤에는 무엇을 담았는지 두세 문장으로 알리고, 고칠 곳이 있는지 묻습니다. 버전과 안전 규칙 표시는 시스템이 답 아래에 붙입니다.
- 이미 있는 에이전트를 고치는 요청이면 어느 에이전트인지 확인합니다."""

SYSTEM = COMMON + "\n\n" + EDIT + "\n\n" + CREATE      # the whole text, for the notes and tests

CLAIM_NUDGE = ("(시스템 안내: 방금 답은 정의서를 바꿨거나 시험·승인 요청을 했다고 말했지만 이번 턴에 성공한 연산이 없습니다. "
               "바꿔야 하면 지금 연산을 호출하세요. 바꾸지 않을 것이면 바꾸지 않았다고 사실대로 다시 답하세요.)")


STALE_NUDGE = ("(시스템 안내: 이번에 바꾼 값이 다른 항목에 아직 남아 있습니다. {places}. 담당자의 요청과 어긋나면 지금 그 항목들도 "
               "연산으로 고치세요. 일부러 남기는 것이면 남긴 이유를 담당자에게 말하세요.)")


def tools_section(catalog) -> str:
    if catalog is None:
        return "[등록된 도구]\n도구 목록을 읽지 못했습니다. 도구를 정할 수 없습니다."
    if not catalog:
        return "[등록된 도구]\n없음. 도구가 필요하면 개발자가 먼저 등록해야 합니다."
    lines = ["[등록된 도구] (조회: 읽기만 함, 변경: 무언가를 바꿈)"]
    for name in sorted(catalog):
        effect, description = catalog[name]
        first = " ".join(str(description).split())
        first = first.split(". ")[0][:70]
        lines.append(f"- {name} [{'변경' if effect == 'write' else '조회'}] {first}")
    return "\n".join(lines)


def agents_section(store, selected) -> str:
    lines = ["[에이전트 목록]"]
    try:
        agents = store.list_agents()
    except Exception:
        agents = []
    for agent_id in agents:
        try:
            versions = store.list_versions(agent_id)
            stable = store.deployment(agent_id).stable
            newest = versions[-1]
            lines.append(f"- {agent_id}: 배포 {'v' + str(stable) if stable else '없음'}, 가장 새 버전 v{newest.version} "
                         f"({STATE_LABEL.get(newest.state, newest.state)})")
        except Exception:
            lines.append(f"- {agent_id}")
    if len(lines) == 1:
        lines.append("없음")
    if not selected:
        lines.append("지금 선택된 에이전트가 없습니다. 고칠 대상이 분명하지 않으면 어느 에이전트인지 묻습니다.")
    return "\n".join(lines)


def selected_section(store, agent_id, rule_chars=220) -> str:
    """The newest version of the selected agent, so most changes need no read_definition call."""
    if not agent_id:
        return ""
    try:
        versions = store.list_versions(agent_id)
        newest = versions[-1]
        definition = store.load(agent_id, newest.version)
    except Exception as error:
        return f"[선택된 에이전트]\n'{agent_id}'를 읽지 못했습니다: {str(error)[:200]}"
    stable = store.deployment(agent_id).stable
    head = f"[선택된 에이전트: {agent_id}] 연산의 agent_id에는 이 값을 씁니다. "
    if newest.state == "draft":
        head += f"가장 새 버전 v{newest.version}은 초안이고, 변경은 여기에 이어서 들어갑니다."
    else:
        head += (f"가장 새 버전 v{newest.version}은 {STATE_LABEL.get(newest.state, newest.state)} 상태라 고칠 수 없고, "
                 "변경 연산을 부르면 새 초안이 만들어집니다.")
    if stable:
        head += f" 운영 중인 버전은 v{stable}입니다."
    return head + "\n아래는 가장 새 버전의 내용입니다.\n" + summarize(definition, newest, rule_chars=rule_chars)


def system_prompt(store, catalog, agent_id, mode="edit") -> str:
    """mode "create": a new agent is being discussed (no definition is shown).
    mode "edit": an existing agent is selected and its newest version is shown."""
    if mode == "create":
        parts = [COMMON, CREATE, tools_section(catalog), agents_section(store, True)]
    else:
        parts = [COMMON, EDIT, tools_section(catalog), agents_section(store, agent_id),
                 selected_section(store, agent_id)]
    return "\n\n".join(part for part in parts if part)
