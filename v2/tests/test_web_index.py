"""Conversation page (web/index.html) against the mock server."""
import json
import urllib.parse
import urllib.request

from test_web_support import BASE, ROUTE_MISSING, focus_is_visible, no_sideways_scroll, open_page, post, reset, shot


def ready(page, agent="shop-support"):
    page.wait_for_selector("#agent:not([disabled])")
    page.select_option("#agent", agent)
    page.wait_for_selector("#toggle:not([disabled])")


def start(page):
    page.click("#toggle")
    page.wait_for_selector("#text:not([disabled])")


def say(page, text):
    page.fill("#text", text)
    page.press("#text", "Enter")


def test_text_channel_refund_consent_and_handoff():
    reset()
    with open_page("/") as page:
        ready(page)
        # lanes come from the agent's tool list; shop-support has no knowledge base and no avatar
        assert page.locator("#sources .src").count() == 5
        assert page.locator("#src-knowledge").count() == 0
        assert page.locator("#src-request_refund.write").count() == 1
        # the avatar is offered for every agent; this one has no face of its own, so the default is used
        assert page.locator("input[name=channel][value=avatar]").is_enabled()
        assert page.locator("#avatar-option").get_attribute("data-face") == "default"
        assert "대화할 준비" in page.inner_text("#chat-log")
        shot(page, "index_idle_shop")
        focus_is_visible(page)

        start(page)
        assert page.locator("#recording").is_hidden()
        say(page, "주문번호 A1001, 뒤 네 자리 1234예요")
        page.wait_for_selector("#src-verify_customer.answered")
        assert "추임새: 본인 확인을 하고 있어요." in page.inner_text("#chat-log")
        page.wait_for_function("document.querySelectorAll('.msg.assistant').length === 1 && document.querySelector('.msg.assistant').textContent.includes('본인 확인')")
        assert page.inner_text("#s-delay") != "-"

        say(page, "이어폰 환불해 주세요")
        page.wait_for_selector(".confirm[data-state=waiting]")
        page.wait_for_selector("#src-request_refund.waiting")
        card = page.inner_text(".confirm")
        assert "동의 대기" in card and "32,000원" in card and "아직 실행되지 않았습니다" in card
        page.wait_for_function("[...document.querySelectorAll('.msg.assistant')].pop().textContent.includes('접수할까요')")
        shot(page, "index_confirm_waiting")

        page.click("text=동의한다고 답하기")
        page.wait_for_selector(".confirm[data-state=executed]")
        page.wait_for_selector("#src-request_refund.answered")
        assert "실행됨" in page.inner_text(".confirm") and page.locator(".confirm .quick").count() == 0
        assert "실행됨" in page.inner_text("#src-request_refund .ans")
        page.wait_for_function("[...document.querySelectorAll('.msg.assistant')].pop().textContent.includes('접수되었습니다')")
        shot(page, "index_refund_executed")

        say(page, "상담원 바꿔 주세요")
        page.wait_for_selector("#handoff-card")
        assert page.locator("#text").is_disabled() and page.locator("#send").is_disabled()
        assert "상담원" in page.get_attribute("#text", "placeholder")
        assert page.inner_text("#s-state") == "상담원 연결됨"
        page.wait_for_function("[...document.querySelectorAll('.msg.assistant')].pop().textContent.includes('연결해 드리겠습니다')")
        shot(page, "index_handoff")

        page.click("#toggle")   # stop: the conversation stays readable, controls unlock
        assert page.inner_text("#toggle") == "대화 시작" and page.locator("#handoff-card").count() == 1
        assert not page.locator("#agent").is_disabled()


def test_declined_write_tool_is_shown_as_not_executed():
    reset()
    with open_page("/") as page:
        ready(page)
        start(page)
        say(page, "A1002 5678")
        page.wait_for_selector("#src-verify_customer.answered")
        say(page, "환불할게요")
        page.wait_for_selector(".confirm[data-state=waiting]")
        page.click("text=하지 않는다고 답하기")
        page.wait_for_selector(".confirm[data-state=declined]")
        assert "실행하지 않음" in page.inner_text(".confirm")
        assert page.locator("#src-request_refund.failed").count() == 1


