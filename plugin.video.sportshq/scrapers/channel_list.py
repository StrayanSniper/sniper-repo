"""
scrapers/channel_list.py — TV Channel List for Sports HQ

Channel slots:
   1-29 : FTA (Nine, Seven, ABC, Ten, SBS via mjh.nz)
  30-40 : Rugby (rugbybox.me via casthill/boanki proxy)
  41-50 : UFC   (mmastream.me via iframe→m3u8)
  51-60 : Boxing (boxingbox.net via iframe→m3u8)
  61-70 : NBA   (crackstreams.ms + RoxieStreams via iframe→m3u8)
"""

import gzip
import json
import os
import re
import sys
import threading
import time
import urllib.parse

import xbmc
import xbmcgui
import xbmcaddon
import xbmcvfs

_ADDON      = xbmcaddon.Addon()
_ADDON_DIR  = _ADDON.getAddonInfo('path')
_ADDON_ICON = _ADDON.getAddonInfo('icon')
_KODI_TEMP  = xbmcvfs.translatePath('special://temp/')

if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)

import extractor_runner_no_chrome as _extractor
from scrapers.rugby import _to_local_time
from scrapers.live_tv import (
    _get_all_sections, _stream_list_load, _stream_list_save,
    _cache_read, _cache_write, _proxy_alive, _CACHE_MAX_AGE_S,
    _live_sessions, _live_sessions_lock,
)


def _img(f):
    return os.path.join(_ADDON_DIR, 'resources', 'images', f)


# ---------------------------------------------------------------------------
# Logo mapping
# ---------------------------------------------------------------------------

_RUGBY_LOGO_MAP = [
    ('/nrl',                  _img('logo_nrl.png'),          'NRL'),
    ('/england-super-league', _img('logo_super_league.png'), 'Super League'),
    ('/union-6-nations',      _img('logo_6_nations.png'),    'Six Nations'),
    ('/union-french-top-14',  _img('logo_top14.png'),        'Top 14'),
    ('/super-rugby',          _img('logo_super_rugby.png'),  'Super Rugby'),
    ('/rugby-union',          _img('logo_rugby_union.png'),  'Rugby Union'),
]

_SPORT_LOGOS = {
    'rugby':  _img('rugby.png'),
    'ufc':    _img('ufc.png'),
    'boxing': _img('boxing.png'),
    'nba':    _img('nba.png'),
}

SLOT_RANGES = {
    'rugby':  (30, 40),
    'ufc':    (41, 50),
    'boxing': (51, 60),
    'nba':    (61, 70),
}


def _rugby_logo(url):
    url_lower = (url or '').lower()
    for pattern, logo, label in _RUGBY_LOGO_MAP:
        if pattern in url_lower:
            return logo, label
    return _img('rugby.png'), 'Rugby'


# ---------------------------------------------------------------------------
# Non-rugby stream scrapers (iframe → m3u8)
# ---------------------------------------------------------------------------

_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)
_HEADERS = {'User-Agent': _UA, 'Accept': 'text/html,*/*;q=0.9'}


def _http_get(url, referer=''):
    try:
        import requests as _req
        h = {**_HEADERS}
        if referer:
            h['Referer'] = referer
        return _req.get(url, headers=h, timeout=12, verify=False).text
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# FTA channels (slots 1-29) via mjh.nz — handles Akamai token refresh
# ---------------------------------------------------------------------------

