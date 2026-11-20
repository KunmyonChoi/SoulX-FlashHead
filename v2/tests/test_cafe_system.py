"""The dummy cafe on its own (agentkit/dummy/cafe.py): menu, search, lenient inputs, cart rules,
coupons, payment. No gateway here; tests/test_cafe_gateway.py goes through the contracts."""
import pytest

from agentkit.dummy.cafe import CATEGORIES, COUPONS, METHODS, CafeSystem


@pytest.fixture
def cafe():
    return CafeSystem()


def ctx(session="s1"):
    return {"session_id": session}


async def add(cafe, item, quantity=None, options=None, session="s1"):
    arguments = {"item_id": item}
    if quantity is not None:
        arguments["quantity"] = quantity
    if options is not None:
        arguments["options"] = options
    return await cafe.call("add_to_cart", arguments, ctx(session))


# ---- menu --------------------------------------------------------------------------------------


async def test_menu_has_four_categories_about_twenty_items_and_sold_out_ones(cafe):
    out = await cafe.call("get_menu", {}, ctx())
    categories = out["data"]["categories"]
    assert [(c["id"], c["name"]) for c in categories] == CATEGORIES
    assert [c["name"] for c in categories] == ["커피", "논커피", "차", "디저트"]
    items = [i for c in categories for i in c["items"]]
    assert 18 <= len(items) <= 24 and len({i["id"] for i in items}) == len(items)
    assert sorted(i["id"] for i in items if i["sold_out"]) == ["choco_cake", "mango_smoothie"]
    for item in items:
        assert item["price"] > 0 and item["price"] % 100 == 0 and item["description"] and item["tags"], item["id"]
    americano = categories[0]["items"][0]
    assert (americano["id"], americano["name"], americano["price"]) == ("americano", "아메리카노", 4500)
    temperature = americano["options"][0]
    assert temperature == {"id": "temperature", "name": "온도", "required": True, "default": None, "choices": [
        {"id": "hot", "name": "따뜻하게", "price_delta": 0}, {"id": "ice", "name": "차갑게", "price_delta": 0}]}
    assert [o["id"] for o in americano["options"]] == ["temperature", "size", "shot"]
    assert [i["options"] for i in categories[3]["items"]] == [[]] * len(categories[3]["items"])


async def test_menu_text_tells_the_model_ids_prices_options_and_sold_out_compactly(cafe):
    out = await cafe.call("get_menu", {}, ctx())
    text = out["text"]
    assert out["items"] == 21 and len(text) < 1600
    for part in ("[커피]", "[논커피]", "[차]", "[디저트]", "- americano 아메리카노 4500원 (온도 필수, 크기, 샷 추가)",
                 "- cold_brew 콜드브루 5000원 (차갑게만, 크기, 샷 추가)", "- choco_cake 초코 케이크 6800원 품절",
                 "temperature(hot 따뜻하게 / ice 차갑게)", "large 크게 +500원"):
        assert part in text, part
    assert "진한 에스프레소" not in text          # descriptions are for the screen, not for this listing


@pytest.mark.parametrize("query,expected,absent", [
    ("달달한 거 뭐 있어요", ["vanilla_latte", "choco_latte"], ["americano", "earl_grey"]),
    ("단 거", ["vanilla_latte"], ["americano"]),
    ("카페인 없는 음료", ["decaf_americano", "chamomile", "peppermint", "lemon_ade"], ["americano", "earl_grey"]),
    ("디카페인", ["decaf_americano"], ["americano"]),
    ("따뜻한 차", ["earl_grey", "chamomile", "yuzu_tea"], ["peach_iced_tea", "americano", "cheesecake"]),
    ("시원한 거", ["cold_brew", "lemon_ade", "peach_iced_tea"], ["cheesecake"]),
    ("라떼", ["cafe_latte", "vanilla_latte", "matcha_latte"], ["americano"]),
    ("아이스 아메리카노", ["americano", "decaf_americano"], ["cold_brew", "lemon_ade"]),
    ("케이크", ["cheesecake", "choco_cake"], ["croissant"]),
    ("디저트", ["cheesecake", "croissant", "macaron"], ["americano"]),
    ("상큼한", ["lemon_ade"], ["americano"]),
])
async def test_search_by_name_description_and_tags(cafe, query, expected, absent):
    out = await cafe.call("search_menu", {"query": query}, ctx())
    assert out["ok"], out["text"]
    found = [i["id"] for i in out["data"]["items"]]
    assert out["data"]["query"] == query and len(found) <= 8
    assert all(e in found for e in expected) and not any(a in found for a in absent), found
    assert out["text"].count("\n") == len(found) and "1. " in out["text"]      # a short numbered list


