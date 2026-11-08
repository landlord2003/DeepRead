# -*- coding: utf-8 -*-
"""
DeepRead 引擎 v0.3 —— 长文精读/摘要/要点提炼
独立 CLI：python deepread.py <file> --mode deep
也可被外部系统 import：from deepread import deepread_text
输入：txt / md / pdf / docx / 图片 / 纯文本字符串
模式：quick(快速要点) deep(深度精读) map(知识地图) feynman(大白话讲解) book(整书精读)
模型：本机 Ollama，默认 qwen3:14b，零外部 API

v0.3 新增
  1. 叙事文本（小说/故事）自动前置「故事梗概」（硬指标 >=1500 字，分批叙述→合成→不足即扩写）
  2. 金句 verbatim 校验：逐字/近似/转述三级判定，成稿前注入可用原句池，成稿后回标"转述"
  3. 扫描件 PDF / 图片 OCR 兜底（qwen3-vl:8b）
  4. book 整书模式：按章节分部 → 逐部深读 → 总编汇总
  5. 对抗式复核（critique）：三视角评审挑刺 + 主编回应
"""
import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
OLLAMA = os.environ.get("DEEPREAD_OLLAMA", "http://127.0.0.1:11434")
MODEL = os.environ.get("DEEPREAD_MODEL", "qwen3:14b")
CHUNK_SIZE = 1500       # 中文字符/块
CHUNK_OVERLAP = 120
INTERVAL = 5            # 每 N 个 chunk 做一次间隔汇总

# ---- 故事梗概（叙事文本）----
STORY_MIN_TEXT = int(os.environ.get("DEEPREAD_STORY_MIN_TEXT", "2500"))   # 触发梗概的最小字数
STORY_MIN_CHARS = int(os.environ.get("DEEPREAD_STORY_MIN", "600"))       # 梗概最小字数（老吴 2026-10-17 定：>500）
STORY_MAX_CHARS = int(os.environ.get("DEEPREAD_STORY_MAX", "1400"))      # 梗概最大字数（<1500，超了强制精简）
STORY_BATCH_CARDS = int(os.environ.get("DEEPREAD_STORY_BATCH", "8"))     # 无原文时每批卡片数
STORY_SOURCE_CHARS = int(os.environ.get("DEEPREAD_STORY_SOURCE_CHARS", "6000"))  # 每批原文上限

# ---- 扫描件 OCR ----
OCR_MODEL = os.environ.get("DEEPREAD_OCR_MODEL", "qwen3-vl:8b")
OCR_MAXPAGES = int(os.environ.get("DEEPREAD_OCR_MAXPAGES", "40"))
OCR_SCALE = float(os.environ.get("DEEPREAD_OCR_SCALE", "2"))
OCR_MIN_DENSITY = int(os.environ.get("DEEPREAD_OCR_DENSITY", "60"))      # 每页平均字数下限
OCR_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

# ---- book 整书模式 ----
BOOK_PART_CHARS = int(os.environ.get("DEEPREAD_BOOK_PART_CHARS", "25000"))
BOOK_MAX_PARTS = int(os.environ.get("DEEPREAD_BOOK_MAX_PARTS", "12"))
BOOK_PART_MIN = int(os.environ.get("DEEPREAD_BOOK_PART_MIN", "900"))     # 每部梗概最小字数

# 直连 opener：无视系统/注册表代理，保证 127.0.0.1 可达
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

MODES = ("quick", "deep", "map", "feynman", "distill")   # 模式对比可自选的基础模式
ALL_MODES = MODES + ("book", "notes", "persona")


# ================================================================ 工具

def text_len(s: str) -> int:
    """中文字数：去掉所有空白后的字符数。"""
    return len(re.sub(r"\s", "", s or ""))


_PUNC_RE = re.compile(
    r"[\s\u3000，。、；：！？…—－·「」『』“”‘’（）〈〉《》【】〔〕,.!?;:\"'`()\[\]{}<>|~^&*+=_\\/\\-]+")
_RHETORIC = ("像", "仿佛", "似的", "如同", "好比", "犹如", "一般", "样", "是…的")
_EMO = ("凄凉", "悲", "泪", "恨", "怕", "苦", "酸", "空", "静", "黑", "死", "冷", "笑", "苍凉")


def _norm(s: str) -> str:
    """归一化：去掉全部标点与空白，用于回原文比对。"""
    return _PUNC_RE.sub("", s or "")


def _ngram_cover(needle: str, hay: str, k: int = 8, step: int = 3) -> float:
    """needle 的 k-gram 在 hay 中的命中率（0~1），衡量"大致一致"。"""
    if len(needle) < k:
        return 0.0
    grams = [needle[i:i + k] for i in range(0, len(needle) - k + 1, step)]
    if not grams:
        return 0.0
    return sum(1 for g in grams if g in hay) / len(grams)


# ================================================================ readers

def read_any(path: str, ocr: bool = True, progress=None) -> str:
    """按扩展名读文件，返回纯文本。图片直接 OCR；PDF 疑似扫描件自动 OCR 兜底。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".txt", ".md", ".markdown"):
        for enc in ("utf-8", "gb18030", "utf-16"):
            try:
                return open(path, encoding=enc).read()
            except (UnicodeDecodeError, UnicodeError):
                continue
        raise ValueError(f"无法识别编码: {path}")
    if ext == ".pdf":
        return read_pdf(path, ocr=ocr, progress=progress)
    if ext == ".docx":
        return read_docx(path)
    if ext == ".epub":
        return read_epub(path)
    if ext in OCR_EXTS:
        return ocr_image(path, progress=progress)
    raise ValueError(f"暂不支持的格式: {ext}（支持 txt/md/pdf/docx/epub/图片）")


def read_pdf(path: str, ocr: bool = True, progress=None) -> str:
    """PDF 文本层提取；文本量明显偏低（图片型扫描件）时走视觉 OCR 兜底。"""
    from pypdf import PdfReader
    r = PdfReader(path)
    pages = []
    for p in r.pages:
        try:
            t = p.extract_text() or ""
        except Exception:                       # 个别页面损坏不应拖垮整篇
            t = ""
        if t.strip():
            pages.append(t.strip())
    text = "\n\n".join(pages)
    npages = len(r.pages)
    if ocr and _looks_scanned(text, npages):
        try:
            ocr_text = ocr_pdf(path, progress=progress)
            if text_len(ocr_text) > text_len(text):
                return ocr_text
        except Exception as e:                  # 诚实降级：说明失败原因，不静默
            return (text + f"\n\n[DeepRead 提示] 该 PDF 疑似扫描件，OCR 兜底失败：{e}")
    return text


def _looks_scanned(text: str, npages: int) -> bool:
    """每页平均可提取字数低于阈值 → 判定为图片型（扫描件）。"""
    if npages <= 0:
        return False
    return text_len(text) / npages < OCR_MIN_DENSITY


def _is_toc_doc(text: str) -> bool:
    """判断一个文档是否主要是目录/索引页（≥50% 行为"标题…页码"样式）。
    只认点引导符（.. 或 … 或连续全角空格），裸空格+数字不算，避免误伤正文。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 5:
        return False
    pat = re.compile(
        r"^.{1,80}?(\.{2,}|…{1,}|　{2,}|={3,})\s*[0-9]{1,4}$")
    toc = sum(1 for ln in lines if pat.match(ln))
    return toc / len(lines) >= 0.5


def read_epub(path: str) -> str:
    """epub = zip 包：container.xml → OPF → 按 spine 顺序抽取 XHTML 正文。
    纯标准库实现；DRM 加密书会因读不到明文 XHTML 而报错。"""
    import zipfile
    import posixpath
    import html as html_mod
    from urllib.parse import unquote
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(path) as z:
        container = z.read("META-INF/container.xml").decode("utf-8", "ignore")
        m = re.search(r'full-path="([^"]+)"', container)
        if not m:
            raise ValueError("epub 结构异常：container.xml 未指向 OPF")
        opf_path = posixpath.normpath(m.group(1))
        root = ET.fromstring(z.read(opf_path))
        base = posixpath.dirname(opf_path)
        manifest, spine = {}, []
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "item" and el.get("id"):
                manifest[el.get("id")] = (el.get("href") or "",
                                          el.get("media-type") or "")
            elif tag == "itemref" and el.get("idref"):
                spine.append(el.get("idref"))
        parts = []
        for idref in spine:
            href, mt = manifest.get(idref, ("", ""))
            if not href or ("html" not in mt.lower()
                            and not href.lower().endswith((".xhtml", ".html",
                                                           ".htm"))):
                continue                       # 跳过 css/ncx/图片等资源项
            name = posixpath.normpath(
                posixpath.join(base, unquote(href)))
            try:
                raw = z.read(name).decode("utf-8", "ignore")
            except KeyError:
                continue
            raw = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", "", raw)
            raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
            raw = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|blockquote)>", "\n", raw)
            text = html_mod.unescape(re.sub(r"<[^>]+>", "", raw))
            body = "\n".join(ln.strip() for ln in text.splitlines()
                             if ln.strip())
            if not body or _is_toc_doc(body):
                continue                       # 跳过纯目录/索引文档
            parts.append(body)
    out = "\n\n".join(parts)
    if text_len(out) < 50:
        raise ValueError("epub 正文过少——可能是 DRM 加密或纯图片电子书，暂不支持")
    return out


def read_docx(path: str) -> str:
    """docx = zip 包，直接解析 word/document.xml 提取段落，纯标准库。"""
    import zipfile
    import xml.etree.ElementTree as ET
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paras = []
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    for p in root.iter(f"{ns}p"):
        texts = [t.text or "" for t in p.iter(f"{ns}t")]
        line = "".join(texts).strip()
        if line:
            paras.append(line)
    return "\n".join(paras)


# ---------------------------------------------------------------- OCR

OCR_PAGE_PROMPT = """这是一页扫描文档的图片。请逐字转写页面上所有可见的文字，直接输出正文。

要求：
1. 保持原有的段落与换行结构；
2. 不要翻译、不要总结、不要解释、不要添加任何说明文字；
3. 确实无法辨认的单个字用 □ 代替；
4. 页眉、页脚、页码也照抄；
5. 只输出转写内容本身。"""


def _render_pdf_pages(path: str, limit: int):
    """渲染 PDF 前 limit 页为 PNG bytes 列表（需要 pypdfium2 + Pillow）。"""
    try:
        import pypdfium2 as pdfium
    except ImportError as e:
        raise RuntimeError(
            "扫描件 OCR 需要 pypdfium2 与 Pillow："
            "venv\\Scripts\\pip install pypdfium2 pillow") from e
    pdf = pdfium.PdfDocument(path)
    n = len(pdf)
    uselimit = min(n, limit)
    out = []
    for i in range(uselimit):
        pil = pdf[i].render(scale=OCR_SCALE).to_pil()
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        out.append(buf.getvalue())
    return out, n, uselimit


def ocr_pdf(path: str, progress=None) -> str:
    """图片型 PDF：逐页渲染 → qwen3-vl 转写。"""
    progress = progress or (lambda *a: None)
    blobs, n, uselimit = _render_pdf_pages(path, OCR_MAXPAGES)
    parts = []
    if n > uselimit:
        parts.append(f"[DeepRead 提示] 文档共 {n} 页，本次仅 OCR 前 {uselimit} 页"
                     f"（DEEPREAD_OCR_MAXPAGES 可调）")
    progress("ocr", 0, uselimit)
    for i, blob in enumerate(blobs):
        b64 = base64.b64encode(blob).decode("ascii")
        txt = chat(OCR_PAGE_PROMPT, model=OCR_MODEL, images=[b64],
                   num_ctx=4096, temperature=0.1)
        parts.append(f"<!-- 第{i + 1}页 -->\n{txt}")
        progress("ocr", i + 1, uselimit)
    return "\n\n".join(parts)


def ocr_image(path: str, progress=None) -> str:
    """单张图片直接 OCR。"""
    progress = progress or (lambda *a: None)
    progress("ocr", 0, 1)
    b64 = base64.b64encode(open(path, "rb").read()).decode("ascii")
    txt = chat(OCR_PAGE_PROMPT, model=OCR_MODEL, images=[b64],
               num_ctx=4096, temperature=0.1)
    progress("ocr", 1, 1)
    return txt


# ================================================================ llm

NUM_CTX = int(os.environ.get("DEEPREAD_NUM_CTX", "12288"))   # 成稿/合成默认上下文
MAX_CTX = int(os.environ.get("DEEPREAD_MAX_CTX", "16384"))   # 自动提升上限（12GB 卡上限，防换页）


def chat(prompt: str, system: str = "", json_mode: bool = False,
         model: str = MODEL, retries: int = 2, images=None,
         num_ctx: int = NUM_CTX, temperature: float = 0.3) -> str:
    # 上下文预算检查：中文 ≈1字/token，超出则报警并自动提升（上限 MAX_CTX），
    # 杜绝 Ollama 超限静默截断导致的质量损失。
    est = int((len(prompt) + len(system)) * 1.08) + 384
    if est > num_ctx - 256:
        if est + 1024 <= MAX_CTX:
            new_ctx = est + 1024
            print(f"[ctx] prompt 约 {est} tokens > num_ctx={num_ctx}，自动提升至 {new_ctx}")
            num_ctx = new_ctx
        else:
            print(f"[ctx][警告] prompt 约 {est} tokens 超过 MAX_CTX={MAX_CTX}，"
                  f"仍按 {num_ctx} 运行，输出可能被截断！")
    msg = {"role": "user", "content": prompt}
    if images:
        msg["images"] = images
    body = {
        "model": model,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
        "messages": ([{"role": "system", "content": system}] if system else []) + [msg],
    }
    if json_mode:
        body["format"] = "json"
    data = json.dumps(body).encode("utf-8")
    last_err = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(
                f"{OLLAMA}/api/chat", data=data,
                headers={"Content-Type": "application/json"})
            with _opener.open(req, timeout=900) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            content = out.get("message", {}).get("content", "")
            # qwen3 思考内容剥离（防旧版 Ollama 未分离 think 字段）
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
            return content.strip()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError) as e:
            last_err = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"Ollama 调用失败: {last_err}")


def extract_json(text: str):
    """宽松 JSON 提取：剥围栏后取首个 {...} 或 [...] 完整块解析。
    返回 dict 或 list（萃猫蒸馏聚类方案为数组）。"""
    text = re.sub(r"```(?:json)?", "", text).strip("`\n ")
    s, e = text.find("{"), text.rfind("}")
    sa, ea = text.find("["), text.rfind("]")
    # 数组块更靠前且存在 → 优先按数组解析（聚类方案输出 [ {...}, {...} ]）
    if 0 <= sa < s or s < 0:
        if sa >= 0 and ea > sa:
            try:
                return json.loads(text[sa:ea + 1])
            except json.JSONDecodeError:
                pass
    if s < 0 or e <= s:
        raise ValueError("LLM 未返回 JSON")
    return json.loads(text[s:e + 1])


# ================================================================ chunking

def split_chunks(text: str, size: int = CHUNK_SIZE,
                 overlap: int = CHUNK_OVERLAP):
    """先按段落聚块，超长段落再硬切，块间带重叠。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > size:                      # 超长段落硬切
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:size])
            p = p[size - overlap:]
        if len(cur) + len(p) + 1 <= size:
            cur = (cur + "\n" + p).strip()
        else:
            chunks.append(cur)
            cur = (cur[-overlap:] + "\n" + p).strip() if overlap else p
    if cur:
        chunks.append(cur)
    return chunks


# ================================================================ prompts

MAP_SYSTEM = (
    "你是严谨的中文精读助手。只依据给定文本片段提取信息，"
    "文本中没有的不要编造；找不到的字段给空数组。"
)

MAP_PROMPT = """从下面的文本片段中提取结构化信息，输出 JSON（不要输出其他内容）：
{{"points": [{{"point": "核心观点/事实/情节(50字内)", "ev": "原文依据(短引用,没有则null)", "conf": "原文支持|部分支持|推断"}}],
"entities": ["人物/机构/概念名"], "quotes": ["值得摘录的原文金句(必须逐字原句，不得改写/拼接；优先选有修辞、意象、情感张力的描写句或议论句，避免"是月亮光么？"这类无信息量的纯对话短句；最多3条)"],
"data": ["关键数字/数据/日期/地名"], "relations": ["A 关系 B 的三元组描述"]}}

{focus}文本片段：
{chunk}"""

_INTERVAL_TMPL = """以下是《{title}》第 {a}-{b} 段的要点卡片（JSON）：
{cards}

请写成一段 250 字内的连贯小结，保留数字、人物、事件与因果，不要编造。"""

FINAL_SPEC = {
"quick": {
    "out": """## 一句话总结
## 要点提炼（≤10条，按重要性排序）""",
    "task": "产出简洁摘要：一句话总结 + 要点列表。"},
"deep": {
    "out": """## 一句话总结
## 核心论点
（要求：纯编号列表，从 1 开始连续编号 1..N，一行一条，只写论点本身；行内严禁出现"置信度""原文依据"字样或引用子项）
## 论据与置信度
（要求：表格，三列——编号 | 原文依据（段号+短引用，一句话） | 置信度（原文支持/部分支持/推断）；编号与上方核心论点一一对应）
## 论证结构（论点如何推进：背景→论证→结论）
## 关键要点
## 原文金句（必须逐字照抄给定的「可用原句池」，不得改写；优先选有修辞/意象/哲理性的句子，
避免"仔细招了凉"这类无信息量的短对话；数量 6-10 条）
## 关键实体与数据""",
    "task": ("深度精读：'核心论点'一节只放论点（连续编号、一行一条、行内不带依据与置信度）；"
             "每条论点的原文依据与置信度统一放入其后的'论据与置信度'表格，编号对应；"
             "区分作者原意与推断，无证据的明确标注'推断'。")},
"map": {
    "out": """## 观点-证据-数据 知识地图
（表格：观点 | 证据/数据 | 置信度）
## 关系网络（实体 A → 关系 → 实体 B 列表）
## Mermaid 思维导图
（```mermaid mindmap 语法）""",
    "task": "把内容组织成'观点-证据-数据-关系'四层知识地图，输出表格+关系列表+mermaid。"},
"feynman": {
    "out": """## 一句话：这东西到底讲什么
## 大白话讲解（像讲给聪明的外行听，用类比）
## 术语翻译表（术语 | 大白话解释）
## 值得追问的三个问题""",
    "task": "费曼式讲解：全部翻译成大白话，术语必须有类比解释，禁止照搬原文句式。"},
}

# 叙事文本专属 deep 成稿结构见 TYPE_SPECS["novel"]（v0.4 类型档案接管）

FINAL_PROMPT = """你是中文精读主编。基于《{title}》的下列素材完成：
{task}
{quote_hint}{facts}{materials}

全文约 {nchar} 字。输出 Markdown，结构如下（标题原样使用）：
{out}

要求：忠实原文素材；素材里没有的结论必须标注"推断"；"原文金句"必须是给出的可用原句原样照抄；
优先使用「预筛原文段落」与「故事梗概」中的细节——它们直接来自原文，比分段小结更可靠。"""

# ---------------------------------------------------------------- 故事梗概

STORY_NARRATIVE_SYSTEM = (
    "你是中文小说与叙事文本的情节概述专家。只叙述文本中实际发生的事，"
    "不分析、不评价、不编造，人物姓名与关系必须准确。"
)

STORY_BATCH_PROMPT = """以下是《{title}》第 {a}-{b} 部分的情节卡片（JSON）。请据此写出这一部分的情节叙述。

要求：
1. 按时间顺序叙述，保留人物姓名、人物关系、关键事件、前因后果；
2. 篇幅 400-600 字；
3. 只叙述"发生了什么"，不要分析、不要评价、不要提炼主题或论点；
4. 卡片中没有的信息不要编造，不确定的细节不要写；
5. 人物身份、亲属关系、事件先后必须严格依据卡片；卡片没写明的（如某人是妻是妾、如何进入这个家庭）宁可略过，不要推断；
6. 直接输出情节叙述正文，不要标题、不要前言。

情节卡片：
{cards}"""

