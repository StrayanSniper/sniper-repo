"""
bump_version.py — increment the patch digit in addon.xml, print the new version.
Called by deploy.bat: python bump_version.py <path/to/addon.xml>
"""
import re
import sys

path = sys.argv[1]
text = open(path, encoding='utf-8').read()

def bump(m):
    major, minor, patch = m.group(1), m.group(2), int(m.group(3))
    return 'version="{}.{}.{}"'.format(major, minor, patch + 1)

new = re.sub(r'version="(\d+)\.(\d+)\.(\d+)"', bump, text, count=1)
open(path, 'w', encoding='utf-8').write(new)

m = re.search(r'version="(\d+\.\d+\.\d+)"', new)
print(m.group(1) if m else '')
