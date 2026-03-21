"""
test.py - standalone stream URL extractor.
Loads a rugbybox.me match page, waits for the casthill.net player to
initialise, then reads the stream URL directly from the jwplayer instance.

Usage: py test.py [url]
"""

import sys
import time

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

URL = sys.argv[1] if len(sys.argv) > 1 else \
    'https://rugbybox.me/nrl/newcastle-knights-vs-new-zealand-warriors-stream'

UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)

# Minimal injection: only block anti-debug redirects so the page stays alive.
INJECT_JS = r"""
(function () {
    try {
        window.location.assign  = function (u) {};
        window.location.replace = function (u) {};
    } catch (e) {}
})();
"""


def find_casthill_iframe(driver):
    """Return the casthill.net iframe element, or None."""
    for iframe in driver.find_elements(By.TAG_NAME, 'iframe'):
        src = iframe.get_attribute('src') or ''
        if 'casthill.net' in src:
            return iframe
    return None


def get_stream_url_from_iframe(driver, iframe):
    """Switch into the casthill iframe and read jwplayer's stream URL."""
    driver.switch_to.frame(iframe)
    try:
        result = driver.execute_script('''
            try {
                if (!window.isPlayerLoaded) return null;
                var item = window.jwplayer().getPlaylistItem();
                if (!item) return null;
                // jwplayer uses playlist[].sources[].file or top-level file
                var file = item.file;
                if (!file && item.sources && item.sources.length) {
                    file = item.sources[0].file;
                }
                return file || null;
            } catch(e) {
                return null;
            }
        ''')
        return result
    finally:
        driver.switch_to.default_content()


def main():
    print(f'Loading: {URL}')

    opts = uc.ChromeOptions()
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--mute-audio')
    opts.add_argument('--autoplay-policy=no-user-gesture-required')
    opts.add_argument('--window-size=1280,720')
    opts.add_argument(f'--user-agent={UA}')

    driver = uc.Chrome(options=opts, headless=True)

    try:
        driver.execute_cdp_cmd(
            'Page.addScriptToEvaluateOnNewDocument',
            {'source': INJECT_JS}
        )

        driver.get(URL)

        timeout = 60
        deadline = time.time() + timeout
        stream_url = None

        while time.time() < deadline:
            time.sleep(2)

            iframe = find_casthill_iframe(driver)
            if iframe:
                url = get_stream_url_from_iframe(driver, iframe)
                if url:
                    stream_url = url
                    break

            remaining = int(deadline - time.time())
            print(f'  waiting for player... ({remaining}s left)', flush=True)

        if stream_url:
            print(f'\nSTREAM URL:\n{stream_url}')
        else:
            print('\nFAILED: no stream URL found within timeout')

    finally:
        driver.quit()


if __name__ == '__main__':
    main()
