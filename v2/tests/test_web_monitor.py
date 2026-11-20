"""Monitor page (web/monitor.html) against the mock server."""
import json
import urllib.request

from test_web_support import BASE, ROUTE_MISSING, focus_is_visible, no_sideways_scroll, open_page, post, reset, shot


def open_session(page, sid, turns):
    page.click(f".sess[data-sid='{sid}']")
    page.wait_for_function(f"document.querySelectorAll('#turns .turn').length === {turns}")
    page.wait_for_selector("#analysis:not([hidden]) :is(.criteria, .stateview:not(.loading))")


def test_session_list_and_turn_cards():
    reset()
    with open_page("/monitor") as page:
        page.wait_for_selector(".sess")
        assert page.locator(".sess").count() == 9       # seven earlier fixtures, the recognition-quality session and the kiosk session
        row = page.inner_text(".sess[data-sid='s-refund-ok']")
        for part in ("쇼핑몰 주문 상담", "끝남", "기준 통과", "v2", "글", "5턴", "지연 중앙값 1.31초"):
            assert part in row, part
        assert "안전 기준 실패" in page.inner_text(".sess[data-sid='s-safety-fail']")
        assert "분석 없음" in page.inner_text(".sess[data-sid='s-live']")
        page.wait_for_selector("#live-state.ok")
        # the newest session opens by itself on a wide screen
        page.wait_for_selector(".sess[data-sid='s-live'][aria-current=true]")

        open_session(page, "s-refund-ok", 5)
        head = page.inner_text("#sess-head")
        assert "지연 기준 2.50초" in head and "기준을 넘은 턴 1개" in head
        assert page.locator("#turns .turn.over").count() == 1
        over = page.locator("#turn-5")
        assert "기준 2.50초 초과: 3.52초" in over.inner_text() and "변경 실행됨" in over.inner_text()
        assert "변경 전 동의 대기" in page.inner_text("#turn-4")
        assert page.locator("#turn-5 .lbar i").count() == 5 and page.locator("#turn-5 .lbar .budget").count() == 1
        text = page.inner_text("#turn-2")
        assert "[주문번호]" in text and "본인 확인이 끝났습니다" in text

        # turn detail: every span with its masked input and output
        page.click("#turn-4 details.more > summary")
        detail = page.inner_text("#turn-4 .spans")
        for part in ("도구 호출", "request_refund", "변경", "실행 전, 동의 대기", "무선 이어폰 32,000원 환불을 접수할까요?", "[주문번호]", "답변 생성", "첫 출력"):
            assert part in detail, part
        page.click("#turn-4 .span[data-kind=tool] details > summary")
        assert '"needs_confirmation": true' in page.inner_text("#turn-4 .span[data-kind=tool] pre.json >> nth=1")

        analysis = page.inner_text("#analysis")
        assert "기준 4개를 모두 통과했습니다" in analysis and "inquiry_type" in analysis and "[채워짐]" in analysis
        assert page.locator("#analysis .crit.pass").count() == 4
        shot(page, "monitor_session")
        focus_is_visible(page)


def test_failed_safety_criterion_and_guard_and_save_case():
    reset()
    with open_page("/monitor?session=s-safety-fail") as page:
        page.wait_for_selector("#analysis .criteria")
        assert page.locator("#turns .turn").count() == 3
        analysis = page.inner_text("#analysis")
        assert "안전 기준 1개를 지키지 못했습니다" in analysis
        first = page.locator("#analysis .crit").first
        assert first.get_attribute("data-criterion") == "verified_before_disclosure"
        assert "실패" in first.inner_text() and "안전 기준" in first.inner_text() and "본인 확인이 끝나기 전에" in first.inner_text()
        assert "검사에 걸린 문장" in page.inner_text("#turn-1") and "도구 실패" in page.inner_text("#turn-2")
        page.click("#turn-1 details.more > summary")
        guard = page.inner_text("#turn-1 .span[data-kind=guard]")
        assert "거둔 문장" in guard and "[주소]" in guard and "거두고 다시 생성함" in guard
        page.click("#turn-2 details.more > summary")
        assert "시간 초과" in page.inner_text("#turn-2 .span[data-kind=tool]")
        shot(page, "monitor_safety_fail")

        first.locator("button:has-text('1번 턴')").click()        # evidence link jumps to the turn
        page.wait_for_function("document.activeElement && document.activeElement.id === 'turn-1'")

        page.click("#save-case")
        page.fill("#analysis form input[type=text]", "case_disclosure_before_verify")
        page.select_option("#analysis form select >> nth=2", "")
        page.click("#analysis form button[type=submit]")
        page.wait_for_selector("#case-saved")
        assert "case_disclosure_before_verify" in page.inner_text("#case-saved")


