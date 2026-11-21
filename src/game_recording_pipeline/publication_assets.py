from datetime import datetime
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

FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
DARK = "#100d18"
YELLOW = "#ffe35a"


def clean_text(text):
    return " ".join(re.sub(r"[|｜]", " ", text).split())


def clip_text(clip):
    mission, _, action = clip.partition("_")
    mission, action = clean_text(mission), clean_text(action.replace("_", " "))
    if not mission:
        mission, action = action, ""
    if not mission:
        raise ValueError("需要任务名或片段内容")
    heading = mission
    if mission in MISSION_LABELS:
        heading = MISSION_LABELS[mission]
        mission = f"{heading}（{mission}）"
    else:
        chinese, _, english = mission.partition("（")
        if english.endswith("）") and MISSION_LABELS.get(english[:-1]) == chinese:
            heading = chinese
    action = "" if action == heading else action
    return f"{mission}_{action}" if action else mission, heading, action


def recording_stem(recorded_at, sequence, clip):
    label, _, _ = clip_text(clip)
    return f"{recorded_at:%Y%m%d}_{sequence:03d}_{label}"[:80].rstrip("_ ")


def recording_text(entry):
    clip = Path(entry["filename"]).stem.split("_", 2)[2]
    _, heading, action = clip_text(clip)
    title = recording_stem(datetime.fromisoformat(entry["recorded_at"]), entry["sequence"], clip)
    return title, heading, action


def text_width(text):
    return sum(1 if east_asian_width(char) in ("F", "W") else 0.60 for char in text)


def heading_lines(heading):
    if text_width(heading) <= 11:
        return [heading]
    midpoint = len(heading) // 2
    boundary = min((i for i, char in enumerate(heading) if char == " "),
                   key=lambda i: abs(i - midpoint), default=midpoint)
    if abs(boundary - midpoint) > len(heading) // 4:
        boundary = midpoint
    return [heading[:boundary].rstrip(), heading[boundary:].lstrip()]


def outline_text(text, color, size, position):
    return ["-pointsize", str(size), "-fill", color, "-stroke", DARK, "-strokewidth", "14",
            "-annotate", position, text,
            "-stroke", color, "-strokewidth", "5", "-annotate", position, text]


def render_cover(video, output, sequence, heading, action, *, at=None):
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
    badge = f"第{sequence:03d}集"
    badge_size = min(110, round(520 / text_width(badge)))
    badge_right = 184 + round(badge_size * text_width(badge))
    lines = heading_lines(heading)
    title_size = min(164 if len(lines) == 1 else 124,
                     round(1040 / max(max(map(text_width, lines)), 1)))
    text = []
    for index, line in enumerate(lines):
        y = (315 if len(lines) == 1 else 267) + index * (title_size + 13)
        text.extend(outline_text(f" {line}", "#ffffff", title_size, f"+117+{y}"))
    if action:
        action_size = min(93, round(1020 / text_width(action)))
        text.extend(outline_text(f" {action}", "#fff29a", action_size, "+117+545"))
    subprocess.run(
        ["magick", "jpeg:-", "-colorspace", "sRGB", "-modulate", "108,114,100",
         "(", "-size", "800x1280", "gradient:#100d18de-#100d1800", "-rotate", "-90", ")",
         "-compose", "Over", "-composite",
         "-fill", YELLOW, "-stroke", "none", "-draw", f"roundrectangle 115,111 {badge_right},250 21,21",
         "-font", FONT, "-gravity", "NorthWest", "-pointsize", str(badge_size),
         "-fill", DARK, "-stroke", DARK, "-strokewidth", "4.8", "-annotate", "+135+122", badge,
         *text, "-sampling-factor", "4:4:4", "-quality", "91", "-strip", f"jpg:{output}"],
        input=frame, check=True, capture_output=True, timeout=60,
    )
