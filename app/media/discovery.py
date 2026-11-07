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
                'CC-BY-4.0': 'https://creativecommons.org/licenses/by/4.0/',
                'CC-BY-SA-4.0': 'https://creativecommons.org/licenses/by-sa/4.0/',
                'CC-BY-3.0': 'https://creativecommons.org/licenses/by/3.0/',
                'CC-BY-SA-3.0': 'https://creativecommons.org/licenses/by-sa/3.0/',
                'CC-BY-2.0': 'https://creativecommons.org/licenses/by/2.0/',
                'CC-BY-SA-2.0': 'https://creativecommons.org/licenses/by-sa/2.0/'}
# When a scene's own search finds nothing, a topic-level photo still beats a plain graphic.
CATEGORY_QUERIES = {'realestate': 'apartment buildings Seoul', 'economy': 'stock exchange trading screen',
                    'auto': 'cars parking lot', 'health': 'pharmacy medicine shelf',
                    'politics': 'National Assembly building Seoul', 'trending': 'Seoul city street', 'tech': 'smartphone'}
# Backgrounds must be photographs; diagrams, maps and logos read as clutter behind text.
NOT_PHOTO = re.compile(r'chart|graph|diagram|\bmaps?\b|logo|icon|screenshot|comparison|table|\bplot|infographic|'
                       r'\bflag|coat of arms|\bseal\b|drawing|illustration|poster|document|\bscan|cartoon|sketch|sticker|anti-|banner', re.IGNORECASE)
GRIM = re.compile(r'crash|accident|burn|destroy|wreck|\bdead|corpse|blood|\bwar\b|memorial|funeral|\bfire\b|protest|riot|'
                  r'ardea|heron|\bbird|\binsect|spider|snake', re.IGNORECASE)
ABSTRACT = {'price', 'prices', 'decline', 'drop', 'rise', 'fall', 'increase', 'decrease', 'market', 'markets', 'comparison',
            'competition', 'policy', 'regulation', 'growth', 'trend', 'crisis', 'pressure', 'economy', 'cost', 'costs',
            'value', 'rate', 'rates', 'analysis', 'impact', 'effect', 'concept', 'news', 'issue', 'problem', 'risk'}
GENERIC = {'photo', 'image', 'file', 'jpg', 'jpeg', 'view', 'picture', 'scene', 'shot', 'close', 'closeup', 'background',
           'city', 'street', 'building', 'people', 'person', 'man', 'woman', 'day', 'night'} | ABSTRACT
DOWNLOAD_HOSTS = {'upload.wikimedia.org', 'thumb.wikimedia.org'}
EXPLICIT = re.compile(r'nud[ei]|naked|porn|sexual intercourse|genital|penis|vulva|vagina|masturbat|노출|성기|나체', re.IGNORECASE)


def plain(text: str) -> str:
    return ' '.join(unescape(re.sub('<[^>]*>', ' ', text)).split())


def query_for(scene) -> str:
    # The writer supplies '한국어 / english stock photo query'; Commons search works far better in English.
    if ' / ' in scene.visual_query:
        english = scene.visual_query.split(' / ', 1)[1].strip()
        if re.search(r'[a-z]{3,}', english, re.IGNORECASE):
            return english[:80]
    text = f'{scene.visual_query} {scene.headline} {scene.narration}'
    for pattern, query in [(r'케겔|발기|정력|운동|건강', 'exercise equipment gym'),
                           (r'아파트|주택|부동산', 'South Korea apartment buildings'),
                           (r'인출|ATM|현금', 'ATM machine'), (r'금리|대출|은행', 'bank building'),
                           (r'자동차|차량', 'car automobile'), (r'회사|직장|출근', 'office desk')]:
        if re.search(pattern, text, re.IGNORECASE):
            return query
    return scene.visual_query[:80]


KOREA = re.compile(r'korea|seoul|busan|incheon|daegu|gwangju|daejeon|한국|서울')