async def test_search_puts_sold_out_items_last_and_marks_them(cafe):
    out = await cafe.call("search_menu", {"query": "케이크"}, ctx())
    assert [(i["id"], i["sold_out"]) for i in out["data"]["items"]] == [("cheesecake", False), ("choco_cake", True)]
    assert "품절" in out["text"]


async def test_search_without_a_hit_or_a_query(cafe):
    nothing = await cafe.call("search_menu", {"query": "민트초코 프라푸치노"}, ctx())
    assert not nothing["ok"] and nothing["error_type"] == "not_found" and "달달한" in nothing["text"]
    empty = await cafe.call("search_menu", {"query": "  "}, ctx())
    assert not empty["ok"] and empty["error_type"] == "invalid_input"


# ---- add_to_cart -------------------------------------------------------------------------------


async def test_add_says_what_was_added_and_the_new_total_in_one_short_text(cafe):
    out = await add(cafe, "americano", 2, {"temperature": "ice"})
    assert out["ok"] and out["executed"] is True and out["repeatable"] is True
    assert out["text"] == "아메리카노(차갑게) 2잔을 담았습니다(줄 L1). 지금 주문은 모두 2개, 합계 9000원입니다."
    assert out["data"]["line"] == {
        "line_id": "L1", "item_id": "americano", "name": "아메리카노", "options": ["차갑게"],
        "selected": {"temperature": "ice", "size": "regular", "shot": "none"},
        "quantity": 2, "unit_price": 4500, "line_total": 9000}
    assert out["data"]["cart"] == {"lines": [out["data"]["line"]], "subtotal": 9000, "discount": 0, "total": 9000,
                                   "coupon": None, "coupon_name": None, "count": 2}
    dessert = await add(cafe, "cheesecake")
    assert dessert["text"] == "치즈 케이크 1개를 담았습니다(줄 L2). 지금 주문은 모두 3개, 합계 15500원입니다."


async def test_same_item_and_options_join_one_line_and_other_options_make_a_new_line(cafe):
    await add(cafe, "americano", 1, {"temperature": "ice"})
    again = await add(cafe, "americano", 2, {"temperature": "ice"})
    assert "이 메뉴는 이제 3잔" in again["text"] and again["data"]["added"] == 2
    await add(cafe, "americano", 1, {"temperature": "hot"})
    await add(cafe, "americano", 1, {"temperature": "ice", "size": "large", "shot": "one"})
    lines = cafe.cart("s1")["lines"]
    assert [(l["line_id"], l["options"], l["quantity"], l["unit_price"]) for l in lines] == [
        ("L1", ["차갑게"], 3, 4500), ("L2", ["따뜻하게"], 1, 4500), ("L3", ["차갑게", "크게", "1샷 추가"], 1, 5500)]
    assert cafe.cart("s1")["total"] == 3 * 4500 + 4500 + 5500


