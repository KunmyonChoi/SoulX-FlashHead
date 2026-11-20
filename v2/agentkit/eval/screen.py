"""What was on the customer's screen, as text (docs/DISPLAY.md).

Three readers need it: the judge (one compact line per screen, between the tool lines of a turn),
the simulated customer (the menu it orders from, the order it sees grow) and the touch convention
(a persona's `facts.touch` names menu items; the ids come from the menu and cart displays).

A display is {"kind", "data", "source"}; the payload shapes are those of docs/DISPLAY.md. Everything
here reads defensively: a payload from another system may lack any of the keys.
"""

SCREEN_KINDS = ("menu", "items", "cart", "confirm", "receipt", "notice")


def _norm(text):
    return "".join(str(text or "").lower().split())


def _won(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{value}원"
    return f"{int(number) if number == int(number) else number}원"


def _text(value):
    """An option or coupon as written on screen: a plain string, or the name of a {id, name} object."""
    if isinstance(value, dict):
        return str(value.get("name") or value.get("code") or value.get("id") or "")
    return "" if value is None else str(value)


def render_lines(lines):
    """Order lines on one line: "아메리카노(차갑게) 2개 9000원, 바닐라 라떼(따뜻하게) 1개 5500원"."""
    parts = []
    for line in lines or []:
        if not isinstance(line, dict):
            continue
        options = [_text(option) for option in line.get("options") or []]
        options = ", ".join(option for option in options if option)
        text = f"{line.get('name') or line.get('item_id') or '?'}{'(' + options + ')' if options else ''}"
        text += f" {line.get('quantity', 1)}개"
        if line.get("line_total") is not None:
            text += f" {_won(line['line_total'])}"
        parts.append(text)
    return ", ".join(parts) if parts else "(비어 있음)"


def _total(data):
    text = f"합계 {_won(data['total'])}" if data.get("total") is not None else ""
    if data.get("discount"):
        coupon = _text(data.get("coupon"))
        text += f" (할인 {_won(data['discount'])}{', 쿠폰 ' + coupon if coupon else ''} 적용됨)"
    return text


def _items(items):
    """Menu items with their prices on one line; sold-out items are marked."""
    parts = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("name") or item.get("id") or "?")
        if item.get("price") is not None:
            text += f" {_won(item['price'])}"
        if item.get("sold_out"):
            text += "(품절)"
        parts.append(text)
    return ", ".join(parts)


def _menu_items(data):
    return [item for category in (data or {}).get("categories") or [] if isinstance(category, dict)
            for item in category.get("items") or [] if isinstance(item, dict)]


def _surcharges(items):
    """Choices that change the price, once each: "크게 +500원"."""
    found = []
    for item in items:
        for option in item.get("options") or []:
            for choice in (option.get("choices") or []) if isinstance(option, dict) else []:
                if isinstance(choice, dict) and choice.get("price_delta"):
                    text = f"{choice.get('name') or choice.get('id')} {choice['price_delta']:+g}원"
                    if text not in found:
                        found.append(text)
    return found


def render_display(display):
    """One screen as one line for the judge's transcript; None for a kind this module does not know."""
    kind = display.get("kind")
    data = display.get("data")
    data = data if isinstance(data, dict) else {}
    if kind == "cart":
        return f"화면(주문 내역): {render_lines(data.get('lines'))} / {_total(data) or '합계 없음'}"
    if kind == "confirm":
        tool = f"{data['tool']} " if data.get("tool") else ""
        text = f"화면(확인 카드, {tool}아직 실행 전): \"{data.get('text', '')}\""
        if data.get("lines"):
            text += f" 내역: {render_lines(data['lines'])}"
        if data.get("total") is not None:
            text += f" / {_total(data)}"
        return text
    if kind == "receipt":
        text = "화면(영수증, 결제됨): "
        if data.get("order_no"):
            text += f"주문 번호 {data['order_no']}, "
        return text + f"내역: {render_lines(data.get('lines'))} / {_total(data) or '합계 없음'}"
    if kind == "menu":
        groups = []
        for category in data.get("categories") or []:
            if isinstance(category, dict) and category.get("items"):
                groups.append(f"{category.get('name') or category.get('id') or ''}: {_items(category['items'])}")
        text = "화면(메뉴): " + ("; ".join(groups) or "(비어 있음)")
        extra = _surcharges(_menu_items(data))
        return text + (f" / 추가 금액이 붙는 선택: {', '.join(extra)}" if extra else "")
    if kind == "items":
        query = f" \"{data['query']}\"" if data.get("query") else ""
        return f"화면(검색 결과{query}): {_items(data.get('items')) or '(없음)'}"
    if kind == "notice":
        return f"화면(안내): {data.get('text', '')}"
    return None


def display_events(spans):
    """Display spans as {turn, t_start, kind, data, source}, in the order they were shown."""
    events = []
    for span in spans or []:
        if span.get("kind") != "display":
            continue
        output, attrs = span.get("output"), span.get("attrs") or {}
        data = output.get("data") if isinstance(output, dict) and "data" in output else output
        events.append({"turn": span.get("turn", 0), "t_start": span.get("t_start") or 0.0,
                       "kind": span.get("name") or attrs.get("kind") or "", "data": data,
                       "source": attrs.get("source", "")})
    events.sort(key=lambda event: (event["turn"], event["t_start"]))
    return events