def search(query: str, require_korea: bool = False) -> list[dict]:
    params = {'action': 'query', 'format': 'json', 'generator': 'search', 'gsrsearch': query + ' -filetype:svg -intitle:nude',
              'gsrnamespace': 6, 'gsrlimit': 15, 'prop': 'imageinfo', 'iiprop': 'url|extmetadata|size', 'iiurlwidth': 960}
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
        grim = GRIM.search(name + ' ' + description)
        if NOT_PHOTO.search(name) or (grim and grim.group(0).lower().strip('-') not in query.lower()):
            continue
        if meta.get('NonFree', {}).get('value') == 'true' or value('Restrictions'):
            continue
        # Search relevance remains required even when the license is acceptable: at least one
        # content word of the query (not a generic/abstract word) must appear in the file's name
        # or description, so 'pill bottle' never answers a query about an approval poll.
        haystack = (name + ' ' + description).lower()
        content_words = [word for word in terms(query) if word not in GENERIC and len(word) >= 3]
        if content_words and not any(word in haystack for word in content_words):
            continue
        if (require_korea or KOREA.search(query.lower())) and not KOREA.search(haystack):
            continue  # Korean politics/news must not be illustrated with another country's parliament
        if min(info.get('width', 0), info.get('height', 0)) < 640:
            continue
        download = info.get('thumburl') or info.get('url', '')
        if not re.search(r'\.jpe?g(?:\?|$)', download, re.IGNORECASE):  # photos, not PNG graphics
            continue
        results.append({'name': name[:180], 'author': author[:500], 'source_url': info.get('descriptionurl', ''),
                        'download_url': download, 'license_type': license_type, 'license_url': LICENSE_URLS[license_type],
                        'description': description[:400]})
    return results


# Politics stories get institutional Korean scenes only; free-text searches return party
# conventions, protests or unrelated "Korea" photos that misrepresent the story.
# Symbolic photos for abstract topics (dream readings, fortune, money luck): the words a scene
# uses decide the picture, so "재물" shows gold or cash and "흉몽" shows storm clouds.
THEMES = [
    (r'금은보화|보석|황금|금괴|금\b', ['gold bars', 'gold jewelry', 'gold coins pile']),
    (r'재물|돈|금전|부자|수익|이익|월급|재산', ['stack of cash money', 'gold coins pile', 'piggy bank coins', 'gold bars']),
    (r'복권|로또|당첨', ['lottery ticket', 'bingo balls', 'stack of cash money']),
    (r'행운|길몽|복이|좋은 소식|기쁜|기회', ['four leaf clover', 'maneki neko lucky cat', 'sunrise over sea', 'rainbow sky']),
    (r'불행|흉몽|불길|나쁜|걱정|근심|위험|손해|사고', ['dark storm clouds', 'rain on window', 'broken glass', 'fog forest path']),
    (r'태몽|임신|출산|아기', ['baby shoes', 'newborn baby hand', 'pregnant woman silhouette']),
    (r'돼지', ['pig farm piglet', 'pig farm', 'piggy bank coins']),
    (r'뱀', ['snake grass', 'snake coiled']),
    (r'용\b|용띠|용이', ['dragon statue Korea', 'dragon sculpture temple']),
    (r'호랑이', ['tiger', 'tiger painting']),
    (r'물고기|잉어', ['koi fish pond', 'goldfish']),
    (r'물|바다|강물|홍수', ['ocean waves', 'river water flowing', 'rain flood street']),
    (r'불\b|화재|불타', ['bonfire flames', 'candle flame dark']),
    (r'죽음|시체|장례', ['cemetery fog', 'white chrysanthemum']),
    (r'사랑|연애|결혼|연인', ['couple holding hands', 'wedding rings', 'red roses bouquet']),
    (r'시험|합격|공부|학교', ['library bookshelves', 'graduation cap', 'pencil notebook desk']),
    (r'직장|회사|승진|사업|일자리', ['office desk laptop', 'business handshake', 'skyscraper office building']),
    (r'건강|병원|아프|치료', ['hospital corridor', 'stethoscope doctor', 'medicine pills']),
    (r'꿈|잠|수면|침대', ['bed sleeping night', 'full moon night sky', 'starry night sky', 'pillow bedroom morning']),
    (r'집|이사|아파트|부동산', ['apartment buildings Seoul', 'keys door lock', 'moving boxes']),
    (r'자동차|운전|차\b', ['car driving road', 'car keys']),
    (r'음식|먹는|요리|밥', ['Korean food table', 'cooking kitchen']),
    (r'여행|비행기|해외', ['airplane window', 'suitcase airport', 'passport travel']),
]