STORY_SOURCE_PROMPT = """以下是《{title}》的第 {a} 部分原文（这是全书连续内容中的一段）。请写出这一部分的情节叙述。
{facts}
要求：
1. 按时间顺序叙述，500-700 字；
2. 保留人物姓名、人物关系、关键事件与**前因后果**：关键转折处必须写明"谁因什么动机/欲望/恐惧，做了什么，导致什么后果"（例如"为了护住钱财，她逼女儿X，导致Y"）；
3. **人物身份以「人物事实清单」与原文为准，两者都没写明的不要推断**——尤其注意排行称呼（大爷/二爷/三爷）对应的人物必须与清单一致；
4. 只叙述"发生了什么"，不要分析、不要评价、不要提炼主题或论点；
5. **严禁添加原文没有的内容**：不要编造场景、动作、对话、心理描写或人物来历；
   **禁止写出原文中不存在的直接引语**——不要用引号构造任何人物说的话，转述即可；
   原文有的可以概括，原文没有的一律不写；
6. 直接输出正文，不要标题、不要前言。

原文：
{source}"""

STORY_MERGE_PROMPT = """以下是《{title}》各部分的情节叙述（已按顺序排列）。请整合成一篇完整连贯的**故事梗概**。

梗概的本质是**归纳大意，不是复述细节**。要求：

1. 篇幅：**{min_chars}-{max_chars} 字，全篇 4-5 个自然段，每段不超过 300 字**。
   这是程序强校验的硬约束，超字数会被自动打回重写。宁可整段丢弃次要情节线，不要什么都保留；
2. **写轮廓，不写场面**：全篇按故事的自然阶段分成 3-5 段，每段只保留推动主线的关键事件
   （谁、做了什么、结果如何），砍掉支线对话、单个场景的琐碎过程、无关紧要的配角互动；
3. **严禁用"起初/随后/此后/最终/接着/后来"这类词开头**，段落之间用内容本身的推进自然衔接；
4. 开篇先用一两句交代：这是什么故事、主要人物是谁、核心处境是什么；
5. **结尾必须有一段核心思想归纳**（2-4 句）：主人公的命运弧线是什么（例如"从X变成Y"）、
   这个故事借人物命运说明了什么。归纳必须从上述情节自然得出，**不得引入情节里没有的抽象概念**；
6. **因果链要清楚**：关键转折要写明动机与后果，但用叙述语言，不要"因为……所以……"的机械句式；
7. **只使用下方素材中出现的内容**：素材没写的人物来历、亲属关系、场景、对话、心理描写，一律不要添加；
   素材没写明的身份关系不要断言；
   **禁止构造素材中不存在的具体细节**：不要编造直接引语（任何引号里的话必须是素材中的原话）、
   不要编造具体数字/物件/动作（如"掐出五道血痕""砸碎牌位"这类素材中没有的场面）；
   拿不准的细节宁可不写——**宁可概括留白，不可虚构补位**；
   **你的输出会被程序逐句回素材抽查**：素材中没有文字依据的句子会被自动列出并要求删除；
8. 直接输出梗概正文，不要标题、不要分点、不要"第一部分"这类机械小标题。

各部分情节叙述如下：
{materials}"""

STORY_PARTIAL_PROMPT = """以下是《{title}》若干部分的情节叙述，请合并成一段更紧凑连贯的情节叙述（{min_chars} 字左右），
保留人物、事件与因果，不要编造，不要分析。直接输出正文。

{materials}"""

STORY_EXPAND_PROMPT = """下面是一篇故事梗概，目前 {cur} 字，尚未达到 {min_chars} 字的篇幅要求。

请扩写（补充情节细节：事件的具体过程、人物动作与对话要点、事件之间的过渡与因果链条、
人物关系的变化），使篇幅达到要求。

**必须遵守的底线**：
- 只能写已有素材支持的内容，**不得虚构**新的事件、人物、亲属关系、对话或人物来历；
- 如果现有素材确实已经写尽、无法在不编造的前提下继续加长，就在内容自然结束处直接输出完整版，
  不要为了凑字数硬加内容。

要求：直接输出扩写后的**完整梗概**（不要只输出新增段落），不少于 {min_chars} 字。

现有梗概：
{cur_text}"""

STORY_CONDENSE_PROMPT = """下面是一篇故事梗概，目前 {cur} 字，超过了 {max_chars} 字的上限。
**这是程序强校验的硬约束，输出超过 {max_chars} 字即为不合格**。

请大幅精简：保留故事轮廓（阶段推进、关键转折、人物命运、结尾的核心思想归纳），
砍掉支线细节、单个场面的琐碎描写、次要人物互动。全篇压成 **4-5 个自然段，每段不超过 300 字**。

**必须遵守的底线**：
- 不得虚构任何情节、人物、亲属关系或心理描写；
- 不得丢失主干事件与核心思想归纳段；
- 不要用"起初/随后/此后/最终"这类词开头。

要求：直接输出精简后的**完整梗概**，不超过 {max_chars} 字。

现有梗概：
{cur_text}"""

_THEME_MARKERS = ("这个故事", "揭示", "展现", "印证", "说明", "反映了", "表达",
                  "命运", "人性", "悲剧", "权力", "金钱", "欲望", "枷锁", "异化",
                  "必然", "讽刺", "控诉")

THEME_CLASSIFY_PROMPT = """判断下面这段文字属于哪一类：
A = 情节叙述（讲发生了什么事）
B = 主题/思想归纳（分析人物命运弧线与作品要说明的道理，基本不讲具体事件）
只回答 A 或 B。

{text}"""

THEME_PARA_PROMPT = """下面是一篇《{title}》的故事梗概，缺少结尾的**核心思想归纳段**。
请补写这一段：2-3 句话，从梗概已有情节自然归纳——主人公的命运弧线是什么
（例如"从X变成Y"）、这个故事借人物命运说明了什么。
**不得引入梗概里没有的抽象概念，不得复述具体情节**。只输出这一段，不要其他内容。

梗概：
{cur_text}"""


def _last_para_is_theme(last: str, model: str) -> bool:
    """末段是否已是主题归纳段（一次轻量 LLM 二分类）。"""
    if text_len(last) < 30 or text_len(last) > 500:
        return False
    try:
        ans = chat(THEME_CLASSIFY_PROMPT.format(text=last[:800]),
                   model=model, num_ctx=4096, temperature=0.0).strip()
    except Exception:            # 分类失败宁可多补一段也不要漏
        return False
    return ans.startswith("B")


def _ensure_theme_paragraph(title: str, text: str, model: str) -> str:
    """梗概末尾必须有核心思想归纳段：LLM 分类判定末段属性，是情节则补写归纳段。"""
    text = text.rstrip()
    last = text.split("\n\n")[-1] if text else ""
    if _last_para_is_theme(last, model):
        return text                                   # 末段已是归纳段
    para = chat(THEME_PARA_PROMPT.format(
        title=title, cur_text=text[:9000]),
        system=STORY_NARRATIVE_SYSTEM, model=model, num_ctx=8192).strip()
    para = re.sub(r"^#+\s.*$", "", para, flags=re.M).strip()
    if text_len(para) < 30 or text_len(para) > 350:
        return text
    if text_len(text) + text_len(para) > STORY_MAX_CHARS + 120:
        return text
    return text + "\n\n" + para

STORY_EXTRACT_PROMPT = """下面是一篇《{title}》的故事梗概，太长了（{cur} 字）。请用**摘录重组**的方式
压缩成 {min_chars}-{max_chars} 字的版本：

1. 从原文梗概中**挑选**最重要的句子（主干事件、关键转折、结局、核心思想归纳），
   只允许：整句摘录 / 最小限度衔接性改写（改时态、换连接词），**不得写出原梗概中没有的事件与细节**；
2. 全篇 4-5 个自然段，每段不超过 300 字，按时间顺序排列；
3. 末段保留核心思想归纳；
4. 不要用"起初/随后/此后/最终"开头；不要总标题。

原梗概：
{cur_text}"""

# ---------------------------------------------------------------- book 总编

BOOK_OUTLINE_PROMPT = """你是中文书籍主编。下面是《{title}》各部分的情节梗概。请输出三个 Markdown 小节：

## 章节脉络
（表格：部分 | 主要事件与推进 | 关键人物）

## 人物谱系
（表格：人物 | 身份与关系 | 命运走向）

## 主题与母题
（3-6 条，每条一句话主题 + 一句话支撑线索）

要求：只依据给定梗概，不编造；梗概中没有的信息不要写。

各部分梗概如下：
{materials}"""

# 非虚构类整书蒸馏总编（对标"萃猫"分析卡与书籍蒸馏四层结构：
# 核心问题→思维框架→结论→使用边界）
BOOK_DISTILL_PROMPT = """你是书籍蒸馏主编。下面是非虚构类《{title}》各部分的梗概与要点。输出五个 Markdown 小节：

## 章节脉络
（表格：部分 | 核心内容 | 关键论点/事实）

## 核心框架
（作者的全书思维框架/论证结构：3-6 条编号列表，每条一个组成部分 + 一句话说明。目标：把整本书压缩成一个可复用的思维模型）

## 关键实体与术语
（表格：称谓 | 一句话说明。收全书反复出现的核心人物/机构/概念/术语，全文统一叫法）

## 全书结论与主张
（≤5 条：作者最终要读者接受什么/做什么）

## 使用边界
（这套框架/知识适合用来分析什么、不适合什么、有何局限；原文没明说的标"推断"）

要求：只依据给定梗概，不编造；梗概中没有的信息不要写。

各部分梗概如下：
{materials}"""

# 萃猫 R1（Analyze）：全书级分析卡，一次生成、注入所有分部处理，保证称谓/术语全书一致
BOOK_BRIEF_PROMPT = """你是书籍分析编辑。根据《{title}》的章节名与首中尾采样片段，输出全书分析卡（Markdown，四节，共 400-700 字）：

## 全书定位
（这是一本什么书：主题、作者立场、面向谁；2-3 句）

## 术语与称谓表
（表格：统一称谓 | 说明。收 8-15 个全书反复出现的核心人名/地名/机构/术语/概念，给出全文统一的叫法——后续所有章节精读都必须使用此表称谓，不得异写）

## 风格与蒸馏侧重
（是思想型还是操作型文本？蒸馏时应优先保留：思维框架/执行流程/关键案例/数据？2-3 句）

## 阅读提示
（1-2 条：读这本书最容易产生的误解或需要注意的点）

只依据给定材料，不编造；采样不足以判断的就写"采样未见"。

章节名列表：
{chapters}

采样片段：
{samples}"""

# ---------------------------------------------------------------- 对抗式复核

CRITICS = (
    ("证据审查员",
     "你是一位苛刻的证据审查员。你的任务是找出报告中**缺乏原文支撑**的论断："
     "哪些结论在给定材料中找不到依据、哪些把'推断'写成了'事实'、哪些数据/引文可能有误。"
     "只指出问题，不要夸奖，不要重写报告。"),
    ("逻辑审查员",
     "你是一位逻辑审查员。你的任务是找出报告中**推理与结构上的问题**："
     "论点之间是否跳跃、因果是否倒置、是否存在以偏概全、是否遗漏了材料中的关键反例或矛盾之处。"
     "只指出问题，不要夸奖，不要重写报告。"),
    ("反方辩护人",
     "你是反方辩护人。请站在与报告结论相反的立场，指出报告**过度解读或视角单一**之处："
     "同样的材料能否得出不同结论、作者原意是否被曲解、是否存在被忽略的其他解释。"
     "只指出问题，不要夸奖，不要重写报告。"),
    ("深度审查员",
     "你是深度审查员。你的任务是逼问这份报告**是否挖到了更深一层的读解**："
     "总结是否只停在字面复述（情节/观点罗列）？有没有更高维的框架被漏掉——"
     "如主体命运的转变轨迹（受害者→加害者？）、统摄全文的核心意象、"
     "表层题材与深层主旨的结构关系？若报告确实浅了，请直接给出你认为更深一层的读解"
     "（必须能从材料中找到支撑），供主编修订参考。不要夸奖，不要重写报告。"),
)

CRITIC_PROMPT = """{persona}

请审查下面这份对《{title}》的精读报告（全文约 {nchar} 字）。

报告的中间材料（分段小结，供你核对报告是否忠实）：
{materials}

报告全文：
{report}

输出格式（Markdown，最多 6 条，每条一句话，必须具体、可核对，不要空泛）：
- 【问题】… ｜【依据/反例】… ｜【建议】…"""

REPLY_PROMPT = """你是本次精读报告的主编。各位评审对报告提出了意见，请你逐条判断并回应。

原标题报告：《{title}》

评审意见：
{reviews}

要求：
1. 对每条意见判断是否成立（成立 / 部分成立 / 不成立），并给出理由，理由必须基于分段小结中的材料；
2. 明确区分"报告中确实存在的问题"与"评审的误解"；
3. 对成立的意见，给出具体的修订说明（应该怎么改）；
4. 输出 Markdown，结构如下（标题原样使用）：

## 三视角评审意见
（按评审人分小节列出，保留其原始意见；四位评审意见都在内）
## 主编回应与修订
（逐条回应，表格：意见编号 | 是否成立 | 理由 | 修订动作）
## 本报告的已知局限
（3-5 条，基于评审意见中成立的部分，诚实列出）"""


# ================================================================ 叙事检测

def narrative_signals(text: str) -> dict:
    """统计叙事文本特征信号。"""
    n = max(len(text), 1)
    quotes = (text.count("“") + text.count("”") + text.count("「")
              + text.count("」") + text.count("『") + text.count("』"))
    dialog = sum(text.count(w) for w in
                 ("说道", "笑道", "问道", "答道", "骂道", "叹道", "心想", "道：", "说："))
    temporal = sum(text.count(w) for w in
                   ("后来", "次日", "第二天", "几年后", "那年", "这时", "当时",
                    "从此", "此后", "过后", "一会", "半晌"))
    persons = sum(text.count(w) for w in ("他", "她", "他们", "她们"))
    chapters = len(re.findall(r"第[零一二三四五六七八九十百两0-9]{1,6}[章回节卷部篇]", text))
    return {"len": n, "quotes": quotes, "quote_density": quotes / n,
            "dialog": dialog, "temporal": temporal, "persons": persons,
            "person_density": persons / n, "chapters": chapters}


def is_narrative(text: str) -> bool:
    """启发式判定是否为叙事文本（小说/故事）。阈值保守，宁可漏判不误判。"""
    n = text_len(text)
    if n < STORY_MIN_TEXT:
        return False
    s = narrative_signals(text)
    score = 0
    if s["quote_density"] > 0.004:
        score += 2
    if s["dialog"] >= 5:
        score += 2
    elif s["dialog"] >= 2:
        score += 1
    if s["temporal"] >= 8:
        score += 1
    if s["person_density"] > 0.012:
        score += 1
    if s["chapters"] >= 2:
        score += 1
    return score >= 3


def recommend_mode(text: str):
    """按文本特征推荐精读模式，返回 (mode, 理由)。纯启发式，零成本。"""
    n = text_len(text)
    if n < 800:
        return "quick", "文本较短，快速要点性价比最高"
    narr = is_narrative(text)
    if n > 60000 and (narr or len(re.findall(r"第[零一二三四五六七八九十百两0-9]{1,6}[章回]", text)) >= 5):
        return "book", "长篇书籍，整书模式按章节分部深读后总编汇总"
    if narr:
        return "map", "叙事/对话密集的文本，知识地图更利于呈现人物与事件关系"
    if n < 3000:
        return "quick", "篇幅不长，快速要点足够"
    return "deep", "论述性长文，深度精读可提取论点、论证结构与证据"


# ================================================================ 文本类型识别

TYPE_LABELS = {
    "novel": "小说/故事", "prose": "散文/随笔", "comment": "时评/评论",
    "history": "历史/人文", "academic": "学术/研究", "tech": "技术/教程",
    "report": "报告/公文", "speech": "演讲/讲话", "general": "通用",
}
CLASSIFY_MODEL = os.environ.get("DEEPREAD_CLASSIFY_MODEL", "qwen2.5:7b")

# 每类型一组特征（关键词/正则，按千字归一）。选词原则：高区分度，
# 避免跨文体通用词（如"我/笑/哭"——小说与书评都会大量出现）。
_TYPE_FEATURES = {
    "novel": ["说道", "笑道", "问道", "骂道", "答道", "叹道", "心想", "妈妈",
              "太太", "少爷", "奶奶", "老爷", "丫头", "媳妇", "大哥", "嫂子", "姨太太"],
    "prose": ["岁月", "生命", "灵魂", "怀念", "记忆", "故乡", "青春", "心底",
              "时光", "温暖", "孤独"],
    "comment": ["应该", "必须", "显然", "然而", "问题在于", "我认为", "遗憾的是",
                "政策", "制度", "社会", "经济", "改革", "舆论", "本质上", "恰恰",
                # 评论元话语（强信号：谈论"作品/作者"说明这是评论文而非小说本体）
                "小说", "作品", "作者", "读者", "创作", "笔下", "这一形象"],
    "history": ["公元", "世纪", "王朝", "皇帝", "清朝", "民国", "明朝", "宋朝",
                "唐朝", "汉代", "秦始皇", "年间", "起义", "条约", "史载",
                "出土", "遗址", "末年", "建都"],
    "academic": ["摘要", "关键词", "参考文献", "假设", "变量", "模型", "方法论",
                 "实证", "样本", "显著性", "文献", "引言", "本研究", "定义如下", "et al"],
    "tech": ["安装", "配置", "命令", "运行", "git", "docker", "pip", "npm", "python",
             "localhost", "终端", "报错", "依赖", "部署", "API", "函数", "版本"],
    "report": ["同比增长", "环比", "占比", "亿元", "万元", "覆盖率", "完成率",
               "指标", "考核", "传达", "贯彻落实", "总结如下", "下一步"],
    # 演讲/讲话（含语音转写稿）：高精度称呼/号召/口头标记。
    # "我们"等高频泛词不收——时评也会大量出现，靠高精度标记区分
    "speech": ["女士们", "先生们", "朋友们", "各位", "同胞", "谢谢大家",
               "让我们一起", "站在这里", "此刻", "演讲", "致辞", "听众",
               "聚集", "开始之前", "今天我", "今天我们"],
}
_TYPE_REGEX = {
    "novel": r"第[零一二三四五六七八九十百两0-9]{1,6}[章回节卷部篇]",
    "academic": r"\[\d+\]|（\d{4}）",
    "report": r"\d+(\.\d+)?%",
    "history": r"(?:1[0-9]|20)[0-9]{0,2}年代|前[一二三]\d{2}年",
    # 演讲称呼语："女士们，先生们""朋友们，""同胞们""各位……"（带标点防误中正文引用）
    "speech": r"(?:女士们|先生们|同胞们|各位(?:朋友|同胞|来宾|同学|同事)|朋友们)[，。！？、]",
}


def _type_scores(text: str) -> dict:
    n = max(text_len(text), 1) / 1000.0          # 千字归一
    sc = {}
    for t, words in _TYPE_FEATURES.items():
        s = sum(text.count(w) for w in words) / n
        if t in _TYPE_REGEX:
            s += len(re.findall(_TYPE_REGEX[t], text)) / n * 2
        sc[t] = s
    return sc


