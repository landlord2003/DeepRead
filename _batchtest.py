# -*- coding: utf-8 -*-
"""批量队列验收：多文件入队 → 单 worker 串行处理 → 逐项状态 → 报告可取"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8790"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

jin = open(r"E:/Workbuddy/Comic_Book/data/0a6e382e-5456-4d28-827a-9c06b74b9600/ch09_金锁记.txt",
           encoding="utf-8").read()
aus = open(r"E:/Workbuddy/DeepRead/source_input/奧地利學派經濟學.txt", encoding="utf-8").read()

# 造两个小文件
files = [("批量A_小说片段.txt", jin[:1400]), ("批量B_论述片段.txt", aus[:1400])]
for name, txt in files:
    open(name, "w", encoding="utf-8").write(txt)

# 手动拼 multipart（纯标准库，避免依赖 requests）
boundary = "----wb" + uuid.uuid4().hex
parts = []
parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"mode\"\r\n\r\nquick\r\n".encode())
parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"critique\"\r\n\r\n0\r\n".encode())
for name, txt in files:
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                  f"name=\"file\"; filename=\"{name}\"\r\n"
                  f"Content-Type: text/plain\r\n\r\n").encode("utf-8")
                 + txt.encode("utf-8") + b"\r\n")
parts.append(f"--{boundary}--\r\n".encode())
body = b"".join(parts)

req = urllib.request.Request(
    BASE + "/api/batch", data=body,
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
t0 = time.time()
with _opener.open(req, timeout=60) as r:
    submit = json.loads(r.read().decode("utf-8"))
print("提交响应:", json.dumps(submit, ensure_ascii=False))
bid = submit.get("id")
assert bid, "未拿到 batch id"
assert submit.get("total") == 2, f"total 应为 2，实际 {submit.get('total')}"

# 轮询
last = ""
while time.time() - t0 < 900:
    time.sleep(5)
    with _opener.open(f"{BASE}/api/batch/status?id={bid}", timeout=20) as r:
        st = json.loads(r.read().decode("utf-8"))
    line = f"[{int(time.time()-t0):>3}s] {st['done']}/{st['total']} " + " | ".join(
        f"{i['name'][:10]}:{i['state']}"
        + (f"({i.get('stage','')}{i.get('sdone',0)}/{i.get('stotal',0)})"
           if i["state"] == "running" else "")
        + (f"→{i.get('file','')[-28:]}" if i["state"] == "done" else "")
        + (f"!{i.get('error','')[:20]}" if i["state"] == "error" else "")
        for i in st["items"])
    if line != last:
        print(line)
        last = line
    if st["state"] == "done":
        print(f"\n✅ 队列完成，总耗时 {int(time.time()-t0)}s")
        okc = sum(1 for i in st["items"] if i["state"] == "done")
        print(f"   成功 {okc}/{st['total']}")
        for i in st["items"]:
            if i["state"] == "done":
                fn = i["file"]
                with _opener.open(f"{BASE}/api/file?name={urllib.parse.quote(fn)}",
                                  timeout=20) as r:
                    rep = json.loads(r.read().decode("utf-8"))
                print(f"   📄 {fn}  → {len(rep.get('md',''))} 字符，标题行："
                      f"{rep.get('md','').splitlines()[0][:40]}")
        break
else:
    print("❌ 超时未完成")

for name, _ in files:
    if os.path.exists(name):
        os.remove(name)
