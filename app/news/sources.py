"""Bounded public-page retrieval and extractive source receipts; no paywall/login bypass."""
import hashlib
import re
import socket
from datetime import date
from html.parser import HTMLParser
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

import httpx

from app.news.feeds import NewsError, public_url

USER_AGENT = 'Headline60/0.3 (local editorial research)'
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'wbr'}


def fetch_public(url: str, *, limit: int = 2_000_000, hosts: set[str] | None = None) -> tuple[str, bytes, str]:
    with httpx.Client(timeout=12, trust_env=False, follow_redirects=False) as client:
        for _ in range(4):
            url = public_url(url)
            host = urlsplit(url).hostname
            if hosts is not None and host not in hosts:
                raise ValueError('허용된 자료 서버가 아닙니다.')
            addresses = socket.getaddrinfo(host, 443 if url.startswith('https:') else 80, type=socket.SOCK_STREAM)
            if not addresses or any(not ip_address(a[4][0]).is_global for a in addresses):
                raise ValueError('공개 IP 자료만 불러올 수 있습니다.')
            with client.stream('GET', url, headers={'User-Agent': USER_AGENT}) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers['location'])
                    continue
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ValueError('자료가 자동 수집 크기 제한을 초과했습니다.')
                return url, bytes(data), response.headers.get('content-type', '')
    raise NewsError('자료의 리디렉션 횟수가 너무 많습니다.')


class SourceHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.meta = {}
        self.all_lines, self.body_lines, self.links = [], [], []
        self.current_link = None
        self.title = ''
        self.blocks, self.paragraphs, self.content_paragraphs = [], [], []
        self.container_lines = []

    def feed(self, data):
        # Newer html.parser reads <noscript> as raw text; an unclosed or tracking-pixel
        # noscript would then swallow the rest of the page. Its content is never article text.
        super().feed(re.sub(r'(?is)<noscript\b.*?</noscript\s*>', ' ', data))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            self.meta[attrs.get('property', attrs.get('name', '')).lower()] = attrs.get('content', '')
        if tag == 'a' and attrs.get('href'):
            self.current_link = [attrs['href'], '']
        # Known article containers, including schema.org articleBody used by many Korean news sites.
        specific = (bool(set(attrs.get('class', '').split()) & {
            'se-main-container', 'se_component_wrap', 'view_cont', 'article_view', 'view-cont', 'article_body', 'article-body'})
            or attrs.get('id') in {'postViewArea', 'newsViewArea', 'articleBody', 'article_body', 'articleBodyContents', 'dic_area'}
            or attrs.get('itemprop') == 'articleBody')
        selected = 2 if specific else 1 if tag in {'article', 'main'} else 0
        if tag not in VOID:
            self.stack.append((tag, selected))
        if tag in {"p", "li", "h1", "h2", "h3"}:
            self.blocks.append([tag, [], any(selected == 2 for _, selected in self.stack)])

    def handle_endtag(self, tag):
        for index in range(len(self.blocks) - 1, -1, -1):
            if self.blocks[index][0] == tag:
                text = ' '.join(self.blocks[index][1])
                if text:
                    self.paragraphs.append(text)
                    if self.blocks[index][2]:
                        self.content_paragraphs.append(text)
                del self.blocks[index:]
                break
        if tag == 'a' and self.current_link:
            self.links.append(tuple(self.current_link))
            self.current_link = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        # noscript is not skipped: pages often leave it unclosed, which would hide the whole article.
        if any(tag in {'script', 'style', 'nav', 'footer'} for tag, _ in self.stack):
            return
        value = ' '.join(data.replace('\u200b', '').split())
        if not value:
            return
        self.all_lines.append(value)
        for _, values, _specific in self.blocks:
            values.append(value)
        if any(selected for _, selected in self.stack):
            self.body_lines.append(value)
        if any(selected == 2 for _, selected in self.stack):
            # Some sites separate paragraphs only with <br>; keep those text runs too.
            self.container_lines.append(value)
        if any(tag == 'title' for tag, _ in self.stack):
            self.title += value
        if self.current_link:
            self.current_link[1] += value + ' '


def source_page(url: str) -> dict:
    original = public_url(url)
    parts = urlsplit(original)
    # The public mobile view exposes the same Naver post without its desktop iframe.
    if parts.hostname in {'blog.naver.com', 'm.blog.naver.com'}:
        match = re.fullmatch(r'/([A-Za-z0-9_-]+)/(\d+)/?', parts.path)
        if match:
            url = f'https://m.blog.naver.com/{match[1]}/{match[2]}'
    final, data, content_type = fetch_public(url)
    if 'text/html' not in content_type:
        raise ValueError('현재 공개 HTML 글만 지원합니다. 로그인·PDF·영상 전용 주소는 불러오지 않습니다.')
    html = data.decode('utf-8', errors='replace')
    parser = SourceHTML()
    parser.feed(html)
    # Prefer whichever reading of the article container holds more text (<p> blocks or <br> runs).
    # <p> blocks keep sentences whole (Naver splits text into spans); <br>-only articles end up
    # as one oversized <p>, so then the container's text runs are the real paragraphs.
    container = parser.content_paragraphs
    if not container or max(map(len, container)) > 600:
        container = parser.container_lines or container
    lines = list(dict.fromkeys(container or parser.paragraphs or parser.body_lines or parser.all_lines))
    if sum(len(line) for line in lines) < 100:
        raise ValueError('요약할 공개 본문을 찾지 못했습니다. 비공개·삭제·접근 제한 여부를 확인하세요.')
    title = parser.meta.get('og:title') or parser.title
    published, precision = publication_date(parser)
    return {'url': original, 'resolved_url': final, 'title': title[:300], 'lines': lines,
            'published_date': published, 'date_precision': precision,
            'publisher': parser.meta.get('og:site_name') or parts.hostname,
            'sha256': hashlib.sha256(data).hexdigest()}


def publication_date(parser: SourceHTML) -> tuple[str | None, str]:
    for key in ('article:published_time', 'datepublished', 'date', 'pubdate', 'article:modified_time', 'datemodified'):
        value = parser.meta.get(key, '')
        match = re.search(r'(20\d{2})[-./](\d{1,2})[-./](\d{1,2})', value)
        if match:
            try:
                result = date(*map(int, match.groups()))
                if result <= date.today():
                    return result.isoformat(), 'day'
            except ValueError:
                pass
    # Naver's publication timestamp and official Korean publication lines.
    for line in parser.all_lines:
        if re.fullmatch(r'20\d{2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2}\.?(?:\s+\d{1,2}:\d{2})?', line):
            try:
                result = date(*map(int, re.findall(r'\d+', line)[:3]))
                if result <= date.today():
                    return result.isoformat(), 'day'
            except ValueError:
                pass
        match = re.fullmatch(r'Last (?:Reviewed|Updated) ([A-Za-z]+) (20\d{2})', line)
        if match:
            from datetime import datetime
            result = datetime.strptime(' '.join(match.groups()), '%B %Y').date()
            if result <= date.today():
                return result.isoformat(), 'month'
    return None, 'unknown'


def terms(text: str) -> list[str]:
    words = re.findall(r'[가-힣A-Za-z]{2,}', text.lower())
    return list(dict.fromkeys(w for w in words if w not in {'뉴스', '발표', '관련', '것으로', '있습니다', '대한', '위한', 'the', 'and', 'with', 'from', 'this'}))
