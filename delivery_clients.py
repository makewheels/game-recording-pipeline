import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile


class DeliveryError(RuntimeError):
    pass


def execute(arguments, work, timeout=None):
    return subprocess.run(arguments, cwd=work, capture_output=True, text=True, timeout=timeout)


def decode_json(output):
    for match in re.finditer(r"(?m)^[\[{]", output):
        try:
            return json.JSONDecoder().raw_decode(output[match.start():])[0]
        except ValueError:
            continue
    raise DeliveryError("服务未返回可读取的回执")


class Baidu:
    def __init__(self, work, directory):
        self.work = work
        self.directory = directory

    def request(self, *arguments, timeout=None):
        command = ["bdpan", *arguments, "--json", "--no-check-update"]
        return decode_json(execute(command, self.work, timeout).stdout)

    def lookup(self, target, *, directory=False):
        data = self.request("ls", target, timeout=60)
        expected = f"/apps/bdpan/{target}"
        if isinstance(data, list):
            matches = [item for item in data if item["path"] == expected and item["isdir"] == directory]
            if len(matches) == 1:
                return matches[0]
        if isinstance(data, dict) and re.search(r"错误码 -9[)），]|^.*\(-9\)", str(data.get("error", ""))):
            return None
        raise DeliveryError(f"百度网盘目录或文件查询失败 {target}")

    def ensure_directory(self):
        if self.lookup(self.directory, directory=True) is None:
            self.request("mkdir", self.directory, timeout=60)
            if self.lookup(self.directory, directory=True) is None:
                raise DeliveryError(f"百度网盘目录创建失败 {self.directory}")

    def upload(self, video, entry):
        self.ensure_directory()
        target = f"{self.directory}/{video.name}"
        item = self.lookup(target)
        if item is None:
            self.request("upload", str(video), target)
            item = self.lookup(target)
        if item is None or item["size"] != entry["size_bytes"]:
            raise DeliveryError("百度网盘未返回匹配的文件大小")
        with video.open("rb") as stream:
            local_md5 = hashlib.file_digest(stream, "md5").hexdigest()
        receipt = {"path": f"我的应用数据/bdpan/{target}", "size_bytes": item["size"]}
        if item.get("md5", "").lower() == local_md5:
            return receipt | {"verified_md5": local_md5}

        with tempfile.TemporaryDirectory(prefix="baidu-check-", dir=self.work) as temporary:
            copy = Path(temporary) / video.name
            self.request("download", target, str(copy))
            if not copy.is_file():
                raise DeliveryError("百度网盘没有提供可核对的文件副本")
            with copy.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                    raise DeliveryError("百度网盘副本的 SHA-256 不一致")
        return receipt | {"verified_sha256": entry["sha256"], "remote_identity": self.identity(item)}

    @staticmethod
    def identity(item):
        return [item["fsid"], item["server_mtime"], item["size"], item.get("md5", "")]

    def confirmed(self, name, receipt):
        item = self.lookup(f"{self.directory}/{name}")
        if not item or item["size"] != receipt["size_bytes"]:
            return False
        if "verified_md5" in receipt:
            return item.get("md5", "").lower() == receipt["verified_md5"]
        return bool(receipt.get("verified_sha256") and self.identity(item) == receipt.get("remote_identity"))


BV_PATTERN = re.compile(r"BV[A-Za-z0-9]{10}\b")
ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
TECHNICAL_REJECTION = re.compile(r"转码失败|视频编码不支持|音频编码不支持|封装格式不支持|分辨率不符合")


class Bilibili:
    def __init__(self, work, cookies, partition, tags, line):
        self.work = work
        self.prefix = ["biliup", "-u", str(cookies)]
        self.partition = partition
        self.tags = tags
        self.line = line

    def find(self, title):
        result = execute([*self.prefix, "list", "-m", "3"], self.work, 90)
        if result.returncode:
            raise DeliveryError("哔哩哔哩稿件列表获取失败")
        matches = []
        for line in ANSI_PATTERN.sub("", result.stdout).splitlines():
            fields = line.split("\t")
            if len(fields) >= 2 and fields[1] == title and BV_PATTERN.fullmatch(fields[0]):
                matches.append(fields[0])
        if len(matches) > 1:
            raise DeliveryError("哔哩哔哩存在同名稿件，请核对 BV 号")
        return matches[0] if matches else None

    def review(self, bvid):
        result = execute([*self.prefix, "show", bvid], self.work, 90)
        if result.returncode:
            raise DeliveryError(f"哔哩哔哩稿件详情暂不可读 {bvid}")
        archive = decode_json(result.stdout).get("archive", {})
        state = archive.get("state")
        reason = archive.get("reject_reason") or archive.get("state_desc") or ""
        if state == 0:
            return "published", ""
        if state == -2 or any(word in reason for word in ("审核不通过", "退回", "打回")):
            return "rejected", reason
        return "reviewing", reason

    def submit(self, video, title, description, *, cover):
        arguments = [*self.prefix, "upload", "--submit", "web", "--tid", str(self.partition),
                     "--tag", self.tags, "--title", title, "--desc", description, "--cover", str(cover)]
        if self.line:
            arguments.extend(["--line", self.line])
        result = execute([*arguments, "--", str(video)], self.work)
        output = result.stdout + "\n" + result.stderr
        if result.returncode == 0:
            match = BV_PATTERN.search(output)
            return match.group(0) if match else self.find(title)
        existing = self.find(title)
        if existing:
            return existing
        if "All files uploaded successfully" not in output and (
            "Failed to pre_upload" in output or "transfer error" in output.lower()
        ):
            raise DeliveryError("哔哩哔哩文件传输失败，可重试")
        raise DeliveryError("哔哩哔哩投稿结果待核对")

    def transcode(self, source, output):
        result = execute(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(source),
             "-map", "0:v:0", "-map", "0:a:0?", "-vf",
             "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2",
             "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output)],
            self.work,
        )
        if result.returncode or not output.is_file():
            raise DeliveryError("技术退回后的转码失败")
