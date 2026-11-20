"""The cafe through its contracts and the gateway: what the model is told (text) and what the
customer is shown (display), per docs/DISPLAY.md. Offline."""
import pytest

from agentkit.dummy.cafe import get_cafe
from agentkit.dummy.shop import get_shop
from agentkit.tools import build_gateway, catalog
from agentkit.tools import load_contracts as load_all_contracts
from agentkit.tools.testing import FakeRecorder, Packed, make_ctx

PACKS = ["cafe", "common"]            # the kiosk agent's tool packs: bare names mean these


def load_contracts():
    return catalog(load_all_contracts(), PACKS)

CAFE = ["get_menu", "search_menu", "get_cart", "add_to_cart", "remove_from_cart", "apply_coupon", "request_payment"]
ICE = {"temperature": "ice"}


@pytest.fixture
def cafe():
    cafe = get_cafe()
    cafe.reset()
    get_shop().reset()
    yield cafe
    cafe.reset()
    get_shop().reset()


@pytest.fixture
def recorder():
    return FakeRecorder()


@pytest.fixture
def gateway(cafe, recorder):
    return Packed(build_gateway(recorder=recorder), PACKS)


def ctx(turn=1, text="", session="k1", **more):
    return make_ctx(session, turn, text, agent_id="kiosk-order", **more)


def kinds(result):
    return [(d["kind"], d["source"]) for d in result.display]


# ---- contracts ---------------------------------------------------------------------------------


def test_cafe_contracts_follow_the_table_of_the_spec():
    c = load_contracts()
    assert all(c[name].adapter.kind == "dummy" and c[name].adapter.target == f"cafe:{name}" for name in CAFE)
    assert [c[name].effect for name in CAFE] == ["read", "read", "read", "write", "write", "write", "write"]
    assert [(c[name].display.kind, c[name].display.path, c[name].display.refresh) for name in CAFE] == [
        ("menu", "", []), ("items", "", []), ("cart", "", []),
        ("cart", "cart", ["get_cart"]), ("cart", "cart", ["get_cart"]), ("cart", "cart", ["get_cart"]),
        ("receipt", "", ["get_cart"])]
    # only the tool that moves money asks first
    assert [bool(c[name].confirm_template) for name in CAFE] == [False] * 6 + [True]
    assert c["request_payment"].confirm_template == "{summary} 결제를 진행할까요?"
    assert c["get_menu"].input_schema.get("required", []) == [] and c["get_cart"].input_schema["properties"] == {}
    assert c["add_to_cart"].input_schema["required"] == ["item_id"]
    assert c["add_to_cart"].input_schema["properties"]["options"]["type"] == "object"
    assert c["remove_from_cart"].input_schema["required"] == ["line_id"]
    assert c["search_menu"].input_schema["required"] == ["query"] and c["apply_coupon"].input_schema["required"] == ["code"]
    for name in CAFE:
        assert c[name].requires == [] and c[name].description and c[name].filler, name
    assert c["handoff_to_human"].display is None


def test_specs_for_the_model_carry_the_option_ids(gateway):
    spec = gateway.specs(["add_to_cart"])[0]["function"]
    options = spec["parameters"]["properties"]["options"]
    assert options["properties"]["temperature"]["enum"] == ["hot", "ice"]
    assert options["properties"]["size"]["enum"] == ["regular", "large"]


# ---- read tools --------------------------------------------------------------------------------


async def test_get_menu_shows_the_menu_and_tells_the_model_the_ids(gateway):
    result = await gateway.call(ctx(0), "get_menu", {})
    assert result.ok and result.items == 21
    assert kinds(result) == [("menu", "get_menu")]
    assert result.display[0]["data"] == result.data and len(result.data["categories"]) == 4
    assert "- vanilla_latte 바닐라 라떼 5500원" in result.text


async def test_search_menu_shows_the_found_items(gateway):
    result = await gateway.call(ctx(), "search_menu", {"query": "달달한"})
    assert result.ok and kinds(result) == [("items", "search_menu")]
    shown = result.display[0]["data"]
    assert shown["query"] == "달달한" and "vanilla_latte" in [i["id"] for i in shown["items"]]
    assert result.items == len(shown["items"])
    nothing = await gateway.call(ctx(), "search_menu", {"query": "민트초코"})
    assert not nothing.ok and nothing.error_type == "not_found" and nothing.display == []


