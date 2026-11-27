---
name: narrated-story-video
description: 把「朗读音频 + 故事原文」合成逐句同步的硬字幕视频（竖屏/横屏皆可），全本地处理、不依赖云端 ASR 或图像生成。内置梦幻动态微光层（柔和呼吸月晕 + 无缝循环星尘微光）、插画自适应构图（横图竖屏模糊画框 + 底部暗遮罩）及中英文智能断句对齐。当用户要求给故事、散文、旁白、睡前故事配字幕做成视频，或把录屏/视频里的朗读音频提取出来做字幕视频，或需要按朗读停顿自动对齐字幕、裁剪片头杂音、给静态插画叠加梦幻氛围光效时，应使用本技能。也适用于已有 ASS 字幕需要重新导出视频的场景。不用于：需要词级精确对齐的卡拉 OK 效果、多机位画面剪辑、云端 TTS 生成朗读音频。
title: 朗读字幕视频生成器
version: 1.2.1
tags: [ffmpeg, ass, subtitle, story-video, bedtime-story, local-only]
---

# 朗读字幕视频生成器

> 一条命令完成：媒体源抽音轨 → 精确裁剪 → **标题后插入 1.2s 停顿（默认）** → 检测朗读停顿 → 原文按标点切句 → 字数比例对齐并吸附到停顿 → 生成 ASS 硬字幕与静态底图 + **梦幻动态微光层（呼吸月晕 + 飘浮星尘）** → ffmpeg 合成 mp4 → 抽帧自检。
>
> 全部处理在本机完成，仅依赖 Python 标准库与 ffmpeg，不上传任何音频或文本。

## 何时使用

- 「给这段故事原文配上字幕做成视频」「把这段朗读做成短视频」
- 源素材是聊天/录屏视频，需要抽出音轨、去掉片头爆音与片尾
- 需要字幕与朗读大致同步，但没有词级时间戳
- 已有 `.ass` 字幕，改完文字后重新导视频

不适用：需要精确逐字卡拉 OK 效果、需要多机位画面剪辑、需要云端 TTS 生成朗读音频。这些场景改用专业剪辑工具或 ASR 强制对齐服务。

## 前置条件

- `ffmpeg` 在 PATH，且编译含 `libass`、`libx264`（用 `ffmpeg -filters | grep ass` 确认）
- `ffprobe` **可选**：缺失时脚本自动改用 `ffmpeg -i` 解析探测（功能等价，自检信息略简）
- Python 3.8+（零第三方包）
- 已安装中文字体；默认 `Noto Sans CJK SC`，可用 `--font` 改（如 `Microsoft YaHei`）
- 主脚本：`scripts/make_story_video.py`（路径相对本技能目录）
- 二进制不在 PATH 时可用环境变量指定：`FFMPEG_BIN` / `FFPROBE_BIN`

### 云沙箱环境准备（华为云码道 CodeArts 实测，aarch64 / EulerOS）

沙箱镜像通常未预装 ffmpeg，且**外网大文件下载极慢（实测 11~21 KB/s，走不通）**；pip 镜像通道
是快源。安装命令显式钉华为云镜像源，保证任何华为云环境下可复现：

```bash
pip3 install imageio-ffmpeg \
  -i https://mirrors.huaweicloud.com/repository/pypi/simple \
  --trusted-host mirrors.huaweicloud.com
FFBIN=$(python3 -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())")
ln -sf "$FFBIN" /usr/local/bin/ffmpeg        # 无写权限时改放 ~/.local/bin 并加入 PATH
ffmpeg -filters 2>/dev/null | grep -i ass     # 确认 ass / subtitles 滤镜存在
```

`imageio-ffmpeg` 自带对应架构的静态 ffmpeg（实测含 `libass` / `libx264` / `libx265` 与完整字体栈），
满足本技能全部依赖；它不带 ffprobe，按上文「可选」处理即可，不必再找 ffprobe。

## 工作流

### 1. 确认素材与输出形态

确认三项：朗读音频（可为 mp3/wav，也可直接传 mp4 由脚本抽音轨）、原文 UTF-8 文本文件（可选用 `--text2` 传第二语种对照译文）、输出 mp4 路径。
询问或判断两项输出选择：
- **画面比例**：默认 `portrait`（`1080x1920` 竖屏）；横屏用 `--orientation landscape`（`1920x1080`，也可用 `--width` / `--height` 自定义）。
- **字幕语言**：用 `--subtitle-lang auto|en|zh|bilingual` 选择只显示英文、只显示中文还是中英双语（如仅提供单语种文本，脚本会自动回退为该可用语种）。英文模式按词边界换行（默认 `--font Georgia`、`--line-chars 34`），中文模式默认 `--font "Noto Sans CJK SC"`、`--line-chars 15`。

