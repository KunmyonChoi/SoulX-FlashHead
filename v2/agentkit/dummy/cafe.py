"""
The dummy cafe: a kiosk ordering system kept in memory (docs/DISPLAY.md, "더미 카페").

- 21 menu items in 4 categories (coffee, non-coffee, tea, dessert) with prices, descriptions,
  tags and options (temperature, size, extra shot). Two items are sold out.
- One cart per session; sessions never see each other's carts.
- Two coupons: WELCOME10 (10% off) and TEA1000 (1000 won off when a tea is in the cart).
- request_payment has a preview (the lines, the total and a one-line summary for the confirmation
  sentence); executing it gives an order number and empties the cart. The cart that was read to
  the customer is the cart that is paid: if it changed after the preview, the payment is refused.

Models are sloppy with inputs, so this system is lenient where the meaning is clear: an item
given by its Korean name ("아이스 아메리카노"), options given in Korean ("차갑게", "큰 거"), a
quantity given as "2" or "두 잔". What cannot be resolved is refused with a sentence naming the
valid choices.

Every method answers with a dict {"ok", "text", "data", "error_type"}. `text` is short and for the
model; the screen is drawn from `data` (payload shapes: docs/DISPLAY.md). A failure may carry
data["display_notice"] = {"level", "text"}, which the gateway shows as a notice.
"""
from __future__ import annotations

import asyncio
import copy
import difflib
import json
import re
import time

METHODS = ("get_menu", "search_menu", "get_cart", "add_to_cart", "remove_from_cart", "apply_coupon",
           "request_payment")
MAX_QUANTITY = 20          # per line
SEARCH_LIMIT = 8
RECORD_LIMIT = 2000
RECEIPT_MESSAGE = "주문이 접수되었습니다. 번호가 불리면 받아 가세요."

CATEGORIES = [("coffee", "커피"), ("non_coffee", "논커피"), ("tea", "차"), ("dessert", "디저트")]

# option id -> name, required, default choice (optional groups), choices (id, name, price_delta)
OPTIONS = {
    "temperature": {"name": "온도", "required": True, "default": None,
                    "choices": [("hot", "따뜻하게", 0), ("ice", "차갑게", 0)]},
    "size": {"name": "크기", "required": False, "default": "regular",
             "choices": [("regular", "기본", 0), ("large", "크게", 500)]},
    "shot": {"name": "샷 추가", "required": False, "default": "none",
             "choices": [("none", "추가 안 함", 0), ("one", "1샷 추가", 500), ("two", "2샷 추가", 1000)]},
}
_T, _S, _SH = "temperature", "size", "shot"

# id, category, name, price, description, tags, options, fixed temperature, sold out, other names
_MENU = [
    ("americano", "coffee", "아메리카노", 4500, "진한 에스프레소에 물을 더한 커피",
     ["진한", "깔끔한", "인기"], [_T, _S, _SH], None, False, ["아메"]),
    ("decaf_americano", "coffee", "디카페인 아메리카노", 5000, "카페인을 뺀 원두로 내린 아메리카노",
     ["카페인 없는", "깔끔한"], [_T, _S], None, False, ["디카페인", "디카페인 커피"]),
    ("cafe_latte", "coffee", "카페 라떼", 5000, "에스프레소에 부드러운 우유를 넣은 커피",
     ["고소한", "부드러운", "인기"], [_T, _S, _SH], None, False, ["라떼", "라테", "카페 라테"]),
    ("vanilla_latte", "coffee", "바닐라 라떼", 5500, "바닐라 시럽을 넣은 달콤한 라떼",
     ["달달한", "부드러운", "인기"], [_T, _S, _SH], None, False, ["바닐라 라테", "바닐라"]),
    ("caramel_macchiato", "coffee", "카라멜 마키아토", 5800, "카라멜 시럽과 우유, 에스프레소가 어우러진 달콤한 커피",
     ["달달한", "부드러운"], [_T, _S, _SH], None, False,
     ["카라멜 마끼아또", "캐러멜 마키아토", "캐러멜 마키아또", "카라멜 마키아또", "마키아토", "마끼아또"]),
    ("cold_brew", "coffee", "콜드브루", 5000, "찬물로 오래 우려 깔끔한 커피. 차갑게만 됩니다",
     ["깔끔한", "진한"], [_S, _SH], "ice", False, ["콜드 브루", "더치 커피"]),

    ("choco_latte", "non_coffee", "초코 라떼", 5500, "진한 초콜릿에 우유를 넣은 달콤한 음료",
     ["달달한", "진한"], [_T, _S], None, False, ["초코 라테", "초코", "초콜릿 라떼", "초코 우유"]),
    ("matcha_latte", "non_coffee", "말차 라떼", 5800, "쌉싸름한 말차에 우유를 넣은 음료",
     ["고소한", "쌉싸름한"], [_T, _S], None, False, ["말차 라테", "녹차 라떼", "녹차 라테", "그린티 라떼"]),
    ("strawberry_latte", "non_coffee", "딸기 라떼", 6000, "딸기 과육과 우유를 섞은 달콤한 음료. 차갑게만 됩니다",
     ["달달한", "카페인 없는", "과일"], [_S], "ice", False, ["딸기 라테", "딸기 우유"]),
    ("lemon_ade", "non_coffee", "레몬 에이드", 5500, "레몬과 탄산수로 만든 상큼한 음료. 차갑게만 됩니다",
     ["상큼한", "카페인 없는", "과일", "탄산"], [_S], "ice", False, ["레모네이드", "레몬에이드"]),
    ("mango_smoothie", "non_coffee", "망고 스무디", 6500, "망고를 얼음과 함께 간 음료. 차갑게만 됩니다",
     ["달달한", "카페인 없는", "과일"], [_S], "ice", True, ["망고 주스"]),

    ("earl_grey", "tea", "얼그레이", 4800, "베르가못 향이 나는 홍차",
     ["향긋한"], [_T], None, False, ["얼그레이 티", "홍차"]),
    ("chamomile", "tea", "캐모마일", 4800, "은은한 꽃 향의 허브차",
     ["카페인 없는", "향긋한", "허브"], [_T], None, False, ["캐모마일 티", "카모마일"]),
    ("peppermint", "tea", "페퍼민트", 4800, "입안이 개운해지는 박하 허브차",
     ["카페인 없는", "개운한", "허브"], [_T], None, False, ["페퍼민트 티", "민트 티", "박하차"]),
    ("yuzu_tea", "tea", "유자차", 5300, "유자청으로 만든 달콤하고 향긋한 차",
     ["달달한", "카페인 없는", "과일", "향긋한"], [_T], None, False, ["유자 차", "유자티"]),
    ("peach_iced_tea", "tea", "복숭아 아이스티", 4800, "복숭아 향의 달콤한 아이스티. 차갑게만 됩니다",
     ["달달한", "과일"], [_S], "ice", False, ["아이스티", "복숭아티", "피치 아이스티"]),

    ("cheesecake", "dessert", "치즈 케이크", 6500, "부드럽고 진한 뉴욕식 치즈 케이크",
     ["달달한", "부드러운", "케이크", "인기"], [], None, False, ["치즈케익", "뉴욕 치즈 케이크"]),
    ("choco_cake", "dessert", "초코 케이크", 6800, "진한 초콜릿 시트의 케이크",
     ["달달한", "진한", "케이크"], [], None, True, ["초코케익", "초콜릿 케이크"]),
    ("croissant", "dessert", "크루아상", 4200, "버터 향이 가득한 바삭한 빵",
     ["고소한", "빵", "담백한"], [], None, False, ["크로와상", "크로아상"]),
    ("choco_cookie", "dessert", "초코칩 쿠키", 3000, "초콜릿 칩이 박힌 쿠키",
     ["달달한", "쿠키"], [], None, False, ["쿠키", "초코 쿠키", "초코칩쿠키"]),
    ("macaron", "dessert", "마카롱", 3500, "바닐라 크림을 넣은 마카롱 한 개",
     ["달달한"], [], None, False, ["마카롱 한 개"]),
]

