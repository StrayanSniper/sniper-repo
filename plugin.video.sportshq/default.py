"""
plugin.video.sportshq — Sports HQ
Tile-based main menu: Rugby Union/League, UFC, Boxing.
"""

import sys
import os
import urllib.parse

import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon
import xbmcvfs

ADDON      = xbmcaddon.Addon()
HANDLE     = int(sys.argv[1])
BASE_URL   = sys.argv[0]
PARAMS     = urllib.parse.parse_qs(urllib.parse.urlparse(sys.argv[2]).query)
ADDON_PATH = ADDON.getAddonInfo('path')
FANART     = ADDON.getAddonInfo('fanart')
ICON       = ADDON.getAddonInfo('icon')

def _img(filename):
    return os.path.join(ADDON_PATH, 'resources', 'images', filename)

SPORTS = [
    ('rugby',  'Rugby Union / League', _img('rugby.png')),
    ('ufc',    'UFC',                  _img('ufc.png')),
    ('boxing', 'Boxing',               _img('boxing.png')),
]


def _param(key):
    v = PARAMS.get(key, [None])[0]
    return urllib.parse.unquote(v) if v else None


def main_menu():
    xbmcplugin.setPluginCategory(HANDLE, 'Sports HQ')
    xbmcplugin.setContent(HANDLE, 'videos')

    for sport_id, label, thumb in SPORTS:
        li = xbmcgui.ListItem(label=label)
        li.setArt({
            'thumb':  thumb,
            'icon':   thumb,
            'fanart': FANART,
            'poster': thumb,
            'banner': thumb,
        })
        li.setInfo('video', {
            'title':   label,
            'plot':    f'Live {label} streams',
            'mediatype': 'video',
        })
        url = f'{BASE_URL}?action=list&sport={sport_id}'
        xbmcplugin.addDirectoryItem(HANDLE, url, li, True)

    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(HANDLE)
    xbmc.executebuiltin('Container.SetViewMode(500)')


def list_sport(sport):
    sport_thumb = {
        'rugby':  _img('rugby.png'),
        'ufc':    _img('ufc.png'),
        'boxing': _img('boxing.png'),
    }.get(sport, ICON)

    xbmcplugin.setContent(HANDLE, 'videos')

    if sport == 'rugby':
        xbmcplugin.setPluginCategory(HANDLE, 'Rugby Union / League')
        from scrapers.rugby import list_matches
        list_matches(HANDLE, BASE_URL, sport_thumb, FANART)
    elif sport == 'ufc':
        xbmcplugin.setPluginCategory(HANDLE, 'UFC')
        from scrapers.ufc import list_events
        list_events(HANDLE, BASE_URL, sport_thumb, FANART)
    elif sport == 'boxing':
        xbmcplugin.setPluginCategory(HANDLE, 'Boxing')
        from scrapers.boxing import list_events
        list_events(HANDLE, BASE_URL, sport_thumb, FANART)
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