### 2. 定位裁剪点（源音频未经处理时）

```bash
python scripts/make_story_video.py analyze --audio <源音频或视频> --range 0:12 --window 0.05
python scripts/make_story_video.py analyze --audio <源音频或视频> --range <总时长-3>:<总时长> --window 0.02
```

片头判读能量表：「高电平突发 → 近 0 dB 静音 → 稳定高电平」即爆音，起点选静音区中部；不要粗暴往后挪 1 秒，会切掉有效内容。

**结尾必须同样做能量判读**：找到末字完整收音（高电平段结束 + 衰减到底噪的位置），终点定在衰减结束再往后 0.1–0.3s。`--trim-end -1`（距结尾 1 秒）这类固定值最常把末字拦腰切断。同时留意文件末尾被硬截断的突发高电平（录音停止提示音 / 下一段起音），终点要落在它之前。

`--trim-start` / `--trim-end` 传正数＝源音频绝对秒；`--trim-end` 传负数＝距结尾 N 秒。推荐按能量表算出绝对秒再传。

### 3. 标题后停顿（标题连读素材默认必做）

**问题**：朗读常把独立标题行（如「第 N 天 · 标题」）和正文首句连读，标题字幕一闪而过、观感太赶。这是此类素材的常见问题，实测 **1.2s** 听感合适，此后按默认值执行，不必每次再问。

**做法**：`build` 的自动对齐分支默认插入 1.2s 静音（等同 `--title-pause 1.2`），并**基于插入后的音频**重新做停顿检测与对齐。插入的静音成为最大句间停顿，标题/正文边界自动吸附过去，效果上是「标题延长 + 其余句整体后移」，ASS / SRT / timeline.json 同源更新，无需事后手工平移（因重新吸附，实际位移可能略大于 1.2s，见 `references/alignment.md`）。

**适用范围**：仅当原文首行是独立标题（无句读、长度适中）时默认启用。散文 / 旁白首句含逗号句号，脚本会自动跳过并打印提示，避免把静音插进第一句话中间；这类素材若确需停顿，显式传 `--title-pause` + `--title-pause-at`，或用 `--title-pause 0` 明确关闭。

**切点定位**（最关键，切错会砍掉标题末字）：

- 标题与正文连读时两者之间**检不出静音**；第一个静音段往往在标题内部（实测案例中出现在标题内逗号之后的 1.09–1.51s），绝不能拿它当切点。
- 不给 `--title-pause-at` 时脚本自动估算：按标题字数估纯语音时长（默认 3.8 字/秒）映射到真实时间，再在 ±0.5s 内找「末字收音低谷 → 正文起音跃升」的最后一个边界。实测自动估算值与人工确认值相差 1ms（2.844s vs 2.845s）。
- 仍要复核：`analyze --audio <源> --range <at-0.6>:<at+0.8> --window 0.02` 看低谷；必要时把切点前后各切一个短片段送 ASR（前段含完整标题、后段以正文首字开头）。确认后**显式传** `--title-pause-at <秒>`。

**音画同源铁律**：音频与字幕必须一起改。只挪 ASS 不动音频 → 字幕整体落后朗读；只插静音不挪字幕 → 字幕整体提前。`--ass` 重导分支默认不插静音、并忽略显式 `--title-pause`，因为外部字幕无法自动改；若改用已插静音的音频，字幕必须来自同一次生成的 ASS。

**校验**：`silencedetect` 实测静音长度 1.20s；抽帧看停顿中点仍显示标题、停顿结束后已切正文；ASR 复核前 8s 无吞字。停用：`--title-pause 0`。细节见 `references/alignment.md`。

### 4. 先 dry-run 校对切句与时间轴

```bash
python scripts/make_story_video.py build \
  --audio <源> --text story.txt --output story.mp4 \
  --trim-start 9.5 --trim-end 94.6 \
  --title-pause 1.2 --title-pause-at 2.85 --dry-run
```

