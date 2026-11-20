"""Masking of personal data before anything is stored (PRD R-OBS-3/4, ARCHITECTURE section 8).

Three layers, applied in this order by the recorder's writer thread:

1. explicit paths (`pii_paths`) given with a span, plus dict keys whose NAME says personal data
   (phone_last4, customer_name, address, ...). The raw values found there are remembered per
   session (in memory only) and replaced wherever they show up again in that session.
2. values learned in layer 1, replaced as plain strings in every later record of the session.
3. regular expressions over every string (`mask_text`).

Regex masking is a heuristic. What it catches and what it misses is listed in
v2/docs/notes/trace.md; do not assume more than that.
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Any, Iterable, Optional

from pydantic import BaseModel

M_PHONE = "[전화번호]"
M_EMAIL = "[이메일]"
M_RRN = "[주민번호]"
M_CARD = "[카드번호]"
M_ORDER = "[주문번호]"
M_ADDRESS = "[주소]"
M_NAME = "[이름]"
M_HIDDEN = "[가려짐]"
MARKERS = (M_PHONE, M_EMAIL, M_RRN, M_CARD, M_ORDER, M_ADDRESS, M_NAME, M_HIDDEN)

# ------------------------------------------------------------------------------------------------
# patterns
# ------------------------------------------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

# 900101-1234567 (also with spaces around the hyphen)
_RRN = re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\s?-\s?[1-8]\d{6}(?!\d)")
# 13 digits in a row that read as birth date + sex digit
_RRN_PLAIN = re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[1-8]\d{6}(?!\d)")

# 13 to 16 digits, in a row or in groups of four (space or hyphen); 4-6-5 (Amex)
_CARD = re.compile(r"(?<![\d-])(?:\d{4}[ -]?){3}\d{1,4}(?![\d-])|(?<![\d-])\d{4}[ -]\d{6}[ -]\d{5}(?![\d-])")

_MOBILE = re.compile(r"(?<!\d)(?:\+?82[-. ]?0?|0)1[016789][-. ]?\d{3,4}[-. ]?\d{4}(?!\d)")
_LANDLINE = re.compile(r"(?<!\d)\(?0(?:2|[3-6][1-5]|70|50\d)\)?[-. ]?\d{3,4}[-. ]?\d{4}(?!\d)")
_KDIGIT = "공영일이삼사오육칠팔구"
# 공일공 일이삼사 오육칠팔
_MOBILE_SPOKEN = re.compile(r"공\s?일\s?[공일육칠팔구](?:[\s,-]*[" + _KDIGIT + r"]){7,8}")

_LAST4_CUE = (
    r"(?:뒷\s*자리|뒷\s*번호|뒤\s*(?:에\s*)?(?:번호\s*)?(?:네|4|사)\s*자리|뒤\s*자리|"
    r"끝\s*(?:번호|자리|(?:네|4)\s*자리)|마지막\s*(?:네|4)\s*자리|"
    r"[Ll]ast\s+(?:four|4)(?:\s+digits)?)"
)
_NOT_QUANTITY = r"(?!\s*(?:원|년|월|일|시|분|개|번째|%))"
# "뒤 네 자리는 1234", "뒷자리 1234", "뒷자리가 일이삼사"
_LAST4 = re.compile(
    _LAST4_CUE + r"(?:\s*(?:네|4)\s*자리)?[^\d\n]{0,12}?(?<!\d)"
    r"(\d{4}|(?:[" + _KDIGIT + r"]\s?){3}[" + _KDIGIT + r"])(?!\d)" + _NOT_QUANTITY
)
# "1234가 뒷자리예요"
_LAST4_REVERSED = re.compile(
    r"(?<![\dA-Za-z])(\d{4})(?!\d)" + _NOT_QUANTITY + r"(?=[^\d\n]{0,8}?(?:뒷\s*자리|뒷\s*번호|뒤\s*(?:네|4)\s*자리))"
)

# letters 1-3 + digits 3 or more (A1001). Not next to "-" or "_" so that pieces of uuids and
# snake_case identifiers are left alone.
_ORDER = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z]{1,3}\d{3,}(?![A-Za-z0-9_-])")
# "주문번호는 20261105-0001": whatever follows the words 주문번호
_ORDER_CONTEXT = re.compile(r"(주문\s*번호(?:는|가|은|이|:)?\s*)([A-Za-z0-9][A-Za-z0-9-]{3,})")

_SIDO = (
    r"(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주|"
    r"충청[남북]|전라[남북]|경상[남북])(?:특별자치시|특별자치도|특별시|광역시|시|도)?"
)
_SGG = r"[가-힣]{1,5}(?:시|군|구)"
_ROAD = r"[가-힣0-9]{1,10}(?:로|길|동|읍|면|리|가)"
_ROAD_STRICT = r"[가-힣0-9]{2,10}(?:로|길)"
_NUM = r"\d{1,5}(?:-\d{1,5})?(?:번지|번길)?(?!\d)(?!\s?(?:원|개|명|시|분|일|월|년|위|차|세|살|도|℃|%))"
_TAIL = (
    r"(?:\s*,?\s*[가-힣A-Za-z0-9]{1,12}(?:아파트|빌라|오피스텔|빌딩|타워|맨션))?"
    r"(?:\s*,?\s*\d{1,4}동)?(?:\s*,?\s*\d{1,5}호)?(?:\s*,?\s*(?:지하\s?)?\d{1,3}층)?"
)
_ADDRESS = re.compile(
    r"(?<![가-힣])" + _SIDO + r"\s+(?:" + _SGG + r"\s+){0,2}" + _ROAD + r"\s*" + _NUM + _TAIL
)
# without the province: 구/군/시 + road name ending in 로/길 + number
_ADDRESS_SHORT = re.compile(
    r"(?<![가-힣])" + _SGG + r"\s+(?:" + _SGG + r"\s+)?" + _ROAD_STRICT + r"\s*" + _NUM + _TAIL
)

_NAME_END = r"(?=\s*(?:입니다|이에요|예요|이요|이고요|이구요|이고|고요|구요|인데요|인데|이라고|라고|요|[.,!?]|$))"
# "제 이름은 김민수입니다", "성함은 김민수예요"
_NAME_INTRO = re.compile(r"(?<!\[)((?:이름|성함)(?:은|는|이|가|:)?\s*)([가-힣]{2,4})" + _NAME_END, re.M)
# "저는 김민수라고 합니다"
_NAME_CALLED = re.compile(r"((?:저는|전|제가)\s*)([가-힣]{2,4}?)(?=(?:이)?라고\s*(?:합니다|해요|하는데요))")
_NAME_EN = re.compile(r"((?:[Mm]y name is|[Mm]y name's)\s+)([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)")
# "김민수 고객님", "김민수 회원님"
_NAME_CUSTOMER = re.compile(r"(?<![가-힣])([가-힣]{2,4})(\s?(?:고객님|회원님))")
_NOT_NAMES = {
    "그럼요", "맞아요", "아니요", "아니오", "물론", "우리", "저희", "해당", "다른", "모든", "일부", "어떤",
    "신규", "기존", "그런데", "그리고", "하지만", "잠시만", "안녕", "그러면", "그럼", "혹시", "아이고",
    "어머", "소중한", "네네", "예예", "먼저", "지금", "오늘", "이번", "감사", "죄송", "그래서", "또한",
    "다만", "물론이죠", "일반", "많은", "여러", "모두", "각", "본인", "현재", "저기", "여기", "그게",
}
_NOT_NAME_ENDINGS = ("요", "다", "까", "죠", "님")
_SURNAMES = (
    "김이박최정강조윤장임한오서신권황안송류유전홍고문양손배백허남심노하곽성차주우구민진지엄채원천방공"
    "현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용"
)
# "김민수님": family name + one or two syllables + 님
_NAME_NIM = re.compile(r"(?<![가-힣])([" + _SURNAMES + r"][가-힣]{1,2})(\s?님)(?![가-힣])")
_NOT_NAME_NIM = {
    "고객", "선생", "기사", "원장", "주임", "여사", "어머", "하나", "임금", "공주", "왕자", "도련",
    "선배", "변호사", "기자", "주인", "지점장", "구매자", "주문자", "이용자", "신청자", "방문자",
    "주무관", "조교", "교수", "손", "상담원", "상담사", "사장", "과장", "대리", "부장", "차장",
    "팀장", "실장", "국장", "이사", "전무", "상무", "원님", "의사", "간호사", "약사", "강사", "판사",
    "검사", "박사", "석사", "하느", "부모", "장모", "장인", "형", "누", "스", "아버", "할머", "할아버",
    "여러분", "운영자", "관리자", "담당자", "작성자", "판매자", "공급자", "소비자", "사용자", "유저",
    "회원", "주주", "임원", "직원", "점원", "민원인", "주치의", "원장", "소장", "반장", "이장", "조장",
    "고모", "이모", "외삼촌", "삼촌", "제수", "형수", "도사", "전도사", "권사", "목사", "신부", "주교",
}
_NOT_NAME_NIM_ENDINGS = ("자", "장", "생", "객", "배", "사")

# keys that name personal data, for dict keys and for `key: value` text
_PII_KEY_WORDS = (
    r"phone\w*|mobile\w*|tel|telephone|e_?mail\w*|address\w*|addr|ssn|rrn|resident_\w+|"
    r"birth(?:day|date)?|card_?(?:no|num|number)|customer_?name|user_?name|full_?name|"
    r"recipient(?:_?name)?|receiver(?:_?name)?|last4|"
    r"이름|성함|고객명|주문자|주문자명|수령인|전화번호|휴대폰|휴대폰번호|연락처|주소|이메일|생년월일"
)
_PII_KEY = re.compile(r"^(?:\w+_)?(?:" + _PII_KEY_WORDS + r")$", re.I)
_NAME_KEY = re.compile(r"name|이름|성함|고객명|주문자|수령인", re.I)
_KV_TEXT = re.compile(
    r"""(?<![A-Za-z가-힣_])(["']?)(""" + _PII_KEY_WORDS + r""")\1(\s*[:=]\s*)"""
    r"""("(?:[^"\\\n]|\\.)*"|'[^'\n]*'|[^\s"',;{}\[\]][^,;{}\[\]\n]*)""",
    re.I,
)
_LITERALS = {"true", "false", "null", "none", ""}


def _marker_for_key(key: str) -> str:
    return M_NAME if _NAME_KEY.search(key) else M_HIDDEN


def _is_marker(text: str) -> bool:
    return text in MARKERS or (text.startswith("[") and text.endswith("]") and len(text) <= 8)


def _kv_text_sub(m: re.Match) -> str:
    quote, key, sep, value = m.group(1), m.group(2), m.group(3), m.group(4)
    bare = value.strip().strip("\"'")
    if bare.lower() in _LITERALS or _is_marker(bare):
        return m.group(0)
    marker = _marker_for_key(key)
    if value[:1] in "\"'":
        marker = value[0] + marker + value[0]
    return f"{quote}{key}{quote}{sep}{marker}"


def _customer_name_sub(m: re.Match) -> str:
    name = m.group(1)
    if name in _NOT_NAMES or name.endswith(_NOT_NAME_ENDINGS):
        return m.group(0)
    return M_NAME + m.group(2)


def _nim_sub(m: re.Match) -> str:
    name = m.group(1)
    if name in _NOT_NAME_NIM or name.endswith(_NOT_NAME_NIM_ENDINGS):
        return m.group(0)
    return M_NAME + m.group(2)


def mask_text(text: str) -> str:
    """Replaces what the patterns recognise by markers. Amounts ("30000원") and dates survive."""
    if not text or not isinstance(text, str):
        return text
    s = text
    if "@" in s:
        s = _EMAIL.sub(M_EMAIL, s)
    if ":" in s or "=" in s:
        s = _KV_TEXT.sub(_kv_text_sub, s)
    if any(ch.isdigit() for ch in s):
        s = _RRN.sub(M_RRN, s)
        s = _RRN_PLAIN.sub(M_RRN, s)
        s = _CARD.sub(M_CARD, s)
        s = _MOBILE.sub(M_PHONE, s)
        s = _LANDLINE.sub(M_PHONE, s)
        s = _ORDER.sub(M_ORDER, s)
        s = _ORDER_CONTEXT.sub(lambda m: m.group(1) + M_ORDER, s)
        s = _LAST4_REVERSED.sub(M_PHONE, s)
        s = _ADDRESS.sub(M_ADDRESS, s)
        s = _ADDRESS_SHORT.sub(M_ADDRESS, s)
    if "공" in s:
        s = _MOBILE_SPOKEN.sub(M_PHONE, s)
    s = _LAST4.sub(lambda m: m.group(0)[: m.start(1) - m.start(0)] + M_PHONE, s)
    if "이름" in s or "성함" in s:
        s = _NAME_INTRO.sub(lambda m: m.group(1) + M_NAME, s)
    if "라고" in s:
        s = _NAME_CALLED.sub(lambda m: m.group(1) + M_NAME, s)
    if "name" in s:
        s = _NAME_EN.sub(lambda m: m.group(1) + M_NAME, s)
    if "님" in s:
        s = _NAME_CUSTOMER.sub(_customer_name_sub, s)
        s = _NAME_NIM.sub(_nim_sub, s)
    return s


# ------------------------------------------------------------------------------------------------
# values learned from pii paths
# ------------------------------------------------------------------------------------------------


def _learnable(value: Any) -> Optional[str]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    v = value.strip()
    if len(v) < 2 or len(v) > 200 or _is_marker(v):
        return None
    return v


def _collect(value: Any, out: Optional[list]) -> None:
    if out is None:
        return
    if isinstance(value, dict):
        for v in value.values():
            _collect(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect(v, out)
    else:
        v = _learnable(value)
        if v:
            out.append(v)


def _secret_regex(secrets: Iterable[str]) -> Optional[re.Pattern]:
    parts = []
    for s in sorted(set(secrets), key=len, reverse=True):
        if s.isdigit():
            # a number: only as a whole number, and not when it is an amount
            parts.append(r"(?<!\d)" + re.escape(s) + r"(?!\d)(?!\s*원)")
        else:
            parts.append(re.escape(s))
    return re.compile("|".join(parts)) if parts else None


def replace_secrets(value: Any, pattern: Optional[re.Pattern]) -> Any:
    """Deep copy of a JSON-like value with every match of `pattern` in a string replaced."""
    if pattern is None:
        return value
    if isinstance(value, str):
        return pattern.sub(M_HIDDEN, value)
    if isinstance(value, dict):
        return {k: replace_secrets(v, pattern) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_secrets(v, pattern) for v in value]
    return value


class SessionSecrets:
    """Raw values seen at pii paths, per session, kept in memory only (never written anywhere)."""

    def __init__(self, max_sessions: int = 2000, max_values: int = 200):
        self._lock = threading.Lock()
        self._values: "OrderedDict[str, set[str]]" = OrderedDict()
        self._patterns: dict[str, Optional[re.Pattern]] = {}
        self._max_sessions = max_sessions
        self._max_values = max_values

    def learn(self, session_id: str, values: Iterable[str]) -> list[str]:
        """Returns the values that were new for this session."""
        new = []
        with self._lock:
            known = self._values.get(session_id)
            if known is None:
                known = self._values[session_id] = set()
                while len(self._values) > self._max_sessions:
                    old, _ = self._values.popitem(last=False)
                    self._patterns.pop(old, None)
            for v in values:
                if v not in known and len(known) < self._max_values:
                    known.add(v)
                    new.append(v)
            if new:
                self._patterns[session_id] = _secret_regex(known)
        return new

    def pattern(self, session_id: str) -> Optional[re.Pattern]:
        with self._lock:
            return self._patterns.get(session_id)

    def forget(self, session_id: str) -> None:
        with self._lock:
            self._values.pop(session_id, None)
            self._patterns.pop(session_id, None)


# ------------------------------------------------------------------------------------------------
# the masker
# ------------------------------------------------------------------------------------------------

_MISSING = object()


class RegexMasker:
    """contracts.Masker."""

    def mask_text(self, text: str) -> str:
        return mask_text(text)

    def mask(self, value: Any, pii_paths: list[str] | None = None, *,
             protect_keys: Iterable[str] = (), _learned: Optional[list] = None) -> Any:
        """Deep copy with every string run through mask_text and every pii path replaced.

        pii_paths are dotted paths from the root of `value` ("input.arguments.phone_last4").
        A path step that meets a list applies to every element; "*" matches any key.
        Dict values under a key that names personal data (see _PII_KEY) are replaced as well.
        protect_keys: dict keys whose string values are copied untouched (identifiers).
        """
        plain = self._plain(value)
        if pii_paths:
            for path in pii_paths:
                try:
                    steps = [p for p in str(path).split(".") if p != ""]
                    if steps:
                        plain = self._hide(plain, steps, _marker_for_key(steps[-1]), _learned)
                except Exception:
                    continue
        return self._walk(plain, frozenset(protect_keys), _learned)

    # -- helpers ----------------------------------------------------------------------------

    def _plain(self, value: Any) -> Any:
        """JSON-like deep copy: models -> dicts, tuples/sets -> lists, unknown objects -> str."""
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, BaseModel):
            return self._plain(value.model_dump())
        if isinstance(value, dict):
            return {str(k): self._plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._plain(v) for v in value]
        if isinstance(value, (bytes, bytearray, memoryview)):
            return f"[bytes {len(value)}]"
        return str(value)

    def _hide(self, node: Any, steps: list[str], marker: str, learned: Optional[list]) -> Any:
        if not steps:
            if node is None or node == "" or (isinstance(node, str) and _is_marker(node)):
                return node
            _collect(node, learned)
            return marker
        head, rest = steps[0], steps[1:]
        if isinstance(node, list):
            if head.isdigit():
                i = int(head)
                if i < len(node):
                    node[i] = self._hide(node[i], rest, marker, learned)
                return node
            if head == "*":
                return [self._hide(v, rest, marker, learned) for v in node]
            return [self._hide(v, steps, marker, learned) for v in node]
        if isinstance(node, dict):
            if head == "*":
                for k in list(node):
                    node[k] = self._hide(node[k], rest, marker, learned)
            elif head in node:
                node[head] = self._hide(node[head], rest, marker, learned)
        return node

    def _walk(self, node: Any, protect: frozenset, learned: Optional[list], key: str = "") -> Any:
        if isinstance(node, str):
            return mask_text(node)
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in protect:
                    out[k] = v
                elif _PII_KEY.match(k) and self._scalarish(v):
                    out[k] = self._hide(v, [], _marker_for_key(k), learned)
                else:
                    out[k] = self._walk(v, protect, learned, k)
            return out
        if isinstance(node, list):
            return [self._walk(v, protect, learned, key) for v in node]
        return node

    @staticmethod
    def _scalarish(v: Any) -> bool:
        if isinstance(v, bool) or v is None:
            return False
        if isinstance(v, (str, int, float)):
            return True
        return isinstance(v, list) and bool(v) and all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in v)
