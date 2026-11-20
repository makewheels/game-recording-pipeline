# 录屏清单

用一个 JSON 文件记录录屏的时间、总序号、原文件名、新文件名和 SHA-256。文件名格式为 `YYYYMMDD-HHMMSS_总序号_任务名_片段内容.扩展名`，按文件名排序就是时间顺序。

需要 macOS、Python 3.11+ 和 uv。脚本使用 Python 标准库。系统临时目录和清单目录需位于同一文件系统，以便原子更新清单。

## 登记并核对视频

```sh
uv run --no-project python recording_catalog.py --catalog '/视频目录/视频列表.json' \
  add '原录屏.webm' --time 2026-01-02T20:15:30 --title '任务名_片段内容' --after 3

uv run --no-project python recording_catalog.py --catalog '/视频目录/视频列表.json' verify
```

`--after 3` 用于首次创建清单并从 004 开始。以后脚本根据 `last_sequence` 递增。清单里的旧视频可以保留记录，即使原视频已归档或删除。加 `verify --sequence 4` 可以只核对一条。

遇到跨文件系统的清单写入错误，脚本会恢复原文件名。文件名冲突会中止改名。

## 登记上传

```sh
uv run --no-project python recording_catalog.py --catalog '/视频目录/视频列表.json' \
  record-upload 4 --baidu-path '我的应用数据/bdpan/录屏/已核对的文件.webm'
```

哔哩哔哩接收稿件后，用 `record-upload 4 --bvid` 后接实际 BV 号，再用 `--title` 给出投稿标题。清单登记的是上传结果，审核状态以创作中心为准。

录屏、实际清单和账号凭据留在使用者的设备或网盘。