读取工作目录 `timeline.json`：确认句数合理、每句时长 ≥1s、标题与尾句独立成句；标题句 End 应落在插入静音的末尾（即正文起音处）。dry-run 同样会生成插入静音后的 `a2-paused.wav`，可直接试听停顿。
切句过多/过少时调整 `--silence-db`、`--silence-min` 或人工整理原文换行。

### 5. 正式合成

去掉 `--dry-run` 重跑。常用附加参数：

| 目标 | 参数 |
|---|---|
| 换背景主题 | `--theme starry\|night\|dawn\|ocean` |
| 用自备图/视频做底 | `--background path/to/img.webp` 或 `.mp4`；默认自动加底部渐变暗遮罩保障字幕可读性（`--no-scrim` 关闭） |
| 梦幻发光与动态微光 | 默认开启「呼吸月晕 + 12s 无缝循环星尘微光」叠加层（亚像素同帧率丝滑移动）；调强度用 `--glow-intensity 0.5~1.8`（默认 `1.0`），`--no-dreamy` 关闭 |
| 背景图适配模式 | `--bg-fit auto\|cover\|blur-frame`（默认 `auto`：竖屏配横图用模糊底+完整画框，其余等比居中裁剪铺满） |
| 画面规格（横/竖屏） | `--orientation portrait\|landscape`（默认 `portrait` 1080×1920；`landscape` 为 1920×1080） |
| 语言与双语字幕 | `--text2 trans.txt`（按段落对照第二语种，或单文件用 `\|\|\|` 分隔）；`--subtitle-lang auto\|en\|zh\|bilingual`（两级 DP 句级配对同窗对齐，单语文本自动回退） |
| 离线精对齐引擎 | `--align auto\|asr\|silence`（`auto` 优先使用本机离线 `whisper-cli` 词级强对齐 + 静音边界微调，无模型时自动回退纯静音；可配 `--asr-bin` / `--asr-model`） |
| 无标题音频展示故事名 | `--header-title "Title" --header-title2 "中文标题"`（不占音频时间轴，按所选字幕语言自动选用或双语拼接；配合 `--title-pause 0 --title-first 0 --title-last 0`） |
| 结尾停留更久 | `--tail 3` |
| 标题后停顿（默认 1.2s） | `--title-pause 1.2 --title-pause-at 2.85`；不给切点则自动估算；散文首句自动跳过；`--title-pause 0` 关闭 |
| `--ass` 重导 | 默认不插静音；音频必须与该 ASS 来自同一次生成 |
| 同步导出可编辑字幕 | 默认在输出旁生成同名 `.ass`；加 `--srt` 再导出 SRT |
| 校对后重导 | `--ass story.ass`（跳过切句与对齐，直接用改后的字幕） |

### 6. 自检（不可跳过）

读取工作目录 `frames/check-01.jpg`、`check-NN.jpg`、`check-XX.jpg`（分别取首句、中间句、尾句的字幕中点）。用图像查看确认：中文无豆腐块、字幕不越界、背景无方形光斑、尾句停留正常。
若尾句 emoji 变成方块，保持默认 `--keep-emoji` 关闭即可（脚本自动把非 BMP 字符及带变体选择符的 BMP 符号换成 ✦）。

### 7. 微调

- 标题后停顿想改长短：改 `--title-pause` 重跑 `build`（自动重算整条时间轴），**不要**在成品 ASS 上手工平移；若已交付只能手改，按 `references/pitfalls.md` 的流程同步改 ASS/SRT/timeline 并备份
- 个别句提前/滞后：改 `.ass` 里对应行的 Start/End，再 `--ass` 重导；或调 `--snap`（吸附阈值）、`--min-cue`（最短显示）
- 末字没读完 / 结尾突然断掉：按第 2 步重看尾部能量，把 `--trim-end` 挪到收音衰减结束之后。只补音频尾部时前段字幕不必重算：把 ASS 末句 End 改成新的「音频时长 + `--tail`」，再用 `--ass` + `--background` 复用原画面重导即可
- 整体偏快/偏慢：优先检查 `--trim-start` 是否切错，其次检查原文是否漏句或多句
- 字幕字号/颜色：`--font-size`、`--title-size`、`--text-color`（RGB hex）、`--outline-color`

对齐原理与调参细节见 `references/alignment.md`；环境与故障排查见 `references/pitfalls.md`。

## 完整示例（Windows）

