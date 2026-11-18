import os
import json
import subprocess
import requests
import yt_dlp
from google import genai
from google.genai import errors

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
# 4. DYNAMIC TITLE & HASHTAG GENERATOR
# ==========================================
def generate_metadata(original_title: str, category: str):
    """Generates optimized titles with relevant hashtags for Shorts/Reels."""
    base_tags = "#GTA6 #GrandTheftAuto6 #Shorts #Reels"
    
    category_tags = {
        "police_chase": "#PoliceChase #GTA6Gameplay #Wanted",
        "glitch": "#Glitch #GTA6Bugs #FunnyMoments",
        "funny": "#Funny #GTA6Moments #Lol",
        "stunt": "#Stunts #GTA6Stunts #Epic",
        "clutch": "#Clutch #GamingMoments #Surviving",
        "easter_egg": "#EasterEgg #Secrets #GTA6Secrets"
    }
    
    cat_tag = category_tags.get(category, "#Gaming #Trending")
    title = f"{original_title[:65]}... | GTA 6 #{category.capitalize()} #Shorts"
    description = f"Insane GTA 6 moment captured! Category: {category.upper()}\n\n{base_tags} {cat_tag}"
    
    return title, description


# ==========================================
# 5. VIDEO PROCESSING & LOCAL SAVING (UNCRROPPED)
# ==========================================
def download_and_trim_clip(video_url: str, start_sec: int, end_sec: int, output_filename: str):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    file_path = os.path.join(OUTPUT_DIR, output_filename)
    duration = end_sec - start_sec
    
    ydl_opts = {
        'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
        'outtmpl': os.path.join(OUTPUT_DIR, 'temp_source.mp4'),
        'quiet': True,
    }

    print(f"📥 Downloading source segment ({start_sec}s - {end_sec}s)...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])

    print(f"✂️ Trimming clip locally (uncropped resolution)...")
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_sec),
        "-i", os.path.join(OUTPUT_DIR, 'temp_source.mp4'),
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "22",
        "-c:a", "aac",
        file_path
    ]
    
    result = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr.decode('utf-8')}")
    
    print(f"✅ Clip saved successfully to local path: {file_path}")
    return file_path


# ==========================================
# 6. API UPLOAD STUBS (YOUTUBE & INSTAGRAM)
# ==========================================
def upload_to_youtube(file_path: str, title: str, description: str):
    """Integration hook for YouTube Data API v3 Shorts upload."""
    print(f"🚀 Uploading '{title}' to YouTube Shorts...")
    # Add your google-api-python-client upload code here using youtube.videos().insert()
    pass

def upload_to_instagram(file_path: str, caption: str):
    """Integration hook for Instagram Graph API Reels upload."""
    print(f"🚀 Uploading clip to Instagram Reels...")
    # Add your Meta Graph API container creation & publishing code here
    pass


# ==========================================
# 7. MAIN PIPELINE EXECUTION
# ==========================================
def main():
    print("🚀 Starting GTA 6 Automated Clipper & Downloader Pipeline...")
    history = load_history()
    
    search_queries = ["GTA 6 gameplay highlights", "GTA VI funny moments"]
    videos_to_process = []

    ydl_search_opts = {
        'extract_flat': True,
        'max_downloads': 3,
        'quiet': True,
    }

    print("🔍 Searching for fresh GTA 6 content...")
    with yt_dlp.YoutubeDL(ydl_search_opts) as ydl:
        for query in search_queries[:1]:
            try:
                search_results = ydl.extract_info(f"ytsearch3:{query}", download=False)
                if 'entries' in search_results:
                    for entry in search_results['entries']:
                        video_id = entry.get('id')
                        if video_id not in history:
                            videos_to_process.append({
                                'id': video_id,
                                'url': f"https://www.youtube.com/watch?v={video_id}",
                                'title': entry.get('title', 'GTA 6 Clip'),
                                'description': entry.get('description', '')
                            })
            except Exception as e:
                print(f"⚠️ Search error: {e}")

    if not videos_to_process:
        print("No new un-processed GTA 6 videos found in this cycle.")
        return

    # Process the top candidate
    video = videos_to_process[0]
    print(f"\nProcessing Video: {video['title']} ({video['url']})")
    
    category = classify_content(video['title'], video['description'])
    title, description = generate_metadata(video['title'], category)
    
    print(f"🎯 Assigned Category: {category}")
    print(f"📝 Generated Title: {title}")
    
    start_sec = 10
    end_sec = 40
    output_filename = f"gta6_{video['id']}_{category}.mp4"
    
    try:
        local_file_path = download_and_trim_clip(video['url'], start_sec, end_sec, output_filename)
        
        # Trigger Uploads
        upload_to_youtube(local_file_path, title, description)
        upload_to_instagram(local_file_path, description)
        
        history.append(video['id'])
        save_history(history)
        print("✨ Pipeline cycle finished successfully!")
        
    except Exception as e:
        print(f"❌ Pipeline execution failed: {e}")

if __name__ == "__main__":
    main()
