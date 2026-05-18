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
    ('livetv', 'Live TV & Streams',    ICON),
    ('rugby',  'Rugby Union / League', _img('rugby.png')),
    ('ufc',    'UFC',                  _img('ufc.png')),
    ('boxing', 'Boxing',               _img('boxing.png')),
    ('nba',    'NBA',                  _img('logo_nba.jpg')),
]

ADVANCED_SETTINGS = """<advancedsettings>
  <network>
    <buffermode>4</buffermode>
    <cachemembuffersize>104857600</cachemembuffersize>
    <readbufferfactor>20</readbufferfactor>
  </network>
</advancedsettings>
"""


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

    # Buffer optimiser tile
    li_buf = xbmcgui.ListItem(label='[B]⚙ Optimise Buffering[/B]')
    li_buf.setArt({'thumb': ICON, 'icon': ICON, 'fanart': FANART})
    li_buf.setInfo('video', {'title': 'Optimise Buffering', 'plot': 'Write advancedsettings.xml to maximise stream buffer. Run once then restart Kodi.', 'mediatype': 'video'})
    xbmcplugin.addDirectoryItem(HANDLE, f'{BASE_URL}?action=optimise_buffer', li_buf, False)

    # Check for updates tile
    li_upd = xbmcgui.ListItem(label='[B]↻ Check for Updates[/B]')
    li_upd.setArt({'thumb': ICON, 'icon': ICON, 'fanart': FANART})
    li_upd.setInfo('video', {'title': 'Check for Updates', 'plot': 'Check Sniper Repo for addon updates and restart Kodi to apply.', 'mediatype': 'video'})
    xbmcplugin.addDirectoryItem(HANDLE, f'{BASE_URL}?action=check_updates', li_upd, False)

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

    if sport == 'livetv':
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        from scrapers.channel_list import open_channel_list
        open_channel_list()
    elif sport == 'rugby':
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
    elif sport == 'nba':
        xbmcplugin.setPluginCategory(HANDLE, 'NBA')
        from scrapers.nba import list_events
        list_events(HANDLE, BASE_URL, sport_thumb, FANART)
    else:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


def play_stream(sport, url):
    if sport == 'rugby':
        from scrapers.rugby import play_stream
        play_stream(HANDLE, url)
    elif sport == 'ufc':
        from scrapers.ufc import play_stream
        play_stream(HANDLE, url, _param('title') or 'UFC')
    elif sport == 'boxing':
        from scrapers.boxing import play_stream
        play_stream(HANDLE, url, _param('title') or 'Boxing')
    elif sport == 'nba':
        from scrapers.nba import play_stream
        play_stream(HANDLE, url, _param('title') or 'NBA')
    elif sport == 'livetv':
        pass  # handled inside ChannelListWindow
    else:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


def check_for_updates():
    xbmcgui.Dialog().notification('Sports HQ', 'Checking for updates...', xbmcgui.NOTIFICATION_INFO, 2000)
    xbmc.executebuiltin('UpdateAddonRepos(repository.sniper)')
    xbmc.sleep(3000)
    restart = xbmcgui.Dialog().yesno(
        'Sports HQ',
        'Update check complete.[CR][CR]Restart Kodi now to apply any updates?',
        nolabel='Later',
        yeslabel='Restart Now'
    )
    if restart:
        xbmc.executebuiltin('RestartApp')
    xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


def optimise_buffer():
    path = xbmcvfs.translatePath('special://masterprofile/advancedsettings.xml')
    try:
        with xbmcvfs.File(path, 'w') as f:
            f.write(ADVANCED_SETTINGS)
        xbmcgui.Dialog().ok(
            'Sports HQ',
            'Buffer settings applied.[CR][CR]Please restart Kodi for the changes to take effect.'
        )
    except Exception as exc:
        xbmcgui.Dialog().ok('Sports HQ', f'Failed to write settings: {exc}')
    xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


def router():
    action = _param('action')
    sport  = _param('sport')
    title  = _param('title')

    if action is None:
        main_menu()
    elif action == 'optimise_buffer':
        optimise_buffer()
    elif action == 'check_updates':
        check_for_updates()
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