@pytest.mark.parametrize("item,options,expected_id,expected_options", [
    ("아메리카노", {"temperature": "ice"}, "americano", ["차갑게"]),           # Korean name instead of the id
    ("아이스 아메리카노", None, "americano", ["차갑게"]),                       # the option is in the name
    ("따뜻한 카페라떼", None, "cafe_latte", ["따뜻하게"]),
    ("아아", None, "americano", ["차갑게"]),
    ("Vanilla-Latte", {"temperature": "hot"}, "vanilla_latte", ["따뜻하게"]),
    ("vanilla latte", {"온도": "아이스"}, "vanilla_latte", ["차갑게"]),          # Korean option name and value
    ("americano", {"temperature": "차갑게"}, "americano", ["차갑게"]),
    ("americano", {"temperature": "뜨겁게"}, "americano", ["따뜻하게"]),
    ("americano", {"temperature": "ICED", "size": "큰 거"}, "americano", ["차갑게", "크게"]),
    ("americano", {"온도": "따뜻한 걸로", "사이즈": "라지", "샷": 2}, "americano", ["따뜻하게", "크게", "2샷 추가"]),
    ("americano", {"temperature": "hot", "shot": True}, "americano", ["따뜻하게", "1샷 추가"]),
    ("americano", {"temp": "hot", "size": "regular", "shot": "none"}, "americano", ["따뜻하게"]),
    ("americano", ["아이스", "큰 거"], "americano", ["차갑게", "크게"]),          # a list of words
    ("americano", "차갑게, 라지", "americano", ["차갑게", "크게"]),               # one string
    ("americano", '{"temperature": "ice"}', "americano", ["차갑게"]),           # JSON in a string
    ("americano", {"아이스": True}, "americano", ["차갑게"]),
    ("콜드브루", {"temperature": "ice"}, "cold_brew", []),                       # it is cold anyway
    ("치즈케이크", {"size": "regular"}, "cheesecake", []),                       # a default it does not have
    ("복숭아 아이스티", {"size": "large"}, "peach_iced_tea", ["크게"]),
    ("아이스티", None, "peach_iced_tea", []),
    ("초코칩", None, "choco_cookie", []),                                        # part of one name only
])
async def test_lenient_item_and_option_inputs(cafe, item, options, expected_id, expected_options):
    out = await add(cafe, item, None, options)
    assert out["ok"], out["text"]
    assert out["data"]["line"]["item_id"] == expected_id and out["data"]["line"]["options"] == expected_options


@pytest.mark.parametrize("quantity,expected", [(None, 1), (2, 2), ("2", 2), ("3잔", 3), ("두 잔", 2), ("한 개", 1), (2.0, 2)])
async def test_lenient_quantity(cafe, quantity, expected):
    out = await add(cafe, "cheesecake", quantity)
    assert out["ok"], out["text"]
    assert out["data"]["line"]["quantity"] == expected


@pytest.mark.parametrize("quantity", [0, -1, 21, "많이", 1.5, True])
async def test_bad_quantity_is_invalid_input_naming_the_range(cafe, quantity):
    out = await add(cafe, "cheesecake", quantity)
    assert not out["ok"] and out["error_type"] == "invalid_input" and "1부터 20" in out["text"]
    assert cafe.cart("s1")["lines"] == []


async def test_sold_out_is_refused_with_similar_items_and_a_notice(cafe):
    out = await add(cafe, "초코 케이크")
    assert not out["ok"] and out["error_type"] == "precondition_failed"
    assert out["text"].startswith("초코 케이크는 품절이라 담지 않았습니다.") and "치즈 케이크(cheesecake) 6500원" in out["text"]
    assert out["data"]["reason"] == "sold_out" and "cheesecake" in out["data"]["similar"]
    assert out["data"]["display_notice"] == {"level": "info", "text": "초코 케이크는 품절입니다."}
    assert "choco_cake" not in out["data"]["similar"] and cafe.cart("s1")["lines"] == []
    cafe.set_sold_out("choco_cake", False)
    assert (await add(cafe, "choco_cake"))["ok"]


