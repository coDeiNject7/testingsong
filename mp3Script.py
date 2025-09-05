import os
import subprocess
import requests
import json
import re
import multiprocessing
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from mutagen.id3 import ID3, USLT, APIC, error
from mutagen.mp3 import MP3

# -----------------------
# Config
# -----------------------
SONGS_DIR = "songs"
META_FILE = "metadata.json"
os.makedirs(SONGS_DIR, exist_ok=True)
GITHUB_USER = "coDeiNject7"
GITHUB_REPO = "testingsong"
GITHUB_BRANCH = "main"

# -----------------------
# Load existing metadata (for resume)
# -----------------------
if os.path.exists(META_FILE):
    with open(META_FILE, "r", encoding="utf-8") as f:
        meta_data = json.load(f)
        metadata_collection = meta_data.get("songs", [])
        last_index = meta_data.get("last_index", -1)
else:
    metadata_collection = []
    last_index = -1

# -----------------------
# Helpers
# -----------------------
def save_metadata():
    """Save metadata + last index to disk safely"""
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "songs": metadata_collection,
            "last_index": last_index
        }, f, indent=2, ensure_ascii=False)

def sanitize_filename(name):
    normalized = unicodedata.normalize("NFKC", name)
    sanitized = re.sub(r'[<>:"/\\|?*\n\r\t]', '_', normalized).strip()
    return sanitized

def song_exists(title):
    return os.path.exists(os.path.join(SONGS_DIR, f"{title}.mp3"))

def already_in_metadata(title):
    for song in metadata_collection:
        if song["title"] == title and song.get("file"):
            return True
    return False

# -----------------------
# Embed metadata
# -----------------------
def embed_metadata(mp3_file, album_art_data=None, lyrics_texts=[]):
    try:
        audio = MP3(mp3_file, ID3=ID3)
        try:
            audio.add_tags()
        except error:
            pass
        if album_art_data:
            audio.tags.add(APIC(
                encoding=3, mime="image/jpeg", type=3, desc="Cover", data=album_art_data
            ))
        for lyrics_text in lyrics_texts:
            audio.tags.add(USLT(
                encoding=3, lang="und", desc="Lyrics", text=lyrics_text
            ))
        audio.save()
    except Exception as e:
        print(f"Error embedding metadata for {mp3_file}: {e}")

