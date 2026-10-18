# -*- coding: utf-8 -*-
"""speech 类型识别冒烟测试：用金锁记失败案例的真实转写片段 + 典型演讲开头。"""
import io, sys, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deepread

# 真实转写片段（取自 20261017 报告中逐字金句）+ 演讲称呼语（演讲必有）
sample = """
女士们，先生们，朋友们，晚上好。
我們的繁榮 不知何故成了罪惡感的來源。
他們將巨石從悬崖上扔下 砸向波斯入侵者。
我們在傳奇的 斯巴達勇士300人的抵抗中 看到了這一點 他們被波斯大軍包圍 寡不敵眾 卻放棄了自救的機會 選擇戰鬥到最後一人。
這種自由的政府形式 這種民眾集會 這種自由辯論 和公共討論的實踐 都在提醒我們 希臘曾經存在過。
我們選擇開拓新的領域 在先進製造業和超級智能領域 在無人機 武器和戰爭領域 在芯片 礦產 網絡安全領域 釋放我們的能量。
我們應該為我們的出身感到驕傲 無比驕傲。
我們文明的故事 是人類歷史上最偉大的故事 而它就始於此。
朋友們，西方文明的未來取決於我們今天的選擇。謝謝大家。
"""

sc = deepread._type_scores(sample)
ranked = sorted(sc.items(), key=lambda kv: -kv[1])
print("规则得分 Top4:", [(k, round(v, 2)) for k, v in ranked[:4]])

t, c, m = deepread.classify_text(sample)
print(f"分类结果: {t} ({c} · {m})  -> 标签: {deepread.TYPE_LABELS.get(t)}")
assert t == "speech", f"FAIL: 期望 speech，得到 {t}"
print("PASS")
