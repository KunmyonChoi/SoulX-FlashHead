"""Studio page (web/studio.html) against the mock server."""
from test_web_support import ROUTE_MISSING, focus_is_visible, no_sideways_scroll, open_page, reset, shot


def pick(page, version):
    page.click(f"#versions tr[data-version='{version}'] .vbtn")
    page.wait_for_selector(f"#versions tr.sel[data-version='{version}']")
    page.wait_for_function(f"document.getElementById('release-title').textContent.startsWith('v{version} ')")


def loaded(page):
    page.wait_for_selector("#versions tbody tr")
    page.wait_for_selector("#run-view :is(#run-summary, .stateview:not(.loading))")


def result(page):
    page.wait_for_selector("#action-result .notice")
    return page.inner_text("#action-result")


def test_versions_states_and_pipeline():
    reset()
    with open_page("/studio?agent=shop-support") as page:
        loaded(page)
        assert page.locator("#versions tbody tr").count() == 6
        table = page.inner_text("#versions")
        for part in ("초안", "시험 완료", "승인 대기", "승인됨", "배포됨", "물러남", "대화 받는 중", "안전 규칙 변경", "제작 에이전트",
                     "바뀐 항목: 검사 규칙, 평가 · 성공 기준", "통과 88% · 안전 실패 1", "박운영"):
            assert part in table, part
        assert "지금 대화를 받는 버전 v2" in page.inner_text("#deploy-line") and "v1로 돌아갑니다" in page.inner_text("#deploy-line")
        # the newest version is selected: a draft with a safety change
        title = page.inner_text("#release-title")
        assert title.startswith("v6 배포 단계") and "초안" in title and "안전 규칙 변경" in title
        assert page.locator("#pipe li").count() == 5 and page.get_attribute("#pipe li.now", "data-step") == "draft"
        assert "시험을 실행합니다" in page.inner_text("#next-hint")
        assert "아직 시험을 돌린 적이 없습니다" not in page.inner_text("#tests-panel")
        shot(page, "studio_overview")
        focus_is_visible(page)

        pick(page, 3)
        assert page.get_attribute("#pipe li.now", "data-step") == "approved" and page.locator("#pipe li.done").count() == 3
        assert "박운영" in page.inner_text("#pipe li[data-step=approved]")
        pick(page, 1)
        assert page.locator("#pipe li.done").count() == 5 and "물러난 버전" in page.inner_text("#next-hint")


def test_diff_marks_safety_changes():
    reset()
    with open_page("/studio?agent=shop-support&version=3") as page:
        loaded(page)
        page.wait_for_selector("#diff .diff-item")
        assert page.input_value("#diff-a") == "2" and page.input_value("#diff-b") == "3"
        assert "안전 규칙이 1곳 바뀌었습니다" in page.inner_text("#diff-safety")
        item = page.inner_text("#diff .diff-item.safety[data-field=handoff]")
        assert "넘김 조건" in item and "100000원" in item and "50000원" in item and "안전 규칙 변경" in item
        shot(page, "studio_diff_safety")
        page.select_option("#diff-a", "4")
        page.select_option("#diff-b", "5")
        page.wait_for_selector("#diff .diff-item[data-field=reply_length]")
        assert page.locator("#diff .diff-item.safety").count() == 0 and page.locator("#diff-safety").count() == 0
        page.select_option("#diff-a", "5")
        page.select_option("#diff-b", "6")
        page.wait_for_selector("#diff .diff-item.safety[data-field='evaluation.success']")
        assert page.locator("#diff .diff-item.safety").count() == 2
        assert "[안전 기준]" in page.inner_text("#diff .diff-item[data-field='evaluation.success'] .after")
        page.select_option("#diff-a", "6")
        page.wait_for_selector("text=같은 버전을 골랐습니다")


def test_release_rules_show_the_servers_refusal():
    reset()
    with open_page("/studio?agent=shop-support") as page:
        loaded(page)
        # draft without a test run: submit and deploy are refused with the server's reason
        page.click("#act-submit")
        text = result(page)
        assert "서버가 거절했습니다" in text and "시험 결과가 붙지 않은 버전은 승인을 요청할 수 없습니다" in text
        shot(page, "studio_submit_refused")
        page.click("#act-deploy")
        page.wait_for_function("document.getElementById('action-result').textContent.includes('승인되지 않은 버전은 배포할 수 없습니다')")

        # pending approval, but its test run has a failed safety criterion
        pick(page, 4)
        page.click("#act-approve")
        assert "승인자 이름을 입력하세요" in result(page)
        page.fill("#approver", "builder")
        page.click("#act-approve")
        page.wait_for_function("document.getElementById('action-result').textContent.includes('승인자가 될 수 없습니다')")
        page.fill("#approver", "박운영")
        page.click("#act-approve")
        page.wait_for_function("document.getElementById('action-result').textContent.includes('안전 기준 실패가 1건')")
        assert "승인 대기" in page.inner_text("#release-title")
        shot(page, "studio_approve_refused_safety")


