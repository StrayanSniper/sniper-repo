"""
extractor_runner_no_chrome.py — Chrome-free casthill.net stream extractor
for rugbybox.me.  Uses plain HTTP requests instead of headless Chrome.

Can be run standalone (CLI):
    python extractor_runner_no_chrome.py <match_page_url> <temp_file_path>

Or imported and called in-process (Kodi on Android / any platform):
    import extractor_runner_no_chrome as extractor
    extractor.extract(match_url, temp_file_path)   # call from a daemon thread

How casthill.net/rugbybox.me streams work (reverse-engineered)
-------------------------------------------------------------
1.  rugbybox.me match page embeds:
      <script>const zmid="rug25"; const pid=5; const edm="casthill.net"
              const gameCat="Rugby"; const gameText="..."</script>
      <script async src="https://sts.casthill.net/scripts/v2/embed.min.js">

2.  embed.min.js reads those variables and dynamically creates an <iframe>
    pointing to  https://casthill.net/sd0embed/{gameCat}?pid=…&v=zmid&csrf=…

3.  The casthill.net embed page contains (in plain JS, not obfuscated):
    • videoSource  = bota(decodeSr([char,codes,…]))   — double-base64 → CDN URL
    • playerId     — random device ID (passed as u_id to CDN)
    • sCode        = decodeSr([char,codes,…])          — auth code
    • expireTs, strUnqId, edgeHostId
    • csrftoken    — triple-base64 CSRF token
    • secTokenUrl  = bota("aHR0cHM6Ly9ib2Fua2kubmV0")  → "https://boanki.net"

4.  The JS calls  https://boanki.net?scode=…&stream=…&expires=…&u_id=…&host_id=…
    (with X-CSRF-Auth header) to register the IP/session with the CDN before
    the CDN will serve the stream.  The response returns a new device_id.

5.  After boanki.net: fetch  videoSource_URL + "?u_id=" + device_id
    → Level-1 HLS master playlist (bandwidth variants).

6.  Follow the highest-bandwidth variant → Level-2 / media playlist.
    Segments are relative URLs like  media-XXXX_NNN.ts?e=…&u_id=…&ssid=…
    AES key is at  https://boanki.net/STREAM_ID/UUID?u_id=…&ssid=…

7.  Start local HTTP proxy on port 19823.  Proxy rewrites segment and AES key
    URLs so Kodi can fetch them with the right Referer header.
    Write proxy URL to <temp_file_path>.

Requirements:  requests  (cloudscraper optional — used if 403/503 encountered)
"""

from __future__ import annotations

import re
import sys
import os
import time
import base64
import threading
import http.server
import urllib.parse

# Ensure the vendored lib/ folder (bundled with the addon) is on sys.path so
# that 'requests' and its dependencies are found without any external pip install.
_lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib')
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

try:
    import requests as _requests
except ImportError:
    print('[no_chrome] ERROR: requests not found (lib/ vendor missing?)', file=sys.stderr)
    sys.exit(1)

try:
    import cloudscraper as _cloudscraper
    _HAS_CLOUDSCRAPER = True
except ImportError:
    _HAS_CLOUDSCRAPER = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)

PROXY_PORT     = 19823
PROXY_TIMEOUT  = 7200
PREFETCH_COUNT = 6
PLAYLIST_TTL   = 2.0

# CDN session — headers updated after the casthill embed URL is known
_cdn_session = _requests.Session()
_cdn_session.headers.update({
    'User-Agent': UA,
    'Accept':     '*/*',
})


# ---------------------------------------------------------------------------
# Shared proxy state
# ---------------------------------------------------------------------------

_cdn_media_url = None
_cdn_base      = None

_seg_cache      = {}
_seg_cache_lock = threading.Lock()

_playlist_cache      = {'text': None, 'expires': 0.0}
_playlist_cache_lock = threading.Lock()

_proxy_server = None   # current running server instance; replaced on each extract()


# ---------------------------------------------------------------------------
# Proxy lifecycle
# ---------------------------------------------------------------------------

def shutdown_proxy():
    """Shut down the proxy server if one is running. Safe to call at any time."""
    global _proxy_server
    if _proxy_server is not None:
        try:
            _proxy_server.shutdown()
        except Exception:
            pass
        _proxy_server = None


# ---------------------------------------------------------------------------
# Segment prefetch cache
# ---------------------------------------------------------------------------

