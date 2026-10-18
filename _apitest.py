# -*- coding: utf-8 -*-
"""接口验收（不触发 LLM 推理）"""
import json
import sys
import urllib.request
sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8790"
# 注册表死代理会劫持 urllib —— 必须用空代理 opener 直连
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_jin = open(r"E:/Workbuddy/Comic_Book/data/0a6e382e-5456-4d28-827a-9c06b74b9600/ch09_金锁记.txt",
            encoding="utf-8").read()
AUS = open(r"E:/Workbuddy/DeepRead/source_input/奧地利學派經濟學.txt", encoding="utf-8").read()


def get(path):
    try:
        with _opener.open(BASE + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def post(path, obj):
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with _opener.open(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


ok = 0
fail = []


def chk(name, cond, detail=""):
    global ok
    if cond:
        ok += 1
        print(f"  ✅ {name} {detail}")
    else:
        fail.append(name)
        print(f"  ❌ {name} {detail}")


print("== 基础接口 ==")
s, j = get("/api/health")
chk("health", s == 200 and j.get("version") == "0.3", str(j))
chk("modes 含 book", "book" in j.get("modes", []), str(j.get("modes")))
chk("ocr_model", j.get("ocr_model") == "qwen3-vl:8b", j.get("ocr_model", ""))

s, j = get("/api/history")
chk("history", s == 200 and len(j.get("items", [])) > 0, f"{len(j.get('items', []))} 份")

s, j = get("/api/file?name=../../server.py")
chk("file 路径穿越拦截", s == 400, str(j))

s, j = get("/api/batch/status?id=nope")
chk("batch 404", s == 404, str(j))

with _opener.open(BASE + "/", timeout=10) as r:
    html = r.read().decode("utf-8")
chk("首页渲染", r.status == 200 and "DeepRead" in html)
chk("首页含整书模式", 'data-m="book"' in html)
chk("首页含对抗复核开关", 'id="critique"' in html)
chk("首页含批量队列面板", 'id="queue"' in html)
chk("首页多选上传", "multiple" in html)

print("== 模式推荐（启发式，零成本）==")
for tag, txt, want in (("小说 31k", _jin, "map"),
                       ("论述 5.9k", AUS, "deep"),
                       ("短文本 300 字", _jin[:300], "quick")):
    s, j = post("/api/recommend", {"text": txt[:20000]})
    chk(f"recommend {tag}", j.get("mode") == want,
        f"→ {j.get('mode')} ({j.get('reason','')[:34]})")

s, j = post("/api/recommend", {"text": "太短"})
chk("recommend 拒绝短文", s == 400, str(j))

print(f"\n通过 {ok} 项，失败 {len(fail)} 项 {fail if fail else ''}")
