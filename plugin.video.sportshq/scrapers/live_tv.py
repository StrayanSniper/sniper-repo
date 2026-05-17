"""
scrapers/live_tv.py — Live Streams IPTV section for Sports HQ.

Scrapes all sections of rugbybox.me, lists every available stream
as a separate channel entry. Each click extracts and plays via the
casthill/boanki proxy pipeline.
"""

import os
import re
import sys
import threading
import urllib.parse
import urllib.request

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs
import xbmcaddon

_ADDON_DIR = xbmcaddon.Addon().getAddonInfo('path')
_KODI_TEMP = xbmcvfs.translatePath('special://temp/')
_TEMP_FILE = os.path.join(_KODI_TEMP, 'sportshq_livetv.url')

if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)

import extractor_runner_no_chrome as _extractor

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

def list_live_streams(handle, base_url, addon_icon, fanart):
    # Scrape all streams
    dialog = xbmcgui.DialogProgress()
    dialog.create('Sports HQ', 'Finding live streams...')
    dialog.update(20)

    streams = _get_all_sections()
    dialog.close()

    if not streams:
        xbmcgui.Dialog().notification(
            'Sports HQ', 'No live streams found right now',
            xbmcgui.NOTIFICATION_INFO, 4000
        )
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    xbmcplugin.setContent(handle, 'videos')

    # Refresh item
    li_r = xbmcgui.ListItem(label='[B]⟳ Refresh[/B]')
    li_r.setArt({'thumb': addon_icon, 'fanart': fanart})
    xbmcplugin.addDirectoryItem(
        handle,
        base_url + '?action=refresh&sport=livetv',
        li_r, True
    )

    for stream in streams:
        label = f'[B]{stream["section"]}[/B]  {stream["title"]}'
        li = xbmcgui.ListItem(label=label)
        li.setArt({'thumb': addon_icon, 'icon': addon_icon, 'fanart': fanart})
        li.setInfo('video', {
            'title':     stream['title'],
            'plot':      f'{stream["section"]} — {stream["title"]}',
            'mediatype': 'video',
        })
        li.setProperty('IsPlayable', 'true')
        url = '{}?action=play&sport=livetv&url={}'.format(
            base_url, urllib.parse.quote(stream['url'], safe='')
        )
        xbmcplugin.addDirectoryItem(handle, url, li, False)

    xbmcplugin.addSortMethod(handle, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(handle)
    xbmc.executebuiltin('Container.SetViewMode(500)')


# ---------------------------------------------------------------------------
# Playback  (same pipeline as rugby scraper)
# ---------------------------------------------------------------------------

def _poll_for_url(thread, dialog, timeout_ms=60000):
    poll_ms    = 500
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

    _extractor.shutdown_proxy()
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
    li = xbmcgui.ListItem(path=stream_url)
    li.setMimeType('application/vnd.apple.mpegurl')
    li.setContentLookup(False)
    li.setProperty('inputstream',                          'inputstream.ffmpegdirect')
    li.setProperty('inputstream.ffmpegdirect.manifest_type',    'hls')
    li.setProperty('inputstream.ffmpegdirect.is_realtime_stream', 'true')
    li.setProperty('inputstream.ffmpegdirect.stream_mode',       'timeshift')
    li.setProperty('inputstream.ffmpegdirect.open_timeout',      '30')
    xbmcplugin.setResolvedUrl(handle, True, li)

    # Keep script alive so proxy daemon threads stay running
    player  = xbmc.Player()
    monitor = xbmc.Monitor()
    timeout = 0
    while not player.isPlaying() and timeout < 20 and not monitor.abortRequested():
        xbmc.sleep(500)
        timeout += 1
    while player.isPlaying() and not monitor.abortRequested():
        xbmc.sleep(1000)
