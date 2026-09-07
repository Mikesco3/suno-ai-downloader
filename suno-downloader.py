#!/usr/bin/env python3
"""
Suno Music Downloader v2.0

A tool for downloading your own music from Suno (https://suno.com).

Copyright (C) 2026  mikesco3

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

Uses a real Chromium browser via Playwright to work around Suno's
CloudFront signed-cookie and encryption protection. A persistent browser
profile lets you log in once and reuse the session on later runs.

Flow:
1. Launch a visible Chromium browser with a persistent context.
2. Navigate to https://suno.com/me.
3. If not logged in, prompt for manual login in the browser window.
4. Call Suno's authenticated feed/v3 API to retrieve the complete library.
5. For each song, get the encrypted media URL from Suno's API data and
   route the request through Suno's service worker passthrough.
   The service worker decrypts the audio on the fly, returning a playable
   MP4 (Opus) file without hitting Suno's download limits.
6. Save each track as songs/<sanitized-title>-id-<hash>.mp4 with a
   matching .txt file containing the prompt/UUID.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from playwright.sync_api import Page, Response, TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
VERSION = "2.0"

# ---------------------------------------------------------------------------
# User-configurable defaults
# ---------------------------------------------------------------------------
# Change this to "mp3" if you always want MP3 output without passing an argument.
DEFAULT_OUTPUT_FORMAT = "mp4"  # "mp4" or "mp3"

SUNO_ME_URL = "https://suno.com/me"
SUNO_SONG_URL = "https://suno.com/song/{}"
SUNO_SW_PASSTHROUGH = "https://suno.com/_sw-mango/passthrough"
PROFILE_DIR = Path(__file__).with_name("browser_profile")
OUTPUT_DIR = Path(__file__).with_name("songs")

LOGIN_TIMEOUT_MS = 30_000          # 30 s to detect an already-logged-in session
FIRST_LOGIN_TIMEOUT_MS = 600_000   # 10 m for a first-time manual login
SCROLL_PAUSE_SECONDS = 2.0
MAX_SCROLL_ATTEMPTS = 200
MAX_DOWNLOAD_RETRIES = 3
SW_READY_TIMEOUT_MS = 20_000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_filename(name: str, max_length: int = 120) -> str:
    """Make a string safe to use as a Linux filename."""
    if not name:
        name = "untitled"
    # Collapse whitespace, lowercase, replace spaces with dashes (matches old style)
    name = re.sub(r"\s+", "-", name.strip().lower())
    # Strip characters that are illegal or annoying in filenames
    name = re.sub(r'[/<>:"|?\\\x00-\x1f]', "", name)
    # Remove leading dots/dashes and trailing dots/spaces
    name = name.strip(".- ")
    # Avoid empty result
    if not name:
        name = "untitled"
    return name[:max_length]


def build_output_name(title: str, song_id: str) -> str:
    """Follow the historical filename convention: title-id-xxxxx."""
    short_id = song_id[:5] if len(song_id) >= 5 else song_id
    safe_title = sanitize_filename(title)
    return f"{safe_title}-id-{short_id}"


def find_key_recursive(obj: Any, key: str) -> Any:
    """Recursively search a dict/list structure for the first occurrence of key."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            result = find_key_recursive(v, key)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = find_key_recursive(item, key)
            if result is not None:
                return result
    return None


def is_audio_url(url: str) -> bool:
    """Heuristic check that a URL looks like a Suno audio file."""
    if not url or not isinstance(url, str):
        return False
    parsed = urlparse(url)
    domain_ok = parsed.netloc.endswith((".suno.ai", ".cloudfront.net"))
    path_ok = parsed.path.lower().endswith((".mp3", ".wav", ".m4a", ".ogg", ".flac", ".mp4"))
    return domain_ok and path_ok


def audio_extension_from_url(url: str) -> str:
    """Return the file extension from a Suno audio URL (default .mp4)."""
    if not url:
        return ".mp4"
    path = urlparse(url).path.lower()
    for ext in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".mp4"):
        if path.endswith(ext):
            return ext
    return ".mp4"


def build_sw_passthrough_url(song_id: str, encrypted_url: str) -> str:
    """Build the service worker URL that decrypts a Suno media file."""
    return (
        f"{SUNO_SW_PASSTHROUGH}"
        f"?contentId={song_id}"
        f"&src={quote(encrypted_url, safe=':/')}"
        f"&contentType=clip"
    )