def test_failed_tool_before_verification():
    reset()
    with open_page("/") as page:
        ready(page)
        start(page)
        say(page, "배송 어디까지 왔어요?")
        page.wait_for_selector("#src-track_delivery.failed")
        assert "먼저 필요한 단계가 빠짐" in page.inner_text("#src-track_delivery .ans")
        assert page.locator(".msg.tool.failed").count() == 1


def test_lanes_follow_the_agent_and_recording_notice():
    reset()
    sockets = []
    with open_page("/", prepare=lambda p: p.on("websocket", lambda ws: sockets.append(ws.url))) as page:
        ready(page, "tech-trend-anchor")
        assert page.locator("#sources .src").count() == 5          # knowledge + four tools
        assert "기술 동향 자료" in page.inner_text("#src-knowledge")
        assert not page.locator("input[name=channel][value=avatar]").is_disabled()
        page.check("#test")
        start(page)
        assert page.locator("#recording").is_visible() and "기록" in page.inner_text("#recording")
        assert "test=1" in sockets[0] and "agent=tech-trend-anchor" in sockets[0] and "channel=text" in sockets[0]
        say(page, "오늘 서울 날씨 알려 줘")
        page.wait_for_selector("#src-knowledge.answered")
        page.wait_for_selector("#src-get_weather.answered")
        page.wait_for_function("document.querySelector('.msg.assistant').textContent.includes('11도')")
        shot(page, "index_test_session_anchor")


def test_voice_channel_plays_fhau_audio():
    reset()
    with open_page("/") as page:
        ready(page)
        page.check("input[name=channel][value=voice]", force=True)
        assert page.locator("#voicebar").is_visible() and page.locator("#path .node").count() == 5
        page.click("#toggle")
        page.wait_for_selector("#text:not([disabled])")
        assert page.inner_text("#listen-label") == "듣는 중"
        say(page, "A1001 1234")
        page.wait_for_function("document.getElementById('s-audio').textContent.startsWith('3청크')")
        assert "1.4초" in page.inner_text("#s-audio")
        page.wait_for_function("document.getElementById('trace').textContent.includes('소리로 나감')")
        page.wait_for_function("parseFloat(document.querySelector('.voicebar .level').style.width) > 0")   # fake microphone
        page.click("#open-settings")
        assert page.locator("#settings").is_visible() and page.input_value("#interrupt-mode") == "unmute"
        shot(page, "index_voice_settings", full=False)
        page.keyboard.press("Escape")
        shot(page, "index_voice")


def test_avatar_channel_draws_fhrt_frames():
    reset()
    with open_page("/") as page:
        ready(page, "tech-trend-anchor")
        page.check("input[name=channel][value=avatar]", force=True)
        assert page.locator("#avatar-box").is_visible()
        page.click("#toggle")
        page.wait_for_selector("#text:not([disabled])")
        say(page, "이번 주 소식")
        page.wait_for_function("document.getElementById('s-audio').textContent.startsWith('3청크')")
        page.wait_for_function("(() => { const c = document.getElementById('canvas');"
                               " const d = c.getContext('2d').getImageData(c.width / 2, c.height * 0.85, 1, 1).data; return d[2] > 100; })()")
        shot(page, "index_avatar")


def test_server_unreachable_and_missing_modules():
    with open_page("/", prepare=lambda p: p.route("**/api/**", lambda r: r.abort())) as page:
        page.wait_for_selector("text=서버에 연결할 수 없습니다")
        assert "agentkit.server" in page.inner_text("#chat-log") and page.locator("#toggle").is_disabled()
        shot(page, "index_unreachable")
    with open_page("/", prepare=lambda p: p.route("**/api/agents", lambda r: r.fulfill(**ROUTE_MISSING))) as page:
        page.wait_for_selector("text=에이전트 목록 기능은 아직 쓸 수 없습니다")
    with open_page("/", prepare=lambda p: p.route("**/api/agents", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))) as page:
        page.wait_for_selector("text=등록된 에이전트가 없습니다")

    def no_tools(p):
        p.route("**/api/tools", lambda r: r.fulfill(**ROUTE_MISSING))
    with open_page("/", prepare=no_tools) as page:     # the tool module is missing: lanes still come from the definition
        ready(page)
        assert page.locator("#sources .src").count() == 5
        start(page)
        say(page, "A1001 1234")
        page.wait_for_selector("#src-verify_customer.answered")


