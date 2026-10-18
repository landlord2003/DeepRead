# -*- coding: utf-8 -*-
"""生成测试素材：①扫描件 PDF（图片型，用于 OCR 兜底验证）②带章节标记的书（book 模式验证）"""
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
from PIL import Image, ImageDraw, ImageFont

JIN = r"E:/Workbuddy/Comic_Book/data/0a6e382e-5456-4d28-827a-9c06b74b9600/ch09_金锁记.txt"
OUT_PDF = r"E:/Workbuddy/DeepRead/_tmp_scan.pdf"
OUT_BOOK = r"E:/Workbuddy/DeepRead/_tmp_book.txt"

src = open(JIN, encoding="utf-8").read()

# ---------- ① 扫描件 PDF ----------
seg = src[:620].replace("\n", "")
FONT = "C:/Windows/Fonts/msyh.ttc"
try:
    font = ImageFont.truetype(FONT, 30)
except OSError:
    font = ImageFont.load_default()
    print("!! 未找到中文字体，OCR 测试将失真")

pages = []
for pi in range(2):
    img = Image.new("RGB", (1240, 1754), "white")
    d = ImageDraw.Draw(img)
    y = 130
    chunk = seg[pi * 300:(pi + 1) * 300]
    line = ""
    for ch in chunk:
        line += ch
        if len(line) >= 34:
            d.text((100, y), line, fill=(20, 20, 20), font=font)
            y += 46
            line = ""
    if line:
        d.text((100, y), line, fill=(20, 20, 20), font=font)
    d.text((580, 1680), f"- {pi + 1} -", fill=(60, 60, 60), font=font)
    pages.append(img)
pages[0].save(OUT_PDF, "PDF", resolution=150.0, save_all=True,
              append_images=pages[1:])
print("scan pdf:", OUT_PDF, os.path.getsize(OUT_PDF), "bytes")

# 验证 pypdf 是否能提取到文本（应几乎为空）
from pypdf import PdfReader
r = PdfReader(OUT_PDF)
extracted = "".join((p.extract_text() or "") for p in r.pages)
print(f"pypdf 提取: {len(extracted)} 字 / {len(r.pages)} 页 → 判定扫描件="
      f"{len(extracted) / len(r.pages) < 60}")

# ---------- ② 带章节的书 ----------
parts = [src[i:i + 9000] for i in range(0, 27000, 9000)]
book = "《金锁记》（分章结构测试版）\n"
for i, p in enumerate(parts, 1):
    book += f"\n第{i}章 第{i}章测试段落\n\n{p}\n"
open(OUT_BOOK, "w", encoding="utf-8").write(book)
print("book txt:", OUT_BOOK, len(book), "chars")

import deepread as D
print("split_book →", [(n, D.text_len(b)) for n, b in D.split_book(book)])
