"""Masking rules of ARCHITECTURE.md section 8. Every case listed in docs/notes/trace.md as
"caught" has a line here; the known misses are pinned down at the bottom so that the notes
stay honest."""
import pytest

from agentkit.trace import get_masker, mask_text

CAUGHT = [
    # phone numbers, written forms
    ("010-1234-5678로 연락 주세요", "1234-5678", "[전화번호]"),
    ("제 번호는 01012345678입니다", "01012345678", "[전화번호]"),
    ("010 1234 5678", "1234 5678", "[전화번호]"),
    ("010.1234.5678", "1234.5678", "[전화번호]"),
    ("011-234-5678", "234-5678", "[전화번호]"),
    ("+82 10-1234-5678", "1234-5678", "[전화번호]"),
    ("매장 전화는 02-345-6789", "345-6789", "[전화번호]"),
    ("031-123-4567", "123-4567", "[전화번호]"),
    ("070-1234-5678", "1234-5678", "[전화번호]"),
    ("0507-1234-5678", "1234-5678", "[전화번호]"),
    ("공일공 일이삼사 오육칠팔이에요", "일이삼사", "[전화번호]"),
    # the last four digits, spoken
    ("뒤 네 자리는 1234", "1234", "[전화번호]"),
    ("뒷자리 1234", "1234", "[전화번호]"),
    ("휴대폰 뒷자리 1234입니다", "1234", "[전화번호]"),
    ("뒷번호가 5678이에요", "5678", "[전화번호]"),
    ("휴대폰 뒤 4자리는 9012예요", "9012", "[전화번호]"),
    ("전화번호 끝자리는 4321입니다", "4321", "[전화번호]"),
    ("마지막 네 자리 7788", "7788", "[전화번호]"),
    ("1234가 뒷자리예요", "1234", "[전화번호]"),
    ("뒷자리는 일이삼사", "일이삼사", "[전화번호]"),
    ("the last four digits are 4321", "4321", "[전화번호]"),
    # e-mail
    ("메일은 minsu.kim@example.co.kr 입니다", "minsu.kim", "[이메일]"),
    ("a+b@test.io", "a+b@test.io", "[이메일]"),
    # resident registration number
    ("주민번호 900101-1234567", "1234567", "[주민번호]"),
    ("9001011234567", "9001011234567", "[주민번호]"),
    # card numbers
    ("카드번호 1234-5678-9012-3456", "5678-9012", "[카드번호]"),
    ("카드 1234 5678 9012 3456", "5678 9012", "[카드번호]"),
    ("1234567890123456", "1234567890123456", "[카드번호]"),
    ("3712 345678 95006", "345678", "[카드번호]"),
    # order numbers
    ("주문번호는 A1001이에요", "A1001", "[주문번호]"),
    ("ab12345 주문이요", "ab12345", "[주문번호]"),
    ("ORD20261 건", "ORD20261", "[주문번호]"),
    ("주문번호 20261105-0001 조회", "20261105-0001", "[주문번호]"),
    # addresses
    ("서울특별시 강남구 테헤란로 152 3층으로 보내주세요", "테헤란로", "[주소]"),
    ("서울시 강남구 역삼동 123-45번지", "역삼동", "[주소]"),
    ("경기도 성남시 분당구 정자일로 95 101동 1203호", "정자일로", "[주소]"),
    ("부산 해운대구 센텀중앙로 79", "센텀중앙로", "[주소]"),
    ("강남구 테헤란로 152", "테헤란로", "[주소]"),
    # names, in the listed phrasings
    ("제 이름은 김민수입니다", "김민수", "[이름]"),
    ("이름은 김민수예요", "김민수", "[이름]"),
    ("성함은 박지영이에요", "박지영", "[이름]"),
    ("저는 이서준이라고 합니다", "이서준", "[이름]"),
    ("김민수 고객님, 안녕하세요", "김민수", "[이름]"),
    ("박지영 회원님의 주문입니다", "박지영", "[이름]"),
    ("홍길동님 주문입니다", "홍길동", "[이름]"),
    ("my name is John Smith", "John", "[이름]"),
    # key: value text (a tool result pasted into a prompt)
    ('{"customer_name": "김민수", "amount": 30000}', "김민수", "[이름]"),
    ('{"phone_last4": "1234"}', "1234", "[가려짐]"),
    ("이름: 최영희, 전화번호: 010-2222-3333", "최영희", "[이름]"),
]


@pytest.mark.parametrize("text,raw,marker", CAUGHT)
def test_caught(text, raw, marker):
    masked = mask_text(text)
    assert raw not in masked, masked
    assert marker in masked, masked


