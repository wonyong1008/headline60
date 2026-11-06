"""Automatically discover official sources; only matched excerpts enter generation."""
import hashlib
import re
import time
from datetime import date, datetime
from urllib.parse import urlencode, urljoin

import httpx

from app.news.evidence import official_url
from app.news.feeds import NewsError
from app.news.sources import SourceHTML, fetch_public, source_page, terms

ED_SOURCE = 'https://www.niddk.nih.gov/health-information/urologic-diseases/erectile-dysfunction/treatment'
# Bump when extraction changes; unreviewed automatic evidence is then rebuilt.
SOURCE_EXTRACT_VERSION = 4
HEALTH = re.compile(r'건강|의약|치료|부작용|발기|정력|케겔|피임|임신|다이어트|탈모|영양제')


def category_for(text: str) -> str:
    for category, pattern in [('health', HEALTH), ('realestate', re.compile(r'부동산|아파트|주택|전세')),
                               ('auto', re.compile(r'자동차|신차|전기차')), ('economy', re.compile(r'금리|대출|은행|경제'))]:
        if pattern.search(text):
            return category
    return 'trending'


def discover(query: str) -> list[str]:
    url = 'https://www.korea.kr/totalSearch.do?' + urlencode({'where': 'ALL', 'topSearchKeyword': query[:100]})
    final, data, _ = fetch_public(url)
    parser = SourceHTML()
    parser.feed(data.decode('utf-8', errors='replace'))
    wanted = terms(query)
    ranked = []
    for href, label in parser.links:
        link = urljoin(final, href)
        if '/news/' in link and 'View.do?' in link and official_url(link):
            score = sum(word in label.lower() for word in wanted)
            if score:
                ranked.append((score, link))
    return list(dict.fromkeys(url for _, url in sorted(ranked, reverse=True)))[:5]


def excerpts(page: dict, query: str, *, official_only: bool = True) -> list[dict]:
    if not page['published_date'] or (official_only and not official_url(page['url'])):
        return []
    wanted = terms(query)
    if re.search(r'발기|정력|케겔', query):
        wanted += ['physical activity', 'exercise', 'erectile', 'blood flow', 'treatment']
    title_numbers = set(re.findall(r'\d+(?:[.,]\d+)*', page.get('title', '')))
    ranked = []
    lead = True
    for index, line in enumerate(page['lines']):
        # Full clauses only; do not truncate a limitation or harvest navigation.
        if re.match(r'^\[\d+\]', line):
            continue
        if not 40 <= len(line) <= 600 or any(term in line for term in ('무단 전재', '재배포 금지', '로그인', '구독 신청')):
            continue
        if not re.search(r'[.!?]|[다요]$', line):
            continue
        score = sum(word in line.lower() for word in wanted)
        # The lead paragraph and figures from the headline carry the core claim.
        score += 2 * len(title_numbers & set(re.findall(r'\d+(?:[.,]\d+)*', line)))
        if lead:
            score += 1 if score else 0
            lead = False
        if score:
            ranked.append((score, index, line))
    # Ties prefer earlier paragraphs (news is written lead-first).
    selected = sorted(sorted(ranked, key=lambda item: (-item[0], item[1]))[:4], key=lambda item: item[1])
    result = []
    title = page.get('title', '').strip()
    if official_url(page['url']) and 10 <= len(title) <= 300:
        # The official publisher's own headline summarizes the announcement it published.
        label = page['published_date'][:7] if page['date_precision'] == 'month' else page['published_date']
        title_item = {'id': hashlib.sha256((page['url'] + '#title' + title).encode()).hexdigest()[:12],
            'url': page['url'], 'publisher': page['publisher'][:100], 'title': title,
            'published_date': page['published_date'], 'accessed_date': date.today().isoformat(),
            'fact': title, 'excerpt': title,
            'conditions': f'공식 원문 제목(발표 요지). 원문 날짜 {label}. 세부 조건은 본문 발췌를 따른다. 자동 발췌이며 최종 대본 검토 필요.',
            'reviewed': False, 'permission': 'official', 'verification': 'source_matched',
            'source_sha256': page['sha256'], 'date_precision': page['date_precision']}
    else:
        title_item = None
    for _, _, quote in selected:
        first = re.split(r'(?<=[.!?])\s+', quote)[0]
        fact = quote if len(quote) <= 300 else first
        if len(fact) > 300 or len(fact) < 10:
            continue
        label = page['published_date'][:7] if page['date_precision'] == 'month' else page['published_date']
        result.append({'id': hashlib.sha256((page['url'] + quote).encode()).hexdigest()[:12],
            'url': page['url'], 'publisher': page['publisher'][:100], 'title': page['title'],
            'published_date': page['published_date'], 'accessed_date': date.today().isoformat(),
            'fact': fact, 'excerpt': quote, 'conditions': f'원문 날짜 {label} ({page["date_precision"]}). 원문의 대상·조건·가능성 표현을 유지. 자동 발췌이며 최종 대본 검토 필요.',
            'reviewed': False, 'permission': 'official' if official_url(page['url']) else 'allowed',
            'verification': 'source_matched', 'source_sha256': page['sha256'], 'date_precision': page['date_precision']})
    # The headline is added only alongside body excerpts, never as the sole evidence.
    return [*result, title_item] if result and title_item else result