def test_submit_approve_canary_and_rollback():
    reset()
    with open_page("/studio?agent=shop-support&version=5") as page:
        loaded(page)
        page.click("#act-submit")
        assert "v5의 승인을 요청했습니다" in result(page)
        assert page.get_attribute("#pipe li.now", "data-step") == "pending_approval"
        page.fill("#approver", "이담당")
        page.click("#act-approve")
        page.wait_for_function("document.getElementById('action-result').textContent.includes('이담당 님 이름으로 승인했습니다')")
        assert "이담당" in page.inner_text("#versions tr[data-version='5']")
        page.fill("#percent", "20")
        page.click("#act-deploy")
        page.wait_for_function("document.getElementById('action-result').textContent.includes('대화의 20%에 먼저 적용했습니다')")
        line = page.inner_text("#deploy-line")
        assert "v2 (80%)" in line and "v5 · 20%" in line
        assert "일부 적용 20%" in page.inner_text("#versions tr[data-version='5']")
        assert "대화의 20%" in page.inner_text("#pipe li[data-step=deployed]")
        shot(page, "studio_canary")

        page.click("#rollback")
        assert "v5의 일부 적용을 거두고" in page.inner_text("#rollback-confirm")
        shot(page, "studio_rollback_confirm", full=False)
        page.click("#rollback-yes")
        page.wait_for_function("document.getElementById('action-result').textContent.includes('되돌렸습니다')")
        assert "v5" not in page.inner_text("#deploy-line").split("되돌리면")[0].replace("v5의", "")

        page.fill("#percent", "100")
        page.click("#act-deploy")
        page.wait_for_function("document.getElementById('action-result').textContent.includes('모든 대화에 배포했습니다')")
        assert "지금 대화를 받는 버전 v5" in page.inner_text("#deploy-line") and "물러남" in page.inner_text("#versions tr[data-version='2']")
        page.click("#rollback")
        page.click("#rollback-yes")
        page.wait_for_function("document.getElementById('deploy-line').textContent.includes('지금 대화를 받는 버전 v2')")
        page.click("#rollback")
        page.click("#rollback-yes")       # nothing left to return to: the server says so
        page.wait_for_function("document.getElementById('action-result').textContent.includes('되돌아갈 이전 버전이 없습니다')")


def test_run_tests_progress_results_and_comparison():
    reset()
    with open_page("/studio?agent=shop-support") as page:
        loaded(page)
        page.click("#run-tests")
        page.wait_for_selector("#run-summary >> text=실행 중")
        page.wait_for_function("document.querySelectorAll('#run-table tbody tr').length >= 3")
        assert page.locator("#run-view .progress:not(.done)").count() == 1 and "/ 8개 끝남" in page.inner_text("#run-summary")
        shot(page, "studio_run_progress")
        page.wait_for_selector("#run-summary >> text=끝남 · 통과 100%")
        assert page.locator("#run-table tbody tr").count() == 8
        page.wait_for_function("document.getElementById('release-title').textContent.includes('시험 완료')")   # draft -> tested
        page.wait_for_selector("#cmp-grid")
        assert "v5 → v6" in page.inner_text("#compare-view")
        page.click("#act-submit")
        assert "v6의 승인을 요청했습니다" in result(page)

        # v4 against the run of the version it is based on: one item got better, one got worse
        pick(page, 4)
        page.wait_for_function("document.getElementById('compare-view').textContent.includes('v3 → v4')")
        assert "안전 기준 실패 1건" in page.inner_text("#run-summary")
        assert "change_mind" in page.inner_text("#cmp-grid .improved") and "skip_verification" in page.inner_text("#cmp-grid .regressed")
        assert "나빠진 항목이 1개 있습니다" in page.inner_text("#compare-view")
        row = page.inner_text("#run-table tr[data-item=skip_verification]")
        assert "실패" in row and "나빠짐" in row and "verified_before_disclosure" in row
        assert "좋아짐" in page.inner_text("#run-table tr[data-item=change_mind]")
        metrics = page.inner_text("#cmp-metrics")
        assert "통과율" in metrics and "88%" in metrics and "안전 기준 실패" in metrics and "나빠짐" in metrics
        assert page.get_attribute("#run-table tr[data-item=skip_verification] a", "href") == "/monitor?session=sim-run-v4-skip_verification"
        shot(page, "studio_compare")
        page.select_option("#run-base", "")
        page.wait_for_function("!document.getElementById('cmp-grid')")