COUPONS = {
    "WELCOME10": "전체 10% 할인",
    "TEA1000": "차 메뉴 1000원 할인",
}

# ---- words the model (or the customer) uses for options ----------------------------------------

_GROUP_WORDS = {
    _T: ["temperature", "temp", "온도", "핫아이스", "hotice", "hot_ice"],
    _S: ["size", "사이즈", "크기"],
    _SH: ["shot", "shots", "샷", "샷추가", "extrashot", "extra_shot", "espressoshot"],
}
_CHOICE_WORDS = {
    _T: {"hot": ["hot", "핫", "따뜻하게", "따뜻한", "따뜻", "따듯하게", "따듯한", "뜨겁게", "뜨거운", "뜨겁", "warm"],
         "ice": ["ice", "iced", "아이스", "차갑게", "차가운", "차갑", "시원하게", "시원한", "찬", "cold"]},
    _S: {"regular": ["regular", "기본", "보통", "레귤러", "작은거", "작게", "작은", "small", "medium", "톨", "tall"],
         "large": ["large", "라지", "큰거", "큰것", "크게", "큰사이즈", "큰", "big", "벤티", "그란데"]},
    _SH: {"none": ["none", "no", "0", "없음", "없이", "추가안함", "안함", "false"],
          "one": ["one", "1", "1샷", "1샷추가", "한샷", "샷추가", "추가", "true", "yes", "single", "한번"],
          "two": ["two", "2", "2샷", "2샷추가", "투샷", "두샷", "double", "두번"]},
}
# words in front of (or behind) an item name that are really an option: "아이스 아메리카노", "따뜻한 라떼"
_NAME_MODIFIERS = [
    (_T, "ice", ["아이스", "차가운", "시원한", "iced", "ice"]),
    (_T, "hot", ["따뜻한", "따듯한", "뜨거운", "핫", "hot"]),
    (_S, "large", ["라지", "큰사이즈", "large"]),
    (_S, "regular", ["레귤러", "regular"]),
]
_NICKNAMES = {"아아": ("americano", {_T: "ice"}), "뜨아": ("americano", {_T: "hot"}),
              "아라": ("cafe_latte", {_T: "ice"}), "아바라": ("vanilla_latte", {_T: "ice"})}
_NUMBER_WORDS = {"한": 1, "하나": 1, "일": 1, "두": 2, "둘": 2, "이": 2, "세": 3, "셋": 3, "삼": 3, "네": 4, "넷": 4,
                 "사": 4, "다섯": 5, "오": 5, "여섯": 6, "육": 6, "일곱": 7, "칠": 7, "여덟": 8, "팔": 8,
                 "아홉": 9, "구": 9, "열": 10, "십": 10}
_SPOKEN_PREFIX = {"hot": "따뜻한", "ice": "아이스"}

