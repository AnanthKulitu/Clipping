import os
import json
import time
import subprocess
import yt_dlp
from google import genai
from google.genai import errors

# ==========================================
# 1. CONFIGURATION & 19-CATEGORY TAXONOMY
# ==========================================
HISTORY_FILE = "posted_history.json"
YOUTUBE_COOKIE_FILE = "cookies.txt"
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
# 2. COOKIES & HISTORY HELPERS
# ==========================================
def get_cookie_opts() -> dict:
    """Returns yt-dlp cookiefile option only when cookies.txt exists and is non-empty."""
    if os.path.exists(COOKIE_FILE) and os.path.getsize(YOUTUBE_COOKIE_FILE) > 0:
        return {"cookiefile": YOUTUBE_COOKIE_FILE}
    return {}


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
# 3. HYBRID CLASSIFICATION (SAVES API QUOTA)
# ==========================================
def classify_clip_locally(text: str) -> tuple[str, int]:
    """Scores text locally against categories to preserve Gemini free tier requests."""
    text_lower = (text or "").lower()
    scores = {
        cat: sum(1 for kw in keywords if kw in text_lower)
        for cat, keywords in CATEGORIES.items()
    }
    best_category = max(scores, key=scores.get)
    return best_category, scores[best_category]


def classify_content(title: str, description: str) -> str:
    combined_text = f"{title or ''} {description or ''}".strip()
    local_category, score = classify_clip_locally(combined_text)

    # Skip Gemini when 2+ local keywords match
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
        f"Classify this GTA 6 video into exactly ONE of these categories: {', '.join(VALID_CATEGORY_NAMES)}.\n"
        f"Title: {title}\n"
        f"Description: {description}\n"
        f"Return ONLY the category name."
    )

    # Retry once on transient 503 high demand; fall back immediately on 429 or other errors
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
            print(f"⚠️ Unexpected Gemini category '{raw_text}'. Falling back to '{local_category}'.")
            return local_category
        except errors.APIError as e:
            err_str = str(e)
            if "503" in err_str and attempt == 0:
                print("⚠️ Gemini 503 high demand. Retrying once in 3 seconds...")
                time.sleep(3)
                continue
            print(f"⚠️ Gemini API error ({e}). Falling back to local category: '{local_category}'.")
            return local_category
        except Exception as e:
            print(f"⚠️ Unexpected error during Gemini call: {e}. Falling back to '{local_category}'.")
            return local_category

    return local_category


# ==========================================
# 4. DYNAMIC TITLE & HASHTAG GENERATOR
# ==========================================
def generate_metadata(original_title: str, category: str) -> tuple[str, str]:
    """Generates titles and descriptions with hashtags for Shorts/Reels and Indian gaming viewers."""
    clean_title = " ".join((original_title or "GTA 6 Gameplay Moment").split())
    base_tags = "#GTA6 #GrandTheftAuto6 #Shorts #Reels #IndianGaming #DesiGamer"

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
    title = f"{clean_title[:65]} | GTA 6 #{category.replace('_', '').capitalize()} #Shorts"
    description = (
        f"GTA 6 {category.replace('_', ' ').title()} clip!\n\n"
        f"{base_tags} {cat_tag}"
    )
    return title, description


# ==========================================
# 5. VIDEO PROCESSING & LOCAL SAVING (UNCROPPED)
# ==========================================
def download_and_trim_clip(video_url: str, start_sec: int, end_sec: int, output_filename: str) -> str:
    """Downloads a video segment and trims it at its original aspect ratio without cropping."""
    if end_sec <= start_sec:
        raise ValueError("end_sec must be greater than start_sec")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    temp_source = os.path.join(OUTPUT_DIR, f"temp_{output_filename}")
    final_path = os.path.join(OUTPUT_DIR, output_filename)
    duration = end_sec - start_sec

    ydl_opts = {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "outtmpl": temp_source,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        **get_cookie_opts(),
    }

    try:
        print(f"📥 Downloading source video ({start_sec}s to {end_sec}s)...")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])

        actual_source = temp_source
        if not os.path.exists(actual_source):
            # Fallback if yt-dlp appended an extension
            for ext in (".mp4", ".mkv", ".webm"):
                candidate = f"{temp_source}{ext}"
                if os.path.exists(candidate):
                    actual_source = candidate
                    break

        if not os.path.exists(actual_source):
            raise FileNotFoundError("Downloaded source video file was not found.")

        print("✂️ Trimming clip without cropping or scaling...")
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-i", actual_source,
            "-t", str(duration),
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
# 6. UPLOAD HOOKS (YOUTUBE & INSTAGRAM)
# ==========================================
def upload_to_youtube(file_path: str, title: str, description: str) -> None:
    """Placeholder hook for YouTube Data API v3 upload."""
    print(f"📤 [YouTube Hook Ready] File: {file_path} | Title: {title}")