def test_server_refusal_reason_is_shown():
    reset()
    with open_page("/?agent=clinic-intake") as page:
        page.wait_for_selector("#toggle:not([disabled])")
        page.evaluate("document.getElementById('version').innerHTML = '<option value=\"\">배포된 버전</option>'")
        page.click("#toggle")         # no version asked for and none deployed: the server explains and closes
        page.wait_for_function("document.getElementById('message').textContent.includes('배포된 버전이 없습니다. 버전을 골라 시작하세요.')")
        assert page.inner_text("#toggle") == "대화 시작"


def test_agent_without_deployed_version():
    reset()
    with open_page("/?agent=clinic-intake") as page:
        page.wait_for_selector("#toggle:not([disabled])")
        assert page.input_value("#version") == "1"
        assert "배포된 버전이 없습니다" in page.inner_text("#message")
        assert "찾아보는 곳이 없습니다" in page.inner_text("#sources")


def test_phone_width():
    reset()
    with open_page("/", width=420, height=900) as page:
        ready(page)
        no_sideways_scroll(page)
        start(page)
        say(page, "A1001 1234")
        page.wait_for_selector("#src-verify_customer.answered")
        say(page, "환불해 주세요")
        page.wait_for_selector(".confirm[data-state=waiting]")
        page.wait_for_function("[...document.querySelectorAll('.msg.assistant')].pop().textContent.includes('접수할까요')")
        no_sideways_scroll(page)
        shot(page, "index_phone")


LANE_GEOMETRY = """() => {
    const wrap = document.getElementById('sources-wrap'), dot = document.querySelector('#n-llm .dot').getBoundingClientRect();
    const stem = getComputedStyle(wrap, '::before'), rail = getComputedStyle(wrap, '::after'), w = wrap.getBoundingClientRect();
    const pad = w.left + parseFloat(getComputedStyle(wrap).borderLeftWidth);
    const stemLeft = pad + parseFloat(stem.left), stemHeight = parseFloat(stem.height);
    return { dotX: dot.left + dot.width / 2, dotBottom: dot.bottom, stemX: stemLeft + parseFloat(stem.width) / 2, stemTop: w.top - stemHeight,
             railLeft: pad + parseFloat(rail.left), railRight: pad + parseFloat(rail.left) + parseFloat(rail.width), blockLeft: w.left, hang: wrap.dataset.hang,
             names: [...document.querySelectorAll('#path .node .name')].map(n => n.textContent),
             xs: [...document.querySelectorAll('#path .node .dot')].map(n => n.getBoundingClientRect().left) };
}"""
STAGES = {"text": ["답변 생성", "글 표시"], "voice": ["듣기", "음성 인식", "답변 생성", "음성 합성", "소리 재생"],
          "avatar": ["듣기", "음성 인식", "답변 생성", "음성 합성", "영상 생성"]}


def check_lanes(page, channel):
    g = page.evaluate(LANE_GEOMETRY)
    assert g["names"] == STAGES[channel] and g["xs"] == sorted(g["xs"]), g           # conversation order, left to right
    assert abs(g["stemX"] - g["dotX"]) <= 1, g                                        # the stem is under the dot of "답변 생성"
    assert abs(g["stemTop"] - g["dotBottom"]) <= 1.5, g                               # and starts at it
    assert g["railLeft"] <= g["blockLeft"] + 0.5 and g["railRight"] >= g["stemX"], g  # and reaches the block of lanes
    return g


