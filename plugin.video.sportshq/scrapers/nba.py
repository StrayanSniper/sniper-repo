"""
scrapers/nba.py — NBA live streams via crackstreams.ms
Pipeline: game page → iframe → source m3u8 URL → ffmpegdirect
"""

import re
import os
import sys
import threading
import urllib.parse

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs
import xbmcaddon

_ADDON_DIR = xbmcaddon.Addon().getAddonInfo('path')
_KODI_TEMP = xbmcvfs.translatePath('special://temp/')

if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)

_BASE = 'https://crackstreams.ms'
_UA   = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)
_HEADERS = {
    'User-Agent':      _UA,
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-GB,en;q=0.5',
}


def _fetch(url, referer=''):
    try:
        import requests as _req
        h = {**_HEADERS}
        if referer:
            h['Referer'] = referer
        return _req.get(url, headers=h, timeout=15, verify=False).text
    except Exception as exc:
        xbmc.log(f'[sportshq/nba] fetch {url}: {exc}', xbmc.LOGWARNING)
        return ''


def _get_games():
    """Scrape crackstreams.ms for live NBA games."""
    for listing in ['/nbastreams', '/crackstreams/nba-streams/', '/nba']:
        html = _fetch(_BASE + listing)
        if not html:
            continue
        games = []
        seen  = set()
        # Find links to individual game/stream pages
        for m in re.finditer(
            r'href=["\']((?:https?://[^"\']*crackstreams[^"\']*|/[^"\']+))["\'][^>]*>\s*([^<]{5,80})',
            html, re.DOTALL
        ):
            href  = m.group(1).strip()
            title = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', m.group(2))).strip()
            if not href.startswith('http'):
                href = _BASE + href
            # Only keep game/stream links
            if not any(x in href.lower() for x in ['stream', 'nba', 'vs', 'spurs', 'lakers', 'celtics']):
                continue
            if href in seen or len(title) < 5:
                continue
            seen.add(href)
            games.append({'title': title, 'url': href})
        if games:
            return games
    return []


def _extract_stream(game_url):
    """
    Extract HLS URL from a crackstreams game page.
    Pattern: game page → iframe → source: 'xxx.m3u8'
    """
    html = _fetch(game_url, referer=_BASE)
    if not html:
        raise RuntimeError('Could not fetch game page')

    # Find iframe
    iframe_m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if not iframe_m:
        raise RuntimeError('No iframe found on game page')

    iframe_url = iframe_m.group(1)
    if not iframe_url.startswith('http'):
        iframe_url = 'https:' + iframe_url if iframe_url.startswith('//') else _BASE + iframe_url

    iframe_html = _fetch(iframe_url, referer=game_url)
    if not iframe_html:
        raise RuntimeError('Could not fetch iframe page')

    # Find m3u8 URL — various patterns used by crackstreams-type sites
    for pattern in [
        r'source\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'src\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'["\']([^"\']+\.m3u8[^"\']*)["\']',
    ]:
        m = re.search(pattern, iframe_html, re.IGNORECASE)
        if m:
            stream_url = m.group(1)
            xbmc.log(f'[sportshq/nba] found stream: {stream_url[:80]}', xbmc.LOGINFO)
            return stream_url, iframe_url

    raise RuntimeError('No m3u8 stream found in iframe page')


def list_events(handle, base_url, sport_thumb='', fanart=''):
    dialog = xbmcgui.DialogProgress()
    dialog.create('Sports HQ', 'Finding NBA games...')
    dialog.update(20)

    games = _get_games()
    dialog.close()

    if not games:
        xbmcgui.Dialog().notification(
            'Sports HQ', 'No live NBA games found right now',
            xbmcgui.NOTIFICATION_INFO, 4000
        )
        xbmcplugin.endOfDirectory(handle)
        return

    xbmcplugin.setContent(handle, 'videos')
    for game in games:
        li = xbmcgui.ListItem(label=game['title'])
        li.setArt({'thumb': sport_thumb, 'icon': sport_thumb, 'fanart': fanart})
        li.setInfo('video', {'title': game['title'], 'mediatype': 'video'})
        li.setProperty('IsPlayable', 'true')
        url = '{}?action=play&sport=nba&url={}&title={}'.format(
            base_url,
            urllib.parse.quote(game['url'], safe=''),
            urllib.parse.quote(game['title'], safe='')
        )
        xbmcplugin.addDirectoryItem(handle, url, li, False)

    xbmcplugin.addSortMethod(handle, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(handle)


def play_stream(handle, match_url, title='NBA'):
    xbmc.log(f'[sportshq/nba] play: {match_url}', xbmc.LOGINFO)

    dialog = xbmcgui.DialogProgress()
    dialog.create('Sports HQ', f'Loading {title}...')
    dialog.update(10)

    result = [None]
    error  = [None]
    done   = [False]

    def _run():
        try:
            stream_url, referer = _extract_stream(match_url)
            result[0] = (stream_url, referer)
        except Exception as exc:
            error[0] = str(exc)
            xbmc.log(f'[sportshq/nba] extract error: {exc}', xbmc.LOGERROR)
        finally:
            done[0] = True

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    elapsed = 0
    while not done[0] and elapsed < 30000:
        if dialog.iscanceled():
            dialog.close()
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return
        xbmc.sleep(200)
        elapsed += 200
        dialog.update(min(90, 10 + int(elapsed / 333)))

    dialog.close()

    if not result[0]:
        msg = error[0] or 'Stream not found'
        xbmcgui.Dialog().notification('Sports HQ', msg, xbmcgui.NOTIFICATION_ERROR, 7000)
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    stream_url, referer = result[0]
    headers_str = urllib.parse.urlencode({'User-Agent': _UA, 'Referer': referer})
    full_url    = f'{stream_url}|{headers_str}'

    li = xbmcgui.ListItem(path=full_url)
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
