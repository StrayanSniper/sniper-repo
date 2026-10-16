"""
extractor_viprow.py — schedule scraper + stream extractor for viprow.im rugby.

Schedule
--------
viprow renders its schedule client-side, but the data isn't fetched from an
API — it is inlined in the page as `window.ftbPageData = { schedule: [...],
league: {...} }`. Game pages carry the same blob keyed by game id, including
a JSON `links` list of third-party embed pages (empty until near kick-off).

Streams
-------
Links point at wrapper pages (todaystreams.cc etc.) that nest iframes down to
the real player. We follow iframes until a page exposes an .m3u8 URL, then
serve it through the same local HLS proxy as the rugbybox extractor.

Known hosts:
  todaystreams.cc/play/N -> dlive.sx -> dembed.st/premiumtv/daddy.php
      `const SRC = "...m3u8"`. Segments are disguised as PNG images on a
      TikTok CDN: the PNG pixel data is "TIKTIKPX" + u32 length + a gzip
      stream of MPEG-TS. _unwrap_segment() decodes them in the proxy.
  streame.center/embed/chN.php -> hls.php
      Tokenised m3u8, needs the player page as Referer.
Not supported (resolution fails cleanly): flyembed.cfd (encrypted player
config), abisnews/liveon5 "sonic" (Flussonic session + heartbeat).
"""

import re
import sys
import os
import json
import gzip
import time
import zlib
import struct
import threading
import urllib.parse

_lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib')
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

import requests as _requests

import extractor_runner_no_chrome as _base

UA        = _base.UA
SITE_BASE = 'https://viprow.im'
SPORT     = 'rugby'

MAX_IFRAME_DEPTH = 5

# Ad / analytics iframes that are never the player
_IFRAME_SKIP = ('about:blank', 'googletagmanager', 'doubleclick', 'histats', 'facebook')


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _origin(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f'{p.scheme}://{p.netloc}'


def _get(url: str, referer: str = '') -> str:
    headers = {
        'User-Agent':      UA,
        'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-GB,en;q=0.5',
    }
    if referer:
        headers['Referer'] = referer
    r = _requests.get(url, headers=headers, timeout=15, allow_redirects=True)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def _page_data_value(html: str, key: str):
    """Decode one JSON value from the inline `window.ftbPageData = {...}` blob."""
    start = html.find('ftbPageData')
    if start < 0:
        raise RuntimeError('viprow page data not found')
    m = re.compile(r'\b' + key + r'\s*:\s*').search(html, start)
    if not m:
        raise RuntimeError(f'viprow page data has no "{key}"')
    value, _ = json.JSONDecoder().raw_decode(html, m.end())
    return value


def _game_slug(name: str) -> str:
    """Mirror of Schedule.js link building (the server only uses the id)."""
    slug = re.sub(r'[^\w\s\-.:&\'()]', '', name, flags=re.UNICODE).strip()
    slug = re.sub(r'\s+', ' ', slug.replace('-', ' ')).replace(' ', '-').lower()
    return urllib.parse.quote(slug, safe='-')


def get_games() -> list:
    """
    Return games that haven't finished, sorted by kick-off:
    [{'id', 'name', 'league', 'start' (UTC epoch), 'end' (UTC epoch), 'url'}]
    """
    html     = _get(f'{SITE_BASE}/sports/{SPORT}/')
    schedule = _page_data_value(html, 'schedule')
    leagues  = _page_data_value(html, 'league') or {}

    now   = time.time()
    games = []
    for g in schedule or []:
        try:
            start = int(g['start'])
        except (KeyError, TypeError, ValueError):
            continue
        league = leagues.get(str(g.get('league_id')), {})
        # The schedule's own `end` field is unreliable (often < start)
        try:
            duration = int(league.get('max_duration') or 180) * 60
        except ValueError:
            duration = 180 * 60
        if now >= start + duration:
            continue
        games.append({
            'id':     g['id'],
            'name':   g.get('name', '').strip(),
            'league': league.get('name', ''),
            'start':  start,
            'end':    start + duration,
            'url':    f"{SITE_BASE}/sports/{SPORT}/{_game_slug(g.get('name', ''))}/{g['id']}/",
        })
    games.sort(key=lambda x: x['start'])
    return games


def get_links(game_url: str) -> list:
    """Return the stream links for one game: [{'name', 'url', 'quality'}]."""
    m = re.search(r'/(\d+)/?$', game_url)
    if not m:
        raise RuntimeError(f'no game id in {game_url}')
    game_id  = m.group(1)
    html     = _get(game_url, referer=f'{SITE_BASE}/sports/{SPORT}/')
    schedule = _page_data_value(html, 'schedule') or {}
    raw      = (schedule.get(game_id) or {}).get('links')
    if not raw:
        return []
    links = []
    for l in json.loads(raw):
        url = (l.get('url') or '').strip()
        if url.startswith('http'):
            links.append({
                'name':    (l.get('name') or '').strip() or 'Link',
                'url':     url,
                'quality': (l.get('quality') or '').strip(),
            })
    return links


# ---------------------------------------------------------------------------
# Embed resolution
# ---------------------------------------------------------------------------

def _find_m3u8(html: str):
    m = re.search(r'\bSRC\s*=\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', html)
    if not m:
        m = re.search(r'["\'](https?://[^"\'\s]+?\.m3u8[^"\'\s]*)["\']', html)
    if not m:
        return None
    return m.group(1).replace('\\u0026', '&').replace('\\/', '/').replace('&amp;', '&')


def _find_iframe(html: str, page_url: str):
    for src in re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I):
        src = src.strip().replace('&amp;', '&')
        if not src or any(s in src for s in _IFRAME_SKIP):
            continue
        return urllib.parse.urljoin(page_url, src)
    return None


