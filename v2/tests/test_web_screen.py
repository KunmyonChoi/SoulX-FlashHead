"""Screen agents (docs/DISPLAY.md): the kiosk layout of the conversation page, outline agents, and the
display and action spans in the monitor. Against the mock server's `kiosk-order` agent and `s-kiosk` session."""
import json
import urllib.parse

from test_web_support import focus_is_visible, no_sideways_scroll, open_page, post, reset, shot

OPENING = "어서 오세요. 화면의 메뉴를 보시고 말씀하시거나 눌러서 고르시면 됩니다."


def choose(page, agent="kiosk-order"):
    page.wait_for_selector("#agent:not([disabled])")
    page.select_option("#agent", agent)
    page.wait_for_function("definition !== null && definition.id === '%s' && document.getElementById('chat-log').querySelector('.stateview:not(.loading)') !== null" % agent)


def frames(page):
    """JSON messages the page sends over /ws from now on."""
    sent = []
    page.on("websocket", lambda ws: ws.on("framesent", lambda payload: sent.append(json.loads(payload))
                                          if isinstance(payload, str) and payload.startswith("{") else None))
    return sent


def start(page, agent="kiosk-order"):
    choose(page, agent)
    page.click("#toggle")
    page.wait_for_selector("#text:not([disabled])")


def opened(page):
    """The opening has arrived: the menu is on screen and the first words are in the chat."""
    page.wait_for_selector("#menu-list .item")
    page.wait_for_function("(() => { const b = document.querySelector('.msg.assistant'); return b && b.textContent === %s; })()" % json.dumps(OPENING))


def say(page, text):
    page.fill("#text", text)
    page.press("#text", "Enter")


def replied(page, part):
    page.wait_for_function("[...document.querySelectorAll('.msg.assistant')].pop().textContent.includes(%s)" % json.dumps(part))


def actions(sent):
    return [m for m in sent if m.get("type") == "action"]


def box(page, selector):
    return page.evaluate("s => { const r = document.querySelector(s).getBoundingClientRect(); return {x: r.left, y: r.top, w: r.width, h: r.height, r: r.right, b: r.bottom}; }", selector)


# ---- layout and the opening ----------------------------------------------------------------------

def test_kiosk_layout_opening_menu_and_empty_order():
    reset()
    with open_page("/") as page:
        choose(page)
        # as soon as the agent is chosen: where the menu and the order will be, backstage out of the way
        assert page.locator("#menu-panel").is_visible() and page.locator("#order-panel").is_visible()
        assert "대화를 시작하면 메뉴가 여기에 나타납니다" in page.inner_text("#menu-list")
        assert page.locator(".backstage").is_hidden() and page.locator("#open-backstage").is_visible()
        shot(page, "kiosk_idle", full=False)

        page.click("#toggle")
        page.wait_for_selector("#text:not([disabled])")
        opened(page)
        # the opening words are an ordinary agent bubble; the menu came from the display payload
        assert page.locator(".msg.assistant").count() == 1 and page.locator(".msg.user").count() == 0
        assert page.locator("#menu-cats button").all_inner_texts() == ["전체", "커피", "논커피", "차", "디저트"]
        assert page.locator("#menu-list .item").count() == 13 and page.locator("#menu-list .menu-cat").count() == 4
        card = page.inner_text("#menu-list .item[data-item=americano]")
        assert "아메리카노" in card and "4,500원" in card and "진한 에스프레소에 물을 더한 커피" in card
        assert "아직 담은 메뉴가 없습니다" in page.inner_text("#order-lines") and page.inner_text("#order-total") == "0원"

        # the menu is the main area, the order beside it, the conversation above the order; nothing scrolls the page
        menu, order, talk = box(page, "#menu-panel"), box(page, "#order-panel"), box(page, ".onair")
        assert menu["w"] > 2 * order["w"] and menu["r"] <= order["x"] and abs(talk["x"] - order["x"]) < 1 and talk["b"] <= order["y"], (menu, order, talk)
        assert order["b"] <= 900 and menu["b"] <= 900 and page.evaluate("document.documentElement.scrollHeight") <= 900
        total = box(page, "#order-total")
        assert 0 < total["y"] and total["b"] <= 900                      # the total is on screen
        # made for a finger: large targets, prices easy to read
        item = box(page, "#menu-list .item[data-item=americano]")
        assert item["h"] >= 100 and item["w"] >= 180, item
        assert page.evaluate("parseFloat(getComputedStyle(document.querySelector('.item .pr')).fontSize)") >= 18
        assert page.evaluate("parseFloat(getComputedStyle(document.getElementById('order-total')).fontSize)") >= 26
        assert min(page.evaluate("[...document.querySelectorAll('#menu-cats button')].map(b => b.getBoundingClientRect().height)")) >= 44
        # developer text is not in the customer's view: tool lines are hidden, the backstage is closed
        assert page.locator("#chat-log .msg.tool").count() >= 1 and page.locator("#chat-log .msg.tool:visible").count() == 0
        assert page.locator(".backstage").is_hidden() and page.locator("#trace").is_hidden()
        no_sideways_scroll(page)
        shot(page, "kiosk_opening", full=False)
        focus_is_visible(page)


def test_spoken_order_fills_the_order_from_the_cart_display():
    reset()
    with open_page("/") as page:
        start(page)
        opened(page)
        say(page, "아이스 아메리카노 두 잔 주세요")
        page.wait_for_selector("#order-lines .oline")
        line = page.inner_text("#order-lines .oline")
        for part in ("아메리카노", "차갑게", "2개", "9,000원", "빼기"):
            assert part in line, (part, line)
        assert page.inner_text("#order-total") == "9,000원" and page.inner_text("#order-count") == "2개"
        replied(page, "담았어요")
        say(page, "쿠폰 있어요")                                          # subtotal, discount, total
        page.wait_for_selector("#order-sums .discount")
        sums = page.inner_text("#order-sums")
        assert "주문 금액" in sums and "9,000원" in sums and "할인 (첫 방문 10% 할인)" in sums and "900원" in sums
        assert page.inner_text("#order-total") == "8,100원"
        assert "WELCOME10 쿠폰으로 10% 할인되었습니다." in page.inner_text("#kiosk-notes")      # a notice display
        replied(page, "쿠폰")
        say(page, "아메리카노 하나는 빼 주세요")
        page.wait_for_function("document.querySelector('#order-lines .oline .qty').textContent === '1개'")
        assert page.inner_text("#order-total") == "4,050원"
        shot(page, "kiosk_order_discount", full=False)