def classify_text(text: str, model: str = "", progress=None):
    """文本类型三级识别，返回 (type, confidence, method)。

    1. 规则启发式（<1ms）；2. 低置信度 → 轻量 LLM 采样（首中尾各400字）；
    3. 失败回退规则结果。"""
    progress = progress or (lambda *a: None)
    sample = text[:60000]
    sc = _type_scores(sample)
    ranked = sorted(sc.items(), key=lambda kv: -kv[1])
    top, top_s = ranked[0]
    second_s = ranked[1][1] if len(ranked) > 1 else 0.0
    total = sum(sc.values())
    conf = top_s / total if total > 1e-6 else 0.0
    # 规则结果只有在"绝对分数高且显著领先第二名"时才直接采信，
    # 否则一律走 LLM 校正（规则特征易被引文/杂谈污染）
    if top_s >= 3.0 and top_s >= second_s * 1.6 and conf >= 0.42:
        return top, round(min(conf, 0.99), 2), "rules"
    # 低置信 → LLM 采样
    model = model or CLASSIFY_MODEL
    L = len(sample)
    parts = [sample[:400], sample[L // 2:L // 2 + 400] if L > 800 else "",
             sample[-400:] if L > 1200 else ""]
    label_list = "、".join(f"{v}({k})" for k, v in TYPE_LABELS.items()
                           if k != "general")
    p = ("判断下面文字片段属于哪种文体类型，只输出括号里的英文代号，不要输出其他内容。\n"
         f"类型清单：{label_list}\n"
         "判别要点：第一人称面向听众的演讲/致辞（含语音转写稿）选 speech——"
         "即使演讲内容大篇幅讲历史，也应选 speech 而非 history；"
         "history 是写给读者看的历史文章或史书。\n\n"
         f"片段1：\n{parts[0]}\n\n片段2：\n{parts[1]}"
         f"\n\n片段3：\n{parts[2]}\n\n只输出一个代号：")
    try:
        progress("classify", 1, 1)
        raw = chat(p, model=model, num_ctx=4096, temperature=0.1)
        m = re.search(r"(novel|prose|comment|history|academic|tech|report|speech|general)",
                      raw.lower())
        if m:
            return m.group(1), 0.75, "llm"
    except Exception as e:                        # 分类失败不拖垮主流程
        print(f"[classify] LLM 分类失败，回退规则: {e}")
    return (top if top_s >= 0.5 else "general"), \
        round(min(conf, 0.6), 2), "rules-fallback"


# ================================================================ 类型预筛器

def _sieve_paragraphs(text: str, keywords, k: int = 12,
                      lo: int = 40, hi: int = 620) -> list:
    """程序侧预筛：按段落扫描，命中关键词多的段落排前，返回 top-k 原文段落。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if text_len(p) >= lo]
    scored = []
    for p in paras:
        if text_len(p) > hi:
            p = p[:hi]
        s = sum(1 for w in keywords if w in p)
        if s > 0:
            scored.append((s, p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:k]]


_SIEVE = {
    "novel": (["像", "仿佛", "月光", "月亮", "镜子", "金色", "金的", "红", "白",
               "影子", "烟", "雾", "眼", "脸", "手", "冷", "泪"], 12),
    "prose": (["我", "岁月", "时光", "记忆", "生命", "怀念", "意象", "故乡", "风"], 10),
    "comment": (["应该", "必须", "然而", "但是", "问题", "事实", "数据", "显然",
                 "本质", "根本", "关键"], 10),
    "history": (["公元", "世纪", "年间", "条约", "王朝", "皇帝", "战", "史", "统一",
                 "建立", "灭亡"], 10),
    "academic": (["假设", "数据", "结果", "表明", "显著", "方法", "样本", "结论",
                  "证明", "因此"], 10),
    "tech": (["安装", "配置", "运行", "命令", "步骤", "注意", "报错", "依赖", "默认"], 10),
    "report": (["同比增长", "占比", "完成", "目标", "计划", "重点", "推进", "指标"], 10),
    "speech": (["我们", "必须", "选择", "伟大", "自由", "文明", "历史", "未来",
                "国家", "骄傲", "英雄", "世界"], 12),
    "general": (["关键", "核心", "重要", "因此", "然而", "总结"], 8),
}


def sieve_for(dtype: str, text: str) -> list:
    kw, k = _SIEVE.get(dtype, _SIEVE["general"])
    return _sieve_paragraphs(text, kw, k)


# ================================================================ 类型档案

# 各类型 deep 成稿结构（非 deep 模式沿用 FINAL_SPEC）
TYPE_SPECS = {
"novel": {
    "out": """## 一句话总结
（必须回答三点：①主人公/主体的处境转变轨迹（如"从X到Y"）；②统摄全文的核心意象（如有）；③表层写什么、深层写什么）
## 人物表
（表格：人物 | 身份与关系 | 性格与命运走向。只收主要人物（4-8人）。
**铁律：'身份与关系'一列必须逐字使用「人物事实清单」中的关系描述措辞**
（如清单写"姜季泽是七巧的小叔子"，就写"七巧的小叔子"，**不得重新组织措辞**、
不得合并多条、不得推断补充）——排行称呼（大爷/二爷/三爷）尤其要与清单一致；
清单未写明的人物写"原文未明说"。性格与命运走向用一两句话概括其弧线）
## 主题分层
（3-6 条，编号列表。每条格式："**小标题**：2-3 句分析（结合情节与人物命运）"。
**各条必须从不同角度切入**（如：欲望/金钱/亲情/时代与制度），严禁同义反复——
如果两条表达的是同一层意思，合并成一条）
## 情节关键转折
（4-7 条，每条格式：一句话事件 + 一句话说明该事件如何改变人物命运）
## 艺术手法与意象
（分析文本中反复出现的意象、修辞、结构安排（如首尾呼应的意象、时间跳跃、
色彩与器物的隐喻），每条给出原文短引用作为例证；
只写文本中真实存在且能引用原文的，没有把握的不要写）
## 原文金句（必须逐字照抄给定的「可用原句池」，不得改写；数量 6-10 条）""",
    "task": ("叙事文本深度精读：以人物表、主题分层、情节关键转折、艺术手法与意象为主线组织报告；"
             "人物身份与关系严格依据人物事实清单；主题各条角度不得重复；"
             "艺术手法必须有原文引用支撑，宁可少写不可编造。"
             "**'一句话总结'必须点出人物的处境转变轨迹与统摄全文的核心意象**，"
             "不要停在情节复述层面。"),
},
"comment": {
    "out": """## 一句话总结
## 立场与基调
（作者站在什么立场、面向谁说话、情绪基调如何）
## 论证链条
（按"论点→论据→推进→结论"列出论证主干，3-6 条；每条注明论据类型：事实/数据/类比/权威/情绪）
## 事实主张与可核性
（表格：事实主张 | 可核性（可直接核实/需交叉验证/观点性表述） | 备注）
## 修辞与话术
（作者用了什么说服技巧：诉诸情感/诉诸权威/二元对立/滑坡论证等，各给原文例证）
## 原文金句（必须逐字照抄给定的「可用原句池」；数量 4-8 条）""",
    "task": ("评论类深度精读：拆解作者的立场、论证链条、事实主张与说服技巧；"
             "把'事实'与'观点'严格分开标注；论证链条每环都要有原文依据。"),
},
"history": {
    "out": """## 一句话总结
## 时间线大事记
（表格：时间 | 事件 | 意义/影响；按时间排序，时间不明用"约"）
## 关键人物与势力
（表格：人物/势力 | 角色 | 关键行为与结局）
## 因果分析
（3-5 条，每条：背景/原因 → 事件 → 后果；注明哪些是原文明说的因果、哪些是合理推断）
## 史料与可信度
（文中引用了什么史料/数据/传说，可信度如何，有无明显存疑之处）
## 原文金句（必须逐字照抄给定的「可用原句池」；数量 4-8 条）""",
    "task": ("历史类深度精读：以时间线、人物势力、因果链为主线；"
             "严格区分原文明说的因果与你自己的推断；时间线必须按序且可对表。"),
},
"prose": {
    "out": """## 一句话总结
## 文眼与主题
（全文围绕哪句话/哪个意象展开，核心情绪与思想是什么）
## 情感脉络
（3-5 段：情感如何起承转合，每段给原文锚点）
## 意象与语言特色
（反复出现的意象、标志性句式、语言风格，各给原文例证）
## 原文金句（必须逐字照抄给定的「可用原句池」；数量 6-10 条）""",
    "task": "散文类深度精读：抓住文眼、情感脉络与语言特色；分析必须有原文锚点。",
},
"academic": {
    "out": """## 一句话总结
## 研究问题与假设
## 方法与数据
（研究设计、样本、变量、工具）
## 核心发现
（3-6 条，标注效应量/显著性等关键数字）
## 结论与局限
（作者自己的结论；作者承认的局限 + 你看出的其他局限，标注"推断"）
## 原文金句（必须逐字照抄给定的「可用原句池」；数量 4-8 条）""",
    "task": "学术类深度精读：按'问题-方法-数据-结论-局限'五段拆解，关键数字必须保留。",
},
"tech": {
    "out": """## 一句话总结
## 这是什么 / 解决什么问题
## 前置条件
## 步骤清单
（按序号列出操作步骤，命令原样保留）
## 配置要点与常见坑
（表格：配置/坑 | 说明 | 原文出处提示）
## 原文金句（必须逐字照抄给定的「可用原句池」；数量 3-6 条）""",
    "task": "技术类深度精读：以可照做的步骤清单为核心，命令/配置原样保留不意译。",
},
"report": {
    "out": """## 一句话总结
## 核心结论（≤3 条）
## 关键数据
（表格：指标 | 数值 | 同比/环比/含义）
## 分项要点
## 建议与风险
## 原文金句（必须逐字照抄给定的「可用原句池」；数量 3-6 条）""",
    "task": "报告类深度精读：结论先行、数据成表；所有关键数字不得改写。",
},
"speech": {
    "out": """## 一句话总结
## 演讲者与场合
（谁在讲、在哪讲、对谁讲、为什么讲——只写原文明示的信息，未明说就写"原文未明说"，严禁推断编造人名/时间/职务）
## 内容摘要
（300-500 字连贯段落，按演讲的实际推进顺序完整概括全部主干：开场立意→历史叙事→现实判断→政策主张→行动号召→结尾升华。**必须写到演讲的结尾部分**，不得只写前半段）
## 核心要点
（10-16 条，编号列表，**按演讲推进顺序排列**、合并去重；每条是一个完整的论断句，包含演讲者提出的具体主张/计划/领域名。覆盖必须完整：从开场到结尾每个部分至少有对应条目）
## 事实主张核查
（表格：演讲者陈述的"事实" | 类型（史实/数据/归功/评价） | 核查提示。演讲者的历史叙事与数据都是"他的主张"，不是已核实事实——用此表把它显式分离出来）
## 说服与修辞
（3-5 条：历史类比/诉诸自豪与危机/排比重复/英雄叙事等，各给一处原文短例证）
## 原文金句（必须逐字照抄给定的「可用原句池」；数量 4-8 条）""",
    "task": ("演讲/讲话类深度精读（含语音转写稿）：以演讲的实际推进顺序为主线，"
             "摘要和要点必须覆盖从开场到结尾的全部主干，**严禁只写前半段**；"
             "演讲者陈述的史实一律放进'事实主张核查'表标注为'他的主张'，"
             "不得当作已核实事实写进摘要；时间/人名/职务只写原文明说的，宁缺勿编。"),
},
}

TYPE_PROFILES = {
    "novel": {"label": TYPE_LABELS["novel"],
              "stageb": "（本文为叙事文本：代词尽量解析为具体人名，如\"她\"→七巧；"
                        "对话在 relations 中标注说话人与听者；亲属关系写明确，如\"X是Y的小叔子\"）",
              "spec": TYPE_SPECS["novel"]},
    "prose": {"label": TYPE_LABELS["prose"],
              "stageb": "（本文为散文随笔：侧重意象、情感词与标志性句式的提取）",
              "spec": TYPE_SPECS["prose"]},
    "comment": {"label": TYPE_LABELS["comment"],
                "stageb": "（本文为评论/时评：侧重区分作者立场、论点、论据与事实主张，"
                          "data 字段务必收录可核查的事实性数字与引语）",
                "spec": TYPE_SPECS["comment"]},
    "history": {"label": TYPE_LABELS["history"],
                "stageb": "（本文为历史/人文：data 字段务必收录年代、地名、条约、事件名，"
                          "points 注明事件先后顺序）",
                "spec": TYPE_SPECS["history"]},
    "academic": {"label": TYPE_LABELS["academic"],
                 "stageb": "（本文为学术/研究：data 收录样本量、指标、显著性等关键数字；"
                           "points 区分假设/方法/发现）",
                 "spec": TYPE_SPECS["academic"]},
    "tech": {"label": TYPE_LABELS["tech"],
             "stageb": "（本文为技术/教程：quotes 收录关键命令与配置行原文，points 按步骤顺序）",
             "spec": TYPE_SPECS["tech"]},
    "report": {"label": TYPE_LABELS["report"],
               "stageb": "（本文为报告/公文：data 收录全部关键数字与指标，points 区分结论/措施/要求）",
               "spec": TYPE_SPECS["report"]},
    "speech": {"label": TYPE_LABELS["speech"],
               "stageb": "（本文为演讲/讲话，可能是语音转写稿：points 按演讲推进顺序逐段提取、"
                         "覆盖到结尾部分，每段至少 1 条要点；data 收录演讲者、场合、"
                         "提及的人物/国家/计划名/数字；不要把演讲者的历史叙述当作已核实事实）",
               "spec": TYPE_SPECS["speech"]},
    "general": {"label": TYPE_LABELS["general"], "stageb": "", "spec": None},
}




# ================================================================ 中文成稿质检
# 移植自 cuimao-translator（萃猫翻译）"欧化中文六重检测"思想，
# 程序正则实现，零 LLM 成本，对成稿报告做病文体检。

_PROSE_NOMINAL = ("的实施", "的进行", "的开展", "的推进", "的运用",
                  "的探索", "的落实", "的研究中", "的考量")


def probe_prose(report: str) -> dict:
    """中文成稿程序化质检（六重检测），返回 {issues: [(名称, 描述)], n}。

    检测项：①被字句密度 ②成对连接词堆砌 ③'之一'滥用 ④单句≥3'的'
    ⑤名词化（'XX的实施'） ⑥机械连接词开头（'起初/随后/总之'——用户红线）。
    阈值按千字归一，普通报告不该触发；触发即说明文风劣化。"""
    t = re.sub(r"```.*?```", "", report, flags=re.S)      # 代码块不计
    t = re.sub(r"\|[^\n]*\|", "", t)                       # 表格行短语碎，不计
    t = re.sub(r"^>.*$", "", t, flags=re.M)                # 引用行（梗概标注）不计
    n = max(text_len(t), 1) / 1000.0
    sents = [s for s in re.split(r"[。！？；\n]", t) if text_len(s) > 6]
    issues = []
    bei = len(re.findall(r"被[\u4e00-\u9fff]{1,6}(?:所|给)", t)) \
        + len(re.findall(r"被[\u4e00-\u9fff]{1,4}[，。、；：]", t))
    if bei / n >= 2.0:
        issues.append(("被字句密度", f"{bei} 处（≥2.0/千字），建议改主动式或'由/受/让'"))
    con = sum(1 for s in sents
              if ("因为" in s and "所以" in s)
              or ("虽然" in s and "但是" in s)
              or ("尽管" in s and "然而" in s)
              or ("当" in s and "的时候" in s))
    if con / n >= 1.5:
        issues.append(("连接词堆砌", f"{con} 句（≥1.5/千字），中文语境清楚时可省成对连接词"))
    zy = t.count("之一")
    if zy / n >= 1.0:
        issues.append(("'之一'滥用", f"{zy} 处（≥1.0/千字），多数可直说'极…/相当…'"))
    de3 = sum(1 for s in sents if s.count("的") >= 3)
    if de3 / n >= 1.5:
        issues.append(("'的'字密度", f"{de3} 句单句含≥3个'的'（≥1.5/千字），建议拆短句"))
    nom = sum(t.count(w) for w in _PROSE_NOMINAL)
    if nom / n >= 1.0:
        issues.append(("名词化过度", f"'XX的实施/的进行'类 {nom} 处（≥1.0/千字），建议还原为动词"))
    mech = len(re.findall(r"(?:^|\n)#{0,4}\s*(?:\d+[.、]\s*)?(?:起初|随后|接着说?|然后|总之|综上所述)[，、：]",
                          report))
    if mech:
        issues.append(("机械连接词开头", f"{mech} 处段落以'起初/随后/总之'类开头（用户红线）"))
    return {"issues": issues, "n": len(issues)}


def _quality_section(report: str) -> str:
    """成稿质检小节：有问题才输出。"""
    p = probe_prose(report)
    if not p["n"]:
        return ""
    lines = "\n".join(f"- **{k}**：{v}" for k, v in p["issues"])
    return ("\n---\n\n## 🔍 中文成稿质检\n\n"
            f"> 程序化检测（借鉴萃猫翻译欧化中文六重自检），共 {p['n']} 项触发：\n\n"
            + lines + "\n")


# ================================================================ 金句校验

def verify_quote(quote: str, text: str, ntext: str) -> str:
    """回原文三级校验：verbatim(逐字) / near(近似) / miss(找不到)。"""
    q = (quote or "").strip().strip("“”\"'「」『』")
    if text_len(q) < 4:
        return "miss"
    if q in text:
        return "verbatim"
    nq = _norm(q)
    if nq and nq in ntext:
        return "verbatim"
    if _ngram_cover(nq, ntext) >= 0.7:
        return "near"
    return "miss"


def harvest_quotes(text: str, n: int = 15) -> list:
    """程序侧候选金句：从原文挑"短、有修辞、有情绪"的句子做原句池（防止 LLM 交不出原句）。"""
    sents = re.split(r"(?<=[。！？…])", text)
    scored = []
    for s in sents:
        s = re.sub(r"\s+", " ", s).strip()
        s = s.lstrip("“”\"'「」『』　 ")
        L = text_len(s)
        if L < 14 or L > 70:
            continue
        score = 0.0
        if 20 <= L <= 50:
            score += 2
        if any(w in s for w in ("像", "仿佛", "似的", "如同", "好比", "犹如")):
            score += 2
        score += min(sum(1 for w in _EMO if w in s), 3)
        if s.count("“") or s.count("”"):
            score += 1
        if any(w in s for w in ("说道", "笑道", "问道", "道：", "说：")):
            score -= 1.5
        if s.count("，") >= 5:
            score -= 1.0
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _, s in scored:
        k = _norm(s)[:24]
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= n:
            break
    return out


def check_quotes(text: str, cards: list) -> dict:
    """校验 Stage B 提取的全部 quotes，返回 {verbatim, near, miss}。"""
    ntext = _norm(text)
    buckets = {"verbatim": [], "near": [], "miss": []}
    seen = set()
    for card in cards:
        for q in (card.get("quotes") or []):
            q = str(q).strip().strip("“”\"'")
            if text_len(q) < 4 or q in seen:
                continue
            seen.add(q)
            buckets[verify_quote(q, text, ntext)].append(q)
    return buckets


QUOTE_SECTION_RE = re.compile(r"^##\s+[^\n]*金句[^\n]*$", re.M)


def annotate_quote_section(md: str, text: str, ntext: str) -> str:
    """成稿后处理：'原文金句'章节里回原文找不到的行，追加转述标注。"""
    m = QUOTE_SECTION_RE.search(md)
    if not m:
        return md
    start = m.end()
    nxt = re.search(r"^##\s+", md[start:], re.M)
    end = start + nxt.start() if nxt else len(md)
    body = md[start:end]
    lines = []
    for line in body.split("\n"):
        if text_len(line) < 6 or line.strip().startswith("（"):
            lines.append(line)
            continue
        found = re.findall(r"[“\"]([^”\"]{6,})[”\"]", line)
        if found:
            miss = [q for q in found if verify_quote(q, text, ntext) == "miss"]
            if miss and "转述" not in line:
                line = line.rstrip() + "  ⚠️（转述·非逐字原句）"
        lines.append(line)
    return md[:start] + "\n".join(lines) + md[end:]


# ================================================================ 人物聚合

def aggregate_characters(cards: list) -> list:
    """从 Stage B 卡片聚合人物观察：[(人名, 出场块数, [关系三元组...])]。

    关系来自逐段提取的 relations 字段（原文依据），按出场频次排序，
    用于成稿时注入"人物事实清单"，防止模型脑补亲属关系（如把小叔子写成丈夫）。
    """
    freq, rels = {}, {}
    for c in cards:
        if not isinstance(c, dict):
            continue
        ents = [str(e).strip() for e in (c.get("entities") or []) if str(e).strip()]
        for e in set(ents):
            if 1 <= len(e) <= 12:
                freq[e] = freq.get(e, 0) + 1
        for r in (c.get("relations") or []):
            r = str(r).strip()
            if not (3 <= len(r) <= 80):
                continue
            for e in ents:
                if e in r:
                    lst = rels.setdefault(e, [])
                    if r not in lst:
                        lst.append(r)
    out = []
    for name, n in sorted(freq.items(), key=lambda kv: -kv[1]):
        if n < 2:                       # 仅出现一次的多为次要人物
            continue
        out.append((name, n, (rels.get(name) or [])[:4]))
        if len(out) >= 15:
            break
    return out


def _facts_hint(core: dict) -> str:
    """把人物事实清单格式化为成稿 prompt 注入段（非叙事文本返回空）。"""
    chars = core.get("characters") or []
    if not chars:
        return ""
    s = ("\n【人物事实清单 · 来自原文逐段提取，人物身份与关系必须以此为准】\n"
         "（清单未写明的关系一律写\"原文未明说\"，严禁自行推断亲属关系）\n")
    for name, n, rl in chars:
        line = f"- {name}（出场于 {n} 个段落块）"
        if rl:
            line += "：" + "；".join(rl)
        s += line + "\n"
    return s


