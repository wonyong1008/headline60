"""Single official-page previews; facts remain subject to explicit human review."""
import socket
from html.parser import HTMLParser
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

import httpx

from app.news.feeds import NewsError, public_url

OFFICIAL_DOMAINS = ("go.kr", "korea.kr", "bok.or.kr", "reb.or.kr", "hyundai.com", "kia.com",
                    "genesis.com", "nih.gov", "fda.gov", "who.int", "ema.europa.eu")


def official_url(url: str) -> bool:
    host = urlsplit(public_url(url)).hostname or ""
    return any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOMAINS)


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.lines: list[str] = []
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.skip = max(0, self.skip - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        value = " ".join(data.split())
        if not self.skip and value:
            self.lines.append(value)
            if self.in_title:
                self.title += value


def preview(url: str) -> dict:
    """No redirects to private networks, no media, no full-body retention."""
    url = public_url(url)
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
        for _ in range(4):
            if not official_url(url):
                raise ValueError("자동 미리보기는 등록된 공식 기관·제조사 도메인만 지원합니다.")
            host = urlsplit(url).hostname
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ip_address(a[4][0]).is_global for a in addresses):
                raise ValueError("공개 IP로 연결되는 공식 주소만 허용합니다.")
            try:
                with client.stream("GET", url, headers={"User-Agent": "Headline60/0.2 personal-research"}) as response:
                    if response.is_redirect:
                        url = public_url(urljoin(url, response.headers["location"]))
                        continue
                    response.raise_for_status()
                    if "text/html" not in response.headers.get("content-type", ""):
                        raise ValueError("HTML 공식 페이지를 입력하세요. PDF는 직접 열어 근거를 입력하세요.")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 2_000_000:
                            raise ValueError("공식 페이지가 너무 큽니다. 직접 확인 후 짧은 근거를 입력하세요.")
                parser = PageText()
                parser.feed(bytes(data).decode(response.encoding or "utf-8", errors="replace"))
                text = "\n".join(parser.lines)
                # Prefer body near the headline when HTML has a long navigation preamble.
                return {"url": url, "title": parser.title[:400], "preview": text[:18000],
                        "notice": "미리보기는 저장하지 않습니다. 필요한 짧은 근거만 선택하고 원문을 직접 확인하세요."}
            except (httpx.HTTPError, OSError) as exc:
                raise NewsError(f"공식 자료 미리보기 실패: {exc}") from exc
    raise NewsError("공식 페이지 리디렉션 횟수가 너무 많습니다.")
