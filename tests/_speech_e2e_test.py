# -*- coding: utf-8 -*-
"""speech 类型端到端冒烟：小样本手动指定 --type speech，验证 spec 注入与成稿。"""
import io, sys, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deepread

sample = open(os.path.join(os.path.dirname(__file__), "_speech_sample.txt"),
              encoding="utf-8").read()
md, struct = deepread.deepread_text(sample, title="speech冒烟", mode="deep",
                                    dtype="speech")
assert "## 演讲者与场合" in md, "speech spec 未注入"
assert "## 内容摘要" in md and "## 核心要点" in md, "摘要/要点段缺失"
assert "时间线大事记" not in md, "不应出现 history 模板"
print("E2E PASS · 报告结构:", [l for l in md.splitlines() if l.startswith("## ")])