def source_outline(page: dict) -> list[dict]:
    """Original section names document what the input covers, never clinical efficacy."""
    headings = [line for line in page['lines'] if re.match(r'^\d+[.)]\s*', line) and len(line) <= 50]
    bounded = []
    for heading in headings[:8]:
        if len('\n'.join([*bounded, heading])) > 280:
            break
        bounded.append(heading)
    quote = '\n'.join(bounded)
    if len(quote) < 10 or not page['published_date']:
        return []
    return [{'id': hashlib.sha256((page['url'] + quote).encode()).hexdigest()[:12],
        'url': page['url'], 'publisher': page['publisher'][:100], 'title': page['title'],
        'published_date': page['published_date'], 'accessed_date': date.today().isoformat(),
        'fact': quote, 'excerpt': quote, 'conditions': '원문의 구성·항목 이름만 확인합니다. 효과·인과·치료를 입증하는 근거로 사용할 수 없습니다.',
        'reviewed': False, 'permission': 'allowed', 'scope': 'source_outline',
        'verification': 'source_matched', 'source_sha256': page['sha256'], 'date_precision': page['date_precision']}]


def gather(draft: dict, source: dict | None = None) -> tuple[list[dict], dict]:
    started = time.perf_counter()
    articles = draft.get('articles', [])
    query = draft.get('topic') or (articles[0]['title'] if articles else '')
    existing = draft.get('evidence', [])
    candidates, pages, errors = [], [], []
    medical = draft.get('category') == 'health' or draft.get('content_mode') == 'myth_check' or bool(HEALTH.search(query))
    if source is not None and not medical:
        pages.append(source)
    if re.search(r'발기|정력|케겔', query):
        candidates.append(ED_SOURCE)
    for article in articles:
        if official_url(article['url']):
            candidates.append(article['url'])
    # General URL summaries use the supplied original. News/myths discover primary material.
    if not pages and not candidates:
        keywords = re.findall(r'기준금리|아파트|주택|전세|대출|탈모|다이어트|의약품|자동차|전기차|인공지능|연금|금리|건강', query)
        searches = [query, ' '.join(dict.fromkeys(keywords)) or ' '.join(terms(query)[:3])]
        for search in dict.fromkeys(searches):
            try:
                candidates.extend(discover(search))
            except (httpx.HTTPError, OSError, ValueError, NewsError) as exc:
                errors.append(f'공식 검색: {str(exc)[:180]}')
            if candidates or time.perf_counter() - started > 20:
                break
    known_urls = {page['url'] for page in pages}
    for url in dict.fromkeys(candidates):
        if url in known_urls:
            continue
        if len(pages) >= 2 or time.perf_counter() - started > 35:
            break
        try:
            page = source_page(url)
            # A recent news item must not silently receive evidence about an older event.
            if not draft.get('source_url') and articles and page['published_date']:
                dated = [a.get('published_at') for a in articles if a.get('published_at')]
                if dated and all(abs((date.fromisoformat(page['published_date']) - datetime.fromisoformat(d).date()).days) > 14 for d in dated):
                    errors.append(f'발표 시점 불일치: {url}')
                    continue
            if excerpts(page, query, official_only=medical or source is None):
                pages.append(page)
                known_urls.add(page['url'])
        except (httpx.HTTPError, OSError, ValueError, NewsError) as exc:
            errors.append(f'{url}: {str(exc)[:180]}')
    found = [item for page in pages for item in excerpts(page, query, official_only=medical or source is None)]
    if source is not None and medical and not official_url(source['url']):
        found += source_outline(source)
    seen = {e['id'] for e in existing}
    additions = []
    for item in found:
        if item['id'] not in seen:
            additions.append(item)
            seen.add(item['id'])
    report = {'status': 'ready' if additions or existing else 'needs_review',
              'added': len(additions), 'seconds': round(time.perf_counter() - started, 3),
              'sources': list(dict.fromkeys(e['url'] for e in additions)), 'errors': errors,
              'notice': '원문 일치 발췌를 자동 연결했습니다. 의료·성 관련 내용의 최종 수동 검토는 유지합니다.' if additions else '관련 공개 원문·날짜·본문을 확보하지 못했습니다. 내용을 지어내지 않고 보류합니다.'}
    return [*existing, *additions][:20], report
