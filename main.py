import os
import json
import time
import subprocess
import requests
import yt_dlp
from google import genai
from google.genai import errors
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ==========================================
# 1. CONFIGURATION & 19-CATEGORY TAXONOMY
# ==========================================
HISTORY_FILE = "posted_history.json"
OUTPUT_DIR = "downloads"

CATEGORIES = {
    "funny": ["laugh", "lol", "haha", "hilarious", "bro", "no way", "oh my god", "wtf", "why did", "i can't", "dead"],
    "weird": ["weird", "what is happening", "what the", "npc", "random", "cursed"],
    "strange": ["strange", "odd", "unexpected", "never seen", "how did"],
    "chaotic": ["chaos", "chaotic", "everyone", "run", "shooting", "explosion", "crazy", "mayhem"],
    "glitch": ["glitch", "bug", "broken", "floating", "stuck", "teleport", "disappeared", "physics"],
    "beautiful": ["beautiful", "look at this", "gorgeous", "amazing view"],
    "trick": ["tip", "trick", "how to", "easy", "you can", "did you know"],
    "stunt": ["stunt", "jump", "jumping", "flip", "land", "ramp", "drift", "wheelie", "crazy jump"],
    "clutch": ["clutch", "1 hp", "one hp", "barely", "survived", "close call", "how did i survive", "last second", "saved it"],
    "fail": ["fail", "failed", "wasted", "crashed", "ruined", "rip", "instant karma", "messed up", "my bad", "worst"],
    "police_chase": ["police", "cops", "chase", "wanted", "five stars", "5 stars", "swat", "sirens", "busted", "escape", "helicopter", "roadblock"],
    "rage": ["rage", "angry", "mad", "hate this game", "bullshit", "rigged", "unfair", "screaming", "bro what", "are you kidding me"],
    "cry": ["cry", "crying", "sad", "heartbreaking", "emotional", "tears", "tragic", "poor guy", "devastating"],
    "troll": ["troll", "trolling", "prank", "tricked", "baited", "got him", "messing with", "funny reaction", "grief"],
    "speedrun": ["speedrun", "world record", "fast", "fastest", "shortcut", "skip", "time save", "pb", "personal best", "optimize"],
    "jumpscare": ["scare", "jump scare", "what is that", "creepy", "scary", "didn't see that", "ambush", "jumped"],
    "easter_egg": ["easter egg", "secret", "reference", "hidden", "detail", "discovered", "found this", "developer", "mind blowing"],
    "cinematic": ["cinematic", "movie", "angle", "shot", "view", "vibe", "aesthetic", "sunset", "lighting"],
    "epic": ["epic", "legendary", "insane", "god tier", "unbelievable", "clean", "smooth", "pro", "masterpiece"],
}

VALID_CATEGORY_NAMES = list(CATEGORIES.keys())


# ==========================================
# 2. HISTORY HELPERS
# ==========================================
def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_history(history: list) -> None:
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


# ==========================================
# 3. TWITCH HELIX API CLIENT
# ==========================================
def get_twitch_app_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_twitch_game_id(client_id: str, token: str, game_name: str) -> str | None:
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    resp = requests.get(
        "https://api.twitch.tv/helix/games",
        headers=headers,
        params={"name": game_name},
        timeout=20,
    )
    resp.raise_for_status()
    items = resp.json().get("data") or []
    return items[0]["id"] if items else None


