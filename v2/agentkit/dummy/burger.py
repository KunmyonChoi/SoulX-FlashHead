"""
The dummy burger shop behind the drive-through agent: a menu and one order sheet per conversation.

- Burgers can be ordered alone or as a set (+2500원: fries and a drink; the drink must be named).
- Breakfast items are sold until 10:30. One item is sold out. One has no allergen sheet (the agent must
  hand over rather than guess).
- submit_order has a preview: the sentence read back is built from the sheet the shop actually holds.

Tool bodies for adapters of kind "python": `fn(arguments, ctx)` -> the wire dict; refusals are raised.
"""
from __future__ import annotations

import re

from .cafe import _session_id
from .common import now, ok, refuse, won

SET_EXTRA = 2500
LARGE_EXTRA = 700
BREAKFAST_UNTIL = "10:30"
LARGE_ORDER = 200000

# id, name, kind, price, allergens (None: no sheet), flags
MENU = [
    {"id": "bulgogi", "name": "불고기버거", "kind": "burger", "price": 5400, "allergens": ["밀", "대두", "소고기", "달걀"]},
    {"id": "cheese", "name": "치즈버거", "kind": "burger", "price": 4900, "allergens": ["밀", "우유", "소고기", "대두"]},
    {"id": "chicken", "name": "치킨버거", "kind": "burger", "price": 5900, "allergens": ["밀", "닭고기", "달걀", "대두"]},
    {"id": "shrimp", "name": "새우버거", "kind": "burger", "price": 5600, "allergens": ["밀", "새우", "달걀", "대두"]},
    {"id": "truffle", "name": "트러플머쉬룸버거", "kind": "burger", "price": 7900, "allergens": None},
    {"id": "egg_muffin", "name": "에그머핀", "kind": "burger", "price": 3900, "allergens": ["밀", "달걀", "우유"], "breakfast": True},
    {"id": "fries", "name": "감자튀김", "kind": "side", "price": 2500, "allergens": ["대두"], "sizes": True},
    {"id": "cheese_sticks", "name": "치즈스틱", "kind": "side", "price": 2900, "allergens": ["밀", "우유"]},
    {"id": "nuggets", "name": "치킨너겟", "kind": "side", "price": 3400, "allergens": ["밀", "닭고기"], "sold_out": True},
    {"id": "cola", "name": "콜라", "kind": "drink", "price": 2000, "allergens": [], "sizes": True},
    {"id": "cider", "name": "사이다", "kind": "drink", "price": 2000, "allergens": [], "sizes": True},
    {"id": "orange_juice", "name": "오렌지주스", "kind": "drink", "price": 2800, "allergens": []},
    {"id": "milkshake", "name": "밀크셰이크", "kind": "drink", "price": 3500, "allergens": ["우유"]},
    {"id": "ice_cream", "name": "아이스크림콘", "kind": "dessert", "price": 1500, "allergens": ["우유", "밀"]},
]
KINDS = {"burger": "버거", "side": "사이드", "drink": "음료", "dessert": "디저트"}
SET_DRINKS = ["cola", "cider"]             # included in a set; other drinks cost their difference
_CARTS = {}                                # session id -> [line]
ORDERS = []
_counter = [0]


def reset():
    _CARTS.clear()
    ORDERS.clear()
    _counter[0] = 0


def _key(text):
    return re.sub(r"[\s·\-_]+", "", str(text or "")).lower()


def _find(value, kind=None):
    key = _key(value)
    key = re.sub(r"(세트|단품|라지|큰거|큰것)$", "", key)
    if not key:
        return None
    pool = [m for m in MENU if kind is None or m["kind"] == kind]
    exact = [m for m in pool if key in (_key(m["id"]), _key(m["name"]))]
    if exact:
        return exact[0]
    near = [m for m in pool if key in _key(m["name"]) or _key(m["name"]) in key or _key(m["name"]).replace("버거", "") == key]
    return near[0] if len(near) == 1 else None