def test_the_screen_ignores_what_the_agent_says():
    """The order is drawn from display payloads only: an agent sentence that names a price changes nothing."""
    reset()
    with open_page("/") as page:
        start(page)
        opened(page)
        say(page, "카페 라떼 하나요")                                     # the agent asks back and calls no tool
        replied(page, "따뜻하게 드릴까요")
        assert "아직 담은 메뉴가 없습니다" in page.inner_text("#order-lines") and page.inner_text("#order-total") == "0원"


# ---- highlight -------------------------------------------------------------------------------------

def test_found_items_are_highlighted_until_the_next_customer_turn():
    reset()
    with open_page("/", height=700) as page:
        start(page)
        opened(page)
        say(page, "달달한 거 뭐 있어요?")
        page.wait_for_selector("#menu-list .item.hl")
        marked = page.evaluate("[...document.querySelectorAll('#menu-list .item.hl')].map(n => n.dataset.item)")
        assert marked == ["vanilla_latte", "choco_latte", "strawberry_ade", "cheesecake", "choco_cake", "cookie"], marked
        assert "“달달한”에 맞는 메뉴 6개를 표시했습니다." in page.inner_text("#kiosk-notes .found")
        assert page.locator("#menu-list .item.hl .tag.found").count() == 5      # the sold-out one keeps its own tag
        # the first one is scrolled into view inside the menu area
        page.wait_for_function("""(() => { const a = document.querySelector('.item[data-item=vanilla_latte]').getBoundingClientRect(),
            b = document.getElementById('menu-scroll').getBoundingClientRect(); return a.top >= b.top - 1 && a.bottom <= b.bottom + 1; })()""")
        colour = page.evaluate("getComputedStyle(document.querySelector('.item.hl')).borderTopColor")
        assert colour == "rgb(90, 214, 200)", colour                        # teal: a result that came back
        replied(page, "화면에 표시해 드렸습니다")
        shot(page, "kiosk_highlight", full=False)
        say(page, "카페 라떼 따뜻하게 하나요")                             # the next customer turn clears the marks
        page.wait_for_function("document.querySelectorAll('#menu-list .item.hl, #kiosk-notes .found, .tag.found').length === 0")
        page.wait_for_selector("#order-lines .oline")


def test_a_touch_also_ends_the_highlight():
    reset()
    with open_page("/") as page:
        start(page)
        opened(page)
        say(page, "추천해 주세요")
        page.wait_for_selector("#menu-list .item.hl")
        replied(page, "표시해 드렸습니다")
        page.click(".item[data-item=cookie]")
        page.wait_for_function("document.querySelectorAll('#menu-list .item.hl').length === 0")
        page.wait_for_selector("#order-lines .oline")


# ---- touch ------------------------------------------------------------------------------------------

def test_item_with_a_required_option_asks_first_then_sends_add_item():
    reset()
    with open_page("/") as page:
        sent = frames(page)
        start(page)
        opened(page)
        page.click(".item[data-item=vanilla_latte]")
        page.wait_for_selector("#picker:not([hidden])")
        assert not actions(sent)                                           # nothing is sent before the choice is made
        text = page.inner_text("#picker")
        for part in ("바닐라 라떼", "5,500원", "온도", "꼭 골라 주세요", "따뜻하게", "차갑게", "크기", "원하면 고르세요", "기본", "+500원", "수량"):
            assert part in text, part
        assert page.locator("#picker-add").is_disabled() and "먼저 골라 주세요" in page.inner_text("#picker-add")
        # the required choice is left to the customer; the optional one starts at its default
        assert page.locator("#picker [data-option=temperature] [aria-pressed=true]").count() == 0
        assert page.get_attribute("#picker [data-option=size] [aria-pressed=true]", "data-choice") == "regular"
        assert min(page.evaluate("[...document.querySelectorAll('#picker button')].map(b => b.getBoundingClientRect().height)")) >= 48
        shot(page, "kiosk_picker", full=False)
        page.click("#picker [data-option=temperature] [data-choice=hot]")
        assert page.inner_text("#picker-add") == "담기 · 5,500원" and page.locator("#picker-add").is_enabled()
        page.click("#picker [data-option=size] [data-choice=large]")
        assert page.inner_text("#picker-add") == "담기 · 6,000원"
        page.click("#picker [aria-label='하나 늘리기']")
        assert page.inner_text("#picker-qty") == "2개" and page.inner_text("#picker-add") == "담기 · 12,000원"
        page.click("#picker-add")
        page.wait_for_selector("#order-lines .oline")
        assert page.locator("#picker").is_hidden() and page.locator("#kscrim").is_hidden()
        act = actions(sent)
        assert len(act) == 1 and act[0]["name"] == "add_item" and isinstance(act[0]["id"], int), act
        assert act[0]["arguments"] == {"item_id": "vanilla_latte", "quantity": 2, "options": {"temperature": "hot", "size": "large"}}, act
        line = page.inner_text("#order-lines .oline")
        assert "바닐라 라떼" in line and "따뜻하게 · 크게" in line and "2개" in line and "12,000원" in line
        assert page.inner_text("#order-total") == "12,000원"
        assert page.inner_text(".msg.user.touch") == "화면에서 누름: 바닐라 라떼 (따뜻하게, 크게) 2개 담기"
        replied(page, "바닐라 라떼 담았어요")
        shot(page, "kiosk_touch_added", full=False)

        page.click(".item[data-item=americano]")                           # cancel and Escape send nothing
        page.wait_for_selector("#picker:not([hidden])")
        page.click("#picker >> text=취소")
        page.click(".item[data-item=americano]")
        page.keyboard.press("Escape")
        assert page.locator("#picker").is_hidden() and len(actions(sent)) == 1


