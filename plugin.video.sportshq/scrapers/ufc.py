"""
scrapers/ufc.py — UFC live streams for Sports HQ.

Sources:
  1. mmastream.me  — dedicated UFC/MMA stream site
  2. JetExtractors search — StreamEast, RoxieStreams, Buffstreams etc.
"""

import re
import threading
import urllib.parse
import urllib.request

import os
import sys

import xbmc
import xbmcgui
import xbmcplugin
from scrapers.rugby import _to_local_time


def _import_jetextractors():
    """Import jetextractors, adding its addon lib path if needed."""
    try:
        from jetextractors import extractor as _jex
        return _jex
    except ImportError:
        pass
    try:
        import xbmcaddon
        je_path = os.path.join(
            xbmcaddon.Addon('script.module.jetextractors').getAddonInfo('path'), 'lib'
        )
        if je_path not in sys.path:
            sys.path.insert(0, je_path)
        from jetextractors import extractor as _jex
        return _jex
    except Exception as exc:
        xbmc.log(f'[sportshq] jetextractors not available: {exc}', xbmc.LOGWARNING)
        return None

_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)
_HEADERS = {'User-Agent': _UA, 'Accept': 'text/html,*/*;q=0.9'}
_BASE_MMA = 'https://mmastream.me'


# ---------------------------------------------------------------------------
# mmastream.me scraper
# ---------------------------------------------------------------------------

def _fetch(url, referer=''):
    try:
        import requests as _req
        headers = {**_HEADERS}
        if referer:
            headers['Referer'] = referer
        return _req.get(url, headers=headers, timeout=10, verify=False).text
    except Exception as exc:
        xbmc.log(f'[sportshq/ufc] fetch error {url}: {exc}', xbmc.LOGWARNING)
        return ''


def _scrape_mmastream():
    """Return list of {'title', 'embed_url'} from mmastream.me."""
    events = []
    html   = _fetch(f'{_BASE_MMA}/ufc-streams')
    if not html:
        return events

    # Build a date map from the listing page (slug -> date string)
    date_map = {}
    for dm in re.finditer(r'(\d{4}-\d{2}-\d{2}).*?href=["\'](/(?:ufc|mma)/([^"\']+)-stream)', html, re.DOTALL):
        date_map[dm.group(3)] = dm.group(1)

    seen = set()
    for m in re.finditer(r'href=["\'](/(?:ufc|mma)/([^"\']+)-stream(?:-\d+)?)["\']', html):
        path  = m.group(1)
        slug  = m.group(2)
        date  = date_map.get(slug, '')
        title = slug.replace('-', ' ').title()
        if date:
            try:
                y, m, d = date.split('-')
                date = f'{d}/{m}/{y[2:]}'
            except Exception:
                pass
            title = f'{date}  {title}'
        url   = _BASE_MMA + path
        if url in seen:
            continue
        seen.add(url)

        # Fetch event page to get embedsports URL
        event_html = _fetch(url, referer=f'{_BASE_MMA}/ufc-streams')
        em = re.search(r'src=["\']([^"\']*embedsports\.[^"\']+)["\']', event_html)
        events.append({'title': _to_local_time(title), 'embed_url': em.group(1) if em else None, 'source': 'MMAStream'})

    return events


# ---------------------------------------------------------------------------
# JetExtractors search
# ---------------------------------------------------------------------------

_JET_INCLUDE = ['RoxieStreams', 'StreamEast', 'Buffstreams', 'SportyBite', 'Streamed']


def _resolve_embedsports(embed_url):
    """
    Resolve embedsports.me/top URL to HLS stream URL.
    Only requires pycryptodome — no JetExtractors dependency.
    """
    import base64
    import requests as _req

    # Normalise embedsports.me/sport/event-1 → embedsports.top/sport/event/1
    m = re.match(r'https?://embedsports\.me/([^/]+)/(.+?)-(\d+)$', embed_url)
    if m:
        embed_url = f'https://embedsports.top/{m.group(1)}/{m.group(2)}/{m.group(3)}'

    parts     = embed_url.rstrip('/').split('/')
    stream_sc = parts[-3]
    stream_id = parts[-2]
    stream_no = parts[-1]

    payload = bytes([
        0x0A, len(stream_sc), *stream_sc.encode('utf-8'),
        0x12, len(stream_id), *stream_id.encode('utf-8'),
        0x1A, len(stream_no), *stream_no.encode('utf-8'),
    ])

    resp = _req.post(
        'https://embedsports.top/fetch',
        data=payload,
        headers={'Content-Type': 'application/octet-stream'},
        timeout=15,
        verify=False,
    )

    b64_len      = resp.content[1]
    b64_cipher   = resp.content[-b64_len:]
    b64_decipher = bytes(x - 47 if x >= 0x50 else x + 47 for x in b64_cipher)
    b64_data     = base64.b64decode(b64_decipher)
    aes_key      = resp.headers['What'].encode('utf-8')
    aes_iv       = b'STOPSTOPSTOPSTOP'

    try:
        from Cryptodome.Cipher import AES
        from Cryptodome.Util  import Counter
    except ImportError:
        from Crypto.Cipher import AES
        from Crypto.Util   import Counter

    ctr        = Counter.new(128, initial_value=int.from_bytes(aes_iv, 'big'))
    stream_url = AES.new(aes_key, AES.MODE_CTR, counter=ctr).decrypt(b64_data).decode('utf-8')
    xbmc.log(f'[sportshq] embedsports resolved: {stream_url[:80]}', xbmc.LOGINFO)
    return stream_url

