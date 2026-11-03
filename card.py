# -*- coding: utf-8 -*-
"""把状态更新画成一张卡片（Pillow，无需浏览器）。

为什么不用 AstrBot 的 text_to_image：那套卡片页眉是它自己的 logo，
且没有浏览器时只能走本地 Pillow 模板，改不了样式。
这里自己画，页眉用 DeepSeek 的 logo，配色也照 DeepSeek 来。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

LOGO = Path(__file__).resolve().parent / "deepseek_logo.png"

# 配色照 DeepSeek 的深蓝 + 状态页的中性灰
PAPER = (255, 255, 255)
INK = (24, 26, 32)
MUTED = (110, 116, 128)
LINE = (226, 229, 234)
ACCENT = (77, 107, 254)      # DeepSeek 蓝
SOFT = (245, 246, 250)

STATUS_STYLE = {
    "resolved": ("已恢复", (22, 163, 74)),
    "monitoring": ("监控中", (217, 119, 6)),
    "identified": ("已定位", (217, 119, 6)),
    "investigating": ("调查中", (220, 38, 38)),
    "maintenance": ("维护", (99, 102, 241)),
}

WIDTH = 880
PAD = 40


def _font(size: int, bold: bool = False):
    """优先用 AstrBot 自带的跨平台字体管理器，拿不到就退回系统字体。"""
    try:
        from astrbot.core.utils.t2i.local_strategy import FontManager

        return FontManager.get_font(size, bold=bold)
    except Exception:
        pass
    for name in (
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    """按像素宽度断行，中文逐字、英文按词，够用就行。"""
    lines: list[str] = []
    for raw in (text or "").splitlines() or [""]:
        if not raw.strip():
            lines.append("")
            continue
        buf = ""
        for token in re.findall(r"[A-Za-z0-9_\-./:%@#?&=+]+|\s+|.", raw):
            candidate = buf + token
            if draw.textlength(candidate, font=font) <= max_width or not buf:
                buf = candidate
            else:
                lines.append(buf.rstrip())
                buf = token.lstrip() if token.isspace() else token
        if buf:
            lines.append(buf.rstrip())
    return lines


def _status_of_entry(entry: dict) -> str:
    text = (entry.get("summary") or "") + (entry.get("title") or "")
    low = text.lower()
    if "已恢复" in text or "resolved" in low:
        return "resolved"
    if "监控" in text or "monitoring" in low:
        return "monitoring"
    if "已定位" in text or "identified" in low:
        return "identified"
    if "调查" in text or "investigating" in low:
        return "investigating"
    if "维护" in text or "maintenance" in low:
        return "maintenance"
    return ""


def render_card(entries: list[dict], out_path: Path, *, max_body_lines: int = 14) -> str:
    """把条目画成图，返回图片路径。"""
    font_title = _font(30, bold=True)
    font_meta = _font(17)
    font_body = _font(19)
    font_pill = _font(16, bold=True)
    font_brand = _font(20, bold=True)

    # 先算高度：标题/正文都要换行，只能先排版再画
    probe = Image.new("RGB", (WIDTH, 10), PAPER)
    pd = ImageDraw.Draw(probe)
    inner = WIDTH - PAD * 2

    blocks = []
    for entry in entries:
        title_lines = _wrap(pd, entry.get("title", "（无标题）"), font_title, inner)
        body = (entry.get("summary") or "").strip()
        # 药丸已经写着「监控中/已恢复」，正文第一行再写一遍是重复
        label_now = STATUS_STYLE.get(_status_of_entry(entry), ("", None))[0]
        if label_now and body.startswith(label_now):
            body = body[len(label_now):].lstrip("\n ·:：")
        if len(body) > 600:
            body = body[:600].rstrip() + "……"
        body_lines = _wrap(pd, body, font_body, inner)[:max_body_lines]
        blocks.append((entry, title_lines, body_lines))

    height = PAD + 54  # 页眉
    for _e, title_lines, body_lines in blocks:
        height += 12 + len(title_lines) * 40 + 34  # 标题 + 状态行
        height += len(body_lines) * 30 + 16
        height += 34 if _e.get("link") else 0
        height += 26  # 分隔
    height += 40  # 页脚
    height = max(height, 320)

    img = Image.new("RGB", (WIDTH, height), PAPER)
    d = ImageDraw.Draw(img)

    # 页眉：DeepSeek logo + 右侧「状态更新」
    x, y = PAD, PAD
    if LOGO.exists():
        try:
            logo = Image.open(LOGO).convert("RGBA")
            ratio = 40 / logo.height
            logo = logo.resize((int(logo.width * ratio), 40), Image.LANCZOS)
            img.paste(logo, (x, y + 2), logo)
            x += logo.width + 14
        except Exception:
            pass
    d.text((x, y + 6), "状态更新", font=font_brand, fill=INK)
    stamp = datetime.now().strftime("%m-%d %H:%M")
    tw = d.textlength(stamp, font=font_meta)
    d.text((WIDTH - PAD - tw, y + 12), stamp, font=font_meta, fill=MUTED)
    y += 54
    d.line([(PAD, y), (WIDTH - PAD, y)], fill=LINE, width=2)
    y += 14

    for entry, title_lines, body_lines in blocks:
        for line in title_lines:
            d.text((PAD, y), line, font=font_title, fill=INK)
            y += 40
        # 状态药丸 + 时间
        status = _status_of_entry(entry)
        label, color = STATUS_STYLE.get(status, ("更新", MUTED))
        pw = d.textlength(label, font=font_pill) + 22
        d.rounded_rectangle([PAD, y, PAD + pw, y + 28], radius=14, fill=color)
        d.text((PAD + 11, y + 5), label, font=font_pill, fill=(255, 255, 255))
        meta = " · ".join(
            str(v) for v in (entry.get("source", ""), entry.get("updated", "")) if v
        )
        if meta:
            d.text((PAD + pw + 12, y + 4), meta, font=font_meta, fill=MUTED)
        y += 34

        for line in body_lines:
            d.text((PAD, y), line, font=font_body, fill=INK if line else PAPER)
            y += 30
        y += 6
        if entry.get("link"):
            d.text((PAD, y), entry["link"], font=font_meta, fill=ACCENT)
            y += 34

        d.line([(PAD, y), (WIDTH - PAD, y)], fill=LINE, width=1)
        y += 26

    d.text((PAD, height - 34), "status.deepseek.com", font=font_meta, fill=MUTED)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return str(out_path)