def test_item_without_required_options_is_added_at_once_and_a_line_can_be_removed():
    reset()
    with open_page("/") as page:
        sent = frames(page)
        start(page)
        opened(page)
        page.click(".item[data-item=cookie]")
        page.wait_for_selector("#order-lines .oline")
        assert page.locator("#picker").is_hidden()
        assert actions(sent)[0]["arguments"] == {"item_id": "cookie", "quantity": 1, "options": {}}
        replied(page, "초코칩 쿠키 담았어요")
        page.click(".item[data-item=cookie]")                              # the same again: the display says two
        page.wait_for_function("document.querySelector('#order-lines .oline .qty').textContent === '2개'")
        replied(page, "초코칩 쿠키 담았어요")
        line_id = page.get_attribute("#order-lines .oline", "data-line")
        page.click("#order-lines .oline .rm")
        page.wait_for_function("document.querySelectorAll('#order-lines .oline').length === 0")
        assert actions(sent)[-1] == {"type": "action", "id": actions(sent)[-1]["id"], "name": "remove_item", "arguments": {"line_id": line_id}}
        assert page.inner_text("#order-total") == "0원" and "초코칩 쿠키 빼기" in page.inner_text("#chat-log")
        assert box(page, "#order-lines") and page.locator("#order-lines .order-empty").count() == 1


def test_sold_out_items_are_dimmed_and_cannot_be_pressed():
    reset()
    with open_page("/") as page:
        sent = frames(page)
        start(page)
        opened(page)
        assert page.evaluate("[...document.querySelectorAll('#menu-list .item.sold')].map(n => n.dataset.item)") == ["mango_smoothie", "choco_cake"]
        sold = page.locator(".item[data-item=choco_cake]")
        assert sold.is_disabled() and "품절" in sold.inner_text()
        assert float(page.evaluate("getComputedStyle(document.querySelector('.item.sold')).opacity")) < 0.6
        assert page.evaluate("getComputedStyle(document.querySelector('.item.sold'), '::after').content") == "none"     # no "+" on it
        sold.click(force=True)
        page.wait_for_timeout(300)
        assert not actions(sent) and page.locator("#picker").is_hidden()
        say(page, "초코 케이크 하나 주세요")                                # asked for by voice: refused, with a short-lived notice
        page.wait_for_selector("#kiosk-notes .notice")
        assert page.inner_text("#kiosk-notes .notice") == "초코 케이크는 품절입니다."
        assert "아직 담은 메뉴가 없습니다" in page.inner_text("#order-lines")
        replied(page, "품절")
        shot(page, "kiosk_sold_out_notice", full=False)
        page.wait_for_selector("#kiosk-notes .notice", state="detached", timeout=9000)


def test_items_are_not_pressable_when_the_agent_offers_no_actions():
    reset()
    post("/mock/set?kiosk_actions=0")
    with open_page("/") as page:
        sent = frames(page)
        start(page)
        opened(page)
        assert page.locator("#menu-list .item").count() == 13 and page.locator("#menu-list button.item").count() == 0
        assert page.evaluate("getComputedStyle(document.querySelector('.item'), '::after').content") == "none"
        page.click(".item[data-item=cookie]")
        page.wait_for_timeout(300)
        assert not actions(sent) and page.locator("#picker").is_hidden()
        say(page, "콜드브루 하나요")                                        # ordering by voice or text still works
        page.wait_for_selector("#order-lines .oline")
        assert page.locator("#order-lines .rm").count() == 0                # no remove control either
        assert "말로 주문하세요" not in page.inner_text("#order-lines")
        shot(page, "kiosk_no_actions", full=False)
    reset()


def test_category_buttons_filter_the_menu():
    reset()
    with open_page("/") as page:
        start(page)
        opened(page)
        shown = lambda: page.evaluate("[...document.querySelectorAll('#menu-list .menu-cat')].filter(n => !n.hidden).map(n => n.getAttribute('aria-label'))")
        pressed = lambda: page.evaluate("[...document.querySelectorAll('#menu-cats button')].filter(b => b.getAttribute('aria-pressed') === 'true').map(b => b.textContent)")
        total = page.locator("#menu-list .item").count()
        assert shown() == ["커피", "논커피", "차", "디저트"] and pressed() == ["전체"]
        page.click("#menu-cats button >> text='차'")
        assert shown() == ["차"] and pressed() == ["차"]
        visible = page.locator("#menu-list .item:visible").count()
        assert 0 < visible < total and page.evaluate("document.getElementById('menu-scroll').scrollTop") == 0
        page.click("#menu-cats button >> text='디저트'")                       # the last kind: used to do nothing
        assert shown() == ["디저트"] and pressed() == ["디저트"]
        page.click(".item[data-item=croissant]")                               # ordering from a filtered menu works
        page.wait_for_selector("#order-lines .oline")
        assert shown() == ["디저트"]                                           # and the filter stays while ordering
        page.click("#menu-cats button >> text='전체'")
        assert shown() == ["커피", "논커피", "차", "디저트"] and page.locator("#menu-list .item:visible").count() == total
        # what the agent finds is shown even if the filter was hiding it
        page.click("#menu-cats button >> text='디저트'")
        replied(page, "크루아상 담았어요")
        say(page, "달달한 거 뭐 있어요?")
        page.wait_for_selector("#menu-list .item.hl")
        assert page.locator("#menu-list .item.hl:visible").count() == page.locator("#menu-list .item.hl").count()
        shot(page, "kiosk_category_filter", full=False)
    reset()


# ---- confirm and receipt ----------------------------------------------------------------------------

