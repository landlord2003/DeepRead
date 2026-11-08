# -*- coding: utf-8 -*-
"""
DeepRead Web 服务 v0.3 —— 纯标准库 ThreadingHTTPServer（沿用 info-hub 架构约定）
GET  /                  → 界面
POST /api/read          → {mode,title,text,critique} 提交精读任务 → {id}
POST /api/compare       → {title,text} 四模式对比任务 → {id}
POST /api/upload        → multipart 上传；带 parse_only=1 只解析不启动
POST /api/batch         → multipart 多文件批量入队（单 worker 串行，避免 Ollama 争抢）
GET  /api/batch/status?id= → {state,done,total,items[]}
POST /api/recommend     → {text} 模式推荐 → {mode,reason}
GET  /api/status?id=    → {state, stage, done, total}
GET  /api/report?id=    → {md} 或对比任务 {md, reports}
GET  /api/file?name=    → 读取 reports/ 下已存报告
GET  /api/history       → 报告列表
GET  /api/health        → {ok, model, modes, version}
端口：DEEPREAD_PORT 环境变量，默认 8790
"""
import json
import os
import re
import threading
import time
import urllib.parse
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import deepread

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
REPORTS = os.path.join(ROOT, "reports")
PORT = int(os.environ.get("DEEPREAD_PORT", "8790"))
VERSION = "0.6"
os.makedirs(REPORTS, exist_ok=True)

JOBS = {}          # id -> {state, stage, done, total, md, ...}
JOB_LOCK = threading.Lock()
SAFE_NAME = re.compile(r"^[\w\u4e00-\u9fff.\-]+\.md$")

# ---- 批量队列（内存队列；服务重启即清空，不落盘）----
BATCHES = {}                        # bid -> {state, done, total, items[]}
QUEUE = deque()                     # (bid, idx)
QUEUE_LOCK = threading.Lock()
_worker_started = False
MAX_BATCH_ITEMS = int(os.environ.get("DEEPREAD_MAX_BATCH", "20"))
MAX_ITEM_CHARS = int(os.environ.get("DEEPREAD_MAX_ITEM_CHARS", "3000000"))
ALLOWED_EXT = (".txt", ".md", ".markdown", ".pdf", ".docx", ".epub") + deepread.OCR_EXTS


def _save_report(md: str, title: str, tag: str) -> str:
    base = (f"{time.strftime('%Y%m%d_%H%M%S')}_"
            f"{re.sub(r'[^\w\u4e00-\u9fff-]', '_', title)[:20]}_{tag}")
    fname = base + ".md"
    n = 1
    while os.path.exists(os.path.join(REPORTS, fname)):     # 同秒重名保护
        n += 1
        fname = f"{base}_{n}.md"
    with open(os.path.join(REPORTS, fname), "w", encoding="utf-8") as f:
        f.write(md)
    return fname


# 阶段元数据：label 前端展示名 / desc 该步骤在做什么 / out 成果描述模板
STAGE_INFO = {
    "parse":    ("解析文件", "识别格式并提取纯文本；扫描件自动转视觉 OCR 逐页转写",
                 "提取全文"),
    "ocr":      ("OCR 识别", "图片型页面走 qwen3-vl 逐页转写", "转写 {total} 页"),
    "wait":     ("排队等待", "批量队列单 worker 串行，前面的任务处理中", ""),
    "map":      ("逐块深读", "把全文切成约 7500 字/块，逐块提取要点卡片、数据与金句候选",
                 "产出 {total} 块卡片"),
    "reduce":   ("分层汇总", "把各块卡片按 5 块一组归并为分段小结，逐层收敛",
                 "归并为 {total} 段小结"),
    "story":    ("生成故事梗概", "基于原文分批叙述再合成 600-1400 字归纳式梗概",
                 "{total} 批原文 → 合成"),
    "cand":     ("候选素材萃取", "逐块原文萃取原则/方法步骤/术语/禁忌，每条带原文逐字摘录",
                 "萃取 {total} 块候选素材"),
    "distill":  ("全书骨架与聚类", "生成全书骨架（主旨/结构/主张/概念/盲点），按使用场景聚类成技能方案",
                 "骨架 + 聚类方案"),
    "skill":    ("生成技能包", "按聚类方案逐个产出 SKILL.md（核心判断/怎么做/判断标准/出处）",
                 "{total} 个技能包"),
    "book":     ("分部深读", "按章节把整书分成若干部，逐部精读", "{total} 部"),
    "compose":  ("成稿", "按模式结构组装最终报告", ""),
    "final":    ("成稿", "按模式结构组装最终报告", ""),
    "critique": ("对抗式复核", "四视角评审挑刺 + 主编回应", "4 视角评审"),
}


