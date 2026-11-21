#!/usr/bin/env python3
import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from .classify_recording import FrameClassifier
from .delivery_clients import Baidu, Bilibili, DeliveryError, TECHNICAL_REJECTION
from .publication_assets import clean_text, recording_text, render_cover
from .recording_catalog import append_recording, catalog_lock, read_catalog, sha256, write_catalog


FORMATS = {".webm", ".mp4", ".mkv", ".mov", ".m4v"}
DISKUTIL = "/usr/sbin/diskutil"
LAUNCHCTL = "/bin/launchctl"
AGENT_NAME = "local.game-recording-pipeline.usb"


def attached_disk(volume_uuid):
    result = subprocess.run([DISKUTIL, "info", "-plist", volume_uuid], capture_output=True, timeout=30)
    if result.returncode:
        return None
    disk = plistlib.loads(result.stdout)
    if (
        disk.get("VolumeUUID", "").upper() != volume_uuid
        or disk.get("BusProtocol") != "USB"
        or disk.get("Internal") is not False
        or not disk.get("Ejectable")
        or not disk.get("MountPoint")
    ):
        return None
    mount = Path(disk["MountPoint"])
    return (mount, disk["DeviceIdentifier"]) if mount.is_mount() else None


def recorded_time(video):
    match = re.search(r"(?<!\d)(\d{14})(?!\d)", video.stem)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    return datetime.fromtimestamp(video.stat().st_mtime)


def safe_path(mount, relative):
    path = mount / relative
    if not path.resolve().is_relative_to(mount) or path.resolve() != path:
        raise ValueError(f"录屏路径不在指定 U 盘目录中 {relative}")
    return path


def checked_local(directory, entry):
    path = directory / entry["filename"]
    if path.is_symlink() or not path.is_file() or sha256(path) != entry["sha256"]:
        raise DeliveryError(f"本地录屏缺失或 SHA-256 不一致 {entry['filename']}")
    return path


def remove_verified(path, checksum, still_allowed=None):
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        initial = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(initial.st_mode):
            raise DeliveryError(f"待清理路径不是普通文件 {path.name}")
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            if digest != checksum or identity != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
                raise DeliveryError(f"待清理文件发生变化或 SHA-256 不一致 {path.name}")
            if still_allowed is not None and not still_allowed():
                raise DeliveryError("删除前 U 盘已变化")
            os.unlink(path.name, dir_fd=directory)
    finally:
        os.close(directory)


def backup_verified(receipt):
    return bool(receipt.get("verified_md5") or receipt.get("verified_sha256"))