def test_confirm_card_appears_over_the_menu_and_the_receipt_follows_consent():
    reset()
    with open_page("/") as page:
        sent = frames(page)
        start(page)
        opened(page)
        say(page, "아이스 아메리카노 두 잔 주세요")
        page.wait_for_selector("#order-lines .oline")
        replied(page, "담았어요")
        say(page, "결제할게요")
        page.wait_for_selector("#spot #k-confirm")
        card = page.inner_text("#k-confirm")
        for part in ("주문 확인", "답을 기다리는 중", "아메리카노 2개, 합계 9,000원 결제를 진행할까요?", "차갑게", "2개", "9,000원", "합계",
                     "“네” 또는 “아니요”라고 말하거나 아래 단추를 눌러 주세요.", "답하기 전에는 아무것도 진행되지 않습니다."):
            assert part in card, part
        # the answer may also be touched: two buttons, which send it as the customer's own words
        assert [b.inner_text() for b in page.locator("#k-confirm .kacts button").all()] == ["아니요", "네"]
        assert page.locator("#k-confirm button:not([disabled])").count() == 2
        # a prominent place: over the menu, not a chat bubble; the old chat card is not used for screen agents
        assert page.locator("#chat-log #k-confirm, #chat-log .confirm").count() == 0
        spot, menu = box(page, "#k-confirm"), box(page, "#menu-panel")
        assert spot["x"] >= menu["x"] and spot["r"] <= menu["r"] and spot["y"] >= menu["y"] and spot["b"] <= menu["b"], (spot, menu)
        assert page.locator("#menu-panel.locked").count() == 1
        assert page.evaluate("(() => { const r = document.querySelector('.item[data-item=cookie]').getBoundingClientRect();"
                             " return !document.elementFromPoint(r.left + 20, r.top + 20).closest('.item'); })()")     # the menu cannot be pressed through it
        replied(page, "결제를 진행할까요")
        assert page.inner_text("#order-total") == "9,000원"                  # the order stays visible beside the card
        shot(page, "kiosk_confirm", full=False)

        say(page, "네")
        page.wait_for_selector("#k-confirm", state="detached")
        page.wait_for_selector("#spot #k-receipt")
        receipt = page.inner_text("#k-receipt")
        for part in ("주문 번호", "K-0013", "아메리카노", "차갑게", "9,000원", "주문이 접수되었습니다. 번호가 불리면 받아 가세요."):
            assert part in receipt, part
        assert page.evaluate("parseFloat(getComputedStyle(document.querySelector('#k-receipt .orderno')).fontSize)") >= 40
        assert page.locator("#chat-log #k-receipt").count() == 0 and page.locator("#menu-panel.locked").count() == 0
        page.wait_for_function("document.getElementById('order-total').textContent === '0원'")       # the cart display after payment
        replied(page, "결제가 끝났습니다")
        shot(page, "kiosk_receipt", full=False)
        page.click("#k-receipt >> text=닫기")
        assert page.locator("#spot").is_hidden() and page.locator("#k-receipt").count() == 0
        assert not actions(sent)


def test_confirm_card_buttons_answer_in_the_customers_words():
    """The buttons are not a second way to run the tool: they send "네…" / "아니요…" as a chat message."""
    reset()
    with open_page("/") as page:
        sent = frames(page)
        start(page)
        opened(page)
        page.click(".item[data-item=croissant]")
        page.wait_for_selector("#order-lines .oline")
        replied(page, "크루아상 담았어요")
        say(page, "결제할게요")
        page.wait_for_selector("#k-confirm")
        replied(page, "결제를 진행할까요")
        page.click("#k-confirm [data-answer=no]")                           # declined by touch: nothing ran, asked again
        page.wait_for_selector("#k-confirm.reask")
        assert page.locator("#k-receipt").count() == 0 and page.inner_text("#order-total") == "4,000원"
        replied(page, "결제하지 않았어요")
        assert page.locator("#k-confirm button:not([disabled])").count() == 2      # the same card can be answered again
        say(page, "결제할게요")
        page.wait_for_selector("#k-confirm:not(.reask)")
        replied(page, "결제를 진행할까요")
        page.click("#k-confirm [data-answer=yes]")
        page.wait_for_selector("#k-receipt")
        assert page.locator("#k-confirm").count() == 0
        chats = [f.get("text") for f in sent if f.get("type") == "chat"]
        assert "아니요, 하지 않을게요." in chats and "네, 그렇게 해 주세요." in chats
        assert not [a for a in actions(sent) if a["name"] not in ("add_item",)]    # no payment action exists: consent stays in words


def test_restart_is_always_offered_and_starts_a_fresh_order():
    reset()
    with open_page("/") as page:
        choose(page, "kiosk-order")
        page.wait_for_selector("#restart:not([disabled])")                  # before any conversation: it starts one
        assert page.locator("#restart").is_visible() and page.inner_text("#restart") == "처음부터"
        page.click("#restart")
        page.wait_for_selector("#text:not([disabled])")
        opened(page)
        page.click(".item[data-item=croissant]")
        page.wait_for_selector("#order-lines .oline")
        replied(page, "크루아상 담았어요")
        assert page.locator("#restart").is_visible() and page.locator("#restart").is_enabled()
        page.click("#restart")                                              # mid-order: the order and the talk are dropped
        opened(page)
        page.wait_for_function("document.querySelectorAll('#order-lines .oline').length === 0")
        assert page.inner_text("#order-total") == "0원" and page.locator("#chat-log .msg.user").count() == 0
        assert page.inner_text("#toggle") == "정지"

        say(page, "아이스 아메리카노 두 잔 주세요")
        page.wait_for_selector("#order-lines .oline")
        replied(page, "담았어요")
        say(page, "결제할게요")
        page.wait_for_selector("#k-confirm")
        replied(page, "결제를 진행할까요")
        page.click("#k-confirm [data-answer=yes]")
        page.wait_for_selector("#k-receipt")
        assert [b.inner_text() for b in page.locator("#k-receipt .kacts button").all()] == ["닫기", "처음으로"]
        replied(page, "결제가 끝났습니다")
        page.click("#k-receipt [data-restart]")                             # after the receipt: back to the first screen
        page.wait_for_selector("#k-receipt", state="detached")
        opened(page)
        assert page.locator("#spot").is_hidden() and page.inner_text("#order-total") == "0원"
        assert page.locator("#chat-log .msg.user").count() == 0
        shot(page, "kiosk_restart", full=False)
    reset()


