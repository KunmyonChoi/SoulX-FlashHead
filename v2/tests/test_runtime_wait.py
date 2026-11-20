"""The framework rule against telling the customer to wait while nothing is running."""
import pytest

from agentkit.runtime.guards import is_empty_wait


@pytest.mark.parametrize("text", [
    "확인을 위해 잠시만 기다려 주세요.",
    "먼저 상품명과 금액, 배송 상태를 확인하겠습니다. 잠시만 기다려 주세요.",
    "환불 사유를 상품 분실로 접수해 드릴까요? 확인을 위해 잠시만 기다려 주세요.",
    "조회해 보겠습니다.",
    "Let me check that for you.",
])
def test_reply_ending_on_a_wait_promise_is_caught(text):
    assert is_empty_wait(text)


@pytest.mark.parametrize("text", [
    "배송 상태를 확인해 드릴게요. 주문번호와 휴대폰 번호 뒤 4자리를 말씀해 주시겠어요?",
    "네, 확인되었습니다.",
    "환불 접수를 도와드리겠습니다. 주문번호를 말씀해 주세요.",
    "주문번호를 확인해 주시겠어요?",
    "무선 이어폰 32000원 환불을 접수할까요?",
    "죄송합니다. 주문번호와 휴대폰 번호 뒤 4자리를 다시 확인해 주시면 바로 환불 가능 여부를 확인해 드릴게요.",
    "주문번호를 말씀해 주시면 조회해 보겠습니다.",
    "",
])
def test_reply_that_hands_the_turn_back_is_left_alone(text):
    assert not is_empty_wait(text)
