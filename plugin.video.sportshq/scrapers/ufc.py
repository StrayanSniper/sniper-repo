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

import xbmc
import xbmcgui
import xbmcplugin
from scrapers.rugby import _to_local_time

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
        headers = {**_HEADERS}
        if referer:
            headers['Referer'] = referer
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode('utf-8', errors='replace')
    except Exception as exc:
        xbmc.log(f'[sportshq/ufc] fetch error {url}: {exc}', xbmc.LOGWARNING)
        return ''


def _scrape_mmastream():
    """Return list of {'title', 'embed_url'} from mmastream.me."""
    events = []
    html   = _fetch(f'{_BASE_MMA}/ufc-streams')
    if not html:
        return events

    seen = set()
    for m in re.finditer(r'href=["\'](/(?:ufc|mma)/([^"\']+)-stream(?:-\d+)?)["\']', html):
        path  = m.group(1)
        slug  = m.group(2)
        title = slug.replace('-', ' ').title()
        url   = _BASE_MMA + path
        if url in seen:
            continue
        seen.add(url)

        # Fetch event page to get embedsports URL
        event_html = _fetch(url, referer=f'{_BASE_MMA}/ufc-streams')
        em = re.search(r'src=["\']([^"\']*embedsports\.[^"\']+)["\']', event_html)
        if em:
            events.append({'title': _to_local_time(title), 'embed_url': em.group(1), 'source': 'MMAStream'})

    return events


# ---------------------------------------------------------------------------
# JetExtractors search
# ---------------------------------------------------------------------------

_JET_INCLUDE = ['RoxieStreams', 'StreamEast', 'Buffstreams', 'SportyBite', 'Streamed']

def _search_jetextractors():
    results = []
    try:
        from jetextractors import extractor as _jex
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
        title  = event.get('title', 'UFC Event')
        source = event.get('source', '')
        label  = f'[B]{source}[/B]  {title}'
        li     = xbmcgui.ListItem(label=label)
        li.setArt({'thumb': sport_thumb, 'icon': sport_thumb, 'fanart': fanart})
        li.setInfo('video', {'title': title, 'plot': f'{source} — {title}', 'mediatype': 'video'})
        li.setProperty('IsPlayable', 'true')

        if 'embed_url' in event:
            payload = urllib.parse.quote(json.dumps({'type': 'embed', 'url': event['embed_url']}), safe='')
        else:
            links = [l.to_dict() for l in event.get('jet_links', [])]
            payload = urllib.parse.quote(json.dumps({'type': 'jet', 'links': links}), safe='')

        url = f'{base_url}?action=play&sport=ufc&url={payload}&title={urllib.parse.quote(title)}'
        xbmcplugin.addDirectoryItem(handle, url, li, False)

    xbmcplugin.addSortMethod(handle, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(handle)


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def play_stream(handle, payload_json, title='UFC'):
    import json
    from jetextractors import extractor as _jex
    from jetextractors.models import JetLink, JetInputstreamFFmpegDirect

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

    def _resolve():
        try:
            if payload['type'] == 'embed':
                embed_url = payload['url']
                link      = JetLink(embed_url)
                ext       = _jex.find_extractor(link)
                if ext:
                    resolved = ext.get_link(link)
                    result[0] = resolved
            else:
                links = [JetLink.from_dict(d) for d in payload.get('links', [])]
                for link in links:
                    if link.links:
                        ext = _jex.find_extractor(link)
                        if ext:
                            resolved = ext.get_links(link)
                            if resolved:
                                result[0] = resolved[0]
                                return
                    else:
                        result[0] = link
                        return
        except Exception as exc:
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
        xbmcgui.Dialog().notification('Sports HQ', 'Could not resolve stream', xbmcgui.NOTIFICATION_ERROR, 5000)
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    resolved = result[0]
    li       = xbmcgui.ListItem(label=title, path=resolved.address)
    li.setContentLookup(False)

    if resolved.inputstream:
        iid = resolved.inputstream.inputstream_id
        li.setProperty('inputstream', iid)
        if hasattr(resolved.inputstream, 'manifest_type') and resolved.inputstream.manifest_type:
            li.setProperty(f'{iid}.manifest_type', resolved.inputstream.manifest_type)
        if hasattr(resolved.inputstream, 'is_realtime_stream'):
            li.setProperty(f'{iid}.is_realtime_stream',
                           'true' if resolved.inputstream.is_realtime_stream else 'false')
    else:
        li.setMimeType('application/vnd.apple.mpegurl')

    if resolved.headers:
        headers_str = '&'.join(f'{k}={urllib.parse.quote(str(v))}' for k, v in resolved.headers.items())
        li.setPath(f'{resolved.address}|{headers_str}')

    xbmcplugin.setResolvedUrl(handle, True, li)
