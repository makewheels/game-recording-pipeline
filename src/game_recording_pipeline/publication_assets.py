from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
from unicodedata import east_asian_width
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


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
COVER_DIRECTORY = "B站封面"
COVER_MARGIN = 176


def clean_text(text):
    return " ".join(re.sub(r"[|｜]", " ", text).split())


def clip_text(clip):
    mission, _, action = clip.partition("_")
    mission = clean_text(re.sub(r"[()（）]", " ", mission))
    action = clean_text(re.sub(r"[()（）_]", " ", action))
    if not mission:
        mission, action = action, ""
    if not mission:
        raise ValueError("需要任务名或片段内容")
    heading = mission
    for english, chinese in MISSION_LABELS.items():
        bilingual = f"{chinese} {english}"
        if mission in (english, bilingual):
            mission, heading = bilingual, chinese
            break
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


def recording_cover_title(entry):
    _, heading, action = recording_text(entry)
    return entry.get("publication", {}).get("cover_title") or action or heading


def text_width(text):
    return sum(1 if east_asian_width(char) in ("F", "W") else 0.60 for char in text)


def cover_heading(heading):
    heading = clean_text(heading) or "游戏实况"
    line_width = 1000
    minimum = 94
    available = line_width / minimum
    if text_width(heading) > available:
        label = ""
        for char in heading:
            if text_width(label + char + "…") > available:
                break
            label += char
        heading = label.rstrip() + "…"
    return heading, min(114, round(line_width / text_width(heading)))


def outline_text(text, font, color, size, position):
    return ["-font", font, "-pointsize", str(size), "-fill", color, "-stroke", DARK, "-strokewidth", "10",
            "-annotate", position, text,
            "-stroke", color, "-strokewidth", "4", "-annotate", position, text]


def extract_cover_frame(video, at=None, *, input_options=()):
    if at is None:
        duration = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
            text=True, timeout=30,
        )
        at = float(duration.strip()) * 0.36
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", *input_options, "-ss", str(at), "-i", video, "-frames:v", "1",
         "-vf", "scale=w='min(1920,iw)':h=-2",
         "-f", "image2pipe", "-c:v", "mjpeg", "-threads", "1", "-"],
        capture_output=True, timeout=60,
    )
    if result.returncode or not result.stdout:
        raise ValueError(f"无法读取 {at:g} 秒的视频帧")
    return result.stdout


def render_cover(video, output, sequence, heading, *, at=None):
    render_cover_frame(extract_cover_frame(video, at), output, sequence, heading)


def bilibili_frame(bvid, at):
    if not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid or "") or at is None:
        raise ValueError("归档录屏需要 BV 号和取帧时间")
    referer = f"https://www.bilibili.com/video/{bvid}/"
    agent = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

    def public_data(route, **query):
        request = Request(f"https://api.bilibili.com{route}?{urlencode(query)}",
                          headers={"User-Agent": agent, "Referer": referer})
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
        if result.get("code") != 0:
            raise ValueError(f"B 站公开视频当前不可读取 {bvid}")
        return result["data"]

    archive = public_data("/x/web-interface/view", bvid=bvid)
    media = public_data("/x/player/playurl", bvid=bvid, cid=archive["cid"], qn=64, fnval=16)
    streams = (media.get("dash") or {}).get("video") or []
    if not streams:
        raise ValueError(f"B 站没有可用的视频流 {bvid}")
    url = max(streams, key=lambda stream: stream["height"])["baseUrl"]
    origin = urlsplit(url)
    if origin.scheme != "https" or not origin.hostname or not origin.hostname.endswith((".bilivideo.com", ".bilivideo.cn")):
        raise ValueError(f"B 站视频流地址不符 {bvid}")
    return extract_cover_frame(url, at, input_options=("-rw_timeout", "12000000", "-user_agent", agent,
                                                        "-referer", referer))


def render_catalog_cover(entry, directory, output):
    publication = entry.get("publication", {})
    video = directory / entry["filename"]
    at = publication.get("cover_frame_seconds")
    title = recording_cover_title(entry)
    if publication.get("cover_frame_source") != "bilibili" and video.is_file():
        render_cover(video, output, entry["sequence"], title, at=at)
        return
    bvid = entry.get("uploads", {}).get("bilibili", {}).get("bvid")
    render_cover_frame(bilibili_frame(bvid, at), output, entry["sequence"], title)


def text_layer(text, font, color, size):
    return ["(", "-size", f"{COVER_WIDTH}x320", "xc:none", "-gravity", "Forget",
            *outline_text(text, font, color, size, "+12+240"), "-trim", "+repage", ")"]


def render_cover_frame(frame, output, sequence, heading):
    label, size = cover_heading(heading)
    canvas = f"{COVER_WIDTH}x{COVER_HEIGHT}"
    subprocess.run(
        ["magick", "jpeg:-", "-colorspace", "sRGB", "-resize", f"{canvas}^",
         "-gravity", "Center", "-extent", canvas, "-modulate", "106,110,100",
         "(", "-size", f"{COVER_WIDTH}x520", "gradient:#100d1800-#100d18ed",
         "-gravity", "South", "-background", "none", "-extent", canvas, ")",
         "-compose", "Over", "-composite",
         *text_layer(f"{sequence:03d}", NUMBER_FONT, "#f6d89a", 198),
         "-gravity", "NorthWest", "-geometry", f"+140+{COVER_MARGIN}", "-composite",
         *text_layer(label, FONT, "#ffffff", size),
         "-gravity", "SouthWest", "-geometry", f"+140+{COVER_MARGIN}", "-composite", "+repage",
         "-sampling-factor", "4:4:4", "-quality", "91", "-strip", f"jpg:{output}"],
        input=frame, check=True, capture_output=True, timeout=60,
    )