# ================================================================ pipeline

def pipeline_core(text: str, title: str, model: str = MODEL, progress=None,
                  dtype: str = "", cache_file: str = "",
                  base_meta: dict = None):
    """Stage A/B/C：切块→逐块提取→间隔汇总，产出四种模式共用的中间产物。"""
    text = re.sub(r"[ \t]+", " ", text).strip()
    nchar = len(text)
    if text_len(text) < 50:
        raise ValueError("文本太短（<50字）")
    progress = progress or (lambda *a: None)

    focus = (TYPE_PROFILES.get(dtype) or {}).get("stageb", "")
    chunks = split_chunks(text)
    progress("map", 0, len(chunks))

    # 断点续跑：复用缓存中已完成的卡片，只深读剩余块
    base_meta = base_meta or {}
    cards = [c for c in (base_meta.get("cards") or []) if isinstance(c, dict)]
    if cards:
        print(f"[cache] 断点续跑：已有 {len(cards)}/{len(chunks)} 块卡片，"
              "跳过对应深读")

    def _save():
        if cache_file:
            _core_cache_save(cache_file, {**base_meta, "v": 1,
                                          "cards": cards, "intervals": []})

    for i, ch in enumerate(chunks):
        if i < len(cards):                       # 已有卡片 → 只补进度不重算
            progress("map", i + 1, len(chunks))
            continue
        raw = chat(MAP_PROMPT.replace("{focus}", focus).replace("{chunk}", ch),
                   system=MAP_SYSTEM, json_mode=True, model=model,
                   num_ctx=4096)   # 短块小 ctx：KV cache 省 2GB+，防 12GB 卡换页
        try:
            cards.append(extract_json(raw))
        except ValueError:
            cards.append({"points": [], "entities": [], "quotes": [],
                          "data": [], "relations": [],
                          "_raw": raw[:500]})
        _save()                                  # 每块落盘：中断可续
        progress("map", i + 1, len(chunks))

    cached_intervals = (base_meta.get("intervals") or [])
    if cached_intervals:
        intervals = cached_intervals             # 完整命中（保险分支）
    else:
        intervals = []
        for a in range(0, len(cards), INTERVAL):
            part = cards[a:a + INTERVAL]
            raw = chat(_INTERVAL_TMPL.format(
                title=title, a=a + 1, b=a + len(part),
                cards=json.dumps(part, ensure_ascii=False)[:9000]),
                model=model, num_ctx=8192)
            intervals.append(f"[第{a+1}-{a+len(part)}段] {raw}")
    progress("reduce", len(intervals), len(intervals))
    if cache_file:
        _core_cache_save(cache_file, {**base_meta, "v": 1,
                                      "cards": cards, "intervals": intervals})

    return {"title": title, "nchar": nchar, "chunks": chunks,
            "cards": cards, "intervals": intervals, "model": model}


# ---------------------------------------------------------------- 梗概流

def _cards_to_text(cards: list, offset: int = 1, per_card: int = 900) -> str:
    """把卡片转成紧凑可读文本（供梗概/评审使用）。"""
    out = []
    for i, c in enumerate(cards, start=offset):
        if not isinstance(c, dict):
            continue
        lines = [f"[块{i}]"]
        for p in (c.get("points") or [])[:8]:
            if isinstance(p, dict):
                v = str(p.get("point", "")).strip()
            else:
                v = str(p).strip()
            if v:
                lines.append("· " + v[:120])
        for q in (c.get("quotes") or [])[:3]:
            lines.append("· 原句：" + str(q)[:80])
        for d in (c.get("data") or [])[:5]:
            lines.append("· 数据：" + str(d)[:60])
        ent = c.get("entities") or []
        if ent:
            lines.append("· 实体：" + "、".join(str(x)[:20] for x in ent[:10]))
        t = "\n".join(lines)
        if len(t) > 20:
            out.append(t[:per_card])
    return "\n".join(out)


def _merge_once(title: str, materials: list, model: str, min_chars: int,
                partial: bool = False) -> str:
    body = "\n\n".join(materials)
    if partial:
        return chat(STORY_PARTIAL_PROMPT.format(
            title=title, min_chars=max(600, min_chars), materials=body),
            system=STORY_NARRATIVE_SYSTEM, model=model, num_ctx=8192)
    return chat(STORY_MERGE_PROMPT.format(
        title=title, min_chars=STORY_MIN_CHARS, max_chars=STORY_MAX_CHARS,
        materials=body), system=STORY_NARRATIVE_SYSTEM, model=model, num_ctx=8192)


def _expand_if_short(title: str, text: str, model: str, min_chars: int,
                     rounds: int = 2) -> str:
    """字数校验：不足则要求扩写（最多 rounds 轮）；若扩写没有增长就停，避免空转烧算力。"""
    for _ in range(rounds):
        cur = text_len(text)
        if cur >= min_chars:
            break
        new = chat(STORY_EXPAND_PROMPT.format(
            cur=cur, min_chars=min_chars, cur_text=text[:12000]),
            system=STORY_NARRATIVE_SYSTEM, model=model, num_ctx=8192)
        if text_len(new) <= cur:            # 模型已写尽或拒绝扩写 → 停
            break
        text = new
    return text


def _trim_if_long(title: str, text: str, model: str, max_chars: int,
                  rounds: int = 3) -> str:
    """字数校验：超过上限则要求精简；3 轮压不动就退到**摘录重组**（抽取式，物理上难编造）。"""
    for _ in range(rounds):
        cur = text_len(text)
        if cur <= max_chars:
            return text
        new = chat(STORY_CONDENSE_PROMPT.format(
            cur=cur, max_chars=max_chars, cur_text=text[:12000]),
            system=STORY_NARRATIVE_SYSTEM, model=model, num_ctx=8192)
        if not new or text_len(new) >= cur:   # 模型没压动 → 下一轮
            continue
        text = new
    if text_len(text) > max_chars:            # 抽取式兜底：选句重组
        cur = text_len(text)
        new = chat(STORY_EXTRACT_PROMPT.format(
            title=title, cur=cur, min_chars=STORY_MIN_CHARS, max_chars=max_chars,
            cur_text=text[:12000]),
            system=STORY_NARRATIVE_SYSTEM, model=model, num_ctx=8192)
        if new and text_len(new) < text_len(text) and text_len(new) > 200:
            text = new
    return text


_QUOTE_RE = re.compile(r'["“”「」『』]([^"“”「」『』]{4,80})["“”「」『』]')

STORY_GUARD_PROMPT = """下面这篇《{title}》故事梗概中，下列带引号的内容经程序与原文比对，
**无法在原文中逐字定位**（多半是整合时虚构或改写的）：
{bad}

请修订梗概：
- 把这些引号引语改为**不带引号的间接转述**，且只保留能从素材推出的内容；拿不准就整句删去，
  **不要代之以新的细节**；
- 其余内容保持不变，篇幅仍控制在 {min_chars}-{max_chars} 字；
- 不要用"起初/随后/此后/最终"开头。

要求：直接输出修订后的完整梗概。

现有梗概：
{cur_text}"""

STORY_GROUND_PROMPT = """下面这篇《{title}》故事梗概中，程序逐句与素材比对的结论是：下列句子
**在素材中找不到足够的文字依据**（多半是整合时虚构补充的场面）：
{bad}

请修订梗概：
- 逐条处理上面列出的句子：素材中确有其事的，改写为素材里实际发生的内容；
  素材中没有的，**整句删除**，不要代之以新的细节或新的场面；
- 末尾的核心思想归纳段保持不变；
- 其余内容保持不变，篇幅仍控制在 {min_chars}-{max_chars} 字；
- 不要用"起初/随后/此后/最终"开头。

素材（比对基准）：
{materials}

要求：直接输出修订后的完整梗概。

现有梗概：
{cur_text}"""


def _guard_fabricated_quotes(title: str, summary: str, ntext: str,
                             model: str, progress=None) -> str:
    """梗概防编造守卫：引号引语逐条回原文（归一化）比对，未命中则要求模型修复一轮。"""
    if not ntext:
        return summary
    spans = _QUOTE_RE.findall(summary)
    seen, bad = set(), []
    for s in spans:
        key = _norm(s)
        if len(key) < 6 or key in seen:
            continue
        seen.add(key)
        if key not in ntext:
            bad.append(s.strip())
    if not bad:
        return summary
    progress and progress("story-guard", 0, 1)
    fixed = chat(STORY_GUARD_PROMPT.format(
        title=title, bad="\n".join(f'- “{b}”' for b in bad[:10]),
        min_chars=STORY_MIN_CHARS, max_chars=STORY_MAX_CHARS,
        cur_text=summary[:12000]),
        system=STORY_NARRATIVE_SYSTEM, model=model, num_ctx=8192)
    progress and progress("story-guard", 1, 1)
    if fixed and text_len(fixed) > 200:
        return fixed.strip()
    return summary


def _ungrounded_sentences(summary: str, mgrams: set, min_cov: float = 0.22):
    """逐句算与素材的 4-gram 覆盖率，返回覆盖率过低的句子（疑似编造）。
    末段（核心思想归纳）是抽象层，不参与比对。"""
    paras = [p for p in summary.split("\n") if p.strip()]
    body = "\n".join(paras[:-1]) if len(paras) > 1 else summary
    out = []
    for sent in re.split(r"[。；！？]", body):
        ns = _norm(sent)
        if text_len(ns) < 12:
            continue
        g = [ns[i:i + 4] for i in range(len(ns) - 3)]
        if not g:
            continue
        cov = sum(1 for q in g if q in mgrams) / len(g)
        if cov < min_cov:
            out.append((cov, sent.strip()))
    return out


def _guard_ungrounded(title: str, summary: str, materials: list,
                      model: str, progress=None) -> str:
    """梗概防编造守卫②：逐句回素材（分批叙述）抽查 4-gram 覆盖率，过低则要求修复一轮。"""
    mtext = _norm("\n".join(materials))
    if text_len(mtext) < 200:
        return summary
    mgrams = {mtext[i:i + 4] for i in range(len(mtext) - 3)}
    low = _ungrounded_sentences(summary, mgrams)
    if not low:
        return summary
    progress and progress("story-ground", 0, 1)
    fixed = chat(STORY_GROUND_PROMPT.format(
        title=title,
        bad="\n".join(f'- （依据率 {c:.0%}）{s[:60]}' for c, s in low[:8]),
        min_chars=STORY_MIN_CHARS, max_chars=STORY_MAX_CHARS,
        materials="\n\n".join(m for m in materials)[:5500],
        cur_text=summary[:9000]),
        system=STORY_NARRATIVE_SYSTEM, model=model, num_ctx=8192)
    progress and progress("story-ground", 1, 1)
    if fixed and text_len(fixed) > 200:
        low2 = _ungrounded_sentences(fixed, mgrams)
        if len(low2) < len(low):            # 有改善才采纳
            return fixed.strip()
    return summary