def test_confirm_card_clears_when_the_customer_declines():
    reset()
    with open_page("/") as page:
        start(page)
        opened(page)
        page.click(".item[data-item=croissant]")
        page.wait_for_selector("#order-lines .oline")
        replied(page, "크루아상 담았어요")
        say(page, "계산해 주세요")
        page.wait_for_selector("#k-confirm")
        replied(page, "결제를 진행할까요")
        say(page, "아니요, 잠깐만요")
        # not consent: nothing ran. The card is dimmed and says so; the menu can be used again
        page.wait_for_selector("#k-confirm.reask")
        assert "다시 확인 중" in page.inner_text("#k-confirm") and "아직 아무것도 진행되지 않았습니다" in page.inner_text("#k-confirm")
        assert page.locator("#k-receipt").count() == 0 and page.locator("#menu-panel.locked").count() == 0
        assert page.inner_text("#order-total") == "4,000원" and page.locator("#order-lines .oline").count() == 1
        replied(page, "결제하지 않았어요")
        say(page, "콜드브루도 하나 주세요")                                 # the order changes: the old question is gone
        page.wait_for_selector("#k-confirm", state="detached")
        page.wait_for_function("document.querySelectorAll('#order-lines .oline').length === 2")
        assert page.locator("#spot").is_hidden()


def test_confirm_card_stays_while_the_agent_asks_again_and_goes_when_the_customer_moves_on():
    reset()
    with open_page("/") as page:
        start(page)
        opened(page)
        page.click(".item[data-item=croissant]")
        page.wait_for_selector("#order-lines .oline")
        replied(page, "크루아상 담았어요")
        say(page, "결제할게요")
        page.wait_for_selector("#k-confirm")
        replied(page, "결제를 진행할까요")
        say(page, "음 글쎄요")                                              # unclear: asked again, no new confirm display
        page.wait_for_selector("#k-confirm.reask")
        replied(page, "네 또는 아니요로")
        assert page.locator("#k-confirm").count() == 1 and "크루아상" in page.inner_text("#k-confirm")
        shot(page, "kiosk_confirm_reask", full=False)
        say(page, "네")                                                     # the same card is answered
        page.wait_for_selector("#k-receipt")
        assert page.locator("#k-confirm").count() == 0
        replied(page, "결제가 끝났습니다")
        page.click("#k-receipt >> text=닫기")

        page.click(".item[data-item=cookie]")                               # a second order, declined, then small talk
        page.wait_for_selector("#order-lines .oline")
        replied(page, "초코칩 쿠키 담았어요")
        say(page, "계산이요")
        page.wait_for_selector("#k-confirm")
        replied(page, "결제를 진행할까요")
        say(page, "아니요")
        page.wait_for_selector("#k-confirm.reask")
        replied(page, "결제하지 않았어요")
        say(page, "안녕하세요")                                             # a turn that neither answers nor changes the order
        page.wait_for_selector("#k-confirm", state="detached")
        assert page.inner_text("#order-total") == "3,000원"


def test_a_refused_touch_is_shown_as_a_notice_without_a_bubble():
    reset()
    with open_page("/") as page:
        start(page)
        opened(page)
        # the server refuses an action it does not offer with one chat_done: no words, the reason in `error`
        page.evaluate("chatBubbles[77] = addBubble('assistant', ''); ws.send(JSON.stringify({type: 'action', id: 77, name: 'pay_now', arguments: {}}))")
        page.wait_for_selector("#kiosk-notes .notice")
        assert "화면 동작 pay_now" in page.inner_text("#kiosk-notes .notice")
        assert page.locator(".msg.assistant").count() == 1                  # only the opening: no empty bubble is left


def test_payment_with_an_empty_order_shows_a_notice_and_no_card():
    reset()
    with open_page("/") as page:
        start(page)
        opened(page)
        say(page, "결제할게요")
        page.wait_for_selector("#kiosk-notes .notice")
        assert "주문 내역이 비어 있습니다" in page.inner_text("#kiosk-notes") and page.locator("#k-confirm").count() == 0


def test_handoff_keeps_the_screen_and_stops_touches():
    reset()
    with open_page("/") as page:
        sent = frames(page)
        start(page)
        opened(page)
        say(page, "직원 좀 불러 주세요")
        page.wait_for_selector("#handoff-card")
        assert "직원에게 연결했습니다. 잠시만 기다려 주세요." in page.inner_text("#kiosk-notes")
        assert page.locator("#menu-panel.frozen").count() == 1 and page.locator("#text").is_disabled()
        page.click(".item[data-item=cookie]", force=True)
        page.wait_for_timeout(300)
        assert not actions(sent)
        page.wait_for_timeout(7200)                                         # this notice stays
        assert "직원에게 연결했습니다" in page.inner_text("#kiosk-notes")


# ---- developers: the backstage dock -------------------------------------------------------------------

def test_backstage_is_behind_a_toggle_for_screen_agents():
    reset()
    with open_page("/") as page:
        start(page)
        opened(page)
        say(page, "아이스 아메리카노 두 잔 주세요")
        page.wait_for_selector("#order-lines .oline")
        replied(page, "담았어요")
        toggle = page.locator("#open-backstage")
        assert toggle.inner_text() == "무대 뒤 보기" and toggle.get_attribute("aria-pressed") == "false"
        assert page.locator(".backstage").is_hidden()
        toggle.click()
        page.wait_for_selector(".backstage", state="visible")
        assert toggle.inner_text() == "무대 뒤 닫기" and toggle.get_attribute("aria-pressed") == "true"
        assert page.locator("#sources .src").count() == 8 and page.locator("#src-add_to_cart.answered").count() == 1
        trace = page.inner_text("#trace")
        assert "화면에 표시 (메뉴): 메뉴 전체: 4종류, 13개 (품절 2개)" in trace and "화면에 표시 (주문 내역): 주문 내역 1줄, 합계 9,000원" in trace
        assert "add_to_cart 에 변경 요청" in trace
        assert page.inner_text("#s-session").startswith("s-")
        dock, order = box(page, ".backstage"), box(page, "#order-panel")
        assert abs(dock["b"] - 900) < 1 and dock["x"] == 0                   # docked to the bottom of the window
        page.evaluate("window.scrollTo(0, 0)")
        assert box(page, "#order-panel")["h"] >= 300, order                  # the ordering layout keeps its size; the page scrolls instead
        no_sideways_scroll(page)
        shot(page, "kiosk_backstage", full=False)
        toggle.click()
        assert page.locator(".backstage").is_hidden() and page.evaluate("document.documentElement.scrollHeight") <= 900


