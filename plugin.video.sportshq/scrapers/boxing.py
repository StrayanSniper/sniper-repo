"""
scrapers/boxing.py — Boxing live streams (coming soon)
"""

import xbmcgui
import xbmcplugin


def list_events(handle, base_url):
    xbmcgui.Dialog().notification(
        'Sports HQ', 'Boxing streams — coming soon!',
        xbmcgui.NOTIFICATION_INFO, 4000
    )
    xbmcplugin.endOfDirectory(handle, succeeded=False)
