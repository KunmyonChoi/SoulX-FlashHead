"""The six shipped definitions (R-DEF-4, R-DEF-6, R-SHOP-5). Read-only on v2/agents."""
import re
from pathlib import Path

import pytest

from agentkit.definition import DefinitionStore

V2 = Path(__file__).resolve().parent.parent
REPO = V2.parent
SKELETONS = ["drive-thru-order", "flight-rebooking", "clinic-intake"]   # skeletons at first; complete since 2026-11-18
KIOSK = "kiosk-order"       # complete, but not tested or deployed yet
FILES = ["agent.yaml", "tools.yaml", "flow.yaml", "guards.yaml", "evaluation.yaml", "tests.yaml", "meta.json"]


@pytest.fixture(scope="module")
def store():
    return DefinitionStore(V2 / "agents")


ASSISTANT = "office-assistant"   # the assistant that delegates web tasks to the browser service


def test_six_agents(store):
    assert store.list_agents() == sorted(["tech-trend-anchor", "shop-support", KIOSK, ASSISTANT, *SKELETONS])


@pytest.mark.parametrize("agent", ["tech-trend-anchor", "shop-support", KIOSK, *SKELETONS])
def test_folder_layout_and_load(store, agent):
    folder = V2 / "agents" / agent / "versions" / "1"
    for file in FILES:
        assert (folder / file).is_file(), file
    d = store.load(agent, 1)
    assert d.id == agent and d.version == 1 and d.persona.strip()
    for guard in d.guards:
        re.compile(guard.pattern)


@pytest.mark.parametrize("agent", ["tech-trend-anchor", "shop-support"])
def test_main_agents_are_deployed(store, agent):
    deployment = store.deployment(agent)                        # which version is a release decision
    stable = deployment.stable
    info = next(v for v in store.list_versions(agent) if v.version == stable)
    assert info.state == "deployed" and info.approved_by
    assert deployment.canary is None
    assert store.load(agent).version == stable                  # version None -> stable
    assert store.resolve(agent, "any-session") == stable


@pytest.mark.parametrize("agent", [KIOSK, *SKELETONS])
def test_undeployed_agents_have_no_stable_version(store, agent):
    # complete, and possibly tested, but a person has not approved and deployed them
    (info,) = store.list_versions(agent)
    assert info.state in ("draft", "tested", "pending_approval", "approved")
    assert not (V2 / "agents" / agent / "deployment.json").exists()
    assert store.deployment(agent).stable is None
    d = store.load(agent, 1)
    assert d.flow and d.handoff and d.prohibitions and d.tools
    assert d.evaluation.success and d.evaluation.extract
    assert any(c.safety for c in d.evaluation.success)


def test_anchor_matches_v1(store):
    d = store.load("tech-trend-anchor", 1)   # version 1 is the port of v1; later versions may differ
    assert d.tools == ["get_weather", "search_news", "get_news_detail", "search_geeknews", "read_geeknews",
                       "search_wikipedia", "list_knowledge_by_date"]
    assert d.knowledge.kind == "weknora" and d.knowledge.name == "Tech Trend"
    assert d.knowledge.kb_id == "29f24b56-2379-4a82-96c7-e1b01963206c" and d.knowledge.threshold == 0.4
    assert d.avatar.enabled and d.avatar.image == "examples/anchor_studio_face.png"
    assert (REPO / d.avatar.image).is_file()
    assert d.live_clock is True
    assert all(g.kind == "claims_lookup" for g in d.guards)

    # the persona is v1's default system prompt, the guard patterns are v1's (read from the v1 source text)
    v1 = (REPO / "realtime_mic_app.py").read_text(encoding="utf-8")
    for sentence in ("당신은 화면 속 아바타로, 사용자와 음성으로 대화합니다.",
                     "한국어 구어체로 한두 문장, 길어도 세 문장 이내로 짧게 답하세요.",
                     "답변은 그대로 음성으로 읽히므로 마크다운, 목록, 이모지, 괄호 설명, 영어 단어는 쓰지 마세요."):
        assert sentence in d.persona and sentence in v1
    guards = {g.id: g for g in d.guards}
    for fragment, guard in (("(확인|찾아|알아|검색|조회)", "lookup_claim"), ("(날씨|뉴스)를", "lookup_claim"),
                            ("(?:영상|영하|기온", "forecast_temperature"), ("비가|비 소식|비 올", "weather_statement")):
        assert fragment in v1 and fragment in guards[guard].pattern
    assert "도구를 호출하지 않고 날씨나 뉴스를 확인한 것처럼" in guards["lookup_claim"].nudge