async def test_missing_required_option_names_the_choices(cafe):
    out = await add(cafe, "americano", 2)
    assert not out["ok"] and out["error_type"] == "invalid_input"
    assert out["data"] == {"item_id": "americano", "missing_options": ["temperature"]}
    for part in ("아메리카노는 온도를 정해야", "따뜻하게(hot)", "차갑게(ice)", "아직 담지 않았습니다"):
        assert part in out["text"], out["text"]
    assert cafe.cart("s1")["lines"] == []


@pytest.mark.parametrize("item,options,parts", [
    ("americano", {"temperature": "미지근하게"}, ["따뜻하게(hot)", "차갑게(ice)"]),
    ("americano", {"temperature": "ice", "size": "초대형"}, ["기본(regular)", "크게(large)"]),
    ("americano", {"temperature": "ice", "whip": "많이"}, ["온도(temperature)", "크기(size)", "샷 추가(shot)"]),
    ("cold_brew", {"temperature": "hot"}, ["콜드브루는 차갑게만 됩니다", "크기(size)"]),
    ("earl_grey", {"temperature": "hot", "shot": "one"}, ["얼그레이에는 샷 추가 옵션이 없습니다"]),
    ("cheesecake", {"size": "large"}, ["치즈 케이크에는 크기 옵션이 없습니다", "고를 옵션이 없습니다"]),
    ("americano", 7, ["options는"]),
])
async def test_options_that_cannot_be_resolved_are_invalid_input_naming_what_is_possible(cafe, item, options, parts):
    out = await add(cafe, item, 1, options)
    assert not out["ok"] and out["error_type"] == "invalid_input", out
    for part in parts:
        assert part in out["text"], out["text"]
    assert cafe.cart("s1")["lines"] == []


async def test_unknown_and_ambiguous_items(cafe):
    unknown = await add(cafe, "민트초코 프라푸치노")
    assert not unknown["ok"] and unknown["error_type"] == "not_found"
    assert "커피, 논커피, 차, 디저트" in unknown["text"] and "get_menu" in unknown["text"]
    typo = await add(cafe, "아메리카누", options={"temperature": "ice"})
    assert typo["error_type"] == "not_found" and "아메리카노(americano)" in typo["text"]
    several = await add(cafe, "케이크")
    assert several["error_type"] == "invalid_input" and several["data"]["candidates"] == ["cheesecake", "choco_cake"]
    assert "치즈 케이크(cheesecake)" in several["text"]
    missing = await cafe.call("add_to_cart", {}, ctx())
    assert missing["error_type"] == "invalid_input"


# ---- remove_from_cart --------------------------------------------------------------------------


async def test_remove_a_line_or_part_of_it(cafe):
    await add(cafe, "americano", 3, {"temperature": "ice"})
    await add(cafe, "cheesecake", 1)
    part = await cafe.call("remove_from_cart", {"line_id": "L1", "quantity": 1}, ctx())
    assert part["ok"] and part["executed"] and part["repeatable"]
    assert part["text"] == "아메리카노(차갑게) 1잔을 뺐습니다(줄 L1에 2잔 남음). 지금 주문은 모두 3개, 합계 15500원입니다."
    assert part["data"]["removed"] == 1 and part["data"]["line"]["quantity"] == 2
    whole = await cafe.call("remove_from_cart", {"line_id": "L1"}, ctx())
    assert whole["data"]["removed"] == 2 and whole["data"]["line"] is None
    assert [l["line_id"] for l in cafe.cart("s1")["lines"]] == ["L2"]
    more_than_there_is = await cafe.call("remove_from_cart", {"line_id": "L2", "quantity": 5}, ctx())
    assert more_than_there_is["data"]["removed"] == 1
    assert more_than_there_is["text"] == "치즈 케이크 1개를 뺐습니다. 주문 내역이 비었습니다."