def test_lanes_hang_from_the_reply_node_in_every_channel():
    reset()
    for width in (1440, 420):
        with open_page("/?agent=tech-trend-anchor", width=width, height=900) as page:
            page.wait_for_selector("#toggle:not([disabled])")
            page.wait_for_selector("#sources .src")
            for channel in ("text", "voice", "avatar"):
                page.check(f"input[name=channel][value={channel}]", force=True)
                page.wait_for_function("document.getElementById('sources-wrap').dataset.hang !== undefined")
                page.wait_for_timeout(120)
                g = check_lanes(page, channel)
                if width == 1440:
                    assert g["hang"] == "straight" and abs(g["blockLeft"] + 1 - g["dotX"]) <= 1, g   # the whole block is under the node
                elif channel != "text":
                    assert g["hang"] == "rail" and g["blockLeft"] < g["dotX"] - 40, g                 # too narrow: full width, joined by a rail
                no_sideways_scroll(page)
                shot(page, f"index_lanes_{channel}_{'wide' if width == 1440 else 'phone'}", full=width != 1440)
            if width == 1440:                                        # re-measured when the window changes
                page.set_viewport_size({"width": 1100, "height": 900})
                page.wait_for_timeout(200)
                check_lanes(page, "avatar")


def test_avatar_video_node_shows_generation_and_speech():
    reset()
    post("/mock/set?idle_video=1")
    with open_page("/") as page:
        ready(page, "tech-trend-anchor")
        page.check("input[name=channel][value=avatar]", force=True)
        page.click("#toggle")
        page.wait_for_selector("#text:not([disabled])")
        page.wait_for_function("document.querySelector('#n-out.working') && document.querySelector('#n-out .val').textContent.includes('청크당')")
        # silent chunks keep arriving: the node is working, with the cost of a chunk
        assert "만드는 중" in page.inner_text("#n-out .val") and "청크당 35ms" in page.inner_text("#n-out .val")
        page.wait_for_timeout(300)                                  # the dot's colour fades in
        colour = page.evaluate("getComputedStyle(document.querySelector('#n-out .dot')).borderTopColor")
        assert colour == "rgb(255, 179, 65)", colour
        shot(page, "index_avatar_generating", full=False)
        say(page, "이번 주 소식")
        page.wait_for_selector("#n-out.live")                       # a chunk that carries speech is on screen
        assert "말하는 영상" in page.inner_text("#n-out .val")
        shot(page, "index_avatar_speaking", full=False)
        page.wait_for_selector("#n-out.working", timeout=8000)      # speech over, generation goes on
        page.wait_for_timeout(3000)
        assert page.locator("#n-out.working").count() == 1          # and it stays lit: it is not left dimmed between questions
        say(page, "내일 날씨")                                       # a new question does not switch it off either
        page.wait_for_selector("#n-llm.active, #n-llm.done")
        assert page.locator("#n-out.working, #n-out.live").count() == 1
        page.click("#toggle")
        assert page.locator("#n-out.working, #n-out.live").count() == 0


# ---- speech recognition quality (docs/ASR_QUALITY.md) -------------------------------------------

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as response:
        return json.loads(response.read())


def start_voice(page, agent="shop-support"):
    page.wait_for_selector("#agent:not([disabled])")
    page.select_option("#agent", agent)
    page.wait_for_selector("#toggle:not([disabled])")
    page.check("input[name=channel][value=voice]", force=True)
    page.click("#toggle")
    page.wait_for_selector("#text:not([disabled])")
    return page.inner_text("#s-session")


def speak(sid, text, **extra):
    post("/mock/say?" + urllib.parse.urlencode(dict(session=sid, text=text, **extra)))


def flag_posts(page):
    """Collects the flag requests the page sends: [(path, body)]."""
    seen = []
    page.on("request", lambda r: seen.append((urllib.parse.urlparse(r.url).path, r.post_data_json))
            if r.method == "POST" and r.url.endswith("/flag") else None)
    return seen