# -----------------------
# Download audio
# -----------------------
def download_audio(youtube_url, song_id):
    global metadata_collection, last_index
    try:
        cmd_info = ["yt-dlp", "--skip-download", "--print-json", youtube_url]
        result = subprocess.run(cmd_info, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error fetching info: {result.stderr}")
            return
        data = json.loads(result.stdout.strip().split("\n")[-1])
        title = data.get("title", f"Unknown_{song_id}")
        safe_title = sanitize_filename(title)
        mp3_file = os.path.join(SONGS_DIR, f"{safe_title}.mp3")

        # --- Resume check ---
        if song_exists(safe_title) or already_in_metadata(title):
            print(f"⏩ Skipping {title}: already downloaded.")
            last_index = song_id
            save_metadata()
            return

        print(f"[download_audio] Will save mp3 as: {mp3_file}")

        thumbnail_url = data.get("thumbnail")
        artist = data.get("artist") or data.get("uploader") or "Unknown Artist"
        audio_lang = data.get("audioLanguage") or "und"

        # Download audio
        subprocess.run([
            "yt-dlp", "-x", "--audio-format", "mp3",
            "-o", f"{SONGS_DIR}/{safe_title}.%(ext)s", youtube_url
        ], check=True)

        # Album art
        album_art_data = None
        if thumbnail_url:
            try:
                album_art_data = requests.get(thumbnail_url).content
                album_art_path = os.path.join(SONGS_DIR, f"{safe_title}.jpg")
                with open(album_art_path, "wb") as f:
                    f.write(album_art_data)
            except Exception as e:
                print(f"Error downloading album art: {e}")

        # Subtitles (lyrics)
        lyrics_texts = []
        for sub_lang in (data.get("subtitles") or {}).keys():
            subprocess.run([
                "yt-dlp", "--skip-download", "--write-auto-sub",
                "--convert-subs", "lrc", "--sub-lang", sub_lang,
                "-o", f"{SONGS_DIR}/{safe_title}.%(ext)s", youtube_url
            ])
            lrc_file = os.path.join(SONGS_DIR, f"{safe_title}.lrc")
            if os.path.exists(lrc_file):
                with open(lrc_file, "r", encoding="utf-8") as f:
                    lyrics_texts.append(f.read())
                os.remove(lrc_file)

        embed_metadata(mp3_file, album_art_data, lyrics_texts)

        # Save metadata immediately (for resume)
        metadata_collection.append({
            "title": title,
            "artist": artist,
            "file": None,
            "album_art": None,
            "audio_lang": audio_lang,
            "lyrics": lyrics_texts
        })
        last_index = song_id
        save_metadata()

        print(f"✅ Downloaded & processed: {title}")
    except Exception as e:
        print(f"Error processing {youtube_url}: {e}")

# -----------------------
# Playlist URLs
# -----------------------
def get_playlist_urls(playlist_url):
    cmd = ["yt-dlp", "--flat-playlist", "-j", playlist_url]
    result = subprocess.run(cmd, capture_output=True, text=True)
    urls = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        data = json.loads(line)
        if "id" in data:
            urls.append(f"https://www.youtube.com/watch?v={data['id']}")
    return urls

# -----------------------
# GitHub Release Upload (resumable)
# -----------------------
def create_github_release_and_upload_assets(tag_name="latest", release_name="Latest Songs Release"):
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("❌ GitHub token not found.")
        return {}

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json"
    }
    api_base = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}"

    # Check if release already exists
    r = requests.get(f"{api_base}/releases/tags/{tag_name}", headers=headers)
    if r.status_code == 200:
        release = r.json()
        print(f"ℹ️ Using existing release: {tag_name}")
    else:
        release_data = {
            "tag_name": tag_name,
            "name": release_name,
            "body": "Auto release of latest songs",
            "draft": False,
            "prerelease": False
        }
        r = requests.post(f"{api_base}/releases", headers=headers, json=release_data)
        if r.status_code != 201:
            print("⚠️ Release creation failed:", r.json())
            return {}
        release = r.json()
        print(f"✅ Created new release: {tag_name}")

    upload_url = release["upload_url"].split("{")[0]

    # Collect existing asset names
    existing_assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
    asset_urls = existing_assets.copy()

    for filename in os.listdir(SONGS_DIR):
        if not filename.endswith((".mp3", ".jpg")):
            continue
        if filename in existing_assets:
            print(f"⏩ Skipping upload, already exists: {filename}")
            continue

        file_path = os.path.join(SONGS_DIR, filename)
        mime_type = "audio/mpeg" if filename.endswith(".mp3") else "image/jpeg"
        with open(file_path, "rb") as f:
            response = requests.post(
                upload_url,
                headers={**headers, "Content-Type": mime_type},
                params={"name": filename},
                data=f.read()
            )
        if response.status_code == 201:
            url = response.json()["browser_download_url"]
            asset_urls[filename] = url
            print(f"📤 Uploaded: {filename}")
        else:
            print(f"❌ Failed upload: {filename} -> {response.json()}")

    return asset_urls

# -----------------------
# GitHub Push
# -----------------------
def push_to_github():
    try:
        subprocess.run(["git", "add", SONGS_DIR, META_FILE], check=True)
        subprocess.run(["git", "commit", "-m", "Update songs + metadata"], check=True)
        subprocess.run(["git", "push", "origin", GITHUB_BRANCH], check=True)
        print("🚀 Pushed songs + metadata.")
    except Exception as e:
        print(f"⚠️ GitHub push failed: {e}")

# -----------------------
# Download Playlist
# -----------------------
def download_playlist_dynamic(playlist_url):
    global last_index
    urls = get_playlist_urls(playlist_url)[:10]

    # Strict resume: only continue from last_index + 1
    start_index = last_index + 1
    urls_to_download = [(url, i) for i, url in enumerate(urls) if i >= start_index]

    if not urls_to_download:
        print("✅ All songs already processed.")
        return

    workers = max(1, min(multiprocessing.cpu_count() - 1, 5))
    print(f"▶ Resuming from index {start_index}, downloading {len(urls_to_download)} songs with {workers} workers...")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(download_audio, url, i) for url, i in urls_to_download]
        for f in as_completed(futures):
            f.result()

    push_to_github()
    asset_urls = create_github_release_and_upload_assets()

    # Map uploaded assets to metadata
    for song_meta in metadata_collection:
        safe_title = sanitize_filename(song_meta["title"])
        for asset_name, url in asset_urls.items():
            compare_name = sanitize_filename(asset_name[:-4])
            if asset_name.endswith(".mp3") and compare_name == safe_title:
                song_meta["file"] = url
            elif asset_name.endswith(".jpg") and compare_name == safe_title:
                song_meta["album_art"] = url

    save_metadata()
    subprocess.run(["git", "add", META_FILE], check=True)
    subprocess.run(["git", "commit", "-m", "Update metadata with release URLs"], check=True)
    subprocess.run(["git", "push", "origin", GITHUB_BRANCH], check=True)

# -----------------------
# Run
# -----------------------
if __name__ == "__main__":
    playlist_url = "https://youtube.com/playlist?list=PL9bw4S5ePsEEqCMJSiYZ-KTtEjzVy0YvK&si=oJ5LnrlmYiOdmMZY"
    download_playlist_dynamic(playlist_url)