def _resolve_m3u8(link_url: str):
    """Follow the iframe chain from a viprow link. Returns (m3u8_url, player_page_url)."""
    url, referer = link_url, SITE_BASE + '/'
    for _ in range(MAX_IFRAME_DEPTH):
        print(f'[viprow] Fetching {url}', file=sys.stderr, flush=True)
        html = _get(url, referer=referer)
        m3u8 = _find_m3u8(html)
        if m3u8:
            return m3u8, url
        nxt = _find_iframe(html, url)
        if not nxt:
            break
        url, referer = nxt, url
    host = urllib.parse.urlparse(url).netloc
    raise RuntimeError(f'unsupported stream host ({host}) — try another link')


def _pick_variant(text: str, playlist_url: str) -> str:
    """Return the highest-bandwidth variant of a master playlist, or None."""
    best_url, best_bw = None, -1
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith('#EXT-X-STREAM-INF:'):
            continue
        m  = re.search(r'BANDWIDTH=(\d+)', line)
        bw = int(m.group(1)) if m else 0
        for nxt in lines[i + 1:]:
            nxt = nxt.strip()
            if nxt and not nxt.startswith('#'):
                if bw > best_bw:
                    best_bw, best_url = bw, urllib.parse.urljoin(playlist_url, nxt)
                break
    return best_url


def _run_pipeline(link_url: str):
    """Returns (requests_Session, media_playlist_url). No globals touched."""
    m3u8, player_url = _resolve_m3u8(link_url)
    print(f'[viprow] m3u8: {m3u8[:100]}', file=sys.stderr, flush=True)

    sess = _requests.Session()
    sess.headers.update({
        'User-Agent': UA, 'Referer': player_url,
        'Origin': _origin(player_url), 'Accept': '*/*',
    })
    resp = sess.get(m3u8, timeout=10)
    if resp.status_code != 200 or '#EXTM3U' not in resp.text[:200]:
        raise RuntimeError(f'stream playlist HTTP {resp.status_code} — stream may be offline')

    media_url = _pick_variant(resp.text, m3u8) or m3u8
    print(f'[viprow] Media: {media_url[:100]}', file=sys.stderr, flush=True)
    return sess, media_url


# ---------------------------------------------------------------------------
# PNG-wrapped segment decoding
# ---------------------------------------------------------------------------

_PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
_TS_MAGIC  = b'TIKTIKPX'


