"""
scrapers/nba.py — NBA live streams via JetExtractors (RoxieStreams etc.)
"""

import threading
import urllib.parse

import xbmc
import xbmcgui
import xbmcplugin


def list_events(handle, base_url, sport_thumb='', fanart=''):
    dialog = xbmcgui.DialogProgress()
    dialog.create('Sports HQ', 'Searching for NBA events...')
    dialog.update(20)

    results = []
    done    = [False]

    def _search():
        try:
            from scrapers.ufc import _import_jetextractors
            _jex = _import_jetextractors()
            if _jex:
                for term in ['nba', 'wnba']:
                    try:
                        items = _jex.search_extractors(
                            term,
                            include=['RoxieStreams', 'StreamEast', 'Buffstreams']
                        )
                        for item in items:
                            results.append({
                                'title':  item.title or term.upper(),
                                'links':  item.links,
                                'source': item.extractor,
                            })
                    except Exception as exc:
                        xbmc.log(f'[sportshq/nba] search "{term}" error: {exc}', xbmc.LOGWARNING)
        except Exception as exc:
            xbmc.log(f'[sportshq/nba] jet error: {exc}', xbmc.LOGWARNING)
        finally:
            done[0] = True

    threading.Thread(target=_search, daemon=True).start()

    elapsed = 0
    while not done[0] and elapsed < 15000:
        if dialog.iscanceled():
            dialog.close()
            xbmcplugin.endOfDirectory(handle, succeeded=False)
            return
        xbmc.sleep(200)
        elapsed += 200
        dialog.update(min(90, 20 + int(elapsed / 166)))

    dialog.close()

    if not results:
        xbmcgui.Dialog().notification(
            'Sports HQ', 'No live NBA games right now',
            xbmcgui.NOTIFICATION_INFO, 4000
        )
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    xbmcplugin.setContent(handle, 'videos')

    import json
    for event in results:
        title  = event['title']
        source = event['source']
        li     = xbmcgui.ListItem(label=f'[B]{source}[/B]  {title}')
        li.setArt({'thumb': sport_thumb, 'icon': sport_thumb, 'fanart': fanart})
        li.setInfo('video', {'title': title, 'mediatype': 'video'})
        li.setProperty('IsPlayable', 'true')
        links   = [l.to_dict() for l in event['links']]
        payload = urllib.parse.quote(json.dumps({'type': 'jet', 'links': links}), safe='')
        url     = f'{base_url}?action=play&sport=nba&url={payload}&title={urllib.parse.quote(title)}'
        xbmcplugin.addDirectoryItem(handle, url, li, False)

    xbmcplugin.addSortMethod(handle, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(handle)


def play_stream(handle, payload_json, title='NBA'):
    from scrapers.ufc import play_stream as _ufc_play
    _ufc_play(handle, payload_json, title)