def _breakfast_open():
    return now().strftime("%H:%M") <= BREAKFAST_UNTIL


def _describe(item):
    parts = [f"{item['name']} {won(item['price'])}"]
    if item["kind"] == "burger" and not item.get("breakfast"):
        parts.append(f"세트 {won(item['price'] + SET_EXTRA)}")
    if item.get("sizes"):
        parts.append(f"라지 {won(item['price'] + LARGE_EXTRA)}")
    if item.get("sold_out"):
        parts.append("품절")
    if item.get("breakfast"):
        parts.append(f"아침 메뉴(오전 10시 30분까지, 지금은 {'주문 가능' if _breakfast_open() else '주문 불가'})")
    parts.append("알레르기 성분: " + ("성분표 없음(확인 불가, 직원 확인 필요)" if item["allergens"] is None
                                   else ", ".join(item["allergens"]) if item["allergens"] else "해당 없음"))
    return " / ".join(parts)


def search_menu(arguments, ctx):
    query = str(arguments.get("query") or "").strip()
    key = _key(query)
    kind = next((k for k, word in KINDS.items() if word in query or k == key), None)
    found = [m for m in MENU if key and (key in _key(m["name"]) or _key(m["name"]) in key or _key(m["name"]).replace("버거", "") in key)]
    if not found and kind:
        found = [m for m in MENU if m["kind"] == kind]
    if not found and (not key or any(w in query for w in ("메뉴", "전체", "뭐", "추천"))):
        found = list(MENU)
    if not found:
        return ok(f"'{query}'에 맞는 메뉴가 없습니다. 메뉴에 없다고 말하고, 있는 메뉴: " + ", ".join(m["name"] for m in MENU) + ".",
                  {"items": []}, items=0)
    return ok("\n".join(_describe(m) for m in found) + f"\n세트는 버거에 감자튀김과 음료(콜라 또는 사이다)가 붙고 {won(SET_EXTRA)}이 더해집니다.",
              {"items": [{k: m.get(k) for k in ("id", "name", "kind", "price", "allergens", "sold_out", "breakfast")} for m in found]},
              items=len(found))


def _line_text(line):
    text = line["name"]
    if line.get("set"):
        text += f" 세트(음료 {line['drink_name']})"
    if line.get("large"):
        text += " 라지"
    return f"{text} {line['quantity']}개"


def _cart_view(session_id):
    lines = _CARTS.get(session_id, [])
    total = sum(l["unit_price"] * l["quantity"] for l in lines)
    return {"lines": [dict(l, line_total=l["unit_price"] * l["quantity"]) for l in lines], "total": total,
            "summary": ", ".join(_line_text(l) for l in lines)}


def _cart_sentence(view):
    if not view["lines"]:
        return "주문서가 비어 있습니다."
    return f"지금 주문서: {view['summary']}. 합계 {won(view['total'])}."


def get_cart(arguments, ctx):
    view = _cart_view(_session_id(ctx))
    return ok(_cart_sentence(view), view)