def test_voice_test_session_has_audio_interruption_and_handoff():
    reset()
    with open_page("/monitor?session=s-handoff-voice") as page:
        page.wait_for_selector("#analysis .criteria")
        assert "시험 세션 (녹음됨)" in page.inner_text("#sess-head") and "상담원 연결" in page.inner_text("#sess-head")
        assert page.locator("#turns audio").count() == 6
        src = page.get_attribute("#turn-2 audio >> nth=1", "src")
        assert src == "/api/sessions/s-handoff-voice/audio/2/agent"
        ok = page.evaluate("fetch('%s').then(r => r.ok && r.headers.get('content-type'))" % src)
        assert ok == "audio/wav"
        assert "고객이 끼어듦" in page.inner_text("#turn-2") and "상담원에게 넘김" in page.inner_text("#turn-3")
        assert page.locator("#turn-2.over").count() == 1
        page.click("#turn-2 details.more > summary")
        spans = page.inner_text("#turn-2 .spans")
        for part in ("말 끝 판단", "음성 인식", "들은 글", "음성 합성", "끼어들기", "10자까지 들림"):
            assert part in spans, part
        page.click("#turn-3 details.more > summary")
        assert "넘김 조건 large_refund" in page.inner_text("#turn-3 .span[data-kind=handoff]")
        shot(page, "monitor_voice_handoff")
    with open_page("/monitor?session=s-refund-ok") as page:      # a live (not test) conversation never offers audio
        page.wait_for_selector("#turn-5")
        assert page.locator("#turns audio").count() == 0


def test_live_update_and_analysis_on_demand():
    reset()
    with open_page("/monitor?session=s-live") as page:
        page.wait_for_function("document.querySelectorAll('#turns .turn').length === 2")
        page.wait_for_selector("#live-state.ok")
        page.wait_for_selector("text=아직 분석이 없습니다")
        assert "진행 중" in page.inner_text("#sess-head")
        post("/mock/turn?session=s-live")                         # a turn arrives while the page is open
        page.wait_for_function("document.querySelectorAll('#turns .turn').length === 3")
        assert "환불이 접수되었습니다" in page.inner_text("#turn-3")
        page.wait_for_function("document.querySelector(\".sess[data-sid='s-live']\").textContent.includes('3턴')")
        shot(page, "monitor_live")
        page.click("#run-analysis")
        page.wait_for_selector("#analysis .criteria")
        assert "판정 불가" in page.inner_text("#analysis")
        page.wait_for_function("!document.querySelector(\".sess[data-sid='s-live']\").textContent.includes('분석 없음')")


def test_new_session_from_the_conversation_page_appears():
    reset()
    with open_page("/monitor") as monitor:
        monitor.wait_for_selector("#live-state.ok")
        with open_page("/") as talk:
            talk.wait_for_selector("#toggle:not([disabled])")
            talk.select_option("#agent", "shop-support")
            talk.wait_for_selector("#toggle:not([disabled])")
            talk.click("#toggle")
            talk.wait_for_selector("#text:not([disabled])")
            sid = talk.inner_text("#s-session")
            monitor.wait_for_selector(f".sess[data-sid='{sid}']")
            talk.fill("#text", "A1001 1234")
            talk.press("#text", "Enter")
            monitor.wait_for_function(f"document.querySelector(\".sess[data-sid='{sid}']\").textContent.includes('1턴')")


