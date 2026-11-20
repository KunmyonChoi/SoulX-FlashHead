"""
Does the customer's latest utterance read as consent to the change that was just proposed?

Three answers: "yes", "no", "unclear". Only "yes" lets a write tool run. The reader errs on the
side of "unclear": a wrongly refused consent costs one more question, a wrongly read one changes
something the customer did not ask for.

  yes     an affirmative is present, no negative is present, and the utterance is not a question
  no      a negative is present ("아니요", "취소", "잠깐만요", "하지 마세요", "환불 말고 ...")
  unclear everything else: silence, a question ("네?", "얼마라고요?"), an unrelated remark
"""
import re

# A refusal, a hold, or a change of mind. Any of these wins over an affirmative in the same
# utterance ("네, 아니 잠깐만요").
_NEGATIVE = re.compile(
    r"아니|아뇨|안\s?돼|안\s?됩|안\s?할|안\s?해|않|취소|잠깐|잠시만|기다려|하지\s?마|하지\s?말|지\s?마|지\s?말|말고|말아|"
    r"싫|그만|됐어|됐습니다|나중에|다음에|보류|필요\s?없|생각해\s?볼|생각\s?좀|다시\s?생각|몰라|모르겠|글쎄|"
    r"\b(no|nope|nah|not|don'?t|do not|never|cancel|wait|stop|hold on|later)\b",
    re.IGNORECASE)

# Short words that are consent only when they stand alone as a word ("네", "네네", "네요" is not).
_AFFIRM_WORD = re.compile(
    r"(?<![가-힣A-Za-z])("
    r"네+|넵|넹|예|옙|응|엉|그래|그래요|그럼요|그러세요|그러죠|그럽시다|맞아|맞아요|맞습니다|맞네요|"
    r"좋아|좋아요|좋습니다|오케이|오케|물론|물론이죠|물론입니다|당연하죠|알겠어요|알겠습니다|동의|동의해요|동의합니다"
    r")(?![가-힣A-Za-z])")

# Requests to go ahead; these may be glued to other words ("환불해주세요", "그렇게진행해주세요").
_AFFIRM_PHRASE = re.compile(
    r"해\s?주세요|해\s?줘|해\s?주십시오|해\s?주시면|해\s?주실|해\s?줄래|부탁|진행해|진행하|접수해|접수하|처리해|"
    r"그렇게\s?해|그렇게\s?하|하겠습니다|할게요|동의합|동의해|"
    r"\b(yes|yeah|yep|yup|sure|ok|okay|please|go ahead|do it|correct|right|confirm|confirmed|agreed?|absolutely)\b",
    re.IGNORECASE)

# The action itself, said as a command: "보내.", "등록해", "추가해 줘", "잡아 주세요" (2026-11-20: "보내." and
# "등록해" were read as unclear, and the user had to say yes twice). "보내지 마" is caught by _NEGATIVE first.
_AFFIRM_ACTION = re.compile(
    r"(?<![가-힣])(보내|등록해|추가해|잡아|넣어|예약해|결제해|저장해|올려)\s?(줘|주세요|주십시오|요|라)?(?![가-힣])")

# "... 해 주세요" about something other than the proposed change: asking to repeat, explain, look
# up, or be put through. Not a refusal, but not consent either.
_OTHER_REQUEST = re.compile(
    r"(말|말씀|설명|얘기|이야기|확인|안내|조회|검색|연결|교환|변경|수정|반복|전달)\s?해\s?(주|줘|줄)\S*")


def read_consent(text):
    """ "yes" | "no" | "unclear" """
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return "unclear"
    if _NEGATIVE.search(text):
        return "no"
    if text.endswith(("?", "？")):       # "네?", "얼마라고요?": asking back is not agreeing
        return "unclear"
    if _AFFIRM_WORD.search(text) or _AFFIRM_ACTION.search(text) or _AFFIRM_PHRASE.search(_OTHER_REQUEST.sub(" ", text)):
        return "yes"
    return "unclear"


def is_consent(text):
    return read_consent(text) == "yes"
