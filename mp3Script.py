import os
import subprocess
import requests
import json
import re
import multiprocessing
from concurrent.futures import ThreadPoolExecutor, as_completed
from mutagen.id3 import ID3, USLT, APIC, error
from mutagen.mp3 import MP3
import urllib.parse

# -----------------------
# Config
# -----------------------
SONGS_DIR = "songs"
META_FILE = "metadata.json"
os.makedirs(SONGS_DIR, exist_ok=True)

# GitHub repo details
GITHUB_USER = "coDeiNject7"
GITHUB_REPO = "testingsong"
GITHUB_BRANCH = "main"

# Global metadata collector
metadata_collection = []


# -----------------------
# Helpers
# -----------------------
def sanitize_filename(name):
    return re.sub(r'[<>:"/\\|?*]', '_', name)


# -----------------------
# Metadata embedding
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
                encoding=3,
                mime="image/jpeg",
                type=3,
                desc="Cover",
                data=album_art_data
            ))

        for lyrics_text in lyrics_texts:
            audio.tags.add(USLT(
                encoding=3,
                lang="und",
                desc="Lyrics",
                text=lyrics_text
            ))

        audio.save()
    except Exception as e:
        print(f"Error embedding metadata for {mp3_file}: {e}")


# -----------------------
# Song exists check
# -----------------------
def song_exists(title):
    return os.path.exists(os.path.join(SONGS_DIR, f"{title}.mp3"))


# -----------------------
# Download one audio
# -----------------------
def download_audio(youtube_url, song_id):
    global metadata_collection
    try:
        cmd_info = ["yt-dlp", "--skip-download", "--print-json", youtube_url]
        result = subprocess.run(cmd_info, capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            print(f"Error fetching info: {result.stderr}")
            return

        data = json.loads(result.stdout.strip().split("\n")[-1])
        title = sanitize_filename(data.get("title", f"Unknown_{song_id}"))
        mp3_file = os.path.join(SONGS_DIR, f"{title}.mp3")
        thumbnail_url = data.get("thumbnail")
        artist = data.get("artist") or data.get("uploader") or "Unknown Artist"
        audio_lang = data.get("audioLanguage") or "und"

        if song_exists(title):
            print(f"Skipping {title} ({audio_lang}): already exists")
            return

        # Download MP3
        subprocess.run([
            "yt-dlp", "-x", "--audio-format", "mp3",
            "-o", f"{SONGS_DIR}/%(title)s.%(ext)s", youtube_url
        ])

        # Download album art
        album_art_file, album_art_data = None, None
        try:
            if thumbnail_url:
                album_art_data = requests.get(thumbnail_url).content
                album_art_file = os.path.join(SONGS_DIR, f"{title}.jpg")
                with open(album_art_file, "wb") as f:
                    f.write(album_art_data)
        except Exception as e:
            print(f"Error downloading album art: {e}")

        # Download subtitles -> lyrics
        lyrics_texts = []
        for sub_lang in (data.get("subtitles") or {}).keys():
            cmd_subs = [
                "yt-dlp", "--skip-download", "--write-auto-sub",
                "--convert-subs=lrc", "--sub-lang", sub_lang,
                "-o", f"{SONGS_DIR}/%(title)s.%(ext)s", youtube_url
            ]
            subprocess.run(cmd_subs)

            lrc_file = os.path.join(SONGS_DIR, f"{title}.lrc")
            if os.path.exists(lrc_file):
                with open(lrc_file, "r", encoding="utf-8") as f:
                    lyrics_texts.append(f.read())
                os.remove(lrc_file)

        embed_metadata(mp3_file, album_art_data, lyrics_texts)

        song_meta = {
            "title": title,
            "artist": artist,
            "file": None,
            "album_art": None,
            "audio_lang": audio_lang,
            "lyrics": lyrics_texts
        }
        metadata_collection.append(song_meta)

        print(f"✅ Downloaded & processed: {title}")

    except Exception as e:
        print(f"Error downloading {youtube_url}: {e}")


# -----------------------
# Playlist handling
# -----------------------
def get_playlist_urls(playlist_url):
    cmd = ["yt-dlp", "--flat-playlist", "-j", playlist_url]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    urls = []
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            data = json.loads(line)
            if "id" in data:
                urls.append(f"https://www.youtube.com/watch?v={data['id']}")
    return urls


def download_playlist_dynamic(playlist_url):
    urls = get_playlist_urls(playlist_url)[:10]
    workers = max(1, min(multiprocessing.cpu_count() - 1, 5))
    print(f"▶ Using {workers} parallel downloads...")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(download_audio, url, i) for i, url in enumerate(urls)]
        for f in as_completed(futures):
            f.result()

    # Save metadata.json (preliminary)
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(metadata_collection, f, indent=2, ensure_ascii=False)

    print(f"\n🎵 Playlist finished. Metadata saved to {META_FILE}")

    push_to_github()
    asset_urls = create_github_release_and_upload_assets()

    # Replace file paths in metadata with release URLs
    for song_meta in metadata_collection:
        sanitized_title = sanitize_filename(song_meta["title"])
        for asset_name, url in asset_urls.items():
            if asset_name.endswith(".mp3") and sanitize_filename(asset_name[:-4]) == sanitized_title:
                song_meta["file"] = url
            if asset_name.endswith(".jpg") and sanitize_filename(asset_name[:-4]) == sanitized_title:
                song_meta["album_art"] = url

    # Save updated metadata
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(metadata_collection, f, indent=2, ensure_ascii=False)

    subprocess.run(["git", "add", META_FILE], check=True)
    subprocess.run(["git", "commit", "-m", "Update metadata with release URLs"], check=True)
    subprocess.run(["git", "push", "origin", GITHUB_BRANCH], check=True)