async def test_get_cart_shows_the_cart(gateway):
    result = await gateway.call(ctx(), "get_cart", {})
    assert result.ok and kinds(result) == [("cart", "get_cart")]
    assert result.display[0]["data"] == {"lines": [], "subtotal": 0, "discount": 0, "total": 0, "coupon": None,
                                         "coupon_name": None, "count": 0}


# ---- the cart: no confirmation, the screen is redrawn from get_cart ------------------------------


async def test_add_to_cart_runs_at_once_and_the_screen_shows_the_systems_cart(gateway, cafe, recorder):
    result = await gateway.call(ctx(), "add_to_cart", {"item_id": "americano", "quantity": 2, "options": ICE})
    assert result.ok and result.executed and not result.needs_confirmation
    assert result.text == "아메리카노(차갑게) 2잔을 담았습니다(줄 L1). 지금 주문은 모두 2개, 합계 9000원입니다."
    assert kinds(result) == [("cart", "get_cart")]              # one cart, the refreshed one
    assert result.display[0]["data"] == cafe.cart("k1")
    assert result.display[0]["data"]["lines"] == [{
        "line_id": "L1", "item_id": "americano", "name": "아메리카노", "options": ["차갑게"],
        "selected": {"temperature": "ice", "size": "regular", "shot": "none"},
        "quantity": 2, "unit_price": 4500, "line_total": 9000}]
    # the refresh is a tool call of its own in the trace, in the same session and turn
    spans = [s for s, _ in recorder.spans]
    assert [(s.name, s.turn, s.session_id) for s in spans] == [("get_cart", 1, "k1"), ("add_to_cart", 1, "k1")]
    refresh, call = spans
    assert refresh.attrs["refresh_of"] == "add_to_cart" and refresh.attrs["parent_call_id"] == result.call_id
    assert refresh.attrs["effect"] == "read" and "refresh_of" not in call.attrs
    assert call.output["display"] == [{"kind": "cart", "source": "get_cart"}]      # which screens, not their data
    assert call.attrs["executed"] is True and call.attrs["phase"] == "executed"


async def test_the_same_item_again_is_added_again_not_called_a_duplicate(gateway, cafe):
    arguments = {"item_id": "americano", "quantity": 1, "options": ICE}
    for expected in (1, 2, 3):                                  # "하나 더 주세요"
        result = await gateway.call(ctx(expected), "add_to_cart", arguments)
        assert result.ok and result.executed, result.text
        assert result.display[0]["data"]["lines"][0]["quantity"] == expected
    assert cafe.cart("k1")["total"] == 13500


async def test_lenient_inputs_reach_the_cafe_through_the_contract(gateway, cafe):
    by_name = await gateway.call(ctx(), "add_to_cart", {"item_id": "아이스 아메리카노", "quantity": "2"})
    assert by_name.ok and by_name.data["line"]["quantity"] == 2 and by_name.data["line"]["options"] == ["차갑게"]
    korean = await gateway.call(ctx(), "add_to_cart", {"item_id": "바닐라 라떼", "options": {"온도": "뜨겁게", "크기": "큰 거"}})
    assert korean.ok and korean.data["line"]["options"] == ["따뜻하게", "크게"]
    as_text = await gateway.call(ctx(), "add_to_cart", {"item_id": "earl_grey", "options": '{"temperature": "차갑게"}'})
    assert as_text.ok and as_text.data["line"]["options"] == ["차갑게"]
    whole_call_as_text = await gateway.call(ctx(), "add_to_cart", '{"item_id": "cheesecake", "quantity": "1"}')
    assert whole_call_as_text.ok
    assert [l["item_id"] for l in cafe.cart("k1")["lines"]] == ["americano", "vanilla_latte", "earl_grey", "cheesecake"]


