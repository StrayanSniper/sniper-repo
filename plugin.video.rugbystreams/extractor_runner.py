"""
extractor_runner.py — runs under system Python (NOT Kodi's Python).

Usage: python extractor_runner.py <match_page_url> <temp_file_path>

Strategy
--------
1. Load the match page in undetected-chromedriver (headless).
2. Wait for the casthill.net iframe player to initialise.
3. Read jwplayer().getPlaylistItem().file  →  the play.m3u8 master URL.
4. Fetch play.m3u8, pick the highest-bandwidth variant (the media playlist).
5. Start a local HTTP proxy on port 19823 that serves the media playlist
   with all relative segment URLs rewritten to absolute CDN URLs, and
   AES key URIs rewritten through the proxy so Kodi can fetch them.
6. Write the proxy playlist URL to <temp_file_path>.
7. Keep the proxy alive for up to 2 hours while Kodi plays.
"""

import re
import sys
import time
import subprocess
import threading
import http.server
import urllib.parse
import urllib.request

import requests as _requests

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By


UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)

FETCH_HEADERS = {
    'User-Agent':      UA,
    'Accept':          '*/*',
    'Accept-Language': 'en-GB,en;q=0.5',
    'Referer':         'https://casthill.net/',
    'Origin':          'https://casthill.net',
}

INJECT_JS = r"""
(function () {
    try {
        window.location.assign  = function (u) {};
        window.location.replace = function (u) {};
    } catch (e) {}
})();
"""

PROXY_PORT      = 19823
PROXY_TIMEOUT   = 7200   # keep proxy alive for 2 hours
PREFETCH_COUNT  = 6      # segments to prefetch ahead
PLAYLIST_TTL    = 2.0    # seconds to cache the CDN playlist response

# Persistent session with connection pooling — reuses TLS connections to CDN
_session = _requests.Session()
_session.headers.update(FETCH_HEADERS)


# ---------------------------------------------------------------------------
# Shared state (set by extractor, read by proxy)
# ---------------------------------------------------------------------------

_cdn_media_url = None   # absolute URL of the media playlist (master.m3u8)
_cdn_base      = None   # base for resolving relative segment/key URLs

# Segment prefetch cache: abs_url → bytes (None = fetch in progress)
_seg_cache      = {}
_seg_cache_lock = threading.Lock()

# Playlist cache so Kodi's rapid polls are served instantly
_playlist_cache      = {'text': None, 'expires': 0.0}
_playlist_cache_lock = threading.Lock()


def _prefetch_seg(url):
    """Download a segment in the background and store in cache."""
    with _seg_cache_lock:
        if url in _seg_cache:
            return  # already cached or in progress
        _seg_cache[url] = None  # mark in-progress
    try:
        data = _session.get(url, timeout=30).content
        with _seg_cache_lock:
            _seg_cache[url] = data
    except Exception as e:
        print(f'[proxy] prefetch error: {e}', file=sys.stderr, flush=True)
        with _seg_cache_lock:
            _seg_cache.pop(url, None)


def _get_seg(url):
    """Return segment bytes from cache (waiting if still prefetching), or fetch now."""
    for _ in range(60):  # wait up to 30 s for in-progress prefetch
        with _seg_cache_lock:
            if url in _seg_cache:
                val = _seg_cache[url]
                if val is not None:
                    del _seg_cache[url]
                    return val
                # None → still downloading, wait
            else:
                break  # not prefetching — fetch directly
        time.sleep(0.5)
    return _session.get(url, timeout=30).content


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_bytes(url: str) -> bytes:
    return _session.get(url, timeout=20).content


def _fetch_text(url: str) -> str:
    return _session.get(url, timeout=20).text


def _abs_url(url: str, base: str) -> str:
    if url.startswith('http'):
        return url
    if url.startswith('//'):
        return 'https:' + url
    if url.startswith('/'):
        p = urllib.parse.urlparse(base)
        return f'{p.scheme}://{p.netloc}{url}'
    return base + url


def _best_variant_url(master_url: str) -> str:
    """Fetch a master HLS playlist and return the highest-bandwidth variant URL."""
    text = _fetch_text(master_url)
    base = master_url.rsplit('/', 1)[0] + '/'

    best_url = None
    best_bw  = -1
    lines    = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('#EXT-X-STREAM-INF:'):
            bw = 0
            for part in line.split(','):
                if 'BANDWIDTH=' in part:
                    try:
                        bw = int(part.split('BANDWIDTH=')[1])
                    except Exception:
                        pass
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].startswith('#')):
                j += 1
            if j < len(lines) and bw > best_bw:
                best_bw  = bw
                best_url = _abs_url(lines[j].strip(), base)
        i += 1

    return best_url or master_url


