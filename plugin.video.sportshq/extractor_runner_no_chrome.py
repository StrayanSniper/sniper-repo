"""
extractor_runner_no_chrome.py — casthill.net stream extractor for rugbybox.me.

The CDN (peulleieo.net) binds auth parameters (u_id, ssid) in segment/key
URLs only for the HTTP session that called boanki.net.  A local proxy is
therefore required: it fetches the CDN playlist with the authenticated
Python session, rewrites all URLs to go through itself, and serves them to
Kodi's ffmpegdirect player.

Port is chosen randomly from free OS ports to avoid any Android conflicts.
"""

import re
import sys
import os
import base64
import socket
import threading
import http.server
import urllib.parse
import time

_lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib')
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

try:
    import requests as _requests
except ImportError:
    print('[extractor] ERROR: requests not found', file=sys.stderr)
    sys.exit(1)

UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)

PLAYLIST_TTL   = 2.0
PREFETCH_COUNT = 4   # segments to pre-fetch ahead of current position

# ---------------------------------------------------------------------------
# Proxy state  (module-level so daemon threads can access it)
# ---------------------------------------------------------------------------

_cdn_session   = _requests.Session()
_cdn_media_url = None
_cdn_base      = None
_proxy_server  = None

_playlist_cache      = {'text': None, 'expires': 0.0}
_playlist_cache_lock = threading.Lock()

_seg_cache      = {}
_seg_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Proxy lifecycle
# ---------------------------------------------------------------------------

def shutdown_proxy():
    global _proxy_server
    if _proxy_server is not None:
        try:
            _proxy_server.shutdown()
            _proxy_server.server_close()
        except Exception:
            pass
        _proxy_server = None


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# CDN helpers
# ---------------------------------------------------------------------------

def _base_of(url: str) -> str:
    return url.split('?')[0].rsplit('/', 1)[0] + '/'


def _abs_url(url: str, base: str) -> str:
    if url.startswith('http'):
        return url
    if url.startswith('//'):
        return 'https:' + url
    if url.startswith('/'):
        p = urllib.parse.urlparse(base)
        return f'{p.scheme}://{p.netloc}{url}'
    return base + url


def _prefetch_seg(url: str):
    with _seg_cache_lock:
        if url in _seg_cache:
            return
        _seg_cache[url] = None  # mark in-progress
    try:
        data = _cdn_session.get(url, timeout=30).content
        with _seg_cache_lock:
            _seg_cache[url] = data
    except Exception:
        with _seg_cache_lock:
            _seg_cache.pop(url, None)


def _get_seg(url: str) -> bytes:
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


def _trigger_prefetch(current_url: str):
    try:
        playlist = _get_cdn_playlist()
        seg_urls = [
            _abs_url(l.strip(), _cdn_base)
            for l in playlist.splitlines()
            if l.strip() and not l.startswith('#')
        ]
        try:
            idx = next(i for i, u in enumerate(seg_urls) if u == current_url)
            upcoming = seg_urls[idx + 1: idx + 1 + PREFETCH_COUNT]
        except StopIteration:
            upcoming = seg_urls[:PREFETCH_COUNT]
        for u in upcoming:
            with _seg_cache_lock:
                if u not in _seg_cache:
                    threading.Thread(target=_prefetch_seg, args=(u,), daemon=True).start()
    except Exception:
        pass


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
            line = f'{proxy_base}/seg?url={urllib.parse.quote(abs_seg, safe="")}'
        out.append(line)
    return '\r\n'.join(out)


def _best_variant_url(master_url: str) -> str:
    text     = _cdn_session.get(master_url, timeout=20).text
    cdn_host = urllib.parse.urlparse(master_url).scheme + '://' + urllib.parse.urlparse(master_url).netloc
    base     = _base_of(master_url)
    best_url, best_bw = None, -1
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('#EXT-X-STREAM-INF:'):
            bw   = int(m.group(1)) if (m := re.search(r'BANDWIDTH=(\d+)', line)) else 0
            j    = i + 1
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
# Proxy HTTP handler
# ---------------------------------------------------------------------------

