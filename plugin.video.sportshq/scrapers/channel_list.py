"""
scrapers/channel_list.py — Custom TV channel list window for Sports HQ.

Opens a full-screen WindowXML styled like a TV channel guide.
Fixed channel slots 30-40 are populated with live rugbybox.me streams.
Channels with live proxies play instantly; others extract on click.
"""

import os
import sys
import threading
import time

import xbmc
import xbmcgui
import xbmcaddon
import xbmcvfs

_ADDON       = xbmcaddon.Addon()
_ADDON_DIR   = _ADDON.getAddonInfo('path')
_ADDON_ICON  = _ADDON.getAddonInfo('icon')
_KODI_TEMP   = xbmcvfs.translatePath('special://temp/')

if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)

import extractor_runner_no_chrome as _extractor
from scrapers.rugby import _to_local_time
from scrapers.live_tv import (
    _get_all_sections, _stream_list_load, _stream_list_save,
    _cache_read, _cache_write, _proxy_alive, _CACHE_MAX_AGE_S,
    _live_sessions, _live_sessions_lock,
)

def _img(filename):
    return os.path.join(_ADDON_DIR, 'resources', 'images', filename)

_LOGO_MAP = [
    ('/nrl',                  _img('logo_nrl.png'),          'NRL'),
    ('/england-super-league', _img('logo_super_league.png'), 'Super League'),
    ('/union-6-nations',      _img('logo_6_nations.png'),    'Six Nations'),
    ('/union-french-top-14',  _img('logo_top14.png'),        'Top 14'),
    ('/super-rugby',          _img('logo_super_rugby.png'),  'Super Rugby'),
    ('/rugby-union',          _img('logo_rugby_union.png'),  'Rugby Union'),
]
_DEFAULT_LOGO = _img('rugby.png')

# Channel slot range for live streams
STREAM_SLOT_START = 30
STREAM_SLOT_END   = 40


# ---------------------------------------------------------------------------
# Channel data helpers
# ---------------------------------------------------------------------------

def _build_channels():
    """
    Build the full channel list.
    Slots 30-40 filled from rugbybox.me live streams; rest are empty.
    Returns list of channel dicts.
    """
    channels = []

    # Static channel stubs (to be populated later with FTA/Fox)
    # Uncomment and fill when real channels are added:
    # channels += [
    #     {'number': 1,  'name': 'Fox League',   'logo': ..., 'url': None, 'section': 'Fox Sports', 'programme': ''},
    # ]

    # Live stream slots 30-40
    streams      = _stream_list_load() or _get_all_sections()
    proxy_cache  = _cache_read()
    now          = time.time()

    if streams:
        _stream_list_save(streams)

    for i, stream in enumerate(streams):
        slot = STREAM_SLOT_START + i
        if slot > STREAM_SLOT_END:
            break

        url     = stream['url']
        entry   = proxy_cache.get(url, {})
        p_url   = entry.get('proxy_url', '')
        fresh   = now - entry.get('saved_at', 0) < _CACHE_MAX_AGE_S

        # Check in-memory sessions first
        with _live_sessions_lock:
            sess = _live_sessions.get(url)
        if sess and sess.proxy_url and _proxy_alive(sess.proxy_url):
            status = 'live'
            play_url = sess.proxy_url
        elif p_url and fresh and _proxy_alive(p_url):
            status = 'live'
            play_url = p_url
        else:
            status = 'loading'
            play_url = url  # will extract on click

        logo, sport_label = _logo_and_label(url)
        channels.append({
            'number':      slot,
            'name':        _to_local_time(stream['title']),
            'logo':        logo,
            'sport_label': sport_label,
            'url':         url,
            'play_url':    play_url,
            'section':     stream.get('section', sport_label),
            'status':      status,
        })

    # Fill remaining slots as Off Air
    used = len([c for c in channels if c['number'] >= STREAM_SLOT_START])
    for i in range(used, STREAM_SLOT_END - STREAM_SLOT_START + 1):
        slot = STREAM_SLOT_START + i
        channels.append({
            'number':      slot,
            'name':        f'Stream {i + 1}',
            'logo':        _DEFAULT_LOGO,
            'sport_label': '',
            'url':         None,
            'play_url':    None,
            'section':     '',
            'status':      'off_air',
        })

    return channels


def _status_label(channel):
    game = channel.get('name', '')
    if channel['status'] == 'live':
        return f'[COLOR FF44FF88]● LIVE[/COLOR]   {game}'
    elif channel['status'] == 'loading':
        return f'[COLOR FFAAAAAA]⏳ Loading...[/COLOR]   {game}'
    else:
        return '[COLOR FF444466]○  Off Air[/COLOR]'


def _logo_and_label(url):
    """Return (logo_path, sport_label) for a stream URL."""
    url_lower = (url or '').lower()
    for pattern, logo, label in _LOGO_MAP:
        if pattern in url_lower:
            return logo, label
    return _DEFAULT_LOGO, 'Rugby'


def _sport_label(channel):
    return channel.get('sport_label') or channel.get('section', '')


def _make_list_item(channel):
    sport = _sport_label(channel)
    li = xbmcgui.ListItem(label=sport)   # left column: NRL / Rugby Union
    li.setLabel2(f'CH {channel["number"]}')
    li.setArt({'icon': channel['logo'], 'thumb': channel['logo']})
    li.setInfo('video', {
        'plot':  '',                       # nothing below the sport label
        'genre': _status_label(channel),   # right column: ● LIVE 18:25 Penrith vs St George
    })
    return li


