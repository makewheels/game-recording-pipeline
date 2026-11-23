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

哔哩哔哩接收稿件后，用 `record-upload 4 --bvid` 后接实际 BV 号，再用 `--title` 给出投稿标题。登记投稿后，可以单独刷新指定序号的审核详情，包括已经归档的稿件。

```sh
uv run game-recording-usb --config workflow.local.json --audit 7
```

审核详情保存在清单的 `uploads.bilibili.audit`，包含 `traffic_limited` 和 `problem_detail`。创作中心提供的审核原因、标记时间段、取证截图和修改建议会随问题一起保存。自动归档在查询现有稿件时也会刷新这些字段。

录屏、实际清单和账号凭据留在使用者的设备或网盘。

## 插入 U 盘后自动归档

安装 FFmpeg、ImageMagick、Xcode 命令行工具，以及已登录的 `biliup` 和 `bdpan`。把 `workflow.example.json` 复制为 `workflow.local.json`，填写录屏目录、U 盘的卷 UUID、网盘目录和投稿设置。卷 UUID 可以通过 `diskutil info /Volumes/卷名` 查看。

```sh
uv run game-recording-usb --config workflow.local.json --scan
uv run game-recording-usb --config workflow.local.json --once
```

`--scan` 只列出候选录屏。`--once` 运行一轮导入和上传。程序从文件名读取录制时间，用 Apple Vision 识别画面里的任务提示。已知任务会用任务名命名，其他片段会尝试用画面中的目标文字命名。

B 站标题和网盘文件共用日期、总序号、任务名和片段内容。视频文件名带扩展名。已识别的任务名写作 `中文任务名 英文原名`。封面以实机画面为背景，第一行放大字号序号，第二行放片段名。两行按文字实际边缘与画布上下留等距。投稿和网盘备份共用同一张 JPEG，网盘图片放在视频目录的 `B站封面/` 子文件夹里，清单登记文件路径和回执。

用 `game-recording-catalog covers` 可以从清单重做封面。省略 `--sequence` 会生成整批。清单可用 `publication.cover_title` 指定封面文字，用 `publication.cover_frame_seconds` 记录取帧时刻。已归档的视频有 BV 号和取帧时刻时，程序可以从 B 站公开视频取帧。

```sh
uv run game-recording-catalog --catalog '/视频目录/视频列表.json' \
  covers --sequence 4 --output-dir '/指定封面输出目录'
```

要把稿件加入已有的 B 站合集，可在配置里填写合集的 `bilibili_collection_id` 和小节的 `bilibili_section_id`。合集内已有视频的公开详情会在 `ugc_season` 和 `ugc_season.sections` 里返回这两个 ID。稿件开放浏览后，流程按录屏总序号依次加入小节并在清单中登记合集回执。启用自动清理时，流程也会等待合集回执。

确认目录、账号和导入结果后，可在配置里将 `delete_after_verification` 改为 `true`。弹出 U 盘后安装后台任务。

```sh
uv run game-recording-usb --config workflow.local.json --install
```

以后插入已绑定的 U 盘即可开始。只有视频和封面的网盘副本核对通过、B 站稿件开放浏览且审核详情未显示流量受影响、U 盘原件 SHA-256 一致时，才会删除对应的 U 盘文件并弹出。弹出成功后再清理 `local_directory` 中由程序导入的电脑副本，JSON 和封面会继续保留。若已有手动归档的录屏，可让清单继续沿用原目录，同时将 `local_directory` 指向单独的电脑暂存目录。

网盘或传输失败会在后续轮次重试。B 站退回时，程序会读取稿件原因。明确的编码、封装或分辨率问题可以转码并补投一次。其他退回原因会留在清单中，并保留源文件供核对。


## 首次投稿后的审核跟踪

`--watch` 在每轮收到 BV 号后会继续查询创作中心，轮次之间间隔约两分钟。已手动登记和已归档的稿件也在查询范围内。遇到退回或流量限制时，程序保存审核详情，并在新增或变化时向标准错误输出一行 `bilibili_audit_issue` JSON。B 站公开状态与流量限制分别记录。

Hermes 等任务执行器可以定期运行以下命令。标准输出的 `issues` 包含待处理稿件的 BV 号、原片路径和 SHA-256、网盘路径、审核原因、标记区间和取证截图。查询失败会列入 `errors`，命令返回非零退出码，其余稿件仍会继续查询。处理时可用 BV 号和 `reject_reason_id` 识别同一审核问题。现有命令通过轮询读取审核结果。

```sh
uv run game-recording-usb --config workflow.local.json --audit-queue
```

- **百度网盘始终备份原始录屏。** 文件名可以按清单重命名，视频内容和 SHA-256 保持原样。B 站退回、限流、修改或补投时，继续保留百度网盘原片与上传回执。原片由清单的 `filename` 和 `sha256` 标识。
- **B 站可以投稿单独的处理版。** 根据审核提示定位画面，把剪辑、打码或转码结果另存，再运行以下命令登记。流水线会核对副本的 SHA-256，并在下一轮投稿。已有稿件时，登记和提交都会确认仍有退回或限流问题。修订版会获得新 BV 号，旧 BV 号和审核记录保留在 `bilibili_history`。旧稿已在合集时，修订版的位置需在创作中心调整。

```sh
uv run game-recording-usb --config workflow.local.json \
  --stage-bilibili-copy 7 --video '/处理版目录/007-打码版.mp4'
```

也可以只提交这一条处理版。接口结果不确定时，重跑只按投稿标题核对回执，避免重复提交。

```sh
uv run game-recording-usb --config workflow.local.json --submit-bilibili-copy 7
```
