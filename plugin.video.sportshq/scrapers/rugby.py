"""
scrapers/rugby.py — Rugby Union & League via rugbybox.me
"""

import os
import sys
import re
import datetime
import threading
import urllib.parse
import urllib.request
import html.parser

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs
import xbmcaddon

_ADDON_DIR  = xbmcaddon.Addon().getAddonInfo('path')
_KODI_TEMP  = xbmcvfs.translatePath('special://temp/')
_TEMP_FILE  = os.path.join(_KODI_TEMP, 'sportshq_rugby.url')

if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)

import extractor_runner_no_chrome as _extractor

SITE_URL = 'https://rugbybox.me/rugby-union-streams'

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-GB,en;q=0.5',
}


# ---------------------------------------------------------------------------
# Sydney time helper
# ---------------------------------------------------------------------------

def _site_to_nsw_offset():
    today = datetime.date.today()
    year  = today.year

    def _last_sunday(year, month):
        import calendar
        last = datetime.date(year, month, calendar.monthrange(year, month)[1])
        while last.weekday() != 6:
            last -= datetime.timedelta(days=1)
        return last

    def _first_sunday(year, month):
        d = datetime.date(year, month, 1)
        while d.weekday() != 6:
            d += datetime.timedelta(days=1)
        return d

    uk_bst  = _last_sunday(year, 3) <= today < _last_sunday(year, 10)
    uk_off  = 1 if uk_bst else 0
    nsw_aest = _first_sunday(year, 4) <= today < _first_sunday(year, 10)
    nsw_off  = 10 if nsw_aest else 11
    return nsw_off - uk_off


def _to_local_time(title):
    m = re.match(r'^(\d{1,2}):(\d{2})(.*)', title)
    if not m:
        return title
    hh, mm, rest = int(m.group(1)), int(m.group(2)), m.group(3)
    offset = _site_to_nsw_offset()
    total  = hh * 60 + mm + offset * 60
    hh, mm = (total // 60) % 24, total % 60
    return f'{hh:02d}:{mm:02d}{rest}'


# ---------------------------------------------------------------------------
# Match listing parser
# ---------------------------------------------------------------------------

class _MatchParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.matches       = []
        self._href         = None
        self._title        = None
        self._capture      = False

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        attrs = dict(attrs)
        href  = attrs.get('href', '')
        if (href and href.startswith('/')
                and href.count('/') >= 2
                and (href.endswith('-stream') or re.search(r'/stream-\d+$', href))):
            self._href    = 'https://rugbybox.me' + href
            self._title   = ''
            self._capture = True

    def handle_data(self, data):
        if self._capture:
            self._title += data

    def handle_endtag(self, tag):
        if tag == 'a' and self._capture:
            self._capture = False
            title = self._title.strip()
            href  = self._href
            if title and href and not any(m['url'] == href for m in self.matches):
                self.matches.append({'title': title, 'url': href})
            self._href  = None
            self._title = None


def _get_matches():
    import requests as _req
    resp = _req.get(SITE_URL, headers=_HEADERS, timeout=15, verify=False)
    resp.raise_for_status()
    parser = _MatchParser()
    parser.feed(resp.text)
    return parser.matches


# ---------------------------------------------------------------------------
# Kodi list / play
# ---------------------------------------------------------------------------

def list_matches(handle, base_url, sport_thumb='', fanart=''):
    try:
        matches = _get_matches()
    except Exception as exc:
        xbmcgui.Dialog().notification(
            'Sports HQ', f'Rugby: failed to load — {exc}',
            xbmcgui.NOTIFICATION_ERROR, 5000
        )
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    if not matches:
        xbmcgui.Dialog().notification(
            'Sports HQ', 'No rugby matches found',
            xbmcgui.NOTIFICATION_INFO, 4000
        )
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    li_refresh = xbmcgui.ListItem(label='[B]⟳ Refresh[/B]')
    li_refresh.setArt({'thumb': sport_thumb, 'fanart': fanart})
    xbmcplugin.addDirectoryItem(
        handle,
        base_url + '?action=refresh&sport=rugby',
        li_refresh, True
    )

    for match in matches:
        title = _to_local_time(match['title'])
        li = xbmcgui.ListItem(label=title)
        li.setArt({
            'thumb':  sport_thumb,
            'icon':   sport_thumb,
            'fanart': fanart,
            'poster': sport_thumb,
        })
        li.setInfo('video', {
            'title':     title,
            'plot':      title,
            'mediatype': 'video',
        })
        li.setProperty('IsPlayable', 'true')
        url = '{}?action=play&sport=rugby&url={}'.format(
            base_url, urllib.parse.quote(match['url'], safe='')
        )
        xbmcplugin.addDirectoryItem(handle, url, li, False)

    xbmcplugin.addSortMethod(handle, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(handle)
    xbmc.executebuiltin('Container.SetViewMode(500)')


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
        dialog.update(progress, f'Extracting stream... ({elapsed_ms // 1000}s)')
    return None, False


def play_stream(handle, match_url):
    xbmc.log(f'[sportshq/rugby] play_stream: {match_url}', xbmc.LOGINFO)

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
            xbmc.log(f'[sportshq/rugby] extractor error: {exc}', xbmc.LOGERROR)

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
            msg = 'Stream not live yet — no embed found on match page'
        xbmcgui.Dialog().notification(
            'Sports HQ', msg, xbmcgui.NOTIFICATION_ERROR, 7000
        )
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    xbmc.log(f'[sportshq/rugby] play_url={stream_url}', xbmc.LOGINFO)
    li = xbmcgui.ListItem(path=stream_url)
    li.setMimeType('application/vnd.apple.mpegurl')
    li.setContentLookup(False)
    li.setProperty('inputstream', 'inputstream.ffmpegdirect')
    li.setProperty('inputstream.ffmpegdirect.manifest_type', 'hls')
    li.setProperty('inputstream.ffmpegdirect.is_realtime_stream', 'true')
    li.setProperty('inputstream.ffmpegdirect.stream_mode', 'timeshift')
    li.setProperty('inputstream.ffmpegdirect.open_timeout', '30')
    xbmcplugin.setResolvedUrl(handle, True, li)