def _make_progress(job: dict):
    """步骤级遥测：阶段切换时关闭上一步（记耗时），并记录各步起止/进度。"""
    cur = {"stage": None}

    def progress(stage, done, total):
        now = time.time()
        with JOB_LOCK:
            steps = job.setdefault("steps", [])
            if stage != cur["stage"]:
                if steps and steps[-1].get("running"):
                    steps[-1]["running"] = False
                    steps[-1]["secs"] = round(now - steps[-1]["t0"], 1)
                label, desc, out = STAGE_INFO.get(stage, (stage, "", ""))
                steps.append({"stage": stage, "label": label, "desc": desc,
                              "out": out, "done": done, "total": total,
                              "t0": now, "running": True})
                cur["stage"] = stage
            s = steps[-1]
            s["done"], s["total"] = done, total
            s["elapsed"] = round(now - s["t0"], 1)
            job.update(stage=stage, done=done, total=total)
    return progress


def _steps_payload(job: dict) -> list:
    """把 job['steps'] 压成前端渲染所需的最小结构。"""
    out = []
    for s in job.get("steps") or []:
        it = {"stage": s["stage"], "label": s["label"], "desc": s["desc"],
              "out": s.get("out") or "", "done": s["done"],
              "total": s["total"], "running": bool(s.get("running"))}
        it["elapsed" if it["running"] else "secs"] = \
            s.get("elapsed") if it["running"] else s.get("secs") or 0
        out.append(it)
    return out


def _close_steps(job: dict):
    """任务结束（done/error）时关闭最后一个未闭合步骤，记下耗时。"""
    steps = job.get("steps") or []
    if steps and steps[-1].get("running"):
        steps[-1]["running"] = False
        steps[-1]["secs"] = round(time.time() - steps[-1]["t0"], 1)


def _run_single(jid, text, title, mode, critique=False, story=True, dtype=""):
    job = JOBS[jid]
    progress = _make_progress(job)
    try:
        md, st = deepread.deepread_text(text, title, mode, progress=progress,
                                        critique=critique, story=story,
                                        dtype=dtype)
        fname = _save_report(md, title, mode)
        with JOB_LOCK:
            _close_steps(job)
            job.update(state="done", md=md, file=fname,
                       nchar=st["nchar"], nchunks=st.get("nchunks", 0),
                       is_narrative=st.get("is_narrative", False),
                       dtype=st.get("dtype", "general"),
                       dtype_conf=st.get("dtype_conf", ""),
                       story_chars=deepread.text_len(st.get("story_summary", "")))
    except Exception as e:  # noqa: BLE001 —— 任务失败要反馈给前端
        with JOB_LOCK:
            _close_steps(job)
            job.update(state="error", error=str(e)[:300])


def _run_compare(jid, text, title, mode=None, critique=False, story=True,
                 dtype="", modes=None):
    job = JOBS[jid]
    progress = _make_progress(job)
    try:
        reports, merged, summary = deepread.deepread_compare(
            text, title, progress=progress, dtype=dtype, modes=modes)
        fname = _save_report(merged, title, "对比")
        with JOB_LOCK:
            _close_steps(job)
            job.update(state="done", md=merged, reports=reports, file=fname,
                       nchar=len(text), summary=summary)
    except Exception as e:  # noqa: BLE001
        with JOB_LOCK:
            _close_steps(job)
            job.update(state="error", error=str(e)[:300])


def _submit(fn, text, title, mode="deep", critique=False, story=True,
            dtype="", modes=None):
    jid = f"{int(time.time()*1000):x}"[-10:]
    with JOB_LOCK:
        JOBS[jid] = {"state": "running", "stage": "map", "done": 0,
                     "total": 0, "title": title, "mode": mode,
                     "t": time.time()}
    kwargs = {"modes": modes} if modes is not None else {}

    def _safe(jid=jid):
        try:
            fn(jid, text, title, mode, critique, story, dtype, **kwargs)
        except Exception as e:  # noqa: BLE001 —— 线程崩溃也要把任务标记为失败
            with JOB_LOCK:
                JOBS[jid].update(state="error", error=f"任务线程崩溃: {e}")
    threading.Thread(target=_safe, daemon=True).start()
    return jid


# ---------------------------------------------------------------- 队列 worker

def _ensure_worker():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=_queue_worker, daemon=True).start()