class Workflow:
    def __init__(self, config, work):
        self.path = Path(config["catalog"]).expanduser().resolve()
        self.directory = Path(config.get("local_directory", self.path.parent)).expanduser().resolve()
        self.volume_uuid = str(uuid.UUID(config["volume_uuid"])).upper()
        self.source_dir = PurePosixPath(config["source_directory"])
        if self.source_dir.is_absolute() or ".." in self.source_dir.parts or not self.source_dir.parts:
            raise ValueError("source_directory 必须指向 U 盘上的录屏子目录")
        remote = PurePosixPath(config["baidu_directory"])
        if remote.is_absolute() or len(remote.parts) != 1 or remote.name in ("", ".."):
            raise ValueError("baidu_directory 必须是网盘应用目录下的一级文件夹名")
        self.work = work
        self.classifier = FrameClassifier(work)
        self.after = int(config.get("start_after", 0))
        if self.after < 0:
            raise ValueError("start_after 必须大于或等于零")
        self.auto_delete = config.get("delete_after_verification") is True
        self.baidu = Baidu(work, str(remote))
        self.bilibili = Bilibili(
            work, Path(config["bilibili_cookies"]).expanduser().resolve(),
            int(config.get("bilibili_partition", 17)),
            config.get("bilibili_tags", "单机游戏,游戏实况"),
            config.get("bilibili_line"),
        )

    def sources(self, mount):
        folder = safe_path(mount, self.source_dir)
        if not folder.is_dir():
            return []
        videos = [
            item for item in folder.iterdir()
            if not item.name.startswith(".") and item.suffix.lower() in FORMATS
            and item.is_file() and not item.is_symlink()
        ]
        return sorted(videos, key=lambda item: (recorded_time(item), item.name))

    def load(self):
        if self.path.exists():
            return read_catalog(self.path)
        return {"schema_version": 1, "last_sequence": self.after, "videos": []}

    def change(self, sequence, **fields):
        with catalog_lock(self.path, write=True):
            catalog = self.load()
            entry = next(item for item in catalog["videos"] if item["sequence"] == sequence)
            entry.update(fields)
            write_catalog(self.path, catalog)

    def video_directory(self, entry):
        storage = entry.get("local_directory")
        if storage is None:
            return self.path.parent
        if storage != str(self.directory):
            raise DeliveryError("清单中的电脑副本目录和配置不一致")
        return self.directory

    def import_one(self, mount, source):
        relative = str(source.relative_to(mount))
        with catalog_lock(self.path, write=True):
            catalog = self.load()
            if any((item.get("usb") or {}).get("relative_path") == relative
                   and (item.get("usb") or {}).get("volume_uuid") == self.volume_uuid
                   and not (item.get("cleanup") or {}).get("usb_removed")
                   for item in catalog["videos"]):
                return
            checksum = sha256(source)
            metadata = {"volume_uuid": self.volume_uuid, "relative_path": relative}
            for item in catalog["videos"]:
                if not item.get("usb") and item["original_filename"] == source.name and item["sha256"] == checksum:
                    checked_local(self.video_directory(item), item)
                    item["usb"] = metadata
                    write_catalog(self.path, catalog)
                    return
            with tempfile.TemporaryDirectory(prefix="copy-", dir=self.work) as temporary:
                copy = Path(temporary) / source.name
                shutil.copyfile(source, copy)
                if sha256(copy) != checksum:
                    raise DeliveryError(f"U 盘和电脑副本的 SHA-256 不一致 {source.name}")
                title = self.classifier.classify(copy)
                extra = {"usb": metadata, "local_directory": str(self.directory)}
                entry = append_recording(self.path, catalog, copy, source.name, recorded_time(source),
                                         title, extra, self.directory)
                print(f'已导入 {entry["sequence"]:03d} {entry["filename"]}', flush=True)

    def active(self):
        with catalog_lock(self.path, write=False):
            return [
                item for item in self.load()["videos"]
                if item.get("usb", {}).get("volume_uuid") == self.volume_uuid
                and not item.get("cleanup", {}).get("completed")
            ]

    def backup_one(self, entry):
        uploads = entry.get("uploads", {})
        if backup_verified(uploads.get("baidu", {})):
            return
        video = checked_local(self.video_directory(entry), entry)
        receipt = self.baidu.upload(video, entry)
        self.change(entry["sequence"], uploads=uploads | {"baidu": receipt}, baidu_error="")

    def publish_one(self, entry):
        sequence = entry["sequence"]
        uploads = entry.get("uploads", {})
        receipt = uploads.get("bilibili", {})
        publication = entry.get("publication", {})
        rejected_bvid = entry.get("rejected_bilibili", {}).get("bvid")
        default_title, heading, action = recording_text(entry)
        title = receipt.get("title") or publication.get("title") or default_title
        bvid = receipt.get("bvid") or self.bilibili.find(title, exclude=rejected_bvid)
        if bvid:
            status, reason = self.bilibili.review(bvid)
            receipt = receipt | {"bvid": bvid, "title": title, "status": status}
            self.change(sequence, uploads=uploads | {"bilibili": receipt}, bilibili_error=reason)
            if (
                status != "rejected" or publication.get("repaired")
                or not TECHNICAL_REJECTION.search(reason)
                or any(word in reason for word in ("版权", "违规", "血腥", "隐私"))
            ):
                return
            self.change(sequence, publication={"title": default_title, "repaired": True, "started": False},
                        uploads={key: value for key, value in uploads.items() if key != "bilibili"},
                        rejected_bilibili=receipt | {"reason": reason})
            return

        if publication.get("started"):
            return
        title = clean_text(title)
        video = checked_local(self.video_directory(entry), entry)
        repaired = publication.get("repaired", False)
        state = {"title": title, "repaired": repaired, "started": True}
        with tempfile.TemporaryDirectory(prefix="submission-", dir=self.work) as temporary:
            submission = video
            cover = Path(temporary) / "封面.jpg"
            render_cover(video, cover, sequence, heading, action)
            if repaired:
                submission = Path(temporary) / f"{video.stem}.mp4"
                self.bilibili.transcode(video, submission)
            self.change(sequence, publication=state)
            try:
                bvid = self.bilibili.submit(submission, title, f"{title}。游戏实况录屏。",
                                             cover=cover, exclude=rejected_bvid)
            except DeliveryError as error:
                if "文件传输失败，可重试" in str(error):
                    self.change(sequence, publication=state | {"started": False})
                raise
        if bvid:
            self.change(sequence, publication=state, bilibili_error="",
                        uploads=uploads | {"bilibili": {"bvid": bvid, "title": title, "status": "reviewing"}})
        else:
            self.change(sequence, bilibili_error="暂未查到稿件回执，继续核对稿件列表")

    def drain(self):
        succeeded = True
        for service, method in (("bilibili", self.publish_one), ("baidu", self.backup_one)):
            for entry in self.active():
                try:
                    method(entry)
                except (OSError, DeliveryError, subprocess.SubprocessError) as exc:
                    message = str(exc) if isinstance(exc, DeliveryError) else type(exc).__name__
                    self.change(entry["sequence"], **{f"{service}_error": message})
                    print(f'{entry["sequence"]:03d} {service} {message}', flush=True)
                    succeeded = False
                    break
        return succeeded

    def finish(self):
        entries = self.active()
        if not entries or not self.auto_delete:
            return
        for entry in entries:
            archive = entry.get("uploads", {}).get("bilibili", {})
            backup = entry.get("uploads", {}).get("baidu", {})
            if archive.get("status") != "published" or not backup_verified(backup):
                return
            if self.bilibili.review(archive["bvid"])[0] != "published":
                return
            if not self.baidu.confirmed(entry["filename"], backup):
                return

        pending = [entry for entry in entries if not entry.get("cleanup", {}).get("usb_ejected")]
        if pending:
            disk = attached_disk(self.volume_uuid)
            if not disk:
                return
            mount, device = disk
            indexed = {entry["usb"]["relative_path"] for entry in pending}
            if any(str(source.relative_to(mount)) not in indexed for source in self.sources(mount)):
                return
            for entry in pending:
                relative = PurePosixPath(entry["usb"]["relative_path"])
                if relative.parent != self.source_dir or relative.suffix.lower() not in FORMATS:
                    raise DeliveryError(f"清单里的 U 盘路径不在指定录屏目录 {relative}")
                source = safe_path(mount, relative)
                if source.exists():
                    remove_verified(source, entry["sha256"], lambda: attached_disk(self.volume_uuid) == disk)
                self.change(entry["sequence"], cleanup={"usb_removed": True})
            result = subprocess.run([DISKUTIL, "eject", device], capture_output=True, timeout=60)
            if result.returncode:
                raise DeliveryError("U 盘暂时无法弹出，电脑副本已保留")
            for entry in pending:
                self.change(entry["sequence"], cleanup={"usb_removed": True, "usb_ejected": True})

        for entry in entries:
            directory = self.video_directory(entry)
            remove_local = "local_directory" in entry
            if remove_local and (directory / entry["filename"]).exists():
                remove_verified(directory / entry["filename"], entry["sha256"])
            self.change(entry["sequence"], cleanup={
                "usb_removed": True, "usb_ejected": True, "local_removed": remove_local, "completed": True,
            })

    def cycle(self):
        disk = attached_disk(self.volume_uuid)
        if disk:
            mount, _ = disk
            for source in self.sources(mount):
                self.import_one(mount, source)
        succeeded = self.drain()
        self.finish()
        return succeeded


