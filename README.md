# Sniper Repo — Kodi Repository

A Kodi add-on repository hosted on GitHub Pages.

## Addons included

| Addon | Description |
|-------|-------------|
| `plugin.video.rugbystreams` | Live Rugby Union & League streams from rugbybox.me |

---

## Setup — one-time steps before publishing

### 1. Replace the placeholder GitHub username

Open `repository.sniper/addon.xml` and replace every occurrence of
`YOUR_GITHUB_USERNAME` with your actual GitHub username:

```xml
<info compressed="false">https://YOUR_GITHUB_USERNAME.github.io/sniper-repo/addons.xml</info>
<checksum>https://YOUR_GITHUB_USERNAME.github.io/sniper-repo/addons.xml.md5</checksum>
<datadir zip="true">https://YOUR_GITHUB_USERNAME.github.io/sniper-repo/zips/</datadir>
```

### 2. Run the build script

```bash
cd sniper-repo
python build.py
```

This generates:
- `zips/repository.sniper/repository.sniper-1.0.0.zip`
- `zips/plugin.video.rugbystreams/plugin.video.rugbystreams-1.0.0.zip`
- `addons.xml`
- `addons.xml.md5`

### 3. Create a GitHub repository named `sniper-repo`

Push this folder to `main` (or `master`) and enable **GitHub Pages** on that branch.

```bash
git init
git add .
git commit -m "Initial Sniper Repo"
git branch -M main
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/sniper-repo.git
git push -u origin main
```

Then go to **Settings → Pages → Source → Deploy from branch → main / (root)**.

After a minute the repo will be live at:
`https://YOUR_GITHUB_USERNAME.github.io/sniper-repo/`

---

## Installing in Kodi

1. Download `zips/repository.sniper/repository.sniper-1.0.0.zip` to your device.
2. In Kodi: **Add-ons → Install from zip file** → select the zip.
3. After installing the repo, go to **Install from repository → Sniper Repo** to install individual addons.

---

## Adding a new addon to the repo

1. Copy the addon folder into the repo root.
2. Add the addon's directory name to `ADDON_DIRS` in `build.py`.
3. Run `python build.py`.
4. Commit and push.

---

## Updating an existing addon

1. Make changes to the addon files.
2. Bump the `version` attribute in the addon's `addon.xml`.
3. Run `python build.py`.
4. Commit and push.