def _prefetch_seg(url):
    with _seg_cache_lock:
        if url in _seg_cache:
            return
        _seg_cache[url] = None
    try:
        data = _cdn_session.get(url, timeout=30).content
        with _seg_cache_lock:
            _seg_cache[url] = data
    except Exception as e:
        print(f'[proxy] prefetch error: {e}', file=sys.stderr, flush=True)
        with _seg_cache_lock:
            _seg_cache.pop(url, None)


def _get_seg(url):
    for _ in range(60):
        with _seg_cache_lock:
            if url in _seg_cache:
                val = _seg_cache[url]
                if val is not None:
                    del _seg_cache[url]
                    return val
            else:
                break
        time.sleep(0.5)
    return _cdn_session.get(url, timeout=30).content


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _abs_url(url: str, base: str) -> str:
    if url.startswith('http'):
        return url
    if url.startswith('//'):
        return 'https:' + url
    if url.startswith('/'):
        p = urllib.parse.urlparse(base)
        return f'{p.scheme}://{p.netloc}{url}'
    return base + url


def _base_of(url: str) -> str:
    return url.split('?')[0].rsplit('/', 1)[0] + '/'


# ---------------------------------------------------------------------------
# HLS helpers
# ---------------------------------------------------------------------------

def _best_variant_url(master_url: str) -> str:
    """Fetch a master HLS playlist; return the highest-bandwidth variant URL."""
    text = _cdn_session.get(master_url, timeout=20).text
    cdn_host = urllib.parse.urlparse(master_url).scheme + '://' + urllib.parse.urlparse(master_url).netloc
    base = _base_of(master_url)
    best_url, best_bw = None, -1
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('#EXT-X-STREAM-INF:'):
            bw = 0
            bw_m = re.search(r'BANDWIDTH=(\d+)', line)
            if bw_m:
                bw = int(bw_m.group(1))
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].startswith('#')):
                j += 1
            if j < len(lines):
                variant = lines[j].strip()
                if variant.startswith('/'):
                    variant = cdn_host + variant
                elif not variant.startswith('http'):
                    variant = base + variant
                if bw > best_bw:
                    best_bw, best_url = bw, variant
        i += 1
    return best_url or master_url


# ---------------------------------------------------------------------------
# HLS Proxy
# ---------------------------------------------------------------------------

def _fake_master(proxy_base: str) -> bytes:
    return (
        '#EXTM3U\n#EXT-X-VERSION:3\n'
        f'#EXT-X-STREAM-INF:BANDWIDTH=2000000\n{proxy_base}/playlist.m3u8\n'
    ).encode('utf-8')


def _get_cdn_playlist() -> str:
    now = time.time()
    with _playlist_cache_lock:
        if _playlist_cache['text'] and now < _playlist_cache['expires']:
            return _playlist_cache['text']
    text = _cdn_session.get(_cdn_media_url, timeout=20).text
    with _playlist_cache_lock:
        _playlist_cache['text']    = text
        _playlist_cache['expires'] = now + PLAYLIST_TTL
    return text


def _rewrite_playlist(text: str, proxy_base: str) -> str:
    out = []
    prefetch_started = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('#EXT-X-KEY:'):
            def _rk(m):
                raw_uri = m.group(1)
                abs_uri = _abs_url(raw_uri, _cdn_base)
                return f'URI="{proxy_base}/key?url={urllib.parse.quote(abs_uri, safe="")}"'
            line = re.sub(r'URI="([^"]*)"', _rk, line)
        elif stripped and not stripped.startswith('#'):
            abs_seg = _abs_url(stripped, _cdn_base)
            if prefetch_started < PREFETCH_COUNT:
                threading.Thread(target=_prefetch_seg, args=(abs_seg,), daemon=True).start()
                prefetch_started += 1
            line = f'{proxy_base}/seg?url={urllib.parse.quote(abs_seg, safe="")}'
        out.append(line)
    return '\r\n'.join(out)


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f'[proxy] {fmt % args}', file=sys.stderr, flush=True)

    def do_GET(self):
        parsed    = urllib.parse.urlparse(self.path)
        qs        = urllib.parse.parse_qs(parsed.query)
        proxy_base = f'http://127.0.0.1:{PROXY_PORT}'

        if parsed.path == '/master.m3u8':
            body = _fake_master(proxy_base)
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)

        elif parsed.path == '/playlist.m3u8':
            try:
                body = _rewrite_playlist(_get_cdn_playlist(), proxy_base).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except Exception as exc:
                print(f'[proxy] playlist error: {exc}', file=sys.stderr, flush=True)
                self.send_response(502); self.end_headers()

        elif parsed.path == '/seg':
            seg_url = qs.get('url', [None])[0]
            if not seg_url:
                self.send_response(400); self.end_headers(); return
            try:
                data = _get_seg(urllib.parse.unquote(seg_url))
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp2t')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data)
            except Exception as exc:
                print(f'[proxy] seg error: {exc}', file=sys.stderr, flush=True)
                self.send_response(502); self.end_headers()

        elif parsed.path == '/key':
            key_url = qs.get('url', [None])[0]
            if not key_url:
                self.send_response(400); self.end_headers(); return
            try:
                data = _cdn_session.get(urllib.parse.unquote(key_url), timeout=20).content
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data)
            except Exception as exc:
                print(f'[proxy] key error: {exc}', file=sys.stderr, flush=True)
                self.send_response(502); self.end_headers()

        else:
            self.send_response(404); self.end_headers()