# ---------------------------------------------------------------------------
# HLS Proxy
# ---------------------------------------------------------------------------

def _fake_master(proxy_base: str) -> bytes:
    """Return a minimal HLS master manifest pointing to our media playlist."""
    content = (
        '#EXTM3U\n'
        '#EXT-X-VERSION:3\n'
        f'#EXT-X-STREAM-INF:BANDWIDTH=2000000\n'
        f'{proxy_base}/playlist.m3u8\n'
    )
    return content.encode('utf-8')


def _get_cdn_playlist() -> str:
    """Return the CDN media playlist, serving from cache if still fresh."""
    now = time.time()
    with _playlist_cache_lock:
        if _playlist_cache['text'] and now < _playlist_cache['expires']:
            return _playlist_cache['text']
    text = _fetch_text(_cdn_media_url)
    with _playlist_cache_lock:
        _playlist_cache['text'] = text
        _playlist_cache['expires'] = now + PLAYLIST_TTL
    return text


def _rewrite_playlist(text: str, proxy_base: str) -> str:
    """
    Rewrite a media playlist so that:
    - Segment URLs route through /seg (proxy adds Referer, prefetches ahead).
    - #EXT-X-KEY URI values route through the proxy /key endpoint.
    Kicks off background prefetch for up to PREFETCH_COUNT segments.
    """
    out = []
    prefetch_started = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('#EXT-X-KEY:'):
            def _rewrite_key_uri(m):
                raw_uri = m.group(1)
                abs_uri = _abs_url(raw_uri, _cdn_base)
                encoded = urllib.parse.quote(abs_uri, safe='')
                return f'URI="{proxy_base}/key?url={encoded}"'
            line = re.sub(r'URI="([^"]*)"', _rewrite_key_uri, line)
        elif stripped and not stripped.startswith('#'):
            abs_seg = _abs_url(stripped, _cdn_base)
            if prefetch_started < PREFETCH_COUNT:
                threading.Thread(target=_prefetch_seg, args=(abs_seg,), daemon=True).start()
                prefetch_started += 1
            encoded = urllib.parse.quote(abs_seg, safe='')
            line = f'{proxy_base}/seg?url={encoded}'
        out.append(line)
    return '\r\n'.join(out)


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f'[proxy] {fmt % args}', file=sys.stderr, flush=True)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs     = urllib.parse.parse_qs(parsed.query)

        proxy_base = f'http://127.0.0.1:{PROXY_PORT}'

        if parsed.path == '/master.m3u8':
            body = _fake_master(proxy_base)
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == '/playlist.m3u8':
            try:
                text      = _get_cdn_playlist()
                rewritten = _rewrite_playlist(text, proxy_base)
                body     = rewritten.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                print(f'[proxy] playlist error: {exc}', file=sys.stderr, flush=True)
                self.send_response(502)
                self.end_headers()

        elif parsed.path == '/seg':
            seg_url = qs.get('url', [None])[0]
            if not seg_url:
                self.send_response(400)
                self.end_headers()
                return
            try:
                data = _get_seg(urllib.parse.unquote(seg_url))
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp2t')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                print(f'[proxy] seg error: {exc}', file=sys.stderr, flush=True)
                self.send_response(502)
                self.end_headers()

        elif parsed.path == '/key':
            key_url = qs.get('url', [None])[0]
            if not key_url:
                self.send_response(400)
                self.end_headers()
                return
            try:
                data = _fetch_bytes(urllib.parse.unquote(key_url))
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                print(f'[proxy] key error: {exc}', file=sys.stderr, flush=True)
                self.send_response(502)
                self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def _kodi_running() -> bool:
    """Return True if kodi.exe is currently running."""
    try:
        result = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq kodi.exe', '/NH'],
            capture_output=True, text=True
        )
        return 'kodi.exe' in result.stdout.lower()
    except Exception:
        return True  # assume running if check fails