# Slot → (display_name, mjh_slug)
# Slugs are region-specific (Sydney); mjh.nz handles token auth transparently
_FTA_SLOT_MAP = [
    (1,  'Channel 9',   'mjh-channel-9-nsw'),
    (2,  '9Gem',        'mjh-gem-nsw'),
    (3,  '9Life',       'mjh-life-nsw'),
    (4,  '9Go!',        'mjh-go-nsw'),
    (5,  '9Rush',       'mjh-rush-nsw'),
    (6,  'Racing.com',  'mjh-racing-fast'),
    (7,  'Sky Racing 1','mjh-sky-racing-1'),
    (8,  'Sky Racing 2','mjh-sky-racing-2'),
    (9,  'Sky Thoroughbred', 'mjh-sky-racing-thoroughbred'),
    (10, 'LIV Golf',    'mjh-livgolfreplays-fast'),
    (11, 'Channel 7',   'mjh-seven-syd'),
    (12, '7TWO',        'mjh-7two-syd'),
    (13, '7mate',       'mjh-7mate-syd'),
    (14, '7flix',       'mjh-7flix-syd'),
    (15, 'Redbull TV',  'mjh-redbull-tv'),
    (16, 'MTV Ridiculousness', 'mjh-10-mtvridiculousness'),
    (21, 'ABC TV',      'mjh-abc-nsw'),
    (22, 'ABC Family',  'mjh-abc-tv-plus'),
    (23, 'ABC NEWS',    'mjh-abc-news'),
    (24, 'Channel 10',  'mjh-10-nsw'),
    (25, '10 Drama',    'mjh-10bold-nsw'),
    (26, '10 Comedy',   'mjh-10peach-nsw'),
    (27, 'SBS',         'mjh-sbs-sbst'),
    (28, 'SBS Food',    'mjh-sbs-3syd'),
    (29, 'NITV',        'mjh-sbs-5nsw'),
]

_MJH_URL   = 'https://i.mjh.nz/au/Sydney/tv.json.gz'
_NZAU_URL  = 'https://i.mjh.nz/nzau/tv.json.gz'
_NZAU_START_SLOT = 71

# Slugs already mapped in slots 1-29 — exclude from nzau extended list
_FTA_SLUGS_USED = {slug for _, _, slug in _FTA_SLOT_MAP}

# Radio channels to skip (audio only, won't work in video player)
_SKIP_NZAU_SLUGS = {
    'mjh-sbs-sbs-radio-1', 'mjh-sbs-sbs-radio-2', 'mjh-sbs-sbs-radio-3',
}


def _fta_logo(slug):
    """Return local logo path for an FTA channel, derived from its mjh slug."""
    name = slug.replace('mjh-', '').replace('-', '_') + '.png'
    return _img(f'fta_{name}')


def _mjh_pipe_headers(headers_dict):
    """Convert mjh.nz headers dict to Kodi pipe-header string."""
    parts = []
    for k, v in (headers_dict or {}).items():
        if not v or not str(v).strip():
            continue
        kl = k.lower()
        if kl == 'seekable':
            parts.append(f'seekable={v}')
        elif kl == 'user-agent':
            parts.append(f'User-Agent={urllib.parse.quote(str(v), safe="")}')
        elif kl == 'referer':
            parts.append(f'Referer={urllib.parse.quote(str(v), safe="")}')
    return '&'.join(parts)


def _get_fta_channels():
    """Return dict of {slot: channel_dict} for FTA channels via mjh.nz."""
    try:
        import requests as _req
        resp = _req.get(_MJH_URL, headers={'User-Agent': _UA}, timeout=15)
        data = json.loads(gzip.decompress(resp.content))
    except Exception as exc:
        xbmc.log(f'[channel_list] MJH fetch failed: {exc}', xbmc.LOGWARNING)
        return {}

    result = {}
    for slot, display_name, slug in _FTA_SLOT_MAP:
        ch = data.get(slug)
        if not ch:
            xbmc.log(f'[channel_list] MJH slug not found: {slug}', xbmc.LOGWARNING)
            continue
        pipe_hdrs  = _mjh_pipe_headers(ch.get('headers', {}))
        mjh_url    = ch['mjh_master']
        play_url   = f'{mjh_url}|{pipe_hdrs}' if pipe_hdrs else mjh_url
        local_logo = _fta_logo(slug)
        result[slot] = {
            'number':      slot,
            'name':        ch.get('name', display_name),
            'logo':        local_logo,
            'sport_label': display_name,
            'url':         mjh_url,
            'play_url':    play_url,
            'status':      'live',
            'sport_type':  'fta',
            'source':      'mjh.nz',
        }

    return result