class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f'[proxy] {fmt % args}', file=sys.stderr, flush=True)

    def do_GET(self):
        parsed     = urllib.parse.urlparse(self.path)
        qs         = urllib.parse.parse_qs(parsed.query)
        proxy_base = f'http://127.0.0.1:{self.server.server_address[1]}'

        if parsed.path == '/shutdown':
            self.send_response(200)
            self.end_headers()
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        elif parsed.path == '/playlist.m3u8':
            try:
                body = _rewrite_playlist(
                    _get_cdn_playlist(), proxy_base
                ).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                print(f'[proxy] playlist error: {exc}', file=sys.stderr, flush=True)
                self.send_response(502)
                self.end_headers()

        elif parsed.path == '/key':
            key_url = qs.get('url', [None])[0]
            if not key_url:
                self.send_response(400); self.end_headers(); return
            try:
                data = _cdn_session.get(urllib.parse.unquote(key_url), timeout=20).content
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                print(f'[proxy] key error: {exc}', file=sys.stderr, flush=True)
                self.send_response(502); self.end_headers()

        elif parsed.path == '/seg':
            seg_url = qs.get('url', [None])[0]
            if not seg_url:
                self.send_response(400); self.end_headers(); return
            try:
                actual_url = urllib.parse.unquote(seg_url)
                data = _get_seg(actual_url)
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp2t')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                threading.Thread(target=_trigger_prefetch, args=(actual_url,), daemon=True).start()
            except Exception as exc:
                print(f'[proxy] seg error: {exc}', file=sys.stderr, flush=True)
                self.send_response(502); self.end_headers()

        else:
            self.send_response(404); self.end_headers()


class _ProxyServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True

    def server_bind(self):
        if hasattr(socket, 'SO_REUSEPORT'):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        super().server_bind()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _build_scrape_session(referer: str) -> '_requests.Session':
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


def _fetch(session, url: str, referer: str = '', **kwargs):
    if referer:
        session.headers['Referer'] = referer
    try:
        r = session.get(url, timeout=20, allow_redirects=True, **kwargs)
        raw = r.content
        if len(raw) >= 2 and raw[0] == 0x1f and raw[1] == 0x8b:
            import gzip
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        return raw.decode('utf-8', errors='replace'), r.status_code
    except Exception as exc:
        print(f'[extractor] fetch error {url}: {exc}', file=sys.stderr, flush=True)
        return '', 0


# ---------------------------------------------------------------------------
# Casthill / boanki extraction
# ---------------------------------------------------------------------------

def _b64d(s: str) -> str:
    return base64.b64decode(s + '==').decode('utf-8', errors='replace')


def _decode_sr(char_list: list) -> str:
    return ''.join(chr(c) for c in char_list)


def _extract_embed_vars(html: str) -> dict:
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

    vs_raw     = _desr(r'videoSource=bota\(decodeSr\(\[([0-9,]+)\]\)\)')
    url_source = _b64d(_b64d(vs_raw))
    scode      = _desr(r'(?<![A-Za-z])sCode=decodeSr\(\[([0-9,]+)\]\)')
    player_id  = _re(r'playerId="([^"]+)"')
    edge_host  = _re(r'edgeHostId="([^"]+)"')
    expire_ts  = _re(r'expireTs=parseInt\("([^"]+)"')
    str_unq    = _re(r'strUnqId="([^"]+)"')
    csrf_raw   = _re(r'csrftoken="([^"]+)"')
    csrf_hdr   = _b64d(_b64d(csrf_raw))
    sec_url    = _b64d(_re(r'secTokenUrl=bota\("([^"]+)"\)'))

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


