"""Checks on a sentence before it is spoken (contracts.Guard). Rules come from the definition,
plus one framework rule the engine applies itself (empty_wait_guard)."""
import logging
import re

logger = logging.getLogger("agentkit.runtime")

# Framework guards, for every agent that has tools (they are not about one agent's business).
#
# A reply that tells the customer to wait, or promises to look something up, and then ends without
# calling any tool leaves the customer waiting for something that never comes: the turn is over and
# nothing is running. Seen in real voice sessions ("확인을 위해 잠시만 기다려 주세요." four times
# in one conversation, each followed by the customer asking "언제까지 기다려요?").
WAIT_PROMISE = (
    r"(잠시|잠깐|조금)만?\s?(기다려|기다리)"
    r"|(확인|조회|검색|알아)\S{0,3}\s?(보겠습니다|볼게요|드리겠습니다|드릴게요|하겠습니다|할게요)"
    # an action promised and not done: "메일 목록을 다시 띄워 드릴게요" with no tool called (2026-11-20)
    r"|(띄워|띄우|불러\s?와|불러\s?오|보여|열어|가져와|가져오|찾아|읽어|정리해|등록해|보내)\s?(드릴게요|드리겠습니다|올게요|오겠습니다|볼게요|보겠습니다)"
    r"|(띄울|불러올|가져올|찾을|열)게요"
    r"|\b(one moment|just a moment|please (wait|hold)|let me (check|look))\b"
)
WAIT_NUDGE = (
    "(시스템 안내: 방금 고객에게 기다리라고 하거나 확인하겠다고만 말하고 아무 도구도 호출하지 않으려 했습니다. "
    "이 답이 끝나면 아무 일도 일어나지 않으므로 고객은 계속 기다리게 됩니다. 확인이 필요하면 지금 이 답 안에서 도구를 "
    "호출하세요. 도구에 필요한 정보가 없으면 기다리라고 하지 말고 그 정보를 고객에게 물으세요. 이미 아는 내용이면 바로 답하세요.)"
)


def empty_wait_guard():
    """Not a sentence guard: the engine applies it at the end of a reply (see Engine.reply), because
    whether a tool was called is only known then, and the right fix is to carry on, not to unsay it."""
    from agentkit.contracts import Guard
    return Guard(id="builtin_no_empty_wait", kind="claims_lookup", pattern=WAIT_PROMISE,
                 unless_tools=[], nudge=WAIT_NUDGE)


WAIT_PROMISE_RE = re.compile(WAIT_PROMISE, re.I)


def is_empty_wait(reply_text):
    """True if a reply ends on a promise to wait or to look something up.

    Only the last sentence counts: "배송 상태를 확인해 드릴게요. 주문번호를 말씀해 주시겠어요?" hands
    the turn back to the customer with a question, which is fine."""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?。])\s+|\n", reply_text or "") if part.strip()]
    if not sentences:
        return False
    last = sentences[-1]
    if last.endswith("?") or ASKS_CUSTOMER.search(last):
        return False   # "…다시 말씀해 주시면 바로 확인해 드릴게요": the customer is asked for something
    return bool(WAIT_PROMISE_RE.search(last))


# The promise depends on something the customer still has to give: the turn is theirs, not a wait.
# "궁금한 내용이 있으면 바로 확인해 드릴게요" is an offer for later, not a wait (2026-11-20: it was taken for one, and
# the reply was made to go on with a second answer).
ASKS_CUSTOMER = re.compile(r"(주시면|하시면|으시면|시다면|말씀해\s?주세요|알려\s?주세요|불러\s?주세요|"
                           r"(있|없|궁금하|필요하|원하|생기)\S{0,2}면|실\s?때|때마다|언제든)")


class GuardSet:
    """The guards of one agent, compiled once.

    check(sentence, called_tools) returns the first Guard the sentence violates, or None.
      kind "regex":         the sentence matches the pattern
      kind "claims_lookup": the sentence matches the pattern although none of `unless_tools` was
                            called for this reply (with an empty `unless_tools`: although no tool
                            at all was called)
    A guard whose pattern does not compile is skipped with a warning instead of breaking the reply.
    """

    def __init__(self, guards):
        self.rules = []
        for guard in guards or []:
            try:
                self.rules.append((guard, re.compile(guard.pattern)))
            except re.error as e:
                logger.warning(f"Guard {guard.id!r} skipped, bad pattern: {e}")

    def __bool__(self):
        return bool(self.rules)

    def check(self, sentence, called_tools=()):
        if not sentence or not sentence.strip():
            return None
        called = set(called_tools or ())
        for guard, pattern in self.rules:
            if not pattern.search(sentence):
                continue
            if guard.kind == "claims_lookup":
                excused = (called & set(guard.unless_tools)) if guard.unless_tools else called
                if excused:
                    continue
            return guard
        return None


# A reply that has started going round in circles: "십육이 아닌 십육이 아니라 십육이 아니고 십육...", "이의 제곱 곱하기
# 구가 아니라 이의 제곱 곱하기 구가 아닌 ..." (2026-11-20: spoken in full, about 270 characters). Two corrections of a
# word by the same word, or one phrase of 8+ letters said three times. A plain step-by-step explanation ("이로 나누면
# 육십사, 육십사를 이로 나누면 삼십이 ...") is neither.
_SELF_DENIAL = re.compile(r"(\S{2,12}?)(?:이|가)?\s?아(?:닌|니라|니고)\s?\1")
_PHRASE = re.compile(r"(?=(\S[^.?!]{8,30}?)\s)")


def loop_start(text):
    """Where a looping reply began to loop (an index into text), or None."""
    hits = list(_SELF_DENIAL.finditer(text or ""))
    if len(hits) >= 2:
        return hits[0].start()
    for m in _PHRASE.finditer(text or ""):
        phrase = m.group(1)
        if len(re.sub(r"\s", "", phrase)) >= 8 and text.count(phrase) >= 3:
            first = text.find(phrase)
            return text.find(phrase, first + 1)          # the first saying stays, the repeats go
    return None