# ---------------------------------------------------------------------------
# Casthill.net extraction helpers
# ---------------------------------------------------------------------------

def _b64d(s: str) -> str:
    """Decode base64 (adds padding if needed)."""
    return base64.b64decode(s + '==').decode('utf-8', errors='replace')


def _decode_sr(char_list: list) -> str:
    """Convert a JS char-code array to a string (mirrors casthill's decodeSr)."""
    return ''.join(chr(c) for c in char_list)


def _fetch(session, url: str, referer: str = '', **kwargs) -> tuple[str, int]:
    """
    GET url → (text, status_code).
    Automatically decompresses unexpected gzip responses (avoids the Brotli
    trap: if Accept-Encoding includes 'br' the server may return Brotli which
    requests cannot decompress, yielding binary garbage).
    Falls back to cloudscraper on 403/503.
    """
    if referer:
        session.headers['Referer'] = referer
    try:
        r = session.get(url, timeout=20, allow_redirects=True, **kwargs)
        if r.status_code in (403, 503) and _HAS_CLOUDSCRAPER:
            print(f'[extractor] {r.status_code} — retrying with cloudscraper',
                  file=sys.stderr, flush=True)
            cs = _cloudscraper.create_scraper()
            cs.headers.update({k: v for k, v in session.headers.items()
                                if k.lower() != 'accept-encoding'})
            r = cs.get(url, timeout=20, allow_redirects=True)
        raw = r.content
        # Manual gzip decompression if requests didn't handle it
        if len(raw) >= 2 and raw[0] == 0x1f and raw[1] == 0x8b:
            import gzip as _gzip
            try:
                raw = _gzip.decompress(raw)
            except Exception:
                pass
        return raw.decode('utf-8', errors='replace'), r.status_code
    except Exception as exc:
        import traceback
        print(f'[extractor] fetch error {url}: {exc}', file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return '', 0


def _build_scrape_session(referer: str) -> '_requests.Session':
    """Session for scraping HTML pages — no Accept-Encoding override."""
    sess = _requests.Session()
    sess.headers.update({
        'User-Agent':                UA,
        'Accept':                    'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language':           'en-GB,en;q=0.5',
        'Referer':                   referer,
        'Upgrade-Insecure-Requests': '1',
        'Connection':                'keep-alive',
    })
    return sess


def _extract_embed_vars(html: str) -> dict:
    """
    Parse the non-obfuscated script block in a casthill.net sd0embed page.
    Returns a dict with all the values needed to call boanki.net and the CDN.
    Raises ValueError if required fields are missing.
    """
    def _re(pattern, default=None):
        m = re.search(pattern, html)
        if m:
            return m.group(1)
        if default is None:
            raise ValueError(f'Pattern not found: {pattern}')
        return default

    def _desr(pattern):
        m = re.search(pattern, html)
        if not m:
            raise ValueError(f'decodeSr pattern not found: {pattern}')
        return _decode_sr([int(x) for x in m.group(1).split(',')])

    # videoSource = bota(decodeSr([...])) — double atob gives the CDN URL
    vs_raw  = _desr(r'videoSource=bota\(decodeSr\(\[([0-9,]+)\]\)\)')
    url_source = _b64d(_b64d(vs_raw))

    scode      = _desr(r'(?<![A-Za-z])sCode=decodeSr\(\[([0-9,]+)\]\)')
    player_id  = _re(r'playerId="([^"]+)"')
    edge_host  = _re(r'edgeHostId="([^"]+)"')
    expire_ts  = _re(r'expireTs=parseInt\("([^"]+)"')
    str_unq    = _re(r'strUnqId="([^"]+)"')
    csrf_raw   = _re(r'csrftoken="([^"]+)"')
    csrf_hdr   = _b64d(_b64d(csrf_raw))         # double atob
    sec_url    = _b64d(_re(r'secTokenUrl=bota\("([^"]+)"\)'))  # boanki.net URL

    return {
        'url_source': url_source,
        'scode':      scode,
        'player_id':  player_id,
        'edge_host':  edge_host,
        'expire_ts':  expire_ts,
        'str_unq':    str_unq,
        'csrf_hdr':   csrf_hdr,
        'sec_url':    sec_url,
    }


def _call_boanki(embed_vars: dict, iframe_url: str, edm: str) -> str:
    """
    Call boanki.net to register this IP/session with the CDN.
    Returns the device_id to use as u_id in CDN requests.
    Raises on failure.
    """
    v = embed_vars
    qs = urllib.parse.urlencode({
        'scode':   v['scode'],
        'stream':  v['str_unq'],
        'expires': v['expire_ts'],
        'u_id':    v['player_id'],
        'host_id': v['edge_host'],
    })
    auth_url = f"{v['sec_url']}?{qs}"
    print(f'[extractor] Calling boanki.net...', file=sys.stderr, flush=True)
    ra = _requests.get(auth_url, timeout=15, headers={
        'User-Agent':   UA,
        'X-CSRF-Auth':  v['csrf_hdr'],
        'Accept':       'application/json',
        'Referer':      iframe_url,
        'Origin':       f'https://{edm}',
    })
    data = ra.json()
    if not data.get('success'):
        raise RuntimeError(f'boanki.net returned failure: {data}')
    device_id = data['device_id']
    print(f'[extractor] boanki.net OK  device_id={device_id}', file=sys.stderr, flush=True)
    return device_id


# ---------------------------------------------------------------------------
# Main extraction entry point
# ---------------------------------------------------------------------------

def extract(match_url: str, temp_file: str):
    """
    Extract the stream URL for match_url, start the local HLS proxy, and
    write the proxy playlist URL to temp_file.

    Raises RuntimeError on any extraction failure (so the caller — a Kodi
    plugin thread — can catch it without calling sys.exit and killing Kodi).
    """
    global _cdn_media_url, _cdn_base, _proxy_server

    # Shut down any proxy left running from a previous playback session
    shutdown_proxy()

    # ---- Step 1: fetch rugbybox.me match page ----------------------------
    print(f'[extractor] Fetching match page: {match_url}', file=sys.stderr, flush=True)
    page_sess = _build_scrape_session('https://rugbybox.me/')
    html_page, status = _fetch(page_sess, match_url)
    if not html_page or status != 200:
        raise RuntimeError(f'match page returned HTTP {status}')

    # ---- Step 2: extract casthill embed config from match page -----------
    zmid_m = re.search(r'zmid\s*=\s*"([^"]+)"', html_page)
    pid_m  = re.search(r'\bpid\s*=\s*(\d+)', html_page)
    edm_m  = re.search(r'edm\s*=\s*"([^"]+)"', html_page)
    if not (zmid_m and pid_m and edm_m):
        # Match page has no embed — look for stream sub-page links
        # e.g. /match-live/nrl/stream-1, /match-live/nrl/stream-2
        sub_links = re.findall(r'href="(/[^"]+/stream-\d+)"', html_page)
        sub_links += re.findall(r"href='(/[^']+/stream-\d+)'", html_page)
        found = False
        for sub in sub_links:
            sub_url = 'https://rugbybox.me' + sub
            print(f'[extractor] Trying stream sub-page: {sub_url}', file=sys.stderr, flush=True)
            sub_html, sub_status = _fetch(page_sess, sub_url)
            if sub_html and sub_status == 200:
                zm = re.search(r'zmid\s*=\s*"([^"]+)"', sub_html)
                pm = re.search(r'\bpid\s*=\s*(\d+)', sub_html)
                em = re.search(r'edm\s*=\s*"([^"]+)"', sub_html)
                if zm and pm and em:
                    html_page = sub_html
                    zmid_m, pid_m, edm_m = zm, pm, em
                    found = True
                    break
        if not found:
            print(f'[extractor] page snippet: {html_page[:800]}', file=sys.stderr, flush=True)
            raise RuntimeError('casthill embed config not found in match page or sub-pages')

    zmid     = zmid_m.group(1)
    pid      = pid_m.group(1)
    edm      = edm_m.group(1)
    game_cat = (re.search(r'gameCat\s*=\s*"([^"]+)"', html_page) or
                type('', (), {'group': lambda s, n: 'sp'})()).group(1)
    game_txt = (re.search(r'gameText\s*=\s*"([^"]+)"', html_page) or
                type('', (), {'group': lambda s, n: ''})()).group(1)

    csrf_raw = re.search(r'"csrf":\s*"([^"]+)"', html_page)
    csrf_ip  = re.search(r'"csrf_ip":\s*"([^"]+)"', html_page)
    if not (csrf_raw and csrf_ip):
        raise RuntimeError('siteConfig CSRF not found in match page')

    # ---- Step 3: fetch casthill.net embed page ---------------------------
    embed_qs = urllib.parse.urlencode({
        'pid': pid, 'gacat': game_txt, 'gatxt': game_cat,
        'v': zmid, 'csrf': csrf_raw.group(1), 'csrf_ip': csrf_ip.group(1),
    })
    iframe_url = f'https://{edm}/sd0embed/{game_cat}?{embed_qs}'
    print(f'[extractor] Fetching embed: {iframe_url[:80]}...', file=sys.stderr, flush=True)

    embed_sess = _build_scrape_session(match_url)
    html_embed, status = _fetch(embed_sess, iframe_url, referer=match_url)
    if not html_embed or status != 200:
        raise RuntimeError(f'embed page returned HTTP {status}')

    # ---- Step 4: extract embed vars (videoSource, sCode, etc.) ----------
    ev = _extract_embed_vars(html_embed)  # raises ValueError on failure

    print(f'[extractor] urlSource: {ev["url_source"][:80]}', file=sys.stderr, flush=True)
    print(f'[extractor] edgeHost:  {ev["edge_host"]}', file=sys.stderr, flush=True)

    # ---- Step 5: call boanki.net to register session with CDN ------------
    device_id = _call_boanki(ev, iframe_url, edm)  # raises RuntimeError on failure

    # ---- Step 6: update CDN session headers with correct Referer ---------
    _cdn_session.headers.update({
        'Referer': iframe_url,
        'Origin':  f'https://{edm}',
    })

    # ---- Step 7: fetch Level-1 master manifest → best Level-2 variant ---
    master_url = ev['url_source'] + f'?u_id={device_id}'
    print(f'[extractor] Fetching master: {master_url[:80]}...', file=sys.stderr, flush=True)

    media_url = _best_variant_url(master_url)  # raises on network failure

    print(f'[extractor] Media playlist: {media_url[:80]}', file=sys.stderr, flush=True)

    _cdn_media_url = media_url
    _cdn_base      = _base_of(media_url)

    # ---- Step 8: start local proxy ---------------------------------------
    _proxy_server = http.server.ThreadingHTTPServer(('127.0.0.1', PROXY_PORT), _ProxyHandler)
    threading.Thread(target=_proxy_server.serve_forever, daemon=True).start()
    print(f'[extractor] Proxy on port {PROXY_PORT}', file=sys.stderr, flush=True)

    proxy_url = f'http://127.0.0.1:{PROXY_PORT}/playlist.m3u8'
    with open(temp_file, 'w') as f:
        f.write(proxy_url)
    print(f'[extractor] Written {proxy_url}', file=sys.stderr, flush=True)
    # Proxy runs as daemon threads — no keep-alive loop needed when in-process.
    # When called from main() the keep-alive is handled there.


def main():
    if len(sys.argv) < 3:
        print('Usage: extractor_runner_no_chrome.py <match_url> <temp_file>',
              file=sys.stderr)
        sys.exit(1)
    try:
        extract(sys.argv[1], sys.argv[2])
    except Exception as exc:
        print(f'[extractor] FATAL: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
    # Keep the process alive so the proxy daemon threads keep serving
    deadline = time.time() + PROXY_TIMEOUT
    try:
        while time.time() < deadline:
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    shutdown_proxy()


if __name__ == '__main__':
    main()
