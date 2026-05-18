"""
scrapers/nba.py — NBA / WNBA live streams for Sports HQ.
Source: nbabox.co (embedsports.me resolution) + JetExtractors search.
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
_HEADERS  = {'User-Agent': _UA, 'Accept': 'text/html,*/*;q=0.9'}
_BASE_NBA = 'http://nbabox.co'


# ---------------------------------------------------------------------------
# nbabox.co scraper
# ---------------------------------------------------------------------------

def _fetch(url, referer=''):
    try:
        import requests as _req
        headers = {**_HEADERS}
        if referer:
            headers['Referer'] = referer
        return _req.get(url, headers=headers, timeout=10, verify=False).text
    except Exception as exc:
        xbmc.log(f'[sportshq/nba] fetch error {url}: {exc}', xbmc.LOGWARNING)
        return ''


def _scrape_nbabox():
    # embedsports protocol changed — disabled, use JetExtractors search
    return []


# ---------------------------------------------------------------------------
# JetExtractors search
# ---------------------------------------------------------------------------

_JET_INCLUDE = ['RoxieStreams', 'StreamEast', 'Buffstreams', 'SportyBite', 'Streamed']

def _search_jetextractors():
    results = []
    try:
        from scrapers.ufc import _import_jetextractors
        _jex = _import_jetextractors()
        if not _jex:
            return results
        for term in ['nba', 'wnba']:
            items = _jex.search_extractors(term, include=_JET_INCLUDE)
            for item in items:
                results.append({
                    'title':     _to_local_time(item.title),
                    'jet_links': item.links,
                    'source':    item.extractor,
                })
    except Exception as exc:
        xbmc.log(f'[sportshq/nba] jet search error: {exc}', xbmc.LOGWARNING)
    return results


# ---------------------------------------------------------------------------
# Kodi list
# ---------------------------------------------------------------------------

def list_events(handle, base_url, sport_thumb='', fanart=''):
    dialog = xbmcgui.DialogProgress()
    dialog.create('Sports HQ', 'Searching for NBA events...')
    dialog.update(10)

    all_events = []
    lock       = threading.Lock()
    done       = [0]

    def _run_nba():
        try:
            evs = _scrape_nbabox()
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

    for fn in [_run_nba, _run_jet]:
        threading.Thread(target=fn, daemon=True).start()

    elapsed = 0
    while done[0] < 2 and elapsed < 25000:
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
            'Sports HQ', 'No live NBA/WNBA games found right now',
            xbmcgui.NOTIFICATION_INFO, 4000
        )
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    xbmcplugin.setContent(handle, 'videos')
    xbmcplugin.setPluginCategory(handle, 'NBA')

    import json
    for event in all_events:
        title     = event.get('title', 'NBA Game')
        source    = event.get('source', '')
        embed_url = event.get('embed_url')
        is_live   = bool(embed_url or event.get('jet_links'))

        if is_live:
            label = f'[B]{source}[/B]  {title}'
        else:
            label = f'[COLOR FF888888][B]{source}[/B]  {title}  — Upcoming[/COLOR]'

        li = xbmcgui.ListItem(label=label)
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

        url = f'{base_url}?action=play&sport=nba&url={payload}&title={urllib.parse.quote(title)}'
        xbmcplugin.addDirectoryItem(handle, url, li, False)

    xbmcplugin.addSortMethod(handle, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(handle)


# ---------------------------------------------------------------------------
# Playback — reuse UFC resolver (same embed system)
# ---------------------------------------------------------------------------

def play_stream(handle, payload_json, title='NBA'):
    import json
    try:
        payload = json.loads(urllib.parse.unquote(payload_json))
        if payload.get('type') == 'upcoming':
            xbmcgui.Dialog().notification(
                'Sports HQ', 'Stream not live yet — check back when the game starts',
                xbmcgui.NOTIFICATION_INFO, 5000
            )
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return
    except Exception:
        pass
    from scrapers.ufc import play_stream as _ufc_play
    _ufc_play(handle, payload_json, title)