@pytest.mark.parametrize("line_id", ["L2", "l2", "2", 2, "L-2", "2번", "줄 2", "cheesecake", "치즈 케이크", "치즈케이크"])
async def test_lenient_line_id(cafe, line_id):
    await add(cafe, "americano", 1, {"temperature": "ice"})
    await add(cafe, "cheesecake", 1)
    out = await cafe.call("remove_from_cart", {"line_id": line_id}, ctx())
    assert out["ok"], out["text"]
    assert [l["item_id"] for l in cafe.cart("s1")["lines"]] == ["americano"]


async def test_remove_by_name_needs_one_matching_line(cafe):
    await add(cafe, "americano", 1, {"temperature": "ice"})
    await add(cafe, "americano", 1, {"temperature": "hot"})
    several = await cafe.call("remove_from_cart", {"line_id": "아메리카노"}, ctx())
    assert several["error_type"] == "invalid_input" and several["data"]["candidates"] == ["L1", "L2"]
    assert "L1 아메리카노(차갑게) 1잔" in several["text"]
    narrowed = await cafe.call("remove_from_cart", {"line_id": "따뜻한 아메리카노"}, ctx())
    assert narrowed["ok"] and [l["line_id"] for l in cafe.cart("s1")["lines"]] == ["L1"]


async def test_remove_what_is_not_there(cafe):
    empty = await cafe.call("remove_from_cart", {"line_id": "L1"}, ctx())
    assert empty["error_type"] == "not_found" and "비어 있어" in empty["text"]
    await add(cafe, "cheesecake", 1)
    for line_id in ("L9", "바닐라 라떼", "뭔가"):
        out = await cafe.call("remove_from_cart", {"line_id": line_id}, ctx())
        assert out["error_type"] == "not_found" and "L1 치즈 케이크 1개" in out["text"], out
    assert len(cafe.cart("s1")["lines"]) == 1


# ---- get_cart, coupons -------------------------------------------------------------------------


async def test_get_cart_empty_and_filled(cafe):
    empty = await cafe.call("get_cart", {}, ctx())
    assert empty["ok"] and empty["text"] == "주문 내역이 비어 있습니다."
    assert empty["data"] == {"lines": [], "subtotal": 0, "discount": 0, "total": 0, "coupon": None,
                             "coupon_name": None, "count": 0}
    await add(cafe, "americano", 2, {"temperature": "ice"})
    await add(cafe, "cheesecake", 1)
    filled = await cafe.call("get_cart", {}, ctx())
    assert filled["text"] == ("주문 내역 2줄, 합계 15500원\n1. [L1] 아메리카노(차갑게) 2잔 9000원\n"
                              "2. [L2] 치즈 케이크 1개 6500원")


async def test_coupon_rules(cafe):
    assert sorted(COUPONS) == ["TEA1000", "WELCOME10"]
    await add(cafe, "americano", 2, {"temperature": "ice"})
    unknown = await cafe.call("apply_coupon", {"code": "FREE100"}, ctx())
    assert unknown["error_type"] == "not_found" and "WELCOME10" not in unknown["text"]
    no_tea = await cafe.call("apply_coupon", {"code": "TEA1000"}, ctx())
    assert no_tea["error_type"] == "precondition_failed" and no_tea["data"]["reason"] == "no_tea"
    assert cafe.cart("s1")["coupon"] is None

    ten = await cafe.call("apply_coupon", {"code": " welcome-10 "}, ctx())
    assert ten["ok"] and ten["executed"] and ten["repeatable"]
    assert ten["text"] == "WELCOME10 쿠폰(전체 10% 할인)을 적용했습니다. 할인 900원, 합계 8100원입니다."
    cart = ten["data"]["cart"]
    assert (cart["subtotal"], cart["discount"], cart["total"], cart["coupon"]) == (9000, 900, 8100, "WELCOME10")
    await add(cafe, "cheesecake", 1)                    # the discount follows the cart
    assert cafe.cart("s1")["discount"] == 1550 and cafe.cart("s1")["total"] == 13950

    await add(cafe, "yuzu_tea", 1, {"temperature": "hot"})
    tea = await cafe.call("apply_coupon", {"code": "TEA1000"}, ctx())
    assert tea["ok"] and tea["data"]["replaced"] == "WELCOME10" and "WELCOME10 대신" in tea["text"]
    cart = cafe.cart("s1")
    assert (cart["subtotal"], cart["discount"], cart["total"], cart["coupon"]) == (20800, 1000, 19800, "TEA1000")
    await cafe.call("remove_from_cart", {"line_id": "yuzu_tea"}, ctx())     # no tea left: nothing to take off
    assert cafe.cart("s1")["discount"] == 0 and cafe.cart("s1")["total"] == 15500

    missing = await cafe.call("apply_coupon", {}, ctx())
    assert missing["error_type"] == "invalid_input"


