import os
import re
import json
import time
import subprocess
import requests
from datetime import datetime, timezone
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from youtube_transcript_api import YouTubeTranscriptApi

# ==========================================
# CONFIGURATION & SECRETS (Loaded from GitHub Secrets)
# ==========================================
YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
TARGET_YT_CHANNEL_IDS = [c.strip() for c in os.environ.get("SOURCE_YT_CHANNEL_IDS", "").split(",") if c.strip()]
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# Your Upload Credentials
YT_CLIENT_ID = os.environ["YT_CLIENT_ID"]
YT_CLIENT_SECRET = os.environ["YT_CLIENT_SECRET"]
YT_REFRESH_TOKEN = os.environ["YT_REFRESH_TOKEN"]

IG_USER_ID = os.environ["IG_USER_ID"]
IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]

# GitHub Repo info (for temporary public MP4 hosting for Instagram Graph API)
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]

HISTORY_FILE = "posted_history.json"

CATEGORIES = [
    "funny", "stunt", "chaotic", "weird", "glitch", "beautiful",
    "clutch", "fail", "police_chase", "rage", "cry", "troll", "speedrun"
]

CATEGORY_HASHTAGS = {
    "funny": "#GTA6Funny #GTA6Moments #GamingMemes #GTAVI",
    "stunt": "#GTA6Stunts #GTA6Wins #GamingClips #GTAVI",
    "chaotic": "#GTA6Chaos #GTA6Mayhem #GTAVI #RockstarGames",
    "weird": "#GTA6Weird #GTA6Physics #GTAVI #GamingFails",
    "glitch": "#GTA6Glitch #GTA6Bugs #GTAVI #GameGlitches",
    "beautiful": "#GTA6Graphics #GTA64K #ViceCity #GTAVI",
    "clutch": "#GTA6Clutch #GamingClutch #GTAVI #ProGamer",
    "fail": "#GTA6Fails #InstantKarma #GTAVI #GamingFails",
    "police_chase": "#GTA6PoliceChase #5Stars #GTAVI #ViceCity",
    "rage": "#GTA6Rage #StreamerRage #GTAVI #TwitchClips",
    "cry": "#GTA6Story #GTA6Emotional #GTAVI #RockstarGames",
    "troll": "#GTA6Troll #GTA6Online #GTAVI #GamingClips",
    "speedrun": "#GTA6Speedrun #WorldRecord #GTAVI #Speedrunning"
}


def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return []


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history[-500:], f, indent=2)


def format_timestamp(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


# ==========================================
# 1. DISCOVER TRENDING GTA 6 VIDEOS & STREAMS
# ==========================================
def fetch_youtube_candidates():
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    candidates = []

    # A. Search popular recent GTA 6 videos/VODs
    search_resp = youtube.search().list(
        q="GTA 6 gameplay OR GTA VI stream",
        part="snippet",
        type="video",
        order="viewCount",
        publishedAfter=(datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")),
        videoDuration="medium",
        maxResults=10
    ).execute()

    for item in search_resp.get("items", []):
        candidates.append({
            "platform": "YouTube",
            "id": item["id"]["videoId"],
            "url": f"https://www.youtube.com/watch?v={item['id']['videoId']}",
            "title": item["snippet"]["title"],
            "creator": item["snippet"]["channelTitle"]
        })

    # B. Also check your specified popular creator channel IDs
    for ch_id in TARGET_YT_CHANNEL_IDS:
        ch_resp = youtube.search().list(
            channelId=ch_id,
            part="snippet",
            type="video",
            order="date",
            maxResults=3
        ).execute()
        for item in ch_resp.get("items", []):
            candidates.append({
                "platform": "YouTube",
                "id": item["id"]["videoId"],
                "url": f"https://www.youtube.com/watch?v={item['id']['videoId']}",
                "title": item["snippet"]["title"],
                "creator": item["snippet"]["channelTitle"]
            })

    return candidates


def fetch_twitch_top_clips():
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return []

    # Get OAuth App Token
    token_resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials"
        }
    ).json()
    access_token = token_resp.get("access_token")
    if not access_token:
        return []

    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {access_token}"
    }

    # Lookup Grand Theft Auto VI Game ID on Twitch
    game_resp = requests.get(
        "https://api.twitch.tv/helix/games",
        headers=headers,
        params={"name": "Grand Theft Auto VI"}
    ).json()
    if not game_resp.get("data"):
        return []
    game_id = game_resp["data"][0]["id"]

    # Fetch top trending clips from last 24h
    clips_resp = requests.get(
        "https://api.twitch.tv/helix/clips",
        headers=headers,
        params={"game_id": game_id, "first": 10}
    ).json()

    results = []
    for clip in clips_resp.get("data", []):
        results.append({
            "platform": "Twitch",
            "id": clip["id"],
            "url": clip["url"],
            "title": clip["title"],
            "creator": clip["broadcaster_name"],
            "duration": int(clip["duration"]),
            "vod_offset": clip.get("vod_offset", 0)
        })
    return results


