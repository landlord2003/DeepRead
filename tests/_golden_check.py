# -*- coding: utf-8 -*-
"""golden 回归检查：对照 tests/golden_金锁记_核心总结与梗概.md 的 6 维评分。

用法：
  python tests/_golden_check.py <报告.md路径>   # 指定报告
  python tests/_golden_check.py                 # 自动取 reports/ 最新金锁记 deep 报告

输出：各维度 🟢/🟡/🔴 + 明细。事实/主干/意象/因果可自动判定，
取舍密度给参考区间，抽象锐度留人工（脚本只提示）。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import deepread as D  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

# 已知硬伤模式（历史版本真实出现过的错误）
HARD_ERRORS = [
    "季泽（七巧丈夫", "七巧的姨太太", "1943年（七巧去世", "长女芝寿",
    "芝寿（七巧的", "绢姑娘（七巧", "长安（七巧之妻",
    "二爷姜季泽",   # v4b 实测：残废的是二爷，季泽是三爷
]
# 主干事件关键词（梗概或正文必须覆盖）
BACKBONE = {
    "麻油店出身": ["麻油", "油铺", "油店"],
    "嫁残废二少爷": ["二爷", "残废", "骨痨"],
    "姜季泽": ["季泽"],
    "分家": ["分家"],
    "长安裹脚/辍学": ["裹脚", "裹足", "辍学", "退学"],
    "长安烟瘾/童世舫": ["烟", "童世舫"],
    "芝寿之死": ["芝寿"],
    "黄金枷锁意象": ["枷"],
}
CAUSAL_WORDS = ["使", "让", "逼", "导致", "为了", "因为", "因而", "于是",
                "迫使", "致使", "逼得", "害得",
                "因", "致", "由于"]  # 介词式因果（"因战乱避居"）也算


def latest_report():
    reps = sorted(f for f in os.listdir(os.path.join(ROOT, "reports"))
                  if "金锁记" in f and f.endswith(".md") and not f.startswith("_TEST_金锁记v2"))
    if not reps:
        reps = sorted(f for f in os.listdir(os.path.join(ROOT, "reports"))
                      if "金锁记" in f and f.endswith(".md"))
    return os.path.join(ROOT, "reports", reps[-1]) if reps else None


def section(md, name):
    m = re.search(rf"^##\s+[^\n]*{name}[^\n]*\n(.*?)(?=^##\s+|\Z)", md, re.S | re.M)
    return m.group(1).strip() if m else ""


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else latest_report()
    if not path or not os.path.exists(path):
        print("未找到金锁记报告"); return 1
    md = open(path, encoding="utf-8").read()
    story = section(md, "故事梗概")
    oneliner = section(md, "一句话总结")
    themes = section(md, "主题分层") or section(md, "核心论点")
    nstory = D.text_len(story)
    print(f"报告: {os.path.basename(path)}  |  梗概 {nstory} 字\n" + "-" * 62)

    rows = []
    # 1 事实准确（自动：已知硬伤模式）
    errs = [e for e in HARD_ERRORS if e in md]
    rows.append(("事实准确(已知硬伤)", "🟢 0 硬伤" if not errs else f"🔴 命中 {errs}", not errs))
    # 2 主干完整
    full_text = story + "\n" + md
    missing = [k for k, ws in BACKBONE.items() if not any(w in full_text for w in ws)]
    rows.append(("主干完整(8要素)", "🟢 8/8" if not missing else f"🔴 缺 {missing}", not missing))
    # 3 取舍密度（老吴 2026-10-17 定：>500 且 <1500）
    ok = 600 <= nstory <= 1400
    rows.append(("取舍密度(梗概字数 600-1400)", f"{'🟢' if ok else '🔴'} {nstory} 字（要求 >500 且 <1500）", ok))
    # 3b 机械连接词（老吴：去掉"起初"这类开头，不是人话）
    bad_conn = [w for w in ("起初，", "随后，", "此后，", "最终，", "接着，")
                if re.search(rf"(^|\n){re.escape(w)}", story)]
    rows.append(("无机械连接词开头(起初/随后…)", "🟢 无" if not bad_conn else f"🔴 命中 {bad_conn}", not bad_conn))
    # 4 因果解释（因果词密度 /千字）
    dens = sum(story.count(w) for w in CAUSAL_WORDS) / max(nstory / 1000, 0.5)
    ok = dens >= 5
    rows.append((f"因果解释(密度 {dens:.1f}/千字)", "🟢" if ok else "🟡（金标准约 9/千字）", ok))
    # 5 意象点睛："枷"进入总结或主题层
    ok = ("枷" in oneliner) or ("枷" in themes)
    rows.append(("意象点睛(枷锁入总结/主题)", "🟢" if ok else "🔴 总结/主题未点出核心意象", ok))
    # 6 抽象锐度（半自动：转变框架/表层深层线索）
    sharp = any(w in md for w in ("被压迫者", "压迫者", "受害者", "加害者", "施虐",
                                  "表层", "深层", "表面上", "底层"))
    rows.append(("抽象锐度(转变框架/表层深层)", "🟢 出现结构性框架" if sharp
                 else "🟡 未出现——建议人工复看 + 开对抗复核", sharp))

    print(f"{'维度':<28}{'判定'}\n" + "-" * 62)
    for k, v, okk in rows:
        print(f"{k:<30}{v}")
    passed = sum(1 for _, _, okk in rows if okk)
    print("-" * 62 + f"\n自动可判 {passed}/{len(rows)} 达标；抽象锐度终判需人工对照金标准。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