async def test_coupon_on_an_empty_cart_waits_for_items(cafe):
    out = await cafe.call("apply_coupon", {"code": "WELCOME10"}, ctx())
    assert out["ok"] and "메뉴를 담으면 할인됩니다" in out["text"]
    await add(cafe, "cheesecake", 2)
    assert cafe.cart("s1")["discount"] == 1300


# ---- payment -----------------------------------------------------------------------------------


async def test_only_request_payment_has_a_preview(cafe):
    for method in METHODS:
        if method != "request_payment":
            assert await cafe.preview(method, {}, ctx()) is None


async def test_empty_cart_cannot_be_paid(cafe):
    for out in (await cafe.preview("request_payment", {}, ctx()), await cafe.call("request_payment", {}, ctx())):
        assert not out["ok"] and out["error_type"] == "precondition_failed" and out["data"]["reason"] == "empty_cart"
        assert out["data"]["display_notice"]["text"].startswith("주문 내역이 비어 있습니다")
    assert cafe.records() == []


async def test_preview_gives_lines_total_and_a_one_line_summary(cafe):
    await add(cafe, "americano", 2, {"temperature": "ice"})
    preview = await cafe.preview("request_payment", {}, ctx())
    assert preview["ok"] and preview["data"]["summary"] == "아이스 아메리카노 2잔, 합계 9000원"
    assert preview["data"]["total"] == 9000 and preview["data"]["lines"] == cafe.cart("s1")["lines"]
    await add(cafe, "vanilla_latte", 1, {"temperature": "hot", "size": "large", "shot": "one"})
    await add(cafe, "cheesecake", 1)
    await cafe.call("apply_coupon", {"code": "WELCOME10"}, ctx())
    preview = await cafe.preview("request_payment", {}, ctx())
    assert preview["data"]["summary"] == ("아이스 아메리카노 2잔, 따뜻한 바닐라 라떼 큰 사이즈 샷 추가 1잔, 치즈 케이크 1개, "
                                          "쿠폰 할인 2200원을 빼고 합계 19800원")
    assert cafe.cart("s1")["count"] == 4 and cafe.records() == []        # nothing was paid, nothing changed


async def test_summary_of_a_long_order_stays_one_line(cafe):
    for item in ("cheesecake", "croissant", "choco_cookie", "macaron"):
        await add(cafe, item, 1)
    await add(cafe, "lemon_ade", 1)
    await add(cafe, "cold_brew", 1)
    summary = (await cafe.preview("request_payment", {}, ctx()))["data"]["summary"]
    assert summary == "치즈 케이크 1개, 크루아상 1개, 초코칩 쿠키 1개, 마카롱 1개 외 2건, 합계 27700원"