# ==========================================
# 2. DETECT BEST MOMENT & CLASSIFY CATEGORY
# ==========================================
def analyze_youtube_video(video: dict):
    """Uses YouTube Most-Replayed heatmap + transcript + Gemini AI to pick the exact timestamp."""
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video["id"])
        # Format first ~15 mins of transcript with timestamps
        formatted_lines = []
        for entry in transcript_list:
            if entry["start"] <= 1800:
                formatted_lines.append(f"[{int(entry['start'])}s] {entry['text']}")
        transcript_text = "\n".join(formatted_lines[:250])
    except Exception:
        transcript_text = f"Video Title: {video['title']} by {video['creator']} (No captions available; inspect middle peak)."

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    You are an expert viral GTA 6 Shorts curator. Analyze this video metadata and transcript to find the SINGLE best 35-to-55 second viral clip moment.

    Creator: {video['creator']}
    Title: {video['title']}
    Transcript:
    {transcript_text}

    Classify the moment into EXACTLY ONE of these categories:
    {", ".join(CATEGORIES)}

    Return ONLY valid JSON with these keys:
    {{
      "start_sec": <integer start second>,
      "end_sec": <integer end second, must be 30 to 55 seconds after start_sec>,
      "category": "<one of the 13 categories>",
      "viral_title": "<catchy short title under 65 chars>",
      "reason": "<1 sentence why this moment fits the category>"
    }}
    """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    return json.loads(response.text)


def classify_twitch_clip(clip: dict):
    """Classifies an existing viral Twitch clip into one of the 13 categories."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    Classify this viral GTA 6 Twitch clip into EXACTLY ONE category from:
    {", ".join(CATEGORIES)}

    Streamer: {clip['creator']}
    Clip Title: {clip['title']}

    Return ONLY valid JSON:
    {{
      "category": "<one of the 13 categories>",
      "viral_title": "<catchy short title under 65 chars>",
      "reason": "<1 sentence summary>"
    }}
    """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    data = json.loads(response.text)
    start_sec = clip.get("vod_offset") or 0
    end_sec = start_sec + min(int(clip.get("duration", 45)), 58)
    data["start_sec"] = start_sec
    data["end_sec"] = end_sec
    return data


# ==========================================
# 3. DOWNLOAD 4K/1080P (16:9) SEGMENT WITH FULL AUDIO
# ==========================================
def download_hd_clip(url: str, start_sec: int, end_sec: int, output_path: str, is_twitch_clip=False):
    cookies_arg = ["--cookies", "cookies.txt"] if os.path.exists("cookies.txt") else []

    cmd = [
        "yt-dlp",
        *cookies_arg,
        # Prefer 4K (2160p) or 1080p MP4/bestvideo + bestaudio, keeping original 16:9 aspect ratio
        "-f", "bestvideo[height<=2160][height>=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height>=1080]+bestaudio/best",
        "--merge-output-format", "mp4",
        # Re-encode audio to AAC 48kHz stereo & faststart moov atom for Instagram Reels + YouTube compatibility
        "--postprocessor-args", "ffmpeg:-c:v libx264 -preset fast -crf 18 -c:a aac -b:a 192k -ar 48000 -movflags +faststart",
        "-o", output_path
    ]

    if not is_twitch_clip and end_sec > start_sec:
        cmd.extend(["--download-sections", f"*{start_sec}-{end_sec}"])

    cmd.append(url)
    subprocess.run(cmd, check=True)


# ==========================================
# 4. UPLOAD TO YOUTUBE & INSTAGRAM REELS
# ==========================================
def upload_to_youtube(video_path: str, title: str, description: str, tags: list):
    creds = Credentials(
        token=None,
        refresh_token=YT_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YT_CLIENT_ID,
        client_secret=YT_CLIENT_SECRET
    )
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags,
            "categoryId": "20"  # Gaming
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False
        }
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    return f"https://youtu.be/{response['id']}"


def host_temp_mp4_on_github_release(video_path: str) -> tuple[str, str]:
    """Uploads the MP4 temporarily to a GitHub Release so Instagram Graph API can fetch its public URL."""
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    tag_name = f"temp-reel-{int(time.time())}"
    rel_resp = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases",
        headers=headers,
        json={"tag_name": tag_name, "name": tag_name, "prerelease": True}
    ).json()

    upload_url = rel_resp["upload_url"].split("{")[0]
    release_id = str(rel_resp["id"])

    with open(video_path, "rb") as f:
        asset_resp = requests.post(
            f"{upload_url}?name=clip.mp4",
            headers={**headers, "Content-Type": "video/mp4"},
            data=f
        ).json()

    return asset_resp["browser_download_url"], release_id


def cleanup_temp_github_release(release_id: str):
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"}
    requests.delete(f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/{release_id}", headers=headers)


def upload_to_instagram_reels(video_path: str, caption: str):
    public_mp4_url, release_id = host_temp_mp4_on_github_release(video_path)
    try:
        # Step 1: Create Reels Media Container
        create_url = f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media"
        res = requests.post(create_url, data={
            "media_type": "REELS",
            "video_url": public_mp4_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": IG_ACCESS_TOKEN
        }).json()

        container_id = res.get("id")
        if not container_id:
            raise RuntimeError(f"Instagram container error: {res}")

        # Step 2: Poll until video processing finishes
        for _ in range(20):
            status_res = requests.get(
                f"https://graph.facebook.com/v21.0/{container_id}",
                params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN}
            ).json()
            if status_res.get("status_code") == "FINISHED":
                break
            if status_res.get("status_code") == "ERROR":
                raise RuntimeError(f"Instagram processing failed: {status_res}")
            time.sleep(6)

        # Step 3: Publish Reel
        pub_res = requests.post(
            f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media_publish",
            data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN}
        ).json()
        return pub_res.get("id")
    finally:
        cleanup_temp_github_release(release_id)


# ==========================================
# MAIN EXECUTION PIPELINE
# ==========================================
def run():
    history = load_history()
    posted_ids = {item["source_id"] for item in history}

    # Combine Twitch Top Clips & YouTube Candidates
    candidates = fetch_twitch_top_clips() + fetch_youtube_candidates()
    selected = None
    analysis = None

    for item in candidates:
        if item["id"] in posted_ids:
            continue
        try:
            if item["platform"] == "Twitch":
                analysis = classify_twitch_clip(item)
            else:
                analysis = analyze_youtube_video(item)

            if analysis.get("category") in CATEGORIES:
                selected = item
                break
        except Exception as e:
            print(f"Skipping {item['id']}: {e}")

    if not selected or not analysis:
        print("No new qualifying GTA 6 moments found in this cycle.")
        return

    category = analysis["category"]
    start_ts = format_timestamp(analysis["start_sec"])
    end_ts = format_timestamp(analysis["end_sec"])
    output_file = "final_clip.mp4"

    # Download clip in 1080p/4K 16:9 with audio
    download_hd_clip(
        url=selected["url"],
        start_sec=analysis["start_sec"],
        end_sec=analysis["end_sec"],
        output_path=output_file,
        is_twitch_clip=(selected["platform"] == "Twitch")
    )

    cat_tags = CATEGORY_HASHTAGS.get(category, "#GTA6 #GTAVI")
    base_hashtags = f"#GTA6 #GTAVI #GrandTheftAuto6 #Shorts #Reels {cat_tags}"
    full_title = f"{analysis['viral_title']} [{category.upper()}] #GTA6"

    description = (
        f"{analysis['viral_title']}\n\n"
        f"Category: #{category}\n"
        f"Creator: {selected['creator']}\n"
        f"Source Stream/Video: {selected['title']}\n"
        f"Timestamp: {start_ts} - {end_ts}\n"
        f"Original Link: {selected['url']}\n\n"
        f"{base_hashtags}"
    )

    # Upload to YouTube & Instagram
    yt_url = upload_to_youtube(
        video_path=output_file,
        title=full_title,
        description=description,
        tags=["GTA 6", "GTA VI", category, selected["creator"], "GTA 6 Clips", "Rockstar Games"]
    )
    ig_media_id = upload_to_instagram_reels(video_path=output_file, caption=description)

    # Log Source Metadata Report (Visible in GitHub Actions Summary)
    report = (
        f"### Clip Uploaded Successfully\n"
        f"- **Category:** `{category}`\n"
        f"- **Creator Name:** {selected['creator']}\n"
        f"- **Stream / Video Name:** {selected['title']}\n"
        f"- **Timestamp Taken From:** `{start_ts} -> {end_ts}`\n"
        f"- **Source URL:** {selected['url']}\n"
        f"- **YouTube Upload:** {yt_url}\n"
        f"- **Instagram Reel ID:** `{ig_media_id}`\n"
    )
    print(report)
    if "GITHUB_STEP_SUMMARY" in os.environ:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary_file:
            summary_file.write(report + "\n")

    # Save to history so duplicates are never uploaded
    history.append({
        "source_id": selected["id"],
        "creator": selected["creator"],
        "stream_title": selected["title"],
        "category": category,
        "timestamp": f"{start_ts}-{end_ts}",
        "uploaded_at": datetime.now(timezone.utc).isoformat()
    })
    save_history(history)


if __name__ == "__main__":
    run()
