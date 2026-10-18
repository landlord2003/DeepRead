# -*- coding: utf-8 -*-
"""离线自测：叙事检测 / 金句校验 / 章节切分（不调用 LLM）"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import deepread as D

JIN = r"E:/Workbuddy/Comic_Book/data/0a6e382e-5456-4d28-827a-9c06b74b9600/ch09_金锁记.txt"
AUS = r"E:/Workbuddy/DeepRead/source_input/奧地利學派經濟學.txt"
LOG = r"E:/Workbuddy/DeepRead/source_input/十个家喻户晓的故事 带你学懂逻辑学的十个核心原理.txt"

print("=" * 60)
for tag, p in (("金锁记(小说)", JIN), ("奥地利学派(论述)", AUS), ("逻辑学故事(?)", LOG)):
    t = D.read_any(p, ocr=False)
    s = D.narrative_signals(t)
    print(f"{tag}: {D.text_len(t)}字 narrative={D.is_narrative(t)} "
          f"推荐={D.recommend_mode(t)[0]}")
    print(f"   引号{s['quotes']}(密度{s['quote_density']:.4f}) 对话{s['dialog']} "
          f"时间{s['temporal']} 人称密度{s['person_density']:.4f} 章节{s['chapters']}")

print("=" * 60)
jin = D.read_any(JIN, ocr=False)
print("split_book(金锁记):", [(n, D.text_len(b)) for n, b in D.split_book(jin)])
print("split_book(奥地利):", [(n, D.text_len(b)) for n, b in D.split_book(D.read_any(AUS, ocr=False))])

print("=" * 60)
# 假金句（旧报告里的转述）vs 真原句
ntext = D._norm(jin)
samples = [
    "七巧抱怨住房分配不公，暗讽姜家'净等着做孤儿寡妇'。",
    "三十年前的月亮是铜钱大，三十的月亮是银元大。",
    "长安活脱一个七巧。",
    "三十年前的上海，一个有月亮的晚上……我们也许没赶上看见三十年前的月亮。",
    "她戴着黄金的枷，用枷角劈死了几个人。",
]
for q in samples:
    print(f"  [{D.verify_quote(q, jin, ntext)}] {q[:45]}")

print("=" * 60)
print("程序候选金句（harvest_quotes 前 8 条）：")
for q in D.harvest_quotes(jin, 8):
    print("  ·", q[:70])

print("=" * 60)
import re
chaptered = re.sub(r"\n", "\n第%d章 测试章节\n" % 1, jin, count=0)
book = "".join(f"\n第{i}章 第{i}个章节标题\n" + jin[:8000] for i in range(1, 9))
print("split_book(合成8章书):", [(n, D.text_len(b)) for n, b in D.split_book(book)])