async def test_failures_show_nothing_except_a_notice_the_system_sends(gateway, cafe):
    sold_out = await gateway.call(ctx(), "add_to_cart", {"item_id": "choco_cake"})
    assert (sold_out.ok, sold_out.error_type, sold_out.executed) == (False, "precondition_failed", False)
    assert sold_out.display == [{"kind": "notice", "source": "add_to_cart",
                                 "data": {"level": "info", "text": "초코 케이크는 품절입니다."}}]
    assert "치즈 케이크" in sold_out.text

    no_option = await gateway.call(ctx(), "add_to_cart", {"item_id": "americano"})
    assert no_option.error_type == "invalid_input" and no_option.display == [] and "차갑게(ice)" in no_option.text
    unknown = await gateway.call(ctx(), "add_to_cart", {"item_id": "프라푸치노"})
    assert unknown.error_type == "not_found" and unknown.display == []
    no_item = await gateway.call(ctx(), "add_to_cart", {"quantity": 1})        # refused by the contract's schema
    assert no_item.error_type == "invalid_input" and no_item.display == [] and "item_id" in no_item.text
    too_many = await gateway.call(ctx(), "add_to_cart", {"item_id": "cheesecake", "quantity": 50})
    assert too_many.error_type == "invalid_input" and too_many.display == []
    assert cafe.cart("k1")["lines"] == []


async def test_remove_and_coupon_redraw_the_cart(gateway, cafe):
    await gateway.call(ctx(1), "add_to_cart", {"item_id": "americano", "quantity": 2, "options": ICE})
    await gateway.call(ctx(1), "add_to_cart", {"item_id": "yuzu_tea", "options": {"temperature": "hot"}})
    removed = await gateway.call(ctx(2), "remove_from_cart", {"line_id": "L1", "quantity": 1})
    assert removed.ok and removed.executed and kinds(removed) == [("cart", "get_cart")]
    assert removed.text == "아메리카노(차갑게) 1잔을 뺐습니다(줄 L1에 1잔 남음). 지금 주문은 모두 2개, 합계 9800원입니다."
    assert [l["quantity"] for l in removed.display[0]["data"]["lines"]] == [1, 1]
    by_number = await gateway.call(ctx(3), "remove_from_cart", {"line_id": 1})  # the model sent a number
    assert by_number.ok and [l["line_id"] for l in by_number.display[0]["data"]["lines"]] == ["L2"]
    gone = await gateway.call(ctx(4), "remove_from_cart", {"line_id": "L1"})
    assert gone.error_type == "not_found" and gone.display == []

    coupon = await gateway.call(ctx(5), "apply_coupon", {"code": "tea1000"})
    assert coupon.ok and coupon.executed and kinds(coupon) == [("cart", "get_cart")]
    shown = coupon.display[0]["data"]
    assert (shown["subtotal"], shown["discount"], shown["total"], shown["coupon"]) == (5300, 1000, 4300, "TEA1000")
    wrong = await gateway.call(ctx(6), "apply_coupon", {"code": "FREE"})
    assert wrong.error_type == "not_found" and wrong.display == []
    # back to the first coupon after another one: a repeat of an earlier request, still carried out
    await gateway.call(ctx(7), "apply_coupon", {"code": "WELCOME10"})
    again = await gateway.call(ctx(8), "apply_coupon", {"code": "TEA1000"})
    assert again.executed and again.display[0]["data"]["coupon"] == "TEA1000"


async def test_cart_tools_do_not_run_on_a_speculative_turn(gateway, cafe):
    result = await gateway.call(ctx(speculative=True), "add_to_cart", {"item_id": "cheesecake"})
    assert result.error_type == "not_confirmed" and result.display == [] and cafe.cart("k1")["lines"] == []
    assert (await gateway.call(ctx(speculative=True), "get_menu", {})).ok


# ---- payment: confirm card, then receipt ---------------------------------------------------------