def test_filters_and_empty_states():
    reset()
    with open_page("/monitor") as page:
        page.wait_for_selector(".sess")
        page.select_option("#f-status", "handoff")
        page.wait_for_function("document.querySelectorAll('.sess').length === 1")
        page.select_option("#f-status", "")
        page.wait_for_function("document.querySelectorAll('.sess').length === 9")
        page.select_option("#f-kind", "sim")
        assert page.locator(".sess").count() == 1 and "가상 고객 refund_small" in page.inner_text(".sess")
        page.select_option("#f-channel", "avatar")
        page.wait_for_selector("text=조건에 맞는 대화가 없습니다")
        shot(page, "monitor_filter_empty")
    empty = lambda p: p.route("**/api/sessions?**", lambda r: r.fulfill(status=200, content_type="application/json", body='{"sessions": []}'))
    with open_page("/monitor", prepare=empty) as page:
        page.wait_for_selector("text=아직 기록된 대화가 없습니다")
        page.wait_for_selector("text=볼 대화가 없습니다")
        shot(page, "monitor_empty")
    with open_page("/monitor?session=s-gone") as page:
        page.wait_for_selector("text=이 대화를 찾을 수 없습니다")


def test_trend_view():
    reset()
    with open_page("/monitor") as page:
        page.click("#tab-trend")
        page.wait_for_selector("#trend-table table")
        assert page.locator("#trend-table tbody tr").count() == 4
        row = page.inner_text("#trend-table tbody tr >> nth=1")       # shop-support v2 against v1
        for part in ("v2", "126", "1.42초", "0.47초 좋아짐", "86%", "15%p 좋아짐", "19%"):
            assert part in row, part
        assert "나빠짐" in page.inner_text("#trend-table tbody tr >> nth=2")
        assert page.locator("#trend-chart polyline").count() == 1 and page.locator("#trend-chart circle").count() == 7
        shot(page, "monitor_trend")
        page.select_option("#t-agent", "tech-trend-anchor")
        page.wait_for_function("document.querySelectorAll('#trend-table tbody tr').length === 1")
        assert page.locator("#trend-chart polyline").count() == 1


def test_unreachable_and_missing_modules():
    with open_page("/monitor", prepare=lambda p: p.route("**/api/**", lambda r: r.abort())) as page:
        page.wait_for_selector("text=서버에 연결할 수 없습니다")
        page.wait_for_selector("text=실시간 연결이 끊겨 다시 연결하는 중")
        shot(page, "monitor_unreachable")
        page.click("#tab-trend")
        page.wait_for_selector("#trend-table >> text=서버에 연결할 수 없습니다")

    def partly(p):       # trace is installed; evaluation, metrics and live notifications are not
        for pattern in ("**/api/sessions/*/analysis", "**/api/metrics**", "**/api/live", "**/api/sessions/*/save_case"):
            p.route(pattern, lambda r: r.fulfill(**ROUTE_MISSING))
    with open_page("/monitor?session=s-refund-ok", prepare=partly) as page:
        page.wait_for_function("document.querySelectorAll('#turns .turn').length === 5")
        page.wait_for_selector("text=대화 분석 기능은 아직 쓸 수 없습니다")
        page.wait_for_selector("text=실시간 알림 없음")
        shot(page, "monitor_modules_missing")
        page.click("#tab-trend")
        page.wait_for_selector("text=추세 기능은 아직 쓸 수 없습니다")
    with open_page("/monitor", prepare=lambda p: p.route("**/api/sessions?**", lambda r: r.fulfill(**ROUTE_MISSING))) as page:
        page.wait_for_selector("text=대화 기록 기능은 아직 쓸 수 없습니다")


def test_phone_width():
    reset()
    with open_page("/monitor", width=420, height=900) as page:
        page.wait_for_selector(".sess")
        assert page.locator("#sess-head").is_hidden()            # the list first, no detail opened by itself
        no_sideways_scroll(page)
        shot(page, "monitor_phone_list")
        page.click(".sess[data-sid='s-refund-ok']")
        page.wait_for_selector("#analysis .criteria")
        assert page.locator(".mon-list").is_hidden()
        no_sideways_scroll(page)
        page.click("#turn-5 details.more > summary")
        no_sideways_scroll(page)
        shot(page, "monitor_phone_session")
        page.click("text=← 대화 목록")
        assert page.locator(".mon-list").is_visible()
        page.click("#tab-trend")
        page.wait_for_selector("#trend-table table")
        no_sideways_scroll(page)
        shot(page, "monitor_phone_trend")


# ---- speech recognition quality (docs/ASR_QUALITY.md) -------------------------------------------

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as response:
        return json.loads(response.read())



