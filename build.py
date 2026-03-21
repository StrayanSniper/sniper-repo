#!/usr/bin/env python3
"""
build.py — Sniper Repo build script.

Run this script from the sniper-repo root whenever you add/update an addon.
It will:
  1. Zip each addon folder into zips/<addon_id>/<addon_id>-<version>.zip
  2. Regenerate addons.xml from all addon.xml files
  3. Regenerate addons.xml.md5

Usage:
    python build.py
"""

import hashlib
import os
import re
import sys
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
]

# Files/dirs to exclude when zipping
EXCLUDE_PATTERNS = {
    '__pycache__', '.git', '.gitignore',
    'test.py',
    'extractor_out.txt', 'extractor_err.txt',
    'out.txt', 'out2.txt', 'err.txt', 'err2.txt',
}


def _addon_version(addon_dir: Path) -> str:
    tree = ET.parse(addon_dir / 'addon.xml')
    return tree.getroot().attrib['version']


def _should_exclude(path: Path) -> bool:
    for part in path.parts:
        if part in EXCLUDE_PATTERNS:
            return True
    return False


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
    print('Done.')


if __name__ == '__main__':
    main()
