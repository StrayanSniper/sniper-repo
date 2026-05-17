"""
extractor_runner_no_chrome.py — casthill.net stream extractor for rugbybox.me.

Supports multiple independent simultaneous stream sessions via _ProxySession.
Each session has its own CDN session, HLS proxy, and segment cache — allowing
parallel pre-extraction of all channels when the Live Streams list loads.
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

PLAYLIST_TTL      = 2.0
PREFETCH_COUNT    = 6
SESSION_REFRESH_S = 270


# ---------------------------------------------------------------------------
# Per-stream session  (independent CDN session + proxy + segment cache)
# ---------------------------------------------------------------------------

class _ProxySession:
    """Encapsulates one fully independent extracted stream."""

    def __init__(self):
        self.cdn_session     = _requests.Session()
        self.cdn_media_url   = None
        self.cdn_base        = None
        self.proxy_server    = None
        self.proxy_url       = None
        self.match_url       = None
        self._playlist_cache = {'text': None, 'expires': 0.0}
        self._playlist_lock  = threading.Lock()
        self._seg_cache      = {}
        self._seg_cache_lock = threading.Lock()

    # -- CDN playlist -------------------------------------------------------

    def get_playlist(self) -> str:
        now = time.time()
        with self._playlist_lock:
            if self._playlist_cache['text'] and now < self._playlist_cache['expires']:
                return self._playlist_cache['text']
        text = self.cdn_session.get(self.cdn_media_url, timeout=8).text
        with self._playlist_lock:
            self._playlist_cache['text']    = text
            self._playlist_cache['expires'] = now + PLAYLIST_TTL
        return text

    def invalidate_playlist(self):
        with self._playlist_lock:
            self._playlist_cache['text']    = None
            self._playlist_cache['expires'] = 0.0

    # -- Segment cache ------------------------------------------------------

    def prefetch_seg(self, url: str):
        with self._seg_cache_lock:
            if url in self._seg_cache:
                return
            self._seg_cache[url] = None
        try:
            data = self.cdn_session.get(url, timeout=30).content
            with self._seg_cache_lock:
                self._seg_cache[url] = data
        except Exception:
            with self._seg_cache_lock:
                self._seg_cache.pop(url, None)

    def get_seg(self, url: str) -> bytes:
        for _ in range(60):
            with self._seg_cache_lock:
                if url in self._seg_cache:
                    val = self._seg_cache[url]
                    if val is not None:
                        del self._seg_cache[url]
                        return val
                else:
                    break
            time.sleep(0.5)
        return self.cdn_session.get(url, timeout=30).content

    def trigger_prefetch(self, current_url: str):
        try:
            seg_urls = [
                _abs_url(l.strip(), self.cdn_base)
                for l in self.get_playlist().splitlines()
                if l.strip() and not l.startswith('#')
            ]
            try:
                idx = next(i for i, u in enumerate(seg_urls) if u == current_url)
                upcoming = seg_urls[idx + 1: idx + 1 + PREFETCH_COUNT]
            except StopIteration:
                upcoming = seg_urls[:PREFETCH_COUNT]
            for u in upcoming:
                with self._seg_cache_lock:
                    if u not in self._seg_cache:
                        threading.Thread(target=self.prefetch_seg, args=(u,), daemon=True).start()
        except Exception:
            pass

    def prefetch_initial(self):
        time.sleep(0.2)
        try:
            seg_urls = [
                _abs_url(l.strip(), self.cdn_base)
                for l in self.get_playlist().splitlines()
                if l.strip() and not l.startswith('#')
            ]
            for u in seg_urls[:PREFETCH_COUNT]:
                with self._seg_cache_lock:
                    if u not in self._seg_cache:
                        threading.Thread(target=self.prefetch_seg, args=(u,), daemon=True).start()
        except Exception:
            pass

    # -- Proxy lifecycle ----------------------------------------------------

    def start_proxy(self) -> str:
        port = _find_free_port()
        srv  = _ProxyServer(('127.0.0.1', port), _ProxyHandler)
        srv.session = self
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.proxy_server = srv
        self.proxy_url    = f'http://127.0.0.1:{port}/playlist.m3u8'
        threading.Thread(target=self.prefetch_initial, daemon=True).start()
        return self.proxy_url

    def shutdown(self):
        if self.proxy_server:
            try:
                self.proxy_server.shutdown()
                self.proxy_server.server_close()
            except Exception:
                pass
            self.proxy_server = None

    def refresh(self):
        """Re-run pipeline and update this session's CDN credentials."""
        if not self.match_url:
            return
        try:
            new_sess, media_url = _run_pipeline(self.match_url)
            self.cdn_session   = new_sess
            self.cdn_media_url = media_url
            self.cdn_base      = _base_of(media_url)
            self.invalidate_playlist()
            with self._seg_cache_lock:
                self._seg_cache.clear()
            print('[extractor] Session refreshed OK', file=sys.stderr, flush=True)
        except Exception as exc:
            print(f'[extractor] Session refresh failed: {exc}', file=sys.stderr, flush=True)

    def start_refresh_loop(self):
        def _loop():
            while True:
                time.sleep(SESSION_REFRESH_S)
                if self.proxy_server:
                    self.refresh()
        threading.Thread(target=_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Single global session  (for backward-compat / rugbystreams plugin)
# ---------------------------------------------------------------------------

_current_session: _ProxySession = None


def shutdown_proxy():
    global _current_session
    if _current_session:
        _current_session.shutdown()
        _current_session = None


# ---------------------------------------------------------------------------
# Proxy server + handler
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class _ProxyServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    session: _ProxySession = None

    def server_bind(self):
        if hasattr(socket, 'SO_REUSEPORT'):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        super().server_bind()


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f'[proxy] {fmt % args}', file=sys.stderr, flush=True)

    def do_GET(self):
        parsed     = urllib.parse.urlparse(self.path)
        qs         = urllib.parse.parse_qs(parsed.query)
        proxy_base = f'http://127.0.0.1:{self.server.server_address[1]}'
        sess       = self.server.session

        if parsed.path == '/shutdown':
            self.send_response(200); self.end_headers()
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        elif parsed.path == '/playlist.m3u8':
            try:
                body = _rewrite_playlist(
                    sess.get_playlist(), proxy_base, sess.cdn_base
                ).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except Exception as exc:
                print(f'[proxy] playlist error: {exc}', file=sys.stderr, flush=True)
                self.send_response(502); self.end_headers()

        elif parsed.path == '/key':
            key_url = qs.get('url', [None])[0]
            if not key_url:
                self.send_response(400); self.end_headers(); return
            try:
                data = sess.cdn_session.get(urllib.parse.unquote(key_url), timeout=10).content
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data)
            except Exception as exc:
                print(f'[proxy] key error: {exc}', file=sys.stderr, flush=True)
                self.send_response(502); self.end_headers()

        elif parsed.path == '/seg':
            seg_url = qs.get('url', [None])[0]
            if not seg_url:
                self.send_response(400); self.end_headers(); return
            try:
                actual = urllib.parse.unquote(seg_url)
                data   = sess.get_seg(actual)
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp2t')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data)
                threading.Thread(target=sess.trigger_prefetch, args=(actual,), daemon=True).start()
            except Exception as exc:
                print(f'[proxy] seg error: {exc}', file=sys.stderr, flush=True)
                try:
                    self.send_response(502); self.end_headers()
                except Exception:
                    pass

        else:
            self.send_response(404); self.end_headers()


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _abs_url(url: str, base: str) -> str:
    if url.startswith('http'):  return url
    if url.startswith('//'): return 'https:' + url
    if url.startswith('/'):
        p = urllib.parse.urlparse(base)
        return f'{p.scheme}://{p.netloc}{url}'
    return base + url