def theme_queries(text: str) -> list[str]:
    """Symbolic search terms for the words in a scene, most specific first."""
    found = []
    for pattern, queries in THEMES:
        if re.search(pattern, text):
            found.extend(queries)
    return list(dict.fromkeys(found))


# Neutral Korean everyday scenes for stories with no concrete subject (dreams, fortune, advice).
NEUTRAL_SCENES = ['Seoul city street', 'Han River park Seoul', 'Namsan Seoul Tower', 'Seoul night skyline',
                  'Seoul subway station', 'Gwanghwamun Square Seoul', 'Busan Haeundae beach', 'Seoul apartment buildings']
POLITICS_SCENES = ['National Assembly building Yeouido Seoul', 'Blue House Seoul', 'Gwanghwamun Square Seoul',
                   'Government Complex Sejong', 'National Assembly Seoul', 'Seoul City Hall', 'Yeouido Seoul skyline',
                   'Gyeongbokgung Seoul']


def query_variants(scene, category: str, index: int = 0) -> list[str]:
    """Scene query, then the same without abstract words, then a topic-level photo."""
    if category == 'politics':
        rotated = POLITICS_SCENES[index % len(POLITICS_SCENES):] + POLITICS_SCENES[:index % len(POLITICS_SCENES)]
        return rotated
    first = query_for(scene)
    themed = theme_queries(f'{scene.headline} {scene.narration} {scene.visual_query}')
    themed = themed[index % len(themed):] + themed[:index % len(themed)] if themed else []
    if category == 'trending':
        # Blogs about dreams, fortune or advice have no real subject to photograph: the scene's
        # words pick symbolic pictures (gold, cash, clover, storm clouds…), then one Korean attempt
        # for the stated subject, then neutral everyday Korean scenes in rotation.
        rotated = NEUTRAL_SCENES[index % len(NEUTRAL_SCENES):] + NEUTRAL_SCENES[:index % len(NEUTRAL_SCENES)]
        return list(dict.fromkeys([*themed[:3], f'{first} Korea', *rotated]))
    words = [w for w in first.split() if w.lower() not in ABSTRACT]
    # Commons matches every word, so long queries often return nothing; try a shorter subject too.
    base = list(dict.fromkeys(q for q in (first, ' '.join(words), ' '.join(words[:2]) if len(words) > 2 else '') if q))[:3]
    # Korean news deserves Korean scenes: each subject with "Korea" first, then plain.
    ordered = []
    for q in base:
        if 'korea' not in q.lower() and 'seoul' not in q.lower():
            ordered.append(f'{q} Korea')
        ordered.append(q)
    return list(dict.fromkeys([*ordered, *themed[:2], CATEGORY_QUERIES.get(category, CATEGORY_QUERIES['trending'])]))