def _get_nzau_channels():
    """
    Fetch all nzau channels from mjh.nz and assign to slots 71+.
    Excludes slugs already mapped in slots 1-29 and skips radio-only channels.
    """
    try:
        import requests as _req
        resp = _req.get(_NZAU_URL, headers={'User-Agent': _UA}, timeout=15)
        data = json.loads(gzip.decompress(resp.content))
    except Exception as exc:
        xbmc.log(f'[channel_list] nzau fetch failed: {exc}', xbmc.LOGWARNING)
        return {}

    result = {}
    slot = _NZAU_START_SLOT
    seen_urls = set()
    for slug, ch in sorted(data.items(), key=lambda x: x[1].get('name', '').lower()):
        if slug in _FTA_SLUGS_USED or slug in _SKIP_NZAU_SLUGS:
            continue
        mjh_url = ch.get('mjh_master', '')
        if not mjh_url or mjh_url in seen_urls:
            continue
        seen_urls.add(mjh_url)
        pipe_hdrs = _mjh_pipe_headers(ch.get('headers', {}))
        play_url  = f'{mjh_url}|{pipe_hdrs}' if pipe_hdrs else mjh_url
        net       = ch.get('network', 'TV') or 'TV'
        result[slot] = {
            'number':      slot,
            'name':        ch.get('name', slug),
            'logo':        ch.get('logo', ''),
            'sport_label': net,
            'url':         mjh_url,
            'play_url':    play_url,
            'status':      'live',
            'sport_type':  'fta',
            'source':      'mjh.nz',
        }
        slot += 1

    xbmc.log(f'[channel_list] nzau loaded {len(result)} extended channels', xbmc.LOGINFO)
    return result


def _scrape_sport_site(listing_url, base_url, source_name, link_keywords):
    """Generic scraper for crackstreams-style listing pages."""
    streams = []
    html    = _http_get(listing_url)
    if not html:
        return streams
    seen = set()
    for m in re.finditer(
        r'href=["\']((?:https?://[^"\']*|/[^"\']+))["\'][^>]*>\s*([^<]{5,80})',
        html, re.DOTALL
    ):
        href  = m.group(1).strip()
        title = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', m.group(2))).strip()
        if not href.startswith('http'):
            href = base_url + href
        if not any(k in href.lower() for k in link_keywords):
            continue
        if href in seen or len(title) < 4:
            continue
        seen.add(href)
        streams.append({'title': title, 'url': href, 'source': source_name})
    return streams


def _get_ufc_streams():
    streams = []
    streams += _scrape_sport_site(
        'https://mmastream.me/ufc-streams',
        'https://mmastream.me',
        'MMAStream',
        ['stream', 'ufc', 'mma', 'fight']
    )
    return streams


def _get_boxing_streams():
    streams = []
    for url in ['https://boxingbox.net/boxing-2024-streams', 'https://boxingbox.net/boxing-streams']:
        streams += _scrape_sport_site(url, 'https://boxingbox.net', 'BoxingBox', ['stream', 'boxing'])
        if streams:
            break
    return streams


def _get_nba_streams():
    streams = []
    # crackstreams.ms
    for listing in ['/nbastreams', '/crackstreams/nba-streams/']:
        s = _scrape_sport_site(
            'https://crackstreams.ms' + listing,
            'https://crackstreams.ms',
            'CrackStreams',
            ['stream', 'nba', 'vs', 'thunder', 'lakers', 'celtics', 'spurs', 'heat', 'warriors']
        )
        if s:
            streams += s
            break
    # RoxieStreams via JetExtractors
    try:
        from scrapers.ufc import _import_jetextractors
        _jex = _import_jetextractors()
        if _jex:
            items = _jex.search_extractors('nba', include=['RoxieStreams'])
            for item in items:
                streams.append({
                    'title':     _to_local_time(item.title),
                    'url':       None,
                    'jet_links': item.links,
                    'source':    item.extractor,
                })
    except Exception:
        pass
    return streams


# ---------------------------------------------------------------------------
# Non-rugby stream extraction (iframe → m3u8)
# ---------------------------------------------------------------------------