# -----------------------
# GitHub release uploader
# -----------------------
def create_github_release_and_upload_assets(tag_name="latest", release_name="Latest Songs Release"):
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("❌ GitHub token not found. Set GITHUB_TOKEN environment variable.")
        return {}

    api_base = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json"
    }

    # Create a release
    release_data = {
        "tag_name": tag_name,
        "name": release_name,
        "body": "Auto release of latest songs",
        "draft": False,
        "prerelease": False
    }

    release_response = requests.post(f"{api_base}/releases", headers=headers, json=release_data)
    if release_response.status_code != 201:
        print("⚠️ Failed to create release:", release_response.json())
        return {}

    release = release_response.json()
    upload_url = release["upload_url"].split("{")[0]
    asset_urls = {}

    for filename in os.listdir(SONGS_DIR):
        if not filename.lower().endswith((".mp3", ".jpg")):
            continue

        file_path = os.path.join(SONGS_DIR, filename)
        mime_type = "audio/mpeg" if filename.endswith(".mp3") else "image/jpeg"
        params = {"name": filename}

        with open(file_path, "rb") as f:
            upload_response = requests.post(
                upload_url,
                headers={**headers, "Content-Type": mime_type},
                params=params,
                data=f.read()
            )

        if upload_response.status_code == 201:
            download_url = upload_response.json()["browser_download_url"]
            asset_urls[filename] = download_url
            print(f"📤 Uploaded: {filename}")
        else:
            print(f"❌ Failed to upload {filename}:", upload_response.json())

    return asset_urls


# -----------------------
# GitHub push
# -----------------------
def push_to_github():
    try:
        subprocess.run(["git", "add", SONGS_DIR, META_FILE], check=True)
        subprocess.run(["git", "commit", "-m", "Update songs + metadata"], check=True)
        subprocess.run(["git", "push", "origin", GITHUB_BRANCH], check=True)
        print("🚀 Pushed songs + metadata to GitHub.")
    except Exception as e:
        print(f"⚠️ GitHub push failed: {e}")


# -----------------------
# Run
# -----------------------
if __name__ == "__main__":
    playlist_url = "https://youtube.com/playlist?list=PL9bw4S5ePsEEqCMJSiYZ-KTtEjzVy0YvK&si=oJ5LnrlmYiOdmMZY"
    download_playlist_dynamic(playlist_url)