SURVIVES = [
    "30000원 환불해 드릴게요",
    "총 1,250,000원이고 3개입니다",
    "결제 금액은 129000원입니다",
    "2026-11-17 주문",
    "2026년 11월 3일 15시 30분",
    "11월 17일에 배송됩니다",
    "20261117",
    "2026.11.17 기준",
    "뒤 네 자리는 5000원",                     # an amount after the cue is not a phone number
    "서울 강남구 역삼동 20도 맑음",            # weather, not an address
    "안녕하세요 고객님",
    "그럼요 고객님",
    "선생님 고객님 기사님",
    "session 9b1deb4d-3b7d-4bad-a123-2b0d7b3dcb6d",   # uuid pieces are not order numbers
    "gemma4:26b get_order refund_small",
    "주문번호를 알려 주세요",
    "휴대폰 뒤 4자리를 말씀해 주세요",
    "배송까지 3일, 1588 상담",
]


@pytest.mark.parametrize("text", SURVIVES)
def test_survives(text):
    assert mask_text(text) == text


def test_context_is_kept():
    assert mask_text("뒤 네 자리는 1234") == "뒤 네 자리는 [전화번호]"
    assert mask_text("주문번호는 A1001이고 30000원입니다") == "주문번호는 [주문번호]이고 30000원입니다"
    assert mask_text("제 이름은 김민수입니다") == "제 이름은 [이름]입니다"


def test_mask_is_a_deep_copy_and_follows_pii_paths():
    masker = get_masker()
    value = {
        "input": {"arguments": {"order_id": "A1001", "phone_last4": "1234", "note": "빨리요"}},
        "output": {"ok": True, "text": "김민수 고객님 확인되었습니다",
                   "data": {"customer_name": "김민수", "address": "어딘가 먼 곳", "amount": 30000,
                            "items": [{"sku": "x", "memo": "비밀"}, {"sku": "y", "memo": "비밀2"}]}},
    }
    masked = masker.mask(value, ["input.arguments.phone_last4", "output.data.address",
                                 "output.data.items.memo", "output.data.missing"])
    assert value["input"]["arguments"]["phone_last4"] == "1234"          # the original is untouched
    assert masked["input"]["arguments"] == {"order_id": "[주문번호]", "phone_last4": "[가려짐]", "note": "빨리요"}
    assert masked["output"]["data"]["address"] == "[가려짐]"
    assert masked["output"]["data"]["customer_name"] == "[이름]"          # by the key's name, no path given
    assert masked["output"]["data"]["amount"] == 30000
    assert [i["memo"] for i in masked["output"]["data"]["items"]] == ["[가려짐]", "[가려짐]"]
    assert masked["output"]["text"] == "[이름] 고객님 확인되었습니다"
    assert masked["output"]["ok"] is True


def test_name_field_marked_by_contract_gets_name_marker():
    masked = get_masker().mask({"output": {"data": {"buyer_name": "김민수"}}}, ["output.data.buyer_name"])
    assert masked["output"]["data"]["buyer_name"] == "[이름]"


def test_mask_handles_odd_values():
    masker = get_masker()

    class Odd:
        def __str__(self):
            return "연락처는 010-1234-5678"

    out = masker.mask({"a": (1, 2), "b": b"\x00\x01", "c": Odd(), "d": None, 5: "A1001"})
    assert out == {"a": [1, 2], "b": "[bytes 2]", "c": "연락처는 [전화번호]", "d": None, "5": "[주문번호]"}
    assert masker.mask("010-1234-5678") == "[전화번호]"
    assert masker.mask(None) is None


# Known misses, written down in docs/notes/trace.md. If one of these starts being caught,
# move it to CAUGHT and update the notes.
MISSED = [
    "김민수입니다",                      # a bare name with no cue
    "저는 김민수입니다",                 # "저는 X입니다" is not treated as a name (X is usually not one)
    "민수라고 불러 주세요",
    "1234요",                            # four digits with no cue (caught only via pii_paths, see recorder test)
    "테헤란로 152 3층",                  # a road and number without 구/군/시
    "공일공에 일이삼사에 오육칠팔",       # digits read out with particles in between
    "kim at example dot com",
    "주문 A-1001",                       # a hyphen inside the order number
]


@pytest.mark.parametrize("text", MISSED)
def test_known_misses(text):
    assert mask_text(text) == text


# Known over-masking, also in the notes.
def test_known_false_positives():
    assert mask_text("RTX4090 그래픽카드") == "[주문번호] 그래픽카드"
    assert mask_text("에어버스 A380, 코덱 H264") == "에어버스 [주문번호], 코덱 [주문번호]"
    assert mask_text("2024 2025 2026 2027") == "[카드번호]"
    assert mask_text("시각 1763371234567") == "시각 [카드번호]"
    assert mask_text("단골 고객님") == "[이름] 고객님"
    assert mask_text("전화번호: 나중에 알려 드릴게요") == "전화번호: [가려짐]"


def test_partial_catches():
    assert mask_text("김 민수 고객님") == "김 [이름] 고객님"          # the family name stays
