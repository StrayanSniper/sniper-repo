#!/usr/bin/env python3
"""
build.py — Sniper Repo build script.

Run this script from the sniper-repo root whenever you add/update an addon.
It will:
  1. Zip each addon folder into zips/<addon_id>/<addon_id>-<version>.zip
  2. Regenerate addons.xml from all addon.xml files
  3. Regenerate addons.xml.md5
  4. Regenerate index.html files for root, zips/, and each zips/<addon_id>/

Usage:
    python build.py
"""

import datetime
import hashlib
import html
import sys
import urllib.parse
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

REPO_ROOT = Path(__file__).parent.resolve()
ZIPS_DIR  = REPO_ROOT / 'zips'

# Addon directories to package (relative to repo root).
# Add new addon IDs here when you add them to the repo.
ADDON_DIRS = [
    'repository.sniper',
    'plugin.video.rugbystreams',
    'plugin.video.sportshq',
    'script.module.jetextractors',
    'script.module.pyjsparser',
    'script.module.pyamf',
    'script.module.pytz',
]

# Files/dirs to exclude when zipping
EXCLUDE_PATTERNS = {
    '__pycache__', '.git', '.gitignore',
    'test.py',
    'extractor_out.txt', 'extractor_err.txt',
    'out.txt', 'out2.txt', 'err.txt', 'err2.txt',
}

# File extensions to exclude (platform-specific compiled files)
EXCLUDE_EXTENSIONS = {'.pyd', '.exe', '.pdb'}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _addon_version(addon_dir: Path) -> str:
    tree = ET.parse(addon_dir / 'addon.xml')
    return tree.getroot().attrib['version']


def _should_exclude(path: Path) -> bool:
    for part in path.parts:
        if part in EXCLUDE_PATTERNS:
            return True
    if path.suffix.lower() in EXCLUDE_EXTENSIONS:
        return True
    return False


def _fmt_size(n: int) -> str:
    return str(n)


def _fmt_date(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%d-%b-%Y %H:%M')


def _index_html(title: str, parent_href: str | None, entries: list[tuple[str, str, str, str]]) -> str:
    """
    Build an Apache-style index page.

    entries: list of (href, display_name, date_str, size_str)
             size_str='' for directories
    """
    lines = []
    lines.append('<!DOCTYPE html>')
    lines.append('<html>')
    lines.append('<head>')
    lines.append(f'  <title>Index of {html.escape(title)}</title>')
    lines.append('</head>')
    lines.append('<body>')
    lines.append(f'<h1>Index of {html.escape(title)}</h1>')
    lines.append('<hr>')
    lines.append('<pre>')
    if parent_href is not None:
        lines.append(f'<a href="{parent_href}">../</a>')
    for href, name, date, size in entries:
        encoded_href = urllib.parse.quote(href, safe='./-')
        display = html.escape(name)
        # pad name column to 55 chars
        padded = f'<a href="{encoded_href}">{display}</a>'
        # calculate visible length (name only, no tags)
        vis_len = len(name)
        pad = max(1, 55 - vis_len)
        lines.append(f'{padded}{" " * pad}{date}  {size}')
    lines.append('</pre>')
    lines.append('<hr>')
    lines.append('</body>')
    lines.append('</html>')
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# Build steps
# ---------------------------------------------------------------------------

def build_zip(addon_dir: Path) -> Path:
    addon_id = addon_dir.name
    version  = _addon_version(addon_dir)
    zip_dir  = ZIPS_DIR / addon_id
    zip_dir.mkdir(parents=True, exist_ok=True)
    zip_path = zip_dir / f'{addon_id}-{version}.zip'

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(addon_dir.rglob('*')):
            rel = file_path.relative_to(addon_dir.parent)
            if _should_exclude(rel):
                continue
            if file_path.is_file():
                zf.write(file_path, rel)

    print(f'  zipped  {zip_path.relative_to(REPO_ROOT)}')
    return zip_path


def build_addons_xml():
    root_el = ET.Element('addons')

    for addon_id in ADDON_DIRS:
        addon_dir = REPO_ROOT / addon_id
        if not (addon_dir / 'addon.xml').exists():
            print(f'  WARN: no addon.xml in {addon_dir}', file=sys.stderr)
            continue
        tree = ET.parse(addon_dir / 'addon.xml')
        root_el.append(tree.getroot())

    ET.indent(root_el, space='  ')
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root_el, encoding='utf-8', xml_declaration=False)

    addons_xml = REPO_ROOT / 'addons.xml'
    addons_xml.write_bytes(xml_bytes)
    print(f'  wrote   addons.xml  ({len(xml_bytes)} bytes)')

    md5 = hashlib.md5(xml_bytes).hexdigest()
    (REPO_ROOT / 'addons.xml.md5').write_text(md5)
    print(f'  wrote   addons.xml.md5  ({md5})')


def build_indexes():
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

    # --- root index.html ---
    root_entries = []
    # addons.xml
    p = REPO_ROOT / 'addons.xml'
    if p.exists():
        s = p.stat()
        root_entries.append(('addons.xml', 'addons.xml', _fmt_date(s.st_mtime), _fmt_size(s.st_size)))
    # addons.xml.md5
    p = REPO_ROOT / 'addons.xml.md5'
    if p.exists():
        s = p.stat()
        root_entries.append(('addons.xml.md5', 'addons.xml.md5', _fmt_date(s.st_mtime), _fmt_size(s.st_size)))
    # zips/ dir link
    root_entries.append(('zips/', 'zips/', _fmt_date(now_ts), ''))

    (REPO_ROOT / 'index.html').write_text(
        _index_html('/sniper-repo/', None, root_entries),
        encoding='utf-8'
    )
    print('  wrote   index.html')

    # --- zips/ index.html ---
    zips_entries = []
    for addon_id in ADDON_DIRS:
        zips_entries.append((f'{addon_id}/', f'{addon_id}/', _fmt_date(now_ts), ''))

    (ZIPS_DIR / 'index.html').write_text(
        _index_html('/sniper-repo/zips/', '../', zips_entries),
        encoding='utf-8'
    )
    print('  wrote   zips/index.html')

    # --- zips/<addon_id>/ index.html ---
    for addon_id in ADDON_DIRS:
        subdir = ZIPS_DIR / addon_id
        if not subdir.is_dir():
            continue
        entries = []
        for zp in sorted(subdir.glob('*.zip')):
            s = zp.stat()
            entries.append((zp.name, zp.name, _fmt_date(s.st_mtime), _fmt_size(s.st_size)))
        (subdir / 'index.html').write_text(
            _index_html(f'/sniper-repo/zips/{addon_id}/', '../', entries),
            encoding='utf-8'
        )
        print(f'  wrote   zips/{addon_id}/index.html')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print('Building Sniper Repo...')
    for addon_id in ADDON_DIRS:
        addon_dir = REPO_ROOT / addon_id
        if not addon_dir.is_dir():
            print(f'  SKIP: {addon_id} directory not found')
            continue
        build_zip(addon_dir)

    print('Generating addons.xml / addons.xml.md5...')
    build_addons_xml()

    print('Generating index.html files...')
    build_indexes()

    print('Done.')


if __name__ == '__main__':
    main()