def control_belongs_to_the_customer(page, wrap):
    """The mark is an action on the customer's own bubble: on its side, under it, not a bubble, not read out with the chat."""
    box = page.evaluate("""sel => {
        const wrap = document.querySelector(sel), bubble = wrap.querySelector('.msg.user'), mh = wrap.querySelector('.mh');
        const btn = mh.querySelector('button'), log = document.getElementById('chat-log').getBoundingClientRect();
        const agent = [...document.querySelectorAll('.msg.assistant')].pop().getBoundingClientRect();
        const b = bubble.getBoundingClientRect(), m = mh.getBoundingClientRect(), line = mh.querySelector('.mh-line').lastElementChild.getBoundingClientRect();
        const cs = getComputedStyle(btn);
        return { bubbleRight: b.right, mhRight: m.right, lineRight: line.right, mhTop: m.top, bubbleBottom: b.bottom, logRight: log.right, agentLeft: agent.left,
                 mhLeft: m.left, msgInside: mh.closest('.msg') !== null || mh.querySelector('.msg') !== null || mh.classList.contains('msg'),
                 bg: cs.backgroundColor, btnHeight: btn.getBoundingClientRect().height, bubbleHeight: b.height, font: parseFloat(cs.fontSize), bubbleFont: parseFloat(getComputedStyle(bubble).fontSize),
                 live: mh.getAttribute('aria-live'), label: btn.getAttribute('aria-label') || '', group: mh.getAttribute('aria-label') || '' };
    }""", wrap)
    assert abs(box["mhRight"] - box["bubbleRight"]) <= 1 and abs(box["lineRight"] - box["bubbleRight"]) <= 3, box       # right edge of the customer's bubble
    assert 0 <= box["mhTop"] - box["bubbleBottom"] <= 8, box                                                           # directly under it
    assert box["logRight"] - box["mhRight"] < 12 and box["mhLeft"] > box["agentLeft"] + 40, box                         # the customer's side, not the agent's
    assert not box["msgInside"] and box["bg"] == "rgba(0, 0, 0, 0)" and box["font"] < box["bubbleFont"] and box["btnHeight"] < box["bubbleHeight"] * 0.7, box
    assert box["live"] == "off" and "가져서 배달됐어요." in box["label"] + box["group"], box


# ---- conversation page ---------------------------------------------------------------------------

def test_recording_starts_on_for_internal_testing_and_remembers_the_choice():
    reset()
    with open_page("/") as page:                                   # a server that is not for internal testing: off
        page.wait_for_selector("#toggle:not([disabled])")
        assert not page.is_checked("#test")
        assert "녹음해서 품질 개선에 쓰기" in page.inner_text("#test-label")
    post("/mock/set?internal_testing=1")
    with open_page("/") as page:
        page.wait_for_selector("#toggle:not([disabled])")
        assert page.is_checked("#test")
        sid = start_voice(page)
        assert page.locator("#recording").is_visible() and "녹음" in page.inner_text("#recording")
        assert get("/api/sessions/" + sid)["meta"]["test"] is True
        shot(page, "asr_index_recording", full=False)
        page.click("#toggle")
        assert page.locator("#recording").is_hidden()
        page.uncheck("#test")                                       # the person's own choice wins from now on
        page.reload()
        page.wait_for_selector("#toggle:not([disabled])")
        assert not page.is_checked("#test")
        page.check("#test")
        page.reload()
        page.wait_for_selector("#toggle:not([disabled])")
        assert page.is_checked("#test")
    with open_page("/", prepare=lambda p: p.route("**/api/health", lambda r: r.fulfill(**ROUTE_MISSING))) as page:
        page.wait_for_selector("#toggle:not([disabled])")           # an older server without the field: off, page works
        assert not page.is_checked("#test")


