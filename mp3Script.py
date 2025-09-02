import os
import subprocess
import requests
import json
import re
import multiprocessing
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
metadata_collection = []

# -----------------------
# Helpers
# -----------------------
def sanitize_filename(name):
    sanitized = re.sub(r'[<>:"/\|?*\n\r\t]', '_', name).strip()
    print(f"[sanitize_filename] Title: {name}\n              Sanitized: {sanitized}")
    return sanitized

def song_exists(title):
    exists = os.path.exists(os.path.join(SONGS_DIR, f"{title}.mp3"))
    print(f"[song_exists] Checking if exists: {title}.mp3 -> {exists}")
    return exists

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
    global metadata_collection
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
        print(f"[download_audio] Will save mp3 as: {mp3_file}")
        thumbnail_url = data.get("thumbnail")
        artist = data.get("artist") or data.get("uploader") or "Unknown Artist"
        audio_lang = data.get("audioLanguage") or "und"
        if song_exists(safe_title):
            print(f"⏩ Skipping {safe_title}: already exists")
            return
        # Download audio
        print(f"[yt-dlp download] yt-dlp -x --audio-format mp3 -o {SONGS_DIR}/%(title)s.%(ext)s {youtube_url}")
        subprocess.run([
            "yt-dlp", "-x", "--audio-format", "mp3",
            "-o", f"{SONGS_DIR}/%(title)s.%(ext)s", youtube_url
        ], check=True)
        # After download, list files
        print(f"[Files in songs dir after download]: {os.listdir(SONGS_DIR)}")
        # Album art
        album_art_data = None
        if thumbnail_url:
            try:
                album_art_data = requests.get(thumbnail_url).content
                album_art_path = os.path.join(SONGS_DIR, f"{safe_title}.jpg")
                with open(album_art_path, "wb") as f:
                    f.write(album_art_data)
                print(f"[download_audio] Saved album art: {album_art_path}")
            except Exception as e:
                print(f"Error downloading album art: {e}")
        # Subtitles (lyrics)
        lyrics_texts = []
        for sub_lang in (data.get("subtitles") or {}).keys():
            subprocess.run([
                "yt-dlp", "--skip-download", "--write-auto-sub",
                "--convert-subs", "lrc", "--sub-lang", sub_lang,
                "-o", f"{SONGS_DIR}/%(title)s.%(ext)s", youtube_url
            ])
            lrc_file = os.path.join(SONGS_DIR, f"{safe_title}.lrc")
            if os.path.exists(lrc_file):
                with open(lrc_file, "r", encoding="utf-8") as f:
                    lyrics_texts.append(f.read())
                os.remove(lrc_file)
        embed_metadata(mp3_file, album_art_data, lyrics_texts)
        metadata_collection.append({
            "title": title,
            "artist": artist,
            "file": None,
            "album_art": None,
            "audio_lang": audio_lang,
            "lyrics": lyrics_texts
        })
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
        data = json.loads(line)
        if "id" in data:
            urls.append(f"https://www.youtube.com/watch?v={data['id']}")
    print(f"[get_playlist_urls] URLs: {urls}")
    return urls

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
    upload_url = release["upload_url"].split("{")[0]
    asset_urls = {}
    print(f"[create_github_release_and_upload_assets] Files in {SONGS_DIR}: {os.listdir(SONGS_DIR)}")
    for filename in os.listdir(SONGS_DIR):
        if not filename.endswith((".mp3", ".jpg")):
            continue
        file_path = os.path.join(SONGS_DIR, filename)
        mime_type = "audio/mpeg" if filename.endswith(".mp3") else "image/jpeg"
        print(f"[Uploading asset] Filename: {filename} Path: {file_path}")
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
            print(f"📤 Uploaded: {filename} -> {url}")
        else:
            print(f"❌ Failed upload: {filename} -> {response.json()}")
    print(f"[create_github_release_and_upload_assets] All asset urls: {json.dumps(asset_urls, indent=2)}")
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
    urls = get_playlist_urls(playlist_url)[:10]
    workers = max(1, min(multiprocessing.cpu_count() - 1, 5))
    print(f"▶ Downloading {len(urls)} songs with {workers} workers...")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(download_audio, url, i) for i, url in enumerate(urls)]
        for f in as_completed(futures):
            f.result()
    # Save intermediate metadata
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(metadata_collection, f, indent=2, ensure_ascii=False)
    push_to_github()
    asset_urls = create_github_release_and_upload_assets()
    # Update metadata with GitHub URLs and LOG comparisons
    print("[download_playlist_dynamic] Starting asset-path mapping")
    for song_meta in metadata_collection:
        safe_title = sanitize_filename(song_meta["title"])
        print(f"[Mapping] Song title: {song_meta['title']}")
        print(f"         Sanitized for lookup: {safe_title}")
        for asset_name, url in asset_urls.items():
            compare_name = sanitize_filename(asset_name[:-4])
            print(f"         Asset filename: {asset_name} | compare_name: {compare_name}")
            if asset_name.endswith(".mp3") and compare_name == safe_title:
                print(f"         -> MP3 MATCH! -> {url}")
                song_meta["file"] = url
            elif asset_name.endswith(".jpg") and compare_name == safe_title:
                print(f"         -> JPG MATCH! -> {url}")
                song_meta["album_art"] = url
    # Save updated metadata
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(metadata_collection, f, indent=2, ensure_ascii=False)
    subprocess.run(["git", "add", META_FILE], check=True)
    subprocess.run(["git", "commit", "-m", "Update metadata with release URLs"], check=True)
    subprocess.run(["git", "push", "origin", GITHUB_BRANCH], check=True)

# -----------------------
# Run
# -----------------------
if __name__ == "__main__":
    playlist_url = "https://youtube.com/playlist?list=PL9bw4S5ePsEEqCMJSiYZ-KTtEjzVy0YvK&si=oJ5LnrlmYiOdmMZY"
    download_playlist_dynamic(playlist_url)