def test_agents_without_a_screen_look_as_before():
    reset()
    with open_page("/") as page:
        choose(page, "shop-support")
        page.wait_for_selector("#sources .src")
        before = [box(page, ".onair"), box(page, ".backstage"), box(page, ".stage")]
        choose(page)                                                         # a screen agent and back
        assert page.locator("#stage.kiosk").count() == 1
        page.click("#open-backstage")
        choose(page, "shop-support")
        page.wait_for_selector("#sources .src")
        assert [box(page, ".onair"), box(page, ".backstage"), box(page, ".stage")] == before
        assert page.evaluate("document.body.className") == "page-talk" and page.get_attribute("#stage", "class") == "stage no-avatar"
        for hidden in ("#menu-panel", "#order-panel", "#order-bar", "#open-backstage", "#picker", "#kscrim", "#spot"):
            assert page.locator(hidden).is_hidden(), hidden
        page.click("#toggle")                                                # ready.screen.panels is empty: nothing changes
        page.wait_for_selector("#text:not([disabled])")
        assert page.get_attribute("#stage", "class") == "stage no-avatar" and page.locator(".backstage").is_visible()
        say(page, "A1001 1234")
        page.wait_for_selector("#src-verify_customer.answered")
        assert page.locator("#chat-log .msg.tool:visible").count() >= 1      # tool lines stay in the chat as before
        say(page, "환불해 주세요")
        page.wait_for_selector("#chat-log .confirm[data-state=waiting]")     # and the consent card with its buttons
        assert page.locator("#chat-log .confirm .quick button").count() == 2


# ---- outline agents ----------------------------------------------------------------------------------

def test_outline_agent_cannot_be_started_and_says_why():
    reset()
    why = "이 에이전트는 뼈대입니다. 도구 3개가 등록되지 않아 대화할 수 없습니다: verify_booking, get_booking, search_alternative_flights"
    with open_page("/") as page:
        sockets = []
        page.on("websocket", lambda ws: sockets.append(ws.url))
        choose(page, "flight-rebooking")
        assert page.locator("#toggle").is_disabled()
        assert page.inner_text("#message") == why
        assert "아직 대화할 수 없습니다" in page.inner_text("#chat-log") and why in page.inner_text("#chat-log")
        assert "뼈대 · 도구 3개 미등록, 대화 불가" in page.inner_text("#agent option:checked")
        page.locator("#toggle").click(force=True)
        page.wait_for_timeout(300)
        assert not sockets and page.inner_text("#toggle") == "대화 시작"
        shot(page, "index_outline_blocked", full=False)
        choose(page, "shop-support")                                         # another agent: the block is gone
        assert page.locator("#toggle").is_enabled() and "뼈대" not in page.inner_text("#message")
        choose(page, "clinic-intake")                                        # a draft without tools is not an outline
        assert page.locator("#toggle").is_enabled()
    with open_page("/?agent=flight-rebooking") as page:                      # also when the address asks for it
        page.wait_for_function("document.getElementById('message').textContent.includes('뼈대')")
        assert page.locator("#toggle").is_disabled()


def test_the_selector_says_how_far_an_undeployed_agent_got_and_reads_tool_packs():
    """agentReadiness (common.js) on rows like GET /api/agents gives them: the newest version's state
    for agents not deployed yet, and tool names looked up in the agent's tool packs."""
    reset()
    with open_page("/") as page:
        labels = page.evaluate("""() => {
          const ids = toolIds([{id: 'cafe.search_menu', name: 'search_menu'}, {id: 'burger.submit_order', name: 'submit_order'},
                               {id: 'common.handoff_to_human', name: 'handoff_to_human'}, {name: 'get_weather'}]);
          const row = (state, more) => ({deployment: {stable: null}, raw: Object.assign({versions: [{version: 1, state: 'draft'}, {version: 2, state}],
                                                             tools: [], deployment: {stable: null}}, more)});
          const say = (r) => agentReadiness(r, ids).label;
          return {
            draft: say(row('draft')), tested: say(row('tested')), pending: say(row('pending_approval')),
            approved: say(row('approved')),
            packed: say(row('tested', {tools: ['submit_order', 'handoff_to_human'], tool_packs: ['burger', 'common']})),
            wrongPack: say(row('tested', {tools: ['submit_order'], tool_packs: ['cafe']})),
            noPacks: say(row('draft', {tools: ['search_menu', 'get_weather']})),
            live: agentReadiness({deployment: {stable: 1}, raw: {versions: [{version: 1, state: 'deployed'}], tools: []}}, ids).label,
          };
        }""")
    assert labels["draft"] == "초안 · 배포 전" and labels["tested"] == "시험 완료 · 배포 전"
    assert labels["pending"] == "승인 대기 · 배포 전" and labels["approved"] == "승인됨 · 배포 전"
    assert labels["packed"] == "시험 완료 · 배포 전"                        # bare names found in the agent's packs
    assert labels["wrongPack"].startswith("뼈대 · 도구 1개 미등록")           # submit_order is not a cafe tool
    assert labels["noPacks"] == "초안 · 배포 전" and labels["live"] == "운영 중 v1"


# ---- voice and avatar ---------------------------------------------------------------------------------

def test_voice_channel_speaks_the_opening_and_a_spoken_turn_changes_the_screen():
    reset()
    with open_page("/") as page:
        choose(page)
        page.check("input[name=channel][value=voice]", force=True)
        page.click("#toggle")
        page.wait_for_selector("#text:not([disabled])")
        opened(page)
        page.click("#open-backstage")
        page.wait_for_function("document.getElementById('s-audio').textContent.startsWith('3청크')")      # the opening is spoken too
        page.click("#open-backstage")
        sid = page.evaluate("cfg.session_id")
        assert page.locator("#voicebar").is_visible() and page.locator("#voicebar .hint").is_hidden()
        post("/mock/say?" + urllib.parse.urlencode(dict(session=sid, text="달달한 거 뭐 있어요?")))
        page.wait_for_selector("#menu-list .item.hl")
        page.wait_for_selector(".heard-wrap")
        page.wait_for_function("document.getElementById('listen-label').textContent === '듣는 중'")
        post("/mock/say?" + urllib.parse.urlencode(dict(session=sid, text="아이스 아메리카노 두 잔 주세요")))
        page.wait_for_selector("#order-lines .oline")
        assert page.locator("#menu-list .item.hl").count() == 0              # cleared by the spoken turn
        page.wait_for_function("document.getElementById('listen-label').textContent === '듣는 중'")
        shot(page, "kiosk_voice", full=False)


