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

def _sydney_utc_offset():
    today = datetime.date.today()
    year  = today.year
    april1    = datetime.date(year, 4, 1)
    dst_end   = april1 + datetime.timedelta(days=(6 - april1.weekday()) % 7)
    oct1      = datetime.date(year, 10, 1)
    dst_start = oct1   + datetime.timedelta(days=(6 - oct1.weekday())   % 7)
    return 10 if dst_end <= today < dst_start else 11


def _to_sydney_time(title):
    m = re.match(r'^(\d{2}):(\d{2})(.*)', title)
    if not m:
        return title
    hh, mm, rest = int(m.group(1)), int(m.group(2)), m.group(3)
    offset = _sydney_utc_offset()
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
    req = urllib.request.Request(SITE_URL, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        html_bytes = resp.read().decode('utf-8', errors='replace')
    parser = _MatchParser()
    parser.feed(html_bytes)
    return parser.matches


# ---------------------------------------------------------------------------
# Kodi list / play
# ---------------------------------------------------------------------------

def list_matches(handle, base_url):
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

    xbmcplugin.setContent(handle, 'videos')

    li_refresh = xbmcgui.ListItem(label='[B]⟳ Refresh[/B]')
    xbmcplugin.addDirectoryItem(
        handle,
        base_url + '?action=refresh&sport=rugby',
        li_refresh, True
    )

    for match in matches:
        title = _to_sydney_time(match['title'])
        li = xbmcgui.ListItem(label=title)
        li.setInfo('video', {'title': title, 'mediatype': 'video'})
        li.setProperty('IsPlayable', 'true')
        url = '{}?action=play&sport=rugby&url={}'.format(
            base_url, urllib.parse.quote(match['url'], safe='')
        )
        xbmcplugin.addDirectoryItem(handle, url, li, False)

    xbmcplugin.endOfDirectory(handle)


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
    xbmcplugin.setResolvedUrl(handle, True, li)
