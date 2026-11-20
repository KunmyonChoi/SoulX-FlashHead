from agentkit.tools.consent import read_consent



def test_a_changed_recipient_or_content_is_a_new_request_not_consent():
    # review 2026-11-20: these were read as "yes", and the unchanged mail would have gone out
    for said in ["이서연한테 보내", "이서연한테 보내 줘", "참조에 박지훈 추가해 줘", "제목에 긴급 넣어", "박지훈 추가해 줘",
                 "오후 세 시로 잡아 줘"]:
        assert read_consent(said) == "unclear", said
    for said in ["보내.", "응, 보내", "그대로 보내 줘", "등록해", "네 추가해 줘", "잡아 줘", "네 환불해 주세요"]:
        assert read_consent(said) == "yes", said