def action_events(spans):
    """Action spans (touches) as {turn, t_start, name, arguments, tool}."""
    events = []
    for span in spans or []:
        if span.get("kind") != "action":
            continue
        payload = span.get("input")
        arguments = payload.get("arguments") if isinstance(payload, dict) and "arguments" in payload else payload
        events.append({"turn": span.get("turn", 0), "t_start": span.get("t_start") or 0.0,
                       "name": span.get("name", ""), "arguments": arguments if isinstance(arguments, dict) else {},
                       "tool": (span.get("attrs") or {}).get("tool", "")})
    events.sort(key=lambda event: (event["turn"], event["t_start"]))
    return events


def render_action(event):
    arguments = ", ".join(f"{key}={value}" for key, value in (event.get("arguments") or {}).items())
    tool = f" → {event['tool']}" if event.get("tool") else ""
    return f"터치: 고객이 말하지 않고 화면에서 {event.get('name') or '?'}을(를) 눌렀다{tool}" + (
        f" ({arguments})" if arguments else "")


# ---- for the simulated customer ------------------------------------------------------------------

def menu_text(data):
    """The menu as the customer reads it: one item per line with price, description and choices."""
    lines = []
    for category in (data or {}).get("categories") or []:
        if not isinstance(category, dict):
            continue
        lines.append(f"[{category.get('name') or category.get('id') or ''}]")
        for item in category.get("items") or []:
            if not isinstance(item, dict):
                continue
            text = f"- {item.get('name') or item.get('id')}"
            if item.get("price") is not None:
                text += f" {_won(item['price'])}"
            if item.get("sold_out"):
                text += " (품절)"
            if item.get("description"):
                text += f": {item['description']}"
            options = []
            for option in item.get("options") or []:
                if not isinstance(option, dict):
                    continue
                choices = []
                for choice in option.get("choices") or []:
                    if isinstance(choice, dict):
                        delta = f" {choice['price_delta']:+g}원" if choice.get("price_delta") else ""
                        choices.append(f"{choice.get('name') or choice.get('id')}{delta}")
                options.append(f"{option.get('name') or option.get('id')}"
                               f"{'(꼭 골라야 함)' if option.get('required') else ''} {'/'.join(choices)}")
            if options:
                text += f" [선택: {'; '.join(options)}]"
            lines.append(text)
    return "\n".join(lines)


def latest(displays, kind):
    """The data of the last display of this kind, or None."""
    for display in reversed(list(displays or [])):
        if isinstance(display, dict) and display.get("kind") == kind:
            return display.get("data")
    return None


def find_item(menu, name):
    """The menu item called `name` (or with that id): exact first, then the closest containing name."""
    wanted = _norm(name)
    items = _menu_items(menu)
    for item in items:
        if wanted and wanted in (_norm(item.get("name")), _norm(item.get("id"))):
            return item
    near = [item for item in items if wanted and (wanted in _norm(item.get("name")) or _norm(item.get("name")) in wanted)]
    return min(near, key=lambda item: abs(len(_norm(item.get("name"))) - len(wanted))) if near else None


def _pick(entries, wanted):
    wanted = _norm(wanted)
    for entry in entries or []:
        if isinstance(entry, dict) and wanted and wanted in (_norm(entry.get("id")), _norm(entry.get("name"))):
            return entry
    return None


def resolve_touch(touch, menu=None, cart=None):
    """A persona's touch entry -> the arguments the screen would send with the action.

        {action, item: "바닐라 라떼", options: {"온도": "차갑게"}, quantity: 1}  -> {item_id, options, quantity}
        {action, line: "아메리카노"}                                             -> {line_id}
        {action, arguments: {...}}                                               -> those arguments as they are

    Names are looked up in the menu display (item, option, choice: by name or id) and in the latest
    cart display (line: by the item's name or id). A name that is not on the menu is sent as it is
    (the cafe accepts names); a line that is not in the cart raises LookupError, because a line
    that is not on screen cannot be touched.
    """
    arguments = dict(touch.get("arguments") or {})
    if touch.get("item") is not None:
        item = find_item(menu, touch["item"])
        arguments["item_id"] = item.get("id") if item and item.get("id") else touch["item"]
        if touch.get("options"):
            options = {}
            for key, value in touch["options"].items():
                option = _pick((item or {}).get("options"), key)
                choice = _pick((option or {}).get("choices"), value)
                options[option["id"] if option and option.get("id") else key] = (
                    choice["id"] if choice and choice.get("id") else value)
            arguments["options"] = options
    if touch.get("quantity") is not None:
        arguments["quantity"] = touch["quantity"]
    if touch.get("line") is not None:
        wanted = _norm(touch["line"])
        lines = [line for line in (cart or {}).get("lines") or [] if isinstance(line, dict)]
        match = [line for line in lines if wanted and wanted in (_norm(line.get("name")), _norm(line.get("item_id")))]
        match = match or [line for line in lines if wanted and wanted in _norm(line.get("name"))]
        if not match or not match[0].get("line_id"):
            raise LookupError(f"주문 내역에 '{touch['line']}' 줄이 없습니다")
        arguments["line_id"] = match[0]["line_id"]
    return arguments


def describe_touch(touch, description=""):
    """How a touch reads to the simulated customer: "바닐라 라떼 (온도 차갑게, 1개): 메뉴를 눌러 담는다"."""
    target = touch.get("item") or touch.get("line") or ""
    details = [f"{key} {value}" for key, value in (touch.get("options") or {}).items()]
    if touch.get("quantity") is not None:
        details.append(f"{touch['quantity']}개")
    text = f"{target}{' (' + ', '.join(details) + ')' if details else ''}".strip() or str(touch.get("action"))
    return f"{text}: {description or touch.get('action')}"