# search: what a query means -> the tag an item must carry
_CONCEPTS = [
    ("달달한", r"달달|달콤|단\s*거|단\s*것|단\s*음료|단\s*맛|스위트|sweet"),
    ("카페인 없는", r"카페인\s*(이|은)?\s*(없|안|적|빼|뺀)|디카페인|무카페인|논카페인|decaf"),
    ("따뜻한", r"따뜻|따듯|뜨거|뜨끈|핫|hot|warm"),
    ("시원한", r"시원|차가|차갑|아이스|찬\s*거|찬\s*것|찬\s*음료|ice|cold"),
    ("상큼한", r"상큼|새콤|시큼"),
    ("고소한", r"고소"),
    ("인기", r"인기|추천|잘\s*나가|베스트|best"),
    ("과일", r"과일"),
]
_CATEGORY_WORDS = {"커피": "coffee", "coffee": "coffee", "논커피": "non_coffee", "차": "tea", "티": "tea",
                   "tea": "tea", "디저트": "dessert", "간식": "dessert", "먹을": "dessert", "dessert": "dessert"}
_STOP = {"거", "것", "걸", "메뉴", "음료", "음식", "뭐", "뭐가", "있어요", "있나요", "있어", "좀", "종류", "중에", "중",
         "추천", "추천해", "주세요", "줘", "알려", "어떤", "the", "a", "menu", "drink", "drinks", "마실", "먹을",
         "한", "이", "가", "은", "는", "을", "를", "로", "으로"}


class CafeError(Exception):
    """A refusal by the cafe. error_type is one of contracts.ToolErrorType."""

    def __init__(self, error_type, text, data=None):
        super().__init__(text)
        self.error_type, self.text, self.data = error_type, text, data


def _key(value):
    """ "Cafe-Latte", "카페 라떼" -> "cafelatte", "카페라떼": what names are compared by."""
    return re.sub(r"[\s\-_.,·'\"()\[\]]", "", str(value if value is not None else "")).casefold()


def _won(amount):
    return f"{int(amount)}원"


def _p(word, with_final, without_final):
    """The word with the right particle: _p("케이크", "은", "는") -> "케이크는"."""
    word = str(word)
    last = word[-1:] if word else ""
    if "가" <= last <= "힣":
        final = (ord(last) - 0xAC00) % 28 != 0
    else:
        final = last in "013678" or last.casefold() in "lmnr"
    return word + (with_final if final else without_final)


def _build_items():
    items = {}
    for iid, category, name, price, description, tags, options, fixed, sold_out, aliases in _MENU:
        items[iid] = {"id": iid, "category": category, "name": name, "price": price, "description": description,
                      "tags": list(tags), "options": list(options), "fixed_temperature": fixed,
                      "sold_out": sold_out, "aliases": list(aliases)}
    return items


def _choice(group, choice_id):
    return next(c for c in OPTIONS[group]["choices"] if c[0] == choice_id)


def _option_payload(group):
    spec = OPTIONS[group]
    return {"id": group, "name": spec["name"], "required": spec["required"], "default": spec["default"],
            "choices": [{"id": cid, "name": name, "price_delta": delta} for cid, name, delta in spec["choices"]]}


def _choices_sentence(group):
    spec = OPTIONS[group]
    return f"{spec['name']}({group})는 " + ", ".join(f"{name}({cid})" for cid, name, _ in spec["choices"])


class _Session:
    def __init__(self):
        self.lines = []           # {"line_id", "item_id", "selected": {option id: choice id}, "quantity"}
        self.next_line = 1
        self.coupon = None
        self.quote = None         # what the cart was when the payment confirmation was made


