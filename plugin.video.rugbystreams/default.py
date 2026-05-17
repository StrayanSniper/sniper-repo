"""
plugin.video.rugbystreams - default.py
Kodi plugin entry point (runs under Kodi's Python).
Scrapes rugbybox.me/rugby-union-streams to show a list of matches.
When a match is selected, runs the stream extractor in a background thread
(in-process — no subprocess), then polls a temp file until the extractor
writes the proxy URL, then calls setResolvedUrl.
"""

import sys
import os
import re
import datetime
import threading
import urllib.parse
import urllib.request
import html.parser

def _site_to_nsw_offset():
    """
    rugbybox.me stores times in UK time (BST=UTC+1 in summer, GMT=UTC+0 in winter).
    NSW is AEST=UTC+10 (Apr-Oct) or AEDT=UTC+11 (Oct-Apr).
    Returns hours to add to site time to get NSW time.
    """
    today = datetime.date.today()
    year  = today.year

    def _last_sunday(year, month):
        d = datetime.date(year, month, 28)
        # find last day of month
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

import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon
import xbmcvfs

ADDON      = xbmcaddon.Addon()
ADDON_DIR  = ADDON.getAddonInfo('path')
HANDLE     = int(sys.argv[1])
BASE_URL   = sys.argv[0]
PARAMS     = urllib.parse.parse_qs(urllib.parse.urlparse(sys.argv[2]).query)

SITE_URL      = 'https://rugbybox.me/rugby-union-streams'
_KODI_TEMP    = xbmcvfs.translatePath('special://temp/')
TEMP_URL_FILE = os.path.join(_KODI_TEMP, 'rugbystreams.url')

# Import the extractor in-process — no subprocess, works on Android/Firestick
if ADDON_DIR not in sys.path:
    sys.path.insert(0, ADDON_DIR)
import extractor_runner_no_chrome as _extractor

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-GB,en;q=0.5',
}


# ---------------------------------------------------------------------------
# Match listing
# ---------------------------------------------------------------------------

class MatchParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.matches       = []
        self._current_href  = None
        self._current_title = None
        self._capture       = False

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        attrs = dict(attrs)
        href  = attrs.get('href', '')
        if (href and href.startswith('/')
                and href.count('/') >= 2
                and (href.endswith('-stream') or re.search(r'/stream-\d+$', href))):
            self._current_href  = 'https://rugbybox.me' + href
            self._current_title = ''
            self._capture       = True

    def handle_data(self, data):
        if self._capture:
            self._current_title += data

    def handle_endtag(self, tag):
        if tag == 'a' and self._capture:
            self._capture = False
            title = self._current_title.strip()
            href  = self._current_href
            if title and href and not any(m['url'] == href for m in self.matches):
                self.matches.append({'title': title, 'url': href})
            self._current_href  = None
            self._current_title = None


def get_matches():
    import requests as _req
    resp = _req.get(SITE_URL, headers=HEADERS, timeout=15, verify=False)
    resp.raise_for_status()
    parser = MatchParser()
    parser.feed(resp.text)
    return parser.matches


def list_matches():
    try:
        matches = get_matches()
    except Exception as exc:
        xbmcgui.Dialog().notification(
            'Rugby Streams', f'Failed to load matches: {exc}',
            xbmcgui.NOTIFICATION_ERROR, 5000
        )
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    if not matches:
        xbmcgui.Dialog().notification(
            'Rugby Streams', 'No matches found',
            xbmcgui.NOTIFICATION_INFO, 4000
        )
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    xbmcplugin.setContent(HANDLE, 'videos')

    # Refresh item at the top
    li_refresh = xbmcgui.ListItem(label='[B]⟳ Refresh[/B]')
    xbmcplugin.addDirectoryItem(HANDLE, BASE_URL + '?action=refresh', li_refresh, True)

    for match in matches:
        title = _to_local_time(match['title'])
        li = xbmcgui.ListItem(label=title)
        li.setInfo('video', {'title': title, 'mediatype': 'video'})
        li.setProperty('IsPlayable', 'true')
        url = '{}?action=play&url={}'.format(
            BASE_URL, urllib.parse.quote(match['url'], safe='')
        )
        xbmcplugin.addDirectoryItem(HANDLE, url, li, False)

    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def _poll_for_url(thread, dialog, timeout_ms=60000):
    poll_ms    = 500
    elapsed_ms = 0
    while elapsed_ms < timeout_ms:
        if dialog.iscanceled():
            return None, True
        try:
            with open(TEMP_URL_FILE, 'r') as f:
                url = f.read().strip()
            if url:
                return url, False
        except OSError:
            pass
        if not thread.is_alive():
            try:
                with open(TEMP_URL_FILE, 'r') as f:
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


def play_stream(match_url):
    xbmc.log(f'[rugbystreams] play_stream: {match_url}', xbmc.LOGINFO)

    # Shut down any proxy left running from a previous playback
    _extractor.shutdown_proxy()

    try:
        os.remove(TEMP_URL_FILE)
    except OSError:
        pass

    dialog = xbmcgui.DialogProgress()
    dialog.create('Rugby Streams', 'Loading stream...')
    dialog.update(5)

    error_holder = [None]

    def _run():
        try:
            _extractor.extract(match_url, TEMP_URL_FILE)
        except Exception as exc:
            error_holder[0] = str(exc)
            xbmc.log(f'[rugbystreams] extractor error: {exc}', xbmc.LOGERROR)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    stream_url, cancelled = _poll_for_url(t, dialog)
    dialog.close()

    if cancelled:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    if not stream_url:
        msg = error_holder[0] or 'Stream not found'
        if 'embed config not found' in msg:
            msg = 'Stream not live yet — no embed found on match page'
        xbmcgui.Dialog().notification(
            'Rugby Streams', msg, xbmcgui.NOTIFICATION_ERROR, 7000
        )
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    xbmc.log(f'[rugbystreams] play_url={stream_url}', xbmc.LOGINFO)
    li = xbmcgui.ListItem(path=stream_url)
    li.setMimeType('application/vnd.apple.mpegurl')
    li.setContentLookup(False)
    li.setProperty('inputstream', 'inputstream.ffmpegdirect')
    li.setProperty('inputstream.ffmpegdirect.manifest_type', 'hls')
    li.setProperty('inputstream.ffmpegdirect.is_realtime_stream', 'true')
    li.setProperty('inputstream.ffmpegdirect.stream_mode', 'timeshift')
    li.setProperty('inputstream.ffmpegdirect.open_timeout', '30')
    xbmcplugin.setResolvedUrl(HANDLE, True, li)

    # Keep this script alive while the video plays so the proxy daemon
    # threads stay alive. Without this Kodi kills the script (and proxy)
    # after a few minutes.
    player  = xbmc.Player()
    monitor = xbmc.Monitor()
    timeout = 0
    while not player.isPlaying() and timeout < 20 and not monitor.abortRequested():
        xbmc.sleep(500)
        timeout += 1
    while player.isPlaying() and not monitor.abortRequested():
        xbmc.sleep(1000)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def router():
    action = PARAMS.get('action', [None])[0]
    if action is None:
        list_matches()
    elif action == 'refresh':
        xbmc.executebuiltin('Container.Refresh')
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
    elif action == 'play':
        raw = PARAMS.get('url', [None])[0]
        if raw:
            play_stream(urllib.parse.unquote(raw))
        else:
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


if __name__ == '__main__':
    router()
