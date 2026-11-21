from datetime import datetime
from pathlib import Path
import re
import subprocess
from unicodedata import east_asian_width


MISSION_LABELS = {
    "Prologue": "序章",
    "Gone Fishing": "出海钓鱼",
    "Lab Fees": "替布莱恩收租",
    "Gear Up": "置办武器",
    "Pipework": "水管活儿",
    "Just a Scratch": "一点小刮蹭",
    "Unwanted Attention": "惹上麻烦",
}

FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
NUMBER_FONT = "/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf"
DARK = "#100d18"
COVER_WIDTH = 1280
COVER_HEIGHT = 800


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


def cover_line(sequence, heading):
    heading = clean_text(heading) or "游戏实况"
    number = f"{sequence:03d}"
    prefix_width = len(number) * 0.49 + 0.33
    line_width = 1030
    minimum = 94
    available = line_width / minimum - prefix_width
    if text_width(heading) > available:
        label = ""
        for char in heading:
            if text_width(label + char + "…") > available:
                break
            label += char
        heading = label.rstrip() + "…"
    size = min(144, round(line_width / (prefix_width + text_width(heading))))
    return number, heading, size, round(prefix_width * size)


def outline_text(text, font, color, size, position):
    return ["-font", font, "-pointsize", str(size), "-fill", color, "-stroke", DARK, "-strokewidth", "10",
            "-annotate", position, text,
            "-stroke", color, "-strokewidth", "4", "-annotate", position, text]


def render_cover(video, output, sequence, heading, *, at=None):
    if at is None:
        duration = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
            text=True, timeout=30,
        )
        at = float(duration.strip()) * 0.36
    frame = subprocess.check_output(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", str(at), "-i", video, "-frames:v", "1",
         "-vf", "scale=w='min(1920,iw)':h=-2",
         "-f", "image2pipe", "-c:v", "mjpeg", "-threads", "1", "-"],
        timeout=60,
    )
    render_cover_frame(frame, output, sequence, heading)


def render_cover_frame(frame, output, sequence, heading):
    number, label, size, offset = cover_line(sequence, heading)
    canvas = f"{COVER_WIDTH}x{COVER_HEIGHT}"
    x, baseline = 116, 630
    subprocess.run(
        ["magick", "jpeg:-", "-colorspace", "sRGB", "-resize", f"{canvas}^",
         "-gravity", "Center", "-extent", canvas, "-modulate", "106,110,100",
         "(", "-size", f"{COVER_WIDTH}x520", "gradient:#100d1800-#100d18ed",
         "-gravity", "South", "-background", "none", "-extent", canvas, ")",
         "-compose", "Over", "-composite",
         "-gravity", "Forget",
         *outline_text(number, NUMBER_FONT, "#f6d89a", size, f"+{x}+{baseline}"),
         *outline_text(label, FONT, "#ffffff", size, f"+{x+offset}+{baseline}"),
         "-sampling-factor", "4:4:4", "-quality", "91", "-strip", f"jpg:{output}"],
        input=frame, check=True, capture_output=True, timeout=60,
    )
