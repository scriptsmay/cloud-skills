#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""朗读音频 + 故事原文 -> 逐句同步硬字幕视频（纯本地，零第三方依赖）。

依赖：Python 3.8+、ffmpeg（含 libass、libx264、libmp3lame 更佳）；ffprobe 可选，缺失时自动用
ffmpeg -i 解析兜底。二进制路径可用环境变量 FFMPEG_BIN / FFPROBE_BIN 指定。
子命令：
  analyze  输出音频时长、停顿段与能量窗，用于定位片头杂音/裁剪点
  build    生成背景、ASS 字幕并合成 mp4
"""
import argparse
import bisect
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import zlib

FF = os.environ.get("FFMPEG_BIN", "ffmpeg")
FP = os.environ.get("FFPROBE_BIN", "ffprobe")

THEMES = {
    # top/mid/bottom 为竖向渐变基色，glow 为中心柔光增量
    "starry": dict(top=(5, 9, 24), mid=(11, 21, 46), bottom=(6, 24, 42),
                   glow=(15, 24, 46), star=True, sea=True),
    "night": dict(top=(4, 7, 18), mid=(8, 14, 32), bottom=(5, 10, 24),
                  glow=(10, 16, 34), star=False, sea=False),
    "dawn": dict(top=(16, 10, 30), mid=(38, 20, 52), bottom=(20, 14, 38),
                 glow=(40, 22, 40), star=False, sea=False),
    "ocean": dict(top=(3, 10, 22), mid=(5, 22, 38), bottom=(4, 32, 44),
                  glow=(10, 30, 46), star=False, sea=True),
}

# ---------------------------------------------------------------- 基础工具


def run(cmd, cwd=None, capture=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=capture,
                       text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError("命令失败 (%d): %s\n%s" % (
            p.returncode, " ".join(str(x) for x in cmd), (p.stderr or "")[-2000:]))
    return p


def _ffmpeg_probe_fallback(path):
    """ffprobe 不可用时的兜底：解析 `ffmpeg -i` 的 stderr。
    返回 (duration秒 或 None, [stream 类型小写...], (width, height) 或 None)。"""
    p = subprocess.run([FF, "-hide_banner", "-i", path],
                       capture_output=True, text=True, errors="replace")
    err = p.stderr or ""
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    dur = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else None
    streams = [s.lower() for s in
               re.findall(r"Stream #\d+:\d+[^:]*: (Video|Audio|Subtitle)", err)]
    dm = re.search(r"Video:[^\n]*?,\s*(\d{2,5})x(\d{2,5})(?:[\s,\[])", err)
    dims = (int(dm.group(1)), int(dm.group(2))) if dm else None
    return dur, streams, dims


def probe_duration(path):
    try:
        out = run([FP, "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", path]).stdout.strip()
        return float(out.splitlines()[0])
    except Exception:
        dur, _, _ = _ffmpeg_probe_fallback(path)
        if dur is None:
            raise RuntimeError("无法探测时长：%s（ffprobe 不可用，ffmpeg -i 解析也失败）" % path)
        return dur


def probe_streams(path):
    try:
        out = run([FP, "-v", "error", "-show_entries", "stream=codec_type",
                   "-of", "csv=p=0", path]).stdout
        return [s.strip() for s in out.splitlines() if s.strip()]
    except Exception:
        _, streams, _ = _ffmpeg_probe_fallback(path)
        return streams


def probe_dimensions(path):
    try:
        out = run([FP, "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=width,height",
                   "-of", "csv=p=0:s=x", path]).stdout.strip()
        w, h = out.splitlines()[0].split("x")
        return int(w), int(h)
    except Exception:
        _, _, dims = _ffmpeg_probe_fallback(path)
        if dims is None:
            raise RuntimeError("无法探测画面分辨率：%s" % path)
        return dims


VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts", ".ts")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")


def is_video(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        return True
    # ffprobe 把 PNG/JPG 也报成 video 流，图片必须按扩展名先排除
    if ext in IMAGE_EXTS:
        return False
    try:
        return "video" in probe_streams(path)
    except Exception:
        return False


def wav_path(work, name):
    return os.path.join(work, name + ".wav")


def prepare_audio(src, work, trim_start, trim_end, force_wav=False):
    """视频 -> 抽音轨；按 trim 精确裁剪。统一产出单声道 44.1k PCM WAV。"""
    cur = src
    generated = None
    if is_video(src) or force_wav:
        generated = wav_path(work, "a0-extracted")
        run([FF, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vn",
             "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", generated])
        cur = generated
    dur = probe_duration(cur)
    end = dur
    if trim_end is not None:
        end = dur + trim_end if trim_end <= 0 else min(dur, trim_end)
    start = max(0.0, trim_start or 0.0)
    if end <= start:
        raise ValueError("裁剪区间无效：start=%.2f end=%.2f (dur=%.2f)" % (start, end, dur))
    if start > 0 or end < dur - 0.01:
        nxt = wav_path(work, "a1-trimmed")
        run([FF, "-hide_banner", "-loglevel", "error", "-y", "-ss", "%.6f" % start,
             "-i", cur, "-t", "%.6f" % (end - start),
             "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", nxt])
        cur = nxt
        if generated:
            try:
                os.remove(generated)
            except OSError:
                pass
    return cur, probe_duration(cur)


def silence_segments(audio, noise_db=-42.0, min_len=0.30):
    """返回有声段 [(start,end)] 与总时长。"""
    p = run([FF, "-hide_banner", "-i", audio, "-af",
             "silencedetect=noise=%.0fdB:d=%.2f" % (noise_db, min_len),
             "-f", "null", "-"], capture=True)
    dur = probe_duration(audio)
    events = [(k, float(v)) for k, v in
              re.findall(r"silence_(start|end):\s*([\d.]+)", p.stderr)]
    segs, cur = [], 0.0
    for kind, t in events:
        if kind == "start" and t > cur + 0.05:
            segs.append([cur, t])
        elif kind == "end":
            cur = t
    if dur > cur + 0.05:
        segs.append([cur, dur])
    return [s for s in segs if s[1] - s[0] >= 0.25], dur


def energy_table(audio, start, length, window):
    """按窗口输出 RMS 电平（dB，0 dB≈数字静音，80 dB≈接近满幅）。"""
    import array
    p = subprocess.run([FF, "-v", "error", "-ss", "%.3f" % start,
                        "-t", "%.3f" % length, "-i", audio,
                        "-f", "s16le", "-ac", "1", "-ar", "44100", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    b = p.stdout
    a = array.array("h")
    a.frombytes(b[:len(b) // 2 * 2])
    n = max(1, int(44100 * window))
    out = []
    for i in range(0, max(0, len(a) - n), n):
        c = a[i:i + n]
        rms = math.sqrt(sum(x * x for x in c) / len(c))
        out.append((start + i / 44100, 20 * math.log10(rms + 1e-9)))
    return out


# ---------------------------------------------------------------- 标题后停顿


def is_title_line(s, lang="zh"):
    """首行是否像独立标题：无句读、长度适中。散文/旁白首句不应插标题停顿。"""
    if lang == "en":
        words = s.split()
        return (1 <= len(words) <= 9
                and not re.search(r"[.,?!;:]", s))
    return (2.0 <= weight(s) <= 24.0
            and not re.search(r"[，。？！；：、]", s))


def auto_title_pause_at(audio, chunks, rate=3.8, noise_db=-42.0, min_len=0.30):
    """估算「标题朗读结束 / 正文起音前」的真实时间（秒）。

    睡前故事朗读常把标题与正文连读，标题与正文之间**没有**可检出的静音段，
    「第一个语音段结束」往往落在标题内部（如「第 N 天，」之后），不能直接用。

    策略：按标题字数估算纯语音时长 -> 沿有声段映射到真实时间 -> 在附近 ±0.5s
    用 20ms 能量窗找「最后一个能量低谷 + 随后起音跃升」的边界，即正文起音点。
    估算值仅供起点，交付前仍应用 analyze 能量表 / ASR 短片段复核；
    拿不准就用 --title-pause-at 显式给秒数。
    """
    segs, dur = silence_segments(audio, noise_db, min_len)
    est = weight(chunks[0]) / rate
    acc, at = 0.0, min(dur, est)
    for s, e in segs:
        if acc + (e - s) >= est:
            at = s + (est - acc)
            break
        acc += e - s
    lo, hi = max(0.0, at - 0.50), min(dur, at + 0.50)
    if hi - lo < 0.20:
        return round(at, 3)
    rows = energy_table(audio, lo, hi - lo, 0.02)
    best = at
    for i in range(1, len(rows) - 2):
        dip = rows[i - 1][1] - rows[i][1]
        rise = rows[i + 2][1] - rows[i][1]
        if dip >= 4.0 and rise >= 15.0:
            best = rows[i][0] + 0.02
    return round(best, 3)


def insert_title_pause(audio, work, at, pause):
    """在 at 秒处插入 pause 秒静音，产出单声道 44.1k WAV，返回 (路径, 新时长)。"""
    out = wav_path(work, "a2-paused")
    fmt = "aformat=sample_fmts=s16:sample_rates=44100:channel_layouts=mono"
    fc = ("[0:a]atrim=start=0:end=%.6f,asetpts=PTS-STARTPTS,%s[a];"
          "[0:a]atrim=start=%.6f,asetpts=PTS-STARTPTS,%s[b];"
          "anullsrc=r=44100:cl=mono,atrim=start=0:end=%.6f,"
          "asetpts=PTS-STARTPTS,%s[s];[a][s][b]concat=n=3:v=0:a=1[out]"
          % (at, fmt, at, fmt, pause, fmt))
    run([FF, "-hide_banner", "-loglevel", "error", "-y", "-i", audio,
         "-filter_complex", fc, "-map", "[out]", "-ac", "1", "-ar", "44100",
         "-c:a", "pcm_s16le", out])
    return out, probe_duration(out)


# ---------------------------------------------------------------- 文本与对齐

SPLIT_ZH = re.compile(r"(?<=[，。？！：；、…])(?![”\"』」）\)])|(?<=——)(?![”\"』」])")
EN_ABBREV = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Prof|St|Sr|Jr|vs|etc|Inc|Ltd)\."
    r"|\b(?:a\.m\.|p\.m\.|U\.S\.)",
    re.IGNORECASE,
)
SPLIT_EN_SEP = re.compile(r'([.?!;:,—])(["\'”’\)\]]*)(?=\s|$)')


def detect_lang(raw):
    cjk = sum(1 for c in raw if "\u4e00" <= c <= "\u9fff")
    alpha = sum(1 for c in raw if c.isalpha())
    if alpha == 0:
        return "zh"
    return "en" if (cjk / alpha) < 0.15 else "zh"


def sanitize(s, keep_emoji):
    s = s.replace("{", "｛").replace("}", "｝").replace("\\", "／")
    if not keep_emoji:
        s = re.sub(r"[\u2600-\u27bf\u2b00-\u2bff]\ufe0f", "✦", s)
        s = s.replace("\ufe0f", "")
        s = "".join("✦" if ord(c) > 0xFFFF else c for c in s)
    return s


def _split_en_line(line):
    """对单段英文按句子/长从句切分，保护缩写、小数、省略号与引号，并合并过短逗号碎片。"""
    tokens = {}

    def stash(m):
        key = "\x00T%d\x00" % len(tokens)
        tokens[key] = m.group(0)
        return key

    tmp = EN_ABBREV.sub(stash, line)
    tmp = re.sub(r"\d+\.\d+", stash, tmp)
    tmp = re.sub(r"\.{3,}|…", stash, tmp)
    # 在句末/分句标点（含紧随的右引号/右括号）后切分，同时避免误拆开引号
    marked = SPLIT_EN_SEP.sub(r"\1\2\n", tmp)
    parts = [p.strip() for p in marked.splitlines() if p.strip()]

    restored = []
    for p in parts:
        for k, v in tokens.items():
            p = p.replace(k, v)
        restored.append(p)

    # 合并过短碎片（如 ", she said." 或 "twinkling,"）以免字幕一闪而过
    merged = []
    i = 0
    while i < len(restored):
        cur = restored[i]
        words = cur.split()
        is_tiny_comma = (cur.endswith(",") and (len(words) <= 2 or len(cur) <= 14))
        is_post_adverb = (len(words) == 1 and cur.lower().rstrip(",") in (
            "too", "though", "however", "indeed", "please"))
        if is_post_adverb and merged:
            merged[-1] = merged[-1] + " " + cur
            i += 1
            continue
        if is_tiny_comma and i + 1 < len(restored):
            restored[i + 1] = cur + " " + restored[i + 1]
            i += 1
            continue
        if merged and len(words) <= 3 and len(cur) <= 18 and not cur.endswith(",") and merged[-1].endswith(","):
            merged[-1] = merged[-1] + " " + cur
            i += 1
            continue
        merged.append(cur)
        i += 1
    return merged


def split_paragraphs(raw, keep_emoji=False, lang="zh"):
    """按空行保留段落结构切分原文，返回 (chunks, para_ids)。"""
    blocks = [b.strip() for b in re.split(r"\r?\n\s*\r?\n", raw.strip()) if b.strip()]
    if not blocks:
        raise ValueError("文本为空或无法切分")
    chunks, para_ids = [], []
    for pid, blk in enumerate(blocks):
        before = len(chunks)
        for line in blk.splitlines():
            line = re.sub(r"^#{1,6}\s*", "", line.strip())
            if not line:
                continue
            pieces = _split_en_line(line) if lang == "en" else SPLIT_ZH.split(line)
            for piece in pieces:
                piece = sanitize(piece.strip(), keep_emoji)
                if not piece:
                    continue
                if chunks and all(not ("\u4e00" <= c <= "\u9fff" or c.isalnum()) for c in piece):
                    chunks[-1] += piece
                else:
                    chunks.append(piece)
                    para_ids.append(pid)
        if len(chunks) == before:
            continue
    if not chunks:
        raise ValueError("文本为空或无法切分")
    return chunks, para_ids


def split_text(raw, keep_emoji=False, lang="zh"):
    chunks, _ = split_paragraphs(raw, keep_emoji=keep_emoji, lang=lang)
    return chunks


def _split_major_sentences(text, lang):
    """按句末标点拆出整句列表，供两级双语对齐使用。"""
    s = text.strip()
    if lang == "en":
        tokens = {}
        def stash(m):
            k = "\x00M%d\x00" % len(tokens)
            tokens[k] = m.group(0)
            return k
        s = EN_ABBREV.sub(stash, s)
        s = re.sub(r"\d+\.\d+", stash, s)
        s = re.sub(r"\.{3,}|…", stash, s)
        marked = re.sub(r'([.?!])(["\'”’\)\]]*)(?=\s|$)', r'\1\2\n', s)
        out = []
        for x in marked.splitlines():
            x = x.strip()
            if not x:
                continue
            for k, v in tokens.items():
                x = x.replace(k, v)
            out.append(x)
        return out
    marked = re.sub(r'([。？！])([”"』」\)\]]*)', r'\1\2\n', s)
    return [x.strip() for x in marked.splitlines() if x.strip()]


def _split_clause_sentences(text, lang):
    """按逗号/分号等从句标点切分，并合并过短碎片。"""
    s = text.strip()
    if lang == "en":
        return _split_en_line(s)
    marked = re.sub(r'(——|[，。？！：；、]|(?<!—)—(?!—))([”"』」\)\]]*)(?![”"』」\)\]]|[，。？！：；、])', r'\1\2\n', s)
    raw = [x.strip() for x in marked.splitlines() if x.strip()]
    merged = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if len(c) <= 5 and i + 1 < len(raw) and c.endswith(("，", "、")):
            raw[i + 1] = c + raw[i + 1]
            i += 1
            continue
        merged.append(c)
        i += 1
    return merged


def _dp_align_lists(list1, list2, lang1, lang2):
    """Gale-Church 长度比动态规划对齐两组句子列表，支持 1:1 / 2:1 / 1:2 / 3:1 / 1:3。"""
    j1 = " " if lang1 == "en" else ""
    j2 = " " if lang2 == "en" else ""
    m, n = len(list1), len(list2)
    if m == 0 or n == 0:
        return [(j1.join(list1), j2.join(list2))]
    w1 = [max(0.5, weight(x)) for x in list1]
    w2 = [max(0.5, weight(x)) for x in list2]
    ratio = sum(w2) / max(1.0, sum(w1))
    INF = 1e18
    dp = [[INF] * (n + 1) for _ in range(m + 1)]
    back = [[None] * (n + 1) for _ in range(m + 1)]
    dp[0][0] = 0.0
    moves = [(1, 1, 0.0), (2, 1, 1.5), (1, 2, 1.5), (3, 1, 4.0), (1, 3, 4.0)]
    for i in range(m + 1):
        for j in range(n + 1):
            if dp[i][j] >= INF:
                continue
            for di, dj, pen in moves:
                ni, nj = i + di, j + dj
                if ni <= m and nj <= n:
                    s1 = sum(w1[i:ni]) * ratio
                    s2 = sum(w2[j:nj])
                    diff = abs(s1 - s2) / max(3.0, (s1 + s2) * 0.5)
                    cost = dp[i][j] + diff * 10.0 + pen
                    if cost < dp[ni][nj]:
                        dp[ni][nj] = cost
                        back[ni][nj] = (i, j)
    if dp[m][n] >= INF:
        return [(j1.join(list1), j2.join(list2))]
    pairs = []
    i, j = m, n
    while i > 0 or j > 0:
        pi, pj = back[i][j]
        pairs.append((j1.join(list1[pi:i]), j2.join(list2[pj:j])))
        i, j = pi, pj
    pairs.reverse()
    return pairs


def align_bilingual_texts(raw1, raw2, lang1, lang2, keep_emoji=False):
    """按自然段进行两级 DP 句级配对（整句对齐 -> 超长句从句对齐），产出严丝合缝的 (p1_chunks, p2_chunks)。"""
    b1 = [re.sub(r"^#{1,6}\s*", "", b.strip()) for b in re.split(r"\r?\n\s*\r?\n", raw1.strip()) if b.strip()]
    b2 = [re.sub(r"^#{1,6}\s*", "", b.strip()) for b in re.split(r"\r?\n\s*\r?\n", raw2.strip()) if b.strip()]
    if len(b1) != len(b2):
        # 段落数不同则全篇合并为 1 段做全局句级对齐
        b1 = [" ".join(b1) if lang1 == "en" else "".join(b1)]
        b2 = [" ".join(b2) if lang2 == "en" else "".join(b2)]

    c1_out, c2_out = [], []
    for p1, p2 in zip(b1, b2):
        p1_line = " ".join(x.strip() for x in p1.splitlines() if x.strip())
        p2_line = " ".join(x.strip() for x in p2.splitlines() if x.strip()) if lang2 == "en" else "".join(x.strip() for x in p2.splitlines() if x.strip())
        maj1 = _split_major_sentences(p1_line, lang1)
        maj2 = _split_major_sentences(p2_line, lang2)
        for m1, m2 in _dp_align_lists(maj1, maj2, lang1, lang2):
            need_refine = ((lang1 == "en" and len(m1) > 90) or (lang1 == "zh" and len(m1) > 42)
                           or (lang2 == "en" and len(m2) > 90) or (lang2 == "zh" and len(m2) > 42))
            if need_refine:
                sub1 = _split_clause_sentences(m1, lang1)
                sub2 = _split_clause_sentences(m2, lang2)
                if len(sub1) > 1 and len(sub2) > 1:
                    for s1, s2 in _dp_align_lists(sub1, sub2, lang1, lang2):
                        c1_out.append(sanitize(s1, keep_emoji))
                        c2_out.append(sanitize(s2, keep_emoji))
                    continue
            c1_out.append(sanitize(m1, keep_emoji))
            c2_out.append(sanitize(m2, keep_emoji))
    return c1_out, c2_out


def weight(s):
    return sum(1.0 if "\u4e00" <= c <= "\u9fff" else 0.25 for c in s)


def _align_segment_range(chunks, segs, start_t, end_t, snap=0.90, min_cue=1.0):
    """把 chunks 按字数权重铺在 [start_t, end_t] 内的语音段上并吸附停顿。"""
    sub_segs = []
    for a, b in segs:
        ca, cb = max(start_t, a), min(end_t, b)
        if cb - ca >= 0.08:
            sub_segs.append([ca, cb])
    if not sub_segs:
        sub_segs = [[start_t, end_t]]

    cum = [0.0]
    for a, b in sub_segs:
        cum.append(cum[-1] + (b - a))
    total = cum[-1]
    if total <= 0.01:
        step = (end_t - start_t) / max(1, len(chunks))
        return [start_t + i * step for i in range(len(chunks))] + [end_t]

    w = [max(0.25, weight(c)) for c in chunks]
    acc = [0.0]
    for x in w:
        acc.append(acc[-1] + x)
    v = [total * x / acc[-1] for x in acc]
    raw = list(v)
    for i in range(1, len(v) - 1):
        k = bisect.bisect_left(cum, v[i])
        lo, hi = max(1, k - 2), min(len(cum) - 1, k + 3)
        cand = [cum[j] for j in range(lo, hi) if cum[j] > v[i - 1] + 0.25]
        if not cand:
            continue
        best = min(cand, key=lambda c: abs(c - v[i]))
        if abs(best - v[i]) <= snap and best < raw[i + 1] - 0.15:
            v[i] = best
    for i in range(1, len(v)):
        if v[i] <= v[i - 1] + 0.20:
            v[i] = v[i - 1] + 0.20
    v[-1] = total

    def f(u):
        k = bisect.bisect_right(cum, u) - 1
        k = max(0, min(k, len(sub_segs) - 1))
        return sub_segs[k][0] + (u - cum[k])

    real = [f(u) for u in v]
    real[0], real[-1] = start_t, end_t
    eff_min = min(min_cue, (end_t - start_t) / max(1, len(chunks)) * 0.85)
    for i in range(1, len(real) - 1):
        left, right = real[i] - real[i - 1], real[i + 1] - real[i]
        if left < eff_min and right > 2 * eff_min:
            real[i] = real[i - 1] + eff_min
        elif right < eff_min and left > 2 * eff_min:
            real[i] = real[i + 1] - eff_min
    return real


def build_timeline(chunks, segs, dur, snap=0.90, min_cue=1.0):
    """字数比例铺到纯语音时间轴 -> 边界吸附到朗读停顿 -> 映射回真实时间。"""
    return _align_segment_range(chunks, segs, 0.0, dur, snap=snap, min_cue=min_cue)


def resolve_asr(args, lang):
    """按 --align 解析本地离线 whisper-cli 与模型路径；auto 找不到时静默回退 None。"""
    mode = getattr(args, "align", "auto")
    if mode == "silence":
        return None, None
    asr_bin = (getattr(args, "asr_bin", None)
               or os.environ.get("WHISPER_BIN")
               or shutil.which("whisper-cli"))
    asr_model = getattr(args, "asr_model", None) or os.environ.get("WHISPER_MODEL")
    if not asr_model:
        cache_dirs = [
            os.path.expanduser("~/.cache/whisper-cpp"),
            os.path.expanduser("~/.cache/whisper"),
            "/opt/homebrew/share/whisper-cpp",
        ]
        names = (["ggml-small.en.bin", "ggml-base.en.bin", "ggml-tiny.en.bin",
                  "ggml-medium.en.bin", "ggml-base.bin", "ggml-small.bin"]
                 if lang == "en" else
                 ["ggml-small.bin", "ggml-base.bin", "ggml-medium.bin", "ggml-tiny.bin"])
        for d in cache_dirs:
            for nm in names:
                cand = os.path.join(d, nm)
                if os.path.isfile(cand):
                    asr_model = cand
                    break
            if asr_model:
                break
    if asr_bin and asr_model and os.path.isfile(asr_model):
        return asr_bin, asr_model
    if mode == "asr":
        raise RuntimeError("已指定 --align asr，但未找到 whisper-cli 或模型文件（可用 --asr-bin / --asr-model 指定）")
    return None, None


def _parse_whisper_ts(s):
    h, m, sec = s.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def _norm_asr_tokens(text, lang):
    if lang == "en":
        return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", text.lower())
    return [c for c in text if "\u4e00" <= c <= "\u9fff" or c.isalnum()]


def build_timeline_asr(chunks, audio, dur, segs, asr_bin, asr_model, work, lang):
    """使用全本地离线 whisper-cli 词级时间戳 + 序列匹配 + 静音边界微调生成精准时间轴。"""
    import difflib
    wav16 = wav_path(work, "a3-asr16k")
    run([FF, "-hide_banner", "-loglevel", "error", "-y", "-i", audio,
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav16])
    asr_prefix = os.path.join(work, "asr")
    run([asr_bin, "-m", asr_model, "-l", lang, "-f", wav16, "-ojf", "-of", asr_prefix, "-np"])
    with open(asr_prefix + ".json", encoding="utf-8") as f:
        wj = json.load(f)

    asr_words = []
    for seg in wj.get("transcription", []):
        if lang == "en":
            cur_w, cur_s, cur_e = "", None, None
            for tok in seg.get("tokens", []):
                txt = tok["text"]
                if txt.startswith("["):
                    continue
                ts_f = _parse_whisper_ts(tok["timestamps"]["from"])
                ts_t = _parse_whisper_ts(tok["timestamps"]["to"])
                if txt.startswith(" "):
                    if cur_w:
                        ws = _norm_asr_tokens(cur_w, "en")
                        dt = max(0.01, (cur_e - cur_s) / max(1, len(ws)))
                        for k, w in enumerate(ws):
                            asr_words.append((w, cur_s + k * dt, cur_s + (k + 1) * dt))
                    cur_w = txt.strip()
                    cur_s, cur_e = ts_f, ts_t
                else:
                    cur_w += txt
                    if cur_s is None:
                        cur_s = ts_f
                    cur_e = ts_t
            if cur_w:
                ws = _norm_asr_tokens(cur_w, "en")
                dt = max(0.01, (cur_e - cur_s) / max(1, len(ws)))
                for k, w in enumerate(ws):
                    asr_words.append((w, cur_s + k * dt, cur_s + (k + 1) * dt))
        else:
            for tok in seg.get("tokens", []):
                txt = tok["text"]
                if txt.startswith("["):
                    continue
                ts_f = _parse_whisper_ts(tok["timestamps"]["from"])
                ts_t = _parse_whisper_ts(tok["timestamps"]["to"])
                cs = _norm_asr_tokens(txt, "zh")
                dt = max(0.01, (ts_t - ts_f) / max(1, len(cs)))
                for k, c in enumerate(cs):
                    asr_words.append((c, ts_f + k * dt, ts_f + (k + 1) * dt))

    if len(asr_words) < 3:
        raise RuntimeError("离线 ASR 未识别出有效词序列")

    our_words = []
    cue_ranges = []
    for c in chunks:
        ws = _norm_asr_tokens(c, lang)
        s_idx = len(our_words)
        our_words.extend(ws)
        cue_ranges.append((s_idx, len(our_words)))

    sm = difflib.SequenceMatcher(None, our_words, [w for w, _, _ in asr_words], autojunk=False)
    word_map = {}
    for a0, b0, size in sm.get_matching_blocks():
        for k in range(size):
            word_map[a0 + k] = b0 + k

    # 为未直接匹配到的词按前后已匹配锚点做线性插值，得到每个词的估算起止时间
    n_w = len(our_words)
    matched_idx = sorted(word_map)
    if not matched_idx:
        raise RuntimeError("ASR 词序列与原文无法对齐")
    word_times = [None] * n_w
    for wi in matched_idx:
        _, ts_s, ts_e = asr_words[word_map[wi]]
        word_times[wi] = (ts_s, ts_e)

    # 填补前后与中间缺词（如专有名词拼写差异）
    first_m = matched_idx[0]
    for wi in range(first_m):
        est = max(0.0, word_times[first_m][0] - (first_m - wi) * 0.28)
        word_times[wi] = (est, est + 0.25)
    last_m = matched_idx[-1]
    for wi in range(last_m + 1, n_w):
        est = min(dur, word_times[last_m][1] + (wi - last_m) * 0.28)
        word_times[wi] = (max(0.0, est - 0.25), est)
    for pos in range(len(matched_idx) - 1):
        a, b = matched_idx[pos], matched_idx[pos + 1]
        if b - a <= 1:
            continue
        t0, t1 = word_times[a][1], word_times[b][0]
        step = max(0.02, (t1 - t0) / (b - a))
        for wi in range(a + 1, b):
            s_est = t0 + (wi - a - 1) * step
            word_times[wi] = (s_est, s_est + step)

    # 汇总每句的起止区间，并用真实静音停顿间隙做边界微贴合
    gaps = [(segs[k][1], segs[k + 1][0]) for k in range(len(segs) - 1)]
    cue_starts = []
    cue_ends = []
    for ws_i, we_i in cue_ranges:
        if we_i > ws_i:
            cue_starts.append(word_times[ws_i][0])
            cue_ends.append(word_times[we_i - 1][1])
        else:
            prev_e = cue_ends[-1] if cue_ends else 0.0
            cue_starts.append(prev_e)
            cue_ends.append(prev_e + 0.5)

    bounds = [0.0 if cue_starts[0] < 0.80 else max(0.0, cue_starts[0] - 0.08)]
    for i in range(1, len(chunks)):
        prev_end = cue_ends[i - 1]
        next_start = cue_starts[i]
        raw_mid = (prev_end + next_start) / 2.0
        chosen = max(bounds[-1] + 0.35, min(next_start - 0.02, raw_mid)) if next_start > prev_end else max(bounds[-1] + 0.35, next_start - 0.02)
        # 在 [prev_end - 0.18, next_start + 0.18] 内找真实静音段中点贴合
        cand_gaps = [g for g in gaps if g[1] >= prev_end - 0.18 and g[0] <= next_start + 0.18]
        if cand_gaps:
            best_gap = min(cand_gaps, key=lambda g: abs((g[0] + g[1]) / 2.0 - raw_mid))
            g_mid = (best_gap[0] + best_gap[1]) / 2.0
            # 确保不越过下一句第一个词起音之后，且大于上一边界
            if bounds[-1] + 0.35 <= g_mid <= next_start + 0.04:
                chosen = g_mid
        bounds.append(round(chosen, 3))
    bounds.append(round(dur, 3))

    # 强制严格单调递增
    for i in range(1, len(bounds)):
        if bounds[i] <= bounds[i - 1] + 0.25:
            bounds[i] = round(bounds[i - 1] + 0.25, 3)
    bounds[-1] = round(max(dur, bounds[-2] + 0.25), 3)
    return bounds, len(matched_idx), len(our_words)


def ts(t):
    t = max(0.0, t)
    h, t = int(t // 3600), t % 3600
    m, t = int(t // 60), t % 60
    return "%d:%02d:%05.2f" % (h, m, t)


def wrap(s, limit, lang="zh"):
    if lang == "en":
        if len(s) <= limit:
            return s
        words = s.split()
        # 按预计行数均匀折行，避免最后一行只剩一个孤立词
        n_lines = max(2, math.ceil(len(s) / limit))
        target = math.ceil(len(s) / n_lines)
        out, cur = [], ""
        for idx, w in enumerate(words):
            cand = (cur + " " + w) if cur else w
            rem_lines = n_lines - len(out)
            if cur and (len(cand) > limit or (rem_lines > 1 and len(cand) > target + 3 and len(cur) >= target * 0.55)):
                out.append(cur)
                cur = w
            else:
                cur = cand
        if cur:
            out.append(cur)
        return r"\N".join(out)

    out = []
    zh_punct = "，。？！：；、…”\"』」）)"
    while len(s) > limit:
        # 末字是标点且仅超出 1~2 字符时，直接归入当前行，避免孤立标点落到下一行
        if len(s) <= limit + 2 and s[-1] in zh_punct:
            break
        cut = limit
        for p in range(limit, max(limit - 6, 0), -1):
            if p < len(s) and s[p - 1] in "，。？：；、—…":
                cut = p
                break
        # 避头规则：不能让下一行以标点开头，也不能从双破折号中间劈开
        while cut < len(s) and (s[cut] in zh_punct or (cut > 0 and s[cut - 1:cut + 1] == "——")):
            cut += 1
        if cut >= len(s):
            break
        out.append(s[:cut])
        s = s[cut:]
    out.append(s)
    return r"\N".join(out)


def ass_color(rgb):
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    return "&H00%s%s%s" % (b.upper(), g.upper(), r.upper())


def _lang_defaults(lang, is_landscape, bilingual=False):
    font = "Georgia" if lang == "en" else "Noto Sans CJK SC"
    if bilingual:
        base_chars = 34 if lang == "en" else (28 if is_landscape else 19)
        fs_main = 46 if not is_landscape else 44
        fs_sec = 38 if not is_landscape else 36
    else:
        base_chars = 34 if lang == "en" else (24 if is_landscape else 15)
        fs_main = (50 if lang == "en" else 58)
        fs_sec = 38
    return font, base_chars, fs_main, fs_sec


def _choose_header_text(args, sub_mode, main_lang):
    h1 = args.header_title.strip() if args.header_title else ""
    h2 = args.header_title2.strip() if args.header_title2 else ""
    if not h1 and not h2:
        return "", main_lang
    if h1 and h2:
        l1, l2 = detect_lang(h1), detect_lang(h2)
        if sub_mode == "bilingual":
            return "%s\n%s" % (h1, h2), l1
        for txt, lg in ((h1, l1), (h2, l2)):
            if lg == sub_mode:
                return txt, lg
        return h1, l1
    only = h1 or h2
    return only, detect_lang(only)


def write_ass(path, chunks, bounds, args, lang, fit_mode, total_dur,
              sec_chunks=None, sec_bounds=None, sec_lang=None, sub_mode=None):
    # 按短边缩放，保证竖屏(1080x1920)与横屏(1920x1080)字号比例协调一致
    short_scale = min(args.width, args.height) / 1080.0
    width_scale = args.width / 1080.0
    is_landscape = args.width > args.height
    bilingual = bool(sec_chunks and sec_bounds)
    sub_mode = sub_mode or ("bilingual" if bilingual else lang)

    def_font, def_chars, def_fs, _ = _lang_defaults(lang, is_landscape, bilingual=bilingual)
    font = args.font or def_font
    base_chars = args.line_chars or def_chars
    fs = args.font_size or round(def_fs * short_scale)
    ts_ = args.title_size or round((62 if lang == "en" else 70) * short_scale)

    sec_l = sec_lang or ("zh" if lang == "en" else "en")
    def_font2, def_chars2, _, def_fs2 = _lang_defaults(sec_l, is_landscape, bilingual=True)
    font2 = args.font2 or def_font2
    fs2 = args.font_size2 or round(def_fs2 * short_scale)

    header_text, header_lang = _choose_header_text(args, sub_mode, lang)
    header_font = args.font or ("Georgia" if header_lang == "en" else "Noto Sans CJK SC")
    header_fs = round((34 if is_landscape else 52 if header_lang == "en" else 58) * short_scale)

    if is_landscape:
        mv = round((158 if bilingual else 84) * args.height / 1080)
        mv2 = round(50 * args.height / 1080)
        ml = round(140 * width_scale)
        header_align = 7
        header_ml = round(54 * width_scale)
        header_mv = round(36 * args.height / 1080)
        header_color = "&H45D5E6EF"
    else:
        mv = round((395 if bilingual else (340 if fit_mode == "blur-frame" else 400)) * args.height / 1920)
        mv2 = round(235 * args.height / 1920)
        ml = round(84 * width_scale)
        header_align = 8
        header_ml = ml
        header_mv = round((245 if "\n" in header_text else 270) * args.height / 1920)
        header_color = "&H00D5E6EF"

    limit_scale = (width_scale * 0.86) if is_landscape else width_scale
    limit = max(6, round(base_chars * limit_scale))
    limit2 = max(6, round(def_chars2 * limit_scale))

    spacing = 0 if lang == "en" else 1
    spacing2 = 0 if sec_l == "en" else 1
    title_spacing = 1 if header_lang == "en" else 3
    fade_d, fade_t = args.fade, max(args.fade, 300)

    head = """[Script Info]
