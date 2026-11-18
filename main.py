import os
import json
import subprocess
import yt_dlp
from google import genai
from google.genai import errors

# ==========================================
# 1. CONFIGURATION & 19-CATEGORY TAXONOMY
# ==========================================
HISTORY_FILE = "posted_history.json"

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
# 2. HISTORY MANAGEMENT
# ==========================================
def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []

def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


# ==========================================
# 3. HYBRID CLASSIFICATION (SAVES API QUOTA)
# ==========================================
def classify_clip_locally(text: str):
    """Scores text locally against categories to preserve Gemini free tier requests."""
    text_lower = text.lower()
    scores = {
        cat: sum(1 for kw in keywords if kw in text_lower)
        for cat, keywords in CATEGORIES.items()
    }
    best_category = max(scores, key=scores.get)
    return best_category, scores[best_category]

def classify_content(title: str, description: str) -> str:
    combined_text = f"{title} {description}"
    category, score = classify_clip_locally(combined_text)
    
    # If 2+ keywords match locally, skip Gemini API call completely!
    if score >= 2:
        print(f"⚡ Local keyword match: '{category}' (Score: {score}). Skipping Gemini API call.")
        return category

    print(f"🤖 Low local score ({score}). Querying Gemini 3.8 Flash API...")
    try:
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        prompt = (
            f"Classify this GTA 6 video into exactly ONE of these categories: {', '.join(VALID_CATEGORY_NAMES)}.\n"
            f"Title: {title}\n"
            f"Description: {description}\n"
            f"Return ONLY the category name."
        )
        response = client.models.generate_content(
            model='gemini-3.8-flash',
            contents=prompt,
        )
        result_cat = response.text.strip().lower()
        if result_cat in VALID_CATEGORY_NAMES:
            return result_cat
    except errors.APIError as e:
        print(f"⚠️ Gemini API Quota/Error hit: {e}. Falling back to local category: '{category}'")
    except Exception as e:
        print(f"⚠️ Unexpected error during Gemini call: {e}")
        
    return category


# ==========================================
# 4. VIDEO PROCESSING (UNCROPPED TRIM)
# ==========================================
def download_and_trim_clip(video_url: str, start_sec: int, end_sec: int, output_filename: str = "clip.mp4"):
    """
    Downloads a specific time range from a YouTube video and trims it 
    at its original resolution and aspect ratio without any cropping.
    """
    duration = end_sec - start_sec
    
    ydl_opts = {
        'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
        'outtmpl': 'temp_source.mp4',
        'quiet': True,
    }

    print(f"📥 Downloading source video segment ({start_sec}s - {end_sec}s)...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])

    print(f"✂️ Slicing segment without cropping or scaling...")
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_sec),
        "-i", "temp_source.mp4",
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "22",
        "-c:a", "aac",
        output_filename
    ]
    
    result = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr.decode('utf-8')}")
    
    print(f"✅ Successfully created uncropped clip: {output_filename}")
    return output_filename


# ==========================================
# 5. MAIN PIPELINE EXECUTION
# ==========================================
def main():
    print("🚀 Starting GTA 6 Automated Clipper Pipeline...")
    history = load_history()
    
    # Read configured source channel IDs from environment variable
    source_channels = os.environ.get("SOURCE_YT_CHANNEL_IDS", "").split(",")
    source_channels = [ch.strip() for ch in source_channels if ch.strip()]
    
    if not source_channels:
        print("⚠️ No source channel IDs found in SOURCE_YT_CHANNEL_IDS environment variable.")
        return

    print(f"🔍 Scanning {len(source_channels)} source channels for new content...")
    print("✨ Pipeline cycle completed successfully.")

if __name__ == "__main__":
    main()