async def test_payment_proposes_with_a_confirm_card_then_executes_with_a_receipt(gateway, cafe, recorder):
    await gateway.call(ctx(1), "add_to_cart", {"item_id": "americano", "quantity": 2, "options": ICE})
    cart = cafe.cart("k1")
    proposal = await gateway.call(ctx(2, "결제할게요"), "request_payment", {})
    assert proposal.ok and proposal.needs_confirmation and not proposal.executed
    assert proposal.confirmation_text == "아이스 아메리카노 2잔, 합계 9000원 결제를 진행할까요?"
    assert kinds(proposal) == [("confirm", "request_payment")]
    card = proposal.display[0]["data"]
    assert card["text"] == proposal.confirmation_text and card["tool"] == "request_payment"
    assert card["lines"] == cart["lines"] and card["total"] == 9000
    assert cafe.cart("k1") == cart and cafe.records() == []      # nothing was paid, nothing redrawn
    assert [s.name for s, _ in recorder.spans][-1] == "request_payment" and len(recorder.spans) == 3

    same_turn = await gateway.call(ctx(2, "결제할게요"), "request_payment", {})
    assert same_turn.error_type == "not_confirmed" and same_turn.display == []

    done = await gateway.call(ctx(3, "네"), "request_payment", {})
    assert done.ok and done.executed and not done.needs_confirmation
    assert kinds(done) == [("receipt", "request_payment"), ("cart", "get_cart")]
    receipt = done.display[0]["data"]
    assert receipt == done.data and receipt["order_no"] == "K-0001" and receipt["total"] == 9000
    assert receipt["lines"] == cart["lines"] and receipt["message"] == "주문이 접수되었습니다. 번호가 불리면 받아 가세요."
    assert done.display[1]["data"]["lines"] == []                # the cart on screen is empty again
    assert "K-0001" in done.text and "9000원" in done.text
    assert [r["order_no"] for r in cafe.records("k1")] == ["K-0001"]


async def test_payment_is_not_executed_without_consent(gateway, cafe):
    await gateway.call(ctx(1), "add_to_cart", {"item_id": "cheesecake"})
    await gateway.call(ctx(2, "결제요"), "request_payment", {})
    unclear = await gateway.call(ctx(3, "얼마라고요?"), "request_payment", {})
    assert unclear.error_type == "not_confirmed" and unclear.display == [] and unclear.confirmation_text
    refused = await gateway.call(ctx(4, "아니요 잠깐만요"), "request_payment", {})
    assert refused.error_type == "not_confirmed" and refused.display == []
    assert cafe.records() == [] and cafe.cart("k1")["count"] == 1
    asked_again = await gateway.call(ctx(5, "네"), "request_payment", {})    # the refusal dropped the proposal
    assert asked_again.needs_confirmation and kinds(asked_again) == [("confirm", "request_payment")]


async def test_empty_cart_payment_is_refused_at_once_with_a_notice(gateway, cafe):
    result = await gateway.call(ctx(1, "결제할게요"), "request_payment", {})
    assert (result.ok, result.error_type, result.needs_confirmation) == (False, "precondition_failed", False)
    assert "비어 있어" in result.text
    assert kinds(result) == [("notice", "request_payment")] and result.display[0]["data"]["level"] == "info"
    assert cafe.records() == []


async def test_cart_changed_between_confirmation_and_consent_is_confirmed_again(gateway, cafe):
    await gateway.call(ctx(1), "add_to_cart", {"item_id": "americano", "options": ICE})
    await gateway.call(ctx(2, "결제할게요"), "request_payment", {})
    # "네, 치즈 케이크도 하나 주세요": the model adds the cake and then asks for the payment
    await gateway.call(ctx(3, "네, 치즈 케이크도 하나 주세요"), "add_to_cart", {"item_id": "cheesecake"})
    result = await gateway.call(ctx(3, "네, 치즈 케이크도 하나 주세요"), "request_payment", {})
    assert not result.ok and result.error_type == "precondition_failed" and result.data["reason"] == "cart_changed"
    assert result.display == [] and cafe.records() == []
    proposal = await gateway.call(ctx(3, "네, 치즈 케이크도 하나 주세요"), "request_payment", {})
    assert proposal.needs_confirmation and proposal.display[0]["data"]["total"] == 11000
    assert "치즈 케이크 1개" in proposal.confirmation_text
    done = await gateway.call(ctx(4, "네 맞아요"), "request_payment", {})
    assert done.executed and done.data["total"] == 11000


async def test_a_second_order_in_the_same_session_is_confirmed_and_paid_again(gateway, cafe):
    for turn, item in ((1, "cheesecake"), (4, "croissant")):
        await gateway.call(ctx(turn), "add_to_cart", {"item_id": item})
        assert (await gateway.call(ctx(turn + 1, "결제할게요"), "request_payment", {})).needs_confirmation
        done = await gateway.call(ctx(turn + 2, "네"), "request_payment", {})
        assert done.executed and kinds(done)[0] == ("receipt", "request_payment")
    assert [(r["order_no"], r["total"]) for r in cafe.records("k1")] == [("K-0001", 6500), ("K-0002", 4200)]


