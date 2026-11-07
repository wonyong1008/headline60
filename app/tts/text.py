"""Conservative Korean pronunciation rules. Unknown bare numbers remain for review."""
import re

LEXICON = {"HEADLINE 60": "헤드라인 육십", "GLP-1": "지엘피 원", "BMW": "비엠더블유",
           "SUV": "에스유브이", "GDP": "지디피", "AI": "에이아이", "IT": "아이티",
           "EV6": "이브이 식스", "EV9": "이브이 나인", "KTX": "케이티엑스"}


def sino(number: int) -> str:
    if number == 0:
        return "영"
    if number < 0:
        return "마이너스 " + sino(-number)
    result = ""
    for unit, label in ((10**12, "조"), (10**8, "억"), (10**4, "만")):
        value, number = divmod(number, unit)
        if value:
            result += ("" if value == 1 and label == "만" else sino(value)) + label
    for unit, label in ((1000, "천"), (100, "백"), (10, "십")):
        value, number = divmod(number, unit)
        if value:
            result += ("" if value == 1 else "영일이삼사오육칠팔구"[value]) + label
    return result + ("영일이삼사오육칠팔구"[number] if number else "")


def read_number(value: str) -> str:
    value = value.replace(",", "")
    if value.startswith(("-", "−")):
        return "마이너스 " + read_number(value[1:])
    if value.startswith("+"):
        return "플러스 " + read_number(value[1:])
    if "." in value:
        whole, decimal = value.split(".", 1)
        return sino(int(whole)) + "점" + "".join("영일이삼사오육칠팔구"[int(c)] for c in decimal)
    return sino(int(value))


# Newspaper hanja abbreviations the TTS model cannot read (left unread, generation runs away).
HANJA = {'與': '여', '野': '야', '李': '이', '文': '문', '尹': '윤', '朴': '박', '金': '김', '北': '북', '南': '남',
         '美': '미', '中': '중', '日': '일', '韓': '한', '英': '영', '獨': '독', '佛': '불', '露': '러', '靑': '청',
         '檢': '검', '軍': '군', '前': '전', '現': '현', '故': '고', '對': '대', '反': '반', '親': '친', '新': '신',
         '舊': '구', '大': '대', '小': '소', '高': '고', '低': '저', '弗': '불', '株': '주', '黨': '당', '政': '정',
         '法': '법', '市': '시', '道': '도', '國': '국', '人': '인', '女': '여', '男': '남', '父': '부', '母': '모'}


def normalize(text: str, lexicon: dict[str, str] | None = None) -> str:
    value = text
    for key, replacement in sorted({**LEXICON, **(lexicon or {})}.items(), key=lambda x: -len(x[0])):
        value = re.sub(r"(?<![A-Za-z0-9])" + re.escape(key) + r"(?![A-Za-z0-9])", replacement, value)
    # Calendar contexts are distinct from quantities and model identifiers.
    value = ''.join(HANJA.get(ch, ch) for ch in value)
    value = re.sub(r'[\u3400-\u4dbf\u4e00-\u9fff]', '', value)  # any remaining ideograph is unreadable: drop it
    value = re.sub(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일",
                   lambda m: f"{sino(int(m[1]))} 년 {('유' if int(m[2]) == 6 else '시' if int(m[2]) == 10 else sino(int(m[2])))}월 {sino(int(m[3]))} 일", value)
    units = {"%p": "퍼센트포인트", "%P": "퍼센트포인트", "퍼센트포인트": "퍼센트포인트",
             "km/h": "킬로미터", "㎞/h": "킬로미터",
             "%": "퍼센트", "퍼센트": "퍼센트", "조": "조", "억": "억", "만": "만",
             "원": "원", "건": "건", "배": "배", "년": "년", "일": "일",
             "개월": "개월", "mg": "밀리그램", "㎎": "밀리그램", "kg": "킬로그램",
             "km": "킬로미터", "㎞": "킬로미터"}
    # Match longest units first; never pronounce only a prefix of an unknown
    # Latin/composite unit (e.g. mg/dL, km/hours or %point).
    unit_pattern = "|".join(re.escape(unit) for unit in sorted(units, key=len, reverse=True))

    def quantity(match: re.Match) -> str:
        number, unit = read_number(match[1]), match[2]
        prefix = ""
        if unit in {"km/h", "㎞/h"} and not match.string[:match.start()].rstrip().endswith("시속"):
            prefix = "시속 "
        return prefix + number + " " + units[unit]

    value = re.sub(r"(?<![A-Za-z0-9.,+−-])([+−-]?\d[\d,]*(?:\.\d+)?)\s*("
                   + unit_pattern + r")(?![A-Za-z0-9_/²³^*·⋅])", quantity, value)
    return re.sub(r"[ \t]+", " ", value).strip()


def phrases(text: str, limit: int = 34) -> list[str]:
    """Split at sentence/clause/word boundaries, never infer time from character counts."""
    result = []
    for sentence in re.split(r"(?<=[.!?。])\s+|(?<=[,，])\s+", text.strip()):
        current = ""
        for word in sentence.split():
            if current and len(current) + len(word) + 1 > limit:
                result.append(current)
                current = ""
            current = (current + " " + word).strip()
        if current:
            result.append(current)
    # A long unbroken proper name is retained rather than inventing pronunciation.
    return result or [text.strip()]


def spoken_parts(narration: str, override: str | None = None) -> list[dict]:
    display = phrases(narration)
    spoken = [line.strip() for line in override.splitlines() if line.strip()] if override else []
    if spoken and len(spoken) != len(display):
        raise ValueError(f"발음용 대본은 자막 구절 수와 같은 {len(display)}줄로 입력하세요.")
    return [{"text": text, "spoken": spoken[i] if spoken else normalize(text)}
            for i, text in enumerate(display)]