def test_anchor_guards_behave_like_v1(store):
    guards = {g.id: re.compile(g.pattern) for g in store.load("tech-trend-anchor").guards}

    def hit(text):
        return sorted(name for name, pattern in guards.items() if pattern.search(text))

    assert "lookup_claim" in hit("확인해 보니 주말엔 비가 온대요.")
    assert hit("확인해 볼게요.") == ["lookup_claim"]
    assert hit("말씀해 주시면 찾아볼게요.") == []                 # an offer, not a claim
    assert hit("내일은 많이 추워요.") == ["weather_statement"]
    assert hit("내일 날씨가 추울까요?") == []                     # a question
    assert hit("내일은 영상 8도예요.") == ["forecast_temperature"]
    assert hit("안녕하세요, 반가워요.") == []


def test_shop_support_scenario(store):
    d = store.load("shop-support")
    assert d.tools == ["verify_customer", "get_order", "track_delivery", "request_refund", "handoff_to_human"]
    flow = " ".join(rule.rule for rule in d.flow)
    for needle in ("주문번호", "휴대폰 뒤 4자리", "verify_customer", "상품명과 금액", "같은 인자로 request_refund",
                   "100000원", "handoff_to_human", "7일"):
        assert needle in flow, needle
    assert any("100000원" in rule.when for rule in d.handoff)
    assert any("본인 확인 전" in text for text in d.prohibitions)

    criteria = {c.id: c for c in d.evaluation.success}
    for name in ("verified_before_disclosure", "consent_before_refund", "no_refund_over_limit"):
        assert criteria[name].safety and criteria[name].judge == "llm"
    assert any(c.judge == "metric" for c in criteria.values())

    fields = {f.name: f for f in d.evaluation.extract}
    assert fields["inquiry_type"].type == "enum" and fields["inquiry_type"].values
    for name in ("resolved", "refund_requested", "handoff"):
        assert fields[name].type == "boolean"


def test_shop_support_tests(store):
    d = store.load("shop-support")
    personas = {p.id: p for p in d.tests.personas}
    assert len(personas) >= 6 and len(d.tests.cases) >= 2
    for name in ("refund_small", "refund_large", "skip_verification", "wrong_phone", "change_of_mind",
                 "delivery_only", "refund_expired"):
        assert personas[name].facts and personas[name].expect, name
    orders = {"A1001": "1234", "A1002": "5678", "A1003": "2468", "A1004": "1357", "A1005": "9012"}
    assert personas["refund_small"].facts["order_id"] == "A1001"
    assert personas["refund_small"].facts["phone_last4"] == orders["A1001"]
    assert personas["refund_large"].facts["phone_last4"] == orders["A1002"]
    assert personas["wrong_phone"].facts["phone_last4"] != orders[personas["wrong_phone"].facts["order_id"]]
    assert personas["refund_large"].expect["no_handoff"] is False
    for item in [*d.tests.personas, *d.tests.cases]:
        for value in item.facts.values():
            assert not isinstance(value, int) or value > 9999 or isinstance(value, bool), \
                f"{item.id}: phone digits must be quoted strings"
        assert "verified_before_disclosure" in item.expect
    for case in d.tests.cases:
        assert len(case.user_turns) >= 2


# ---- kiosk-order: the second complete scenario (docs/DISPLAY.md S12) ------------------------------

CAFE_TOOLS = ["get_menu", "search_menu", "get_cart", "add_to_cart", "remove_from_cart", "apply_coupon",
              "request_payment", "handoff_to_human"]


def test_only_the_kiosk_has_a_screen(store):
    for agent in ["tech-trend-anchor", "shop-support", *SKELETONS]:
        d = store.load(agent, 1)
        assert d.opening.say == "" and not d.opening.tools and not d.screen.panels and not d.actions, agent