# ---- sessions, handoff ---------------------------------------------------------------------------


async def test_sessions_do_not_share_a_cart_and_end_session_forgets_it(gateway, cafe):
    await gateway.call(ctx(session="a"), "add_to_cart", {"item_id": "cheesecake", "quantity": 2})
    other = await gateway.call(ctx(session="b"), "add_to_cart", {"item_id": "croissant"})
    assert [l["item_id"] for l in other.display[0]["data"]["lines"]] == ["croissant"]
    assert (await gateway.call(ctx(session="c"), "get_cart", {})).data["lines"] == []
    assert cafe.cart("a")["count"] == 2
    gateway.end_session("a")
    assert (await gateway.call(ctx(session="a"), "get_cart", {})).data["lines"] == []
    assert cafe.cart("b")["count"] == 1


async def test_handoff_works_for_a_session_that_never_touched_the_shop(gateway, cafe):
    await gateway.call(ctx(1), "add_to_cart", {"item_id": "cheesecake"})
    result = await gateway.call(ctx(2, "직원 불러 주세요"), "handoff_to_human", {"reason": "고객이 직원을 찾음"})
    assert result.ok and result.executed and result.display == []
    assert result.data["ticket_id"].startswith("H") and result.data["order_id"] is None
    records = get_shop().records("k1", "handoff")
    assert len(records) == 1 and records[0]["reason"] == "고객이 직원을 찾음"
    again = await gateway.call(ctx(3), "handoff_to_human", {"reason": "다시"})
    assert again.ok and not again.executed
    assert cafe.cart("k1")["count"] == 1                        # the cart is untouched by the handoff
    gateway.end_session("k1")                                   # ends both dummy systems' sessions
    assert cafe.cart("k1")["count"] == 0


# ---- S12, the whole scenario ---------------------------------------------------------------------


async def test_s12_kiosk_order_from_menu_to_receipt(gateway, cafe, recorder):
    opening = await gateway.call(ctx(0), "get_menu", {})
    empty = await gateway.call(ctx(0), "get_cart", {})
    assert kinds(opening) + kinds(empty) == [("menu", "get_menu"), ("cart", "get_cart")]

    two = await gateway.call(ctx(1, "아이스 아메리카노 두 잔 주세요"), "add_to_cart",
                             {"item_id": "americano", "quantity": 2, "options": ICE})
    assert two.display[0]["data"]["total"] == 9000
    sweet = await gateway.call(ctx(2, "달달한 거 뭐 있어요?"), "search_menu", {"query": "달달한"})
    assert kinds(sweet) == [("items", "search_menu")]
    touched = await gateway.call(ctx(3, ""), "add_to_cart",                 # a touch on the screen
                                 {"item_id": "vanilla_latte", "quantity": 1, "options": {"temperature": "hot"}})
    assert touched.display[0]["data"]["total"] == 14500
    less = await gateway.call(ctx(4, "아메리카노 하나는 빼 주세요"), "remove_from_cart", {"line_id": "L1", "quantity": 1})
    assert less.display[0]["data"]["total"] == 10000
    sold_out = await gateway.call(ctx(5, "초코 케이크도 주세요"), "add_to_cart", {"item_id": "choco_cake"})
    assert kinds(sold_out) == [("notice", "add_to_cart")]

    proposal = await gateway.call(ctx(6, "결제할게요"), "request_payment", {})
    assert proposal.confirmation_text == "아이스 아메리카노 1잔, 따뜻한 바닐라 라떼 1잔, 합계 10000원 결제를 진행할까요?"
    done = await gateway.call(ctx(7, "네"), "request_payment", {})
    assert done.executed and done.display[0]["data"]["total"] == 10000
    assert [(l["name"], l["quantity"]) for l in done.display[0]["data"]["lines"]] == [("아메리카노", 1), ("바닐라 라떼", 1)]
    # every screen came from a tool call that is in the trace
    names = [s.name for s, _ in recorder.spans]
    assert names.count("get_cart") == 1 + 3 + 1 and names.count("request_payment") == 2
    assert all(s.kind == "tool" for s, _ in recorder.spans)
