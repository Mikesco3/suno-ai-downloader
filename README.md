# Suno Music Downloader

A Python tool that downloads your own music from [Suno](https://suno.com) using a
real Chromium browser via [Playwright](https://playwright.dev/python/). This lets
it work with Suno's current AWS CloudFront signed-cookie and encryption
protection, which blocks plain HTTP downloads.

**Version:** 2.0  
**License:** [GNU General Public License v3.0](LICENSE)

> This tool is for downloading music you have created or have rights to access
> on Suno. It is not intended for redistributing others' work.

## How it works

1. Launches a visible Chromium browser with a **persistent profile** (`browser_profile/`).
2. Opens `https://suno.com/me`.
3. On the first run, you log in manually inside the browser. The session is saved
   for future runs.
4. Calls Suno's authenticated `feed/v3` API to retrieve **your complete song
   library** (paginated), bypassing the limited DOM rendering.
5. For each song, extracts the encrypted streaming URL from Suno's API data and
   routes it through Suno's own service worker passthrough. The service worker
   decrypts the audio on the fly, returning a playable MP4 (Opus) file.
6. Saves each track as `songs/<title>-id-<hash>.mp4` (or `.mp3`) with a
   matching `.txt` file containing the original UUID and prompt.

This approach uses Suno's **streaming path**, so it does not hit the Download
button's rate limits.

## Setup

1. Clone this repository.
2. (Recommended) Use a virtual environment so Playwright's dependencies do not
   conflict with your system Python packages. On Arch / Manjaro this also avoids
   the `externally-managed-environment` errors you can get with `pip3`:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
3. Install the Python requirements:
   ```bash
   pip install -r requirements.txt
   ```
4. Install the Playwright Chromium browser binary:
   ```bash
   playwright install chromium
   ```

> **MP3 output only:** if you want `.mp3` files, make sure `ffmpeg` is installed
> (`sudo pacman -S ffmpeg` on Arch/Manjaro, `sudo apt install ffmpeg` on Debian/Ubuntu,
> or `brew install ffmpeg` on macOS). MP4 output does not need ffmpeg.

## Usage

Run the downloader (default output is MP4):

```bash
python3 suno-downloader.py
```

To save every song as an MP3 instead, pass `mp3` as an argument:

```bash
python3 suno-downloader.py mp3
```

Add `--debug` to save the HTML of any song page where an audio URL cannot be found:

```bash
python3 suno-downloader.py --debug
python3 suno-downloader.py --debug mp3
```

You can also change the default format by editing the `DEFAULT_OUTPUT_FORMAT`
constant near the top of `suno-downloader.py`.

### First run

A browser window will open. Log in to Suno normally. Once you can see your
library at `https://suno.com/me`, press `Enter` in the terminal. The script
will remember your login in `browser_profile/`.

### Subsequent runs

The script will reuse the saved profile and start downloading automatically.

## Output structure

```
songs/
  ├── song-name-id-xxxxx.mp4  # Decrypted audio (Opus in MP4 container, default)
  ├── song-name-id-xxxxx.mp3  # Same audio transcoded to MP3 (if requested)
  └── song-name-id-xxxxx.txt  # Original UUID + generation prompt
```

## Features

- **Persistent browser session** — log in once, reuse later.
- **Complete library discovery** — uses Suno's `feed/v3` API with pagination
  instead of relying on the page's lazy-loaded DOM.
- **Bypasses download limits** — uses Suno's streaming/service-worker path
  instead of the limited Download button.
- **On-the-fly decryption** — Suno's service worker decrypts the encrypted
  CloudFront stream, producing a playable MP4.
- **CloudFront-cookie aware** — requests use the browser's authenticated context,
  avoiding `403 Forbidden` errors.
- **Resumable** — skips songs whose audio and `.txt` files already exist.
- **Optional MP3 output** — transcodes the decrypted MP4 stream to MP3 with
  ffmpeg when you pass `mp3` on the command line (default remains MP4).
- **Filename sanitization** — special characters are stripped for safe Linux paths.
- **Network interception** — captures Suno's own authenticated API responses on
  `/me` to grab media URLs directly.

## Files

- `suno-downloader.py` — main Playwright-based downloader.
- `requirements.txt` — Python package dependencies.
- `browser_profile/` — persistent Chromium profile (created automatically).
- `songs/` — downloaded MP4s and prompt text files.

## Troubleshooting

- **`pip install` fails with an externally-managed-environment error** (common on
  Arch / Manjaro): create and activate a virtual environment first:
  ```bash
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt
  playwright install chromium
  ```
- **Browser does not open / `playwright` not found**: run `playwright install chromium`.
- **Login prompt every run**: make sure `browser_profile/` is writable and not
  deleted between runs.
- **`mp3` conversion fails / `ffmpeg` not found**: install ffmpeg for your distro
  (`sudo pacman -S ffmpeg`, `sudo apt install ffmpeg`, `brew install ffmpeg`, etc.).
- **Some songs fail to download**: Suno's DOM/API changes over time. The script
  includes several fallback strategies, including direct API calls and song-page
  scraping, but if Suno changes their encryption or service worker format the
  passthrough URL may need to be updated.

## License

This project is licensed under the GNU General Public License v3.0 — see the
[LICENSE](LICENSE) file for details.

## Changelog

### v2.0

- Replaced the broken `getData.js` browser-console workflow with a fully
  automated Playwright-based downloader.
- Added persistent Chromium profile support so you only need to log in once.
- Switched library discovery from DOM scrolling to Suno's authenticated
  `feed/v3` API, reliably retrieving the complete library (e.g. 100+ songs
  across multiple pages).
- Added on-the-fly decryption via Suno's `/_sw-mango/passthrough` service worker
  endpoint, producing playable MP4 files instead of encrypted `.m4a` blobs.
- Removed dependency on `tqdm`.
- Added `--debug` flag for troubleshooting extraction failures.
- Added optional MP3 output via a command-line argument (`python3 suno-downloader.py mp3`).