def _base_of(url: str) -> str:
    return url.split('?')[0].rsplit('/', 1)[0] + '/'


def _rewrite_playlist(text: str, proxy_base: str, cdn_base: str) -> str:
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('#EXT-X-KEY:'):
            def _rk(m):
                abs_uri = _abs_url(m.group(1), cdn_base)
                return f'URI="{proxy_base}/key?url={urllib.parse.quote(abs_uri, safe="")}"'
            line = re.sub(r'URI="([^"]*)"', _rk, line)
        elif stripped and not stripped.startswith('#'):
            abs_seg = _abs_url(stripped, cdn_base)
            line    = f'{proxy_base}/seg?url={urllib.parse.quote(abs_seg, safe="")}'
        out.append(line)
    return '\r\n'.join(out)


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
        r = session.get(url, timeout=10, allow_redirects=True, **kwargs)
        raw = r.content
        if len(raw) >= 2 and raw[0] == 0x1f and raw[1] == 0x8b:
            import gzip
            try: raw = gzip.decompress(raw)
            except Exception: pass
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
        if m: return m.group(1)
        if default is None: raise ValueError(f'Pattern not found: {pattern}')
        return default

    def _desr(pattern):
        m = re.search(pattern, html)
        if not m: raise ValueError(f'decodeSr not found: {pattern}')
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
        'url_source': url_source, 'scode': scode, 'player_id': player_id,
        'edge_host': edge_host, 'expire_ts': expire_ts, 'str_unq': str_unq,
        'csrf_hdr': csrf_hdr, 'sec_url': sec_url,
    }


