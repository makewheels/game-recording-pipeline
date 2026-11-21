import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


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
        arguments = ("ls", target, "--folder") if directory else ("ls", target)
        data = self.request(*arguments, timeout=60)
        expected = f"/apps/bdpan/{target}"
        if isinstance(data, list):
            matches = [item for item in data if item["path"] == expected and item["isdir"] == directory]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                return None
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

    def find(self, title, *, exclude=None):
        result = execute([*self.prefix, "list", "-m", "3"], self.work, 90)
        if result.returncode:
            raise DeliveryError("哔哩哔哩稿件列表获取失败")
        matches = []
        for line in ANSI_PATTERN.sub("", result.stdout).splitlines():
            fields = line.split("\t")
            if len(fields) >= 2 and fields[1] == title and fields[0] != exclude and BV_PATTERN.fullmatch(fields[0]):
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

    def submit(self, video, title, description, *, cover, exclude=None):
        arguments = [*self.prefix, "upload", "--submit", "web", "--tid", str(self.partition),
                     "--tag", self.tags, "--title", title, "--desc", description, "--cover", str(cover)]
        if self.line:
            arguments.extend(["--line", self.line])
        result = execute([*arguments, "--", str(video)], self.work)
        output = result.stdout + "\n" + result.stderr
        if result.returncode == 0:
            match = BV_PATTERN.search(output)
            return match.group(0) if match and match.group(0) != exclude else self.find(title, exclude=exclude)
        existing = self.find(title, exclude=exclude)
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


class BilibiliCollection:
    MEMBER = "https://member.bilibili.com"
    PUBLIC = "https://api.bilibili.com"
    USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

    def __init__(self, cookies, collection_id, section_id):
        if any(type(value) is not int or value <= 0 for value in (collection_id, section_id)):
            raise ValueError("B 站合集 ID 和小节 ID 必须是正整数")
        self.cookies = cookies
        self.collection_id = collection_id
        self.section_id = section_id

    def credentials(self):
        try:
            info = json.loads(self.cookies.read_text(encoding="utf-8"))
            cookies = {c["name"]: c["value"] for c in info["cookie_info"]["cookies"]}
            owner = int(cookies["DedeUserID"])
            csrf = cookies["bili_jct"]
            session = cookies["SESSDATA"]
        except (OSError, TypeError, ValueError, KeyError) as exc:
            raise DeliveryError("B 站合集操作需要有效的 biliup 登录信息") from exc
        headers = {"Cookie": f"SESSDATA={session}; bili_jct={csrf}; DedeUserID={owner}",
                   "Origin": self.MEMBER, "Referer": self.MEMBER + "/platform/upload-manager/season"}
        return headers, csrf, owner

    def request(self, origin, route, query, headers, payload=None):
        headers = {"User-Agent": self.USER_AGENT} | headers
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        try:
            request = Request(f"{origin}{route}?{urlencode(query)}", headers=headers, data=body)
            with urlopen(request, timeout=20) as response:
                result = json.load(response)
        except (OSError, ValueError) as exc:
            # 变更请求的 URL 含有 CSRF 令牌，错误里只保留状态码。
            status = getattr(exc, "code", None) or type(exc).__name__
            raise DeliveryError(f"B 站合集请求失败 {status}") from None
        if not isinstance(result, dict):
            raise DeliveryError("B 站合集未返回可读取的数据")
        if result.get("code") != 0:
            raise DeliveryError(f'B 站合集请求失败 {result.get("code")} {result.get("message", "")}')
        return result.get("data")

    def episodes(self, headers):
        data = self.request(self.MEMBER, "/x2/creative/web/season/section", {"id": self.section_id}, headers)
        section = data.get("section", {}) if isinstance(data, dict) else {}
        if section.get("id") != self.section_id or section.get("seasonId") != self.collection_id:
            raise DeliveryError("配置的 B 站小节与合集不匹配")
        return sorted(data.get("episodes") or [], key=lambda episode: episode["order"])

    def new_episode(self, bvid, owner):
        data = self.request(self.PUBLIC, "/x/web-interface/view", {"bvid": bvid},
                            {"Referer": "https://www.bilibili.com/"})
        if not isinstance(data, dict) or data.get("bvid") != bvid or data.get("owner", {}).get("mid") != owner:
            raise DeliveryError(f"B 站稿件与当前登录账号不匹配 {bvid}")
        if data.get("season_id") or data.get("ugc_season"):
            raise DeliveryError(f"B 站稿件已经位于某个合集小节 {bvid}")
        if len(data.get("pages", [])) != 1:
            raise DeliveryError(f"B 站合集自动添加需要单个分 P 的稿件 {bvid}")
        return {key: data[key] for key in ("aid", "cid", "title")} | {"charging_pay": 0}

    def ensure(self, bvids):
        if not bvids:
            return {}
        if len(set(bvids)) != len(bvids) or not all(BV_PATTERN.fullmatch(bvid) for bvid in bvids):
            raise ValueError("B 站合集列表里的 BV 号无效或有重复")
        headers, csrf, owner = self.credentials()
        existing = [e for e in self.episodes(headers) if e["bvid"] in bvids]
        if [e["bvid"] for e in existing] != bvids[:len(existing)]:
            raise DeliveryError("B 站合集的现有稿件顺序与清单不一致")
        missing = bvids[len(existing):]
        if missing:
            episodes = [self.new_episode(bvid, owner) for bvid in missing]
            self.request(self.MEMBER, "/x2/creative/web/season/section/episodes/add", {"csrf": csrf}, headers,
                         {"sectionId": self.section_id, "episodes": episodes})
            for attempt in range(4):
                existing = [e for e in self.episodes(headers) if e["bvid"] in bvids]
                if len(existing) >= len(bvids):
                    break
                if attempt < 3:
                    time.sleep(2)
        if [e["bvid"] for e in existing] != bvids:
            raise DeliveryError("B 站合集未回显全部稿件或顺序不一致")
        return {e["bvid"]: {"collection_id": self.collection_id, "section_id": self.section_id,
                            "episode_id": e["id"]}
                for e in existing}