def add_to_cart(arguments, ctx):
    session_id = _session_id(ctx)
    item = _find(arguments.get("item"))
    if item is None:
        refuse("not_found", f"'{arguments.get('item')}'은 메뉴에 없거나 여러 메뉴에 해당합니다. search_menu로 확인하고 고객에게 다시 물어보세요.")
    quantity = arguments.get("quantity") or 1
    if not isinstance(quantity, int) or not 1 <= quantity <= 30:
        refuse("invalid_input", "수량은 1에서 30 사이여야 합니다.")
    if item.get("sold_out"):
        refuse("precondition_failed", f"{item['name']}은 지금 품절입니다. 고객에게 알리고 다른 메뉴를 물어보세요.")
    if item.get("breakfast") and not _breakfast_open():
        refuse("precondition_failed", f"{item['name']}은 아침 메뉴라 오전 10시 30분까지만 주문할 수 있습니다. 다른 메뉴를 안내하세요.")
    as_set = arguments.get("set") is True or str(arguments.get("item") or "").replace(" ", "").endswith("세트")
    large = arguments.get("size") == "large"
    line = {"item_id": item["id"], "name": item["name"], "quantity": quantity, "unit_price": item["price"]}
    if as_set:
        if item["kind"] != "burger" or item.get("breakfast"):
            refuse("invalid_input", f"{item['name']}은 세트로 주문할 수 없습니다. 단품으로 담으세요.")
        if not arguments.get("drink"):
            refuse("invalid_input", "세트에 넣을 음료가 빠졌습니다. 고객에게 콜라와 사이다 중 무엇으로 할지 물은 뒤 drink를 넣어 다시 호출하세요.")
        drink = _find(arguments.get("drink"), "drink")
        if drink is None:
            refuse("not_found", f"음료 '{arguments.get('drink')}'을 찾지 못했습니다. 음료: " + ", ".join(m["name"] for m in MENU if m["kind"] == "drink") + ".")
        line.update(set=True, drink_id=drink["id"], drink_name=drink["name"],
                    unit_price=item["price"] + SET_EXTRA + max(0, drink["price"] - 2000))
    elif large:
        if not item.get("sizes"):
            refuse("invalid_input", f"{item['name']}은 크기를 고를 수 없습니다.")
        line.update(large=True, unit_price=item["price"] + LARGE_EXTRA)
    cart = _CARTS.setdefault(session_id, [])
    same = next((l for l in cart if all(l.get(k) == line.get(k) for k in ("item_id", "set", "drink_id", "large"))), None)
    if same:
        same["quantity"] += quantity
        line = same
    else:
        cart.append(line)
    view = _cart_view(session_id)
    return ok(f"담았습니다: {_line_text(dict(line, quantity=quantity))}. {_cart_sentence(view)}", {"added": quantity, "cart": view},
              executed=True, repeatable=True)


def remove_from_cart(arguments, ctx):
    session_id = _session_id(ctx)
    cart = _CARTS.get(session_id, [])
    item = _find(arguments.get("item"))
    lines = [l for l in cart if item is not None and l["item_id"] == item["id"]]
    if not lines:
        refuse("not_found", f"주문서에 '{arguments.get('item')}'이 없습니다. " + _cart_sentence(_cart_view(session_id)))
    line = lines[-1]
    quantity = arguments.get("quantity")
    if isinstance(quantity, int) and 0 < quantity < line["quantity"]:
        line["quantity"] -= quantity
        removed = quantity
    else:
        cart.remove(line)
        removed = line["quantity"]
    view = _cart_view(session_id)
    return ok(f"뺐습니다: {line['name']} {removed}개. {_cart_sentence(view)}", {"removed": removed, "cart": view},
              executed=True, repeatable=True)


def _ready_cart(ctx):
    view = _cart_view(_session_id(ctx))
    if not view["lines"]:
        refuse("precondition_failed", "주문서가 비어 있습니다. 먼저 메뉴를 담으세요.")
    if view["total"] > LARGE_ORDER:
        refuse("precondition_failed", f"합계가 {won(LARGE_ORDER)}을 넘는 주문은 직원이 받습니다. handoff_to_human으로 넘기세요.")
    return view


def preview_submit_order(arguments, ctx):
    view = _ready_cart(ctx)
    return ok("", {"summary": f"{view['summary']}, 합계 {won(view['total'])}입니다."})


def submit_order(arguments, ctx):
    session_id = _session_id(ctx)
    view = _ready_cart(ctx)
    _counter[0] += 1
    order = {"order_no": f"D-{_counter[0]:04d}", "session_id": session_id, **view}
    ORDERS.append(order)
    _CARTS.pop(session_id, None)
    return ok(f"주문이 확정되었습니다. 주문 번호 {order['order_no']}, {view['summary']}, 합계 {won(view['total'])}. "
              "다음 창구에서 결제하고 받아 가시라고 안내하세요.", order, executed=True)
