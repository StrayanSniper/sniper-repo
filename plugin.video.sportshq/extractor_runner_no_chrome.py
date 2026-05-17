"""
extractor_runner_no_chrome.py — casthill.net stream extractor for rugbybox.me.

Resolves the HLS master URL + auth headers and writes them to temp_file as:
    https://cdn.../master.m3u8?u_id=XXX|User-Agent=...&Referer=...

Kodi plays this directly via inputstream.ffmpegdirect — no local proxy needed.
"""

import re
import sys
import os
import base64
import urllib.parse

_lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib')
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

try:
    import requests as _requests
except ImportError:
    print('[extractor] ERROR: requests not found', file=sys.stderr)
    sys.exit(1)

UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)


def shutdown_proxy():
    """No-op — kept for API compatibility with callers."""
    pass


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _build_scrape_session(referer: str) -> '_requests.Session':
    sess = _requests.Session()
    sess.headers.update({
        'User-Agent':                UA,
        'Accept':                    'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language':           'en-GB,en;q=0.5',
        'Referer':                   referer,
        'Upgrade-Insecure-Requests': '1',
        'Connection':                'keep-alive',
    })
    return sess


def _fetch(session, url: str, referer: str = '', **kwargs):
    if referer:
        session.headers['Referer'] = referer
    try:
        r = session.get(url, timeout=20, allow_redirects=True, **kwargs)
        raw = r.content
        if len(raw) >= 2 and raw[0] == 0x1f and raw[1] == 0x8b:
            import gzip
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        return raw.decode('utf-8', errors='replace'), r.status_code
    except Exception as exc:
        print(f'[extractor] fetch error {url}: {exc}', file=sys.stderr, flush=True)
        return '', 0


# ---------------------------------------------------------------------------
# Casthill / boanki extraction
# ---------------------------------------------------------------------------

def _b64d(s: str) -> str:
    return base64.b64decode(s + '==').decode('utf-8', errors='replace')


def _decode_sr(char_list: list) -> str:
    return ''.join(chr(c) for c in char_list)


def _extract_embed_vars(html: str) -> dict:
    def _re(pattern, default=None):
        m = re.search(pattern, html)
        if m:
            return m.group(1)
        if default is None:
            raise ValueError(f'Pattern not found: {pattern}')
        return default

    def _desr(pattern):
        m = re.search(pattern, html)
        if not m:
            raise ValueError(f'decodeSr pattern not found: {pattern}')
        return _decode_sr([int(x) for x in m.group(1).split(',')])

    vs_raw     = _desr(r'videoSource=bota\(decodeSr\(\[([0-9,]+)\]\)\)')
    url_source = _b64d(_b64d(vs_raw))
    scode      = _desr(r'(?<![A-Za-z])sCode=decodeSr\(\[([0-9,]+)\]\)')
    player_id  = _re(r'playerId="([^"]+)"')
    edge_host  = _re(r'edgeHostId="([^"]+)"')
    expire_ts  = _re(r'expireTs=parseInt\("([^"]+)"')
    str_unq    = _re(r'strUnqId="([^"]+)"')
    csrf_raw   = _re(r'csrftoken="([^"]+)"')
    csrf_hdr   = _b64d(_b64d(csrf_raw))
    sec_url    = _b64d(_re(r'secTokenUrl=bota\("([^"]+)"\)'))

    return {
        'url_source': url_source,
        'scode':      scode,
        'player_id':  player_id,
        'edge_host':  edge_host,
        'expire_ts':  expire_ts,
        'str_unq':    str_unq,
        'csrf_hdr':   csrf_hdr,
        'sec_url':    sec_url,
    }