def _extract_iframe_m3u8(page_url):
    """Extract m3u8 URL from a crackstreams-style page via iframe."""
    html = _http_get(page_url, referer=page_url.rsplit('/', 1)[0])
    if not html:
        raise RuntimeError('Could not fetch page')
    iframe_m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if not iframe_m:
        raise RuntimeError('No iframe found')
    iframe_url = iframe_m.group(1)
    if not iframe_url.startswith('http'):
        iframe_url = 'https:' + iframe_url if iframe_url.startswith('//') else iframe_url

    iframe_html = _http_get(iframe_url, referer=page_url)
    if not iframe_html:
        raise RuntimeError('Could not fetch iframe')

    for pat in [
        r'source\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'["\']([^"\']+\.m3u8[^"\']*)["\']',
    ]:
        m = re.search(pat, iframe_html, re.IGNORECASE)
        if m:
            return m.group(1), iframe_url
    raise RuntimeError('No m3u8 found in iframe')


# ---------------------------------------------------------------------------
# Build channel list
# ---------------------------------------------------------------------------

def _build_channels():
    channels = []
    proxy_cache = _cache_read()
    now         = time.time()

    # ── FTA slots 1-29 ─────────────────────────────────────────────────────
    fta = _get_fta_channels()
    for slot in range(1, 30):
        if slot in fta:
            channels.append(fta[slot])
        else:
            # Leave unmapped slots empty (no Off Air placeholder for FTA)
            pass

    # ── Rugby slots 30-40 ──────────────────────────────────────────────────
    rugby_streams = _stream_list_load() or _get_all_sections()
    if rugby_streams:
        _stream_list_save(rugby_streams)

    start, end = SLOT_RANGES['rugby']
    for i, stream in enumerate(rugby_streams):
        slot = start + i
        if slot > end:
            break
        url   = stream['url']
        entry = proxy_cache.get(url, {})
        p_url = entry.get('proxy_url', '')
        fresh = now - entry.get('saved_at', 0) < _CACHE_MAX_AGE_S
        with _live_sessions_lock:
            sess = _live_sessions.get(url)
        if sess and sess.proxy_url and _proxy_alive(sess.proxy_url):
            status, play_url = 'live', sess.proxy_url
        elif p_url and fresh and _proxy_alive(p_url):
            status, play_url = 'live', p_url
        else:
            status, play_url = 'loading', url
        logo, sport_label = _rugby_logo(url)
        channels.append({
            'number': slot, 'name': _to_local_time(stream['title']),
            'logo': logo, 'sport_label': sport_label,
            'url': url, 'play_url': play_url,
            'status': status, 'sport_type': 'rugby',
        })
    _fill_off_air(channels, SLOT_RANGES['rugby'], _SPORT_LOGOS['rugby'], 'Rugby')

    # ── UFC slots 41-50 ────────────────────────────────────────────────────
    _add_sport_slots(channels, _get_ufc_streams(), SLOT_RANGES['ufc'],
                     _SPORT_LOGOS['ufc'], 'UFC', proxy_cache, now)

    # ── Boxing slots 51-60 ─────────────────────────────────────────────────
    _add_sport_slots(channels, _get_boxing_streams(), SLOT_RANGES['boxing'],
                     _SPORT_LOGOS['boxing'], 'Boxing', proxy_cache, now)

    # ── NBA slots 61-70 ────────────────────────────────────────────────────
    _add_sport_slots(channels, _get_nba_streams(), SLOT_RANGES['nba'],
                     _SPORT_LOGOS['nba'], 'NBA', proxy_cache, now)

    # ── Extended TV channels 71+ (mjh.nz nzau — AU/NZ FAST channels) ───────
    nzau = _get_nzau_channels()
    for slot in sorted(nzau.keys()):
        channels.append(nzau[slot])

    return channels


def _add_sport_slots(channels, streams, slot_range, logo, label, proxy_cache, now):
    start, end = slot_range
    for i, stream in enumerate(streams):
        slot = start + i
        if slot > end:
            break
        url      = stream.get('url')
        source   = stream.get('source', label)
        title    = f'{stream["title"]}  [{source}]'
        # Check if already extracted (cached direct URL)
        p_url = proxy_cache.get(url or '', {}).get('proxy_url', '') if url else ''
        fresh = now - proxy_cache.get(url or '', {}).get('saved_at', 0) < _CACHE_MAX_AGE_S
        if p_url and fresh and _proxy_alive(p_url):
            status, play_url = 'live', p_url
        else:
            status, play_url = 'loading', url
        channels.append({
            'number': slot, 'name': _to_local_time(title) if ':' in title else title,
            'logo': logo, 'sport_label': label,
            'url': url, 'play_url': play_url,
            'status': status, 'sport_type': stream.get('sport_type', label.lower()),
            'jet_links': stream.get('jet_links'),
            'source': source,
        })
    _fill_off_air(channels, slot_range, logo, label)


def _fill_off_air(channels, slot_range, logo, label):
    start, end = slot_range
    used_slots  = {c['number'] for c in channels if start <= c['number'] <= end}
    for slot in range(start, end + 1):
        if slot not in used_slots:
            channels.append({
                'number': slot, 'name': f'{label} Stream',
                'logo': logo, 'sport_label': label,
                'url': None, 'play_url': None,
                'status': 'off_air', 'sport_type': label.lower(),
            })


# ---------------------------------------------------------------------------
# Status label / list item
# ---------------------------------------------------------------------------

def _status_label(channel):
    game = channel.get('name', '')
    if channel['status'] == 'live':
        return f'[COLOR FF44FF88]● LIVE[/COLOR]   {game}'
    elif channel['status'] == 'loading':
        return f'[COLOR FFAAAAAA]⏳ Loading...[/COLOR]   {game}'
    return '[COLOR FF444466]○  Off Air[/COLOR]'


def _make_list_item(channel):
    li = xbmcgui.ListItem(label=channel.get('sport_label', ''))
    li.setLabel2(f'CH {channel["number"]}')
    li.setArt({'icon': channel['logo'], 'thumb': channel['logo']})
    li.setInfo('video', {'plot': '', 'genre': _status_label(channel)})
    return li


# ---------------------------------------------------------------------------
# Pre-extraction
# ---------------------------------------------------------------------------

def _preextract_channel(channel):
    url        = channel.get('url')
    sport_type = channel.get('sport_type', 'rugby')
    # FTA channels are direct HLS — no extraction needed
    if not url or channel['status'] == 'live' or sport_type == 'fta':
        return

    if sport_type == 'rugby':
        try:
            sess = _extractor.create_session(url)
            with _live_sessions_lock:
                _live_sessions[url] = sess
            _cache_write(url, sess.proxy_url)
            channel['play_url'] = sess.proxy_url
            channel['status']   = 'live'
        except Exception as exc:
            xbmc.log(f'[channel_list] rugby pre-extract failed {url}: {exc}', xbmc.LOGWARNING)
    else:
        # UFC/Boxing/NBA: iframe → m3u8
        try:
            stream_url, referer = _extract_iframe_m3u8(url)
            headers  = urllib.parse.urlencode({'User-Agent': _UA, 'Referer': referer})
            full_url = f'{stream_url}|{headers}'
            _cache_write(url, full_url)
            channel['play_url'] = full_url
            channel['status']   = 'live'
        except Exception as exc:
            xbmc.log(f'[channel_list] {sport_type} pre-extract failed {url}: {exc}', xbmc.LOGWARNING)


# ---------------------------------------------------------------------------
# Window class
# ---------------------------------------------------------------------------

class ChannelListWindow(xbmcgui.WindowXML):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.channels  = []
        self.list_ctrl = None

    def onInit(self):
        self.list_ctrl = self.getControl(100)
        self.setProperty('addon_version', f'v{_ADDON.getAddonInfo("version")}')
        threading.Thread(target=self._init_async, daemon=True).start()

    def _init_async(self):
        self.channels = _build_channels()
        self._refresh_list()
        self._start_preextraction()

    def _start_preextraction(self):
        loading = [ch for ch in self.channels if ch['status'] == 'loading' and ch.get('url')]
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
            self._refresh_list()
        threading.Thread(target=_do_all, daemon=True).start()

    def _refresh_list(self):
        try:
            pos = self.list_ctrl.getSelectedPosition()
            self.list_ctrl.reset()
            for ch in sorted(self.channels, key=lambda c: c['number']):
                self.list_ctrl.addItem(_make_list_item(ch))
            self.list_ctrl.selectItem(max(0, pos))
        except Exception:
            pass

    def onAction(self, action):
        if action.getId() in (
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
        channel = sorted(self.channels, key=lambda c: c['number'])[idx]

        if not channel.get('url') and not channel.get('jet_links'):
            xbmcgui.Dialog().notification('Sports HQ', 'Off Air', xbmcgui.NOTIFICATION_INFO, 2000)
            return

        play_url   = channel.get('play_url')
        sport_type = channel.get('sport_type', 'rugby')

        # FTA channels: direct play
        if sport_type == 'fta' and play_url:
            self._play_url(channel, play_url)
            return

        # Fast play if already extracted
        if play_url and ('127.0.0.1' in play_url or '.m3u8' in play_url):
            if '127.0.0.1' in play_url and not _proxy_alive(play_url):
                pass  # fall through to fresh extract
            else:
                self._play_url(channel, play_url)
                return

        # Extract now
        sport_type = channel.get('sport_type', 'rugby')
        dialog     = xbmcgui.DialogProgress()
        dialog.create('Sports HQ', f'Loading {channel["name"][:40]}...')
        dialog.update(10)

        result = [None]
        error  = [None]

        def _run():
            try:
                if sport_type == 'rugby':
                    temp_f = os.path.join(_KODI_TEMP, 'sportshq_ch_play.url')
                    _extractor.extract(channel['url'], temp_f)
                    with open(temp_f) as f:
                        result[0] = f.read().strip()
                    _cache_write(channel['url'], result[0])
                    channel['play_url'] = result[0]
                    channel['status']   = 'live'
                elif channel.get('jet_links'):
                    # JetExtractors path (RoxieStreams etc.)
                    from scrapers.ufc import _import_jetextractors
                    from jetextractors.models import JetLink
                    _jex = _import_jetextractors()
                    if _jex:
                        for link in channel['jet_links']:
                            if link.links:
                                ext = _jex.find_extractor(link)
                                if ext:
                                    resolved = ext.get_links(link)
                                    if resolved:
                                        result[0] = resolved[0].address
                                        return
                            else:
                                result[0] = link.address
                                return
                else:
                    stream_url, referer = _extract_iframe_m3u8(channel['url'])
                    headers  = urllib.parse.urlencode({'User-Agent': _UA, 'Referer': referer})
                    result[0] = f'{stream_url}|{headers}'
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
            dialog.update(min(90, 10 + int(elapsed / 333)))
        dialog.close()

        if not result[0]:
            msg = error[0] or 'Stream not found'
            xbmcgui.Dialog().notification('Sports HQ', msg, xbmcgui.NOTIFICATION_ERROR, 5000)
            return

        self._play_url(channel, result[0])

    def _play_url(self, channel, play_url):
        sport_type = channel.get('sport_type', 'rugby')
        xbmc.log(f'[channel_list] _play_url sport={sport_type} url={play_url[:80]}', xbmc.LOGINFO)

        if sport_type == 'fta':
            # Split pipe headers — extract HTTP headers for ffmpegdirect, drop Kodi-only directives
            if '|' in play_url:
                url_part, hdr_str = play_url.split('|', 1)
                # Keep only real HTTP headers (skip seekable=0 etc.)
                http_hdrs = '&'.join(
                    p for p in hdr_str.split('&')
                    if '=' in p and p.split('=', 1)[0].lower() not in ('seekable',)
                )
            else:
                url_part, http_hdrs = play_url, ''
            li = xbmcgui.ListItem(channel.get('name', ''), path=url_part)
            li.setMimeType('application/vnd.apple.mpegurl')
            li.setContentLookup(False)
            li.setProperty('inputstream',                                'inputstream.ffmpegdirect')
            li.setProperty('inputstream.ffmpegdirect.manifest_type',     'hls')
            li.setProperty('inputstream.ffmpegdirect.is_realtime_stream','true')
            li.setProperty('inputstream.ffmpegdirect.open_timeout',      '10')
            if http_hdrs:
                li.setProperty('inputstream.ffmpegdirect.stream_headers', http_hdrs)
            xbmc.Player().play(url_part, li)
            return

        li = xbmcgui.ListItem(channel.get('name', ''), path=play_url)
        li.setArt({'icon': channel['logo']})
        li.setMimeType('application/vnd.apple.mpegurl')
        li.setContentLookup(False)
        li.setProperty('inputstream',                               'inputstream.ffmpegdirect')
        li.setProperty('inputstream.ffmpegdirect.manifest_type',    'hls')
        li.setProperty('inputstream.ffmpegdirect.is_realtime_stream', 'true')
        li.setProperty('inputstream.ffmpegdirect.open_timeout',     '15')
        xbmc.Player().play(play_url, li)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def open_channel_list():
    window = ChannelListWindow('ChannelList.xml', _ADDON_DIR, 'default', '720p')
    window.doModal()
    del window