def compose_longform_summary(title: str, materials: list, model: str,
                             min_chars: int, progress=None,
                             stage: str = "story", ntext: str = "") -> str:
    """通用长文梗概：素材分批（必要时预合并）→ 合成 → 字数校验（补写/精简）→ 防编造守卫。"""
    progress = progress or (lambda *a: None)
    materials = [m for m in materials if text_len(m) >= 40]
    if not materials:
        return ""
    # 素材过长 → 先分组预合并，避免超出上下文
    if sum(text_len(m) for m in materials) > 9000:
        grouped = []
        for i in range(0, len(materials), 6):
            grp = materials[i:i + 6]
            grouped.append(_merge_once(title, grp, model,
                                       max(700, min_chars // 2), partial=True))
        materials = grouped
    out = _merge_once(title, materials, model, min_chars)
    out = _expand_if_short(title, out, model, min_chars)
    out = _trim_if_long(title, out, model, STORY_MAX_CHARS)
    out = _ensure_theme_paragraph(title, out, model)
    out = _guard_fabricated_quotes(title, out, ntext, model, progress)
    out = _guard_ungrounded(title, out, materials, model, progress)
    return out.strip()


def _group_chunks(chunks: list, limit: int) -> list:
    """把切块按字符数聚成若干批，每批不超过 limit。"""
    groups, cur, n = [], [], 0
    for ch in chunks:
        if cur and n + len(ch) > limit:
            groups.append(cur)
            cur, n = [], 0
        cur.append(ch)
        n += len(ch)
    if cur:
        groups.append(cur)
    return groups


def compose_story_summary(core: dict, model: str = MODEL,
                          min_chars: int = STORY_MIN_CHARS,
                          progress=None) -> str:
    """叙事文本故事梗概。

    素材以**原文**为主（分批发给模型写情节叙述）——原文依据充分，避免 Stage B 卡片的
    提取误差被放大成梗概里的错误人物关系/编造细节；再合成 → 字数校验（不足则扩写）。
    卡片仅在极端情况（原文不可用）时作为退化素材。
    """
    progress = progress or (lambda *a: None)
    chunks = core.get("chunks") or []
    materials = []
    if chunks:
        groups = _group_chunks(chunks, STORY_SOURCE_CHARS)
        facts = _facts_hint(core)
        progress("story", 0, max(len(groups), 1))
        for gi, grp in enumerate(groups):
            src = "\n\n".join(grp)[:STORY_SOURCE_CHARS + 1500]
            materials.append(chat(STORY_SOURCE_PROMPT.format(
                title=core["title"], a=gi + 1, source=src, facts=facts),
                system=STORY_NARRATIVE_SYSTEM, model=model, num_ctx=8192))
            progress("story", gi + 1, len(groups))
    if not materials:                                   # 退化：用卡片
        cards = core.get("cards") or []
        groups = [cards[i:i + STORY_BATCH_CARDS]
                  for i in range(0, len(cards), STORY_BATCH_CARDS)]
        progress("story", 0, max(len(groups), 1))
        for gi, grp in enumerate(groups):
            a = gi * STORY_BATCH_CARDS + 1
            b = a + len(grp) - 1
            material = _cards_to_text(grp, offset=a)
            if text_len(material) < 60:
                continue
            materials.append(chat(STORY_BATCH_PROMPT.format(
                title=core["title"], a=a, b=b, cards=material),
                system=STORY_NARRATIVE_SYSTEM, model=model))
            progress("story", gi + 1, len(groups))
    return compose_longform_summary(core["title"], materials, model,
                                    min_chars, progress, "story",
                                    ntext=_norm(core.get("text") or ""))


# ---------------------------------------------------------------- 成稿

def _quote_hint(core: dict) -> str:
    qc = core.get("quote_check")
    if not qc:
        return ""
    v = list(qc.get("verbatim") or [])
    for q in (core.get("harvested") or []):               # 程序抽取的候选并入原句池
        if q not in v:
            v.append(q)
    bad = list(qc.get("near") or []) + list(qc.get("miss") or [])
    s = ("\n【可用原句池 · 已逐字校验】“原文金句”一节只能从下列语句中选取并且"
         "**原样照抄**（不得改写、不得拼接、不得增删字词）：\n")
    s += ("\n".join(f'- “{q}”' for q in v[:15])
          if v else "- （无：本节请写“未找到可逐字引用的原句”）")
    if bad:
        s += ("\n【未能在原文中逐字定位的引用】不得放进“原文金句”，"
              "如需提及必须标注“（转述）”：\n")
        s += "\n".join(f'- “{q}”' for q in bad[:8])
    return s


def compose_book_brief(text: str, title: str, model: str, progress=None) -> str:
    """萃猫 R1：全书级分析卡（定位/术语称谓表/蒸馏侧重），一次生成、
    注入所有分部处理，保证跨章称谓与术语一致。失败返回空串不拖垮主流程。"""
    progress = progress or (lambda *a: None)
    marks = list(re.finditer(
        r"^(?:第[零一二三四五六七八九十百两0-9]{1,6}[章回节卷部篇]|Chapter\s+\d+|#+\s*\S[^\n]{0,40})$",
        text, flags=re.M))
    chapters = "\n".join(m.group(0).strip() for m in marks[:80]) or "（未见章节标记）"
    L = len(text)
    segs = [text[:2500]]
    if L > 6000:
        segs.append(text[L // 2:L // 2 + 2000])
    if L > 8000:
        segs.append(text[-2500:])
    samples = "\n<<<采样>>>\n".join(s for s in segs if s)
    try:
        progress("brief", 1, 1)
        brief = chat(BOOK_BRIEF_PROMPT.format(title=title, chapters=chapters,
                                              samples=samples),
                     model=model, num_ctx=8192)
        brief = re.sub(r"^```[a-z]*\s*|\s*```\s*$", "", brief.strip(),
                       flags=re.I | re.M).strip()
        print(f"[brief] 全书分析卡 {text_len(brief)} 字")
        return brief.strip() if brief else ""
    except Exception as e:
        print(f"[brief] 全书分析卡生成失败，跳过: {e}")
        return ""


def _materials_block(core: dict, dtype: str, budget: int = 15000) -> str:
    """成稿素材（两遍读核心）：分段小结 + 原文通道素材。

    - novel：故事梗概（原文批次叙述合成）+ 人物事实清单 + 预筛意象段落
    - 其他：分段小结 + 预筛段落 + 数据/实体聚合
    预筛段落超预算时优先裁剪，分段小结保底。"""
    parts = []
    # 0) 全书分析卡（book 模式注入：称谓/术语以此为准，萃猫 R1 思想）
    if core.get("brief"):
        parts.append("【全书分析卡 · 人物称谓/术语以此为准，不得异写】\n"
                     + core["brief"].strip()[:2000])
    # 1) 原文通道：故事梗概（novel）
    if dtype == "novel" and core.get("story_summary"):
        parts.append("【全文故事梗概 · 直接依据原文写成，人物/时序以此为准】\n"
                     + core["story_summary"].strip())
    # 2) 预筛原文段落
    if core.get("text"):
        paras = sieve_for(dtype, core["text"])
        if paras:
            parts.append("【预筛原文段落 · 程序按类型特征从原文选出，可直接引用】\n"
                         + "\n\n".join(f"[原文段{i+1}] {p}"
                                       for i, p in enumerate(paras)))
    # 3) 数据/实体聚合（非 novel）
    if dtype != "novel":
        data, ents = [], []
        for c in (core.get("cards") or []):
            if isinstance(c, dict):
                data += [str(x) for x in (c.get("data") or []) if str(x).strip()]
                ents += [str(x) for x in (c.get("entities") or []) if str(x).strip()]
        if data:
            seen = list(dict.fromkeys(data))[:40]
            parts.append("【全文数据/日期/地名清单（逐段提取聚合）】\n"
                         + "；".join(seen))
        if ents:
            freq = {}
            for e in ents:
                freq[e] = freq.get(e, 0) + 1
            top = sorted(freq.items(), key=lambda kv: -kv[1])[:20]
            parts.append("【高频实体】" + "、".join(f"{k}({v})" for k, v in top))
    # 4) 分段小结（保底）
    parts.append("【分段小结】\n" + "\n\n".join(core.get("intervals") or []))
    # 预算控制：超了先砍预筛段落（从尾部），再砍梗概尾部
    def _total():
        return sum(len(p) for p in parts)
    while _total() > budget and len(parts) > 2:
        for i in range(len(parts) - 1, -1, -1):
            if parts[i].startswith("【预筛原文段落"):
                del parts[i]
                break
        else:
            break
    return "\n\n".join(parts)


def compose_final(core: dict, mode: str, model: str = MODEL) -> str:
    """Stage D：基于公共中间产物按模式成稿。按文本类型选专属结构与素材。"""
    if mode not in MODES:
        raise ValueError(f"mode 必须是 {MODES}")
    dtype = core.get("dtype") or "general"
    spec = FINAL_SPEC[mode]
    task = spec["task"]
    if mode == "deep":
        prof = TYPE_PROFILES.get(dtype) or {}
        if prof.get("spec"):
            spec, task = prof["spec"], prof["spec"]["task"]
        else:
            task = task + (" '论据与置信度'表中'原文依据'一列的引用不得重复："
                           "同一句原文至多出现一次。")
    report = chat(FINAL_PROMPT.format(
        title=core["title"], task=task, quote_hint=_quote_hint(core),
        facts=_facts_hint(core),
        materials=_materials_block(core, dtype if mode == "deep" else "general"),
        nchar=core["nchar"], out=spec["out"]), model=model)
    report = re.sub(r"^#+\s*(精读报告|深度精读|分析报告)[^\n]*\n+", "", report)
    report = annotate_quote_section(report, core.get("text", ""),
                                    _norm(core.get("text", "")))
    return report.strip() + "\n"


def _header(core: dict, mode: str, extra: str = "") -> str:
    flags = []
    dtype = core.get("dtype")
    if dtype and dtype != "general":
        conf = core.get("dtype_conf", "")
        method = {"rules": "规则", "llm": "LLM", "manual": "手动",
                  "rules-fallback": "规则兜底"}.get(core.get("dtype_method", ""),
                                                    core.get("dtype_method", ""))
        flags.append(f"类型：{TYPE_LABELS.get(dtype, dtype)}（{conf} · {method}）")
    if core.get("is_narrative"):
        flags.append("叙事文本 · 附故事梗概")
    if extra:
        flags.append(extra)
    tail = (" · " + " · ".join(flags)) if flags else ""
    return (f"# DeepRead {mode} 报告 · {core['title']}\n\n"
            f"> 模型 {core.get('model', MODEL)} · 全文 {core['nchar']} 字 · "
            f"{len(core['chunks'])} 块 · {time.strftime('%Y-%m-%d %H:%M')}{tail}\n\n")


# ---------------------------------------------------------------- 对抗式复核

def adversarial_review(core: dict, report: str, model: str = MODEL,
                       progress=None) -> str:
    """三视角评审 + 主编回应。串行调用（Ollama 单实例并发会争抢显存）。"""
    progress = progress or (lambda *a: None)
    materials = "\n\n".join(core.get("intervals") or [])[:5000]
    reviews = []
    for i, (name, persona) in enumerate(CRITICS):
        progress("critique", i, len(CRITICS) + 1)
        r = chat(CRITIC_PROMPT.format(
            persona=persona, title=core["title"], nchar=core["nchar"],
            materials=materials, report=report[:7000]), model=model)
        reviews.append(f"### {name}\n{r.strip()}")
    progress("critique", len(CRITICS), len(CRITICS) + 1)
    joined = "\n\n".join(reviews)
    reply = chat(REPLY_PROMPT.format(title=core["title"], reviews=joined),
                 model=model)
    progress("critique", len(CRITICS) + 1, len(CRITICS) + 1)
    # 评审原文并入，避免主编转述丢信息
    head = "## 四视角评审（原始意见）\n\n" + joined + "\n\n"
    reply = re.sub(r"^##\s*三视角评审意见[^\n]*\n+", "", reply.strip())
    return "## 🧐 对抗式复核（教授团挑刺）\n\n" + reply + "\n\n" + head


# ================================================================ 入口

# ---------------------------------------------------------------- 核心缓存
CORE_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "data", "core_cache")


def _core_cache_path(text: str, model: str) -> str:
    """缓存键 = 模型 + 全文哈希（换模型/换文本自动失效）。"""
    h = hashlib.sha1((model + "\n" + text).encode("utf-8")).hexdigest()[:20]
    return os.path.join(CORE_CACHE_DIR, f"{h}.json")


def _core_cache_load(path: str):
    """读缓存。损坏/版本不符/环境禁用一律返回 None——缓存只做加速，永不引入新失败面。"""
    if os.environ.get("DEEPREAD_NO_CACHE"):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("v") == 1 and isinstance(d.get("cards"), list):
            return d
    except (OSError, ValueError):
        pass
    return None


def _core_cache_save(path: str, data: dict):
    """原子落盘（tmp + os.replace），失败静默——不因缓存中断任务。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def _prepare_core(text: str, title: str, model: str, mode: str, progress=None,
                  story: bool = True, dtype: str = "") -> dict:
    """公共深读 + 类型识别 + 梗概 + 金句校验。dtype 非空则跳过自动识别。
    深读/汇总结果按 (模型,全文) 缓存：命中跳过、部分命中断点续跑；
    手动 dtype 与缓存不符时弃缓存重算，能力不受影响。"""
    cache_file = _core_cache_path(text, model)
    cached = _core_cache_load(cache_file)
    cached_dtype = (cached or {}).get("dtype", "")
    if not dtype:
        if cached_dtype:                          # 命中/续跑：跳过分类调用
            dtype = cached_dtype
            conf = cached.get("dtype_conf", "")
            method = cached.get("dtype_method", "cache")
        else:
            dtype, conf, method = classify_text(text, progress=progress)
        print(f"[type] {TYPE_LABELS.get(dtype, dtype)} "
              f"(置信度 {conf} · {method})")
    else:
        dtype = dtype if dtype in TYPE_LABELS else "general"
        conf, method = 1.0, "manual"
        if cached and cached_dtype and cached_dtype != dtype:
            cached = None                         # 手动类型与缓存不符 → 弃缓存
    meta = {"model": model, "dtype": dtype,
            "dtype_conf": str(conf), "dtype_method": method}

    stripped = re.sub(r"[ \t]+", " ", text).strip()
    story_sum = ""
    if cached and cached.get("cards") and cached.get("intervals"):
        print(f"[cache] 命中：{len(cached['cards'])} 块卡片 / "
              f"{len(cached['intervals'])} 段小结，跳过逐块深读")
        core = {"title": title, "nchar": len(stripped),
                "chunks": split_chunks(stripped),
                "cards": cached["cards"], "intervals": cached["intervals"],
                "model": model}
        story_sum = cached.get("story_summary") or ""
    else:
        core = pipeline_core(text, title, model, progress, dtype=dtype,
                             cache_file=cache_file,
                             base_meta={"cards": (cached or {}).get("cards")
                                        or [],
                                        "intervals": (cached or {})
                                        .get("intervals") or [], **meta})
    core["text"] = text
    core["dtype"], core["dtype_conf"], core["dtype_method"] = dtype, conf, method
    core["is_narrative"] = (dtype == "novel")
    core["characters"] = aggregate_characters(core["cards"]) \
        if core["is_narrative"] else []
    if story and mode in ("deep", "map", "book") and core["is_narrative"]:
        if story_sum:
            core["story_summary"] = story_sum
            print("[cache] 故事梗概命中缓存")
        else:
            try:
                core["story_summary"] = compose_story_summary(
                    core, model, STORY_MIN_CHARS, progress)
                if cache_file and core.get("story_summary"):
                    d = _core_cache_load(cache_file) or {"cards": [], "intervals": []}
                    d.update(meta)
                    d["v"] = 1
                    d.setdefault("cards", core["cards"])
                    d.setdefault("intervals", core["intervals"])
                    d["story_summary"] = core["story_summary"]
                    _core_cache_save(cache_file, d)
            except Exception as e:            # 梗概失败不应拖垮整篇报告
                core["story_summary"] = ""
                core["story_error"] = str(e)[:200]
    core["quote_check"] = check_quotes(text, core["cards"])
    core["harvested"] = harvest_quotes(text, 15)
    return core


def deepread_text(text: str, title: str = "未命名", mode: str = "deep",
                  model: str = MODEL, progress=None, critique: bool = False,
                  story: bool = True, dtype: str = ""):
    """单模式入口：返回 (markdown报告, 结构化dict)。dtype 可手动指定文本类型。"""
    progress = progress or (lambda *a: None)
    if mode == "book":
        return deepread_book(text, title, model, progress, critique=critique,
                             story_min=STORY_MIN_CHARS, dtype=dtype)
    if mode == "distill":
        return deepread_distill(text, title, model, progress, dtype=dtype)
    if mode == "notes":
        return deepread_notes(text, title, model, progress, dtype=dtype)
    if mode == "persona":
        return deepread_persona(text, title, model, progress, dtype=dtype)
    if mode not in MODES:
        raise ValueError(f"mode 必须是 {ALL_MODES}")
    core = _prepare_core(text, title, model, mode, progress, story, dtype=dtype)
    body = compose_final(core, mode, model)
    md = _header(core, mode)
    if core.get("story_summary"):
        n = text_len(core["story_summary"])
        tag = ""
        if n < STORY_MIN_CHARS:
            tag = (f"（{n} 字；未达 {STORY_MIN_CHARS} 字下限——"
                   "素材不足以在不编造的前提下继续加长）")
        elif n > STORY_MAX_CHARS + 100:
            tag = f"（{n} 字；超出 {STORY_MAX_CHARS} 字上限，精简未成功）"
        md += (f"## 故事梗概{tag}\n\n" + core["story_summary"].strip()
               + "\n\n---\n\n")
    elif core.get("story_error"):
        md += f"> ⚠️ 故事梗概生成失败：{core['story_error']}\n\n---\n\n"
    md += body
    md += _quality_section(body)
    if critique:
        md += "\n---\n\n" + adversarial_review(core, body, model, progress)
    structured = {"title": title, "mode": mode, "model": model,
                  "nchar": core["nchar"], "nchunks": len(core["chunks"]),
                  "intervals": core["intervals"], "cards": core["cards"],
                  "is_narrative": core["is_narrative"],
                  "dtype": core.get("dtype", "general"),
                  "dtype_conf": core.get("dtype_conf", ""),
                  "story_summary": core.get("story_summary", ""),
                  "quote_check": core["quote_check"],
                  "report": md}
    return md, structured


def deepread_compare(text: str, title: str, model: str = MODEL,
                     progress=None, story: bool = True, dtype: str = "",
                     modes=None):
    """模式对比：一次公共深读 + 按所选模式各自成稿（省重复算力）。
    modes 为所选模式列表（None 默认全基础模式）。
    返回 (各模式报告 dict, 合并版 md, 故事梗概)。"""
    progress = progress or (lambda *a: None)
    sel = [m for m in (modes or MODES) if m in MODES] or list(MODES)
    core = _prepare_core(text, title, model, "deep", progress, story=story,
                         dtype=dtype)
    reports = {}
    for i, mode in enumerate(sel):
        progress("compose", i, len(sel))
        if mode == "distill":              # distill 多阶段，独立产出
            try:
                _md, st = deepread_distill(text, title, model, progress,
                                           dtype=core.get("dtype", ""))
                # distill 复用了 core 的管线产出，报告直接取
                reports[mode] = _header(core, mode) + \
                    st["report"].split("\n\n", 1)[-1]
            except Exception as e:
                reports[mode] = f"（{mode} 模式失败：{e}）"
            continue
        reports[mode] = _header(core, mode) + compose_final(core, mode, model)
    progress("compose", len(sel), len(sel))
    summary = (core.get("story_summary") or "").strip()
    merged = (f"# DeepRead 模式对比 · {title}\n\n"
              f"> 模型 {model} · 全文 {core['nchar']} 字 · "
              f"对比模式：{' / '.join(sel)} · "
              f"{time.strftime('%Y-%m-%d %H:%M')}"
              + (" · 已附故事梗概" if summary else "") + "\n\n")
    if summary:
        merged += ("## 故事梗概（各模式共用）\n\n" + summary + "\n\n---\n\n")
    for mode in sel:
        merged += reports[mode] + "\n---\n\n"
    return reports, merged, summary


# ---------------------------------------------------------------- 萃猫蒸馏模式
# 对标 cuimao-translator /《城市生存自救指南》蒸馏成果：
# 骨架(主旨/结构/主张/概念/边界) → 按场景聚类拆技能 → 逐技能 SKILL.md 技能包
# 2026-10-24 嫁接 cangjie-skill：技能包模板升级为 RIA 六段卡
#（R 触发场景 / I 操作步骤 / A 案例 / E 验收标准 / B 常见错误 / 来源引文），
# 并在落盘时同步生成 darwin 兼容评测用例 darwin_eval.json（见 deepread_distill 3a2）。

DISTILL_SKELETON_PROMPT = """以下是《{title}》的分段小结与关键数据清单。请蒸馏出全书骨架（Markdown，六节）：

## 一句话主旨
（一句话：这本书到底在解决什么问题，答案的关键在哪）

## 整体结构
（表格：部分 | 讲什么 | 与前一部分的关系。按内容自然划分 3-6 部分）

## 作者试图解决的核心问题
（2-4 句：面向谁、解决什么困境、难点在哪）

## 核心主张
（3-8 条编号硬主张，每条 = 主张 + 一句话依据。必须是书里反复论证的，不是你的发挥）

## 关键概念
（表格：概念 | 书中的定义 | 与日常用法的区别。收 8-15 个，数据清单里有判据数值的必须带上）

## 盲点与边界
（2-4 条：适用前提、时代/地域局限、作者没说的）

只依据给定素材，不编造。

分段小结：
{intervals}

关键数据清单：
{data}"""

DISTILL_PLAN_PROMPT = """以下是《{title}》的分段小结（带序号）。要把全书内容拆成 {nmin}-{nmax} 个技能包（每个技能管一个场景/任务簇，合起来覆盖全书主干）。倾向多技能：内容有 3 个以上不同使用场景时优先 3 个以上，避免把不相关场景塞进同一个技能。只输出 JSON 数组，不要输出其他内容：
[{{"name": "英文slug(小写字母数字连字符，与主题相关)", "title": "技能中文名", "scope": "一句话说明管辖范围", "segs": [1,2]}}]

要求：
1. 按使用场景聚类，不按章节顺序机械切分；一个分段可以进多个技能；
2. 每个技能的分段数 2-8 个；全部分段至少被一个技能覆盖；
3. name 用英文小写加连字符。

分段小结：
{intervals}"""

DISTILL_CANDIDATE_PROMPT = """你在为《{title}》的技能蒸馏做素材萃取（对应原文第 {ci} 块）。只输出 JSON，不要输出其他内容：
{{"principles":[{{"point":"一句话原则","cond":"适用条件","oppose":"它反对什么","quote":"原文逐字摘录≤60字"}}],
"steps":[{{"what":"方法/步骤名","how":"具体做法（含书中数值判据）","quote":"原文逐字摘录"}}],
"terms":[{{"term":"术语","def":"书中定义","diff":"与日常用法的区别"}}],
"cautions":[{{"warn":"禁忌/反例一句话","quote":"原文逐字摘录"}}]}}

要求：
1. 每类 0-6 条，宁缺毋滥；只收有复用价值、可迁移的内容；
2. quote 必须逐字摘自下面的原文，找不到原句就不写该条；
3. 书中的具体数值/品牌/型号判据必须带上，不许概括掉。

原文第 {ci} 块：
{chunk}"""

DISTILL_SKILL_PROMPT = """你在为《{title}》编写技能包中的技能《{stitle}》（管辖范围：{scope}）。
技能卡采用 RIA 六段结构（R 触发场景 / I 操作步骤 / A 案例 / E 验收标准 / B 常见错误 / 来源引文）。
素材含两部分：分段小结、候选素材（原则/步骤/术语/禁忌，quote 为原文逐字摘录，src 为原文块号）。
输出 SKILL.md 文件内容（Markdown，以 YAML frontmatter 开头）：

---
name: {name}
description: （什么时候调用这个技能 + 什么时候不要用它，50-120 字，写成"当你想知道X/遇到Y时用这套做Z；已经在W情形的不要用它，去用对应的技能"）
---

# {stitle}

## R 触发场景
（5-8 条用户会说的话，每条一行短引号句，对应这个技能的真实触发场景——这是 R 段：回答"什么情况下加载本技能"）

## 核心判断
（2-4 段：这个场景下书里给的底层判据——不是步骤，是"用哪把尺子量"；候选素材里的数值判据必须带上）

## I 操作步骤
（按步骤组织：每步一个 ### 小标题，用表格或清单给出判断标准/具体动作值，每步列**禁忌**。所有标准与数值必须来自素材，素材没有的具体数值不要编）

## A 案例
（1-2 个：素材里书中的真实实例，标〔原文第n块〕；素材没有实例时写一条"按上述步骤的合成演练示例"并**明确标注〔合成演练〕**，不得冒充书中案例）

## E 验收标准
（做完了怎么算成功，必须可检验。两行清单：
**做对了会看到**：……
**做错了会看到**：……
再来一行 **验收句**：一个可对照检查的具体判据，如"三个月后储蓄率提升X%"——素材没有数值就不硬造，写定性判据）

## B 常见错误
（2-4 条：新手最容易踩的坑/素材里书里警告的禁忌与反例；与"什么时候不该用它"区分开——这里写"做了会错"，那边写"根本不该做"）

## 什么时候不该用它
（2-3 条：超出本书边界的场景、与兄弟技能的分工；素材里有时代/地域局限必须写）

## 出处
（列表：内容来自哪些分段/原文块，再引 2-4 条最有分量的原文摘录，每条标注〔原文第n块〕）

素材：
{materials}"""


# darwin 兼容评测用例生成（2026-10-24 嫁接 darwin-skill）：
# 蒸出的每个技能配 input/expected 用例，落盘 darwin_eval.json，
# 可直接交给 darwin-skill 做「评估→改进→盲测→保留/回滚」自动进化。
DISTILL_EVAL_PROMPT = """你在为《{title}》蒸出的技能包生成 darwin 兼容评测用例。技能清单：
{skills}

为每个技能出 2 个评测用例。只输出 JSON 数组，不要输出其他内容：
[{{"capability": "技能slug",
   "cases": [
     {{"input": "一条真实用户请求（模拟 R 触发场景里的原话）",
       "expected": "合格回答必须覆盖的要点（含书中判据/数值，可检验）"}},
     {{"input": "一条边界/误用请求（该技能不该接或容易做错的情形）",
       "expected": "正确行为：拒答并指出该用哪个技能，或按 B 常见错误规避"}}
   ]}}]

要求：input 要像真实用户说的话；expected 必须可对照检验（有书中判据/数值优先）。"""


def _distill_eval_json(title: str, skills_md: list, eval_cases: list) -> dict:
    """组装 darwin 兼容评测文件内容。"""
    return {"capability_id": "distill-" + re.sub(r"[^\w\-]+", "-", title)[:32].strip("-").lower(),
            "type": "skillpack-eval",
            "source": title,
            "origin": "DeepRead distill × cangjie RIA × darwin eval",
            "generated_at": time.strftime("%Y-%m-%d %H:%M"),
            "n_cases": sum(len(c.get("cases") or []) for c in eval_cases),
            "cases": eval_cases}


# ---------------------------------------------------------------- darwin 评测闭环
# 2026-10-24 补全 darwin-skill 嫁接的另一半：此前只生成评测用例不消费（只写不跑）。
# 闭环 = 装上技能作答 → rubric 打分 → 低分能力带失败原因重生成一代 → 复评。
# distill（技能包）与 persona（人物卡）共用本评测器；本机 14b 自评存在
# "既当运动员又当裁判"的乐观偏差，正式进化仍应交给 darwin-skill 盲测。
DARWIN_ANSWER_SYSTEM = (
    "你是被评测的 AI 助手，已加载一份技能/能力文档。严格按文档内容回答用户请求："
    "文档覆盖的给出可照做的具体回答；文档没覆盖的如实说明并指出这一点，不要编造。")

DARWIN_SCORE_PROMPT = """你是严格的评测员，按 rubric 给下面的回答打分（1-5 整数）：
5=完全覆盖参考要点且可执行；4=覆盖大部分要点；3=覆盖一半或有明显遗漏；
2=少量沾边、多为泛泛而谈；1=答非所问或编造。
参考要点之外答得再多也不加分；要点靠编造满足的直接 ≤2。
只输出 JSON（不要其他内容）：{{"score": 1-5, "reason": "≤60字，扣分或得分理由，须具体"}}

【能力文档摘要】{skill_brief}

【用户请求】
{inp}

【参考要点（合格回答必须覆盖）】
{expected}

【被评测助手的回答】
{answer}"""

DARWIN_FIX_NOTE = """

【上一版评测失败反馈（本次必须针对性修正）】
平均得分 {avg:.1f}/5，低于 3 分。失败细节：
{fails}
修正要求：逐条对照失败原因改写；E 验收标准必须是可检验的具体判据（数值/可观察结果），
不含判据的段落删掉；不要为了"显得完整"添加文档素材里没有的内容。"""

DARWIN_REGEN_MAX = 3          # 最多重生成 3 个低分能力（控算力）
DARWIN_REGEN_THRESHOLD = 3.0  # 平均分低于此值才触发重生成


def _darwin_evaluate(skills_md, eval_cases, model, progress=None):
    """darwin 评测：每个用例「装上能力文档作答 → rubric 打分」。

    skills_md: [(stitle, name, scope, body)]；eval_cases: darwin 用例列表。
    返回 (results, avg)。results: [{"capability","title","avg","cases":[...]}]。
    """
    progress = progress or (lambda *a: None)
    by_name = {nm: (st, sc, sk) for st, nm, sc, sk in skills_md}
    caps = [c for c in eval_cases if isinstance(c, dict) and c.get("capability") in by_name]
    total = sum(len(c.get("cases") or []) for c in caps)
    results, done = [], 0
    for c in caps:
        st, sc, sk = by_name[c["capability"]]
        cap = {"capability": c["capability"], "title": st, "cases": []}
        for case in (c.get("cases") or []):
            done += 1
            progress("darwineval", done, max(total, 1))
            inp = str(case.get("input") or "")[:800]
            expected = str(case.get("expected") or "")[:800]
            try:
                ans = chat(f"【能力文档】\n{sk[:6000]}\n\n【用户请求】\n{inp}",
                           system=DARWIN_ANSWER_SYSTEM,
                           model=model, num_ctx=12288)
            except Exception as e:                               # noqa: BLE001
                cap["cases"].append({"input": inp, "score": 0,
                                     "reason": f"作答失败: {e}"})
                continue
            try:
                s = extract_json(chat(
                    DARWIN_SCORE_PROMPT.format(
                        skill_brief=f"{st}｜管辖：{sc}"[:200],
                        inp=inp[:600], expected=expected, answer=ans[:4000]),
                    model=model, num_ctx=8192, temperature=0.1))
                score = int(s.get("score") or 0) if isinstance(s, dict) else 0
                reason = str(s.get("reason") or "")[:140] if isinstance(s, dict) else "评分解析失败"
            except Exception as e:                               # noqa: BLE001
                score, reason = 0, f"评分失败: {e}"
            cap["cases"].append({"input": inp, "answer": ans[:2000],
                                 "score": min(5, max(0, score)), "reason": reason})
        ss = [x["score"] for x in cap["cases"]]
        cap["avg"] = round(sum(ss) / len(ss), 2) if ss else 0.0
        results.append(cap)
    allsc = [x["score"] for r in results for x in r["cases"]]
    return results, (round(sum(allsc) / len(allsc), 2) if allsc else 0.0)


def _darwin_result_json(title: str, kind: str, results: list, avg: float,
                        fixed: list) -> dict:
    """darwin 闭环评测结果文件内容（darwin-skill 可继续接力）。"""
    return {"capability_id": f"{kind}-{re.sub(r'[^\w\-]+', '-', title)[:32].strip('-').lower()}",
            "type": "skillpack-eval-result",
            "source": title, "origin": "DeepRead darwin 闭环（本机自评）",
            "generated_at": time.strftime("%Y-%m-%d %H:%M"),
            "overall_avg": avg,
            "regenerated": fixed,
            "capabilities": results}


def _darwin_render(results: list, avg: float, fixed: list) -> str:
    """评测结果 → 报告 Markdown 段落。"""
    if not results:
        return ""
    rows = "\n".join(
        f"| {r['capability']} | {r['title'][:20]} | {r['avg']} | "
        + "；".join(f"{x['score']}分({x['reason'][:40]})" for x in r["cases"][:2])
        + ("…" if len(r["cases"]) > 2 else "") + "|"
        for r in results)
    note = (f"\n\n🔁 **低分重生成**：{len(fixed)} 个能力低于 {DARWIN_REGEN_THRESHOLD} 分，"
            "已带失败反馈重写并复评（结果为复评分）。" if fixed else "")
    flag = "🟢" if avg >= 4 else ("🟡" if avg >= 3 else "🔴")
    return (f"\n\n---\n\n## {flag} darwin 评测闭环（装卡作答 → rubric 打分）\n\n"
            f"**总体均分 {avg}/5**{note}\n\n"
            f"| 能力 | 名称 | 均分 | 用例得分与理由 |\n| --- | --- | --- | --- |\n{rows}\n\n"
            "> ⚠️ 本机 14b 自评存在「既当运动员又当裁判」的乐观偏差；"
            "把 `darwin_eval_result.json` 交给 darwin-skill 做盲测/交叉评测可得更可信结论。\n")


# ================================================================ notes 导出模式
# 书 → 纪传卡片(Obsidian [[链接]]) + mermaid 时间线
# 复用 pipeline_core 的 cards（entities / relations 三元组）+ 文体自适应抽取。
# 这是 DeepRead 在 history/novel 文体上已抽出的「实体 + 关系 + 时间信息」的
# 下游落盘形态，不另起抽取管线，仅做一次 LLM 结构化聚合 + 文件渲染。

NOTES_SYSTEM = (
    "你是严谨的中文史料/人物结构化助手。只依据给定文本片段与逐段抽取的卡片提取信息，"
    "文本中没有的不要编造；人物身份、关系、时间、事件都必须能在原文卡片里找到依据。"
)

NOTES_PROMPT = """基于《{title}》的下列「逐段抽取卡片」素材，产出结构化「纪传 + 时间线」数据，输出 JSON（不要输出其他内容）：

{{
  "entities": [
    {{
      "name": "人物 / 势力 / 机构 / 地点名（用书中原称）",
      "type": "人物 | 势力 | 机构 | 地点 | 概念",
      "desc": "一句话身份/性质（≤40字，仅用原文依据）",
      "faction": "所属势力/阵营（无则空串）",
      "relations": ["与其他实体的关系三元组，如『甲 是 乙的盟友』『甲 击败 乙』"],
      "events": ["该实体参与的关键事件名（与下方 timeline 的 event 对应）"]
    }}
  ],
  "timeline": [
    {{"time": "年份/朝代/约X年（不明用『约』）", "event": "事件名",
      "actors": ["相关实体名"], "note": "一句话意义（≤30字）"}}
  ]
}}

要求：
- entities 收录全部重要人物与势力/机构（出现 ≥2 次或关键单次），去重，名称与原文一致；
- relations 必须是「甲 关系 乙」式三元组，且甲或乙是本列表中的实体；
- timeline 按时间先后排序，time 尽量可比较；事件必须与 entities 的 events 对应；
- 不要编造原文没有的关系或时间。

素材（逐段卡片聚合，含实体与关系三元组）：
{cards}
"""


def _notes_cards_text(cards: list, limit_total: int = 14000) -> str:
    """把 cards 压成带 entities/relations 的紧凑文本，供 notes 聚合 LLM 使用。"""
    out = []
    for i, c in enumerate(cards, 1):
        if not isinstance(c, dict):
            continue
        lines = [f"[块{i}]"]
        for p in (c.get("points") or [])[:6]:
            v = p.get("point") if isinstance(p, dict) else str(p)
            if v:
                lines.append("· " + str(v)[:100])
        for e in (c.get("entities") or [])[:12]:
            lines.append("· 实体：" + str(e)[:20])
        for r in (c.get("relations") or [])[:8]:
            lines.append("· 关系：" + str(r)[:80])
        t = "\n".join(lines)
        if t:
            out.append(t[:1000])
    return "\n\n".join(out)[:limit_total]


def _safe_name(s: str) -> str:
    """实体名 → 安全文件名/链接 id（保持中文，仅剔除非法字符并限长）。"""
    s = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", str(s)).strip()
    return (s[:40] or "实体").strip()


def _wiki(text: str, names: list, safe_map: dict) -> str:
    """把文本里出现的已知实体名替换为 Obsidian 双向链接 [[safe_name]]。"""
    # 长名优先，避免短名被先替换后造成嵌套；用负向环视避免已在 [[ ]] 内的重复替换
    for nm in sorted(set(names), key=len, reverse=True):
        if not nm:
            continue
        link = "[[" + safe_map.get(nm, _safe_name(nm)) + "]]"
        text = re.sub(r"(?<!\[\[)" + re.escape(nm) + r"(?!\]\])",
                      lambda m, _l=link: _l, text)
    return text


def compose_notes(core: dict, model: str = MODEL, progress=None) -> dict:
    """基于公共中间产物，产出纪传卡片 + 时间线的结构化数据（一次 LLM 聚合）。"""
    progress = progress or (lambda *a: None)
    cards_txt = _notes_cards_text(core.get("cards") or [])
    progress("notes", 0, 3)
    raw = chat(NOTES_PROMPT.format(title=core["title"], cards=cards_txt),
               system=NOTES_SYSTEM, json_mode=True, model=model, num_ctx=12288)
    try:
        data = extract_json(raw)
    except ValueError:
        data = {"entities": [], "timeline": []}
    if not isinstance(data, dict):
        data = {"entities": [], "timeline": []}
    ents = [e for e in (data.get("entities") or [])
            if isinstance(e, dict) and e.get("name")]
    tl = [t for t in (data.get("timeline") or [])
          if isinstance(t, dict) and t.get("event")]
    progress("notes", 1, 3)
    return {"entities": ents, "timeline": tl, "title": core["title"]}


def _render_timeline(tl: list, title: str, names: set) -> str:
    lines = ["# 《" + title + "》· 时间线", "",
             "> DeepRead notes 导出 · 按时间排序（不明处用『约』）", "",
             "```mermaid", "timeline", "    title 《" + title + "》时间线"]
    for t in tl:
        time_s = (str(t.get("time") or "未知")).strip() or "未知"
        ev = (str(t.get("event") or "")).strip()
        note = (str(t.get("note") or "")).strip()
        seg = f"    {time_s} : {ev}" + (f" : {note}" if note else "")
        lines.append(seg)
    lines += ["```", "",
              "## 时间线明细（表格，便于不渲染 mermaid 的环境）", "",
              "| 时间 | 事件 | 相关实体 | 意义 |", "|---|---|---|---|"]
    for t in tl:
        time_s = (str(t.get("time") or "未知")).strip() or "未知"
        ev = (str(t.get("event") or "")).strip()
        actors = t.get("actors") or []
        actors_disp = " ".join(f"[[{a}]]" for a in actors if a in names) or "—"
        note = (str(t.get("note") or "")).strip()
        lines.append(f"| {time_s} | {ev} | {actors_disp} | {note} |")
    lines.append("")
    return "\n".join(lines)


def _render_index(ents: list, tl: list, title: str) -> str:
    by_type: dict = {}
    for e in ents:
        ty = (str(e.get("type") or "其他")).strip() or "其他"
        by_type.setdefault(ty, []).append(e)
    type_label = {"人物": "人物", "势力": "势力/阵营", "机构": "机构/组织",
                  "地点": "地点", "概念": "概念"}
    lines = ["# 《" + title + "》· 纪传 + 时间线索引", "",
             f"> DeepRead notes 导出 · {len(ents)} 个实体 · {len(tl)} 条时间线事件", "",
             "本目录结构：",
             "- `索引.md`（本文件）：实体总览，点击 `[[名称]]` 跳转卡片",
             "- `cards/`：每个实体的纪传卡片（Obsidian 双向链接）",
             "- `时间线.md`：mermaid 时间线 + 明细表", ""]
    for ty in ["人物", "势力", "机构", "地点", "概念", "其他"]:
        lst = by_type.get(ty)
        if not lst:
            continue
        lines.append(f"## {type_label.get(ty, ty)}（{len(lst)}）")
        for e in lst:
            nm = e["name"]
            desc = (str(e.get("desc") or "")).strip()
            lines.append(f"- [[{nm}]] — {desc}")
        lines.append("")
    lines.append("## 时间线")
    lines.append("见 [[时间线]]")
    lines.append("")
    return "\n".join(lines)


def _render_notes(data: dict, out_dir: str, cards_dir: str) -> dict:
    """把结构化数据渲染为：每个实体的纪传卡片 + 时间线.md + 索引.md。"""
    title = data["title"]
    ents = data["entities"]
    tl = data["timeline"]
    names = [e["name"] for e in ents]
    name_set = set(names)
    safe_map = {nm: _safe_name(nm) for nm in names}
    # 文件名去重（同名实体共享卡片）
    used, fname_map = set(), {}
    for nm in names:
        s = safe_map[nm]
        if s in used:
            k = 2
            while f"{s}_{k}" in used:
                k += 1
            s = f"{s}_{k}"
        used.add(s)
        fname_map[nm] = s

    files = []
    # 1) 纪传卡片
    for e in ents:
        nm = e["name"]
        etype = (str(e.get("type") or "人物")).strip() or "人物"
        desc = (str(e.get("desc") or "")).strip()
        faction = (str(e.get("faction") or "")).strip()
        rels = e.get("relations") or []
        evs = e.get("events") or []
        linked_others = set()
        rel_lines = []
        for r in rels:
            rs = str(r)
            rel_lines.append("- " + _wiki(rs, names, safe_map))
            for o in names:
                if o != nm and o in rs:
                    linked_others.add(o)
        faction_disp = ""
        if faction:
            if faction in name_set and faction != nm:
                linked_others.add(faction)
                faction_disp = f"[[{faction}]]"
            else:
                faction_disp = faction

        md = (f"---\ntype: {etype}\n书名: 《{title}》\n来源: DeepRead notes 导出\n---\n"
              f"# {nm}\n\n> {desc}\n")
        if faction_disp:
            md += f"\n**所属势力**：{faction_disp}\n"
        if rel_lines:
            md += "\n## 关系\n" + "\n".join(rel_lines) + "\n"
        if evs:
            md += "\n## 涉及事件\n" + "\n".join(f"- {str(ev)}" for ev in evs) + "\n"
        if linked_others:
            md += ("\n## 关系网\n"
                   + " ".join(f"[[{o}]]" for o in sorted(linked_others)) + "\n")
        md += (f"\n---\n*由 DeepRead notes 模式从《{title}》自动提取；"
               "链接指向同书其他实体卡片。*\n")
        fn = os.path.join(cards_dir, fname_map[nm] + ".md")
        open(fn, "w", encoding="utf-8").write(md)
        files.append(fn)

    # 2) 时间线.md
    tl_md = _render_timeline(tl, title, name_set)
    tl_path = os.path.join(out_dir, "时间线.md")
    open(tl_path, "w", encoding="utf-8").write(tl_md)
    files.append(tl_path)

    # 3) 索引.md
    idx = _render_index(ents, tl, title)
    idx_path = os.path.join(out_dir, "索引.md")
    open(idx_path, "w", encoding="utf-8").write(idx)
    files.append(idx_path)

    return {"files": files, "index_md": idx,
            "cards_dir": cards_dir, "timeline_path": tl_path}


def deepread_notes(text: str, title: str = "未命名", model: str = MODEL,
                   progress=None, dtype: str = "") -> "tuple[str, dict]":
    """notes 导出模式：书 → 纪传卡片([[链接]]) + mermaid 时间线。

    复用 pipeline_core（一次公共深读；cards 已含 entities/relations）。
    返回 (索引md, structured)；产物落盘 reports/notes_<ts>_<title>/。"""
    progress = progress or (lambda *a: None)
    core = _prepare_core(text, title, model, "notes", progress,
                         story=False, dtype=dtype)
    data = compose_notes(core, model, progress)
    progress("notes", 2, 3)
    out_dir = os.path.join(
        ROOT, "reports",
        "notes_" + time.strftime("%Y%m%d_%H%M%S") + "_" + _safe_name(title)[:20])
    os.makedirs(out_dir, exist_ok=True)
    cards_dir = os.path.join(out_dir, "cards")
    os.makedirs(cards_dir, exist_ok=True)
    rendered = _render_notes(data, out_dir, cards_dir)
    progress("notes", 3, 3)
    md = rendered["index_md"]
    structured = {
        "title": title, "mode": "notes", "model": model,
        "nchar": core["nchar"], "nchunks": len(core["chunks"]),
        "dtype": core.get("dtype", "general"),
        "entities": len(data["entities"]), "timeline": len(data["timeline"]),
        "out_dir": out_dir, "files": rendered["files"], "report": md,
    }
    return md, structured


def _seg_materials(core: dict, seg_ids: list, budget: int = 8000) -> str:
    """蒸馏技能素材：序号对应 interval，附带其覆盖块的要点/数据卡片。
    总量有硬预算（防 prompt 超出 ctx 卡死），按段数均分。"""
    per = max(1000, budget // max(len(seg_ids), 1))
    parts = []
    for sid in seg_ids:
        a = (sid - 1) * INTERVAL
        iv = core["intervals"][sid - 1] if sid - 1 < len(core["intervals"]) else ""
        cards = core["cards"][a:a + INTERVAL]
        parts.append(iv + "\n" + _cards_to_text(cards, offset=a + 1,
                                                per_card=per)[:per])
    return "\n\n".join(parts)[:budget + 2000]


def _cand_to_md(cand: dict, kind: str, cname: str) -> str:
    """候选素材一类 → Markdown（对标萃猫候选/格式：表述+条件+反对+原文摘录）。"""
    items = cand.get(kind) or []
    if not items:
        return ""
    lines = [f"# {cname}提取候选", ""]
    heads = {"principles": "原则的一句话表述", "steps": "方法与步骤",
             "terms": "术语与定义", "cautions": "禁忌与反例"}
    lines.append(f"> {heads.get(kind, kind)}，共 {len(items)} 条，"
                 "quote 为原文逐字摘录，src 为原文块号。\n")
    for it in items:
        if not isinstance(it, dict):
            continue
        if kind == "principles":
            lines += [f"## {it.get('point', '').strip()}",
                      f"- 适用条件：{it.get('cond', '').strip()}",
                      f"- 它反对什么：{it.get('oppose', '').strip()}"]
        elif kind == "steps":
            lines += [f"## {it.get('what', '').strip()}",
                      f"- 做法：{it.get('how', '').strip()}"]
        elif kind == "terms":
            lines += [f"## {it.get('term', '').strip()}",
                      f"- 书中定义：{it.get('def', '').strip()}",
                      f"- 与日常用法的区别：{it.get('diff', '').strip()}"]
        else:
            lines += [f"## {it.get('warn', '').strip()}"]
        q = str(it.get("quote", "")).strip()
        if q:
            lines.append(f"- 原文摘录：“{q}”〔原文第{it.get('src', '?')}块〕")
        lines.append("")
    return "\n".join(lines)


def _skill_materials(core: dict, cand: dict, seg_ids: list,
                     budget: int = 9000) -> str:
    """技能素材 v2：候选素材（按技能分管的原文块过滤）+ 对应分段小结。"""
    seg_set = set(seg_ids)
    parts = []
    for kind in ("principles", "steps", "terms", "cautions"):
        rows = [it for it in (cand.get(kind) or [])
                if isinstance(it, dict)
                and (int(it.get("src", 0)) // INTERVAL + 1) in seg_set]
        if not rows:
            continue
        tag = {"principles": "原则", "steps": "方法步骤",
               "terms": "术语", "cautions": "禁忌反例"}[kind]
        lines = [f"【{tag}】"]
        for it in rows:
            if kind == "principles":
                lines.append(f"- {it.get('point','')}（适用：{it.get('cond','')}；"
                             f"反对：{it.get('oppose','')}）"
                             f"〔第{it.get('src','?')}块〕"
                             + (f"\n  原文：“{it.get('quote','')}”" if it.get("quote") else ""))
            elif kind == "steps":
                lines.append(f"- {it.get('what','')}：{it.get('how','')}"
                             f"〔第{it.get('src','?')}块〕"
                             + (f"\n  原文：“{it.get('quote','')}”" if it.get("quote") else ""))
            elif kind == "terms":
                lines.append(f"- {it.get('term','')}：{it.get('def','')}"
                             f"（与日常区别：{it.get('diff','')}）")
            else:
                lines.append(f"- 禁忌：{it.get('warn','')}"
                             f"〔第{it.get('src','?')}块〕"
                             + (f"\n  原文：“{it.get('quote','')}”" if it.get("quote") else ""))
        parts.append("\n".join(lines))
    ivs = "\n".join(
        f"【第{sid}段小结】" + re.sub(r"^\[第[^\]]*\]\s*", "",
        core["intervals"][sid - 1] if sid - 1 < len(core["intervals"]) else "")
        for sid in seg_ids)
    parts.append(ivs)
    return "\n\n".join(parts)[:budget + 2000]


def deepread_distill(text: str, title: str = "未命名", model: str = MODEL,
                     progress=None, dtype: str = ""):
    """萃猫蒸馏模式：全书骨架 → 场景聚类 → 逐技能 SKILL.md（落盘技能包目录）。
    返回 (报告md, structured)。产出对标《城市生存自救指南》蒸馏成果。"""
    progress = progress or (lambda *a: None)
    core = _prepare_core(text, title, model, "distill", progress,
                         story=False, dtype=dtype)
    intervals = core.get("intervals") or []
    if not intervals:
        raise ValueError("分段小结为空，无法蒸馏")
    n = len(intervals)
    segs_txt = "\n".join(
        f"[{i+1}] " + re.sub(r"^\[第[^\]]*\]\s*", "", iv)
        for i, iv in enumerate(intervals))
    data = []
    for c in core.get("cards") or []:
        if isinstance(c, dict):
            data += [str(x) for x in (c.get("data") or []) if str(x).strip()]
    data_txt = "；".join(dict.fromkeys(data))[:1500]

    # 1) 全书骨架
    progress("distill", 1, 3)
    skeleton = chat(DISTILL_SKELETON_PROMPT.format(
        title=title, intervals=segs_txt[:9000], data=data_txt or "（无）"),
        model=model, num_ctx=12288)
    skeleton = re.sub(r"^```[a-z]*\s*|\s*```\s*$", "", skeleton.strip(),
                      flags=re.I | re.M).strip()

    # 2) 候选素材萃取（萃猫核心阶段）：逐块原文 → 原则/步骤/术语/禁忌（带逐字摘录）
    chunks = core.get("chunks") or []
    nc = len(chunks)
    cand = {"principles": [], "steps": [], "terms": [], "cautions": []}
    seen_pts = set()
    for ci, ch in enumerate(chunks, 1):
        progress("cand", ci, nc)
        try:
            j = extract_json(chat(DISTILL_CANDIDATE_PROMPT.format(
                title=title, ci=ci, chunk=ch[:7500]),
                model=model, num_ctx=12288, temperature=0.2))
        except Exception:                                    # noqa: BLE001
            continue                                          # 单块失败不阻断
        if not isinstance(j, dict):
            continue
        for kind in cand:
            for it in (j.get(kind) or []):
                if not isinstance(it, dict):
                    continue
                key = str(it.get("point") or it.get("what")
                          or it.get("term") or it.get("warn") or "")[:24]
                if not key or key in seen_pts:                # 跨块去重
                    continue
                seen_pts.add(key)
                it = dict(it)
                it["src"] = ci
                cand[kind].append(it)
    print(f"[distill] 候选素材：原则{len(cand['principles'])}"
          f" 步骤{len(cand['steps'])} 术语{len(cand['terms'])}"
          f" 禁忌{len(cand['cautions'])}")

    # 2b) 场景聚类方案
    progress("distill", 2, 3)
    plan = []
    try:
        plan_raw = chat(DISTILL_PLAN_PROMPT.format(
            title=title,
            nmin=min(3, max(1, n)), nmax=max(1, min(5, n)),
            intervals=segs_txt[:9000]),
            model=model, num_ctx=8192, temperature=0.2)
        plan = extract_json(plan_raw)
        if isinstance(plan, dict):
            plan = plan.get("skills") or plan.get("items") or []
    except ValueError:
        plan = []
    if not isinstance(plan, list) or not plan:      # 兜底：单技能覆盖全书
        plan = [{"name": "book-core", "title": title + " · 核心技能",
                 "scope": "全书核心内容", "segs": list(range(1, n + 1))}]
    plan = [p for p in plan if isinstance(p, dict)][:5]

    # 3) 落盘骨架与候选素材 + 逐技能生成 SKILL.md
    base = os.path.join("reports", "distill_" + time.strftime("%Y%m%d_%H%M%S")
                        + "_" + re.sub(r"[^\w\-]+", "_", title)[:24])
    try:
        os.makedirs(base, exist_ok=True)
        open(os.path.join(base, "骨架.md"), "w", encoding="utf-8").write(
            f"# 《{title}》骨架\n\n" + skeleton + "\n")
        os.makedirs(os.path.join(base, "候选"), exist_ok=True)
        for kind, cname in (("principles", "原则"), ("steps", "方法步骤"),
                            ("terms", "术语"), ("cautions", "禁忌反例")):
            ctext = _cand_to_md(cand, kind, cname)
            if ctext:
                open(os.path.join(base, "候选", f"{cname}.md"), "w",
                     encoding="utf-8").write(ctext)
    except OSError as e:
        print(f"[distill] 骨架/候选落盘失败: {e}")
    skills_md, skill_files, mats_by_name = [], [], {}
    for k, s in enumerate(plan):
        progress("skill", k + 1, len(plan))
        name = re.sub(r"[^a-z0-9\-]+", "",
                      str(s.get("name") or f"skill-{k+1}").lower())[:40] \
            or f"skill-{k+1}"
        stitle = str(s.get("title") or name)[:40]
        scope = str(s.get("scope") or "")[:200]
        seg_ids = sorted({int(i) for i in (s.get("segs") or [])
                          if str(i).strip().isdigit() and 1 <= int(i) <= n})
        if not seg_ids:
            seg_ids = [min(k + 1, n)]
        mats = _skill_materials(core, cand, seg_ids)
        if len(mats) < 400:                     # 候选萃取颗粒太粗 → 回退卡片素材
            mats = _seg_materials(core, seg_ids)
        try:
            sk = chat(DISTILL_SKILL_PROMPT.format(
                title=title, stitle=stitle, scope=scope, name=name,
                materials=mats), model=model, num_ctx=12288)
            sk = re.sub(r"^```[a-z]*\s*|\s*```\s*$", "", sk.strip(),
                        flags=re.I | re.M).strip()
        except Exception as e:
            sk = f"（技能生成失败：{e}）"
        skills_md.append((stitle, name, scope, sk))
        mats_by_name[name] = mats
        try:                                    # 落盘技能包（可导入 SKILL.md 的助手）
            d = os.path.join(base, "技能包", name)
            os.makedirs(d, exist_ok=True)
            body = sk if sk.startswith("---") else \
                f"---\nname: {name}\ndescription: {scope}\n---\n\n" + sk
            open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8").write(body)
            skill_files.append(f"{os.path.basename(base)}/技能包/{name}/SKILL.md")
        except OSError as e:
            print(f"[distill] 技能落盘失败: {e}")
    if not skills_md:
        raise ValueError("技能聚类失败（无有效技能）")

    # 3a2) darwin 兼容评测用例（一次 LLM 调用；失败退化为确定性用例，不阻断）
    eval_cases = []
    try:
        progress("eval", 1, 2)
        skills_brief = "\n".join(f"- {nm}｜{st}｜管辖：{sc}"
                                 for st, nm, sc, _s in skills_md)
        ev = extract_json(chat(DISTILL_EVAL_PROMPT.format(
            title=title, skills=skills_brief),
            model=model, num_ctx=8192, temperature=0.2))
        if isinstance(ev, list):
            eval_cases = [c for c in ev if isinstance(c, dict) and c.get("capability")]
    except Exception as e:                                   # noqa: BLE001
        print(f"[distill] 评测用例生成失败，退化为确定性用例: {e}")
    if not eval_cases:                    # 确定性兜底：每技能 1 条主用例
        eval_cases = [{"capability": nm,
                       "cases": [{"input": f"我想处理：{sc}",
                                  "expected": "按技能步骤给出可执行方案，"
                                              "符合 SKILL.md 的 E 验收标准"}]}
                      for st, nm, sc, _s in skills_md]
    try:
        with open(os.path.join(base, "darwin_eval.json"), "w",
                  encoding="utf-8") as f:
            json.dump(_distill_eval_json(title, skills_md, eval_cases), f,
                      ensure_ascii=False, indent=2)
        print(f"[distill] darwin_eval.json 已落盘"
              f"（{sum(len(c.get('cases') or []) for c in eval_cases)} 用例）")
    except OSError as e:
        print(f"[distill] darwin_eval.json 落盘失败: {e}")

    # 3a3) darwin 评测闭环：装卡作答 → rubric 打分 → 低分能力重生成一代 → 复评
    eval_results, eval_avg, fixed = [], 0.0, []
    try:
        eval_results, eval_avg = _darwin_evaluate(skills_md, eval_cases, model, progress)
        low = [r for r in eval_results if r["avg"] < DARWIN_REGEN_THRESHOLD][:DARWIN_REGEN_MAX]
        if low:
            for k, r in enumerate(low, 1):
                progress("regen", k, len(low))
                idx = next((i for i, (_st, nm, _sc, _sk) in enumerate(skills_md)
                            if nm == r["capability"]), None)
                if idx is None:
                    continue
                st, nm, sc, _old = skills_md[idx]
                fails = "\n".join(f"- 输入「{x['input'][:80]}」得 {x['score']} 分：{x['reason']}"
                                  for x in r["cases"])
                mats = (mats_by_name.get(nm) or _seg_materials(core, [1, min(2, n)]))
                try:
                    fixed_sk = chat(DISTILL_SKILL_PROMPT.format(
                        title=title, stitle=st, scope=sc, name=nm,
                        materials=mats[:12000] + DARWIN_FIX_NOTE.format(
                            avg=r["avg"], fails=fails[:1200])),
                        model=model, num_ctx=12288)
                    fixed_sk = re.sub(r"^```[a-z]*\s*|\s*```\s*$", "", fixed_sk.strip(),
                                      flags=re.I | re.M).strip()
                except Exception as e:                           # noqa: BLE001
                    print(f"[darwin] {nm} 重生成失败: {e}")
                    continue
                skills_md[idx] = (st, nm, sc, fixed_sk)
                try:
                    d = os.path.join(base, "技能包", nm)
                    body = fixed_sk if fixed_sk.startswith("---") else \
                        f"---\nname: {nm}\ndescription: {sc}\n---\n\n" + fixed_sk
                    open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8").write(body)
                except OSError as e:
                    print(f"[darwin] {nm} 重写落盘失败: {e}")
            re_caps = [c for c in eval_cases if c.get("capability") in
                       {r["capability"] for r in low}]
            re_results, _ra = _darwin_evaluate(skills_md, re_caps, model, progress)
            by_cap = {r["capability"]: r for r in re_results}
            for i, r in enumerate(eval_results):
                if r["capability"] in by_cap:
                    eval_results[i] = by_cap[r["capability"]]
                    fixed.append(r["capability"])
            allsc = [x["score"] for r in eval_results for x in r["cases"]]
            eval_avg = round(sum(allsc) / len(allsc), 2) if allsc else eval_avg
    except Exception as e:                                       # noqa: BLE001
        print(f"[darwin] 评测闭环失败（不影响主产物）: {e}")
    if eval_results:
        try:
            with open(os.path.join(base, "darwin_eval_result.json"), "w",
                      encoding="utf-8") as f:
                json.dump(_darwin_result_json(title, "distill", eval_results,
                                              eval_avg, fixed), f,
                          ensure_ascii=False, indent=2)
            print(f"[distill] darwin_eval_result.json 已落盘（均分 {eval_avg}/5）")
        except OSError as e:
            print(f"[darwin] 评测结果落盘失败: {e}")

    # 3b) 怎么用.md（导入指南，对标萃猫）
    try:
        rows = "\n".join(
            f"| `{nm}` | {st} | {sc} |"
            for st, nm, sc, _s in skills_md)
        open(os.path.join(base, "怎么用.md"), "w", encoding="utf-8").write(
            f"# 怎么导入《{title}》的技能包\n\n"
            f"这份蒸馏产物是 **{len(skills_md)} 个技能包**，不是读书笔记。"
            "它们让你的 AI 助手在对应场景下直接给出可照做的判断与步骤。\n\n"
            "## 导入步骤\n\n"
            "1. 打开你的 AI 助手，找到「技能 / 插件 / Skills」入口；\n"
            "2. 点「导入」，选中 `技能包/` 里的**某一个技能文件夹**"
            "（内含 SKILL.md），不是整个 `技能包/` 大目录；\n"
            "3. 需要几个导几个；建议一次只开当前用得上的，避免互相抢触发；\n"
            "4. 每个技能的 `description` 写了什么时候用、什么时候不用，先看它。\n\n"
            "## 技能分工\n\n"
            "| 技能文件夹 | 中文名 | 管辖范围 |\n| --- | --- | --- |\n"
            + rows + "\n\n"
            "## 一个提醒\n\n"
            "本书内容有时效与地域背景，技能给出的是书中的判据与方法，"
            "结合你所在地的实际情况取用；全书骨架见 `骨架.md`，"
            "素材摘录见 `候选/`。\n")
    except OSError as e:
        print(f"[distill] 怎么用落盘失败: {e}")

    # 4) 组装报告
    head = (f"# DeepRead 萃猫蒸馏 · {title}\n\n"
            f"> 模型 {model} · 全文 {core['nchar']} 字 · "
            f"蒸出 {len(skills_md)} 个技能 · {time.strftime('%Y-%m-%d %H:%M')}"
            + (f" · 技能包已落盘 {base}/技能包/" if skill_files else "")
            + "\n\n")
    md = head + "## 全书骨架\n\n" + skeleton + "\n\n---\n\n"
    for stitle, _name, _scope, sk in skills_md:
        md += f"## 🧩 技能：{stitle}\n\n{sk}\n\n---\n\n"
    md += ("## 怎么用\n\n"
           f"本次蒸出 **{len(skills_md)}** 个技能包"
           + ("，已落盘至 `reports/` 下对应目录" if skill_files else "") + "：\n\n"
           + "\n".join(f"- `{nm}` — {st}（{sc}）"
                       for st, nm, sc, _ in skills_md)
           + "\n\n1. 每个技能是独立文件夹（内含 SKILL.md），可直接导入支持"
             " SKILL.md 规范的 AI 助手；\n"
           "2. 建议一次只开当前用得上的技能，装多个不等于开多个；\n"
           "3. 判断技能用途先看 frontmatter 的 description"
           "（写了什么时候用、什么时候不用）；\n"
            "4. 报告内「全书骨架」是所有技能共享的判据来源，技能之间有冲突时以骨架为准；\n"
           "5. `darwin_eval.json` 是评测用例、`darwin_eval_result.json` 是本机闭环评测结果"
           "（darwin-skill 兼容格式）——把两者一起交给 darwin-skill 即可自动进化"
           "（评估→改进→盲测→保留/回滚）。\n")
    md += _darwin_render(eval_results, eval_avg, fixed)
    structured = {"title": title, "mode": "distill", "model": model,
                  "nchar": core["nchar"], "nchunks": len(core["chunks"]),
                  "intervals": intervals, "cards": core["cards"],
                  "is_narrative": False,
                  "dtype": core.get("dtype", "general"),
                  "dtype_conf": core.get("dtype_conf", ""),
                  "skills": [st for st, _n, _s, _sk in skills_md],
                  "skill_files": skill_files,
                  "darwin_eval_n": sum(len(c.get("cases") or []) for c in eval_cases),
                  "darwin_eval_avg": eval_avg,
                  "darwin_regenerated": fixed,
                  "distill_dir": os.path.basename(base),
                  "cand": {k: len(v) for k, v in cand.items()},
                  "report": md}
    return md, structured


# ---------------------------------------------------------------- persona 人物精读模式
# 2026-10-24 嫁接 nuwa-skill 思想：蒸馏人而非书。
# 访谈/传记/自述/演讲稿 → 「认知操作系统卡」：思维框架/决策模式/表达DNA/
# 反共识观点/盲点争议 + 逐字金句（回原文校验），并落盘可导入的人物 SKILL.md。
# 与 distill 互补：distill 蒸"书本方法"，persona 蒸"人物思维"。

PERSONA_SYSTEM = (
    "你是严谨的人物认知蒸馏助手（nuwa 思想：蒸馏思维方式，不是写吹捧小传）。"
    "只依据给定素材提炼，每个字段都要有原文依据；素材不足的字段如实留空数组，"
    "严禁编造人物没说过的话、没表达过的观点。"
)

PERSONA_PROMPT = """基于《{title}》的下列素材，把说话人/主人公蒸馏成一张「认知操作系统卡」。只输出 JSON（不要其他内容）：

{{
  "identity": "一句话身份+底色（≤40字）",
  "mental_models": [
    {{"name": "思维模型名（他看世界用的一把尺子）",
      "core": "内核一句话",
      "trigger": "什么问题/场景出现时该启用这把尺子（写成用户会遇到的情境，具体）",
      "steps": ["按这个模型分析问题的操作步骤，2-4步，每步可照做"],
      "pass_criteria": "用对了会看到什么（可检验的标志：产出物/判断形式/结论形状）",
      "misuse": "最容易用错的方式（素材撑不起来就空串）",
      "evidence": "原文依据（概述或短摘录，须能在素材中找到）"}}],
  "decisions": [
    {{"situation": "他面临X时", "tendency": "倾向做Y（具体动作）", "basis": "原文依据"}}],
  "dna": {{"sentence": "句式特征", "catchphrase": "口头禅/高频词",
           "argument": "论证风格（数据驱动/类比/讲故事/先破后立…）",
           "avoid": "他从不说/回避的表达"}},
  "contrarian": [
    {{"view": "反共识观点", "reason": "他的理由", "evidence": "原文依据"}}],
  "blindspots": ["盲点/争议/适用局限（2-4条，基于素材能指出什么就写什么）"],
  "quotes": ["金句（必须逐字摘自素材）"]
}

数量要求：mental_models 3-5 个；decisions 3-6 条；contrarian 0-4 条（素材撑不起来就空）；quotes 3-8 条。
硬性要求：evidence/quotes 必须能在素材中找到依据；quotes 一字不改。
RIA 要求：trigger/steps/pass_criteria 必须从素材中他实际的做法里归纳，不是通用方法论套话；
素材撑不出操作步骤的模型，steps 如实留空数组，不要编通用步骤凑数。

素材（分段小结与原文摘录）：
{materials}"""

# nuwa 三重验证（2026-10-24 补全女娲嫁接的另一半——此前只借了"认知卡"概念，
# 没接它真正的差异化能力：跨域复现 / 问题生成力 / 认知排他性）。
PERSONA_VALIDATE_PROMPT = """你是苛刻的认知蒸馏验证员，对下面这张「认知操作系统卡」执行 nuwa 三重验证，只输出 JSON（不要其他内容）：

{
 "cross_domain": {"score": 1-5, "note": "跨域复现力：把卡里的思维框架迁移到它原本领域之外（例如把投资框架用在带团队/做产品上），还能自洽运转吗？note 必须指出哪几条能迁移、哪几条会断裂，并各举一例"},
 "question_power": {"score": 1-5, "note": "问题生成力：用这张卡能提出多少有锋利度的新问题？note 里给出 2 个最好的问题原句"},
 "exclusivity": {"score": 1-5, "note": "认知排他性：把人物名字遮掉，这套思维还有辨识度吗？还是通篇是任何成功学都有的正确废话？note 必须引用卡里具体条目作证据"},
 "verdict": "PASS / REVISE / FAIL 三选一"
}

打分纪律：4 分及以上必须举出卡里的具体条目；note 写不出具体证据就老实给低分；不写恭维话。

【认知卡】
{card}"""

# 人物卡 darwin 评测用例生成（2 条正常 + 1 条边界；失败退化为确定性用例）
PERSONA_EVAL_PROMPT = """基于下面这张人物认知卡，出 3 条 darwin 评测用例，只输出 JSON 数组（不要其他内容）：
[{"capability": "persona", "cases": [
   {"input": "正常请求①：要求用卡里某条思维模型分析一个具体问题（input 要像真实用户说的话）",
     "expected": "合格回答必须调用卡中哪条/哪几条模型（引用条目名），按其触发场景与步骤展开"}},
   {"input": "正常请求②：要求模拟此人面对某取舍的决策或按其表达风格输出",
     "expected": "合格回答须符合卡中决策模式/表达DNA，不引入卡外倾向"}},
   {"input": "边界请求：问一个卡中明确未涉猎或不适用的领域",
     "expected": "正确行为：指出超出卡片依据范围并拒答或说明局限，不强行套用"}}]]

expected 必须可对照检验（引用卡中具体条目名）。

【认知卡】
{card}"""


def _persona_render(title: str, p: dict, quote_marks: dict) -> str:
    """认知卡 JSON → Markdown 报告正文。quote_marks: 金句→是否逐字命中原文。"""
    def _list(items, render):
        return "\n".join(render(i) for i in items) or "（素材不足，未提炼）"
    md = f"## 身份底色\n\n{p.get('identity') or '（未提炼）'}\n"
    md += "\n## 思维框架（mental models · RIA 可执行化）\n\n" + _list(
        p.get("mental_models") or [],
        lambda m: (f"- **{m.get('name','')}**：{m.get('core','')}\n"
                   f"  - 🔔 触发场景：{m.get('trigger') or m.get('usage','') or '（未提炼）'}\n"
                   f"  - 🪜 操作步骤：{' → '.join(m.get('steps') or []) or '（素材不足以支撑步骤，未提炼）'}\n"
                   f"  - ✅ 验收标准：{m.get('pass_criteria','') or '（未提炼）'}\n"
                   f"  - ⚠️ 常见误用：{m.get('misuse','') or '（未提炼）'}\n"
                   f"  - 原文依据：{m.get('evidence','')}")) + "\n"
    md += "\n## 决策模式\n\n" + _list(
        p.get("decisions") or [],
        lambda d: f"- 面临**{d.get('situation','')}** → 倾向{d.get('tendency','')}"
                  f"（依据：{d.get('basis','')}）") + "\n"
    dna = p.get("dna") or {}
    if dna:
        md += ("\n## 表达 DNA\n\n"
               f"- 句式：{dna.get('sentence','') or '（未提炼）'}\n"
               f"- 口头禅/高频词：{dna.get('catchphrase','') or '（未提炼）'}\n"
               f"- 论证风格：{dna.get('argument','') or '（未提炼）'}\n"
               f"- 回避的表达：{dna.get('avoid','') or '（未提炼）'}\n")
    if p.get("contrarian"):
        md += "\n## 反共识观点\n\n" + _list(
            p["contrarian"],
            lambda c: f"- **{c.get('view','')}**——{c.get('reason','')}"
                      f"（依据：{c.get('evidence','')}）") + "\n"
    md += "\n## 盲点与争议\n\n" + _list(
        p.get("blindspots") or [], lambda b: f"- {b}") + "\n"
    md += "\n## 金句（逐字校验）\n\n" + _list(
        p.get("quotes") or [],
        lambda q: f"- 「{q}」" + quote_marks.get(q, "")) + "\n"
    return md


def _persona_validation_md(v: dict) -> str:
    """nuwa 三重验证结果 → 报告 Markdown 段落。"""
    if not v:
        return ""
    def _one(key, label):
        it = v.get(key)
        if not isinstance(it, dict):
            return f"- **{label}**：（未验证）"
        return f"- **{label}**：{it.get('score', '?')}/5 —— {it.get('note', '')}"
    icon = {"PASS": "🟢", "REVISE": "🟡", "FAIL": "🔴"}.get(
        str(v.get("verdict", "")).strip().upper()[:6], "⚪")
    return (f"\n## {icon} nuwa 三重验证（{v.get('verdict', '')}）\n\n"
            + _one("cross_domain", "跨域复现力") + "\n"
            + _one("question_power", "问题生成力") + "\n"
            + _one("exclusivity", "认知排他性") + "\n"
            + "\n> 验证方法嫁接自 nuwa-skill；分数是本机 14b 评判，仅供参考。\n")


def deepread_persona(text: str, title: str = "未命名", model: str = MODEL,
                     progress=None, dtype: str = ""):
    """persona 人物精读：访谈/传记/自述 → 认知操作系统卡 + 人物 SKILL.md。
    返回 (报告md, structured)。嫁接 nuwa-skill 思想，金句回原文逐字校验。"""
    progress = progress or (lambda *a: None)
    core = _prepare_core(text, title, model, "persona", progress,
                         story=False, dtype=dtype)
    n = len(core.get("intervals") or [])
    mats = _seg_materials(core, list(range(1, n + 1)) if n else [],
                          budget=12000)
    if len(mats) < 500:                       # 小结太粗 → 直接喂原文块
        mats = "\n\n".join(str(c) for c in (core.get("chunks") or []))[:12000]

    progress("persona", 1, 2)
    # prompt 含大量 JSON 花括号模板，不能用 .format()（单}会炸），改用 replace 传参
    def _gen_card(mats_text: str, fix_note: str = ""):
        prompt = PERSONA_PROMPT.replace("{title}", title).replace(
            "{materials}", mats_text + fix_note)
        return extract_json(chat(prompt, system=PERSONA_SYSTEM, json_mode=True,
                                 model=model, num_ctx=16384, temperature=0.3))

    def _quote_check(card: dict) -> dict:
        qm = {}
        for q in (card.get("quotes") or []):
            qm[q] = "✅ 已逐字命中原文" \
                if re.sub(r"\s+", "", str(q)) in norm else "⚠️ 未能在原文逐字命中"
        return qm

    j = _gen_card(mats)
    if not isinstance(j, dict):
        raise ValueError("人物卡 JSON 解析失败")

    # 金句回原文逐字校验（复用蒸馏模式的防编造底线）
    norm = re.sub(r"\s+", "", text)
    quote_marks = _quote_check(j)
    n_hit = sum(1 for v in quote_marks.values() if v.startswith("✅"))

    # 3b) darwin 评测闭环：人物卡出题（2 正常 + 1 边界）→ 装卡作答 → 打分 → 低分重生成
    pe_cases = []
    try:
        ev = extract_json(chat(PERSONA_EVAL_PROMPT.replace(
            "{card}", json.dumps(j, ensure_ascii=False)),
            model=model, num_ctx=8192, temperature=0.2))
        if isinstance(ev, list) and ev and isinstance(ev[0], dict) and ev[0].get("cases"):
            pe_cases = ev
    except Exception as e:                                       # noqa: BLE001
        print(f"[persona] 评测用例生成失败，退化为确定性用例: {e}")
    if not pe_cases:
        mm_names = "、".join(str(m.get("name") or "")
                             for m in (j.get("mental_models") or [])[:3]) or "其思维框架"
        pe_cases = [{"capability": "persona", "cases": [
            {"input": f"请用他的思维框架（{mm_names}）分析一个我正在纠结的决定",
             "expected": "须调用卡中至少一条思维模型并按其触发场景/步骤展开，"
                         "结论形状符合该模型的验收标准"},
            {"input": "模拟他在重大取舍面前的决策倾向，并说明理由",
             "expected": "须依据卡中决策模式/反共识观点，不得编造卡中没有的倾向"},
            {"input": "请他用这套思维预测明天的股市大盘走势",
             "expected": "正确行为：指出该请求超出卡片依据范围（卡中无此能力依据），不强行套用"}]}]
    p_skills = [(str(title)[:30] or "人物", "persona",
                 str(j.get("identity") or "")[:80],
                 json.dumps(j, ensure_ascii=False)[:6000])]
    eval_results, eval_avg, fixed = [], 0.0, []
    try:
        eval_results, eval_avg = _darwin_evaluate(p_skills, pe_cases, model, progress)
        if eval_results and eval_avg < DARWIN_REGEN_THRESHOLD:
            progress("regen", 1, 1)
            fails = "\n".join(f"- 输入「{x['input'][:70]}」得 {x['score']} 分：{x['reason']}"
                              for r in eval_results for x in r["cases"])
            j2 = _gen_card(mats, DARWIN_FIX_NOTE.format(avg=eval_avg, fails=fails[:1200]))
            if isinstance(j2, dict) and j2.get("mental_models"):
                j = j2
                quote_marks = _quote_check(j)
                n_hit = sum(1 for v in quote_marks.values() if v.startswith("✅"))
                fixed.append("persona")
                p_skills[0] = (p_skills[0][0], "persona",
                               str(j.get("identity") or "")[:80],
                               json.dumps(j, ensure_ascii=False)[:6000])
                eval_results, eval_avg = _darwin_evaluate(
                    p_skills, pe_cases, model, progress)
    except Exception as e:                                       # noqa: BLE001
        print(f"[persona] darwin 评测闭环失败（不影响主产物）: {e}")

    # 3c) nuwa 三重验证（对最终卡；验证结论只如实上报，不自动改写内容）
    validation = {}
    try:
        progress("nuwacheck", 1, 1)
        vj = extract_json(chat(PERSONA_VALIDATE_PROMPT.replace(
            "{card}", json.dumps(j, ensure_ascii=False)),
            model=model, num_ctx=8192, temperature=0.2))
        if isinstance(vj, dict) and vj.get("verdict"):
            validation = vj
    except Exception as e:                                       # noqa: BLE001
        print(f"[persona] 三重验证失败: {e}")

    body = _persona_render(title, j, quote_marks) + _persona_validation_md(validation)

    # 落盘：认知卡 + 可导入的人物 SKILL.md（nuwa 产物形态）
    base = os.path.join("reports", "persona_" + time.strftime("%Y%m%d_%H%M%S")
                        + "_" + re.sub(r"[^\w\-]+", "_", title)[:24])
    skill_files = []
    try:
        os.makedirs(base, exist_ok=True)
        ident = str(j.get("identity") or title)[:60]
        skill_body = (
            f"---\nname: persona-{re.sub(r'[^a-z0-9\-]+', '', title.lower())[:32] or 'figure'}\n"
            f"description: 蒸馏自《{title}》的人物认知卡（{ident}）。"
            f"当你想用此人的思维框架分析问题、模拟其决策视角、或按其表达风格输出时调用；"
            f"涉及此人没有涉猎的领域不要用它。\n---\n\n"
            f"# 人物认知卡：{title}\n\n" + body)
        open(os.path.join(base, "SKILL.md"), "w", encoding="utf-8").write(skill_body)
        with open(os.path.join(base, "认知卡.json"), "w", encoding="utf-8") as f:
            json.dump({**j, "validation": validation}, f, ensure_ascii=False, indent=2)
        skill_files.append(os.path.basename(base) + "/SKILL.md")
    except OSError as e:
        print(f"[persona] 落盘失败: {e}")
    if eval_results:
        try:
            with open(os.path.join(base, "darwin_eval_result.json"), "w",
                      encoding="utf-8") as f:
                json.dump(_darwin_result_json(title, "persona", eval_results,
                                              eval_avg, fixed), f,
                          ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"[persona] 评测结果落盘失败: {e}")

    md = (f"# DeepRead 人物精读 · {title}\n\n"
          f"> 模型 {model} · 全文 {core['nchar']} 字 · 金句逐字命中 {n_hit}/"
          f"{len(quote_marks)} · {time.strftime('%Y-%m-%d %H:%M')}\n"
          f"> 蒸馏思想嫁接自 nuwa-skill（蒸馏人而非书）；与萃猫蒸馏（蒸书本方法）互补\n\n"
          + body
          + _darwin_render(eval_results, eval_avg, fixed)
          + (f"\n\n---\n\n## 产物\n\n人物 SKILL.md 已落盘：`{skill_files[0]}`"
             "——可直接导入支持 SKILL.md 规范的 AI 助手，让助手以该人物的"
             "思维框架与决策模式对话。\n" if skill_files else ""))
    structured = {"title": title, "mode": "persona", "model": model,
                  "nchar": core["nchar"], "nchunks": len(core["chunks"]),
                  "intervals": core["intervals"], "cards": core["cards"],
                  "is_narrative": core.get("is_narrative", False),
                  "dtype": core.get("dtype", "general"),
                  "dtype_conf": core.get("dtype_conf", ""),
                  "quote_hit": f"{n_hit}/{len(quote_marks)}",
                  "persona": j,
                  "nuwa_validation": validation,
                  "darwin_eval_avg": eval_avg,
                  "darwin_regenerated": fixed,
                  "skill_files": skill_files,
                  "persona_dir": os.path.basename(base),
                  "report": md}
    return md, structured


# ---------------------------------------------------------------- book 模式

CHAP_RE = re.compile(
    r"(?m)^[ \t]*(第[零一二三四五六七八九十百千两0-9]{1,6}[章回节卷部篇][^\n]{0,40})$")


def split_book(text: str):
    """把整书切成若干部：优先按章节标记，缺失则按字数均分。返回 [(名称, 正文)]。"""
    marks = list(CHAP_RE.finditer(text))
    parts = []
    if len(marks) >= 3:
        for i, m in enumerate(marks):
            start = m.start()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            body = text[start:end].strip()
            name = m.group(1).strip()[:30]
            if text_len(body) >= 150:
                parts.append([name, body])
    if len(parts) < 3:                                   # 章节标记不足 → 按字数
        parts = []
        paras = re.split(r"(?<=\n)", text)
        cur, idx = "", 0
        for p in paras:
            cur += p
            if text_len(cur) >= BOOK_PART_CHARS:
                idx += 1
                parts.append([f"第{idx}部", cur.strip()])
                cur = ""
        if text_len(cur) > 200:
            idx += 1
            parts.append([f"第{idx}部", cur.strip()])
        return parts
    # 章节粒度整理：合并过小章节，切分超大章节
    def _span(a, b):
        return a if a == b else f"{a}~{b}"
    merged = []
    buf_name, buf_last, buf = None, None, ""
    for name, body in parts:
        L = text_len(body)
        if buf_name is None:
            buf_name, buf_last, buf = name, name, body
            continue
        if text_len(buf) + L <= BOOK_PART_CHARS * 1.2 or text_len(buf) < 1500:
            buf += "\n\n" + body
            buf_last = name
        else:
            merged.append([_span(buf_name, buf_last), buf])
            buf_name, buf_last, buf = name, name, body
        while text_len(buf) > BOOK_PART_CHARS * 2:       # 超大章节硬切
            cut = buf[:int(BOOK_PART_CHARS * 1.5)]
            merged.append([buf_name + "（上）", cut])
            buf = buf[len(cut):]
            buf_name = buf_last = name + "（下）"
    if buf_name:
        merged.append([_span(buf_name, buf_last), buf])
    return merged


def deepread_book(text: str, title: str = "未命名", model: str = MODEL,
                  progress=None, critique: bool = False,
                  story_min: int = STORY_MIN_CHARS, dtype: str = ""):
    """整书精读：分部 → 逐部深读（含部梗概）→ 总编汇总 + 全书梗概。"""
    progress = progress or (lambda *a: None)
    if not dtype:
        dtype, conf, method = classify_text(text, progress=progress)
        print(f"[type] {TYPE_LABELS.get(dtype, dtype)} (置信度 {conf} · {method})")
    else:
        dtype = dtype if dtype in TYPE_LABELS else "general"
    parts = split_book(text)
    truncated = False
    if len(parts) > BOOK_MAX_PARTS:
        parts = parts[:BOOK_MAX_PARTS]
        truncated = True
    n_all = len(parts)
    # 萃猫 R1：全书分析卡一次生成，注入所有分部（称谓/术语一致性）
    brief = compose_book_brief(text, title, model, progress)
    part_blocks, part_digests, metas = [], [], []
    for i, (name, body) in enumerate(parts):
        progress("book", i, n_all)
        core_i = pipeline_core(body, f"{title}·{name}", model,
                               lambda *a: progress("book", i, n_all),
                               dtype=dtype)
        digest = ""
        try:
            digest = compose_story_summary(core_i, model, BOOK_PART_MIN,
                                           lambda *a: progress("book", i, n_all))
        except Exception as e:
            digest = ""
            core_i["story_error"] = str(e)[:200]
        if not digest:
            digest = "\n\n".join(core_i["intervals"])[:1500]
        core_i["text"] = body
        core_i["dtype"] = dtype
        core_i["dtype_conf"], core_i["dtype_method"] = "", ""
        core_i["is_narrative"] = (dtype == "novel")
        core_i["brief"] = brief
        core_i["characters"] = (aggregate_characters(core_i["cards"])
                                if core_i["is_narrative"] else [])
        core_i["quote_check"] = check_quotes(body, core_i["cards"])
        core_i["harvested"] = harvest_quotes(body, 8)
        try:
            report_i = compose_final(core_i, "deep", model)
        except Exception as e:
            report_i = f"（本部报告生成失败：{e}）"
        report_i += _quality_section(report_i)
        part_digests.append(f"【{name}】\n{digest.strip()}")
        part_blocks.append(f"### 附录 · {name}（{core_i['nchar']} 字）\n\n{report_i}")
        metas.append({"name": name, "nchar": core_i["nchar"],
                      "summary": digest.strip(), "report": report_i})
    progress("book", n_all, n_all)

    # 全书梗概（目标字数 story_min，素材不足时诚实标注而非编造填充）
    try:
        whole = compose_longform_summary(title, part_digests, model, story_min,
                                         progress, "story",
                                         ntext=_norm(text))
    except Exception as e:
        whole = f"（全书梗概生成失败：{e}）"

    # 总编：虚构类→章节脉络/人物谱系/主题母题；非虚构类→五块蒸馏结构
    materials = ("\n\n【全书分析卡】\n" + brief[:1200]) if brief else ""
    materials += "\n\n".join(f"【{m['name']}】{m['summary'][:400]}" for m in metas)
    outline_prompt = BOOK_OUTLINE_PROMPT if dtype == "novel" else BOOK_DISTILL_PROMPT
    try:
        outline = chat(outline_prompt.format(title=title, materials=materials),
                       model=model, num_ctx=8192)
    except Exception as e:
        outline = f"（总编汇总生成失败：{e}）"

    head = (f"# DeepRead book 报告 · {title}\n\n"
            f"> 模型 {model} · 全文 {text_len(text)} 字 · 共 {len(parts)} 部 · "
            f"{time.strftime('%Y-%m-%d %H:%M')} · 分部深读+总编汇总"
            + ("　⚠️ 已按 DEEPREAD_BOOK_MAX_PARTS 截断，仅处理前 %d 部" % len(parts)
               if truncated else "") + "\n\n")
    wn = text_len(whole)
    wtag = ""
    if wn < story_min:
        wtag = f"（{wn} 字；未达 {story_min} 字下限——素材不足以在不编造的前提下继续加长）"
    elif wn > STORY_MAX_CHARS + 100:
        wtag = f"（{wn} 字；超出 {STORY_MAX_CHARS} 字上限，精简未成功）"
    md = (head + f"## 全书故事梗概{wtag}\n\n" + whole.strip()
          + "\n\n---\n\n" + outline.strip() + "\n")
    md += _quality_section(whole.strip() + "\n\n" + outline.strip())
    if critique:
        pseudo_core = {"title": title, "nchar": text_len(text),
                       "intervals": [m["summary"][:600] for m in metas],
                       "model": model}
        md += "\n---\n\n" + adversarial_review(pseudo_core, md, model, progress)
    md += "\n---\n\n## 分部精读报告（附录）\n\n" + "\n\n---\n\n".join(part_blocks) + "\n"
    structured = {"title": title, "mode": "book", "model": model,
                  "nchar": text_len(text), "nparts": len(parts),
                  "truncated": truncated, "parts": metas,
                  "story_summary": whole, "report": md}
    return md, structured


# ================================================================ cli

def main():
    global STORY_MIN_CHARS
    ap = argparse.ArgumentParser(description="DeepRead 长文精读")
    ap.add_argument("input", help="文件路径(txt/md/pdf/docx/图片)，或 - 代表 stdin")
    ap.add_argument("--mode", default="deep", choices=ALL_MODES)
    ap.add_argument("--title", default=None)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--out", default=None, help="输出 md 路径（默认 reports/）")
    ap.add_argument("--critique", action="store_true", help="附加对抗式复核")
    ap.add_argument("--no-ocr", action="store_true", help="关闭扫描件 OCR 兜底")
    ap.add_argument("--no-story", action="store_true", help="关闭叙事文本故事梗概")
    ap.add_argument("--story-min", type=int, default=STORY_MIN_CHARS,
                    help=f"故事梗概最小字数（默认 {STORY_MIN_CHARS}）")
    ap.add_argument("--type", default="", choices=[""] + list(TYPE_LABELS),
                    help="手动指定文本类型（默认自动识别）")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    text = (sys.stdin.read() if args.input == "-"
            else read_any(args.input, ocr=not args.no_ocr))
    title = args.title or (os.path.basename(args.input) if args.input != "-"
                           else "粘贴文本")

    def progress(stage, done, total):
        print(f"[{stage}] {done}/{total}", flush=True)

    t0 = time.time()
    STORY_MIN_CHARS = args.story_min
    md, st = deepread_text(text, title, args.mode, args.model, progress,
                           critique=args.critique, story=not args.no_story,
                           dtype=args.type)
    if args.mode == "notes":
        # notes 是多文件导出（cards/时间线/索引），目录已在引擎内落盘
        out_dir = st.get("out_dir", "")
        print(f"OK notes -> {out_dir}")
        print(f"   实体卡片 {st.get('entities', 0)} 张 · 时间线事件 "
              f"{st.get('timeline', 0)} 条 · 文件 {len(st.get('files', []))} 个")
        if args.out:                      # 可选：单独再存一份索引 md
            open(args.out, "w", encoding="utf-8").write(md)
            print(f"   索引副本 -> {args.out}")
    else:
        out = args.out or os.path.join(
            ROOT, "reports",
            f"{time.strftime('%Y%m%d_%H%M%S')}_{title[:20]}_{args.mode}.md")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "w", encoding="utf-8").write(md)
        print(f"OK {out}  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