def _call_boanki(ev: dict, iframe_url: str, edm: str) -> str:
    qs = urllib.parse.urlencode({
        'scode': ev['scode'], 'stream': ev['str_unq'],
        'expires': ev['expire_ts'], 'u_id': ev['player_id'],
        'host_id': ev['edge_host'],
    })
    print('[extractor] Calling boanki.net...', file=sys.stderr, flush=True)
    ra = _requests.get(f"{ev['sec_url']}?{qs}", timeout=8, headers={
        'User-Agent': UA, 'X-CSRF-Auth': ev['csrf_hdr'],
        'Accept': 'application/json', 'Referer': iframe_url,
        'Origin': f'https://{edm}',
    })
    data = ra.json()
    if not data.get('success'):
        raise RuntimeError(f'boanki.net failure: {data}')
    device_id = data['device_id']
    print(f'[extractor] boanki OK device_id={device_id}', file=sys.stderr, flush=True)
    return device_id


def _run_pipeline(match_url: str):
    """
    Run casthill → boanki pipeline.
    Returns (new_requests_Session, media_url).  No globals touched.
    """
    print(f'[extractor] Pipeline: {match_url}', file=sys.stderr, flush=True)
    page_sess = _build_scrape_session('https://rugbybox.me/')
    html_page, status = _fetch(page_sess, match_url)
    if not html_page or status != 200:
        raise RuntimeError(f'match page HTTP {status}')

    zmid_m = re.search(r'zmid\s*=\s*"([^"]+)"', html_page)
    pid_m  = re.search(r'\bpid\s*=\s*(\d+)', html_page)
    edm_m  = re.search(r'edm\s*=\s*"([^"]+)"', html_page)

    if not (zmid_m and pid_m and edm_m):
        sub_links  = re.findall(r'href="(/[^"]+/stream-\d+)"', html_page)
        sub_links += re.findall(r"href='(/[^']+/stream-\d+)'", html_page)
        found = False
        for sub in sub_links:
            sub_html, sub_status = _fetch(page_sess, 'https://rugbybox.me' + sub)
            if sub_html and sub_status == 200:
                zm = re.search(r'zmid\s*=\s*"([^"]+)"', sub_html)
                pm = re.search(r'\bpid\s*=\s*(\d+)', sub_html)
                em = re.search(r'edm\s*=\s*"([^"]+)"', sub_html)
                if zm and pm and em:
                    html_page = sub_html; zmid_m, pid_m, edm_m = zm, pm, em
                    found = True; break
        if not found:
            raise RuntimeError('casthill embed config not found')

    zmid     = zmid_m.group(1); pid = pid_m.group(1); edm = edm_m.group(1)
    cat_m    = re.search(r'gameCat\s*=\s*"([^"]+)"', html_page)
    txt_m    = re.search(r'gameText\s*=\s*"([^"]+)"', html_page)
    game_cat = cat_m.group(1) if cat_m else 'sp'
    game_txt = txt_m.group(1) if txt_m else ''
    csrf_m   = re.search(r'"csrf"\s*:\s*"([^"]+)"', html_page)
    csrfip_m = re.search(r'"csrf_ip"\s*:\s*"([^"]+)"', html_page)
    if not csrf_m or not csrfip_m:
        raise RuntimeError('CSRF not found')

    embed_qs   = urllib.parse.urlencode({
        'pid': pid, 'gacat': game_txt, 'gatxt': game_cat,
        'v': zmid, 'csrf': csrf_m.group(1), 'csrf_ip': csrfip_m.group(1),
    })
    iframe_url = f'https://{edm}/sd0embed/{game_cat}?{embed_qs}'
    print(f'[extractor] Embed: {iframe_url[:80]}...', file=sys.stderr, flush=True)

    embed_sess = _build_scrape_session(match_url)
    html_embed, status = _fetch(embed_sess, iframe_url, referer=match_url)
    if not html_embed or status != 200:
        raise RuntimeError(f'embed HTTP {status}')

    ev        = _extract_embed_vars(html_embed)
    device_id = _call_boanki(ev, iframe_url, edm)

    new_sess = _requests.Session()
    new_sess.headers.update({
        'User-Agent': UA, 'Referer': iframe_url,
        'Origin': f'https://{edm}', 'Accept': '*/*',
    })

    master_url = ev['url_source'] + f'?u_id={device_id}'
    print(f'[extractor] Master: {master_url[:80]}', file=sys.stderr, flush=True)

    # Fetch + parse master to find best variant
    master_text = new_sess.get(master_url, timeout=10).text
    cdn_host    = urllib.parse.urlparse(master_url).scheme + '://' + urllib.parse.urlparse(master_url).netloc
    base        = _base_of(master_url)
    best_url, best_bw = None, -1
    lines = master_text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith('#EXT-X-STREAM-INF:'):
            bw = int(m.group(1)) if (m := re.search(r'BANDWIDTH=(\d+)', lines[i])) else 0
            j  = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].startswith('#')):
                j += 1
            if j < len(lines):
                v = lines[j].strip()
                if v.startswith('/'): v = cdn_host + v
                elif not v.startswith('http'): v = base + v
                if bw > best_bw: best_bw, best_url = bw, v
        i += 1
    media_url = best_url or master_url
    print(f'[extractor] Media: {media_url[:80]}', file=sys.stderr, flush=True)
    return new_sess, media_url


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_session(match_url: str) -> _ProxySession:
    """
    Full pipeline → returns a ready _ProxySession with proxy running.
    Independent of all module globals — safe to call concurrently.
    """
    new_sess, media_url = _run_pipeline(match_url)
    sess               = _ProxySession()
    sess.match_url     = match_url
    sess.cdn_session   = new_sess
    sess.cdn_media_url = media_url
    sess.cdn_base      = _base_of(media_url)
    sess.start_proxy()
    sess.start_refresh_loop()
    return sess


def extract(match_url: str, temp_file: str):
    """
    Single-stream extraction (used by rugbystreams plugin).
    Replaces any previous global session.
    """
    global _current_session

    # Try to send shutdown to any existing proxy
    try:
        import urllib.request as _ureq
        _ureq.urlopen('http://127.0.0.1:19823/shutdown', timeout=1)
        time.sleep(0.3)
    except Exception:
        pass

    if _current_session:
        _current_session.shutdown()

    _current_session           = create_session(match_url)
    _current_session.match_url = match_url

    with open(temp_file, 'w') as f:
        f.write(_current_session.proxy_url)
    print(f'[extractor] Written {_current_session.proxy_url}', file=sys.stderr, flush=True)