def _queue_worker():
    """单 worker 串行消费：Ollama 单实例并发会争抢显存，必须串行。"""
    while True:
        item = None
        with QUEUE_LOCK:
            if QUEUE:
                item = QUEUE.popleft()
        if not item:
            time.sleep(0.5)
            continue
        bid, idx = item
        batch = BATCHES.get(bid)
        if not batch or idx >= len(batch["items"]):
            continue
        rec = batch["items"][idx]
        if rec.get("state") != "queued":
            continue
        rec["state"] = "running"
        rec["stage"] = "parse"
        try:
            path = rec.pop("path", None)
            text = deepread.read_any(path, progress=_mk_progress(rec)) if path \
                else rec.get("text") or ""
            if path and os.path.exists(path):
                os.remove(path)
            rec.pop("text", None)
            if deepread.text_len(text) < 50:
                raise ValueError("文本太短（<50字）")
            md, st = deepread.deepread_text(
                text, rec["title"], rec["mode"],
                progress=_mk_progress(rec), critique=rec.get("critique", False),
                story=rec.get("story", True), dtype=rec.get("dtype", ""))
            fname = _save_report(md, rec["title"], rec["mode"])
            rec.update(state="done", file=fname, nchar=st["nchar"],
                       is_narrative=st.get("is_narrative", False),
                       dtype=st.get("dtype", "general"),
                       dtype_conf=st.get("dtype_conf", ""),
                       story_chars=deepread.text_len(st.get("story_summary", "")))
        except Exception as e:  # noqa: BLE001
            rec.update(state="error", error=str(e)[:300])
            p = rec.pop("path", None)
            if p and os.path.exists(p):
                os.remove(p)
        finally:
            with QUEUE_LOCK:
                batch["done"] = sum(1 for x in batch["items"]
                                    if x["state"] in ("done", "error"))
                if batch["done"] >= len(batch["items"]):
                    batch["state"] = "done"


def _mk_progress(rec):
    def progress(stage, done, total):
        rec.update(stage=stage, sdone=done, stotal=total)
    return progress


