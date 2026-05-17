"""
plugin.video.sportshq — Sports HQ
Main menu: Rugby Union/League, UFC, Boxing.
"""

import sys
import urllib.parse

import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon

ADDON    = xbmcaddon.Addon()
HANDLE   = int(sys.argv[1])
BASE_URL = sys.argv[0]
PARAMS   = urllib.parse.parse_qs(urllib.parse.urlparse(sys.argv[2]).query)

SPORTS = [
    ('rugby',  'Rugby Union / League'),
    ('ufc',    'UFC'),
    ('boxing', 'Boxing'),
]


def _param(key):
    v = PARAMS.get(key, [None])[0]
    return urllib.parse.unquote(v) if v else None


def main_menu():
    xbmcplugin.setContent(HANDLE, 'files')
    for sport_id, label in SPORTS:
        li = xbmcgui.ListItem(label=f'[B]{label}[/B]')
        li.setArt({'thumb': ADDON.getAddonInfo('icon'),
                   'fanart': ADDON.getAddonInfo('fanart')})
        url = f'{BASE_URL}?action=list&sport={sport_id}'
        xbmcplugin.addDirectoryItem(HANDLE, url, li, True)
    xbmcplugin.endOfDirectory(HANDLE)


def list_sport(sport):
    if sport == 'rugby':
        from scrapers.rugby import list_matches
        list_matches(HANDLE, BASE_URL)
    elif sport == 'ufc':
        from scrapers.ufc import list_events
        list_events(HANDLE, BASE_URL)
    elif sport == 'boxing':
        from scrapers.boxing import list_events
        list_events(HANDLE, BASE_URL)
    else:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


def play_stream(sport, url):
    if sport == 'rugby':
        from scrapers.rugby import play_stream
        play_stream(HANDLE, url)
    else:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


def router():
    action = _param('action')
    sport  = _param('sport')

    if action is None:
        main_menu()
    elif action == 'list' and sport:
        list_sport(sport)
    elif action == 'play' and sport:
        url = _param('url')
        if url:
            play_stream(sport, url)
        else:
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
    elif action == 'refresh':
        xbmc.executebuiltin('Container.Refresh')
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
    else:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


if __name__ == '__main__':
    router()
