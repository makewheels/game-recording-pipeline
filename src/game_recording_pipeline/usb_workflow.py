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
from .delivery_clients import Baidu, Bilibili, BilibiliCollection, DeliveryError, TECHNICAL_REJECTION
from .publication_assets import COVER_DIRECTORY, clean_text, recording_cover_title, recording_text, render_cover
from .recording_catalog import append_recording, catalog_lock, read_catalog, rename_without_replacing, sha256, write_catalog


FORMATS = {".webm", ".mp4", ".mkv", ".mov", ".m4v"}
DISKUTIL = "/usr/sbin/diskutil"
LAUNCHCTL = "/bin/launchctl"
AGENT_NAME = "local.game-recording-pipeline.usb"
POLL_SECONDS = 120
WORKFLOW_ERRORS = (OSError, DeliveryError, subprocess.SubprocessError, ValueError)


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


def needs_review(receipt):
    return receipt.get("status") == "rejected" or receipt.get("audit", {}).get("traffic_limited") is True


def review_signature(receipt, reason):
    if not needs_review(receipt):
        return None
    audit = receipt.get("audit", {})
    problems = [(item.get("reject_reason_id"), item.get("reject_reason"), item.get("violation_time"))
                for item in audit.get("problem_detail", [])]
    return receipt.get("bvid"), receipt["status"], audit.get("traffic_limited", False), reason, problems


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
        self.baidu_covers = Baidu(work, str(remote / COVER_DIRECTORY))
        cookies = Path(config["bilibili_cookies"]).expanduser().resolve()
        self.bilibili = Bilibili(
            work, cookies,
            int(config.get("bilibili_partition", 17)),
            config.get("bilibili_tags", "单机游戏,游戏实况"),
            config.get("bilibili_line"),
        )
        collection_id = config.get("bilibili_collection_id")
        section_id = config.get("bilibili_section_id")
        self.collection = None
        if collection_id is not None or section_id is not None:
            self.collection = BilibiliCollection(cookies, collection_id, section_id)

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

    def entries(self):
        with catalog_lock(self.path, write=False):
            return self.load()["videos"]

    def active(self):
        return [
            item for item in self.entries()
            if item.get("usb", {}).get("volume_uuid") == self.volume_uuid
            and not item.get("cleanup", {}).get("completed")
        ]

    def cover_asset(self, entry, video=None):
        publication = entry.get("publication", {})
        default = Path(COVER_DIRECTORY) / Path(entry["filename"]).with_suffix(".jpg").name
        relative = Path(publication.get("cover_file", default))
        if relative.parent != Path(COVER_DIRECTORY) or relative.suffix != ".jpg":
            raise DeliveryError("清单中的封面路径不在 B 站封面目录中")
        cover = self.path.parent / relative
        if cover.is_symlink() or cover.parent.is_symlink():
            raise DeliveryError("封面或封面目录不能是符号链接")
        if not cover.exists():
            if video is None:
                video = checked_local(self.video_directory(entry), entry)
            heading = recording_cover_title(entry)
            with tempfile.TemporaryDirectory(prefix="cover-", dir=self.work) as temporary:
                staging = Path(temporary) / "封面.jpg"
                render_cover(video, staging, entry["sequence"], heading,
                             at=publication.get("cover_frame_seconds"))
                cover.parent.mkdir(exist_ok=True)
                rename_without_replacing(staging, cover)
            publication = publication | {"cover_title": heading}
        checksum = sha256(cover)
        if publication.get("cover_sha256", checksum) != checksum:
            raise DeliveryError(f"本地封面 SHA-256 不一致 {cover.name}")
        return cover, publication | {"cover_file": str(relative), "cover_sha256": checksum}

    def backup_one(self, entry):
        uploads = entry.get("uploads", {})
        sequence = entry["sequence"]
        video = None
        if not backup_verified(uploads.get("baidu", {})):
            video = checked_local(self.video_directory(entry), entry)
            uploads = uploads | {"baidu": self.baidu.upload(video, entry)}
            self.change(sequence, uploads=uploads)
        if backup_verified(uploads.get("baidu_cover", {})):
            return
        cover, publication = self.cover_asset(entry, video)
        receipt = self.baidu_covers.upload(cover, {
            "size_bytes": cover.stat().st_size, "sha256": publication["cover_sha256"],
        })
        self.change(sequence, publication=publication, uploads=uploads | {"baidu_cover": receipt}, baidu_error="")

    def refresh_audit(self, sequence, receipt):
        bvid = receipt["bvid"]
        status, reason = self.bilibili.review(bvid)
        audit = self.bilibili.audit_details(bvid)
        with catalog_lock(self.path, write=True):
            catalog = self.load()
            entry = next(item for item in catalog["videos"] if item["sequence"] == sequence)
            uploads = entry.setdefault("uploads", {})
            current = uploads.get("bilibili", {})
            if current.get("bvid", bvid) != bvid:
                raise DeliveryError("清单中的 BV 号已变化")
            updated = receipt | current | {"status": status, "audit": audit}
            updated["review_reason"] = reason
            previous = review_signature(current, current.get("review_reason", entry.get("bilibili_error", "")))
            if updated != current or entry.get("bilibili_error") != reason:
                uploads["bilibili"] = updated
                entry["bilibili_error"] = reason
                write_catalog(self.path, catalog)
        issue = review_signature(updated, reason)
        if issue is not None and issue != previous:
            print(json.dumps({"event": "bilibili_audit_issue", "sequence": sequence, "bvid": bvid,
                              "status": status, "traffic_limited": audit["traffic_limited"]},
                             ensure_ascii=False), file=sys.stderr, flush=True)
        return updated, reason

    def audit_queue(self, entries):
        result = {"issues": [], "errors": []}
        for entry in entries:
            receipt = entry.get("uploads", {}).get("bilibili", {})
            if not receipt.get("bvid"):
                continue
            identity = {"sequence": entry["sequence"], "bvid": receipt["bvid"]}
            try:
                updated, reason = self.refresh_audit(entry["sequence"], receipt)
            except WORKFLOW_ERRORS as exc:
                result["errors"].append(identity | {"error": str(exc)})
                continue
            if needs_review(updated):
                directory = Path(entry.get("local_directory", self.path.parent))
                result["issues"].append(identity | {
                    "title": updated.get("title"),
                    "status": updated["status"],
                    "reason": reason,
                    "audit": updated["audit"],
                    "original_path": str(directory / entry["filename"]),
                    "original_sha256": entry["sha256"],
                    "baidu_path": entry["uploads"].get("baidu", {}).get("path"),
                    "bilibili_copy": entry.get("publication", {}).get("bilibili_copy"),
                })
        return result

    def recording(self, sequence):
        entry = next((item for item in self.entries() if item["sequence"] == sequence), None)
        if entry is None:
            raise DeliveryError(f"清单里没有序号 {sequence}")
        return entry

    def stage_bilibili_copy(self, sequence, video):
        entry = self.recording(sequence)
        publication = entry.get("publication", {})
        receipt = entry.get("uploads", {}).get("bilibili", {})
        if publication.get("bilibili_copy") or (publication.get("started") and not receipt.get("bvid")):
            raise DeliveryError("已有待处理的 B 站投稿")
        source = self.video_directory(entry) / entry["filename"]
        video = video.expanduser().absolute()
        if video.is_symlink() or not video.is_file() or video.suffix.lower() not in FORMATS:
            raise DeliveryError("需要独立保存的 B 站视频副本")
        video = video.resolve()
        checksum = sha256(video)
        if (video == source.resolve() or (source.exists() and video.samefile(source))
                or checksum == entry["sha256"]):
            raise DeliveryError("B 站处理版必须与原片独立保存，且内容已修改")
        title = receipt.get("title") or publication.get("title") or recording_text(entry)[0]
        if receipt.get("bvid"):
            current, _ = self.refresh_audit(sequence, receipt)
            if not needs_review(current):
                raise DeliveryError("当前 B 站稿件没有退回或限流问题")
            if checksum == current.get("video_sha256"):
                raise DeliveryError("处理版与当前 B 站稿件的视频相同")
            title = re.sub(r"【修订版 \d+】$", "", title)
            title += f"【修订版 {len(entry.get('bilibili_history', [])) + 1}】"
        copy = {"path": str(video), "sha256": checksum, "title": clean_text(title),
                "supersedes": receipt.get("bvid"), "started": False}
        self.change(sequence, publication=publication | {"bilibili_copy": copy})
        return copy

    def submit_bilibili_copy(self, entry):
        sequence = entry["sequence"]
        uploads = entry.get("uploads", {})
        previous = uploads.get("bilibili", {})
        publication = entry.get("publication", {})
        copy = publication.get("bilibili_copy")
        if not copy:
            raise DeliveryError(f"序号 {sequence} 尚未登记 B 站处理版")
        title = copy["title"]
        old_bvid = copy["supersedes"]
        if previous.get("bvid") != old_bvid:
            raise DeliveryError("待处理稿件的 BV 号已变化")
        if copy["started"]:
            bvid = self.bilibili.find(title, exclude=old_bvid)
            if not bvid:
                raise DeliveryError("B 站处理版投稿结果待核对")
        else:
            if old_bvid:
                previous, _ = self.refresh_audit(sequence, previous)
                if not needs_review(previous):
                    raise DeliveryError("当前 B 站稿件没有退回或限流问题")
            if self.bilibili.find(title, exclude=old_bvid):
                raise DeliveryError("已有同名 B 站稿件，请先核对")
            video = Path(copy["path"])
            if video.is_symlink() or not video.is_file() or sha256(video) != copy["sha256"]:
                raise DeliveryError("B 站处理版缺失或 SHA-256 不一致")
            cover, publication = self.cover_asset(entry)
            self.change(sequence, publication=publication | {"bilibili_copy": copy | {"started": True}})
            try:
                bvid = self.bilibili.submit(video, title, f"{title}。游戏实况录屏。",
                                             cover=cover, exclude=old_bvid)
            except DeliveryError as exc:
                if "文件传输失败，可重试" in str(exc):
                    self.change(sequence, publication=publication)
                raise
            if not bvid:
                raise DeliveryError("B 站处理版投稿结果待核对")
        receipt = {"bvid": bvid, "title": title, "status": "reviewing", "video_sha256": copy["sha256"]}
        history = entry.get("bilibili_history", [])
        if old_bvid:
            history = [*history, previous]
        publication = {key: value for key, value in publication.items() if key != "bilibili_copy"}
        self.change(sequence, publication=publication | {"title": title, "started": True},
                    uploads=uploads | {"bilibili": receipt}, bilibili_history=history, bilibili_error="")
        return receipt

    def publish_one(self, entry):
        if entry.get("publication", {}).get("bilibili_copy"):
            self.submit_bilibili_copy(entry)
            return
        sequence = entry["sequence"]
        uploads = entry.get("uploads", {})
        receipt = uploads.get("bilibili", {})
        publication = entry.get("publication", {})
        rejected_bvid = entry.get("rejected_bilibili", {}).get("bvid")
        default_title, _, _ = recording_text(entry)
        title = receipt.get("title") or publication.get("title") or default_title
        bvid = receipt.get("bvid") or self.bilibili.find(title, exclude=rejected_bvid)
        if bvid:
            receipt, reason = self.refresh_audit(sequence, receipt | {"bvid": bvid, "title": title})
            if (
                receipt["status"] != "rejected" or receipt["audit"]["traffic_limited"]
                or publication.get("repaired") or receipt.get("video_sha256")
                or not TECHNICAL_REJECTION.search(reason)
                or any(word in reason for word in ("版权", "违规", "血腥", "隐私"))
            ):
                return
            self.change(sequence, publication=publication | {"title": default_title, "repaired": True, "started": False},
                        uploads={key: value for key, value in uploads.items() if key != "bilibili"},
                        rejected_bilibili=receipt | {"reason": reason})
            return

        if publication.get("started"):
            return
        title = clean_text(title)
        video = checked_local(self.video_directory(entry), entry)
        repaired = publication.get("repaired", False)
        cover, publication = self.cover_asset(entry, video)
        state = publication | {"title": title, "repaired": repaired, "started": True}
        with tempfile.TemporaryDirectory(prefix="submission-", dir=self.work) as temporary:
            submission = video
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

    def sync_collection(self, entries):
        if self.collection is None:
            return
        pending = {}
        for entry in entries:
            receipt = entry.get("uploads", {}).get("bilibili", {})
            if (receipt.get("collection_id"), receipt.get("section_id")) == (
                self.collection.collection_id, self.collection.section_id
            ):
                continue
            if receipt.get("status") != "published" or not receipt.get("bvid"):
                break
            # 修订版与已经入合集的旧稿件需要在创作中心交换位置。
            if any((old.get("collection_id"), old.get("section_id")) == (
                self.collection.collection_id, self.collection.section_id
            ) for old in entry.get("bilibili_history", [])):
                continue
            pending[entry["sequence"]] = receipt["bvid"]
        results = self.collection.ensure(list(pending.values()))
        if not results:
            return
        with catalog_lock(self.path, write=True):
            catalog = self.load()
            for entry in catalog["videos"]:
                bvid = pending.get(entry["sequence"])
                if bvid is None:
                    continue
                receipt = entry["uploads"]["bilibili"]
                if receipt["bvid"] != bvid:
                    raise DeliveryError("清单中的 BV 号已变化")
                receipt.update(results[bvid])
            write_catalog(self.path, catalog)

    def drain(self):
        succeeded = True
        active = self.active()
        active_sequences = {entry["sequence"] for entry in active}
        other = [entry for entry in self.entries() if entry["sequence"] not in active_sequences]
        staged = [entry for entry in other if entry.get("publication", {}).get("bilibili_copy")]
        manual = [entry for entry in other if not entry.get("publication", {}).get("bilibili_copy")]
        for error in self.audit_queue(manual)["errors"]:
            print(json.dumps(error, ensure_ascii=False), file=sys.stderr, flush=True)
            succeeded = False
        # 优先核对已有 BV 号，再开始可能耗时的上传。
        active.sort(key=lambda entry: not entry.get("uploads", {}).get("bilibili", {}).get("bvid"))
        for service, method, entries in (
            ("bilibili", self.publish_one, active + staged),
            ("baidu", self.backup_one, self.active()),
        ):
            for entry in entries:
                try:
                    method(entry)
                except WORKFLOW_ERRORS as exc:
                    message = str(exc) if isinstance(exc, DeliveryError) else type(exc).__name__
                    self.change(entry["sequence"], **{f"{service}_error": message})
                    print(f'{entry["sequence"]:03d} {service} {message}', flush=True)
                    succeeded = False
        self.sync_collection(self.active())
        return succeeded

    def finish(self):
        entries = self.active()
        if not entries or not self.auto_delete:
            return
        for entry in entries:
            archive = entry.get("uploads", {}).get("bilibili", {})
            backup = entry.get("uploads", {}).get("baidu", {})
            cover = entry.get("uploads", {}).get("baidu_cover", {})
            if (entry.get("publication", {}).get("bilibili_copy") or archive.get("status") != "published"
                    or not backup_verified(backup) or not backup_verified(cover)):
                return
            if self.collection and (archive.get("collection_id"), archive.get("section_id")) != (
                self.collection.collection_id, self.collection.section_id
            ):
                return
            receipt, _ = self.refresh_audit(entry["sequence"], archive)
            if receipt["status"] != "published" or receipt["audit"]["traffic_limited"]:
                return
            if not self.baidu.confirmed(entry["filename"], backup):
                return
            if not self.baidu_covers.confirmed(Path(entry["publication"]["cover_file"]).name, cover):
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
    mode.add_argument("--audit", type=int, metavar="序号", help="仅刷新指定稿件的审核详情")
    mode.add_argument("--audit-queue", action="store_true", help="刷新已登记稿件并输出待处理审核问题 JSON")
    mode.add_argument("--stage-bilibili-copy", type=int, metavar="序号", help="登记下一轮仅用于 B 站投稿的处理版")
    mode.add_argument("--submit-bilibili-copy", type=int, metavar="序号", help="只提交指定序号已登记的 B 站处理版")
    parser.add_argument("--video", type=Path, help="与 --stage-bilibili-copy 一起使用的处理版视频")
    mode.add_argument("--watch", action="store_true", help="持续等待 U 盘和审核结果")
    mode.add_argument("--install", action="store_true", help="在下一次插入 U 盘前安装后台任务")
    args = parser.parse_args()
    if (args.stage_bilibili_copy is None) != (args.video is None):
        parser.error("--stage-bilibili-copy 和 --video 必须一起使用")
    config_path = args.config.expanduser().resolve()
    with config_path.open(encoding="utf-8") as config_file:
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
            if args.audit_queue or any(sequence is not None for sequence in
                                       (args.audit, args.stage_bilibili_copy, args.submit_bilibili_copy)):
                fcntl.flock(config_file, fcntl.LOCK_EX)
                try:
                    if args.audit_queue:
                        result = workflow.audit_queue(workflow.entries())
                    elif args.stage_bilibili_copy is not None:
                        result = workflow.stage_bilibili_copy(args.stage_bilibili_copy, args.video)
                    elif args.submit_bilibili_copy is not None:
                        result = workflow.submit_bilibili_copy(workflow.recording(args.submit_bilibili_copy))
                    else:
                        entry = workflow.recording(args.audit)
                        receipt = entry.get("uploads", {}).get("bilibili", {})
                        if not receipt.get("bvid"):
                            raise DeliveryError(f"序号 {args.audit} 没有登记 B 站 BV 号")
                        updated, _ = workflow.refresh_audit(args.audit, receipt)
                        result = updated["audit"]
                except WORKFLOW_ERRORS as exc:
                    parser.exit(1, f"{exc}\n")
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if args.audit_queue and result["errors"]:
                    sys.exit(1)
                return
            workflow.directory.mkdir(parents=True, exist_ok=True)
            if args.install:
                install_agent(config_path, workflow)
                return
            while True:
                try:
                    fcntl.flock(config_file, fcntl.LOCK_EX)
                    try:
                        succeeded = workflow.cycle()
                    finally:
                        fcntl.flock(config_file, fcntl.LOCK_UN)
                except WORKFLOW_ERRORS as exc:
                    print(str(exc), file=sys.stderr, flush=True)
                    if args.once:
                        sys.exit(1)
                if args.once:
                    sys.exit(0 if succeeded else 1)
                time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