def _png_pixels(data: bytes) -> bytes:
    """Decode a non-interlaced 8-bit PNG to raw (unfiltered) pixel bytes."""
    pos, idat, ihdr = 8, [], None
    while pos + 8 <= len(data):
        length, = struct.unpack('>I', data[pos:pos + 4])
        ctype   = data[pos + 4:pos + 8]
        body    = data[pos + 8:pos + 8 + length]
        if ctype == b'IHDR':
            ihdr = struct.unpack('>IIBBBBB', body)
        elif ctype == b'IDAT':
            idat.append(body)
        elif ctype == b'IEND':
            break
        pos += 12 + length
    if not ihdr:
        raise ValueError('PNG has no IHDR')
    width, height, depth, colour, _, _, interlace = ihdr
    if depth != 8 or interlace:
        raise ValueError(f'unsupported PNG depth={depth} interlace={interlace}')
    bpp    = {0: 1, 2: 3, 4: 2, 6: 4}.get(colour, 1)
    stride = width * bpp
    raw    = zlib.decompress(b''.join(idat))

    out  = bytearray(stride * height)
    prev = bytearray(stride)
    for y in range(height):
        off  = y * (stride + 1)
        ft   = raw[off]
        line = bytearray(raw[off + 1:off + 1 + stride])
        if ft == 1:      # Sub
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 0xFF
        elif ft == 2:    # Up
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prev))
        elif ft == 3:    # Average
            for x in range(bpp):
                line[x] = (line[x] + (prev[x] >> 1)) & 0xFF
            for x in range(bpp, stride):
                line[x] = (line[x] + ((line[x - bpp] + prev[x]) >> 1)) & 0xFF
        elif ft == 4:    # Paeth
            for x in range(bpp):
                line[x] = (line[x] + prev[x]) & 0xFF
            for x in range(bpp, stride):
                a, b, c = line[x - bpp], prev[x], prev[x - bpp]
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                if pa <= pb and pa <= pc:
                    p = a
                elif pb <= pc:
                    p = b
                else:
                    p = c
                line[x] = (line[x] + p) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return bytes(out)


def _unwrap_segment(data: bytes) -> bytes:
    """Return MPEG-TS bytes; PNG-disguised segments are decoded, others pass through."""
    if not data.startswith(_PNG_MAGIC):
        return data
    px = _png_pixels(data)
    if px.startswith(_TS_MAGIC):
        length, = struct.unpack('>I', px[8:12])
        payload = px[12:12 + length]
        return gzip.decompress(payload) if payload[:2] == b'\x1f\x8b' else payload
    # Unknown wrapper — fall back to the first run of aligned TS packets
    for off in range(min(len(px), 4096)):
        if all(px[off + 188 * k] == 0x47 for k in range(5) if off + 188 * k < len(px)):
            return px[off:]
    raise ValueError('no MPEG-TS inside PNG segment')


# ---------------------------------------------------------------------------
# Proxy session
# ---------------------------------------------------------------------------

class _ViprowSession(_base._ProxySession):
    """_ProxySession that unwraps disguised segments and re-resolves via viprow."""

    def prefetch_seg(self, url: str):
        with self._seg_cache_lock:
            if url in self._seg_cache:
                return
            self._seg_cache[url] = None
        try:
            data = _unwrap_segment(self.cdn_session.get(url, timeout=30).content)
            with self._seg_cache_lock:
                self._seg_cache[url] = data
        except Exception:
            with self._seg_cache_lock:
                self._seg_cache.pop(url, None)

    def get_seg(self, url: str) -> bytes:
        # Cache hits are already unwrapped; unwrapping is a no-op on TS data
        return _unwrap_segment(super().get_seg(url))

    def refresh(self):
        if not self.match_url:
            return
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            new_sess, media_url = _run_pipeline(self.match_url)
            self.cdn_session   = new_sess
            self.cdn_media_url = media_url
            self.cdn_base      = _base._base_of(media_url)
            self.invalidate_playlist()
            print('[viprow] Session refreshed', file=sys.stderr, flush=True)
        except Exception as exc:
            print(f'[viprow] Session refresh failed — keeping old session: {exc}', file=sys.stderr, flush=True)
        finally:
            self._refresh_lock.release()


# ---------------------------------------------------------------------------
# Public API  (same shape as extractor_runner_no_chrome)
# ---------------------------------------------------------------------------

_current_session: _ViprowSession = None


def shutdown_proxy():
    global _current_session
    if _current_session:
        _current_session.shutdown()
        _current_session = None


def create_session(link_url: str) -> _ViprowSession:
    new_sess, media_url = _run_pipeline(link_url)
    sess               = _ViprowSession()
    sess.match_url     = link_url
    sess.cdn_session   = new_sess
    sess.cdn_media_url = media_url
    sess.cdn_base      = _base._base_of(media_url)
    sess.start_proxy()
    sess.start_refresh_loop()
    return sess


def extract(link_url: str, temp_file: str):
    """Resolve a viprow link, start the proxy, write the proxy URL to temp_file."""
    global _current_session
    shutdown_proxy()
    _current_session = create_session(link_url)
    with open(temp_file, 'w') as f:
        f.write(_current_session.proxy_url)
    print(f'[viprow] Written {_current_session.proxy_url}', file=sys.stderr, flush=True)