def upload_to_instagram(file_path: str, caption: str) -> None:
    """Placeholder hook for Instagram Graph API upload."""
    print(f"📤 [Instagram Hook Ready] File: {file_path} | Caption lines: {len(caption.splitlines())}")


# ==========================================
# 7. MAIN PIPELINE EXECUTION
# ==========================================
def main() -> None:
    print("🚀 Starting GTA 6 Automated Clipper & Downloader Pipeline...")
    history = load_history()
    seen_ids = set(history)

    # Optional channel URLs/IDs from environment variable
    raw_channels = os.environ.get("SOURCE_YT_CHANNEL_IDS", "")
    channels = [c.strip() for c in raw_channels.split(",") if c.strip()]

    search_queries = ["GTA 6 gameplay", "GTA VI funny moments"]
    videos_to_process = []

    ydl_search_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        **get_cookie_opts(),
    }

    with yt_dlp.YoutubeDL(ydl_search_opts) as ydl:
        # 1. Check configured channels if provided
        for ch in channels:
            ch_url = ch if ch.startswith("http") else f"https://www.youtube.com/channel/{ch}/videos"
            try:
                info = ydl.extract_info(ch_url, download=False)
                entries = (info or {}).get("entries") or []
                for entry in entries[:5]:
                    vid = entry.get("id")
                    if vid and vid not in seen_ids:
                        videos_to_process.append({
                            "id": vid,
                            "url": entry.get("url") or f"https://www.youtube.com/watch?v={vid}",
                            "title": entry.get("title") or "GTA 6 Clip",
                            "description": entry.get("description") or "",
                        })
            except Exception as e:
                print(f"⚠️ Channel fetch error ({ch}): {e}")

        # 2. Fallback to YouTube search if no new channel videos found
        if not videos_to_process:
            for query in search_queries:
                try:
                    results = ydl.extract_info(f"ytsearch5:{query}", download=False)
                    entries = (results or {}).get("entries") or []
                    for entry in entries:
                        vid = entry.get("id")
                        if vid and vid not in seen_ids:
                            videos_to_process.append({
                                "id": vid,
                                "url": entry.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
                                "title": entry.get("title") or "GTA 6 Clip",
                                "description": entry.get("description") or "",
                            })
                    if videos_to_process:
                        break
                except Exception as e:
                    print(f"⚠️ Search error for '{query}': {e}")

    if not videos_to_process:
        print("ℹ️ No new unprocessed GTA 6 videos found.")
        return

    for video in videos_to_process:
        vid = video["id"]
        print(f"\n🎬 Evaluating: {video['title']} ({video['url']})")
        category = classify_content(video["title"], video["description"])
        title, description = generate_metadata(video["title"], category)
        output_filename = f"gta6_{vid}_{category}.mp4"

        print(f"🎯 Category: {category}")
        print(f"📝 Title: {title}")
        print(f"🏷️ Description:\n{description}")

        try:
            saved_path = download_and_trim_clip(video["url"], start_sec=10, end_sec=40, output_filename=output_filename)
            upload_to_youtube(saved_path, title, description)
            upload_to_instagram(saved_path, description)

            history.append(vid)
            save_history(history)
            print("✨ Clip processed and saved successfully!")
            break
        except Exception as e:
            print(f"❌ Failed on video {vid}: {e}")
            continue


if __name__ == "__main__":
    main()
