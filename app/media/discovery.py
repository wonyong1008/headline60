"""Optional bounded Commons discovery. Unavailable/unlicensed media never blocks rendering."""
import json
import re
import time
from html import unescape
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.media.library import listing, register_upload
from app.news.sources import fetch_public, terms
from app.process import save_json
from app.renderer.storyboard import select_visual

LICENSE_URLS = {'CC0': 'https://creativecommons.org/publicdomain/zero/1.0/',
                'CC-BY-4.0': 'https://creativecommons.org/licenses/by/4.0/'}
EXPLICIT = re.compile(r'nud[ei]|naked|porn|sexual intercourse|genital|penis|vulva|vagina|masturbat|노출|성기|나체', re.IGNORECASE)


def plain(text: str) -> str:
    return ' '.join(unescape(re.sub('<[^>]*>', ' ', text)).split())


def query_for(scene) -> str:
    text = f'{scene.visual_query} {scene.headline} {scene.narration}'
    for pattern, query in [(r'케겔|발기|정력|운동|건강', 'exercise equipment gym'),
                           (r'아파트|주택|부동산', 'South Korea apartment buildings'),
                           (r'인출|ATM|현금', 'ATM machine'), (r'금리|대출|은행', 'bank building'),
                           (r'자동차|차량', 'car automobile'), (r'회사|직장|출근', 'office desk')]:
        if re.search(pattern, text, re.IGNORECASE):
            return query
    return scene.visual_query[:80]


def search(query: str) -> list[dict]:
    params = {'action': 'query', 'format': 'json', 'generator': 'search', 'gsrsearch': query + ' -filetype:svg -intitle:nude',
              'gsrnamespace': 6, 'gsrlimit': 5, 'prop': 'imageinfo', 'iiprop': 'url|extmetadata|size', 'iiurlwidth': 960}
    _, data, _ = fetch_public('https://commons.wikimedia.org/w/api.php?' + urlencode(params),
                              hosts={'commons.wikimedia.org'})
    response = json.loads(data)
    results = []
    for page in response.get('query', {}).get('pages', {}).values():
        info = (page.get('imageinfo') or [{}])[0]
        meta = info.get('extmetadata', {})
        value = lambda key, metadata=meta: plain(metadata.get(key, {}).get('value', ''))
        name, author, license_url = page.get('title', ''), value('Artist'), value('LicenseUrl').replace('http:', 'https:')
        license_type = next((kind for kind, url in LICENSE_URLS.items() if license_url.rstrip('/') == url.rstrip('/')), None)
        description = value('ImageDescription')
        if not license_type or not author or EXPLICIT.search(name + ' ' + description):
            continue
        if meta.get('NonFree', {}).get('value') == 'true' or value('Restrictions'):
            continue
        # Search relevance remains required even when the license is acceptable.
        if not any(word in (name + ' ' + description).lower() for word in terms(query)):
            continue
        if min(info.get('width', 0), info.get('height', 0)) < 640:
            continue
        download = info.get('thumburl') or info.get('url', '')
        if not re.search(r'\.(?:jpe?g|png|webp)(?:\?|$)', download, re.IGNORECASE):
            continue
        results.append({'name': name[:180], 'author': author[:500], 'source_url': info.get('descriptionurl', ''),
                        'download_url': download, 'license_type': license_type, 'license_url': LICENSE_URLS[license_type],
                        'description': description[:400]})
    return results


def ensure_media(config: Settings, script, category: str) -> dict:
    started, imported, errors, searched = time.perf_counter(), [], [], set()
    assets = [a for a in listing(config) if a['category'] == category and a['usage_status'] == 'approved'
              and a['media_type'] in {'image', 'video'}]
    for index, scene in enumerate(script.scenes):
        if scene.asset_id or scene.chart or scene.visual_type == 'data_chart' or select_visual(scene, assets, index):
            continue
        query = query_for(scene)
        if not query or query in searched or len(searched) >= 2 or time.perf_counter() - started > 22:
            continue
        searched.add(query)
        try:
            for candidate in search(query):
                previous = next((a for a in listing(config) if a['source_url'] == candidate['source_url']), None)
                if previous:
                    if previous['category'] == category:
                        assets.append(previous)
                    continue
                final, content, content_type = fetch_public(candidate['download_url'], limit=12 * 1024**2,
                                                            hosts={'upload.wikimedia.org'})
                if not content_type.startswith('image/'):
                    continue
                asset = register_upload(config, content, name=candidate['name'], category=category,
                    license_type=candidate['license_type'], source_url=candidate['source_url'],
                    license_url=candidate['license_url'], rights_confirmed=True,
                    attribution=f"{candidate['author']} · {candidate['source_url']} · {candidate['license_type']} · 변경: 세로 크롭·색상/자막 합성",
                    tags=terms(scene.visual_query)[:12] + terms(query)[:8],
                    description='자동 검색 자료화면 · 실제 사건·특정인의 건강 상태를 나타내지 않음. ' + candidate['description'][:300],
                    rights_basis='license_metadata')
                assets.append(asset)
                imported.append({'asset_id': asset['asset_id'], **candidate, 'download_url': final})
                break
        except (httpx.HTTPError, OSError, ValueError, KeyError, RuntimeError) as exc:
            errors.append(str(exc)[:200])
    result = {'imported': imported, 'queries': sorted(searched), 'errors': errors,
              'seconds': round(time.perf_counter() - started, 3),
              'notice': '라이선스와 저작자 메타데이터가 확인된 자료만 가져옵니다. 실패 시 기존 관련 자료 또는 브랜드 그래픽을 사용합니다.'}
    if searched:
        from uuid import uuid4
        folder = config.data / 'media' / 'discovery'
        folder.mkdir(parents=True, exist_ok=True)
        save_json(folder / (uuid4().hex + '.json'), result)
    return result