def _call_boanki(ev: dict, iframe_url: str, edm: str) -> str:
    qs = urllib.parse.urlencode({
        'scode':   ev['scode'],
        'stream':  ev['str_unq'],
        'expires': ev['expire_ts'],
        'u_id':    ev['player_id'],
        'host_id': ev['edge_host'],
    })
    auth_url = f"{ev['sec_url']}?{qs}"
    print('[extractor] Calling boanki.net...', file=sys.stderr, flush=True)
    ra = _requests.get(auth_url, timeout=15, headers={
        'User-Agent':  UA,
        'X-CSRF-Auth': ev['csrf_hdr'],
        'Accept':      'application/json',
        'Referer':     iframe_url,
        'Origin':      f'https://{edm}',
    })
    data = ra.json()
    if not data.get('success'):
        raise RuntimeError(f'boanki.net returned failure: {data}')
    device_id = data['device_id']
    print(f'[extractor] boanki.net OK  device_id={device_id}', file=sys.stderr, flush=True)
    return device_id


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def extract(match_url: str, temp_file: str):
    global _cdn_media_url, _cdn_base, _proxy_server, _cdn_session

    shutdown_proxy()

    print(f'[extractor] Fetching match page: {match_url}', file=sys.stderr, flush=True)
    page_sess = _build_scrape_session('https://rugbybox.me/')
    html_page, status = _fetch(page_sess, match_url)
    if not html_page or status != 200:
        raise RuntimeError(f'match page returned HTTP {status}')

    zmid_m = re.search(r'zmid\s*=\s*"([^"]+)"', html_page)
    pid_m  = re.search(r'\bpid\s*=\s*(\d+)', html_page)
    edm_m  = re.search(r'edm\s*=\s*"([^"]+)"', html_page)

    if not (zmid_m and pid_m and edm_m):
        sub_links  = re.findall(r'href="(/[^"]+/stream-\d+)"', html_page)
        sub_links += re.findall(r"href='(/[^']+/stream-\d+)'", html_page)
        found = False
        for sub in sub_links:
            sub_url  = 'https://rugbybox.me' + sub
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
            raise RuntimeError('casthill embed config not found in match page or sub-pages')

    zmid     = zmid_m.group(1)
    pid      = pid_m.group(1)
    edm      = edm_m.group(1)
    cat_m    = re.search(r'gameCat\s*=\s*"([^"]+)"', html_page)
    txt_m    = re.search(r'gameText\s*=\s*"([^"]+)"', html_page)
    game_cat = cat_m.group(1) if cat_m else 'sp'
    game_txt = txt_m.group(1) if txt_m else ''
    csrf_m   = re.search(r'"csrf"\s*:\s*"([^"]+)"', html_page)
    csrfip_m = re.search(r'"csrf_ip"\s*:\s*"([^"]+)"', html_page)
    if not csrf_m or not csrfip_m:
        raise RuntimeError('CSRF tokens not found in siteConfig')

    embed_qs = urllib.parse.urlencode({
        'pid':     pid,
        'gacat':   game_txt,
        'gatxt':   game_cat,
        'v':       zmid,
        'csrf':    csrf_m.group(1),
        'csrf_ip': csrfip_m.group(1),
    })
    iframe_url = f'https://{edm}/sd0embed/{game_cat}?{embed_qs}'
    print(f'[extractor] Fetching embed: {iframe_url[:80]}...', file=sys.stderr, flush=True)

    embed_sess = _build_scrape_session(match_url)
    html_embed, status = _fetch(embed_sess, iframe_url, referer=match_url)
    if not html_embed or status != 200:
        raise RuntimeError(f'embed page returned HTTP {status}')

    ev = _extract_embed_vars(html_embed)
    print(f'[extractor] urlSource: {ev["url_source"][:80]}', file=sys.stderr, flush=True)

    device_id = _call_boanki(ev, iframe_url, edm)

    # Build CDN session with correct auth headers for all CDN requests
    _cdn_session = _requests.Session()
    _cdn_session.headers.update({
        'User-Agent': UA,
        'Referer':    iframe_url,
        'Origin':     f'https://{edm}',
        'Accept':     '*/*',
    })

    master_url     = ev['url_source'] + f'?u_id={device_id}'
    print(f'[extractor] Fetching master: {master_url[:80]}...', file=sys.stderr, flush=True)
    media_url      = _best_variant_url(master_url)
    print(f'[extractor] Media playlist: {media_url[:80]}', file=sys.stderr, flush=True)

    _cdn_media_url = media_url
    _cdn_base      = _base_of(media_url)

    # Reset caches for the new session
    with _playlist_cache_lock:
        _playlist_cache['text']    = None
        _playlist_cache['expires'] = 0.0
    with _seg_cache_lock:
        _seg_cache.clear()

    # Use a random free port — no port conflicts on any platform
    port = _find_free_port()
    _proxy_server = _ProxyServer(('127.0.0.1', port), _ProxyHandler)
    threading.Thread(target=_proxy_server.serve_forever, daemon=True).start()
    print(f'[extractor] Proxy on port {port}', file=sys.stderr, flush=True)

    proxy_url = f'http://127.0.0.1:{port}/playlist.m3u8'
    with open(temp_file, 'w') as f:
        f.write(proxy_url)
    print(f'[extractor] Written {proxy_url}', file=sys.stderr, flush=True)