def test_misheard_mark_on_a_spoken_bubble():
    reset()
    post("/mock/set?internal_testing=1")
    with open_page("/") as page:
        posts = flag_posts(page)
        sid = start_voice(page)
        page.fill("#text", "A1001 1234")                            # typed on the voice channel: turn 1, no action on it
        page.press("#text", "Enter")
        page.wait_for_function("document.querySelectorAll('.msg.assistant').length === 1 && document.querySelector('.msg.assistant').textContent.includes('본인 확인')")
        assert page.locator(".mh").count() == 0
        speak(sid, "가져서 배달됐어요.")
        page.wait_for_selector(".heard-wrap[data-turn='2']")
        assert page.inner_text(".heard-wrap .msg.user") == "가져서 배달됐어요."
        page.wait_for_function("document.querySelectorAll('.msg.assistant').length === 2 && [...document.querySelectorAll('.msg.assistant')].pop().textContent.length > 10")
        page.wait_for_function("document.getElementById('listen-label').textContent === '듣는 중'")

        page.click(".heard-wrap >> text=잘못 알아들음")
        page.wait_for_selector(".heard-wrap .mh-on")
        page.wait_for_function("document.querySelector('.heard-wrap .mh-btn:not([disabled])') !== null")
        assert posts[0] == (f"/api/sessions/{sid}/turns/2/flag", {"misheard": True})
        # the field is offered but the keyboard is not taken, and the microphone session goes on
        assert page.locator(".heard-wrap .mh-field input").is_visible()
        assert page.evaluate("document.activeElement.tagName") != "INPUT"
        assert page.inner_text("#toggle") == "정지" and page.inner_text("#listen-label") == "듣는 중"
        assert "녹음하지 않는" not in page.inner_text(".heard-wrap")
        control_belongs_to_the_customer(page, ".heard-wrap[data-turn='2']")
        assert get("/api/sessions/" + sid)["turns"][1]["flags"]["misheard"] is True
        shot(page, "asr_index_mishear_field")

        page.fill(".heard-wrap .mh-field input", "떨어진 상태로 배달됐다고요")
        page.press(".heard-wrap .mh-field input", "Enter")
        page.wait_for_selector(".heard-wrap >> text=저장했습니다")
        assert posts[1] == (f"/api/sessions/{sid}/turns/2/flag", {"misheard": True, "actual_text": "떨어진 상태로 배달됐다고요"})
        assert "떨어진 상태로 배달됐다고요" in page.inner_text(".heard-wrap .mh-actual")
        case = [c for c in get("/api/asr/cases")["cases"] if c["session_id"] == sid]
        assert len(case) == 1 and case[0]["turn"] == 2 and case[0]["expected"] == "떨어진 상태로 배달됐다고요" and case[0]["heard"] == "가져서 배달됐어요."

        speak(sid, "배송 어디까지 왔어요?")                           # the conversation goes on
        page.wait_for_selector(".heard-wrap[data-turn='3']")
        page.wait_for_function("document.querySelectorAll('.msg.assistant').length === 3 && [...document.querySelectorAll('.msg.assistant')].pop().textContent.includes('배송')")
        shot(page, "asr_index_mishear_saved")

        page.click(".heard-wrap[data-turn='2'] >> text=표시 취소")
        page.wait_for_selector(".heard-wrap[data-turn='2'] >> text=잘못 알아들음")
        page.wait_for_function("document.querySelector(\".heard-wrap[data-turn='2'] .mh-on\") === null")
        assert posts[2][1] == {"misheard": False}
        assert not [c for c in get("/api/asr/cases")["cases"] if c["session_id"] == sid]

        page.click("#toggle")                                       # after the conversation the marks still work
        page.click(".heard-wrap[data-turn='3'] >> text=잘못 알아들음")
        page.wait_for_selector(".heard-wrap[data-turn='3'] .mh-on")
        assert posts[3][0] == f"/api/sessions/{sid}/turns/3/flag"


