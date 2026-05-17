"""
scrapers/live_tv.py — Live Streams IPTV section for Sports HQ.

Scrapes all sections of rugbybox.me, lists every available stream
as a separate channel entry. Each click extracts and plays via the
casthill/boanki proxy pipeline.
"""

import os
import re
import sys
import json
import socket as _socket
import threading
import time
import urllib.parse
import urllib.request

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs
import xbmcaddon

_ADDON_DIR         = xbmcaddon.Addon().getAddonInfo('path')
_KODI_TEMP         = xbmcvfs.translatePath('special://temp/')
_TEMP_FILE         = os.path.join(_KODI_TEMP, 'sportshq_livetv.url')
_PROXY_CACHE_FILE  = os.path.join(_KODI_TEMP, 'sportshq_proxy_cache.json')
_STREAM_LIST_FILE  = os.path.join(_KODI_TEMP, 'sportshq_stream_list.json')
_GRACE_PERIOD_S    = 60   # keep proxy alive this long after stopping
_CACHE_MAX_AGE_S   = 240  # 4 minutes — matches CDN session lifetime
_LIST_CACHE_AGE_S  = 300  # 5 minutes — stream list cache

if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)

import extractor_runner_no_chrome as _extractor

# In-process session registry: match_url -> _ProxySession
# Populated during pre-extraction, alive for the grace period
_live_sessions = {}
_live_sessions_lock = threading.Lock()

_BASE    = 'https://rugbybox.me'
_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-GB,en;q=0.5',
}


# ---------------------------------------------------------------------------
# Site scraping
# ---------------------------------------------------------------------------

def _get(url):
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode('utf-8', errors='replace')


def _find_stream_links(html, base_url):
    """Return list of (title, absolute_url) stream links found in html."""
    results = []
    seen    = set()
    # Match any href that ends in -stream or /stream-N
    for m in re.finditer(
        r'<a[^>]+href=["\']([^"\']*(?:-stream|/stream-\d+))["\'][^>]*>(.*?)</a>',
        html, re.IGNORECASE | re.DOTALL
    ):
        href  = m.group(1).strip()
        title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        if not href or not title:
            continue
        if href.startswith('/'):
            href = _BASE + href
        elif not href.startswith('http'):
            continue
        if href not in seen:
            seen.add(href)
            results.append((title, href))
    return results


def _scrape_section(url):
    """Scrape a single section page and return (title, stream_url) pairs."""
    try:
        html = _get(url)
        return _find_stream_links(html, url)
    except Exception as exc:
        xbmc.log(f'[sportshq/livetv] scrape error {url}: {exc}', xbmc.LOGWARNING)
        return []


def _get_all_sections():
    """
    Fetch rugbybox.me homepage, find all section nav links,
    then scrape each section for stream links.
    Returns list of dicts: {title, url, section, sport_thumb, fanart}
    """
    try:
        home_html = _get(_BASE)
    except Exception as exc:
        xbmc.log(f'[sportshq/livetv] homepage error: {exc}', xbmc.LOGWARNING)
        # Fallback to known sections
        home_html = ''

    # Find nav section links from homepage
    section_urls = []
    seen_sections = set()

    # Look for nav links that point to sport sections
    for m in re.finditer(
        r'<a[^>]+href=["\'](/[a-z0-9-]+/?)["\'][^>]*>([^<]+)</a>',
        home_html, re.IGNORECASE
    ):
        href  = m.group(1).rstrip('/')
        label = m.group(2).strip()
        if (href and href != '/' and href not in seen_sections
                and not any(x in href for x in ['login', 'register', 'contact', 'about', 'privacy'])):
            seen_sections.add(href)
            section_urls.append((_BASE + href, label))

    # Always include known working sections
    for known in [('/rugby-union-streams', 'Rugby Union'), ('/nrl', 'NRL')]:
        url = _BASE + known[0]
        if url not in {u for u, _ in section_urls}:
            section_urls.insert(0, (url, known[1]))

    # Scrape each section in parallel
    streams    = []
    streams_lock = threading.Lock()

    def _scrape_and_collect(section_url, section_label):
        links = _scrape_section(section_url)
        with streams_lock:
            for title, stream_url in links:
                streams.append({
                    'title':   title or section_label,
                    'url':     stream_url,
                    'section': section_label,
                })

    threads = []
    for sec_url, sec_label in section_urls[:12]:  # cap at 12 sections
        t = threading.Thread(
            target=_scrape_and_collect,
            args=(sec_url, sec_label),
            daemon=True
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=10)

    # Deduplicate by URL
    seen_urls = set()
    unique    = []
    for s in streams:
        if s['url'] not in seen_urls:
            seen_urls.add(s['url'])
            unique.append(s)

    return unique


# ---------------------------------------------------------------------------
# Kodi list
# ---------------------------------------------------------------------------

