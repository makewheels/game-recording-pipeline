# 游戏录屏处理流程

从 PS5 U 盘导入录屏，识别游戏任务、重命名、生成封面，备份到百度网盘并投稿到哔哩哔哩。程序记录上传结果和审核状态，核对通过后可清理副本并弹出 U 盘。

用一个 JSON 文件记录录屏的时间、总序号、原文件名、新文件名和 SHA-256。文件名格式为 `YYYYMMDD_总序号_任务名_片段内容.扩展名`。总序号至少三位，录制时分秒保存在清单里。同一日期下按文件名排列时，序号决定先后。

需要 macOS、Python 3.11+ 和 uv。Python 和 Apple Vision 的 Swift 源码位于 `src/game_recording_pipeline/`。系统临时目录和清单目录需位于同一文件系统，以便原子更新清单。

```sh
git clone https://github.com/makewheels/game-recording-pipeline.git
cd game-recording-pipeline
```

## 登记并核对视频

```sh
uv run game-recording-catalog --catalog '/视频目录/视频列表.json' \
  add '原录屏.webm' --time 2026-01-02T20:15:30 --title '任务名_片段内容' --after 3

uv run game-recording-catalog --catalog '/视频目录/视频列表.json' verify
```

`--after 3` 用于首次创建清单并从 004 开始。以后脚本根据 `last_sequence` 递增。清单里的旧视频可以保留记录，即使原视频已归档或删除。加 `verify --sequence 4` 可以只核对一条。

遇到跨文件系统的清单写入错误，脚本会恢复原文件名。文件名冲突会中止改名。

## 登记上传

```sh
uv run game-recording-catalog --catalog '/视频目录/视频列表.json' \
  record-upload 4 --baidu-path '我的应用数据/bdpan/录屏/已核对的文件.webm'
```

哔哩哔哩接收稿件后，用 `record-upload 4 --bvid` 后接实际 BV 号，再用 `--title` 给出投稿标题。清单登记的是上传结果，审核状态以创作中心为准。

录屏、实际清单和账号凭据留在使用者的设备或网盘。

## 插入 U 盘后自动归档

安装 FFmpeg、ImageMagick、Xcode 命令行工具，以及已登录的 `biliup` 和 `bdpan`。把 `workflow.example.json` 复制为 `workflow.local.json`，填写录屏目录、U 盘的卷 UUID、网盘目录和投稿设置。卷 UUID 可以通过 `diskutil info /Volumes/卷名` 查看。

```sh
uv run game-recording-usb --config workflow.local.json --scan
uv run game-recording-usb --config workflow.local.json --once
```

`--scan` 只列出候选录屏。`--once` 运行一轮导入和上传。程序从文件名读取录制时间，用 Apple Vision 识别画面里的任务提示。已知任务会用任务名命名，其他片段会尝试用画面中的目标文字命名。

B 站标题和网盘文件共用日期、总序号、任务名和片段内容。视频文件名带扩展名。已识别的任务名写作 `中文任务名 英文原名`。封面以实机画面为背景，大字号序号在第一行，任务名在第二行。投稿和网盘备份共用同一张 JPEG，网盘图片放在视频目录的 `B站封面/` 子文件夹里，清单登记文件路径和回执。

要把稿件加入已有的 B 站合集，可在配置里填写合集的 `bilibili_collection_id` 和小节的 `bilibili_section_id`。合集内已有视频的公开详情会在 `ugc_season` 和 `ugc_season.sections` 里返回这两个 ID。稿件开放浏览后，流程按录屏总序号依次加入小节并在清单中登记合集回执。启用自动清理时，流程也会等待合集回执。

确认目录、账号和导入结果后，可在配置里将 `delete_after_verification` 改为 `true`。弹出 U 盘后安装后台任务。

```sh
uv run game-recording-usb --config workflow.local.json --install
```

以后插入已绑定的 U 盘即可开始。只有视频和封面的网盘副本核对通过、B 站稿件开放浏览、U 盘原件 SHA-256 一致时，才会删除对应的 U 盘文件并弹出。弹出成功后再清理 `local_directory` 中由程序导入的电脑副本，JSON 和封面会继续保留。若已有手动归档的录屏，可让清单继续沿用原目录，同时将 `local_directory` 指向单独的电脑暂存目录。

网盘或传输失败会在后续轮次重试。B 站退回时，程序会读取稿件原因。明确的编码、封装或分辨率问题可以转码并补投一次。其他退回原因会留在清单中，并保留源文件供核对。