def test_kiosk_definition(store):
    d = store.load(KIOSK, 1)
    assert d.tools == CAFE_TOOLS
    assert d.opening.say.strip() and [call.name for call in d.opening.tools] == ["get_menu"]
    assert d.screen.panels == ["menu", "cart"]
    assert {a.name: a.tool for a in d.actions} == {"add_item": "add_to_cart", "remove_item": "remove_from_cart"}
    assert "request_payment" not in [a.tool for a in d.actions]         # payment needs spoken consent
    for action in d.actions:
        assert action.say.startswith("(") and "{" in action.say and action.description
    voice = d.voice
    assert (voice.speculative, voice.barge_in, voice.interrupt_mode, voice.endpoint) == (False, True, "unmute_gate", "smart")
    assert d.avatar.enabled and d.avatar.image == "examples/girl.png" and (REPO / d.avatar.image).is_file()
    assert d.reply_length == "short" and "한 번에 한 가지" in d.persona and "화면" in d.persona

    flow = " ".join(rule.rule for rule in d.flow)
    for needle in ("add_to_cart", "remove_from_cart", "search_menu", "apply_coupon", "get_cart", "handoff_to_human",
                   "request_payment를 한 번 호출", "request_payment를 다시 호출", "품절", "꼭 골라야 하는 선택",
                   "도구 결과에 적힌 숫자만", "(화면에서"):
        assert needle in flow, needle
    assert any("직원" in rule.when for rule in d.handoff)
    assert any("동의하기 전에 결제" in text for text in d.prohibitions)
    assert any("가격" in text for text in d.prohibitions)
    for tool in (name for guard in d.guards for name in guard.unless_tools):
        assert tool in d.tools


def test_kiosk_guards(store):
    guards = {g.id: re.compile(g.pattern) for g in store.load(KIOSK, 1).guards}

    def hit(text):
        return sorted(name for name, pattern in guards.items() if pattern.search(text))

    assert hit("아이스 아메리카노 두 잔 담았어요. 더 필요하신 거 있으세요?") == ["no_fake_add"]
    assert hit("바닐라 라떼 한 잔 추가했습니다.") == ["no_fake_add"]
    assert hit("결제가 완료되었습니다. 주문 번호는 K-0012입니다.") == ["no_fake_payment"]
    assert hit("직원을 불러 드릴게요. 잠시만 기다려 주세요.") == ["no_fake_staff_call"]
    assert hit("카드 번호를 말씀해 주세요.") == ["no_spoken_card_number"]
    for fine in ("따뜻하게 드릴까요, 차갑게 드릴까요?", "아메리카노 2잔, 합계 9000원 결제를 진행할까요?",
                 "초코 케이크는 품절이에요. 치즈 케이크는 어떠세요?", "어떤 메뉴를 담아 드릴까요?",
                 "결제하지 않았어요. 무엇을 바꿀까요?"):
        assert hit(fine) == [], fine


def test_kiosk_evaluation(store):
    d = store.load(KIOSK, 1)
    criteria = {c.id: c for c in d.evaluation.success}
    for name in ("order_matches_request", "consent_before_payment", "no_invented_prices"):
        assert criteria[name].safety and criteria[name].judge == "llm", name
    assert "영수증" in criteria["order_matches_request"].rule and "터치" in criteria["order_matches_request"].rule
    assert criteria["paid"].metric == "payment_executed" and criteria["cart_used"].metric == "cart_changes"
    assert criteria["no_staff_needed"].metric == "handoff"
    fields = {f.name: f for f in d.evaluation.extract}
    assert fields["items_ordered"].type == "number"
    for name in ("paid", "used_touch", "handoff"):
        assert fields[name].type == "boolean", name
    assert not any(f.pii for f in d.evaluation.extract)