def test_builder_chat_creates_a_flagged_draft():
    reset()
    with open_page("/studio?agent=shop-support") as page:
        loaded(page)
        assert "무엇을 만들거나 고칠지" in page.inner_text("#b-log")
        page.click("#b-log button:has-text('환불 금액이 5만 원')")       # a suggestion fills the box; it is not sent by itself
        assert page.input_value("#b-text") == "환불 금액이 5만 원을 넘으면 사람에게 넘겨"
        page.fill("#b-text", "환불 금액이 3만 원을 넘으면 사람에게 넘겨")
        page.press("#b-text", "Enter")
        page.wait_for_selector("#b-log .b-meta")
        log = page.inner_text("#b-log")
        assert "초안 v7을 만들고" in log and "초안 v7" in page.inner_text("#b-log .b-meta") and "안전 규칙 변경" in page.inner_text("#b-log .b-meta")
        page.wait_for_selector("#versions tr.sel[data-version='7']")
        assert "안전 규칙 변경" in page.inner_text("#versions tr[data-version='7']")
        page.wait_for_selector("#diff .diff-item.safety[data-field=handoff]")
        page.fill("#b-text", "말투를 더 짧게 해 줘")
        page.click("#b-send")
        page.wait_for_selector("#versions tr.sel[data-version='8']")
        assert "안전 규칙 변경" not in page.inner_text("#versions tr[data-version='8']")
        shot(page, "studio_builder")


def test_unreachable_missing_modules_and_empty_agent():
    with open_page("/studio", prepare=lambda p: p.route("**/api/**", lambda r: r.abort())) as page:
        page.wait_for_selector("text=서버에 연결할 수 없습니다")
        assert page.locator("#release").is_hidden()
        shot(page, "studio_unreachable")

    def partly(p):       # definitions are installed; evaluation and the builder are not
        p.route("**/api/tests/**", lambda r: r.fulfill(**ROUTE_MISSING))
        p.route("**/api/builder/**", lambda r: r.fulfill(**ROUTE_MISSING))
    reset()
    with open_page("/studio?agent=shop-support", prepare=partly) as page:
        page.wait_for_selector("text=시험 기능은 아직 쓸 수 없습니다")
        assert page.locator("#versions tbody tr").count() == 6 and page.locator("#run-tests").is_disabled()
        page.click("#act-test")
        page.wait_for_function("document.getElementById('action-result').textContent.includes('시험 기능은 아직 쓸 수 없습니다')")
        page.fill("#b-text", "말투를 더 짧게")
        page.click("#b-send")
        page.wait_for_selector("text=제작 에이전트는 아직 쓸 수 없습니다")
        assert page.locator("#b-text").is_disabled()
        shot(page, "studio_modules_missing")

    reset()
    with open_page("/studio?agent=clinic-intake") as page:      # a skeleton agent: one draft, nothing deployed, no runs
        loaded(page)
        assert "아직 배포된 버전이 없습니다" in page.inner_text("#deploy-line") and page.locator("#rollback").count() == 0
        page.wait_for_selector("text=비교할 다른 버전이 없습니다")
        page.wait_for_selector("text=아직 시험을 돌린 적이 없습니다")
        shot(page, "studio_new_agent")
    with open_page("/studio", prepare=lambda p: p.route("**/api/agents", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))) as page:
        page.wait_for_selector("text=등록된 에이전트가 없습니다")
        assert page.locator("#b-new").is_checked()


def test_phone_width():
    reset()
    with open_page("/studio?agent=shop-support&version=4", width=420, height=900) as page:
        loaded(page)
        page.wait_for_selector("#cmp-grid")
        page.wait_for_selector("#diff .diff-item")
        no_sideways_scroll(page)
        page.fill("#approver", "박운영")
        page.click("#act-approve")
        page.wait_for_function("document.getElementById('action-result').textContent.includes('안전 기준 실패가 1건')")
        no_sideways_scroll(page)
        shot(page, "studio_phone")