def _kill_driver(driver):
    """Quit the WebDriver and kill any lingering Chrome child processes."""
    browser_pid = getattr(driver, 'browser_pid', None)
    try:
        driver.quit()
    except Exception:
        pass
    # Kill by specific browser PID + its process tree
    if browser_pid:
        try:
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(browser_pid)],
                capture_output=True
            )
        except Exception:
            pass
    # Sweep up any remaining chrome.exe processes (catches renderer/GPU children)
    try:
        subprocess.run(
            ['taskkill', '/F', '/IM', 'chrome.exe', '/T'],
            capture_output=True
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Selenium extraction
# ---------------------------------------------------------------------------

def _find_casthill_iframe(driver):
    for iframe in driver.find_elements(By.TAG_NAME, 'iframe'):
        if 'casthill.net' in (iframe.get_attribute('src') or ''):
            return iframe
    return None


def _read_player_url(driver, iframe):
    driver.switch_to.frame(iframe)
    try:
        return driver.execute_script('''
            try {
                if (!window.isPlayerLoaded) return null;
                var item = window.jwplayer().getPlaylistItem();
                if (!item) return null;
                var f = item.file;
                if (!f && item.sources && item.sources.length)
                    f = item.sources[0].file;
                return f || null;
            } catch(e) { return null; }
        ''')
    finally:
        driver.switch_to.default_content()


def extract(match_url: str, temp_file: str, timeout: int = 90):
    global _cdn_media_url, _cdn_base

    opts = uc.ChromeOptions()
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--mute-audio')
    opts.add_argument('--autoplay-policy=no-user-gesture-required')
    opts.add_argument('--window-size=1280,720')
    opts.add_argument(f'--user-agent={UA}')
    # Speed up page load — skip images, fonts, plugins
    opts.add_argument('--blink-settings=imagesEnabled=false')
    opts.add_argument('--disable-extensions')
    opts.add_argument('--disable-default-apps')
    opts.add_argument('--disable-sync')
    opts.add_argument('--no-first-run')

    driver = uc.Chrome(options=opts, headless=True)
    try:
        driver.execute_cdp_cmd(
            'Page.addScriptToEvaluateOnNewDocument',
            {'source': INJECT_JS}
        )
        print(f'[extractor] Loading: {match_url}', file=sys.stderr, flush=True)
        driver.get(match_url)

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.5)
            iframe = _find_casthill_iframe(driver)
            if iframe:
                play_url = _read_player_url(driver, iframe)
                if play_url:
                    print(f'[extractor] play.m3u8: {play_url[:80]}',
                          file=sys.stderr, flush=True)

                    # Resolve play.m3u8 → highest-bandwidth media playlist
                    media_url = _best_variant_url(play_url)
                    print(f'[extractor] media:     {media_url[:80]}',
                          file=sys.stderr, flush=True)

                    _cdn_media_url = media_url
                    # Base for resolving relative URLs (strip query string + filename)
                    _cdn_base = media_url.split('?')[0].rsplit('/', 1)[0] + '/'

                    # Start local proxy (threaded so segments don't block playlist refreshes)
                    server = http.server.ThreadingHTTPServer(
                        ('127.0.0.1', PROXY_PORT), _ProxyHandler
                    )
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    print(f'[extractor] Proxy on port {PROXY_PORT}',
                          file=sys.stderr, flush=True)

                    proxy_url = f'http://127.0.0.1:{PROXY_PORT}/playlist.m3u8'
                    with open(temp_file, 'w') as f:
                        f.write(proxy_url)
                    print(f'[extractor] Written {proxy_url}',
                          file=sys.stderr, flush=True)

                    # Stay alive until Kodi exits or timeout reached
                    deadline_proxy = time.time() + PROXY_TIMEOUT
                    while time.time() < deadline_proxy:
                        time.sleep(5)
                        if not _kodi_running():
                            print('[extractor] Kodi exited — shutting down proxy',
                                  file=sys.stderr, flush=True)
                            break
                    server.shutdown()
                    return

            remaining = round(deadline - time.time(), 1)
            print(f'[extractor] Waiting for player... ({remaining}s left)',
                  file=sys.stderr, flush=True)

        print('[extractor] ERROR: timed out', file=sys.stderr, flush=True)

    except Exception as exc:
        print(f'[extractor] Error: {exc}', file=sys.stderr, flush=True)
    finally:
        _kill_driver(driver)


def main():
    if len(sys.argv) < 3:
        print('Usage: extractor_runner.py <match_url> <temp_file>',
              file=sys.stderr)
        sys.exit(1)
    extract(sys.argv[1], sys.argv[2])


if __name__ == '__main__':
    main()