def _call_boanki(ev: dict, iframe_url: str, edm: str) -> str:
    qs = urllib.parse.urlencode({
        'scode':   ev['scode'],
        'stream':  ev['str_unq'],
        'expires': ev['expire_ts'],
        'u_id':    ev['player_id'],
        'host_id': ev['edge_host'],
    })
    auth_url = f"{ev['sec_url']}?{qs}"
    print('[extractor] Calling boanki.net...', file=sys.stderr, flush=True)
    ra = _requests.get(auth_url, timeout=15, headers={
        'User-Agent':  UA,
        'X-CSRF-Auth': ev['csrf_hdr'],
        'Accept':      'application/json',
        'Referer':     iframe_url,
        'Origin':      f'https://{edm}',
    })
    data = ra.json()
    if not data.get('success'):
        raise RuntimeError(f'boanki.net returned failure: {data}')
    device_id = data['device_id']
    print(f'[extractor] boanki.net OK  device_id={device_id}', file=sys.stderr, flush=True)
    return device_id


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def extract(match_url: str, temp_file: str):
    """
    Resolve the stream for match_url and write  url|headers  to temp_file.
    The caller passes this directly to inputstream.ffmpegdirect — no proxy.
    """
    print(f'[extractor] Fetching match page: {match_url}', file=sys.stderr, flush=True)
    page_sess = _build_scrape_session('https://rugbybox.me/')
    html_page, status = _fetch(page_sess, match_url)
    if not html_page or status != 200:
        raise RuntimeError(f'match page returned HTTP {status}')

    # Extract casthill embed config; fall back to stream sub-pages if needed
    zmid_m = re.search(r'zmid\s*=\s*"([^"]+)"', html_page)
    pid_m  = re.search(r'\bpid\s*=\s*(\d+)', html_page)
    edm_m  = re.search(r'edm\s*=\s*"([^"]+)"', html_page)

    if not (zmid_m and pid_m and edm_m):
        sub_links  = re.findall(r'href="(/[^"]+/stream-\d+)"', html_page)
        sub_links += re.findall(r"href='(/[^']+/stream-\d+)'", html_page)
        found = False
        for sub in sub_links:
            sub_url = 'https://rugbybox.me' + sub
            print(f'[extractor] Trying sub-page: {sub_url}', file=sys.stderr, flush=True)
            sub_html, sub_status = _fetch(page_sess, sub_url)
            if sub_html and sub_status == 200:
                zm = re.search(r'zmid\s*=\s*"([^"]+)"', sub_html)
                pm = re.search(r'\bpid\s*=\s*(\d+)', sub_html)
                em = re.search(r'edm\s*=\s*"([^"]+)"', sub_html)
                if zm and pm and em:
                    html_page = sub_html
                    zmid_m, pid_m, edm_m = zm, pm, em
                    found = True
                    break
        if not found:
            raise RuntimeError('casthill embed config not found in match page or sub-pages')

    zmid     = zmid_m.group(1)
    pid      = pid_m.group(1)
    edm      = edm_m.group(1)
    cat_m    = re.search(r'gameCat\s*=\s*"([^"]+)"', html_page)
    txt_m    = re.search(r'gameText\s*=\s*"([^"]+)"', html_page)
    game_cat = cat_m.group(1) if cat_m else 'sp'
    game_txt = txt_m.group(1) if txt_m else ''
    csrf_m   = re.search(r'"csrf"\s*:\s*"([^"]+)"', html_page)
    csrfip_m = re.search(r'"csrf_ip"\s*:\s*"([^"]+)"', html_page)
    if not csrf_m or not csrfip_m:
        raise RuntimeError('CSRF tokens not found in siteConfig')

    embed_qs = urllib.parse.urlencode({
        'pid':     pid,
        'gacat':   game_txt,
        'gatxt':   game_cat,
        'v':       zmid,
        'csrf':    csrf_m.group(1),
        'csrf_ip': csrfip_m.group(1),
    })
    iframe_url = f'https://{edm}/sd0embed/{game_cat}?{embed_qs}'
    print(f'[extractor] Fetching embed: {iframe_url[:80]}...', file=sys.stderr, flush=True)

    embed_sess = _build_scrape_session(match_url)
    html_embed, status = _fetch(embed_sess, iframe_url, referer=match_url)
    if not html_embed or status != 200:
        raise RuntimeError(f'embed page returned HTTP {status}')

    ev = _extract_embed_vars(html_embed)
    print(f'[extractor] urlSource: {ev["url_source"][:80]}', file=sys.stderr, flush=True)

    device_id  = _call_boanki(ev, iframe_url, edm)
    stream_url = ev['url_source'] + f'?u_id={device_id}'

    headers = urllib.parse.urlencode({
        'User-Agent': UA,
        'Referer':    iframe_url,
        'Origin':     f'https://{edm}',
    })
    full_url = f'{stream_url}|{headers}'

    print('[extractor] Stream URL ready', file=sys.stderr, flush=True)
    with open(temp_file, 'w') as f:
        f.write(full_url)