def convert_to_mp3(input_path: Path, output_path: Path) -> bool:
    """Transcode an MP4/Opus file to MP3 using ffmpeg."""
    if not shutil.which("ffmpeg"):
        print(
            "  MP3 conversion requested but ffmpeg was not found on PATH. "
            "Install ffmpeg and try again (e.g. `sudo pacman -S ffmpeg`)."
        )
        return False

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-q:a",
        "0",
        "-map_metadata",
        "-1",
        str(output_path),
    ]
    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return output_path.exists() and output_path.stat().st_size > 0
    except subprocess.CalledProcessError as exc:
        print(f"  ffmpeg conversion failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Network interception: capture Suno's own API responses
# ---------------------------------------------------------------------------

class NetworkCatcher:
    """Listen to API responses and extract song metadata Suno sends as JSON."""

    def __init__(self) -> None:
        self.songs: dict[str, dict[str, Any]] = {}

    def _pick_audio_url(self, obj: dict) -> str | None:
        """Choose the best audio URL from a song-like dict."""
        # Modern Suno puts the real file in media_urls; audio_url is often a placeholder.
        media_urls = obj.get("media_urls")
        if isinstance(media_urls, list):
            for media in media_urls:
                if isinstance(media, dict) and media.get("url"):
                    return media["url"]
        return (
            obj.get("audio_url")
            or obj.get("source_audio_url")
            or obj.get("stream_url")
            or obj.get("video_url")
        )

    def _extract_songs(self, obj: Any) -> None:
        """Recursively walk JSON and record objects that look like Suno songs."""
        if isinstance(obj, dict):
            obj_id = obj.get("id") or obj.get("clip_id") or obj.get("song_id")
            if isinstance(obj_id, str):
                audio_url = self._pick_audio_url(obj)
                if audio_url and is_audio_url(audio_url):
                    prompt = (
                        find_key_recursive(obj, "gpt_description_prompt")
                        or find_key_recursive(obj, "metadata_prompt")
                        or find_key_recursive(obj, "prompt")
                        or find_key_recursive(obj, "description")
                    )
                    title = (
                        find_key_recursive(obj, "title")
                        or find_key_recursive(obj, "display_title")
                        or obj_id
                    )
                    if obj_id not in self.songs:
                        self.songs[obj_id] = {
                            "id": obj_id,
                            "title": str(title) if title else obj_id,
                            "audio_url": str(audio_url),
                            "prompt": str(prompt) if prompt else "",
                            "uuid": obj_id,
                        }
            # Keep walking regardless, in case songs are nested deeper.
            for v in obj.values():
                self._extract_songs(v)
        elif isinstance(obj, list):
            for item in obj:
                self._extract_songs(item)

    def handle_response(self, response: Response) -> None:
        """Playwright response handler."""
        try:
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            # Only care about Suno API calls (skip static assets/CDN)
            if not urlparse(response.url).netloc.endswith(("suno.com", "suno.ai")):
                return
            body = response.body()
            if not body:
                return
            data = response.json()
            self._extract_songs(data)
        except Exception:
            # Most responses are not the ones we want; ignore parse failures.
            pass


# ---------------------------------------------------------------------------
# Browser / page helpers
# ---------------------------------------------------------------------------

def wait_for_service_worker(page: Page) -> None:
    """Ensure Suno's service worker is activated (required for decryption)."""
    print("Waiting for Suno service worker to be ready...")
    try:
        page.wait_for_function(
            """() => {
                if (!('serviceWorker' in navigator)) return false;
                return navigator.serviceWorker.ready.then(r => r.active?.state === 'activated');
            }""",
            timeout=SW_READY_TIMEOUT_MS,
        )
        print("Service worker is active.\n")
    except PWTimeout:
        print("Warning: service worker did not report ready; continuing anyway.\n")


def ensure_logged_in(page: Page) -> None:
    """Navigate to /me and block until the user's library is visible."""
    print("Navigating to https://suno.com/me ...")
    page.goto(SUNO_ME_URL, wait_until="domcontentloaded")

    try:
        page.wait_for_selector('a[href*="/song/"]', timeout=LOGIN_TIMEOUT_MS)
        print("Library detected — session is logged in.\n")
        wait_for_service_worker(page)
        return
    except PWTimeout:
        pass

    print(
        "\nCould not detect a logged-in library. "
        "Please log in to Suno using the browser window that just opened."
    )
    print("Once you are on https://suno.com/me and can see your songs, press ENTER here.")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        print("\nAborting.")
        sys.exit(1)

    page.goto(SUNO_ME_URL, wait_until="domcontentloaded")
    page.wait_for_selector('a[href*="/song/"]', timeout=FIRST_LOGIN_TIMEOUT_MS)
    print("Library detected — continuing.\n")
    wait_for_service_worker(page)


def _count_song_links(page: Page) -> int:
    """Count unique /song/<id> links currently in the DOM."""
    return page.evaluate(
        """() => {
            const links = document.querySelectorAll('a[href*="/song/"]');
            return new Set(Array.from(links).map(a => {
                const m = (a.getAttribute('href') || '').match(/\\/song\\/([^/?#]+)/);
                return m ? m[1] : null;
            }).filter(Boolean)).size;
        }"""
    )


def scroll_to_bottom(page: Page, catcher: NetworkCatcher | None = None) -> None:
    """Scroll /me until no new song cards appear for several attempts."""
    print("Scrolling library to load every song card...")

    max_no_growth_attempts = 5
    no_growth_count = 0
    total_attempts = 0
    last_count = _count_song_links(page)

    while total_attempts < MAX_SCROLL_ATTEMPTS and no_growth_count < max_no_growth_attempts:
        # Scroll down by most of the viewport in small hops, which is more
        # reliable for virtualized/paginated grids than one big jump.
        page.evaluate("""() => {
            const scrollAmount = Math.max(window.innerHeight * 0.75, 800);
            window.scrollBy(0, scrollAmount);
        }""")
        time.sleep(SCROLL_PAUSE_SECONDS)

        new_count = _count_song_links(page)
        if new_count > last_count:
            last_count = new_count
            no_growth_count = 0
        else:
            no_growth_count += 1

        total_attempts += 1

    # One final big scroll to the absolute bottom and a pause for in-flight calls.
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(3.0)

    final_count = _count_song_links(page)
    print(f"Finished scrolling after {total_attempts} pass(es).")
    print(f"Total unique song links found: {final_count}\n")
    if catcher:
        print(f"Network interceptor captured {len(catcher.songs)} song(s) from API responses.\n")


def _get_session_token(page: Page) -> str | None:
    """Return the Suno __session JWT cookie value."""
    for cookie in page.context.cookies("https://suno.com"):
        if cookie["name"] == "__session":
            return cookie["value"]
    return None


def fetch_all_songs_via_api(page: Page) -> list[dict[str, Any]]:
    """Paginate through Suno's feed/v3 API to get the full library."""
    print("Fetching complete song library from Suno API...")
    token = _get_session_token(page)
    if not token:
        raise RuntimeError("Could not find Suno __session cookie; cannot call feed/v3 API.")

    all_clips: list[dict[str, Any]] = []
    cursor: str | None = None
    page_num = 0

    while page_num < 20:
        page_num += 1
        body = {
            "cursor": cursor,
            "limit": 100,
            "filters": {
                "disliked": "False",
                "trashed": "False",
                "fromStudioProject": {"presence": "False"},
                "stem": {"presence": "False"},
                "stemComplement": "False",
                "user": {"presence": "True"},
            },
        }

        result = page.evaluate(
            """async ({url, token, body}) => {
                const res = await fetch(url, {
                    method: 'POST',
                    headers: {
                        'Authorization': 'Bearer ' + token,
                        'Content-Type': 'application/json'
                    },
                    credentials: 'include',
                    body: JSON.stringify(body)
                });
                const text = await res.text();
                try {
                    return { status: res.status, data: JSON.parse(text) };
                } catch (e) {
                    return { status: res.status, text: text.slice(0, 500) };
                }
            }""",
            {
                "url": "https://studio-api-prod.suno.com/api/feed/v3",
                "token": token,
                "body": body,
            },
        )

        if result.get("status") != 200:
            raise RuntimeError(
                f"feed/v3 returned HTTP {result.get('status')}: {result.get('text', result)}"
            )

        clips = result["data"].get("clips", [])
        if not clips:
            break

        all_clips.extend(clips)

        # Next cursor is either explicit metadata or the last clip's id.
        metadata = result["data"].get("metadata", {})
        next_cursor = metadata.get("next_cursor") or clips[-1].get("id")
        if next_cursor == cursor or not next_cursor:
            break
        cursor = next_cursor
        time.sleep(0.3)

    print(f"API returned {len(all_clips)} song(s).\n")
    return all_clips


def extract_library_songs(page: Page) -> list[dict[str, str]]:
    """Return unique {id, title} dicts for every song link in the library.

    Falls back to DOM scraping if the feed/v3 API fails.
    """
    try:
        clips = fetch_all_songs_via_api(page)
        return [
            {
                "id": clip["id"],
                "title": (clip.get("title") or clip["id"]).replace(r"\s+", " ").strip(),
            }
            for clip in clips
        ]
    except Exception as exc:  # noqa: BLE001
        print(f"API library fetch failed ({exc}); falling back to DOM scrolling.")

    print("Extracting song IDs and titles from the library...")

    data = page.evaluate(
        """() => {
            const links = Array.from(document.querySelectorAll('a[href*="/song/"]'));
            const songs = [];
            const seen = new Set();
            for (const a of links) {
                const href = a.getAttribute('href') || '';
                const match = href.match(/\\/song\\/([^/?#]+)/);
                if (!match) continue;
                const id = match[1];
                if (seen.has(id)) continue;
                seen.add(id);
                let title = (
                    a.getAttribute('title') ||
                    a.getAttribute('aria-label') ||
                    a.textContent
                ).trim();
                if (!title) {
                    const card = a.closest('div[class*="group"], div[class*="card"], article, [data-clip-id]') || a.parentElement;
                    title = (
                        card?.querySelector('h1, h2, h3, h4, [data-testid="song-title"]')?.textContent ||
                        card?.querySelector('img[alt]')?.getAttribute('alt') ||
                        id
                    );
                }
                songs.push({ id, title: title.replace(/\\s+/g, ' ').trim() || id });
            }
            return songs;
        }"""
    )

    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for song in data:
        sid = song["id"]
        if sid not in seen:
            seen.add(sid)
            unique.append(song)

    print(f"Found {len(unique)} unique song(s).\n")
    return unique


# ---------------------------------------------------------------------------
# Song detail extraction
# ---------------------------------------------------------------------------

def _js_extract_encrypted_audio_url() -> str | None:
    """JavaScript snippet run on a song page to fish out the encrypted media URL."""
    return """
    () => {
        const candidates = [];
        const add = (u) => { if (u && typeof u === 'string') candidates.push(u); };

        // 1. Walk Next.js / React hydrated data looking for media_urls first.
        //    audio_url is a forbidden placeholder; media_urls holds the real (encrypted) CDN URL.
        const walk = (obj) => {
            if (!obj || typeof obj !== 'object') return;
            if (Array.isArray(obj.media_urls)) {
                for (const media of obj.media_urls) {
                    if (media && media.url) add(media.url);
                }
            }
            if (obj.audio_url) add(obj.audio_url);
            if (obj.source_audio_url) add(obj.source_audio_url);
            if (obj.stream_url) add(obj.stream_url);
            if (obj.video_url) add(obj.video_url);
            for (const v of Object.values(obj)) walk(v);
        };
        if (window.__NEXT_DATA__) walk(window.__NEXT_DATA__);

        // 2. Any inline <script> tag containing a Suno media URL string
        const urlRe = /https:\\/\\/[^"'\\s]+\\.(mp3|wav|m4a|ogg|flac|mp4)/gi;
        for (const script of document.querySelectorAll('script')) {
            const text = script.textContent || '';
            let m;
            while ((m = urlRe.exec(text)) !== null) add(m[0]);
        }

        // Prefer CloudFront media URLs (the encrypted ones we want for SW passthrough)
        const isMediaHost = u => u.includes('.cloudfront.net') || u.includes('.suno.ai');
        const good = candidates.filter(isMediaHost);
        const m4a = good.find(u => u.toLowerCase().endsWith('.m4a'));
        const mp4 = good.find(u => u.toLowerCase().endsWith('.mp4'));
        const mp3 = good.find(u => u.toLowerCase().endsWith('.mp3'));
        return m4a || mp4 || mp3 || good[0] || candidates[0] || null;
    }
    """


def _js_extract_prompt() -> str | None:
    """JavaScript snippet run on a song page to fish out the prompt/UUID."""
    return """
    () => {
        const result = { title: '', prompt: '', uuid: '', tags: '' };

        if (window.__NEXT_DATA__) {
            const walk = (obj) => {
                if (!obj || typeof obj !== 'object') return;
                if (obj.title && !result.title) result.title = obj.title;
                if (obj.id && !result.uuid) result.uuid = obj.id;
                if (obj.metadata_prompt && !result.prompt) result.prompt = obj.metadata_prompt;
                if (obj.prompt && !result.prompt) result.prompt = obj.prompt;
                if (obj.gpt_description_prompt && !result.prompt) result.prompt = obj.gpt_description_prompt;
                if (obj.tags && !result.tags) result.tags = obj.tags;
                for (const v of Object.values(obj)) walk(v);
            };
            walk(window.__NEXT_DATA__);
        }

        // Fallback: visible description spans
        if (!result.prompt) {
            const spans = Array.from(document.querySelectorAll('span[title]'));
            const long = spans.find(s => (s.textContent || '').trim().length > 30);
            if (long) result.prompt = long.getAttribute('title') || long.textContent;
        }

        return result;
    }
    """


def get_song_details(
    page: Page,
    song_id: str,
    cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str | None]:
    """Return {audio_url, title, prompt, uuid} for a song.

    Uses cached network data if available, otherwise scrapes the song page.
    The returned audio_url is the *decrypted* service-worker passthrough URL.
    """
    if cache and song_id in cache:
        cached = cache[song_id]
        encrypted_url = cached.get("encrypted_url") or cached.get("audio_url")
        if encrypted_url:
            print("  Using encrypted URL captured from Suno's API response.")
            cached["audio_url"] = build_sw_passthrough_url(song_id, encrypted_url)
            return cached

    url = SUNO_SONG_URL.format(song_id)
    page.goto(url, wait_until="domcontentloaded")
    # Give React a moment to hydrate and/or fire API calls
    time.sleep(2.0)

    encrypted_url = page.evaluate(_js_extract_encrypted_audio_url())

    details = page.evaluate(_js_extract_prompt())
    details.setdefault("uuid", song_id)
    if not details.get("title"):
        details["title"] = song_id

    # If the page didn't expose media_urls, try the public Suno API endpoints.
    if not encrypted_url:
        encrypted_url = _fetch_encrypted_url_via_api(page, song_id)

    if encrypted_url:
        details["audio_url"] = build_sw_passthrough_url(song_id, encrypted_url)
        details["encrypted_url"] = encrypted_url

    return details


def _fetch_encrypted_url_via_api(page: Page, song_id: str) -> str | None:
    """Last-resort: ask the page to call Suno's API and return an encrypted media URL."""
    endpoints = [
        f"https://studio-api-prod.suno.com/api/clips/get_songs_by_ids?ids={song_id}",
    ]

    for endpoint in endpoints:
        try:
            response = page.evaluate(
                """async ({endpoint}) => {
                    try {
                        const res = await fetch(endpoint, { credentials: 'include' });
                        if (!res.ok) return null;
                        const data = await res.json();
                        const walk = (obj) => {
                            if (!obj || typeof obj !== 'object') return null;
                            if (Array.isArray(obj.media_urls)) {
                                for (const m of obj.media_urls) {
                                    if (m && m.url) return m.url;
                                }
                            }
                            if (obj.audio_url) return obj.audio_url;
                            if (obj.source_audio_url) return obj.source_audio_url;
                            if (obj.stream_url) return obj.stream_url;
                            for (const v of Object.values(obj)) {
                                const found = walk(v);
                                if (found) return found;
                            }
                            return null;
                        };
                        return walk(data);
                    } catch (e) {
                        return null;
                    }
                }""",
                {"endpoint": endpoint},
            )
            if response and is_audio_url(response):
                return response
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

def download_with_browser(
    page: Page,
    audio_url: str,
    output_path: Path,
) -> bool:
    """Download an audio file through the browser (service worker decrypts it)."""
    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        try:
            # Use page.evaluate + fetch so the request is made by a SW client.
            result = page.evaluate(
                """async ({url}) => {
                    const res = await fetch(url, { credentials: 'include' });
                    const blob = await res.blob();
                    const buf = await blob.arrayBuffer();
                    return {
                        ok: res.ok,
                        status: res.status,
                        ctype: res.headers.get('content-type') || '',
                        bytes: Array.from(new Uint8Array(buf))
                    };
                }""",
                {"url": audio_url},
            )

            if not result["ok"]:
                print(
                    f"  HTTP {result['status']} for {audio_url} "
                    f"(attempt {attempt}/{MAX_DOWNLOAD_RETRIES})"
                )
                if attempt < MAX_DOWNLOAD_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                return False

            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(bytes(result["bytes"]))
            return True

        except Exception as exc:  # noqa: BLE001
            print(f"  Download error: {exc} (attempt {attempt}/{MAX_DOWNLOAD_RETRIES})")
            if attempt < MAX_DOWNLOAD_RETRIES:
                time.sleep(2 ** attempt)
            else:
                return False

    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def dump_debug_info(page: Page, song_id: str) -> None:
    """Save page HTML and console context when extraction fails."""
    debug_dir = OUTPUT_DIR / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    html_path = debug_dir / f"{song_id}.html"
    try:
        html_path.write_text(page.content(), encoding="utf-8")
        print(f"  Debug page saved to {html_path}")
    except Exception as exc:  # noqa: BLE001
        print(f"  Could not save debug HTML: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download your Suno library using a real browser."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save page HTML for any song whose audio URL cannot be found.",
    )
    parser.add_argument(
        "format",
        nargs="?",
        choices=["mp4", "mp3"],
        default=DEFAULT_OUTPUT_FORMAT,
        help=(
            "Output audio format. Suno serves an MP4 (Opus) stream; "
            "choosing mp3 transcodes it with ffmpeg. (default: %(default)s)"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    OUTPUT_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        print(f"Suno Music Downloader v{VERSION}")
        print(f"Launching Chromium with persistent profile: {PROFILE_DIR}\n")
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        try:
            catcher = NetworkCatcher()
            page.on("response", catcher.handle_response)
            ensure_logged_in(page)
            scroll_to_bottom(page, catcher=catcher)
            songs = extract_library_songs(page)

            if not songs:
                print("No songs found. Exiting.")
                return 1

            # Merge any songs captured from network responses that didn't make
            # it into the DOM yet (e.g. extra pages loaded in the background).
            cache = catcher.songs
            dom_ids = {s["id"] for s in songs}
            for song_id, cached in cache.items():
                if song_id not in dom_ids:
                    songs.append(
                        {
                            "id": song_id,
                            "title": cached.get("title") or song_id,
                        }
                    )
                    dom_ids.add(song_id)

            print(f"Ready to download {len(songs)} song(s).\n")

            successful = 0
            failed = 0

            for idx, song in enumerate(songs, start=1):
                song_id = song["id"]
                base_title = song.get("title") or song_id
                output_base = build_output_name(base_title, song_id)
                txt_path = OUTPUT_DIR / f"{output_base}.txt"

                print(f"[{idx}/{len(songs)}] {base_title} ({song_id})")

                try:
                    details = get_song_details(page, song_id, cache=cache)
                except Exception as exc:  # noqa: BLE001
                    print(f"  Failed to read song page: {exc}")
                    failed += 1
                    continue

                audio_url = details.get("audio_url")
                if not audio_url:
                    print(f"  Could not find an audio URL for this song.")
                    if args.debug:
                        dump_debug_info(page, song_id)
                    failed += 1
                    continue

                target_ext = f".{args.format}"
                audio_path = OUTPUT_DIR / f"{output_base}{target_ext}"

                if audio_path.exists() and txt_path.exists():
                    print(f"  Already downloaded — skipping.")
                    successful += 1
                    continue

                # Suno's passthrough always returns an MP4/Opus stream.
                mp4_path = OUTPUT_DIR / f"{output_base}.mp4"

                # Save prompt/UUID text file first
                uuid = details.get("uuid") or song_id
                prompt = details.get("prompt") or "No description available"
                txt_path.write_text(
                    f"Original filename: {uuid}{target_ext}\n\nPrompt:\n{prompt.strip()}\n",
                    encoding="utf-8",
                )

                # Download audio through the browser (service worker decrypts)
                if not download_with_browser(page, audio_url, mp4_path):
                    print(f"  Download failed.")
                    failed += 1
                    continue

                if args.format == "mp3":
                    if convert_to_mp3(mp4_path, audio_path):
                        print(f"  Saved -> {audio_path.name}")
                        try:
                            mp4_path.unlink()
                        except OSError:
                            pass
                        successful += 1
                    else:
                        print(f"  Kept intermediate MP4: {mp4_path.name}")
                        successful += 1
                else:
                    print(f"  Saved -> {audio_path.name}")
                    successful += 1

            print(f"\nDone. Downloaded {successful}/{len(songs)} song(s). Failed: {failed}")
            return 0 if failed == 0 else 1

        finally:
            context.close()


if __name__ == "__main__":
    sys.exit(main())