def test_turn_cards_show_signals_audio_confidence_and_take_marks():
    reset()
    with open_page("/monitor?session=s-asr-voice") as page:
        page.wait_for_function("document.querySelectorAll('#turns .turn').length === 4")
        one = page.inner_text("#turn-1")
        for part in ("오인식으로 표시됨", "낮은 신뢰도", "되물음", "가져서 배달됐어요.", "실제 한 말", "떨어진 상태로 배달됐다고요",
                     "인식 Whisper", "확신도 -1.12 낮음", "소리 크기 -43.5 dBFS", "길이 2.1초", "말 끝 판단: 조용함 0.7초, 끝났을 확률 0.71"):
            assert part in one, part
        assert page.locator("#turn-1 .badge.b-misheard").count() == 1 and page.locator("#turn-1 .badge.b-suspect").count() == 2
        assert page.locator("#turn-1.misheard").count() == 1 and page.locator("#turn-2.suspect").count() == 1
        assert "같은 말 반복" in page.inner_text("#turn-2") and "불만 표현" in page.inner_text("#turn-4")
        assert "기다리라고만 함" in page.inner_text("#turn-3") and "낮은 신뢰도" in page.inner_text("#turn-3")
        # the recording sits in the same row as the recognised text
        assert page.get_attribute("#turn-1 .heard audio", "src") == "/api/sessions/s-asr-voice/audio/1/user"
        assert page.inner_text("#turn-1 .heard > span") == "가져서 배달됐어요."
        assert page.locator("#turns .heard audio").count() == 4

        page.click("#turn-3 details.more > summary")
        stt = page.inner_text("#turn-3 .span[data-kind=stt]")
        for part in ("낮은 신뢰도", "글자 확신도 -0.95", "-0.80보다 낮으면 불확실", "말소리가 아닐 확률 62%", "같은 글자가 되풀이되는 정도 0.90",
                     "발화 길이 0.32초: 0.4초보다 짧아 믿기 어려움", "소리 크기 -47.2 dBFS", "작게 들어옴", "종합: 신뢰도 낮음"):
            assert part in stt, part
        assert page.locator("#turn-3 .span[data-kind=stt] .line.low").count() == 4
        page.click("#turn-4 details.more > summary")
        assert "종합: 신뢰도 충분" in page.inner_text("#turn-4 .span[data-kind=stt]")
        assert page.locator("#turn-4 .span[data-kind=stt] .line.low").count() == 0

        page.click("#turn-2 >> text=잘못 알아들음")                   # mark here too, with what was really said
        page.wait_for_selector("#turn-2 .badge.b-misheard")
        page.fill("#turn-2 .mh-field input", "떨어진 상태로 배달됐다고요")
        page.click("#turn-2 .mh-field >> text=저장")
        page.wait_for_selector("#turn-2 .mh-actual")
        page.wait_for_function("document.querySelector(\".sess[data-sid='s-asr-voice']\").textContent.includes('오인식 표시 2턴')")
        assert any(c["id"] == "c-s-asr-voice-2" and c["expected"] == "떨어진 상태로 배달됐다고요" for c in get("/api/asr/cases")["cases"])
        assert page.locator("#turn-3 details.more[open]").count() == 1          # nothing else on the page was rebuilt
        shot(page, "asr_monitor_session")

        page.click("#turn-1 >> text=표시 취소")
        page.wait_for_function("document.querySelector('#turn-1 .badge.b-misheard') === null")
        assert get("/api/sessions/s-asr-voice")["turns"][0]["flags"]["misheard"] is False
        assert page.locator("#turn-1.suspect").count() == 1
    with open_page("/monitor?session=s-handoff-voice") as page:     # recordings from before the confidence values existed still read well
        page.wait_for_function("document.querySelectorAll('#turns .turn').length === 3")
        assert "인식 Whisper · 말 끝 판단" in page.inner_text("#turn-1 .asr-line") and page.locator("#turns .badge.b-suspect").count() == 0
        assert page.locator("#turn-1 .mh-btn").count() == 1
    with open_page("/monitor?session=s-refund-ok") as page:         # a text conversation has nothing to mishear
        page.wait_for_function("document.querySelectorAll('#turns .turn').length === 5")
        assert page.locator("#turns .mh").count() == 0 and page.locator("#turns .asr-line").count() == 0