ScriptType: v4.00+
WrapStyle: 0
PlayResX: %d
PlayResY: %d
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,%s,%d,%s,&H00FFFFFF,%s,&HA0000000,0,0,0,0,100,100,%d,0,1,2.4,1.5,2,%d,%d,%d,1
Style: Title,%s,%d,%s,&H00FFFFFF,%s,&HA0000000,0,0,0,0,100,100,%d,0,1,2.6,2,2,%d,%d,%d,1
Style: Secondary,%s,%d,&H00C9DEE6,&H00FFFFFF,%s,&H90000000,0,0,0,0,100,100,%d,0,1,2.0,1.2,2,%d,%d,%d,1
Style: Header,%s,%d,%s,&H00FFFFFF,%s,&H80000000,0,0,0,0,100,100,%d,0,1,1.3,1.0,%d,%d,%d,%d,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" % (args.width, args.height,
       font, fs, ass_color(args.text_color), ass_color(args.outline_color), spacing, ml, ml, mv,
       font, ts_, ass_color(args.text_color), ass_color(args.outline_color), title_spacing, ml, ml, mv,
       font2, fs2, ass_color(args.outline_color), spacing2, ml, ml, mv2,
       header_font, header_fs, header_color, ass_color(args.outline_color), title_spacing,
       header_align, header_ml, header_ml, header_mv)

    lines = [head]
    if header_text:
        ht_lines = [wrap(sanitize(part.strip(), args.keep_emoji), max(12, limit), detect_lang(part))
                    for part in header_text.splitlines() if part.strip()]
        lines.append("Dialogue: 1,%s,%s,Header,,0,0,0,,{\\fad(450,450)}%s\n" % (
            ts(0.0), ts(total_dur), r"\N".join(ht_lines)))

    n = len(chunks)
    for i, c in enumerate(chunks):
        title = i < args.title_first or i >= n - args.title_last
        fade = fade_t if title else fade_d
        end = bounds[i + 1] + (args.tail if i == n - 1 else 0)
        lines.append("Dialogue: 0,%s,%s,%s,,0,0,0,,{\\fad(%d,%d)}%s\n" % (
            ts(bounds[i]), ts(end), "Title" if title else "Default",
            fade, fade, wrap(c, limit, lang)))

    if bilingual:
        n2 = len(sec_chunks)
        for i, c in enumerate(sec_chunks):
            end = sec_bounds[i + 1] + (args.tail if i == n2 - 1 else 0)
            lines.append("Dialogue: 0,%s,%s,Secondary,,0,0,0,,{\\fad(%d,%d)}%s\n" % (
                ts(sec_bounds[i]), ts(end), fade_d, fade_d, wrap(c, limit2, sec_l)))

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def srt_time(x):
    h, r = int(x // 3600), x % 3600
    m, r = int(r // 60), r % 60
    return "%02d:%02d:%02d,%03d" % (h, m, int(r), round((r - int(r)) * 1000))


def write_srt(path, chunks, bounds, tail=0.0):
    out = []
    n = len(chunks)
    for i, c in enumerate(chunks):
        end = bounds[i + 1] + (tail if i == n - 1 else 0)
        out.append("%d\n%s --> %s\n%s\n\n" % (
            i + 1, srt_time(bounds[i]), srt_time(end), c))
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out)


# ---------------------------------------------------------------- PNG 生成工具（背景与遮罩）


def _write_png(path, W, H, channels, buf):
    color_type = 6 if channels == 4 else 2

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    stride = W * channels
    raw = b"".join(b"\x00" + bytes(buf[y * stride:(y + 1) * stride]) for y in range(H))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, color_type, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def make_dreamy_overlay(path, W, H, fps=30, loop_sec=12.0, seed=20261105, intensity=1.0,
                        frame_box=None):
    """生成无缝循环的梦幻动态微光叠加层（呼吸月晕 + 飘浮闪烁星尘），RGBA 透明通道输出为无损 MOV。

    采用 1/3 分辨率 + 4x4 亚像素稀疏高斯光斑模板 + 与主视频同帧率(30fps)长周期(12s)运动，
    消除整数像素舍入跳变与帧率抽帧顿挫，保证丝滑缓和的睡前动态质感。
    """
    ow = max(180, round(W / 3) // 2 * 2)
    oh = max(180, round(H / 3) // 2 * 2)
    n_frames = max(30, int(round(loop_sec * fps)))
    rng = random.Random(seed + 77)
    sub = 4

    def build_subpixel_stamps(radius, power=2.0):
        """预生成 sub x sub 个亚像素偏移下的稀疏高斯像素列表 [(dx, dy, weight)]。"""
        r = max(2, int(math.ceil(radius))) + 1
        table = []
        for sy in range(sub):
            oy = sy / sub
            row_list = []
            for sx in range(sub):
                ox = sx / sub
                pts = []
                for dy in range(-r, r + 1):
                    fy = dy - oy
                    for dx in range(-r, r + 1):
                        fx = dx - ox
                        d = math.hypot(fx, fy) / radius
                        if d >= 1.0:
                            continue
                        g = math.exp(-(d * d) * 3.6) * ((1.0 - d * d) ** power)
                        if g > 0.005:
                            pts.append((dx, dy, g))
                row_list.append(pts)
            table.append(row_list)
        return table

    # 1. 柔和呼吸光晕（极缓慢微动，主靠明暗呼吸营造月光/星云氛围）
    halo_stamps = [build_subpixel_stamps(r, 1.8) for r in (ow * 0.22, ow * 0.16)]
    halos = []
    halo_colors = [(255, 224, 165), (170, 205, 255), (225, 195, 255), (255, 232, 185)]
    for i in range(4):
        st_idx = i % len(halo_stamps)
        cx = rng.uniform(0.18, 0.82) * ow
        cy = rng.uniform(0.15, 0.82) * oh
        if frame_box and i < 2:
            fx, fy, fw, fh = frame_box
            cx = (fx + fw * (0.28 if i == 0 else 0.72)) * ow / W
            cy = (fy if i == 0 else fy + fh) * oh / H
        halos.append(dict(
            stamp=halo_stamps[st_idx],
            cx=cx, cy=cy,
            amp_x=rng.uniform(1.5, 3.5),
            amp_y=rng.uniform(1.5, 3.0),
            phase=rng.uniform(0, 2 * math.pi),
            max_a=rng.uniform(32, 48) * intensity,
            color=halo_colors[i % len(halo_colors)],
        ))

    # 2. 缓和飘浮的梦幻星尘 / 萤火微光
    dust_stamps = [build_subpixel_stamps(r, 1.6) for r in (2.8, 4.2, 6.2, 8.8)]
    dust_colors = [(255, 236, 182), (255, 218, 150), (195, 225, 255), (242, 215, 255)]
    particles = []
    n_particles = 32 if H >= W else 36
    for i in range(n_particles):
        st = dust_stamps[i % len(dust_stamps)]
        particles.append(dict(
            stamp=st,
            bx=rng.uniform(0.05, 0.95) * ow,
            by=rng.uniform(0.06, 0.92) * oh,
            ax=rng.uniform(2.5, 7.5),
            ay=rng.uniform(3.0, 9.0),
            px=rng.uniform(0, 2 * math.pi),
            py=rng.uniform(0, 2 * math.pi),
            pa=rng.uniform(0, 2 * math.pi),
            base_a=rng.uniform(85, 180) * intensity,
            color=dust_colors[i % len(dust_colors)],
        ))

    def stamp_add(buf, x0, y0, table, alpha_scale, color):
        ix = int(math.floor(x0))
        iy = int(math.floor(y0))
        sx = min(sub - 1, int((x0 - ix) * sub))
        sy = min(sub - 1, int((y0 - iy) * sub))
        pts = table[sy][sx]
        cr, cg, cb = color
        for dx, dy, g in pts:
            a = int(g * alpha_scale)
            if a <= 1:
                continue
            x = ix + dx
            y = iy + dy
            if not (0 <= x < ow and 0 <= y < oh):
                continue
            p = (y * ow + x) * 4
            old_a = buf[p + 3]
            new_a = old_a + a
            if new_a > 235:
                new_a = 235
            if old_a == 0:
                buf[p] = cr
                buf[p + 1] = cg
                buf[p + 2] = cb
                buf[p + 3] = new_a
            else:
                tot = old_a + a
                buf[p] = min(255, (buf[p] * old_a + cr * a) // tot)
                buf[p + 1] = min(255, (buf[p + 1] * old_a + cg * a) // tot)
                buf[p + 2] = min(255, (buf[p + 2] * old_a + cb * a) // tot)
                buf[p + 3] = new_a

    cmd = [
        FF, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba",
        "-s", "%dx%d" % (ow, oh), "-r", str(fps), "-i", "-",
        "-c:v", "png", "-pix_fmt", "rgba", path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    blank = bytes(ow * oh * 4)
    try:
        for f in range(n_frames):
            t = 2.0 * math.pi * f / n_frames
            buf = bytearray(blank)
            for h in halos:
                hx = h["cx"] + h["amp_x"] * math.sin(t + h["phase"])
                hy = h["cy"] + h["amp_y"] * math.cos(t + h["phase"])
                pulse = 0.58 + 0.42 * math.sin(t + h["phase"])
                stamp_add(buf, hx, hy, h["stamp"], h["max_a"] * pulse, h["color"])
            for pt in particles:
                px = pt["bx"] + pt["ax"] * math.sin(t + pt["px"])
                py = pt["by"] + pt["ay"] * math.cos(t + pt["py"])
                tw = 0.28 + 0.72 * (0.5 + 0.5 * math.sin(t + pt["pa"])) ** 1.4
                stamp_add(buf, px, py, pt["stamp"], pt["base_a"] * tw, pt["color"])
            proc.stdin.write(buf)
        proc.stdin.close()
        stderr = proc.stderr.read().decode("utf-8", errors="replace")
        ret = proc.wait()
        if ret != 0:
            raise RuntimeError("生成梦幻动态层失败 (%d): %s" % (ret, stderr[-1000:]))
    finally:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        proc.wait()


def make_scrim(path, W, H, start_ratio=0.52, max_alpha=0.72, top_fade=False):
    """生成透明渐变暗遮罩 PNG，保障插画背景上的字幕与抬头可读性。"""
    buf = bytearray(W * H * 4)
    y_start = int(H * start_ratio)
    y_top_end = int(H * 0.16) if top_fade else 0
    for y in range(H):
        alpha = 0
        if y >= y_start:
            k = (y - y_start) / max(1, H - y_start)
            alpha = int(255 * max_alpha * (k ** 1.3))
        elif top_fade and y < y_top_end:
            k = 1.0 - (y / max(1, y_top_end))
            alpha = int(255 * 0.45 * (k ** 1.4))
        if alpha <= 0:
            continue
        row = bytes([6, 8, 18, alpha]) * W
        p = y * W * 4
        buf[p:p + W * 4] = row
    _write_png(path, W, H, 4, buf)


def add_glow(buf, W, H, cx, cy, radius, bright, color):
    sigma = max(radius, 0.6)
    r0 = int(sigma * 2.6) + 2
    for y in range(max(0, cy - r0), min(H, cy + r0 + 1)):
        for x in range(max(0, cx - r0), min(W, cx + r0 + 1)):
            d = math.hypot(x - cx, y - cy)
            if d > r0:
                continue
            k = bright * math.exp(-(d * d) / (2.0 * sigma * sigma))
            k *= max(0.0, 1.0 - (d / r0) ** 2) ** 2
            if k < 1:
                continue
            p = (y * W + x) * 3
            for ch in range(3):
                val = buf[p + ch] + k * color[ch]
                buf[p + ch] = 255 if val > 255 else int(val)


def make_background(path, args):
    W, H, theme = args.width, args.height, THEMES[args.theme]
    random.seed(args.seed)
    buf = bytearray(W * H * 3)
    cx0, cy0 = W / 2, H * 0.375
    dx2 = [((x - cx0) / (W * 0.91)) ** 2 for x in range(W)]
    for y in range(H):
        t = y / (H - 1)
        if t < 0.55:
            u = t / 0.55
            base = tuple(theme["top"][i] + (theme["mid"][i] - theme["top"][i]) * u for i in range(3))
        else:
            u = (t - 0.55) / 0.45
            base = tuple(theme["mid"][i] + (theme["bottom"][i] - theme["mid"][i]) * u for i in range(3))
        dy2 = ((y - cy0) / (H * 0.62)) ** 2
        p = y * W * 3
        for x in range(W):
            d2 = dx2[x] + dy2
            glow = (1.0 - d2) ** 2 if d2 < 1.0 else 0.0
            vig = 1.0 - 0.34 * min(1.0, d2 * 0.85)
            q = p + x * 3
            for ch in range(3):
                buf[q + ch] = min(255, int(base[ch] * vig + glow * theme["glow"][ch]))

    if theme["star"]:
        sx, sy = int(W * 0.73), int(H * 0.20)
        add_glow(buf, W, H, sx, sy, W * 0.14, 26, (1.0, 0.32, 0.30))
        add_glow(buf, W, H, sx, sy, W * 0.024, 90, (1.0, 0.45, 0.40))
        add_glow(buf, W, H, sx, sy, 4, 190, (1.0, 0.72, 0.62))

    count = max(40, int(260 * W * H / (1080 * 1920)))
    for _ in range(count):
        x, y = random.randint(20, W - 20), int(random.triangular(40, H * 0.78, H * 0.20))
        r = random.choice([0.6, 0.7, 0.9, 1.2, 1.6]) * max(0.7, W / 1080)
        tint = random.choice([(1.0, 0.98, 0.92), (0.82, 0.90, 1.0), (1.0, 0.88, 0.78)])
        add_glow(buf, W, H, x, y, r, random.uniform(70, 210), tint)

    if theme["sea"]:
        y0 = int(H * 0.77)
        for y in range(y0, H):
            k = (y - y0) / (H - y0)
            p = y * W * 3
            for x in range(W):
                q = p + x * 3
                sh = 0.55 + 0.45 * math.sin(x / (W * 0.088) + y / 33.0)
                buf[q + 1] = min(255, int(buf[q + 1] + 12 * k * sh))
                buf[q + 2] = min(255, int(buf[q + 2] + 24 * k * sh))
        for _ in range(46):
            add_glow(buf, W, H, random.randint(30, W - 30), random.randint(y0 + 20, H - 40),
                     random.uniform(5, 13), random.uniform(30, 95), (0.35, 0.95, 1.0))

    _write_png(path, W, H, 3, buf)


# ---------------------------------------------------------------- 合成与自检


def resolve_bg_fit(args, bg, bg_is_video):
    if args.bg_fit != "auto":
        return args.bg_fit
    if bg_is_video or not args.background:
        return "cover"
    try:
        bw, bh = probe_dimensions(bg)
        if args.height > args.width and (bw / max(1, bh)) > 1.15:
            return "blur-frame"
    except Exception:
        pass
    return "cover"


def prepare_still_bg(bg, fit_mode, args, work):
    """预先单帧合成静态底图（虚化背景 + 主画框 + 暗遮罩），避免编码时逐帧重算模糊与叠加。
    返回 (composed_bg_path, frame_box_or_None)。"""
    W, H = args.width, args.height
    use_scrim = bool(args.background) and not args.no_scrim
    scrim_path = os.path.join(work, "scrim.png")
    composed = os.path.join(work, "composed-bg.png")

    if fit_mode == "blur-frame":
        make_scrim(scrim_path, W, H, start_ratio=0.50, max_alpha=0.58,
                   top_fade=bool(args.header_title))
        fg_w = round(W * 0.92)
        fg_h_max = round(H * 0.42)
        fg_y = round(H * 0.235)
        fc = (
            "[0:v]split=2[b][f];"
            "[b]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
            "gblur=sigma=36,eq=brightness=-0.24:saturation=0.82[bg];"
            "[f]scale=%d:%d:force_original_aspect_ratio=decrease,"
            "pad=iw+4:ih+4:2:2:color=0x505468[fg];"
            "[bg][fg]overlay=(W-w)/2:%d[comp];"
            "[comp][1:v]overlay=0:0[vout]"
            % (W, H, W, H, fg_w, fg_h_max, fg_y)
        )
        run([FF, "-hide_banner", "-loglevel", "error", "-y",
             "-i", bg, "-i", scrim_path,
             "-filter_complex", fc, "-map", "[vout]", "-frames:v", "1", composed])
        return composed, ((W - fg_w) // 2, fg_y, fg_w, fg_h_max)

    # cover 模式（静态高清）
    base = "scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d" % (W, H, W, H)
    if use_scrim:
        make_scrim(scrim_path, W, H, start_ratio=0.55 if W > H else 0.50,
                   max_alpha=0.70, top_fade=bool(args.header_title and W > H))
        fc = "[0:v]%s[bg];[bg][1:v]overlay=0:0[vout]" % base
        run([FF, "-hide_banner", "-loglevel", "error", "-y",
             "-i", bg, "-i", scrim_path,
             "-filter_complex", fc, "-map", "[vout]", "-frames:v", "1", composed])
    else:
        run([FF, "-hide_banner", "-loglevel", "error", "-y",
             "-i", bg, "-vf", base, "-frames:v", "1", composed])
    return composed, None


def compose(out, audio, bg, bg_is_video, fit_mode, duration, args, work):
    total = duration + args.tail
    W, H = args.width, args.height
    use_scrim = bool(args.background) and not args.no_scrim
    use_dreamy = not args.no_dreamy and not bg_is_video and args.glow_intensity > 0
    scrim_path = os.path.join(work, "scrim.png")
    extra_v = []

    if bg_is_video:
        pre = ["-stream_loop", "-1", "-i", bg]
        vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,ass=subs.ass"
              % (W, H, W, H))
        cmd = ([FF, "-hide_banner", "-loglevel", "error", "-y"] + pre
               + ["-i", audio, "-vf", vf])
    elif fit_mode == "cover" and args.zoom > 0:
        base = (
            "scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
            "zoompan=z='min(zoom+%.6f,1.08)':x='iw/2-(iw/zoom/2)'"
            ":y='ih/2-(ih/zoom/2)':d=1:s=%dx%d:fps=%d"
            % (W * 2, H * 2, W * 2, H * 2, args.zoom, W, H, args.fps)
        )
        pre = ["-loop", "1", "-framerate", str(args.fps), "-i", bg]
        if use_scrim:
            make_scrim(scrim_path, W, H, start_ratio=0.55 if W > H else 0.50,
                       max_alpha=0.70, top_fade=bool(args.header_title and W > H))
            fc = "[0:v]%s[bg];[bg][2:v]overlay=0:0,ass=subs.ass[vout]" % base
            cmd = ([FF, "-hide_banner", "-loglevel", "error", "-y"] + pre
                   + ["-i", audio,
                      "-loop", "1", "-framerate", str(args.fps), "-i", scrim_path,
                      "-filter_complex", fc, "-map", "[vout]", "-map", "1:a"])
        else:
            cmd = ([FF, "-hide_banner", "-loglevel", "error", "-y"] + pre
                   + ["-i", audio, "-vf", base + ",ass=subs.ass"])
    else:
        if args.background:
            still, frame_box = prepare_still_bg(bg, fit_mode, args, work)
        else:
            still, frame_box = bg, None

        if use_dreamy:
            ambient_mov = os.path.join(work, "ambient-glow.mov")
            make_dreamy_overlay(ambient_mov, W, H, fps=args.fps,
                                loop_sec=12.0, seed=args.seed,
                                intensity=args.glow_intensity, frame_box=frame_box)
            fc = (
                "[2:v]scale=%d:%d:flags=bicubic,format=yuva420p[glow];"
                "[0:v][glow]overlay=0:0:shortest=1,ass=subs.ass[vout]" % (W, H)
            )
            cmd = ([FF, "-hide_banner", "-loglevel", "error", "-y",
                    "-loop", "1", "-framerate", str(args.fps), "-i", still,
                    "-i", audio,
                    "-stream_loop", "-1", "-i", ambient_mov,
                    "-filter_complex", fc, "-map", "[vout]", "-map", "1:a"])
        else:
            cmd = ([FF, "-hide_banner", "-loglevel", "error", "-y",
                    "-loop", "1", "-framerate", str(args.fps), "-i", still,
                    "-i", audio, "-vf", "ass=subs.ass"])
            extra_v = ["-tune", "stillimage"]

    cmd += (["-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf)]
            + extra_v + [
                "-pix_fmt", "yuv420p", "-r", str(args.fps),
                "-c:a", "aac", "-b:a", "160k", "-af", "apad=pad_dur=%.3f" % args.tail,
                "-t", "%.3f" % total, "-movflags", "+faststart", out,
            ])
    run(cmd, cwd=work)


def ass_cues(path):
    def sec(s):
        h, m, rest = s.split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)
    cues = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"Dialogue:\s*\d+,([^,]+),([^,]+),([^,]*),", line)
            if m and m.group(3).strip() != "Header":
                cues.append((sec(m.group(1)), sec(m.group(2))))
    return cues


def verify(out, work, args):
    try:
        info = run([FP, "-v", "error", "-show_entries",
                    "format=duration:stream=codec_name,width,height,r_frame_rate",
                    "-of", "default=noprint_wrappers=1", out]).stdout.strip()
    except Exception:
        dur, streams, dims = _ffmpeg_probe_fallback(out)
        info = "ffprobe 不可用，ffmpeg -i 兜底: duration=%s dims=%s streams=%s" % (
            dur, dims, ",".join(streams))
    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)
    cues = ass_cues(os.path.join(work, "subs.ass"))
    picks = sorted({0, len(cues) // 2, len(cues) - 1}) if cues else []
    made = []
    for i in picks:
        s, e = cues[i]
        t = (s + e) / 2
        dst = os.path.join(frames, "check-%02d.jpg" % (i + 1))
        run([FF, "-v", "error", "-ss", "%.3f" % t, "-i", out,
             "-frames:v", "1", "-q:v", "2", "-y", dst])
        made.append(dst)
    return info, made


# ---------------------------------------------------------------- 子命令


def cmd_analyze(args):
    dur = probe_duration(args.audio)
    segs, _ = silence_segments(args.audio, args.silence_db, args.silence_min)
    print("duration = %.3f s" % dur)
    print("voice_segments = %d" % len(segs))
    print("silence_db = %.0f dB / min_len = %.2f s" % (args.silence_db, args.silence_min))
    lo, hi = (float(x) for x in args.range.split(":"))
    rows = energy_table(args.audio, lo, hi - lo, args.window)
    print("\nenergy window (%.0f ms, %.2f~%.2f s):" % (args.window * 1000, lo, hi))
    for t, db in rows:
        bar = "#" * max(0, min(60, int((db + 5) / 1.5)))
        print("  %7.2fs  %7.1f dB  %s" % (t, db, bar))
    print("\n判读：高电平突发 -> 近 0 dB 静音 -> 稳定高电平 = 片头杂音，切点选静音区。")
    print("      标题后停顿：片头 2~4s 内找「末字收音低谷 -> 正文起音跃升」，用 --title-pause-at 指定。")
    return 0


def resolve_dimensions(args):
    if args.width is not None and args.height is not None:
        return
    if args.orientation == "landscape":
        if args.width is None:
            args.width = 1920
        if args.height is None:
            args.height = 1080
    else:
        if args.width is None:
            args.width = 1080
        if args.height is None:
            args.height = 1920


def _load_text_sources(args):
    """读取主文本与可选第二语种文本；支持双文件或 `|||` 单文件内联双语，并保留原始文本供句级配对。"""
    with open(args.text, "r", encoding="utf-8-sig") as f:
        raw1 = f.read()
    raw2 = None
    inline_pairs = None
    if args.text2:
        with open(args.text2, "r", encoding="utf-8-sig") as f:
            raw2 = f.read()
    else:
        lines = [ln for ln in raw1.splitlines() if ln.strip()]
        if lines and all("|||" in ln for ln in lines):
            inline_pairs = [(ln.split("|||", 1)[0].strip(), ln.split("|||", 1)[1].strip())
                            for ln in lines]
            raw1 = "\n\n".join(a for a, _ in inline_pairs)
            raw2 = "\n\n".join(b for _, b in inline_pairs)

    lang1 = detect_lang(raw1) if args.lang == "auto" else args.lang
    chunks1, pids1 = split_paragraphs(raw1, args.keep_emoji, lang=lang1)

    lang2, paired = None, None
    if raw2:
        lang2 = detect_lang(raw2)
        if lang2 == lang1:
            print("      注意：--text 与 --text2 检测为同一语种（%s），将忽略重复语种文本" % lang1)
        elif inline_pairs:
            paired = (
                [sanitize(a, args.keep_emoji) for a, _ in inline_pairs],
                [sanitize(b, args.keep_emoji) for _, b in inline_pairs],
            )
        else:
            paired = align_bilingual_texts(raw1, raw2, lang1, lang2, keep_emoji=args.keep_emoji)
    return (chunks1, lang1), (paired, lang2)


def cmd_build(args):
    resolve_dimensions(args)
    out = os.path.abspath(args.output)
    outdir = os.path.dirname(out)
    work = os.path.abspath(args.work_dir or os.path.join(outdir, ".story-video-work"))
    os.makedirs(work, exist_ok=True)

    audio, dur = prepare_audio(os.path.abspath(args.audio), work,
                               args.trim_start, args.trim_end)
    print("[1/5] audio: %.3fs  (%s)" % (dur, os.path.basename(audio)))

    ass = os.path.join(work, "subs.ass")
    chunks, lang = [], ("zh" if args.lang == "auto" else args.lang)
    paired_texts, sec_lang = None, None
    if not args.ass:
        if not args.text:
            raise ValueError("必须提供 --text（或使用 --ass 直接导入字幕）")
        (chunks, lang), (paired_texts, sec_lang) = _load_text_sources(args)

    sil_db = args.silence_db if args.silence_db is not None else (-38.0 if lang == "en" else -42.0)
    sil_min = args.silence_min if args.silence_min is not None else (0.22 if lang == "en" else 0.30)
    snap = args.snap if args.snap is not None else (1.35 if lang == "en" else 0.90)

    # 标题后停顿：默认 1.2s，且必须在对齐之前插入音频。
    pause = args.title_pause
    if pause is None:
        pause = 0.0 if args.ass else 1.2
    if args.ass and pause > 0:
        print("      注意：--ass 分支不改字幕，已忽略 --title-pause（音频与字幕必须同源）")
        pause = 0.0
    if pause > 0 and args.title_pause is None and not is_title_line(chunks[0], lang=lang):
        print("      注意：首句不像独立标题（含句读或过长），已跳过默认标题后停顿；"
              "确需插入请显式传 --title-pause + --title-pause-at")
        pause = 0.0
    if pause > 0:
        at, src = args.title_pause_at, "手动指定"
        if at is None:
            at = auto_title_pause_at(audio, chunks,
                                     noise_db=sil_db,
                                     min_len=sil_min)
            src = "自动估算"
        if not (0.10 < at < dur - 0.50):
            raise ValueError("标题停顿插入点 %.3fs 超出音频范围 (0.10~%.2fs)；"
                             "请用 --title-pause-at 显式指定" % (at, dur - 0.50))
        audio, dur = insert_title_pause(audio, work, at, pause)
        print("      标题后停顿：%.3fs 处插入 %.2fs 静音（%s），audio -> %.3fs"
              % (at, pause, src, dur))
        if src == "自动估算":
            print("      自动切点务必复核：analyze --audio <源> --range %.2f:%.2f "
                  "--window 0.02；不准就显式传 --title-pause-at"
                  % (max(0.0, at - 0.6), at + 0.8))

    if args.background:
        bg = os.path.abspath(args.background)
        bg_is_video = is_video(bg)
        fit_mode = resolve_bg_fit(args, bg, bg_is_video)
        print("[3/5] 自定义背景：%s (%s, fit=%s)" % (
            bg, "视频" if bg_is_video else "图片", fit_mode))
    else:
        bg = os.path.join(work, "bg.png")
        make_background(bg, args)
        bg_is_video = False
        fit_mode = "cover"
        print("[3/5] 生成背景：theme=%s  %dx%d" % (args.theme, args.width, args.height))

    if args.ass:
        shutil.copyfile(os.path.abspath(args.ass), ass)
        print("[2/5] 使用外部 ASS：跳过切句与对齐")
    else:
        segs, dur2 = silence_segments(audio, sil_db, sil_min)
        if len(segs) < 2:
            raise RuntimeError("未检测到朗读停顿，无法自动对齐；请检查音轨或调低 --silence-db")

        asr_bin, asr_model = resolve_asr(args, lang)

        def align_chunks(target_chunks):
            if asr_bin and asr_model:
                try:
                    b, m_cnt, t_cnt = build_timeline_asr(
                        target_chunks, audio, dur2, segs, asr_bin, asr_model, work, lang)
                    return b, ("asr(%d/%d)" % (m_cnt, t_cnt))
                except Exception as ex:
                    if args.align == "asr":
                        raise
                    print("      注意：离线 ASR 对齐失败（%s），自动回退静音对齐" % ex)
            return build_timeline(target_chunks, segs, dur2, snap, args.min_cue), "silence"

        # 解析目标字幕语言（仅单语种文本时自动退化为可用语种）
        req_sub = args.subtitle_lang
        has_two = bool(paired_texts and sec_lang)
        if not has_two:
            if req_sub not in ("auto", lang):
                print("      注意：仅提供单一语种文本（%s），已自动仅输出 %s 字幕" % (lang, lang))
            sub_mode = lang
            bounds, align_used = align_chunks(chunks)
            out_chunks, out_bounds, out_lang = chunks, bounds, lang
            out_sec_chunks, out_sec_bounds, out_sec_lang = None, None, None
        else:
            sub_mode = "bilingual" if req_sub == "auto" else req_sub
            if sub_mode == lang:
                bounds, align_used = align_chunks(chunks)
                out_chunks, out_bounds, out_lang = chunks, bounds, lang
                out_sec_chunks, out_sec_bounds, out_sec_lang = None, None, None
            else:
                # 双语模式或仅输出译文时，均用主语种句对对齐音频，并共享同一时间窗避免双语错位
                p1_chunks, p2_chunks = paired_texts
                bounds, align_used = align_chunks(p1_chunks)
                if sub_mode == "bilingual":
                    out_chunks, out_bounds, out_lang = p1_chunks, bounds, lang
                    out_sec_chunks, out_sec_bounds, out_sec_lang = p2_chunks, bounds, sec_lang
                elif sub_mode == sec_lang:
                    out_chunks, out_bounds, out_lang = p2_chunks, bounds, sec_lang
                    out_sec_chunks, out_sec_bounds, out_sec_lang = None, None, None
                else:
                    out_chunks, out_bounds, out_lang = p1_chunks, bounds, lang
                    out_sec_chunks, out_sec_bounds, out_sec_lang = None, None, None

        write_ass(ass, out_chunks, out_bounds, args, out_lang, fit_mode, dur2 + args.tail,
                  sec_chunks=out_sec_chunks, sec_bounds=out_sec_bounds,
                  sec_lang=out_sec_lang, sub_mode=sub_mode)
        report = [dict(index=i + 1, start=round(out_bounds[i], 3),
                       end=round(out_bounds[i + 1], 3), text=c)
                  for i, c in enumerate(out_chunks)]
        tl_payload = dict(subtitle_lang=sub_mode, lang=out_lang, align=align_used,
                          width=args.width, height=args.height, fit=fit_mode,
                          duration=dur2, tail=args.tail, cues=report)
        if out_sec_chunks:
            tl_payload["secondary_lang"] = out_sec_lang
            tl_payload["secondary_cues"] = [
                dict(index=i + 1, start=round(out_sec_bounds[i], 3),
                     end=round(out_sec_bounds[i + 1], 3), text=c)
                for i, c in enumerate(out_sec_chunks)
            ]
        with open(os.path.join(work, "timeline.json"), "w", encoding="utf-8") as f:
            json.dump(tl_payload, f, ensure_ascii=False, indent=2)
        sec_info = (" + %s %d 句" % (out_sec_lang, len(out_sec_chunks))) if out_sec_chunks else ""
        print("[2/5] subtitle=%s | align=%s | %s %d 句%s / %d 语音段" % (
            sub_mode, align_used, out_lang, len(out_chunks), sec_info, len(segs)))
        sub_out = os.path.abspath(args.subtitle_out or os.path.splitext(out)[0] + ".ass")
        shutil.copyfile(ass, sub_out)
        if args.srt:
            write_srt(os.path.splitext(sub_out)[0] + ".srt", out_chunks, out_bounds, args.tail)
        print("      ASS -> %s" % sub_out)

    if args.dry_run:
        print("[4/5] --dry-run：跳过编码。工作目录 %s" % work)
        return 0

    compose(out, audio, bg, bg_is_video, fit_mode, dur, args, work)
    print("[4/5] 合成完成：%s" % out)
    info, frames = verify(out, work, args)
    print("[5/5] 自检：\n" + info)
    print("      抽帧：" + ", ".join(os.path.basename(x) for x in frames))
    print("      工作目录：%s（timeline.json / bg.png / subs.ass / frames）" % work)
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="朗读音频 + 原文 -> 逐句同步字幕视频")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="分析音频停顿与开头能量，定位裁剪点")
    a.add_argument("--audio", required=True)
    a.add_argument("--range", default="0:12", help="能量分析区间，默认 0:12")
    a.add_argument("--window", type=float, default=0.05, help="能量窗秒数，默认 0.05")
    a.add_argument("--silence-db", type=float, default=-42.0)
    a.add_argument("--silence-min", type=float, default=0.30)
    a.set_defaults(func=cmd_analyze)

    b = sub.add_parser("build", help="生成背景、字幕并合成视频")
    b.add_argument("--audio", required=True, help="朗读音频；也可传视频，会自动抽音轨")
    b.add_argument("--text", help="主原文 .txt/.md（UTF-8，建议与朗读音频同语种）；使用 --ass 时可省略")
    b.add_argument("--text2", "--trans-text", dest="text2", default=None,
                   help="可选第二语种对照文本（按段落与 --text 对应，用于输出中文/英文/双语字幕）")
    b.add_argument("--output", "--out", dest="output", required=True, help="输出 mp4")
    b.add_argument("--ass", help="直接使用已有 ASS（跳过切句/对齐，用于改字幕后重导）")
    b.add_argument("--background", help="自定义背景图或视频；不传则按主题生成")
    b.add_argument("--bg-fit", choices=["auto", "cover", "blur-frame"], default="auto",
                   help="背景图适配方式：auto=竖屏配横图用 blur-frame，其余 cover；cover=等比裁剪铺满")
    b.add_argument("--no-scrim", action="store_true", help="关闭自定义背景上的底部渐变暗遮罩")
    b.add_argument("--no-dreamy", action="store_true", help="关闭背景上的呼吸月晕与飘浮星尘微光动态层")
    b.add_argument("--glow-intensity", type=float, default=1.0,
                   help="梦幻发光与星尘微光强度倍率（默认 1.0，可调 0.5~1.8，0 关闭）")
    b.add_argument("--theme", choices=sorted(THEMES), default="starry")
    b.add_argument("--orientation", choices=["portrait", "landscape"], default="portrait",
                   help="画面规格：portrait=1080x1920（默认），landscape=1920x1080")
    b.add_argument("--width", type=int, default=None)
    b.add_argument("--height", type=int, default=None)
    b.add_argument("--fps", type=int, default=30)
    b.add_argument("--seed", type=int, default=20261105)
    b.add_argument("--lang", choices=["auto", "zh", "en"], default="auto",
                   help="主原文（音频语种）：auto 自动识别，zh 中文，en 英文")
    b.add_argument("--subtitle-lang", "--sub-lang", dest="subtitle_lang",
                   choices=["auto", "en", "zh", "bilingual"], default="auto",
                   help="输出字幕语言：auto(双语文本时出双语，单语时出单语) / en / zh / bilingual；若仅有单一语种文本则自动回退该语种")
    b.add_argument("--header-title", default=None,
                   help="可选常驻故事标题（不占音频时间轴，适合音频直接读正文时展示故事名）")
    b.add_argument("--header-title2", default=None,
                   help="可选第二语种故事标题（配合 --subtitle-lang 自动选用或双语拼接）")
    b.add_argument("--tail", type=float, default=2.0, help="结尾停留秒数，默认 2")
    b.add_argument("--title-pause", type=float, default=None,
                   help="标题与正文之间插入的静音秒数；自动对齐默认 1.2，--ass 重导默认 0，传 0 关闭")
    b.add_argument("--title-pause-at", type=float, default=None,
                   help="插入点绝对秒（标题朗读结束、正文起音前）；不给则自动估算，建议复核后显式指定")
    b.add_argument("--trim-start", type=float, default=0.0)
    b.add_argument("--trim-end", type=float, default=None,
                   help="正数=绝对终点秒；负数=距结尾 N 秒（如 -1）")
    b.add_argument("--silence-db", type=float, default=None, help="静音检测阈值 dB（默认中文 -42、英文 -38）")
    b.add_argument("--silence-min", type=float, default=None, help="最短静音时长秒（默认中文 0.30、英文 0.22）")
    b.add_argument("--snap", type=float, default=None, help="句边界吸附到停顿的最大偏差秒（默认中文 0.90、英文 1.35）")
    b.add_argument("--align", choices=["auto", "asr", "silence"], default="auto",
                   help="对齐引擎：auto=优先探测本地离线 whisper-cli，无则回退静音对齐；asr=强制离线 ASR；silence=仅静音对齐")
    b.add_argument("--asr-bin", default=None, help="本地 whisper-cli 路径（默认查 PATH 或 WHISPER_BIN）")
    b.add_argument("--asr-model", default=None, help="本地 whisper ggml 模型文件路径（默认查 ~/.cache/whisper-cpp/ 或 WHISPER_MODEL）")
    b.add_argument("--min-cue", type=float, default=1.0, help="单句最短显示秒数")
    b.add_argument("--line-chars", type=int, default=None,
                   help="每行最多字符数（1080 宽基准；默认中文 13、英文 34）")
    b.add_argument("--font", default=None, help="主字幕字体（默认中文 Noto Sans CJK SC，英文 Georgia）")
    b.add_argument("--font2", default=None, help="双语第二语种字体（不传则按语种自动选）")
    b.add_argument("--font-size", type=int, default=None)
    b.add_argument("--font-size2", type=int, default=None, help="双语第二语种字号")
    b.add_argument("--title-size", type=int, default=None)
    b.add_argument("--title-first", type=int, default=1, help="前 N 句用标题样式")
    b.add_argument("--title-last", type=int, default=1, help="后 N 句用标题样式")
    b.add_argument("--text-color", default="FFF7F4", help="字幕主色 RGB hex")
    b.add_argument("--outline-color", default="281810", help="描边色 RGB hex")
    b.add_argument("--fade", type=int, default=170, help="淡入淡出毫秒")
    b.add_argument("--keep-emoji", action="store_true", help="保留 emoji（可能显示方块）")
    b.add_argument("--subtitle-out", help="额外导出 ASS 路径")
    b.add_argument("--srt", action="store_true", help="同时导出 SRT")
    b.add_argument("--zoom", type=float, default=0.0, help="cover 图片背景每帧推近量（如 0.00002），默认 0 关闭以保证长片快速稳定编码")
    b.add_argument("--preset", default="veryfast")
    b.add_argument("--crf", type=int, default=20)
    b.add_argument("--work-dir", help="中间产物目录")
    b.add_argument("--dry-run", action="store_true", help="只生成 ASS/背景/时间轴，不编码")
    b.set_defaults(func=cmd_build)
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    try:
        sys.exit(args.func(args))
    except Exception as e:
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)