def fetch_top_twitch_clips(client_id: str, token: str, game_id: str, limit: int = 10) -> list[dict]:
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    resp = requests.get(
        "https://api.twitch.tv/helix/clips",
        headers=headers,
        params={"game_id": game_id, "first": min(max(limit, 1), 100)},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("data") or []


# ==========================================
# 4. HYBRID CLASSIFICATION (SAVES API QUOTA)
# ==========================================
def classify_clip_locally(text: str) -> tuple[str, int]:
    text_lower = (text or "").lower()
    scores = {
        cat: sum(1 for kw in keywords if kw in text_lower)
        for cat, keywords in CATEGORIES.items()
    }
    best_category = max(scores, key=scores.get)
    return best_category, scores[best_category]


def classify_content(title: str, broadcaster: str = "") -> str:
    combined_text = f"{title or ''} {broadcaster or ''}".strip()
    local_category, score = classify_clip_locally(combined_text)

    if score >= 2:
        print(f"⚡ Local keyword match: '{local_category}' (Score: {score}). Skipping Gemini API call.")
        return local_category

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print(f"ℹ️ GEMINI_API_KEY not set. Using local category: '{local_category}'.")
        return local_category

    print(f"🤖 Low local score ({score}). Querying Gemini 3.8 Flash API...")
    client = genai.Client(api_key=api_key)
    prompt = (
        f"Classify this GTA 6 Twitch clip into exactly ONE of these categories: {', '.join(VALID_CATEGORY_NAMES)}.\n"
        f"Title: {title}\n"
        f"Streamer: {broadcaster}\n"
        f"Return ONLY the category name."
    )

    for attempt in range(2):
        try:
            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=prompt,
            )
            raw_text = (response.text or "").strip().lower()
            for valid_cat in VALID_CATEGORY_NAMES:
                if valid_cat == raw_text or valid_cat in raw_text.split():
                    return valid_cat
            return local_category
        except errors.APIError as e:
            if "503" in str(e) and attempt == 0:
                print("⚠️ Gemini 503 high demand. Retrying once in 3 seconds...")
                time.sleep(3)
                continue
            print(f"⚠️ Gemini API error ({e}). Falling back to '{local_category}'.")
            return local_category
        except Exception as e:
            print(f"⚠️ Unexpected Gemini error: {e}. Falling back to '{local_category}'.")
            return local_category

    return local_category


# ==========================================
# 5. DYNAMIC TITLE & HASHTAG GENERATOR
# ==========================================
def generate_metadata(original_title: str, broadcaster: str, category: str) -> tuple[str, str]:
    clean_title = " ".join((original_title or "GTA 6 Twitch Clip").split())
    base_tags = "#GTA6 #GrandTheftAuto6 #TwitchClips #Shorts #Reels #IndianGaming #DesiGamer"

    category_tags = {
        "police_chase": "#PoliceChase #GTA6Gameplay #Wanted",
        "glitch": "#Glitch #GTA6Bugs #FunnyMoments",
        "funny": "#Funny #GTA6Moments #Lol",
        "stunt": "#Stunts #GTA6Stunts #Epic",
        "clutch": "#Clutch #GamingMoments #Surviving",
        "fail": "#Fail #GamingFail #Wasted",
        "chaotic": "#Chaos #GTA6Chaos #Gaming",
        "easter_egg": "#EasterEgg #Secrets #GTA6Secrets",
        "cinematic": "#Cinematic #GTA6Graphics #Gaming",
        "epic": "#EpicMoments #GTA6Clips #GamingIndia",
    }

    cat_tag = category_tags.get(category, "#Gaming #Trending")
    title = f"{clean_title[:62]} | GTA 6 #{category.replace('_', '').capitalize()} #Shorts"
    description = (
        f"GTA 6 {category.replace('_', ' ').title()} clip from Twitch streamer {broadcaster or 'creator'}!\n\n"
        f"{base_tags} {cat_tag}"
    )
    return title, description


# ==========================================
# 6. VIDEO DOWNLOAD & UNCROPPED TRIM
# ==========================================
def download_and_trim_clip(clip_url: str, output_filename: str, max_duration: int = 58) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    temp_source = os.path.join(OUTPUT_DIR, f"temp_{output_filename}")
    final_path = os.path.join(OUTPUT_DIR, output_filename)

    ydl_opts = {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "outtmpl": temp_source,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }

    try:
        print(f"📥 Downloading Twitch clip: {clip_url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([clip_url])

        actual_source = temp_source
        if not os.path.exists(actual_source):
            for ext in (".mp4", ".mkv", ".webm"):
                candidate = f"{temp_source}{ext}"
                if os.path.exists(candidate):
                    actual_source = candidate
                    break

        if not os.path.exists(actual_source):
            raise FileNotFoundError("Downloaded Twitch clip file was not found.")

        print("✂️ Exporting clean MP4 without cropping or scaling...")
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-i", actual_source,
            "-t", str(max_duration),
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "22",
            "-c:a", "aac",
            "-movflags", "+faststart",
            final_path,
        ]

        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {result.stderr.strip()}")

        print(f"✅ Clip saved locally: {final_path}")
        return final_path
    finally:
        for path in {temp_source, f"{temp_source}.mp4", f"{temp_source}.mkv", f"{temp_source}.webm"}:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