def install_agent(config_path, workflow):
    if attached_disk(workflow.volume_uuid):
        raise ValueError("请先弹出指定 U 盘，后台任务会从下次插入开始")
    folder = Path.home() / "Library" / "LaunchAgents"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"{AGENT_NAME}.plist"
    program = [sys.executable, "-B", "-m", "game_recording_pipeline.usb_workflow",
               "--config", str(config_path), "--watch"]
    settings = {
        "Label": AGENT_NAME,
        "ProgramArguments": program,
        "EnvironmentVariables": {"PATH": os.environ["PATH"]},
        "RunAtLoad": True,
        "KeepAlive": True,
    }
    with destination.open("xb") as stream:
        plistlib.dump(settings, stream)
    try:
        subprocess.run([LAUNCHCTL, "bootstrap", f"gui/{os.getuid()}", str(destination)], check=True, timeout=30)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    print(f"后台任务已启用 {destination}")


def shut_down(signum, frame):
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, shut_down)
    parser = argparse.ArgumentParser(description="监测指定的 PS5 录屏 U 盘并管理上传")
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan", action="store_true", help="只列出 U 盘上的候选录屏")
    mode.add_argument("--once", action="store_true", help="运行一轮导入、上传与核对")
    mode.add_argument("--watch", action="store_true", help="持续等待 U 盘和审核结果")
    mode.add_argument("--install", action="store_true", help="在下一次插入 U 盘前安装后台任务")
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    with config_path.open(encoding="utf-8") as config_file:
        if not (args.scan or args.install):
            fcntl.flock(config_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.load(config_file)
        with tempfile.TemporaryDirectory(prefix="recording-workflow-") as temporary:
            workflow = Workflow(config, Path(temporary))
            if args.scan:
                disk = attached_disk(workflow.volume_uuid)
                if disk:
                    for source in workflow.sources(disk[0]):
                        print(f"{recorded_time(source).isoformat()}  {source.name}")
                return
            if not workflow.path.parent.is_dir():
                raise ValueError(f"请先创建录屏目录 {workflow.path.parent}")
            workflow.directory.mkdir(parents=True, exist_ok=True)
            if args.install:
                install_agent(config_path, workflow)
                return
            while True:
                try:
                    succeeded = workflow.cycle()
                except (OSError, DeliveryError, subprocess.SubprocessError, ValueError) as exc:
                    print(str(exc), file=sys.stderr, flush=True)
                    if args.once:
                        sys.exit(1)
                if args.once:
                    sys.exit(0 if succeeded else 1)
                time.sleep(120)


if __name__ == "__main__":
    main()