```bash
python scripts/make_story_video.py build \
  --audio D:/media/story-1.mp4 \
  --text D:/media/story.txt \
  --output D:/media/story-1-subtitled.mp4 \
  --trim-start 9.5 --trim-end 94.6 \
  --title-pause 1.2 --title-pause-at 2.85 \
  --theme starry --tail 2 --srt
```

`--title-pause-at` 的秒数按第 3 步复核后再填；若暂时不知道，先去掉该参数跑 `--dry-run`，从日志里读自动估算值并复核。

若 `python` 不在 PATH，使用 Python 可执行文件绝对路径调用。

## 关键约束

- **全程本地**：禁止把用户音频/文本提交到云端 ASR 或图像生成服务，除非用户明确要求。本技能的价值之一就是零上传、零额外费用。
- **标题后停顿默认开启**：自动对齐分支恒插 1.2s 静音，音频与字幕必须同源；`--ass` 重导是唯一例外（默认关闭）。切点必须复核，不能盲信自动值。
- **不要覆盖用户原素材**：输出写到新文件；工作目录默认 `<输出目录>/.story-video-work/`，保留 `bg.png`、`subs.ass`、`timeline.json`、`frames/` 供复核。
- **对齐是估算**：基于字数比例 + 停顿吸附，连读句可能有 0.3–0.5s 偏差。交付时提示用户抽看并反馈需微调的句子。
- **检查中间产物**：`timeline.json` 与抽帧是判断成败的依据，不要只看命令退出码。

## 检查清单

- [ ] `ffmpeg -filters | grep ass` 确认 libass 可用，中文字体已安装
- [ ] 片头与片尾都跑过 `analyze` 能量判读，切点落在静音区 / 收音衰减结束区
- [ ] `--dry-run` 产出的 `timeline.json` 句数与每句时长已核对
- [ ] 抽帧确认中文无豆腐块、尾句显示到片尾、背景无方形光斑
- [ ] 交付时附上可编辑 `.ass`，并提示用户抽看对齐偏差
- [ ] 标题后停顿的自动切点已用 `analyze` 能量表 / ASR 短片段复核，并显式传 `--title-pause-at`

## 实测坑点（三个最常踩）

- **emoji 变豆腐块**：`Noto Sans CJK` 无 emoji 字形，🌌 这类字符会渲染成方块——保持默认 `--keep-emoji` 关闭，脚本自动替换为 ✦。
- **背景出现方形光斑**：大半径光晕的滑动窗口过小时会露出方形边界——抽帧自检（第 6 步）是发现手段，调小光晕半径或增大窗口可解。
- **结尾机械截尾切断末字**：按「结尾前 1 秒」这类固定值截尾最常把末字拦腰切断——必须用 `analyze` 能量判读定位「末字收音衰减结束」再定终点（第 2 步）。

## 版本记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| 1.2.1 | 2026-11-27 | 新增本地离线 `whisper-cli` 词级强对齐引擎（`--align auto\|asr\|silence` + 静音间隙中点贴合，消除长音频语速波动造成的 2–5s 提前漂移）；优化英文纯静音兜底阈值（`-38dB/0.22s/snap=1.35s`）；修复中文双破折号 `——` 被劈断的问题 |
| 1.2.0 | 2026-11-27 | 英文朗读适配（`--lang auto\|en\|zh` 英文标点切句、缩写保护、按词平衡换行、默认 Georgia）；多语种字幕选择（`--text2` 两级 DP 句级配对同窗对齐 + `--subtitle-lang auto\|en\|zh\|bilingual`，单语文本自动回退）；自定义插画背景升级（`--orientation portrait\|landscape`、横图竖屏 `blur-frame` 模糊画框、等比裁切、底部渐变暗遮罩、`--header-title`/`--header-title2`）；新增默认梦幻动态微光层（呼吸月晕 + 12s 无缝循环亚像素飘浮星尘、`--glow-intensity` / `--no-dreamy`） |
| 1.1.0 | 2026-11-10 | `ffprobe` 可选化（缺失时用 `ffmpeg -i` 解析兜底），新增 `FFMPEG_BIN` / `FFPROBE_BIN` 环境变量及云沙箱安装指南 |
| 1.0.0 | 2026-11-10 | 首个公开发布版：片头/片尾能量裁剪、标题后停顿默认 1.2s（含自动切点估算）、朗读停顿检测、字数比例对齐并吸附、程序化背景、`--ass` 重导、emoji 自动替换、抽帧自检 |