def test_kiosk_tests(store):
    from agentkit.eval.simulate import persona_touches
    d = store.load(KIOSK, 1)
    personas = {p.id: p for p in d.tests.personas}
    assert len(personas) >= 6 and len(d.tests.cases) >= 2
    for name in ("simple_order", "options_unsaid", "change_mind_remove", "recommend_sweet", "sold_out_item",
                 "coupon_user", "decline_at_payment", "touch_and_talk"):
        assert personas[name].goal and personas[name].expect, name
    assert "달달한" in personas["recommend_sweet"].opening
    assert personas["decline_at_payment"].expect["paid"] is False
    assert personas["decline_at_payment"].expect["declined_payment_not_charged"] is True
    assert personas["coupon_user"].facts["coupon_code"] in ("WELCOME10", "TEA1000")
    # touches name menu items, never ids; every touch maps to an action of the definition
    touches = persona_touches(personas["touch_and_talk"], d)
    assert touches and touches == personas["touch_and_talk"].facts["touch"]
    for touch in touches:
        assert re.search("[가-힣]", touch.get("item") or touch.get("line"))
    assert not [p.id for p in d.tests.personas if p.id != "touch_and_talk" and "touch" in p.facts]
    for item in [*d.tests.personas, *d.tests.cases]:
        assert "_" not in "".join(getattr(item, "user_turns", [])) + (getattr(item, "opening", "") or ""), item.id
    paying = [item for item in [*d.tests.personas, *d.tests.cases] if item.expect.get("paid")]
    assert len(paying) >= 5 and all(item.expect.get("consent_before_payment") for item in paying)
    for case in d.tests.cases:
        assert case.user_turns and case.expect


# ---- the three former skeletons: every tool they name has a contract ------------------------------

@pytest.mark.parametrize("agent,tools", [
    ("clinic-intake", ["find_patient", "list_available_slots", "book_appointment", "cancel_appointment", "handoff_to_human"]),
    ("drive-thru-order", ["search_menu", "get_cart", "add_to_cart", "remove_from_cart", "submit_order",
                          "handoff_to_human"]),
    ("flight-rebooking", ["verify_booking", "get_booking", "search_alternative_flights", "quote_change_fee", "change_flight",
                          "handoff_to_human"]),
])
def test_former_skeletons_are_complete(store, agent, tools):
    from agentkit.tools.loader import load_contracts
    contracts = load_contracts()
    from agentkit.tools import resolve
    d = store.load(agent, 1)
    assert d.tools == tools and len(d.tool_packs) == 2 and d.tool_packs[1] == "common"
    ids = {name: resolve(contracts, name, d.tool_packs) for name in d.tools}         # every name is in the agent's packs
    assert ids["handoff_to_human"] == "common.handoff_to_human" and ids[tools[0]] == f"{d.tool_packs[0]}.{tools[0]}"
    guards = {g.id: g for g in d.guards}
    assert "confirm_through_tool" in guards                      # the confirmation sentence comes from the write tool
    writes = [name for name in d.tools if contracts[ids[name]].confirm_template]
    assert writes and set(guards["confirm_through_tool"].unless_tools) == set(writes)
    assert len(d.tests.personas) >= 2


def test_the_emergency_handoff_fixes_what_is_said(store):
    rules = {rule.id: rule for rule in store.load("clinic-intake", 1).handoff}
    assert "119" in rules["emergency"].say and all(not rule.say for key, rule in rules.items() if key != "emergency")


def test_every_agent_names_its_tool_packs(store):
    from agentkit.tools import load_contracts, resolve
    contracts = load_contracts()
    for agent in store.list_agents():
        d = store.load(agent, 1)
        assert d.tool_packs, agent
        ids = [resolve(contracts, name, d.tool_packs) for name in d.tools]
        assert len({i.split(".")[-1] for i in ids}) == len(ids), agent          # no two tools of one agent share a name
    # the reason for packs: the cafe and the burger shop both have these, and each agent gets its own
    kiosk, drive = store.load(KIOSK, 1), store.load("drive-thru-order", 1)
    for name in ("search_menu", "get_cart", "add_to_cart", "remove_from_cart"):
        assert name in kiosk.tools and name in drive.tools
        assert resolve(contracts, name, kiosk.tool_packs) == "cafe." + name
        assert resolve(contracts, name, drive.tool_packs) == "burger." + name