def ensure_media(config: Settings, script, category: str) -> dict:
    """Give every scene its own licensed photo where possible and pin it to that scene."""
    started, imported, errors, searched = time.perf_counter(), [], [], set()
    def korean_enough(asset):
        text = ' '.join([asset.get('name', ''), asset.get('description', ''), *asset.get('tags', [])]).lower()
        return category != 'politics' or bool(KOREA.search(text))
    assets = [a for a in listing(config) if a['category'] == category and a['usage_status'] == 'approved'
              and a['media_type'] in {'image', 'video'} and not NOT_PHOTO.search(a.get('name', '')) and korean_enough(a)]
    results, used, offline = {}, set(), False
    for index, scene in enumerate(script.scenes):
        if scene.asset_id or scene.chart or scene.visual_type == 'data_chart':
            continue
        match = None if category in {'politics', 'trending'} else select_visual(scene, [a for a in assets if a['asset_id'] not in used], 0)
        if match:
            # Reuse a stored photo only on a real subject match (two content words), not one loose tag.
            subject = {w for w in terms(query_for(scene)) if w not in GENERIC and len(w) >= 3}
            text = ' '.join([match.get('name', ''), match.get('description', ''), *match.get('tags', [])]).lower()
            if len([w for w in subject if w in text]) < 2:
                match = None
        if match:
            used.add(match['asset_id'])
            scene.asset_id = match['asset_id']
            continue
        for query in query_variants(scene, category, index):
            if offline or len(searched) >= 40 or time.perf_counter() - started > 150:
                break
            if query not in results:
                searched.add(query)
                try:
                    results[query] = search(query, require_korea=category == 'politics')
                except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
                    errors.append(str(exc)[:200])
                    results[query] = []
                    offline = isinstance(exc, httpx.TransportError)  # stop instead of retrying every scene
                    continue
            asset = None
            for candidate in results[query]:
                previous = next((a for a in listing(config) if a['source_url'] == candidate['source_url']), None)
                if previous:
                    if previous['category'] == category and previous['asset_id'] not in used:
                        asset = previous
                        break
                    continue
                try:
                    final, content, content_type = fetch_public(candidate['download_url'], limit=12 * 1024**2,
                                                                hosts=DOWNLOAD_HOSTS)
                    if not content_type.startswith('image/'):
                        continue
                    asset = register_upload(config, content, name=candidate['name'], category=category,
                        license_type=candidate['license_type'], source_url=candidate['source_url'],
                        license_url=candidate['license_url'], rights_confirmed=True,
                        attribution=f"{candidate['author']} · {candidate['source_url']} · {candidate['license_type']} · 변경: 세로 크롭·색상/자막 합성",
                        tags=[t[:60] for t in dict.fromkeys(terms(scene.visual_query)[:12] + terms(scene.headline)[:6] + terms(query)[:8])][:24],
                        description='자동 검색 자료화면 · 실제 사건·특정인의 건강 상태를 나타내지 않음. ' + candidate['description'][:300],
                        rights_basis='license_metadata')
                except (httpx.HTTPError, OSError, ValueError, KeyError, RuntimeError) as exc:
                    errors.append(str(exc)[:200])  # one bad file never stops the others
                    continue
                assets.append(asset)
                imported.append({'asset_id': asset['asset_id'], **candidate, 'download_url': final})
                break
            if asset:
                used.add(asset['asset_id'])
                scene.asset_id = asset['asset_id']
                break
        if not scene.asset_id:
            # Last resort: any approved photo in the library, so no scene falls back to a plain graphic.
            pool = [a for a in listing(config) if a['usage_status'] == 'approved' and a['media_type'] == 'image'
                    and not NOT_PHOTO.search(a.get('name', '')) and a['asset_id'] not in used and korean_enough(a)]
            def neutral(asset):  # city/landscape scenes read fine behind any topic; pills or cars do not
                text = ' '.join([asset.get('name', ''), asset.get('description', ''), *asset.get('tags', [])]).lower()
                return not re.search(r'seoul|korea|city|street|skyline|river|park|building', text)
            pool.sort(key=lambda a: (neutral(a), a['category'] != category, a.get('created_at', '')))
            if pool:
                used.add(pool[0]['asset_id'])
                scene.asset_id = pool[0]['asset_id']
                errors.append(f'장면 {index + 1}: 관련 사진이 없어 보관 사진을 사용했습니다.')
    result = {'imported': imported, 'queries': sorted(searched), 'errors': errors,
              'seconds': round(time.perf_counter() - started, 3),
              'notice': '라이선스와 저작자 메타데이터가 확인된 자료만 가져옵니다. 실패 시 기존 관련 자료 또는 브랜드 그래픽을 사용합니다.'}
    if searched:
        from uuid import uuid4
        folder = config.data / 'media' / 'discovery'
        folder.mkdir(parents=True, exist_ok=True)
        save_json(folder / (uuid4().hex + '.json'), result)
    return result