def _preextract_all(streams, dialog):
    """
    Pre-extract all streams in parallel while the loading dialog is open.
    Each stream gets its own independent _ProxySession.
    """
    total   = len(streams)
    done    = [0]
    lock    = threading.Lock()
    errors  = []

    def _do_one(stream):
        url = stream['url']
        try:
            sess = _extractor.create_session(url)
            with _live_sessions_lock:
                _live_sessions[url] = sess
            _cache_write(url, sess.proxy_url)
        except Exception as exc:
            errors.append(exc)
        finally:
            with lock:
                done[0] += 1
                pct = 20 + int(done[0] / total * 75)
                try:
                    dialog.update(pct, f'Preparing streams... {done[0]}/{total}')
                except Exception:
                    pass

    threads = [threading.Thread(target=_do_one, args=(s,), daemon=True) for s in streams]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)  # max 30s total wait


def _keepalive_loop():
    """Keep this Python invocation alive so proxy daemon threads don't die."""
    monitor = xbmc.Monitor()
    timeout = _CACHE_MAX_AGE_S  # stay alive until CDN sessions expire
    while timeout > 0 and not monitor.abortRequested():
        xbmc.sleep(1000)
        timeout -= 1


def _show_stream_list(handle, base_url, addon_icon, fanart, streams):
    xbmcplugin.setContent(handle, 'videos')
    li_r = xbmcgui.ListItem(label='[B]⟳ Refresh[/B]')
    li_r.setArt({'thumb': addon_icon, 'fanart': fanart})
    xbmcplugin.addDirectoryItem(handle, base_url + '?action=refresh&sport=livetv', li_r, True)
    for stream in streams:
        label = f'[B]{stream["section"]}[/B]  {stream["title"]}'
        li = xbmcgui.ListItem(label=label)
        li.setArt({'thumb': addon_icon, 'icon': addon_icon, 'fanart': fanart})
        li.setInfo('video', {'title': stream['title'], 'plot': f'{stream["section"]} — {stream["title"]}', 'mediatype': 'video'})
        li.setProperty('IsPlayable', 'true')
        url = '{}?action=play&sport=livetv&url={}'.format(base_url, urllib.parse.quote(stream['url'], safe=''))
        xbmcplugin.addDirectoryItem(handle, url, li, False)
    xbmcplugin.addSortMethod(handle, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(handle)
    xbmc.executebuiltin('Container.SetViewMode(500)')


def list_live_streams(handle, base_url, addon_icon, fanart):
    proxy_cache = _cache_read()
    now         = time.time()

    # --- Fast path: all proxies still alive from a previous load ---
    cached_streams = _stream_list_load()
    if cached_streams:
        all_alive = all(
            _proxy_alive(proxy_cache.get(s['url'], {}).get('proxy_url', ''))
            and now - proxy_cache.get(s['url'], {}).get('saved_at', 0) < _CACHE_MAX_AGE_S
            for s in cached_streams
        )
        if all_alive and cached_streams:
            xbmc.log('[sportshq/livetv] all proxies alive — instant list', xbmc.LOGINFO)
            _show_stream_list(handle, base_url, addon_icon, fanart, cached_streams)
            # Keep alive so proxy threads stay running
            _keepalive_loop()
            return

    # --- Slow path: scrape + extract ---
    dialog = xbmcgui.DialogProgress()
    dialog.create('Sports HQ', 'Finding live streams...')
    dialog.update(10)

    streams = _get_all_sections()

    if not streams:
        dialog.close()
        xbmcgui.Dialog().notification('Sports HQ', 'No live streams found right now', xbmcgui.NOTIFICATION_INFO, 4000)
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    _stream_list_save(streams)
    dialog.update(20, f'Found {len(streams)} stream(s) — loading...')
    _preextract_all(streams, dialog)
    dialog.close()

    _show_stream_list(handle, base_url, addon_icon, fanart, streams)
    # Keep alive so pre-extracted proxy threads survive for channel clicks
    _keepalive_loop()


# ---------------------------------------------------------------------------
# Proxy cache helpers
# ---------------------------------------------------------------------------

def _cache_read():
    try:
        with open(_PROXY_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _cache_write(match_url, proxy_url):
    try:
        cache = _cache_read()
        cache[match_url] = {'proxy_url': proxy_url, 'saved_at': time.time()}
        with open(_PROXY_CACHE_FILE, 'w') as f:
            json.dump(cache, f)
    except Exception:
        pass


def _stream_list_save(streams):
    try:
        with open(_STREAM_LIST_FILE, 'w') as f:
            json.dump({'streams': streams, 'saved_at': time.time()}, f)
    except Exception:
        pass


def _stream_list_load():
    try:
        with open(_STREAM_LIST_FILE) as f:
            data = json.load(f)
        if time.time() - data.get('saved_at', 0) < _LIST_CACHE_AGE_S:
            return data.get('streams', [])
    except Exception:
        pass
    return None


def _proxy_alive(proxy_url):
    """Return True if the proxy at proxy_url is still accepting connections."""
    try:
        port = int(proxy_url.split(':')[2].split('/')[0])
        s = _socket.socket()
        s.settimeout(0.3)
        s.connect(('127.0.0.1', port))
        s.close()
        return True
    except Exception:
        return False


def _cached_proxy(match_url):
    """Return a live proxy URL from cache, or None."""
    cache = _cache_read()
    entry = cache.get(match_url)
    if not entry:
        return None
    if time.time() - entry.get('saved_at', 0) > _CACHE_MAX_AGE_S:
        return None
    proxy_url = entry.get('proxy_url', '')
    return proxy_url if _proxy_alive(proxy_url) else None


# ---------------------------------------------------------------------------
# Playback  (same pipeline as rugby scraper)
# ---------------------------------------------------------------------------

def _play_url(handle, stream_url):
    li = xbmcgui.ListItem(path=stream_url)
    li.setMimeType('application/vnd.apple.mpegurl')
    li.setContentLookup(False)
    li.setProperty('inputstream',                               'inputstream.ffmpegdirect')
    li.setProperty('inputstream.ffmpegdirect.manifest_type',    'hls')
    li.setProperty('inputstream.ffmpegdirect.is_realtime_stream', 'true')
    li.setProperty('inputstream.ffmpegdirect.open_timeout',     '15')
    xbmcplugin.setResolvedUrl(handle, True, li)

    player  = xbmc.Player()
    monitor = xbmc.Monitor()
    timeout = 0
    while not player.isPlaying() and timeout < 20 and not monitor.abortRequested():
        xbmc.sleep(500)
        timeout += 1
    while player.isPlaying() and not monitor.abortRequested():
        xbmc.sleep(1000)
    # Grace period — all pre-extracted proxies stay alive for channel surfing
    grace = _GRACE_PERIOD_S
    while grace > 0 and not monitor.abortRequested():
        xbmc.sleep(1000)
        grace -= 1
    # Clean up sessions
    with _live_sessions_lock:
        _live_sessions.clear()


def _poll_for_url(thread, dialog, timeout_ms=60000):
    poll_ms    = 200  # 200ms for faster detection
    elapsed_ms = 0
    while elapsed_ms < timeout_ms:
        if dialog.iscanceled():
            return None, True
        try:
            with open(_TEMP_FILE, 'r') as f:
                url = f.read().strip()
            if url:
                return url, False
        except OSError:
            pass
        if not thread.is_alive():
            try:
                with open(_TEMP_FILE, 'r') as f:
                    url = f.read().strip()
                if url:
                    return url, False
            except OSError:
                pass
            return None, False
        xbmc.sleep(poll_ms)
        elapsed_ms += poll_ms
        progress = min(90, 5 + int(elapsed_ms / timeout_ms * 85))
        dialog.update(progress, f'Loading stream... ({elapsed_ms // 1000}s)')
    return None, False


def play_stream(handle, match_url):
    xbmc.log(f'[sportshq/livetv] play: {match_url}', xbmc.LOGINFO)

    # 1. Same-invocation in-memory session (pre-extracted during list load)
    with _live_sessions_lock:
        sess = _live_sessions.get(match_url)
    if sess and sess.proxy_url and _proxy_alive(sess.proxy_url):
        xbmc.log(f'[sportshq/livetv] instant: {sess.proxy_url}', xbmc.LOGINFO)
        _play_url(handle, sess.proxy_url)
        return

    # 2. File cache from a previous invocation's grace period
    cached = _cached_proxy(match_url)
    if cached:
        xbmc.log(f'[sportshq/livetv] cache hit: {cached}', xbmc.LOGINFO)
        _play_url(handle, cached)
        return

    # 3. Full extraction
    try:
        os.remove(_TEMP_FILE)
    except OSError:
        pass

    dialog = xbmcgui.DialogProgress()
    dialog.create('Sports HQ', 'Loading stream...')
    dialog.update(5)

    error_holder = [None]

    def _run():
        try:
            _extractor.extract(match_url, _TEMP_FILE)
        except Exception as exc:
            error_holder[0] = str(exc)
            xbmc.log(f'[sportshq/livetv] extractor error: {exc}', xbmc.LOGERROR)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    stream_url, cancelled = _poll_for_url(t, dialog)
    dialog.close()

    if cancelled:
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    if not stream_url:
        msg = error_holder[0] or 'Stream not found'
        if 'embed config not found' in msg:
            msg = 'Stream not live yet'
        xbmcgui.Dialog().notification(
            'Sports HQ', msg, xbmcgui.NOTIFICATION_ERROR, 7000
        )
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    xbmc.log(f'[sportshq/livetv] play_url={stream_url}', xbmc.LOGINFO)
    _cache_write(match_url, stream_url)
    _play_url(handle, stream_url)