def _search_jetextractors():
    results = []
    try:
        _jex = _import_jetextractors()
        if not _jex:
            return results
        items = _jex.search_extractors('ufc', include=_JET_INCLUDE)
        for item in items:
            results.append({'title': _to_local_time(item.title), 'jet_links': item.links, 'source': item.extractor})
    except Exception as exc:
        xbmc.log(f'[sportshq/ufc] jet search error: {exc}', xbmc.LOGWARNING)
    return results


# ---------------------------------------------------------------------------
# Kodi list
# ---------------------------------------------------------------------------

def list_events(handle, base_url, sport_thumb='', fanart=''):
    dialog = xbmcgui.DialogProgress()
    dialog.create('Sports HQ', 'Searching for UFC events...')
    dialog.update(10)

    all_events = []
    lock       = threading.Lock()
    done       = [0]

    def _run_mma():
        try:
            evs = _scrape_mmastream()
            with lock:
                all_events.extend(evs)
        finally:
            with lock:
                done[0] += 1

    def _run_jet():
        try:
            evs = _search_jetextractors()
            with lock:
                all_events.extend(evs)
        finally:
            with lock:
                done[0] += 1

    for fn in [_run_mma, _run_jet]:
        threading.Thread(target=fn, daemon=True).start()

    elapsed = 0
    while done[0] < 2 and elapsed < 10000:
        if dialog.iscanceled():
            dialog.close()
            xbmcplugin.endOfDirectory(handle, succeeded=False)
            return
        xbmc.sleep(200)
        elapsed += 200
        dialog.update(min(90, 10 + int(elapsed / 220)))

    dialog.close()

    if not all_events:
        xbmcgui.Dialog().notification(
            'Sports HQ', 'No live UFC events found right now',
            xbmcgui.NOTIFICATION_INFO, 4000
        )
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    xbmcplugin.setContent(handle, 'videos')
    xbmcplugin.setPluginCategory(handle, 'UFC')

    import json
    for event in all_events:
        title     = event.get('title', 'UFC Event')
        source    = event.get('source', '')
        embed_url = event.get('embed_url')
        is_live   = bool(embed_url or event.get('jet_links'))

        label = f'[B]{source}[/B]  {title}' if is_live else f'[COLOR FF888888][B]{source}[/B]  {title}  — Upcoming[/COLOR]'
        li    = xbmcgui.ListItem(label=label)
        li.setArt({'thumb': sport_thumb, 'icon': sport_thumb, 'fanart': fanart})
        li.setInfo('video', {'title': title, 'plot': f'{source} — {title}', 'mediatype': 'video'})
        li.setProperty('IsPlayable', 'true')

        if embed_url:
            payload = urllib.parse.quote(json.dumps({'type': 'embed', 'url': embed_url}), safe='')
        elif event.get('jet_links'):
            links   = [l.to_dict() for l in event['jet_links']]
            payload = urllib.parse.quote(json.dumps({'type': 'jet', 'links': links}), safe='')
        else:
            payload = urllib.parse.quote(json.dumps({'type': 'upcoming'}), safe='')

        url = f'{base_url}?action=play&sport=ufc&url={payload}&title={urllib.parse.quote(title)}'
        xbmcplugin.addDirectoryItem(handle, url, li, False)

    xbmcplugin.addSortMethod(handle, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(handle)


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def play_stream(handle, payload_json, title='UFC'):
    import json
    try:
        if json.loads(urllib.parse.unquote(payload_json)).get('type') == 'upcoming':
            xbmcgui.Dialog().notification('Sports HQ', 'Stream not live yet — check back when the event starts', xbmcgui.NOTIFICATION_INFO, 5000)
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return
    except Exception:
        pass

    try:
        payload = json.loads(urllib.parse.unquote(payload_json))
    except Exception:
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    dialog = xbmcgui.DialogProgress()
    dialog.create('Sports HQ', f'Loading {title}...')
    dialog.update(20)

    result = [None]
    done   = [False]

    error_msg = [None]

    def _resolve():
        try:
            if payload['type'] == 'embed':
                stream_url = _resolve_embedsports(payload['url'])
                if stream_url:
                    result[0] = stream_url
            else:
                # JetExtractors path for non-embed sources
                _jex = _import_jetextractors()
                if _jex:
                    from jetextractors.models import JetLink
                    links = [JetLink.from_dict(d) for d in payload.get('links', [])]
                    for link in links:
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
        except Exception as exc:
            error_msg[0] = str(exc)
            xbmc.log(f'[sportshq/ufc] resolve error: {exc}', xbmc.LOGERROR)
        finally:
            done[0] = True

    threading.Thread(target=_resolve, daemon=True).start()

    elapsed = 0
    while not done[0] and elapsed < 30000:
        if dialog.iscanceled():
            dialog.close()
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return
        xbmc.sleep(200)
        elapsed += 200
        dialog.update(min(90, 20 + int(elapsed / 333)))

    dialog.close()

    if not result[0]:
        msg = error_msg[0] or 'Could not resolve stream'
        xbmcgui.Dialog().ok('Sports HQ', f'Stream error:[CR]{msg}')
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    stream_url = result[0] if isinstance(result[0], str) else result[0].address
    li = xbmcgui.ListItem(label=title, path=stream_url)
    li.setMimeType('application/vnd.apple.mpegurl')
    li.setContentLookup(False)
    li.setProperty('inputstream',                               'inputstream.ffmpegdirect')
    li.setProperty('inputstream.ffmpegdirect.manifest_type',    'hls')
    li.setProperty('inputstream.ffmpegdirect.is_realtime_stream', 'true')
    li.setProperty('inputstream.ffmpegdirect.open_timeout',     '15')
    xbmcplugin.setResolvedUrl(handle, True, li)
