import json
from pathlib import Path
import re
import subprocess
import tempfile


MISSION_RULES = (
    (
        "Lab Fees_替布莱恩收租",
        {"collect brian s rent": 5, "moonstone boutique": 4, "brian s lockup": 4,
         "brian s strong box": 4, "brian s strongbox": 4, "collect acid": 4},
    ),
    (
        "Gear Up_去枪店买武器",
        {"purchase a longarm": 5, "pick up another gun": 3, "chuck s tackle shot": 3},
    ),
    (
        "Pipework_给曼迪修水槽",
        {"fix the sink": 5, "follow mandy": 4, "collect mandy s rent": 3, "pick up the plunger": 4},
    ),
    (
        "Just a Scratch_修皮卡引擎并喷漆",
        {"restore the engine": 5, "coastal auto care": 3, "full engine": 1},
    ),
    (
        "Unwanted Attention_和卡尔去扣车场找车",
        {"drive to hamlet": 5, "search the impound": 5, "take the phoenix": 5,
         "go see cal": 2, "police radio scanner": 3, "hamlet": 1, "phoenix": 2},
    ),
)


def normalize(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def interpret(frames):
    combined = " ".join(normalize(line["text"]) for frame in frames for line in frame)
    scores = [(sum(weight for cue, weight in cues.items() if cue in combined), title)
              for title, cues in MISSION_RULES]
    score, title = max(scores)
    if score >= 3:
        return title

    for frame in frames:
        for line in frame:
            text = line["text"].strip()
            if (
                0.35 < line["x"] < 0.65
                and 0.04 < line["y"] < 0.17
                and 8 <= len(text) <= 48
                and re.match(r"^(Collect|Drive|Follow|Talk|Meet|Enter|Return|Find|Take|Pick up)\b", text)
            ):
                return re.sub(r'[:/\\\x00-\x1f]', " ", text).strip()
    return "游戏实况片段"


def call(arguments, *, timeout):
    return subprocess.run(arguments, capture_output=True, text=True, check=True, timeout=timeout).stdout


class FrameClassifier:
    def __init__(self, work):
        self.work = work
        self.helper = work / "read-frames"

    def classify(self, video):
        if not self.helper.is_file():
            call(["swiftc", "-module-cache-path", self.work / "swift-cache",
                  Path(__file__).with_name("read_frames.swift"), "-o", self.helper], timeout=180)
        duration = float(call(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                               "-of", "csv=p=0", video], timeout=30).strip())
        fractions = (0.01, 0.022, 0.04, 0.1, 0.12, 0.135, 0.18, 0.26, 0.35,
                     0.385, 0.405, 0.45, 0.55, 0.65, 0.75, 0.85, 0.96)
        with tempfile.TemporaryDirectory(prefix="frames-", dir=self.work) as temporary:
            frames = []
            for index, fraction in enumerate(fractions):
                frame = Path(temporary) / f"{index}.jpg"
                call(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(duration * fraction), "-i", video,
                      "-frames:v", "1", "-vf", "scale=w='min(1920,iw)':h=-2",
                      "-q:v", "3", "-update", "1", frame], timeout=45)
                frames.append(frame)
            observations = json.loads(call([self.helper, *frames], timeout=180))
            return interpret(observations)