async def test_payment_gives_an_order_number_a_receipt_and_empties_the_cart(cafe):
    await add(cafe, "americano", 2, {"temperature": "ice"})
    await cafe.call("apply_coupon", {"code": "WELCOME10"}, ctx())
    lines = cafe.cart("s1")["lines"]
    await cafe.preview("request_payment", {}, ctx())
    out = await cafe.call("request_payment", {}, ctx())
    assert out["ok"] and out["executed"] is True and out["repeatable"] is True
    assert out["data"] == {"order_no": "K-0001", "lines": lines, "subtotal": 9000, "discount": 900, "total": 8100,
                           "coupon": "WELCOME10", "message": "주문이 접수되었습니다. 번호가 불리면 받아 가세요."}
    assert "K-0001" in out["text"] and "8100원" in out["text"]
    assert cafe.cart("s1") == {"lines": [], "subtotal": 0, "discount": 0, "total": 0, "coupon": None,
                               "coupon_name": None, "count": 0}
    record = cafe.records("s1")[0]
    assert record["order_no"] == "K-0001" and record["session_id"] == "s1" and record["total"] == 8100

    await add(cafe, "cheesecake", 1)                    # the next order of the same customer
    assert cafe.cart("s1")["lines"][0]["line_id"] == "L1"
    second = await cafe.call("request_payment", {}, ctx())
    assert second["data"]["order_no"] == "K-0002" and second["data"]["total"] == 6500
    assert [r["order_no"] for r in cafe.records()] == ["K-0001", "K-0002"]


async def test_cart_changed_after_the_confirmation_is_not_paid(cafe):
    await add(cafe, "americano", 1, {"temperature": "ice"})
    await cafe.preview("request_payment", {}, ctx())
    await add(cafe, "cheesecake", 1)                    # the customer heard 4500 won, the cart says 11000
    out = await cafe.call("request_payment", {}, ctx())
    assert not out["ok"] and out["error_type"] == "precondition_failed" and out["data"]["reason"] == "cart_changed"
    assert cafe.records() == [] and cafe.cart("s1")["total"] == 11000
    await cafe.preview("request_payment", {}, ctx())    # confirmed again with the new cart
    assert (await cafe.call("request_payment", {}, ctx()))["data"]["total"] == 11000


# ---- sessions ----------------------------------------------------------------------------------


async def test_carts_are_per_session(cafe):
    await add(cafe, "americano", 2, {"temperature": "ice"}, session="a")
    await cafe.call("apply_coupon", {"code": "WELCOME10"}, ctx("a"))
    await add(cafe, "cheesecake", 1, session="b")
    assert [l["item_id"] for l in cafe.cart("a")["lines"]] == ["americano"] and cafe.cart("a")["coupon"] == "WELCOME10"
    assert [l["item_id"] for l in cafe.cart("b")["lines"]] == ["cheesecake"] and cafe.cart("b")["coupon"] is None
    assert cafe.cart("b")["lines"][0]["line_id"] == "L1"        # line numbers are the session's own
    assert (await cafe.call("get_cart", {}, ctx("c")))["data"]["lines"] == []
    paid = await cafe.call("request_payment", {}, ctx("b"))
    assert paid["data"]["total"] == 6500 and cafe.cart("a")["total"] == 8100
    cafe.end_session("a")
    assert cafe.cart("a")["lines"] == [] and [r["session_id"] for r in cafe.records()] == ["b"]
    cafe.reset()
    assert cafe.records() == []


async def test_unknown_method_and_no_context(cafe):
    assert (await cafe.call("delete_menu", {}, ctx()))["error_type"] == "invalid_input"
    assert (await cafe.call("get_cart", None))["ok"]
    assert (await add(cafe, "cheesecake", session="x"))["ok"] and cafe.cart("default")["lines"] == []


async def test_returned_data_is_a_copy(cafe):
    menu = (await cafe.call("get_menu", {}, ctx()))["data"]
    menu["categories"][0]["items"][0]["price"] = 1
    assert (await cafe.call("get_menu", {}, ctx()))["data"]["categories"][0]["items"][0]["price"] == 4500
    out = await add(cafe, "cheesecake", 1)
    out["data"]["cart"]["lines"].clear()
    assert len(cafe.cart("s1")["lines"]) == 1
