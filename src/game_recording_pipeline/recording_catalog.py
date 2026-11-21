#!/usr/bin/env python3
import argparse
from contextlib import contextmanager
import ctypes
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from .publication_assets import recording_stem


@contextmanager
def catalog_lock(path, *, write):
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        yield
    finally:
        os.close(descriptor)


def read_catalog(path):
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if catalog["schema_version"] != 1:
        raise ValueError("清单版本不受支持")
    sequences = [entry["sequence"] for entry in catalog["videos"]]
    names = [entry["filename"] for entry in catalog["videos"]]
    if (
        sequences != sorted(set(sequences))
        or catalog["last_sequence"] < max(sequences, default=0)
        or len(set(names)) != len(names)
    ):
        raise ValueError("清单序号或文件名存在冲突")
    for name in names:
        check_filename(name)
    return catalog


def write_catalog(path, catalog):
    descriptor, temporary_name = tempfile.mkstemp(prefix="recording-catalog-", suffix=".json")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(catalog, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def check_filename(name):
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ValueError(f"需要视频文件名 {name!r}")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def rename_without_replacing(source, destination):
    rename = ctypes.CDLL(None, use_errno=True).renamex_np
    rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    rename_excl = 0x00000004
    if rename(os.fsencode(source), os.fsencode(destination), rename_excl):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def append_recording(path, catalog, source, original_filename, recorded_at, title, extra=None, directory=None):
    if not title.strip() or re.search(r"[:/\\\x00-\x1f]", title):
        raise ValueError("任务标题为空或含有文件名不能使用的字符")
    sequence = catalog["last_sequence"] + 1
    name = recording_stem(recorded_at, sequence, title) + source.suffix
    destination = (directory or path.parent) / name
    before = source.stat()
    digest = sha256(source)
    after = source.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise OSError(f"计算散列时视频发生了变化 {source.name}")
    entry = {
        "recorded_at": recorded_at.isoformat(),
        "sequence": sequence,
        "filename": destination.name,
        "original_filename": original_filename,
        "sha256": digest,
        "size_bytes": before.st_size,
    }
    entry.update(extra or {})
    catalog["last_sequence"] = sequence
    catalog["videos"].append(entry)
    try:
        rename_without_replacing(source, destination)
        write_catalog(path, catalog)
    except BaseException:
        committed = path.exists() and read_catalog(path) == catalog
        if not committed and not source.exists() and destination.exists() and destination.stat().st_ino == before.st_ino:
            rename_without_replacing(destination, source)
        raise
    return entry


def add_recording(path, args):
    if args.after < 0:
        raise ValueError("起始序号必须大于或等于零")
    check_filename(args.file)
    source = path.parent / args.file
    if source.is_symlink() or not source.is_file() or not source.suffix or source == path:
        raise ValueError(f"找不到可登记的视频 {source.name}")
    recorded_at = datetime.strptime(args.time, "%Y-%m-%dT%H:%M:%S")
    if path.exists():
        catalog = read_catalog(path)
    else:
        catalog = {"schema_version": 1, "last_sequence": args.after, "videos": []}
    if any(entry["filename"] == source.name for entry in catalog["videos"]):
        raise ValueError(f"视频已在清单中 {source.name}")
    entry = append_recording(path, catalog, source, source.name, recorded_at, args.title)
    print(f'{entry["sequence"]:03d} {entry["filename"]}')


def verify_recordings(path, args):
    catalog = read_catalog(path)
    entries = [entry for entry in catalog["videos"] if args.sequence in (None, entry["sequence"])]
    if args.sequence is not None and not entries:
        raise ValueError(f"清单里没有序号 {args.sequence}")

    failed = False
    for entry in entries:
        directory = Path(entry["local_directory"]) if "local_directory" in entry else path.parent
        file = directory / entry["filename"]
        if not file.exists() and entry.get("cleanup", {}).get("local_removed"):
            status = "已归档"
        elif file.is_symlink() or not file.is_file():
            status = "缺失"
        elif file.stat().st_size != entry["size_bytes"] or sha256(file) != entry["sha256"]:
            status = "内容不一致"
        else:
            status = "SHA-256 一致"
        print(f'{entry["sequence"]:03d}  {status}  {file.name}')
        failed |= status not in {"SHA-256 一致", "已归档"}
    return int(failed)


def record_upload(path, args):
    catalog = read_catalog(path)
    entry = next((item for item in catalog["videos"] if item["sequence"] == args.sequence), None)
    if entry is None:
        raise ValueError(f"清单里没有序号 {args.sequence}")
    if args.baidu_path:
        if args.title:
            raise ValueError("标题用于哔哩哔哩稿件")
        service = "baidu"
        receipt = {"path": args.baidu_path}
    else:
        if not re.fullmatch(r"BV[a-zA-Z0-9]{10}", args.bvid) or not args.title or not args.title.strip():
            raise ValueError("需要有效的 BV 号和投稿标题")
        service = "bilibili"
        receipt = {"bvid": args.bvid, "title": args.title}
    uploads = entry.setdefault("uploads", {})
    if service in uploads and uploads[service] != receipt:
        raise ValueError(f"该视频已有不同的上传记录 {service}")
    uploads[service] = receipt
    write_catalog(path, catalog)
    print(f'{entry["sequence"]:03d}  已登记 {service}')


def main():
    parser = argparse.ArgumentParser(description="用序号和 SHA-256 管理录屏")
    parser.add_argument("--catalog", type=Path, default=Path("视频列表.json"))
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="重命名一个视频并登记")
    add.add_argument("file", help="清单所在目录中的原文件名")
    add.add_argument("--time", required=True, help="当地时间 YYYY-MM-DDTHH:MM:SS")
    add.add_argument("--title", required=True, help="用于文件名的任务和内容")
    add.add_argument("--after", type=int, default=0, help="首次创建清单时最后用过的总序号")

    verify = commands.add_parser("verify", help="核对本地文件和 SHA-256")
    verify.add_argument("--sequence", type=int, help="仅核对指定总序号")

    record = commands.add_parser("record-upload", help="登记已确认的上传结果")
    record.add_argument("sequence", type=int)
    service = record.add_mutually_exclusive_group(required=True)
    service.add_argument("--baidu-path")
    service.add_argument("--bvid")
    record.add_argument("--title", help="哔哩哔哩投稿标题")

    args = parser.parse_args()
    path = args.catalog.resolve()
    try:
        with catalog_lock(path, write=args.command != "verify"):
            if args.command == "add":
                add_recording(path, args)
            elif args.command == "verify":
                return verify_recordings(path, args)
            else:
                record_upload(path, args)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        print(f"错误 {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
