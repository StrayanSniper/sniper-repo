"""
plugin.video.rugbystreams - default.py
Kodi plugin entry point (runs under Kodi's Python).
Scrapes rugbybox.me/rugby-union-streams to show a list of matches.
When a match is selected, spawns extractor_runner.py under system Python,
then polls a temp file until the extractor writes the proxy URL for the
720p stream, then calls setResolvedUrl.
"""

import sys
import os
import re
import signal
import datetime
import subprocess
import tempfile
import urllib.parse
import urllib.request
import html.parser

def _sydney_utc_offset():
    """Return Sydney's current UTC offset (10 = AEST, 11 = AEDT), no imports needed."""
    today = datetime.date.today()
    year  = today.year
    # DST ends first Sunday in April; starts first Sunday in October
    april1  = datetime.date(year, 4,  1)
    dst_end = april1  + datetime.timedelta(days=(6 - april1.weekday())  % 7)
    oct1    = datetime.date(year, 10, 1)
    dst_start = oct1 + datetime.timedelta(days=(6 - oct1.weekday()) % 7)
    return 10 if dst_end <= today < dst_start else 11


def _to_sydney_time(title):
    """Convert a leading HH:MM UTC time in a match title to Australia/Sydney time."""
    m = re.match(r'^(\d{2}):(\d{2})(.*)', title)
    if not m:
        return title
    hh, mm, rest = int(m.group(1)), int(m.group(2)), m.group(3)
    offset = _sydney_utc_offset()
    total  = hh * 60 + mm + offset * 60
    hh, mm = (total // 60) % 24, total % 60
    return f'{hh:02d}:{mm:02d}{rest}'

import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon

ADDON      = xbmcaddon.Addon()
ADDON_DIR  = ADDON.getAddonInfo('path')
HANDLE     = int(sys.argv[1])
BASE_URL   = sys.argv[0]
PARAMS     = urllib.parse.parse_qs(urllib.parse.urlparse(sys.argv[2]).query)

if sys.platform == 'win32':
    _win_py = r'C:\Users\surfy\AppData\Local\Programs\Python\Python313\python.exe'
    SYSTEM_PYTHON = _win_py if os.path.isfile(_win_py) else sys.executable
else:
    SYSTEM_PYTHON = sys.executable
SITE_URL         = 'https://rugbybox.me/rugby-union-streams'
TEMP_URL_FILE    = os.path.join(tempfile.gettempdir(), 'rugbystreams.url')
PID_FILE         = os.path.join(tempfile.gettempdir(), 'rugbystreams_extractor.pid')
# no-chrome extractor is tried first; Chrome extractor used as fallback
EXTRACTOR_NO_CHROME = os.path.join(ADDON_DIR, 'extractor_runner_no_chrome.py')
EXTRACTOR_CHROME    = os.path.join(ADDON_DIR, 'extractor_runner.py')

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
        if (href and href.startswith('/') and href.endswith('-stream')
                and href.count('/') == 2):
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
    req = urllib.request.Request(SITE_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        html_bytes = resp.read().decode('utf-8', errors='replace')
    parser = MatchParser()
    parser.feed(html_bytes)
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
        title = _to_sydney_time(match['title'])
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

def _launch_extractor(extractor_script, match_url, log_path):
    """Start an extractor subprocess, return the Popen object."""
    log_file = open(log_path, 'w')
    kwargs = {}
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [SYSTEM_PYTHON, extractor_script, match_url, TEMP_URL_FILE],
        stdout=log_file,
        stderr=log_file,
        **kwargs,
    )
    return proc


def _poll_for_url(proc, dialog, timeout_ms, progress_start, progress_end, label):
    """
    Poll TEMP_URL_FILE until the extractor writes a URL or times out.
    Returns (stream_url_or_None, cancelled_bool).
    """
    poll_ms    = 500
    elapsed_ms = 0
    while elapsed_ms < timeout_ms:
        if dialog.iscanceled():
            proc.kill()
            return None, True
        try:
            with open(TEMP_URL_FILE, 'r') as f:
                url = f.read().strip()
            if url:
                return url, False
        except OSError:
            pass
        if proc.poll() is not None:
            xbmc.log(f'[rugbystreams] {label} exited rc={proc.returncode}', xbmc.LOGWARNING)
            try:
                with open(TEMP_URL_FILE, 'r') as f:
                    url = f.read().strip()
                if url:
                    return url, False
            except OSError:
                pass
            return None, False   # exited without writing URL
        xbmc.sleep(poll_ms)
        elapsed_ms += poll_ms
        progress = min(progress_end, progress_start + int(elapsed_ms / timeout_ms * (progress_end - progress_start)))
        dialog.update(progress, f'{label}... ({elapsed_ms // 1000}s)')
    return None, False


def play_stream(match_url):
    xbmc.log(f'[rugbystreams] play_stream: {match_url}', xbmc.LOGINFO)

    # Kill any previous extractor process (it holds port 19823)
    try:
        with open(PID_FILE, 'r') as f:
            old_pid = int(f.read().strip())
        if sys.platform == 'win32':
            kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW}
            subprocess.call(['taskkill', '/PID', str(old_pid), '/F', '/T'], **kwargs)
        else:
            os.kill(old_pid, signal.SIGTERM)
        xbmc.log(f'[rugbystreams] killed old extractor pid={old_pid}', xbmc.LOGINFO)
    except Exception:
        pass

    try:
        os.remove(TEMP_URL_FILE)
    except OSError:
        pass

    dialog = xbmcgui.DialogProgress()
    dialog.create('Rugby Streams', 'Loading stream...')
    dialog.update(5)

    log_path = os.path.join(tempfile.gettempdir(), 'rugbystreams_extractor.log')
    stream_url = None

    # --- Try 1: no-chrome static extractor (fast, no Chrome dependency) ---
    try:
        proc = _launch_extractor(EXTRACTOR_NO_CHROME, match_url, log_path)
        xbmc.log(f'[rugbystreams] no-chrome extractor pid={proc.pid}', xbmc.LOGINFO)
        try:
            with open(PID_FILE, 'w') as f:
                f.write(str(proc.pid))
        except Exception:
            pass

        stream_url, cancelled = _poll_for_url(
            proc, dialog,
            timeout_ms=45000,
            progress_start=5, progress_end=50,
            label='Extracting (fast)'
        )
        if cancelled:
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            return
    except Exception as exc:
        xbmc.log(f'[rugbystreams] no-chrome extractor failed to start: {exc}', xbmc.LOGWARNING)

    # --- Try 2: Chrome/Selenium extractor (fallback if static failed) -----
    if not stream_url:
        xbmc.log('[rugbystreams] static extractor found no URL — falling back to Chrome',
                 xbmc.LOGINFO)
        try:
            os.remove(TEMP_URL_FILE)
        except OSError:
            pass
        dialog.update(50, 'Trying Chrome fallback...')
        try:
            proc = _launch_extractor(EXTRACTOR_CHROME, match_url, log_path)
            xbmc.log(f'[rugbystreams] Chrome extractor pid={proc.pid}', xbmc.LOGINFO)
            try:
                with open(PID_FILE, 'w') as f:
                    f.write(str(proc.pid))
            except Exception:
                pass

            stream_url, cancelled = _poll_for_url(
                proc, dialog,
                timeout_ms=60000,
                progress_start=50, progress_end=95,
                label='Extracting (Chrome)'
            )
            if cancelled:
                xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
                return
        except Exception as exc:
            dialog.close()
            xbmc.log(f'[rugbystreams] Chrome extractor failed to start: {exc}', xbmc.LOGERROR)
            xbmcgui.Dialog().notification(
                'Rugby Streams', f'Could not start extractor: {exc}',
                xbmcgui.NOTIFICATION_ERROR, 5000
            )
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            return

    dialog.close()
    xbmc.log(f'[rugbystreams] stream_url={stream_url}', xbmc.LOGINFO)

    if not stream_url:
        xbmcgui.Dialog().notification(
            'Rugby Streams', 'Stream not found — check Kodi log for details',
            xbmcgui.NOTIFICATION_ERROR, 6000
        )
        try:
            proc.kill()
        except Exception:
            pass
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    xbmc.log(f'[rugbystreams] play_url={stream_url}', xbmc.LOGINFO)
    li = xbmcgui.ListItem(path=stream_url)
    li.setMimeType('application/vnd.apple.mpegurl')
    li.setContentLookup(False)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


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