class CafeSystem:
    """One cafe. `options` are system-wide defaults (delay_s)."""

    name = "cafe"
    methods = METHODS

    def __init__(self, options=None):
        self.items = _build_items()
        self._sessions = {}
        self._orders = []         # paid orders, kept after end_session for inspection
        self._order_count = 0
        self.options = dict(options or {})
        self._names = {}
        for item in self.items.values():
            for word in [item["id"], item["name"], *item["aliases"]]:
                self._names.setdefault(_key(word), item["id"])

    # ---- state ------------------------------------------------------------------------------

    def _session(self, session_id):
        if session_id not in self._sessions:
            self._sessions[session_id] = _Session()
        return self._sessions[session_id]

    def end_session(self, session_id):
        """Forgets the session's cart. Paid orders stay in records()."""
        self._sessions.pop(session_id, None)

    def reset(self):
        self.items = _build_items()
        self._sessions.clear()
        self._orders.clear()
        self._order_count = 0

    def set_sold_out(self, item_id, sold_out=True):
        self.items[item_id]["sold_out"] = bool(sold_out)

    def records(self, session_id=None):
        """Paid orders: [{"order_no", "session_id", "lines", "total", ...}], oldest first."""
        return [copy.deepcopy(o) for o in self._orders if session_id is None or o["session_id"] == session_id]

    def cart(self, session_id):
        """The session's cart payload (a copy), for tests and inspection."""
        return self._cart(self._session(session_id))

    # ---- entry points -----------------------------------------------------------------------

    async def call(self, method, arguments, ctx=None, options=None):
        """Runs one method. Returns {"ok", "text", "data", "error_type"} (+"executed" for writes)."""
        if method not in METHODS:
            return _fail("invalid_input", f"카페 시스템에 '{method}' 기능은 없습니다.")
        session_id = _session_id(ctx)
        await self._delay(options)
        try:
            return getattr(self, "_" + method)(self._session(session_id), _arguments(arguments), session_id)
        except CafeError as e:
            return _fail(e.error_type, e.text, e.data)

    async def preview(self, method, arguments, ctx=None, options=None):
        """What a write would do, without doing it. For request_payment: the lines, the total and
        `summary`, the words that go into the confirmation sentence; or the refusal the request
        would meet (an empty cart). None for methods that have nothing to preview."""
        if method != "request_payment":
            return None
        await self._delay(options)
        state = self._session(_session_id(ctx))
        try:
            cart = self._payable(state)
        except CafeError as e:
            return _fail(e.error_type, e.text, e.data)
        state.quote = self._signature(state)
        return _ok("", dict(cart, summary=self._summary(cart)))

    async def _delay(self, options):
        opts = dict(self.options)
        opts.update(options or {})
        delay = float(opts.get("delay_s") or 0)
        if delay > 0:
            await asyncio.sleep(delay)

    # ---- menu -------------------------------------------------------------------------------

    def _item_payload(self, item):
        return {"id": item["id"], "name": item["name"], "price": item["price"], "description": item["description"],
                "category": item["category"], "tags": list(item["tags"]), "sold_out": item["sold_out"],
                "options": [_option_payload(group) for group in item["options"]]}

    def menu(self):
        """The `menu` display payload."""
        return {"categories": [
            {"id": cid, "name": cname,
             "items": [self._item_payload(i) for i in self.items.values() if i["category"] == cid]}
            for cid, cname in CATEGORIES]}

    def _option_words(self, item):
        words = []
        if item["fixed_temperature"] == "ice":
            words.append("차갑게만")
        for group in item["options"]:
            words.append(OPTIONS[group]["name"] + (" 필수" if OPTIONS[group]["required"] else ""))
        return ", ".join(words)

    def _menu_line(self, item):
        line = f"{item['id']} {item['name']} {_won(item['price'])}"
        words = self._option_words(item)
        if words:
            line += f" ({words})"
        return line + (" 품절" if item["sold_out"] else "")

    def _get_menu(self, state, arguments, session_id):
        rows = ["카페 메뉴입니다. 주문은 add_to_cart의 item_id에 아래 영문 id를 넣습니다. 가격은 기본 크기 기준입니다.",
                "옵션: 온도 temperature(hot 따뜻하게 / ice 차갑게), 크기 size(regular 기본 / large 크게 +500원), "
                "샷 추가 shot(none / one +500원 / two +1000원). '온도 필수'인 메뉴는 고객에게 온도를 물어 정합니다. "
                "'품절'인 메뉴는 담을 수 없습니다."]
        for cid, cname in CATEGORIES:
            rows.append(f"[{cname}]")
            rows += ["- " + self._menu_line(i) for i in self.items.values() if i["category"] == cid]
        out = _ok("\n".join(rows), self.menu())
        out["items"] = len(self.items)
        return out

    def _search_tags(self, item):
        tags = set(item["tags"])
        if _T in item["options"]:
            tags |= {"따뜻한", "시원한"}
        elif item["fixed_temperature"] == "ice":
            tags.add("시원한")
        return tags

    def _search_menu(self, state, arguments, session_id):
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise CafeError("invalid_input", "검색어가 없습니다. 찾을 말을 query에 넣어 다시 호출하세요. "
                                             "전체 메뉴는 get_menu로 봅니다.")
        lowered = query.casefold()
        concepts = [tag for tag, pattern in _CONCEPTS if re.search(pattern, lowered)]
        rest = lowered
        for _, pattern in _CONCEPTS:
            rest = re.sub(pattern, " ", rest)
        tokens = [t for t in re.split(r"[\s,./?!]+", rest) if t]
        categories = {_CATEGORY_WORDS[t] for t in tokens if t in _CATEGORY_WORDS}
        if re.search(r"케이크|케익|빵|쿠키", lowered):
            categories.discard("dessert")       # those are searched as words, they name items
        words = [t for t in tokens if t not in _CATEGORY_WORDS and t not in _STOP and len(_key(t)) >= 2]
        whole = _key(query)
        found = []
        for item in self.items.values():
            tags = self._search_tags(item)
            hay = _key(" ".join([item["name"], item["description"], *item["aliases"], *item["tags"]]))
            if whole and (whole in _key(item["name"]) or any(whole == _key(a) for a in item["aliases"])):
                found.append((3, item))         # the query is (part of) the item's name
                continue
            if any(tag not in tags for tag in concepts) or (categories and item["category"] not in categories):
                continue
            hits = sum(1 for w in words if _key(w) in hay)
            if not concepts and not categories and not hits:
                continue
            # "시원한 거": what is only ever cold comes before what can also be made cold
            own = all(tag in item["tags"] or (tag == "시원한" and item["fixed_temperature"] == "ice")
                      for tag in concepts)
            found.append((1 + (hits > 0) + 0.5 * own, item))
        if not found:
            raise CafeError("not_found",
                            f"'{query}'에 맞는 메뉴를 찾지 못했습니다. 고객에게 없다고 말하고, 달달한, 카페인 없는, 따뜻한, "
                            f"시원한, 상큼한, 고소한, 인기 같은 말이나 메뉴 이름으로 다시 찾아보세요.",
                            {"query": query, "items": []})
        if any(score >= 2 for score, _ in found):   # something matched by word: the rest is noise
            found = [pair for pair in found if pair[0] >= 2]
        found.sort(key=lambda pair: (pair[1]["sold_out"], -pair[0]))
        items = [item for _, item in found][:SEARCH_LIMIT]
        rows = [f"'{query}' 검색 결과 {len(items)}개입니다. 화면에 표시되니 두세 개만 말로 소개하세요."]
        rows += [f"{n}. {self._menu_line(item)}: {item['description']}" for n, item in enumerate(items, 1)]
        return _ok("\n".join(rows), {"query": query, "items": [self._item_payload(i) for i in items]})

    # ---- resolving what the model sent ------------------------------------------------------

    def _find_item(self, value):
        """(item, options implied by the wording) or raises. "아이스 아메리카노" -> americano, ice."""
        key = _key(value)
        if not key:
            raise CafeError("invalid_input", "메뉴가 없습니다. item_id에 메뉴 id를 넣어 다시 호출하세요. "
                                             "id는 get_menu나 search_menu의 결과에 있습니다.")
        if key in self._names:
            return self.items[self._names[key]], {}
        if key in _NICKNAMES:
            iid, implied = _NICKNAMES[key]
            return self.items[iid], dict(implied)
        implied, stripped = {}, key
        for group, choice, words in _NAME_MODIFIERS:
            for word in words:
                if group not in implied and stripped != word and (stripped.startswith(word) or stripped.endswith(word)):
                    stripped = stripped[len(word):] if stripped.startswith(word) else stripped[:-len(word)]
                    implied[group] = choice
                    break
        if stripped in self._names:
            return self.items[self._names[stripped]], implied
        if len(stripped) >= 2:
            near = [i for i in self.items.values()
                    if stripped in _key(i["name"]) or _key(i["name"]) in stripped
                    or any(stripped == _key(a) or _key(a) in stripped for a in i["aliases"] if len(_key(a)) >= 3)]
            if len(near) == 1:
                return near[0], implied
            if near:
                raise CafeError("invalid_input",
                                f"'{value}'에 해당하는 메뉴가 여러 개입니다: "
                                + ", ".join(f"{i['name']}({i['id']})" for i in near)
                                + ". 고객에게 어느 것인지 물어본 뒤 그 id로 다시 호출하세요.",
                                {"candidates": [i["id"] for i in near]})
        names = {_key(i["name"]): i for i in self.items.values()}
        close = [names[k] for k in difflib.get_close_matches(stripped, list(names), n=3, cutoff=0.5)]
        hint = ("비슷한 메뉴: " + ", ".join(f"{i['name']}({i['id']})" for i in close) + ". " if close else "")
        raise CafeError("not_found",
                        f"'{value}'라는 메뉴는 없습니다. 담지 않았습니다. {hint}"
                        f"메뉴 종류는 {', '.join(name for _, name in CATEGORIES)}입니다. search_menu로 찾거나 "
                        f"get_menu로 전체 메뉴의 id를 확인하세요.",
                        {"item": str(value), "suggestions": [i["id"] for i in close]})

    @staticmethod
    def _group_of(word):
        key = _key(word)
        return next((g for g, words in _GROUP_WORDS.items() if key in words), None)

    @staticmethod
    def _choice_of(value, group=None):
        """(group, choice id) for a word such as "아이스", "큰 거", True; (group, None) if unknown."""
        key = "true" if value is True else "false" if value is False else _key(value)
        key = re.sub(r"(으로|로|요)$", "", key) or key
        groups = [group] if group else list(_CHOICE_WORDS)
        for g in groups:                        # the word itself
            for choice, words in _CHOICE_WORDS[g].items():
                if key in words:
                    return g, choice
        best = None                             # or a longer wording that contains it: "큰 걸로 주세요"
        for g in groups:
            for choice, words in _CHOICE_WORDS[g].items():
                for word in words:
                    if len(word) >= 2 and not word.isascii() and word in key and (best is None or len(word) > len(best[2])):
                        best = (g, choice, word)
        return (best[0], best[1]) if best else (group, None)

    def _options_help(self, item):
        if not item["options"]:
            return f"{item['name']}에는 고를 옵션이 없습니다."
        text = f"{item['name']}의 옵션: " + "; ".join(_choices_sentence(g) for g in item["options"]) + "."
        if item["fixed_temperature"] == "ice":
            text += " 차갑게만 됩니다."
        return text

    def _read_options(self, item, raw, implied):
        """{option id: choice id} for every option of the item, or raises naming what is possible."""
        if isinstance(raw, str):
            text = raw.strip()
            try:
                raw = json.loads(text) if text[:1] in "{[" else [p for p in re.split(r"[,/]", text) if p.strip()]
            except ValueError:
                raw = [p for p in re.split(r"[,/]", text) if p.strip()]
        pairs = []
        if isinstance(raw, dict):
            for name, value in raw.items():
                if value is None or value == "":
                    continue
                group = self._group_of(name)
                if group is None and value is True:     # {"아이스": true}
                    name, value = None, name
                pairs.append((group, name, value))
        elif isinstance(raw, (list, tuple)):
            pairs = [(None, None, value) for value in raw if value not in (None, "")]
        elif raw is not None:
            raise CafeError("invalid_input", "options는 {\"옵션 id\": \"선택 id\"} 묶음이어야 합니다. "
                                             + self._options_help(item))
        selected = {}
        for group, name, value in pairs:
            found_group, choice = self._choice_of(value, group)
            if choice is None:
                if found_group:
                    raise CafeError("invalid_input",
                                    f"'{value}'은(는) 고를 수 없는 값입니다. {_choices_sentence(found_group)} 중에서 "
                                    f"고를 수 있습니다. 맞는 값으로 다시 호출하세요.", {"option": found_group})
                raise CafeError("invalid_input",
                                f"'{name if name is not None else value}'(이)라는 옵션은 알 수 없습니다. "
                                + self._options_help(item) + " 맞는 값으로 다시 호출하세요.")
            selected[found_group] = choice
        for group, choice in implied.items():
            selected.setdefault(group, choice)
        for group, choice in list(selected.items()):
            if group in item["options"]:
                continue
            if choice == OPTIONS[group]["default"] or (group == _T and choice == item["fixed_temperature"]):
                del selected[group]             # says what the item already is
                continue
            if group == _T and item["fixed_temperature"] == "ice":
                reason = f"{_p(item['name'], '은', '는')} 차갑게만 됩니다. 따뜻하게는 만들 수 없습니다."
            else:
                reason = f"{item['name']}에는 {OPTIONS[group]['name']} 옵션이 없습니다."
            raise CafeError("invalid_input", f"{reason} 담지 않았습니다. 고객에게 알리고 어떻게 할지 물어보세요. "
                                             + self._options_help(item), {"item_id": item["id"], "option": group})
        missing = [g for g in item["options"] if OPTIONS[g]["required"] and g not in selected]
        if missing:
            asks = " ".join(f"{_choices_sentence(g)} 중 하나를 골라야 합니다." for g in missing)
            raise CafeError("invalid_input",
                            f"{_p(item['name'], '은', '는')} {_p(', '.join(OPTIONS[g]['name'] for g in missing), '을', '를')} 정해야 담을 수 "
                            f"있습니다. 아직 담지 않았습니다. {asks} 고객에게 물어본 뒤 options에 넣어 다시 호출하세요.",
                            {"item_id": item["id"], "missing_options": missing})
        for group in item["options"]:
            selected.setdefault(group, OPTIONS[group]["default"])
        return {group: selected[group] for group in item["options"]}

    @staticmethod
    def _read_quantity(value, default=1):
        if value is None or value == "":
            return default
        number = None
        if isinstance(value, bool):
            pass
        elif isinstance(value, (int, float)):
            number = int(value) if float(value).is_integer() else None
        else:
            text = re.sub(r"(잔|개|조각|컵|병|인분|ea)$", "", _key(value))
            if re.fullmatch(r"[+]?\d+", text):
                number = int(text)
            else:
                number = _NUMBER_WORDS.get(text)
        if number is None or not 1 <= number <= MAX_QUANTITY:
            raise CafeError("invalid_input", f"수량 '{value}'을(를) 읽지 못했습니다. quantity에는 1부터 {MAX_QUANTITY}까지의 "
                                             f"숫자를 넣어 다시 호출하세요.")
        return number

    # ---- cart -------------------------------------------------------------------------------

    def _line_payload(self, line):
        item = self.items[line["item_id"]]
        unit = item["price"] + sum(_choice(g, c)[2] for g, c in line["selected"].items())
        shown = [_choice(g, c)[1] for g, c in line["selected"].items()
                 if OPTIONS[g]["required"] or c != OPTIONS[g]["default"]]
        return {"line_id": line["line_id"], "item_id": item["id"], "name": item["name"], "options": shown,
                "selected": dict(line["selected"]), "quantity": line["quantity"], "unit_price": unit,
                "line_total": unit * line["quantity"]}

    def _cart(self, state):
        """The `cart` display payload."""
        lines = [self._line_payload(line) for line in state.lines]
        subtotal = sum(line["line_total"] for line in lines)
        discount = 0
        if state.coupon == "WELCOME10":
            discount = subtotal * 10 // 100
        elif state.coupon == "TEA1000":
            tea = sum(l["line_total"] for l in lines if self.items[l["item_id"]]["category"] == "tea")
            discount = min(1000, tea)
        return {"lines": lines, "subtotal": subtotal, "discount": discount, "total": subtotal - discount,
                "coupon": state.coupon, "coupon_name": COUPONS.get(state.coupon),
                "count": sum(line["quantity"] for line in lines)}

    def _unit(self, item_id):
        return "개" if self.items[item_id]["category"] == "dessert" else "잔"

    def _label(self, line):
        """ "아메리카노(차갑게, 크게)" """
        return line["name"] + (f"({', '.join(line['options'])})" if line["options"] else "")

    def _spoken_count(self, line, quantity):
        """ "아이스 아메리카노 두 잔": the item as said aloud, with a native Korean count (for reply templates)."""
        words = self._spoken(dict(line, quantity=0)).rsplit(" ", 1)[0]
        unit = self._unit(line["item_id"])
        native = {1: "한", 2: "두", 3: "세", 4: "네", 5: "다섯", 6: "여섯", 7: "일곱", 8: "여덟", 9: "아홉", 10: "열"}
        return f"{words} {native[quantity]} {unit}" if quantity in native else f"{words} {quantity}{unit}"

    def _spoken(self, line):
        """ "아이스 아메리카노 큰 사이즈 2잔": for the sentence that is read aloud."""
        selected = line["selected"]
        words = [_SPOKEN_PREFIX[selected[_T]]] if selected.get(_T) else []
        words.append(line["name"])
        if selected.get(_S) == "large":
            words.append("큰 사이즈")
        if selected.get(_SH) in ("one", "two"):
            words.append("샷 추가" if selected[_SH] == "one" else "샷 두 번 추가")
        return f"{' '.join(words)} {line['quantity']}{self._unit(line['item_id'])}"

    def _summary(self, cart):
        lines = cart["lines"]
        parts = [self._spoken(line) for line in lines[:4]]
        if len(lines) > 4:
            parts[-1] += f" 외 {len(lines) - 4}건"
        if cart["discount"]:
            return f"{', '.join(parts)}, 쿠폰 할인 {_won(cart['discount'])}을 빼고 합계 {_won(cart['total'])}"
        return f"{', '.join(parts)}, 합계 {_won(cart['total'])}"

    def _total_sentence(self, cart):
        if not cart["lines"]:
            return "주문 내역이 비었습니다."
        text = f"지금 주문은 모두 {cart['count']}개, 합계 {_won(cart['total'])}입니다"
        return text + (f"(쿠폰 할인 {_won(cart['discount'])} 적용)." if cart["discount"] else ".")

    def _get_cart(self, state, arguments, session_id):
        cart = self._cart(state)
        if not cart["lines"]:
            return _ok("주문 내역이 비어 있습니다.", cart)
        rows = [f"주문 내역 {len(cart['lines'])}줄, 합계 {_won(cart['total'])}"
                + (f" (쿠폰 {cart['coupon']} 할인 {_won(cart['discount'])} 적용)" if cart["coupon"] else "")]
        rows += [f"{n}. [{l['line_id']}] {self._label(l)} {l['quantity']}{self._unit(l['item_id'])} {_won(l['line_total'])}"
                 for n, l in enumerate(cart["lines"], 1)]
        return _ok("\n".join(rows), cart)

    def _add_to_cart(self, state, arguments, session_id):
        item, implied = self._find_item(arguments.get("item_id"))
        if item["sold_out"]:
            others = [i for i in self.items.values() if i["category"] == item["category"] and not i["sold_out"]]
            others.sort(key=lambda i: -len(set(i["tags"]) & set(item["tags"])))
            similar = ", ".join(f"{i['name']}({i['id']}) {_won(i['price'])}" for i in others[:3])
            raise CafeError("precondition_failed",
                            f"{_p(item['name'], '은', '는')} 품절이라 담지 않았습니다. 고객에게 품절이라고 알리고 비슷한 메뉴를 "
                            f"권하세요: {similar}.",
                            {"item_id": item["id"], "reason": "sold_out", "similar": [i["id"] for i in others[:3]],
                             "display_notice": {"level": "info", "text": f"{_p(item['name'], '은', '는')} 품절입니다."}})
        selected = self._read_options(item, arguments.get("options"), implied)
        quantity = self._read_quantity(arguments.get("quantity"))
        line = next((l for l in state.lines if l["item_id"] == item["id"] and l["selected"] == selected), None)
        if line is not None and line["quantity"] + quantity > MAX_QUANTITY:
            raise CafeError("invalid_input", f"한 메뉴는 {MAX_QUANTITY}개까지만 담을 수 있습니다. 담지 않았습니다.")
        merged = line is not None
        if merged:
            line["quantity"] += quantity
        else:
            line = {"line_id": f"L{state.next_line}", "item_id": item["id"], "selected": selected, "quantity": quantity}
            state.next_line += 1
            state.lines.append(line)
        cart = self._cart(state)
        view = next(l for l in cart["lines"] if l["line_id"] == line["line_id"])
        unit = self._unit(item["id"])
        text = f"{self._label(view)} {_p(f'{quantity}{unit}', '을', '를')} 담았습니다(줄 {view['line_id']}"
        text += f", 이 메뉴는 이제 {view['quantity']}{unit}). " if merged else "). "
        out = _ok(text + self._total_sentence(cart), {"line": view, "added": quantity, "cart": cart,
                                                       "spoken": self._spoken_count(view, quantity)})
        out.update(executed=True, repeatable=True)
        return out

    def _find_line(self, state, value):
        if not state.lines:
            raise CafeError("not_found", "주문 내역이 비어 있어 뺄 것이 없습니다.")
        key = _key(value)
        listing = ", ".join(f"{l['line_id']} {self._label(l)} {l['quantity']}{self._unit(l['item_id'])}"
                            for l in self._cart(state)["lines"])
        if not key:
            raise CafeError("invalid_input", f"뺄 줄이 없습니다. line_id에 줄 번호를 넣어 다시 호출하세요. 지금 주문: {listing}.")
        match = re.fullmatch(r"(?:l|line|줄)?0*(\d+)(?:번|번줄)?", key)
        if match:
            line = next((l for l in state.lines if l["line_id"] == f"L{match.group(1)}"), None)
            if line is None:
                raise CafeError("not_found", f"주문 내역에 '{value}' 줄이 없습니다. 지금 주문: {listing}.")
            return line
        try:
            item, implied = self._find_item(value)
        except CafeError:
            raise CafeError("not_found", f"주문 내역에서 '{value}'에 해당하는 줄을 찾지 못했습니다. 지금 주문: {listing}.")
        lines = [l for l in state.lines if l["item_id"] == item["id"]]
        narrowed = [l for l in lines if all(l["selected"].get(g) == c for g, c in implied.items())]
        lines = narrowed or lines
        if len(lines) == 1:
            return lines[0]
        if not lines:
            raise CafeError("not_found", f"주문 내역에 {_p(item['name'], '이', '가')} 없습니다. 지금 주문: {listing}.")
        raise CafeError("invalid_input",
                        f"주문 내역에 {_p(item['name'], '이', '가')} 여러 줄 있습니다. 고객에게 어느 것인지 물어본 뒤 줄 번호로 "
                        f"다시 호출하세요. 지금 주문: {listing}.", {"candidates": [l["line_id"] for l in lines]})

    def _remove_from_cart(self, state, arguments, session_id):
        line = self._find_line(state, arguments.get("line_id"))
        view = self._line_payload(line)
        quantity = self._read_quantity(arguments.get("quantity"), default=line["quantity"])
        quantity = min(quantity, line["quantity"])
        if quantity >= line["quantity"]:
            state.lines.remove(line)
        else:
            line["quantity"] -= quantity
        cart = self._cart(state)
        left = next((l for l in cart["lines"] if l["line_id"] == view["line_id"]), None)
        unit = self._unit(view["item_id"])
        text = f"{self._label(view)} {_p(f'{quantity}{unit}', '을', '를')} 뺐습니다"
        text += f"(줄 {view['line_id']}에 {left['quantity']}{unit} 남음). " if left else ". "
        out = _ok(text + self._total_sentence(cart),
                  {"line_id": view["line_id"], "removed": quantity, "line": left, "cart": cart,
                   "spoken": self._spoken_count(view, quantity)})
        out.update(executed=True, repeatable=True)
        return out

    def _apply_coupon(self, state, arguments, session_id):
        code = re.sub(r"[\s\-_]", "", str(arguments.get("code") or "")).upper()
        if not code:
            raise CafeError("invalid_input", "쿠폰 코드가 없습니다. 고객에게 쿠폰 코드를 물어보세요.")
        if code not in COUPONS:
            raise CafeError("not_found", f"쿠폰 코드 {code}는 없는 쿠폰입니다. 적용하지 않았습니다. 고객에게 코드를 다시 "
                                         f"확인해 달라고 하세요.", {"code": code})
        if code == "TEA1000" and not any(self.items[l["item_id"]]["category"] == "tea" for l in state.lines):
            raise CafeError("precondition_failed",
                            "TEA1000 쿠폰은 차 메뉴를 담았을 때만 쓸 수 있습니다. 적용하지 않았습니다. 고객에게 차 메뉴를 "
                            "담으면 쓸 수 있다고 알리세요.", {"code": code, "reason": "no_tea"})
        replaced = state.coupon if state.coupon and state.coupon != code else None
        state.coupon = code
        cart = self._cart(state)
        text = f"{code} 쿠폰({COUPONS[code]})을 적용했습니다. "
        if replaced:
            text += f"쿠폰은 한 장만 쓸 수 있어 {replaced} 대신 적용했습니다. "
        if cart["lines"]:
            text += f"할인 {_won(cart['discount'])}, 합계 {_won(cart['total'])}입니다."
        else:
            text += "주문 내역이 비어 있어 메뉴를 담으면 할인됩니다."
        out = _ok(text, {"code": code, "name": COUPONS[code], "discount": cart["discount"], "replaced": replaced,
                         "cart": cart})
        out.update(executed=True, repeatable=True)
        return out

    # ---- payment ----------------------------------------------------------------------------

    def _signature(self, state):
        return json.dumps([[(l["item_id"], l["selected"], l["quantity"]) for l in state.lines], state.coupon],
                          sort_keys=True, ensure_ascii=False)

    def _payable(self, state):
        cart = self._cart(state)
        if not cart["lines"]:
            raise CafeError("precondition_failed",
                            "주문 내역이 비어 있어 결제할 수 없습니다. 고객에게 무엇을 주문할지 먼저 물어보세요.",
                            {"reason": "empty_cart",
                             "display_notice": {"level": "info", "text": "주문 내역이 비어 있습니다. 메뉴를 먼저 담아 주세요."}})
        return cart

    def _request_payment(self, state, arguments, session_id):
        cart = self._payable(state)
        if state.quote is not None and state.quote != self._signature(state):
            state.quote = None
            raise CafeError("precondition_failed",
                            "고객이 확인한 뒤에 주문 내역이 바뀌어 결제하지 않았습니다. request_payment를 다시 호출해 바뀐 "
                            "내역으로 확인을 받으세요.", {"reason": "cart_changed"})
        self._order_count += 1
        order = {"order_no": f"K-{self._order_count:04d}", "lines": cart["lines"], "subtotal": cart["subtotal"],
                 "discount": cart["discount"], "total": cart["total"], "coupon": cart["coupon"],
                 "message": RECEIPT_MESSAGE}
        self._orders.append(dict(copy.deepcopy(order), session_id=session_id, at=time.time()))
        del self._orders[:-RECORD_LIMIT]
        state.lines, state.coupon, state.quote, state.next_line = [], None, None, 1
        out = _ok(f"결제 요청이 접수되었습니다. 주문 번호는 {order['order_no']}, 결제 금액은 {_won(order['total'])}입니다. "
                  f"고객에게 주문 번호를 알려 주고, 번호가 불리면 받아 가라고 안내하세요.", order)
        out.update(executed=True, repeatable=True)
        return out


def _ok(text, data=None):
    return {"ok": True, "text": text, "data": data, "error_type": None}


def _fail(error_type, text, data=None):
    return {"ok": False, "text": text, "data": data, "error_type": error_type}


def _arguments(arguments):
    return dict(arguments) if isinstance(arguments, dict) else {}


def _session_id(ctx):
    if ctx is None:
        return "default"
    if isinstance(ctx, dict):
        return str(ctx.get("session_id") or "default")
    return str(getattr(ctx, "session_id", None) or "default")


_cafe = None


def get_cafe():
    """The process-wide cafe that `adapter: {kind: dummy, target: "cafe:..."}` talks to."""
    global _cafe
    if _cafe is None:
        _cafe = CafeSystem()
    return _cafe