# ==========================================
# 7. UPLOAD HOOKS (YOUTUBE & INSTAGRAM)
# ==========================================
def upload_to_youtube(file_path: str, title: str, description: str, privacy_status: str = "public") -> str | None:
    client_id = os.environ.get("YT_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YT_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("YT_REFRESH_TOKEN", "").strip()

    if not all([client_id, client_secret, refresh_token]):
        print("ℹ️ YouTube OAuth secrets not set. Skipping YouTube upload.")
        return None

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    creds.refresh(Request())

    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": ["GTA6", "GrandTheftAuto6", "TwitchClips", "IndianGaming", "Gaming"],
            "categoryId": "20",
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(file_path, mimetype="video/mp4", chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _, response = request.next_chunk()

    video_id = response.get("id")
    print(f"✅ Uploaded to YouTube! Video ID: {video_id}")
    return video_id


def upload_to_instagram(file_path: str, caption: str) -> None:
    print(f"📤 [Instagram Hook Ready] File: {file_path} | Caption lines: {len(caption.splitlines())}")


# ==========================================
# 8. MAIN PIPELINE EXECUTION
# ==========================================
def main() -> None:
    print("🚀 Starting Twitch GTA 6 Clipper Pipeline...")
    twitch_client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    twitch_client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    game_name = os.environ.get("TWITCH_GAME_NAME", "Grand Theft Auto VI").strip()

    if not twitch_client_id or not twitch_client_secret:
        raise RuntimeError("Missing TWITCH_CLIENT_ID or TWITCH_CLIENT_SECRET environment variables.")

    history = load_history()
    seen_ids = set(history)

    token = get_twitch_app_token(twitch_client_id, twitch_client_secret)
    game_id = get_twitch_game_id(twitch_client_id, token, game_name)
    if not game_id:
        print(f"⚠️ Twitch category '{game_name}' not found yet. Falling back to 'Grand Theft Auto V'.")
        game_id = get_twitch_game_id(twitch_client_id, token, "Grand Theft Auto V")

    if not game_id:
        print("❌ Could not resolve a Twitch game_id.")
        return

    clips = fetch_top_twitch_clips(twitch_client_id, token, game_id=game_id, limit=15)
    new_clips = [c for c in clips if c.get("id") and c["id"] not in seen_ids]

    if not new_clips:
        print("ℹ️ No new unprocessed Twitch clips found.")
        return

    for clip in new_clips:
        clip_id = clip["id"]
        clip_url = clip.get("url") or f"https://clips.twitch.tv/{clip_id}"
        clip_title = clip.get("title") or "GTA 6 Twitch Clip"
        broadcaster = clip.get("broadcaster_name") or ""

        print(f"\n🎮 Evaluating Twitch Clip: {clip_title} ({clip_url})")
        category = classify_content(clip_title, broadcaster)
        title, description = generate_metadata(clip_title, broadcaster, category)
        output_filename = f"twitch_{clip_id}_{category}.mp4"

        print(f"🎯 Category: {category}")
        print(f"📝 Title: {title}")

        try:
            saved_path = download_and_trim_clip(clip_url, output_filename=output_filename)
            upload_to_youtube(saved_path, title, description)
            upload_to_instagram(saved_path, description)

            history.append(clip_id)
            save_history(history)
            print("✨ Twitch clip processed and saved successfully!")
            break
        except Exception as e:
            print(f"❌ Failed on clip {clip_id}: {e}")
            continue


if __name__ == "__main__":
    main()
