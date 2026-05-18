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

_VERSION = ADDON.getAddonInfo('version')

SPORTS = [
    ('livetv',  'Live TV & Streams',    _img('live_tv.png') if xbmcvfs.exists(_img('live_tv.png')) else ICON),
    ('rugby',   'Rugby Union / League', _img('rugby.png')),
    ('ufc',     'UFC',                  _img('ufc.png')),
    ('boxing',  'Boxing',               _img('boxing.png')),
    ('nba',     'NBA',                  _img('nba.png')),
    ('tools',   'Tools',                _img('tools.png') if xbmcvfs.exists(_img('tools.png')) else ICON),
    ('version', f'Version\n[B]{_VERSION}[/B]', _img('blank.png')),
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

    if sport == 'version':
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
    elif sport == 'tools':
        list_tools()
    elif sport == 'livetv':
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
        list_events(HANDLE, BASE_URL, _img('nba.png'), FANART)
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
        play_stream(HANDLE, urllib.parse.unquote(url) if url else '', _param('title') or 'NBA')
    elif sport == 'livetv':
        pass  # handled inside ChannelListWindow
    else:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


def list_tools():
    xbmcplugin.setPluginCategory(HANDLE, 'Tools')
    xbmcplugin.setContent(HANDLE, 'files')

    tools = [
        ('optimise_buffer', '⚙ Optimise Buffering',
         'Write advancedsettings.xml to maximise stream buffer. Run once then restart Kodi.'),
        ('check_updates',   '↻ Check for Updates',
         'Check Sniper Repo for addon updates and restart Kodi to apply.'),
    ]

    for action, label, desc in tools:
        li = xbmcgui.ListItem(label=f'[B]{label}[/B]')
        li.setArt({'thumb': ICON, 'icon': ICON, 'fanart': FANART})
        li.setInfo('video', {'title': label, 'plot': desc, 'mediatype': 'video'})
        xbmcplugin.addDirectoryItem(HANDLE, f'{BASE_URL}?action={action}', li, False)

    xbmcplugin.endOfDirectory(HANDLE)


def set_autostart():
    path = xbmcvfs.translatePath('special://userdata/autoexec.py')
    content = (
        'import xbmc\n'
        'xbmc.sleep(4000)\n'
        "xbmc.executebuiltin('RunAddon(plugin.video.sportshq)')\n"
    )
    try:
        with xbmcvfs.File(path, 'w') as f:
            f.write(content)
        xbmcgui.Dialog().ok(
            'Sports HQ',
            'Auto-start set.[CR][CR]Kodi will now open Sports HQ automatically on every launch.[CR][CR]Restart Kodi for the change to take effect.'
        )
    except Exception as exc:
        xbmcgui.Dialog().ok('Sports HQ', f'Failed to write autoexec.py: {exc}')
    xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


def check_for_updates():
    xbmcgui.Dialog().notification('Sports HQ', 'Clearing cache and checking for updates...', xbmcgui.NOTIFICATION_INFO, 2000)
    xbmc.executebuiltin('CleanLibrary(video)')
    xbmc.executebuiltin('UpdateAddonRepos(repository.sniper)')
    # Clear thumbnail cache recursively
    import shutil
    thumb_path = xbmcvfs.translatePath('special://userdata/Thumbnails/')
    try:
        shutil.rmtree(thumb_path, ignore_errors=True)
    except Exception:
        pass
    xbmc.sleep(2000)
    restart = xbmcgui.Dialog().yesno(
        'Sports HQ',
        'Cache cleared and update check complete.[CR][CR]Restart Kodi now to apply any updates?',
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
    elif action == 'set_autostart':
        set_autostart()
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
