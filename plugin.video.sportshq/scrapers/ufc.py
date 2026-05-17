"""
scrapers/ufc.py — UFC live streams (coming soon)
"""

import xbmcgui
import xbmcplugin


def list_events(handle, base_url, sport_thumb='', fanart=''):
    xbmcgui.Dialog().notification(
        'Sports HQ', 'UFC streams — coming soon!',
        xbmcgui.NOTIFICATION_INFO, 4000
    )
    xbmcplugin.endOfDirectory(handle, succeeded=False)