def test_mark_on_a_server_without_the_feature_explains_itself():
    reset()
    with open_page("/monitor?session=s-asr-voice", prepare=lambda p: p.route("**/api/sessions/*/turns/*/flag", lambda r: r.fulfill(**ROUTE_MISSING))) as page:
        page.wait_for_function("document.querySelectorAll('#turns .turn').length === 4")
        page.click("#turn-4 >> text=잘못 알아들음")
        page.wait_for_selector("#turn-4 >> text=서버에 인식 품질 기능이 설치되지 않아 표시를 저장하지 못했습니다")


def test_session_list_marks_and_filters_suspected_conversations():
    reset()
    with open_page("/monitor") as page:
        page.wait_for_selector(".sess")
        row = page.inner_text(".sess[data-sid='s-asr-voice']")
        assert "오인식 표시 1턴" in row and "의심 4턴" in row
        assert page.locator(".sess .asr-mark").count() == 2          # no other fixture conversation is marked
        page.select_option("#f-asr", "any")
        assert page.locator(".sess").count() == 1 and page.get_attribute(".sess", "data-sid") == "s-asr-voice"
        shot(page, "asr_monitor_list_filter", full=False)
        page.select_option("#f-asr", "marked")
        assert page.locator(".sess").count() == 1
        page.select_option("#f-channel", "text")
        page.wait_for_selector("text=조건에 맞는 대화가 없습니다")
        page.select_option("#f-channel", "")
        page.select_option("#f-asr", "")
        assert page.locator(".sess").count() == 9


# ---- monitor: recognition quality view -----------------------------------------------------------

