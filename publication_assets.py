from pathlib import Path
import re
import subprocess
from unicodedata import east_asian_width


MISSION_LABELS = {
    "Lab Fees": "替布莱恩收租",
    "Gear Up": "置办武器",
    "Pipework": "水管活儿",
    "Just a Scratch": "一点小刮蹭",
    "Unwanted Attention": "惹上麻烦",
}

FONT = "/System/Library/Fonts/STHeiti Medium.ttc"


def clean_text(text):
    return " ".join(re.sub(r"[|｜]", " ", text).split())


def publication_text(sequence, mission, action):
    mission, action = clean_text(mission), clean_text(action)
    heading = MISSION_LABELS.get(mission, mission)
    title = f"第{sequence:03d}集 {heading}"
    if mission in MISSION_LABELS:
        title += f"（{mission}）"
    if action and action != heading:
        title += f" {action}"
    return title[:80], heading, "" if action == heading else action


def recording_text(entry):
    clip = Path(entry["filename"]).stem.split("_", 2)[2]
    mission, _, action = clip.partition("_")
    return publication_text(entry["sequence"], mission, action.replace("_", " "))


def point_size(text, maximum):
    width = sum(1 if east_asian_width(char) in ("F", "W") else 0.58 for char in text)
    return str(min(maximum, round(705 / max(width, 1))))


def render_cover(video, output, sequence, heading, action, *, at=None, series="游戏实况"):
    if at is None:
        duration = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
            text=True, timeout=30,
        )
        at = float(duration.strip()) * 0.36
    frame = subprocess.check_output(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", str(at), "-i", video, "-frames:v", "1",
         "-vf", "scale=1280:800:force_original_aspect_ratio=increase,crop=1280:800",
         "-f", "image2pipe", "-c:v", "mjpeg", "-threads", "1", "-"],
        timeout=60,
    )
    subprocess.run(
        ["magick", "jpeg:-", "-colorspace", "sRGB", "-modulate", "110,110,100",
         "(", "-size", "800x910", "gradient:#0d0915ed-#0d091500", "-rotate", "-90",
          "-gravity", "West", "-background", "none", "-extent", "1280x800", ")",
         "-compose", "Over", "-composite",
         "-fill", "#ffba8d", "-draw", "rectangle 85,286 154,293",
         "-font", FONT, "-gravity", "NorthWest",
         "-pointsize", "32", "-annotate", "+85+313", f"{series}   第{sequence:03d}集",
         "-pointsize", point_size(heading, 104), "-fill", "#fff8f1",
         "-stroke", "#fff8f1", "-strokewidth", "1.8", "-annotate", "+84+376", f" {heading}",
         "-stroke", "none", "-pointsize", point_size(action, 48), "-fill", "#fff1dc",
         "-annotate", "+85+515", f" {action}",
         "-sampling-factor", "4:4:4", "-quality", "91", "-strip", f"jpg:{output}"],
        input=frame, check=True, capture_output=True, timeout=60,
    )