def test_misheard_mark_without_recording_says_no_audio_is_kept():
    reset()
    with open_page("/") as page:
        sid = start_voice(page)
        assert page.locator("#recording").is_hidden()
        speak(sid, "슬머세요.")
        page.wait_for_selector(".heard-wrap")
        page.click(".heard-wrap >> text=잘못 알아들음")
        page.wait_for_selector(".heard-wrap >> text=녹음하지 않는 대화라 음성은 남지 않습니다")
        page.fill(".heard-wrap .mh-field input", "환불해 주세요")
        page.click(".heard-wrap .mh-field >> text=저장")
        page.wait_for_selector(".heard-wrap >> text=저장했습니다")
        page.wait_for_function("document.querySelector('.msg.assistant').textContent.length > 10 && document.getElementById('listen-label').textContent === '듣는 중'")
        flags = get("/api/sessions/" + sid)["turns"][0]["flags"]
        assert flags["misheard"] is True and flags["actual_text"] == "환불해 주세요"       # the mark is stored
        assert not [c for c in get("/api/asr/cases")["cases"] if c["session_id"] == sid]   # but there is no recording to make a case of
        shot(page, "asr_index_mishear_unrecorded")


def test_turn_number_comes_from_the_server_when_it_sends_one_and_early_marks_are_retried():
    reset()
    state = {"refuse": True}

    def flag(route):
        if state["refuse"]:
            route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "이 세션에 그 턴이 없습니다"}))
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "flags": dict(route.request.post_data_json, auto=[]), "case": None}))
    with open_page("/", prepare=lambda p: p.route("**/api/sessions/*/turns/*/flag", flag)) as page:
        posts = flag_posts(page)
        sid = start_voice(page)
        # marked while the agent is still thinking, before the server has stored the turn: refused, then sent again by itself
        speak(sid, "가져서 배달됐어요.", delay=1.5)
        page.wait_for_selector(".heard-wrap[data-turn='1']")
        page.click(".heard-wrap >> text=잘못 알아들음")
        page.wait_for_selector(".heard-wrap >> text=서버가 아직 이 턴을 모릅니다")
        state["refuse"] = False
        page.wait_for_function("document.querySelector('.heard-wrap .mh-note.bad-text') === null", timeout=6000)
        assert [p[0] for p in posts] == [f"/api/sessions/{sid}/turns/1/flag"] * 2 and page.locator(".heard-wrap .mh-on").count() == 1

        speak(sid, "뺏어져서 배달됐다고요.", turn=7)                  # `turn` on the transcript
        page.wait_for_selector(".heard-wrap[data-turn='7']")
        page.click(".heard-wrap[data-turn='7'] >> text=잘못 알아들음")
        page.wait_for_selector(".heard-wrap[data-turn='7'] .mh-on")
        assert posts[-1][0] == f"/api/sessions/{sid}/turns/7/flag"

        speak(sid, "떨어진 상태로 배달됐다구요.", done_turn=12)       # `turn` on chat_done corrects the count (it would say 8)
        page.wait_for_selector(".heard-wrap[data-turn='12']")
        page.click(".heard-wrap[data-turn='12'] >> text=잘못 알아들음")
        page.wait_for_selector(".heard-wrap[data-turn='12'] .mh-on")
        assert posts[-1][0] == f"/api/sessions/{sid}/turns/12/flag"

        speak(sid, "네.")                                            # no number again: one more than the last known turn
        page.wait_for_selector(".heard-wrap[data-turn='13']")

        state["refuse"] = True                                      # a refusal after the reply is over: shown, with a way to try again
        page.click(".heard-wrap[data-turn='13'] >> text=잘못 알아들음")
        page.wait_for_selector(".heard-wrap[data-turn='13'] >> text=다시 시도")
        state["refuse"] = False
        page.click(".heard-wrap[data-turn='13'] >> text=다시 시도")
        page.wait_for_function("document.querySelector(\".heard-wrap[data-turn='13'] .mh-note.bad-text\") === null")


def test_asr_phone_width():
    reset()
    with open_page("/", width=420, height=900) as page:
        sid = start_voice(page)
        speak(sid, "가져서 배달됐어요.")
        page.wait_for_selector(".heard-wrap")
        page.click(".heard-wrap >> text=잘못 알아들음")
        page.wait_for_selector(".heard-wrap .mh-field input")
        page.wait_for_function("[...document.querySelectorAll('.msg.assistant')].pop().textContent.length > 10")
        no_sideways_scroll(page)
        shot(page, "asr_phone_index")