def test_avatar_channel_keeps_the_face_small_beside_the_conversation():
    reset()
    with open_page("/") as page:
        choose(page)
        page.check("input[name=channel][value=avatar]", force=True)
        page.click("#toggle")
        page.wait_for_selector("#text:not([disabled])")
        opened(page)
        page.wait_for_function("(() => { const c = document.getElementById('canvas');"
                               " const d = c.getContext('2d').getImageData(c.width / 2, c.height * 0.85, 1, 1).data; return d[2] > 100; })()")
        face, talk, menu = box(page, "#avatar-box"), box(page, ".talk"), box(page, "#menu-panel")
        assert face["w"] <= 140 and face["x"] > menu["r"] and talk["x"] > face["r"], (face, talk, menu)
        page.click(".item[data-item=cookie]")
        page.wait_for_selector("#order-lines .oline")
        replied(page, "초코칩 쿠키 담았어요")
        shot(page, "kiosk_avatar", full=False)


# ---- phone width ----------------------------------------------------------------------------------------

def test_phone_width_keeps_the_order_one_touch_away():
    reset()
    with open_page("/", width=420, height=900) as page:
        sent = frames(page)
        start(page)
        opened(page)
        no_sideways_scroll(page)
        assert page.locator("#order-panel").is_hidden()
        bar = page.locator("#order-bar")
        assert bar.is_visible() and "주문 내역 0개" in bar.inner_text() and "0원" in bar.inner_text()
        assert page.evaluate("document.documentElement.scrollHeight") > 900     # the menu is long: the page scrolls
        page.locator(".item[data-item=cookie]").scroll_into_view_if_needed()
        spot = box(page, "#order-bar")
        assert spot["b"] <= 900 and spot["y"] > 800 and spot["h"] >= 56, spot    # the bar stays at the bottom of the window
        shot(page, "kiosk_phone", full=False)

        page.click(".item[data-item=earl_grey]")
        page.wait_for_selector("#picker:not([hidden])")
        sheet = box(page, "#picker")
        assert abs(sheet["b"] - 900) < 1 and sheet["w"] == 420, sheet           # a sheet from the bottom
        shot(page, "kiosk_phone_picker", full=False)
        page.click("#picker [data-choice=ice]")
        page.click("#picker-add")
        page.wait_for_function("document.getElementById('order-bar').textContent.includes('1개')")
        assert "4,500원" in bar.inner_text()
        page.click(".item[data-item=cookie]")
        page.wait_for_function("document.getElementById('order-bar').textContent.includes('2개')")
        assert "7,500원" in bar.inner_text() and len(actions(sent)) == 2
        replied(page, "초코칩 쿠키 담았어요")

        bar.click()                                                          # the order opens as a sheet
        page.wait_for_selector("#order-panel", state="visible")
        text = page.inner_text("#order-panel")
        for part in ("주문 내역", "얼그레이", "차갑게", "초코칩 쿠키", "7,500원", "빼기", "닫기"):
            assert part in text, part
        panel = box(page, "#order-panel")
        assert abs(panel["b"] - 900) < 1 and panel["y"] > 100, panel
        no_sideways_scroll(page)
        shot(page, "kiosk_phone_order", full=False)
        page.click("#order-lines .oline >> nth=1 >> .rm")
        page.wait_for_function("document.querySelectorAll('#order-lines .oline').length === 1")
        page.click("#order-close")
        assert page.locator("#order-panel").is_hidden() and page.locator("#kscrim").is_hidden()
        page.wait_for_function("document.getElementById('order-bar').textContent.includes('1개')")
        replied(page, "뺐어요")

        say(page, "결제할게요")
        page.wait_for_selector("#k-confirm")
        page.wait_for_function("(() => { const r = document.getElementById('k-confirm').getBoundingClientRect(); return r.top >= 0 && r.top < 500; })()")
        assert page.locator("#text").is_visible() or page.evaluate("document.getElementById('text').getBoundingClientRect().height") > 0
        replied(page, "결제를 진행할까요")
        no_sideways_scroll(page)
        shot(page, "kiosk_phone_confirm", full=False)
        say(page, "네")
        page.wait_for_selector("#k-receipt")
        page.wait_for_function("document.getElementById('order-bar').textContent.includes('0개')")
        replied(page, "결제가 끝났습니다")
        no_sideways_scroll(page)
        shot(page, "kiosk_phone_receipt", full=False)
        page.click("#open-backstage")                                        # the developer dock also fits
        page.wait_for_selector(".backstage", state="visible")
        no_sideways_scroll(page)


# ---- monitor -------------------------------------------------------------------------------------------

def open_kiosk_session(page):
    page.wait_for_function("document.querySelectorAll('#turns .turn').length === 6")


