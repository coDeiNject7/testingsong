import os
import subprocess
import requests
import json
import re
import multiprocessing
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from mutagen.id3 import ID3, USLT, APIC, error, TIT2, TPE1, TALB, TDRC, TCON, TXXX
from mutagen.mp3 import MP3

# -----------------------
# Config
# -----------------------
SONGS_DIR = "songs"
META_FILE = "metadata.json"
SONGS_JSON = "j-ysongs.json"   # input file with song list
os.makedirs(SONGS_DIR, exist_ok=True)
GITHUB_USER = "coDeiNject7"
GITHUB_REPO = "testingsong"
GITHUB_BRANCH = "main"
BATCH_SIZE = 10  # Push & release after every 10 songs

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
# Load Songs JSON (limit 100)
# -----------------------
with open(SONGS_JSON, "r", encoding="utf-8") as f:
    songs_data = json.load(f)[:100]   # Take only first 100

# -----------------------
# Helpers
# -----------------------
def save_metadata():
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
        if song["song"] == title and song.get("file"):
            return True
    return False

# -----------------------
# Embed metadata
# -----------------------
def embed_metadata(mp3_file, song_entry, album_art_data=None):
    try:
        audio = MP3(mp3_file, ID3=ID3)
        try:
            audio.add_tags()
        except error:
            pass

        audio.tags.add(TIT2(encoding=3, text=song_entry.get("song", "")))
        audio.tags.add(TPE1(encoding=3, text=song_entry.get("artists", "")))
        audio.tags.add(TALB(encoding=3, text=song_entry.get("movie", "")))
        audio.tags.add(TDRC(encoding=3, text=song_entry.get("year", "")))
        if song_entry.get("genre"):
            audio.tags.add(TCON(encoding=3, text=song_entry.get("genre")))

        audio.tags.add(TXXX(encoding=3, desc="Composers", text=song_entry.get("composers", "")))
        audio.tags.add(TXXX(encoding=3, desc="Language", text=song_entry.get("language", "")))
        audio.tags.add(TXXX(encoding=3, desc="Duration", text=song_entry.get("duration", "")))
        audio.tags.add(TXXX(encoding=3, desc="Label", text=song_entry.get("label", "")))

        if album_art_data:
            audio.tags.add(APIC(
                encoding=3, mime="image/jpeg", type=3, desc="Cover", data=album_art_data
            ))

        audio.save()
    except Exception as e:
        print(f"Error embedding metadata for {mp3_file}: {e}")

# -----------------------
# Download audio using JSON entry
# -----------------------
def download_audio_from_json(song_entry, song_id):
    global metadata_collection, last_index
    try:
        youtube_url = song_entry["youtube"]
        song_name = song_entry["song"]
        safe_title = sanitize_filename(song_name)
        mp3_file = os.path.join(SONGS_DIR, f"{safe_title}.mp3")

        if song_exists(safe_title) or already_in_metadata(song_name):
            print(f"⏩ Skipping {song_name}: already downloaded.")
            last_index = song_id
            save_metadata()
            return

        print(f"[download_audio] Will save mp3 as: {mp3_file}")

        cmd_info = ["yt-dlp", "--skip-download", "--print-json", youtube_url]
        result = subprocess.run(cmd_info, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error fetching info: {result.stderr}")
            return
        data = json.loads(result.stdout.strip().split("\n")[-1])
        thumbnail_url = data.get("thumbnail")

        subprocess.run([
            "yt-dlp", "-x", "--audio-format", "mp3",
            "-o", f"{SONGS_DIR}/{safe_title}.%(ext)s", youtube_url
        ], check=True)

        album_art_data = None
        if thumbnail_url:
            try:
                album_art_data = requests.get(thumbnail_url).content
                album_art_path = os.path.join(SONGS_DIR, f"{safe_title}.jpg")
                with open(album_art_path, "wb") as f:
                    f.write(album_art_data)
            except Exception as e:
                print(f"Error downloading album art: {e}")

        embed_metadata(mp3_file, song_entry, album_art_data)

        entry_with_release = dict(song_entry)
        entry_with_release.update({
            "file": None,
            "album_art": None
        })
        metadata_collection.append(entry_with_release)
        last_index = song_id
        save_metadata()

        print(f"✅ Downloaded & processed: {song_name}")
    except Exception as e:
        print(f"Error processing {song_entry.get('youtube')}: {e}")

# -----------------------
# GitHub Release Upload
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
# Batch processing function
# -----------------------
def process_batch():
    push_to_github()
    asset_urls = create_github_release_and_upload_assets()

    for song_meta in metadata_collection:
        safe_title = sanitize_filename(song_meta["song"])
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
# Download Songs from JSON
# -----------------------
def download_songs_from_json():
    global last_index
    start_index = last_index + 1
    songs_to_download = [(entry, i) for i, entry in enumerate(songs_data) if i >= start_index]

    if not songs_to_download:
        print("✅ All songs already processed.")
        return

    workers = max(1, min(multiprocessing.cpu_count() - 1, 5))
    print(f"▶ Resuming from index {start_index}, downloading {len(songs_to_download)} songs with {workers} workers...")

    batch_counter = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(download_audio_from_json, entry, i) for entry, i in songs_to_download]
        for f in as_completed(futures):
            f.result()
            batch_counter += 1

            if batch_counter % BATCH_SIZE == 0:
                print(f"\n🚀 Processing batch of {BATCH_SIZE} songs...")
                process_batch()

    if batch_counter % BATCH_SIZE != 0:
        print("\n🚀 Processing final batch...")
        process_batch()

# -----------------------
# Run
# -----------------------
if __name__ == "__main__":
    download_songs_from_json()