def test_quality_view_cases_and_comparison():
    reset()
    with open_page("/monitor") as page:
        page.click("#tab-asr")
        page.wait_for_selector("#cases .case")
        assert page.locator("#cases .case").count() == 4 and page.inner_text("#case-count") == "4건"
        first = page.locator(".case[data-case='c-s-asr-voice-1']")
        text = first.inner_text()
        for part in ("실제 한 말", "떨어진 상태로 배달됐다고요", "그때 인식", "쇼핑몰 주문 상담", "그때 인식 모델 Whisper", "길이 2.1초", "1번 턴"):
            assert part in text, part
        assert first.locator("audio").get_attribute("src") == "/api/sessions/s-asr-voice/audio/1/user"
        assert page.evaluate("fetch('/api/sessions/s-1105-voice/audio/2/user').then(r => r.headers.get('content-type'))") == "audio/wav"
        assert first.locator("mark.d-wrong").count() >= 1 and first.locator("del.d-miss").count() >= 1
        assert "아직 적지 않았습니다" in page.inner_text(".case[data-case='c-s-1109-anchor-1']")

        # the newest comparison is shown on arrival
        page.wait_for_selector("#bench-head[data-run='bench-1'][data-status='finished']")
        assert "실제 한 말이 없어 뺀 사례 1건" in page.inner_text("#bench-head")
        assert [h.strip() for h in page.locator("#bench-engines th").all_inner_texts()] == ["인식 모델", "사례 수", "글자 오류율", "완전 일치", "인식 시간 (중앙값)"]
        whisper = page.inner_text("#bench-engines tr[data-engine=whisper]")
        qwen = page.inner_text("#bench-engines tr[data-engine=qwen]")
        for part in ("Whisper", "3", "32.3%", "1 / 3 (33%)", "0.43초"):
            assert part in whisper, part
        for part in ("Qwen3-ASR", "3.2%", "가장 낮음", "2 / 3 (67%)", "0.58초"):
            assert part in qwen, part
        assert page.locator("#bench-items tbody tr").count() == 3
        row = page.locator("#bench-items tr[data-case='c-s-asr-voice-1']")
        cells = row.locator("td")
        heard = lambda i: cells.nth(i).evaluate("td => { const c = td.cloneNode(true); c.querySelectorAll('del, small').forEach(n => n.remove()); return c.textContent; }")
        assert heard(2) == "뺏어져서 배달됐다고요." and heard(3) == "떨어진 상태로 배달됐다구요"      # the engine's own words, letters in place
        assert cells.nth(2).locator("mark.d-wrong").count() >= 1 and "오류율" in cells.nth(2).inner_text()
        assert cells.nth(3).locator("mark.d-wrong").all_inner_texts() == ["구"]      # 다고요 / 다구요: one letter differs
        exact = page.locator("#bench-items tr[data-case='c-s-1105-voice-2'] td").nth(3)
        assert exact.locator("mark, del").count() == 0 and "오류율 0.0%" in exact.inner_text()
        assert page.locator("#bench-run-list tbody tr").count() == 2
        shot(page, "asr_quality")

        # an earlier comparison in which one engine was not available
        page.click("#bench-run-list tr[data-run='bench-0'] >> text=결과 보기")
        page.wait_for_selector("#bench-head[data-run='bench-0']")
        gone = page.inner_text("#bench-engines tr[data-engine=qwen]")
        assert "쓸 수 없음" in gone and "Qwen3-ASR 서버가 꺼져 있으면" in gone
        assert page.locator("#bench-engines tr.unavailable").count() == 1 and "가장 낮음" not in page.inner_text("#bench-engines")
        assert page.locator("#bench-items tbody tr").count() == 2 and "쓸 수 없음" in page.inner_text("#bench-items tbody tr >> nth=0")
        assert "Qwen3-ASR 쓸 수 없음" in page.inner_text("#bench-run-list tr[data-run='bench-0']")
        shot(page, "asr_quality_engine_unavailable")

        # run it again: progress, then the result and one more line in the history
        page.click("#bench-run")
        page.wait_for_selector("#bench-head[data-status='running']")
        assert "돌리는 중" in page.inner_text("#bench-head") and page.locator("#bench-run").is_disabled()
        shot(page, "asr_quality_running", full=False)
        page.wait_for_selector("#bench-head[data-status='finished']", timeout=10000)
        assert page.get_attribute("#bench-head", "data-run") not in ("bench-0", "bench-1")
        assert page.locator("#bench-items tbody tr").count() == 3 and "3.2%" in page.inner_text("#bench-engines tr[data-engine=qwen]")
        page.wait_for_function("document.querySelectorAll('#bench-run-list tbody tr').length === 3")
        assert not page.locator("#bench-run").is_disabled()

        # write what was really said for the case that had none, then delete a case (asked twice)
        blank = page.locator(".case[data-case='c-s-1105-voice-2']")
        blank.locator("text=삭제").click()
        blank.locator("text=취소").click()
        assert page.locator("#cases .case").count() == 4
        blank.locator("text=삭제").click()
        blank.locator("text=사례 삭제 확인").click()
        page.wait_for_function("document.querySelectorAll('#cases .case').length === 3")
        assert all(c["id"] != "c-s-1105-voice-2" for c in get("/api/asr/cases")["cases"])

        page.locator(".case[data-case='c-s-asr-voice-1'] >> text=대화에서 보기").click()      # from a case to its turn
        page.wait_for_function("document.activeElement && document.activeElement.id === 'turn-1'")
        assert page.locator("#view-asr").is_hidden() and page.get_attribute("#tab-sessions", "aria-selected") == "true"

        page.click("#tab-asr")
        page.wait_for_selector("#cases .case")
        page.select_option("#a-agent", "tech-trend-anchor")          # only a case nobody wrote the real words for: nothing to compare
        page.wait_for_function("document.querySelectorAll('#cases .case').length === 1")
        assert page.locator("#bench-run").is_disabled()


def test_quality_view_with_an_engine_down_now():
    reset()
    post("/mock/set?qwen=0")
    with open_page("/monitor?view=asr") as page:
        page.wait_for_selector("#cases .case")
        page.wait_for_selector("#bench-head[data-run='bench-1']")
        page.click("#bench-run")
        page.wait_for_function("(() => { const h = document.getElementById('bench-head'); return h && h.dataset.status === 'finished' && h.dataset.run !== 'bench-1'; })()", timeout=10000)
        assert page.locator("#bench-engines tr.unavailable[data-engine=qwen]").count() == 1
        assert "32.3%" in page.inner_text("#bench-engines tr[data-engine=whisper]")