def _parse_multipart(raw: bytes, boundary: bytes):
    """通用 multipart 解析：返回 (字段dict, [(filename, blob), ...])。"""
    fields, files = {}, []
    for part in raw.split(b"--" + boundary):
        part = part.strip(b"\r\n--") if part.strip() == b"--" else part
        if not part.strip() or part.strip() == b"--":
            continue
        head, _, body = part.partition(b"\r\n\r\n")
        text = head.decode("utf-8", "ignore")
        mn = re.search(r'name="([^"]+)"', text)
        mf = re.search(r'filename="([^"]+)"', text)
        if body.endswith(b"\r\n"):
            body = body[:-2]
        if mf:
            files.append((urllib.parse.unquote(mf.group(1)), body))
        elif mn:
            fields[mn.group(1)] = body.decode("utf-8", "ignore").strip()
    return fields, files


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 静默访问日志
        pass

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ---------------- GET ----------------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            try:
                data = open(os.path.join(WEB, "index.html"), "rb").read()
                self._send(200, data, "text/html; charset=utf-8")
            except FileNotFoundError:
                self._json({"error": "web/index.html 缺失"}, 500)
        elif u.path.startswith("/vendor/"):
            fname = u.path[len("/vendor/"):]
            if ".." in fname or fname.startswith("/") or not fname.endswith((".js", ".css")):
                self._json({"error": "not found"}, 404)
            else:
                fpath = os.path.join(WEB, "vendor", fname)
                if os.path.isfile(fpath):
                    data = open(fpath, "rb").read()
                    ctype = ("application/javascript; charset=utf-8"
                             if fname.endswith(".js") else "text/css; charset=utf-8")
                    self._send(200, data, ctype)
                else:
                    self._json({"error": "not found"}, 404)
        elif u.path == "/api/status":
            job = JOBS.get((q.get("id") or [""])[0])
            if not job:
                self._json({"error": "job 不存在"}, 404)
            else:
                with JOB_LOCK:
                    keys = ("state", "stage", "done", "total", "error",
                            "title", "mode", "nchar", "nchunks", "file",
                            "is_narrative")
                    out = {k: job.get(k) for k in keys}
                    out["steps"] = _steps_payload(job)
                    self._json(out)
        elif u.path == "/api/report":
            job = JOBS.get((q.get("id") or [""])[0])
            if not job:
                self._json({"error": "job 不存在"}, 404)
            elif job.get("state") == "done":
                out = {"md": job["md"], "title": job["title"],
                       "mode": job["mode"], "file": job.get("file")}
                if job.get("reports"):          # 对比任务
                    out["reports"] = job["reports"]
                    out["summary"] = job.get("summary") or ""
                self._json(out)
            else:
                self._json({"error": "报告未就绪"}, 409)
        elif u.path == "/api/batch/status":
            b = BATCHES.get((q.get("id") or [""])[0])
            if not b:
                self._json({"error": "batch 不存在"}, 404)
            else:
                self._json({"state": b["state"], "done": b["done"],
                            "total": b["total"],
                            "items": [{k: v for k, v in it.items()
                                       if k not in ("text", "path")}
                                      for it in b["items"]]})
        elif u.path == "/api/file":
            name = (q.get("name") or [""])[0]
            if not SAFE_NAME.match(name) or ".." in name:
                self._json({"error": "非法文件名"}, 400)
                return
            p = os.path.join(REPORTS, name)
            if not os.path.isfile(p):
                self._json({"error": "文件不存在"}, 404)
            else:
                self._json({"md": open(p, encoding="utf-8").read(),
                            "title": name})
        elif u.path == "/api/history":
            items = []
            for fn in sorted(os.listdir(REPORTS), reverse=True)[:80]:
                p = os.path.join(REPORTS, fn)
                if os.path.isdir(p) and fn.startswith("distill_"):
                    items.append({"file": fn, "type": "dir",
                                  "size": 0,
                                  "mtime": int(os.path.getmtime(p) * 1000)})
                elif fn.endswith(".md"):
                    items.append({"file": fn, "type": "file",
                                  "size": os.path.getsize(p),
                                  "mtime": int(os.path.getmtime(p) * 1000)})
            items.sort(key=lambda x: -x["mtime"])
            self._json({"items": items[:60]})
        elif u.path == "/api/open_folder":
            name = (q.get("name") or [""])[0]
            target = REPORTS
            if name:
                if ".." in name or "/" in name or "\\" in name:
                    self._json({"error": "非法名称"}, 400)
                    return
                p = os.path.join(REPORTS, name)
                if not os.path.exists(p):
                    self._json({"error": "不存在: " + name}, 404)
                    return
                target = p
            try:
                if os.path.isdir(target):
                    os.startfile(target)          # noqa: S606 打开目录
                else:
                    # 资源管理器定位到该文件（路径含空格需引号）
                    os.startfile("explorer",  # noqa: S606
                                 arguments=f'/select,"{target}"')
                self._json({"ok": True})
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)}, 500)
        elif u.path == "/api/health":
            self._json({"ok": True, "model": deepread.MODEL,
                        "modes": list(deepread.ALL_MODES),
                        "ocr_model": deepread.OCR_MODEL,
                        "version": VERSION})
        else:
            self._json({"error": "not found"}, 404)

    # ---------------- POST ----------------
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        try:
            self._route_post(raw)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"服务端异常: {e}"}, 500)

    def _route_post(self, raw: bytes):
        if self.path == "/api/read":
            body = json.loads(raw.decode("utf-8"))
            text = (body.get("text") or "").strip()
            if deepread.text_len(text) < 50:
                self._json({"error": "文本太短（至少50字）"}, 400)
                return
            jid = _submit(_run_single, text, body.get("title") or "粘贴文本",
                          body.get("mode") or "deep",
                          bool(body.get("critique")),
                          body.get("story", True) is not False,
                          body.get("dtype") or "")
            self._json({"id": jid})
        elif self.path == "/api/compare":
            body = json.loads(raw.decode("utf-8"))
            text = (body.get("text") or "").strip()
            if deepread.text_len(text) < 50:
                self._json({"error": "文本太短（至少50字）"}, 400)
                return
            modes = [m for m in (body.get("modes") or [])
                     if m in deepread.ALL_MODES]
            if len(modes) < 2:
                self._json({"error": "对比至少选择 2 种模式"}, 400)
                return
            jid = _submit(_run_compare, text,
                          body.get("title") or "未命名", mode="compare",
                          modes=modes)
            self._json({"id": jid})
        elif self.path == "/api/recommend":
            body = json.loads(raw.decode("utf-8"))
            text = (body.get("text") or "").strip()
            if deepread.text_len(text) < 50:
                self._json({"error": "文本太短"}, 400)
                return
            mode, reason = deepread.recommend_mode(text)
            self._json({"mode": mode, "reason": reason})
        elif self.path == "/api/upload":
            self._route_upload(raw)
        elif self.path == "/api/batch":
            self._route_batch(raw)
        elif self.path == "/api/delete":
            body = json.loads(raw.decode("utf-8"))
            names = body.get("names") or []
            if not isinstance(names, list) or not names:
                self._json({"error": "未选择任何项"}, 400)
                return
            deleted, errors = 0, []
            for name in names:
                if (not isinstance(name, str) or ".." in name
                        or "/" in name or "\\" in name):
                    errors.append(f"{name}: 非法名称")
                    continue
                p = os.path.join(REPORTS, name)
                if not os.path.exists(p):
                    errors.append(f"{name}: 不存在")
                    continue
                try:
                    if os.path.isdir(p):
                        import shutil
                        shutil.rmtree(p)          # distill 技能包目录整体删除
                    else:
                        os.remove(p)
                    deleted += 1
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{name}: {e}")
            self._json({"deleted": deleted, "errors": errors})
        else:
            self._json({"error": "not found"}, 404)

    def _read_parts(self, raw):
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype)
        if not m:
            self._json({"error": "不是 multipart"}, 400)
            return None, None
        return _parse_multipart(raw, (m.group(1) or m.group(2)).encode())

    def _route_upload(self, raw):
        fields, files = self._read_parts(raw)
        if fields is None:
            return
        if not files:
            self._json({"error": "没有文件字段"}, 400)
            return
        filename, blob = files[0]
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXT:
            self._json({"error": f"不支持 {ext}，用 txt/md/pdf/docx/图片"}, 400)
            return
        tmp = os.path.join(REPORTS, f"_upload_{int(time.time()*1000)}{ext}")
        open(tmp, "wb").write(blob)
        try:
            text = deepread.read_any(tmp)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"解析失败: {e}"}, 400)
            os.remove(tmp)
            return
        os.remove(tmp)
        if deepread.text_len(text) < 50:
            self._json({"error": "文件文本太短"}, 400)
            return
        if fields.get("parse_only") == "1":   # 只解析，不启动任务
            self._json({"text": text, "nchar": deepread.text_len(text),
                        "title": os.path.basename(filename),
                        "is_narrative": deepread.is_narrative(text)})
            return
        jid = _submit(_run_single, text, os.path.basename(filename),
                      fields.get("mode") or "deep",
                      fields.get("critique") == "1",
                      fields.get("story", "1") != "0",
                      fields.get("dtype") or "")
        self._json({"id": jid, "nchar": deepread.text_len(text)})

    def _route_batch(self, raw):
        fields, files = self._read_parts(raw)
        if fields is None:
            return
        if not files:
            self._json({"error": "没有文件字段"}, 400)
            return
        if len(files) > MAX_BATCH_ITEMS:
            self._json({"error": f"单次最多 {MAX_BATCH_ITEMS} 个文件"}, 400)
            return
        mode = fields.get("mode") or "deep"
        critique = fields.get("critique") == "1"
        story = fields.get("story", "1") != "0"
        dtype = fields.get("dtype") or ""
        bid = "b" + f"{int(time.time()*1000):x}"[-8:]
        items = []
        for idx, (filename, blob) in enumerate(files):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in ALLOWED_EXT:
                items.append({"name": filename, "title": filename,
                              "mode": mode, "critique": critique,
                              "story": story, "dtype": dtype,
                              "state": "error",
                              "error": f"不支持的格式 {ext}"})
                continue
            tmp = os.path.join(REPORTS,
                               f"_upload_{int(time.time()*1000)}_{idx}{ext}")
            open(tmp, "wb").write(blob)
            items.append({"name": filename, "title": os.path.basename(filename),
                          "mode": mode, "critique": critique, "story": story,
                          "dtype": dtype,
                          "state": "queued", "path": tmp, "stage": "wait",
                          "sdone": 0, "stotal": 0, "nbytes": len(blob)})
        BATCHES[bid] = {"state": "running", "done": 0, "total": len(items),
                        "items": items, "t": time.time()}
        with QUEUE_LOCK:
            for idx, it in enumerate(items):
                if it["state"] == "queued":
                    QUEUE.append((bid, idx))
            BATCHES[bid]["done"] = sum(1 for x in items
                                       if x["state"] in ("done", "error"))
            if BATCHES[bid]["done"] >= len(items):
                BATCHES[bid]["state"] = "done"
        _ensure_worker()
        self._json({"id": bid, "total": len(items),
                    "items": [{"name": it["name"], "state": it["state"]}
                              for it in items]})


def main():
    _ensure_worker()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"DeepRead v{VERSION} 服务 http://127.0.0.1:{PORT}  "
          f"模型={deepread.MODEL}  OCR={deepread.OCR_MODEL}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
