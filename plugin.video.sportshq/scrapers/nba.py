"""
scrapers/nba.py — NBA live streams via crackstreams.ms
Uses the same casthill/boanki pipeline as rugbybox.me.
"""

import os
import re
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
_TEMP_FILE = os.path.join(_KODI_TEMP, 'sportshq_nba.url')

if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)

import extractor_runner_no_chrome as _extractor

_BASE = 'https://crackstreams.ms'
_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-GB,en;q=0.5',
}


def _get_games():
    try:
        import requests as _req
        resp = _req.get(f'{_BASE}/nbastreams', headers=_HEADERS, timeout=15, verify=False)
        html = resp.text
        games = []
        seen  = set()
        for m in re.finditer(
            r'href=["\']((?:https?://[^"\']*crackstreams[^"\']*|/[^"\']+))["\'][^>]*>\s*([^<]{5,})',
            html, re.DOTALL
        ):
            href  = m.group(1).strip()
            title = re.sub(r'\s+', ' ', m.group(2)).strip()
            if not href.startswith('http'):
                href = _BASE + href
            if 'stream' not in href.lower() and 'nba' not in href.lower():
                continue
            if href in seen or not title:
                continue
            seen.add(href)
            games.append({'title': title, 'url': href})
        return games
    except Exception as exc:
        xbmc.log(f'[sportshq/nba] scrape error: {exc}', xbmc.LOGWARNING)
        return []


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
        url = '{}?action=play&sport=nba&url={}'.format(
            base_url, urllib.parse.quote(game['url'], safe='')
        )
        xbmcplugin.addDirectoryItem(handle, url, li, False)

    xbmcplugin.addSortMethod(handle, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(handle)


def _poll_for_url(thread, dialog, timeout_ms=60000):
    poll_ms    = 200
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
        dialog.update(min(90, 5 + int(elapsed_ms / timeout_ms * 85)))
    return None, False


def play_stream(handle, match_url, title='NBA'):
    xbmc.log(f'[sportshq/nba] play: {match_url}', xbmc.LOGINFO)

    _extractor.shutdown_proxy()
    try:
        os.remove(_TEMP_FILE)
    except OSError:
        pass

    dialog = xbmcgui.DialogProgress()
    dialog.create('Sports HQ', f'Loading {title}...')
    dialog.update(5)

    error_holder = [None]

    def _run():
        try:
            _extractor.extract(match_url, _TEMP_FILE)
        except Exception as exc:
            error_holder[0] = str(exc)
            xbmc.log(f'[sportshq/nba] extractor error: {exc}', xbmc.LOGERROR)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    stream_url, cancelled = _poll_for_url(t, dialog)
    dialog.close()

    if cancelled:
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    if not stream_url:
        msg = error_holder[0] or 'Stream not found'
        if any(x in (msg or '') for x in ['embed config not found', 'decodeSr', 'casthill', 'CSRF']):
            msg = 'Stream not live yet — check back when the game starts'
        xbmcgui.Dialog().notification('Sports HQ', msg, xbmcgui.NOTIFICATION_ERROR, 7000)
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    xbmc.log(f'[sportshq/nba] play_url={stream_url}', xbmc.LOGINFO)
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