def test_quality_view_empty_and_not_installed():
    reset()
    ok = lambda body: (lambda r: r.fulfill(status=200, content_type="application/json", body=body))

    def empty(p):
        p.route("**/api/asr/cases**", ok('{"cases": []}'))
        p.route("**/api/asr/bench", ok('{"runs": []}'))
    with open_page("/monitor?view=asr", prepare=empty) as page:
        page.wait_for_selector("text=아직 인식 사례가 없습니다")
        assert "잘못 알아들음" in page.inner_text("#cases") and "실제로 한 말" in page.inner_text("#cases")
        page.wait_for_selector("text=아직 비교 결과가 없습니다")
        page.wait_for_selector("text=아직 비교를 돌린 적이 없습니다")
        assert page.locator("#bench-run").is_disabled()
        shot(page, "asr_quality_empty")
    with open_page("/monitor?view=asr", prepare=lambda p: p.route("**/api/asr/**", lambda r: r.fulfill(**ROUTE_MISSING))) as page:
        page.wait_for_selector("text=인식 품질 기능은 아직 쓸 수 없습니다")
        page.wait_for_selector("#bench >> text=인식 모델 비교 기능은 아직 쓸 수 없습니다")
        assert page.locator("#bench-run").is_disabled()
        shot(page, "asr_quality_missing")
        page.click("#tab-sessions")                                  # the rest of the monitor is not affected
        page.wait_for_selector(".sess")
    with open_page("/monitor?view=asr", prepare=lambda p: p.route("**/api/**", lambda r: r.abort())) as page:
        page.wait_for_selector("#cases >> text=서버에 연결할 수 없습니다")


# ---- monitor: trend ------------------------------------------------------------------------------

def test_trend_shows_recognition_quality_and_empty_waits():
    reset()
    with open_page("/monitor?view=trend") as page:
        page.wait_for_selector("#asr-by-version")
        tiles = page.inner_text("#asr-tiles")
        for part in ("673", "음성 턴", "15%", "인식 의심", "104턴", "3.7%", "사람이 표시한 오인식", "되물음", "같은 말 반복", "낮은 신뢰도", "22", "기다리라고만 한 턴"):
            assert part in tiles, part
        assert page.locator("#asr-by-version tbody tr").count() == 4
        v2 = page.inner_text("#asr-by-version tbody tr >> nth=1")     # shop-support v2 against v1
        for part in ("v2", "310", "17%", "7.9%p 좋아짐", "3.9%", "9 (1.6%)"):
            assert part in v2, part
        assert page.locator("#asr-by-engine tbody tr").count() == 2
        assert "Whisper" in page.inner_text("#asr-by-engine tr[data-engine=whisper]") and "470" in page.inner_text("#asr-by-engine tr[data-engine=whisper]")
        assert "Qwen3-ASR" in page.inner_text("#asr-by-engine tr[data-engine=qwen]") and "203" in page.inner_text("#asr-by-engine tr[data-engine=qwen]")
        assert page.locator("#trend-table tbody tr").count() == 4       # the table that was there is untouched
        shot(page, "asr_trend")
        page.select_option("#t-agent", "tech-trend-anchor")
        page.wait_for_function("document.querySelectorAll('#asr-by-engine tbody tr').length === 1")
        assert "203" in page.inner_text("#asr-tiles")

    def old_server(p):                                               # metrics without the new blocks
        def strip(route):
            body = route.fetch().json()
            for key in ("asr", "empty_wait_turns", "empty_wait_rate", "by_stt_engine"):
                body.pop(key, None)
            for row in body["by_version"]:
                for key in ("asr", "empty_wait_turns", "empty_wait_rate"):
                    row.pop(key, None)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        p.route("**/api/metrics**", strip)
    with open_page("/monitor?view=trend", prepare=old_server) as page:
        page.wait_for_selector("#trend-table table")
        page.wait_for_selector("text=인식 품질 지표는 아직 쓸 수 없습니다")



def test_asr_phone_width():
    reset()
    with open_page("/monitor?session=s-asr-voice", width=420, height=900) as page:
        page.wait_for_function("document.querySelectorAll('#turns .turn').length === 4")
        page.click("#turn-3 details.more > summary")
        page.click("#turn-2 >> text=잘못 알아들음")
        page.wait_for_selector("#turn-2 .mh-field input")
        no_sideways_scroll(page)
        shot(page, "asr_phone_session")
        page.click("#tab-asr")
        page.wait_for_selector("#bench-items")
        no_sideways_scroll(page)
        shot(page, "asr_phone_quality")
        page.click("#tab-trend")
        page.wait_for_selector("#asr-by-engine")
        no_sideways_scroll(page)
        shot(page, "asr_phone_trend")