# ---------------------------------------------------------------------------
# Pre-extraction
# ---------------------------------------------------------------------------

def _preextract_channel(channel):
    url = channel['url']
    if not url:
        return
    # Skip if already live
    if channel['status'] == 'live':
        return
    try:
        sess = _extractor.create_session(url)
        with _live_sessions_lock:
            _live_sessions[url] = sess
        _cache_write(url, sess.proxy_url)
        channel['play_url'] = sess.proxy_url
        channel['status']   = 'live'
        print(f'[channel_list] pre-extracted {url}', file=sys.stderr, flush=True)
    except Exception as exc:
        print(f'[channel_list] pre-extract failed {url}: {exc}', file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Window class
# ---------------------------------------------------------------------------

class ChannelListWindow(xbmcgui.WindowXML):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.channels      = []
        self.list_ctrl     = None
        self._preext_done  = False

    def onInit(self):
        self.list_ctrl = self.getControl(100)
        self.setProperty('addon_version', f'v{_ADDON.getAddonInfo("version")}')
        # Return immediately so the window renders — load everything in background
        threading.Thread(target=self._init_async, daemon=True).start()

    def _init_async(self):
        """Runs after the window is visible. Scrapes, populates, pre-extracts."""
        if not _stream_list_load():
            streams = _get_all_sections()
            if streams:
                _stream_list_save(streams)
        self._load_channels()
        self._start_preextraction()

    def _load_channels(self):
        self.channels = _build_channels()
        self.list_ctrl.reset()
        for ch in self.channels:
            self.list_ctrl.addItem(_make_list_item(ch))

    def _start_preextraction(self):
        """Pre-extract all loading channels in parallel background threads."""
        loading = [ch for ch in self.channels if ch['status'] == 'loading' and ch['url']]
        if not loading:
            return

        def _do_all():
            threads = [
                threading.Thread(target=_preextract_channel, args=(ch,), daemon=True)
                for ch in loading
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            # Refresh the list once all done
            self._refresh_list()

        threading.Thread(target=_do_all, daemon=True).start()

    def _refresh_list(self):
        """Update list items to reflect current extraction status."""
        try:
            pos = self.list_ctrl.getSelectedPosition()
            self.list_ctrl.reset()
            for ch in self.channels:
                self.list_ctrl.addItem(_make_list_item(ch))
            self.list_ctrl.selectItem(pos)
        except Exception:
            pass

    def onAction(self, action):
        action_id = action.getId()
        if action_id in (
            xbmcgui.ACTION_PREVIOUS_MENU,
            xbmcgui.ACTION_NAV_BACK,
            xbmcgui.ACTION_BACKSPACE,
        ):
            self.close()

    def onClick(self, control_id):
        if control_id == 100:
            self._play_selected()

    def _play_selected(self):
        idx     = self.list_ctrl.getSelectedPosition()
        channel = self.channels[idx]

        if not channel['url']:
            xbmcgui.Dialog().notification('Sports HQ', 'Off Air', xbmcgui.NOTIFICATION_INFO, 2000)
            return

        play_url = channel.get('play_url')

        # Fast play from pre-extracted proxy
        if play_url and play_url.startswith('http://127.0.0.1') and _proxy_alive(play_url):
            self._play_url(channel, play_url)
            return

        # Extract now with progress dialog
        dialog = xbmcgui.DialogProgress()
        dialog.create('Sports HQ', f'Loading {channel["name"]}...')
        dialog.update(10)

        result   = [None]
        error    = [None]
        temp_f   = os.path.join(_KODI_TEMP, 'sportshq_ch_play.url')

        def _run():
            try:
                _extractor.extract(channel['url'], temp_f)
                with open(temp_f) as f:
                    result[0] = f.read().strip()
                _cache_write(channel['url'], result[0])
                channel['play_url'] = result[0]
                channel['status']   = 'live'
            except Exception as exc:
                error[0] = str(exc)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        elapsed = 0
        while t.is_alive() and elapsed < 30000:
            if dialog.iscanceled():
                dialog.close()
                return
            xbmc.sleep(200)
            elapsed += 200
            dialog.update(min(90, 10 + int(elapsed / 300)))

        dialog.close()

        if error[0] or not result[0]:
            msg = error[0] or 'Stream not found'
            if any(x in (msg or '') for x in [
                'embed config not found', 'decodeSr', 'videoSource',
                'CSRF', 'Pattern not found', 'casthill',
            ]):
                msg = 'Stream not live yet — check back when the match starts'
            xbmcgui.Dialog().notification('Sports HQ', msg, xbmcgui.NOTIFICATION_ERROR, 5000)
            return

        self._play_url(channel, result[0])

    def _play_url(self, channel, proxy_url):
        li = xbmcgui.ListItem(channel['name'], path=proxy_url)
        li.setArt({'icon': channel['logo']})
        li.setMimeType('application/vnd.apple.mpegurl')
        li.setContentLookup(False)
        li.setProperty('inputstream',                               'inputstream.ffmpegdirect')
        li.setProperty('inputstream.ffmpegdirect.manifest_type',    'hls')
        li.setProperty('inputstream.ffmpegdirect.is_realtime_stream', 'true')
        li.setProperty('inputstream.ffmpegdirect.open_timeout',     '15')
        xbmc.Player().play(proxy_url, li)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def open_channel_list():
    """Open the TV channel list window immediately. All loading happens inside onInit."""
    window = ChannelListWindow('ChannelList.xml', _ADDON_DIR, 'default', '720p')
    window.doModal()
    del window
