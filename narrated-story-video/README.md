# narrated-story-video · 朗读字幕视频生成器

把「朗读音频 + 故事原文」合成逐句同步的硬字幕视频（竖屏/横屏皆可）。全本地处理，
不依赖云端 ASR 或图像生成，仅需 Python 3.8+（零第三方包）与 ffmpeg。

## 功能

- 媒体源抽音轨 → 精确裁剪（能量表判读爆音/尾音）→ 标题后插入停顿（默认 1.2s，无标题音频可 `--title-pause 0`）
- 检测朗读停顿，自动识别中/英文并按标点切句，字数比例对齐并吸附到停顿（英文按词平衡换行）
- 支持字幕语言选择（`--subtitle-lang auto|en|zh|bilingual`，可配 `--text2` 两级句级配对同时间窗输出中英双语，单语自动回退）
- 生成 ASS 硬字幕 + 程序化背景或自定义插画背景（支持竖屏/横屏切换、横图竖屏模糊画框、底部渐变暗遮罩、**默认叠加呼吸月晕与飘浮星尘梦幻动态层**）→ ffmpeg 合成 mp4 → 抽帧自检
- 已有 `.ass` 字幕时可重新导出视频

## 前置条件

- `ffmpeg` 在 PATH，编译含 `libass`、`libx264`（`ffmpeg -filters | grep ass` 确认）
- `ffprobe` 可选（缺失时脚本自动用 `ffmpeg -i` 解析兜底）
- Python 3.8+，仅标准库
- 中文字体（默认 `Noto Sans CJK SC`，`--font` 可改）

云沙箱无 ffmpeg 时的安装配方（华为云镜像源）见 `SKILL.md`「云沙箱环境准备」。

## 快速上手

```bash
# 1. 能量表定位裁剪点
python scripts/make_story_video.py analyze --audio story.mp4 --range 0:12 --window 0.05

# 2. 一键合成（裁剪 + 停顿检测 + 对齐 + 字幕 + 背景 + mp4）
python scripts/make_story_video.py build \
  --audio story.mp4 --text story.txt --out story-subtitled.mp4 \
  --trim-start 3.2 --trim-end -1.5
```

用法细节、参数口径与对齐算法见 `SKILL.md` 与 `references/`。

## 目录结构

```text
SKILL.md                    技能定义（完整工作流与参数说明）
scripts/make_story_video.py 主脚本
references/alignment.md     对齐算法与边界情况
references/pitfalls.md      常见坑与排错
```

## 隐私

全部处理在本地完成，不上传任何音频或文本。