def test_monitor_shows_what_was_on_the_screen_and_what_was_touched():
    reset()
    with open_page("/monitor?session=s-kiosk") as page:
        open_kiosk_session(page)
        assert "5턴" in page.inner_text("#sess-head")                        # the opening is not a customer turn
        legend = page.inner_text("#sess-head .klegend")
        assert "화면 표시" in legend and "화면 터치" in legend
        row = page.inner_text(".sess[data-sid='s-kiosk']")
        assert "키오스크 주문 도우미" in row and "5턴" in row

        first = page.inner_text("#turn-0")                                   # turn 0: the opening
        assert "시작" in first and "고객이 말하기 전" in first and OPENING in first and "화면 표시 1건" in first
        assert "0번 턴" not in first and page.locator("#turn-0 .mh").count() == 0 and page.locator("#turn-0.over").count() == 0
        page.click("#turn-0 details.more > summary")
        page.wait_for_selector("#turn-0 .spans")
        menu = page.inner_text("#turn-0 .span[data-kind=display]")
        for part in ("화면 표시", "메뉴", "메뉴 전체: 4종류, 13개 (품절 2개)", "커피 4개", "논커피 3개", "차 2개", "디저트 4개", "출처: get_menu 도구의 결과"):
            assert part in menu, part

        page.click("#turn-1 details.more > summary")
        page.wait_for_selector("#turn-1 .span.sub")
        # the cart refresh the gateway made is shown under the call it belongs to, not counted as a call of the model
        assert "도구 1회" in page.inner_text("#turn-1 .turn-head") and page.locator("#turn-1 .span[data-kind=tool]").count() == 2
        sub = page.inner_text("#turn-1 .span.sub")
        assert "get_cart" in sub and "화면 갱신" in sub and "add_to_cart 호출에 딸린 조회입니다" in sub
        cart = page.inner_text("#turn-1 .span[data-kind=display]")
        for part in ("주문 내역", "주문 내역 1줄, 합계 9,000원", "아메리카노 (차갑게) × 2 · 9,000원", "출처: get_cart"):
            assert part in cart, part

        page.click("#turn-2 details.more > summary")
        page.wait_for_selector("#turn-2 .spans")
        items = page.inner_text("#turn-2 .span[data-kind=display]")
        for part in ("찾은 메뉴", "“달달한” 찾은 메뉴 6개를 강조", "바닐라 라떼 5,500원", "초코 케이크 6,500원 (품절)"):
            assert part in items, part

        touch = page.inner_text("#turn-3")                                   # the touch turn is marked on the card
        assert "화면 터치" in touch and "고객 (터치)" in touch and "add_item: vanilla_latte 1개 (temperature hot)" in touch
        assert "(화면에서 바닐라 라떼를 담았습니다)" in touch and page.locator("#turn-3 .mh").count() == 0
        assert page.locator("#turn-1 .badge.b-touch").count() == 0 and page.locator("#turn-3 .badge.b-touch").count() == 1
        assert page.locator("#turn-1 .mh").count() == 1                      # spoken turns keep the misheard control
        page.click("#turn-3 details.more > summary")
        page.wait_for_selector("#turn-3 .spans")
        action = page.inner_text("#turn-3 .span[data-kind=action]")
        for part in ("화면 터치", "add_item", "고객이 화면에서 누름", "vanilla_latte 1개 (temperature hot)", "이 터치로 호출된 도구: add_to_cart"):
            assert part in action, part
        assert "주문 내역 2줄, 합계 14,500원" in page.inner_text("#turn-3 .span[data-kind=display]")

        page.click("#turn-4 details.more > summary")
        page.wait_for_selector("#turn-4 .spans")
        confirm = page.inner_text("#turn-4 .span[data-kind=display]")
        for part in ("확인 카드", "고객 화면에 띄운 글", "아메리카노 2개, 바닐라 라떼 1개, 합계 14,500원 결제를 진행할까요?",
                     "아메리카노 (차갑게) × 2 · 9,000원", "바닐라 라떼 (따뜻하게) × 1 · 5,500원", "합계 14,500원"):
            assert part in confirm, part

        page.click("#turn-5 details.more > summary")
        page.wait_for_selector("#turn-5 .span[data-kind=display]")
        assert page.locator("#turn-5 .span[data-kind=display]").count() == 2
        receipt = page.inner_text("#turn-5 .span[data-kind=display] >> nth=0")
        for part in ("영수증", "주문 번호 K-0012, 합계 14,500원", "바닐라 라떼 (따뜻하게) × 1 · 5,500원", "주문이 접수되었습니다. 번호가 불리면 받아 가세요."):
            assert part in receipt, part
        assert "주문 내역 비어 있음" in page.inner_text("#turn-5 .span[data-kind=display] >> nth=1")
        # the stored payload is still there for whoever needs it
        page.click("#turn-5 .span[data-kind=display] >> nth=0 >> details > summary")
        assert '"order_no": "K-0012"' in page.inner_text("#turn-5 .span[data-kind=display] >> nth=0")
        assert page.locator("#turn-3 .lbar i.tick").count() >= 2             # the touch and the display are moments on the bar
        no_sideways_scroll(page)
        shot(page, "monitor_kiosk_session")


def test_monitor_makes_an_opening_card_from_spans_when_the_store_has_no_turn_0():
    reset()

    def without_turn_0(route):
        response = route.fetch()
        body = response.json()
        body["turns"] = [t for t in body["turns"] if t["turn"] != 0]
        route.fulfill(response=response, body=json.dumps(body))
    with open_page("/monitor?session=s-kiosk", prepare=lambda p: p.route("**/api/sessions/s-kiosk", without_turn_0)) as page:
        open_kiosk_session(page)
        assert "시작" in page.inner_text("#turn-0") and "화면 표시 1건" in page.inner_text("#turn-0")


def test_monitor_kiosk_session_at_phone_width_and_live_touch_turns():
    reset()
    with open_page("/monitor?session=s-kiosk", width=420, height=900) as page:
        open_kiosk_session(page)
        for n in (0, 3, 4, 5):
            page.click(f"#turn-{n} details.more > summary")
        no_sideways_scroll(page)
        shot(page, "monitor_kiosk_phone")
    # a touch made on the conversation page is stored as an action span and shows up in the monitor
    with open_page("/") as talk:
        start(talk)
        opened(talk)
        talk.click(".item[data-item=cookie]")
        talk.wait_for_selector("#order-lines .oline")
        replied(talk, "초코칩 쿠키 담았어요")
        sid = talk.evaluate("cfg.session_id")
        with open_page("/monitor?session=" + sid) as page:
            page.wait_for_function("document.querySelectorAll('#turns .turn').length === 2")
            assert "시작" in page.inner_text("#turn-0")
            text = page.inner_text("#turn-1")
            assert "화면 터치" in text and "add_item: cookie 1개" in text
