# DeepRead · 交接手册（给 DeepRead长文精读 空间的执行 Agent）

> 交接日期：2026-10-16 23:20 ｜ 移交人：WorkBuddy 伙计（主空间）｜ 上级文档：PROJECT.md
> **接手第一步：读完 PROJECT.md 再回来读这份。本服务可能正在运行（127.0.0.1:8790），动代码前先确认。**
> 当前版本 **v0.4**（2026-10-17：类型自适应精读，见 PROJECT.md §二）。

## 0. Owner 偏好（必须遵守）

- 中文，直接给结论，不废话；结构化输出（表格/emoji 信号灯）
- 无人值守习惯：不问、用默认并显式声明、不重复已完成的事
- **汇报前必须读源码/实测核验，不靠猜测**；诚实上报疏忽与风险
- 本地优先，拒绝付费云 API（全链 Ollama 本机推理）
- **改界面后必须实测**（curl 接口 + 浏览器验证），不报没验证的成果
- **报告质量要求高**：老吴 2026-10-16 深夜明确指出"31k 字小说的 deep 精读过于粗糙，**起码要有不少于 1500 字的故事梗概**，然后才是原有精读内容"。梗概是硬指标，不能退回。
- **2026-10-17 老吴提供权威书评对照**（tests/golden_金锁记_核心总结与梗概.md，含 6 维评分表）：**每次引擎改进后重跑金锁记跑 tests/_golden_check.py 对照打分，不凭感觉说"变好了"**。写作类对照改进工作流：金标准样本 → 逐维度对比 → 差距落成 prompt/结构 → 回归打分。

## 1. 项目现状（截至 2026-10-17 15:35）

- **v0.4 类型自适应精读（2026-10-17，金锁记 31k 实测 894s，golden 6/6 自动达标）**：
  - ✅ **classify_text 三级类型识别**（规则千字特征需显著领先 → 低置信走 qwen2.5:7b 采样 → 回退）：实测金锁记→novel(0.88·规则)、书评→comment(0.57·规则)、学术论文→academic(0.75·LLM)；`--type` 手动覆盖；结果写报告头
  - ✅ **TYPE_PROFILES 八类型档案**：每类型 = Stage B 提取侧重 + deep 专属成稿结构（TYPE_SPECS）+ 程序侧预筛器（novel 意象段/comment 论据段/history 年代段…top-10~12 原文段落）
  - ✅ **两遍读·原文通道成稿**：31k→成稿压缩比曾 15:1（成稿只见 6% 原文=幻觉根源），现成稿素材 = 故事梗概(原文通道) + 预筛原文段落 + 数据聚合 + 分段小结
  - ✅ **上下文治理**：按阶段分配 ctx（map 4096/reduce 8192/合成 12288），超限自动提升(≤16384)+显式报警；**教训：v4 首跑曾把 map 也提到 16384 → KV cache 翻倍 → 12GB 卡换页 → 713s 暴涨到 2121s，已修复回 894s**
  - ✅ **梗概也注入人物事实清单**（v4b 实测"二爷姜季泽"硬伤后补的防线）+ 梗概强制因果链（"谁因什么做了什么导致什么"）
  - ✅ **深度审查员**（第四评审：逼问更深读解）+ Web 长文本(>2万字)默认勾选对抗复核
  - ✅ **golden 回归**：tests/golden_金锁记_核心总结与梗概.md（老吴提供的达标线）+ tests/_golden_check.py（6 维自动打分）；v3 基线 4/6 → v4c **6/6**
- ⚠️ **已知残余（诚实标注）**：人物表"身份与关系"列仍有幻觉（v4c 出现"季泽=七巧的侄子""兰仙的姐夫"错乱）——已加"关系列必须逐字照抄人物事实清单措辞"约束（TYPE_SPECS novel），**尚未回归验证**，下次跑金锁记时用 golden check 复核
- **未做（老吴明确指示不做）**：info-hub 摘要子系统接入、开机自启；v0.4 备选未做：全读模式（7b@32k，VRAM 需实测）、SeqGPT-560M
- 界面已无头浏览器截图实测（5 模式卡、两个开关、批量提示均正常渲染）
- 侧栏已注册（"DeepRead 长文精读"，workspaces+sessions 双表）
  - ✅ 叙事文本自动前置**故事梗概**（**以原文为素材**分批叙述→合成→字数校验）—— 字数规格 2026-10-17 依老吴反馈改为 **600-1400 字归纳式**（故事轮廓+核心思想，非流水账；详见 PROJECT.md）
  - ✅ **金句 verbatim 校验**（三级匹配 逐字/近似/转述）+ 程序侧候选原句池；报告金句章节未命中行自动标 `⚠️（转述·非逐字原句）`（旧报告 5 条金句有 3 条是转述，现已被拦下）
  - ✅ **扫描件 PDF OCR 兜底**（qwen3-vl:8b，pypdfium2 渲染）—— 2 页扫描件实测原句命中 79%
  - ✅ **book 整书模式**（分章分部→逐部深读→总编汇总+全书梗概）—— 26,737 字切 3 部实测 846s，全书梗概 2,844 字；v2（原文素材）人物谱系已修正（v1 曾把姜季泽错写成七巧丈夫）
  - ✅ **对抗式总结**（证据审查员/逻辑审查员/反方辩护人 三视角挑刺 + 主编回应）—— 1,600 字实测 195s，评审能抓到报告改写原文的表述
  - ✅ **批量队列**（单 worker 串行，避免 Ollama 显存争抢）—— 2×1,400 字实测 95s，逐项状态灯实时更新
- 侧栏已注册（"DeepRead 长文精读"，workspaces+sessions 双表）
- **未做（老吴明确指示不做）**：info-hub 摘要子系统接入、开机自启

## 2. 环境要点（本项目踩过的坑，勿重踩）

| 坑 | 解法（已内置） |
|---|---|
| **Windows 注册表死代理**（127.0.0.1:64309 等）劫持 pip 和 urllib | 引擎用 `ProxyHandler({})` 直连 opener；**自写测试脚本请求本机服务也必须如此，否则 502 Bad Gateway**；pip 装 `--proxy ""`；tuna 源异常换阿里云 `--index-url https://mirrors.aliyun.com/pypi/simple/` |
| git-bash `/tmp` ≠ Windows Python 的 `/tmp`（不同盘！） | 传文件用显式 `E:/...` 路径或管道直通，别用 /tmp 中转 |
| `head -c N` 切多字节汉字产生坏字节 | 用 Python 按字符截断 |
| **线程内异常不冒泡到 HTTP 响应**（job 永远卡 running） | run_job/run_compare/队列 worker 均已 try/except 写回 state=error；新增后台线程照此办理，stderr 是唯一线索 |
| qwen3 思考内容污染输出 | `chat()` 已剥 `<think>`；`json_mode=True` 用 format json |
| Ollama 偶发 URLError | chat() 自带 2 次重试+退避（超时已放宽到 900s，OCR/长文更稳） |
| **Ollama 显存争抢**：qwen3:14b(9.3GB)+qwen3-vl:8b(6.1GB) > 12GB | 一切 LLM 调用必须**串行**：批量队列单 worker、对抗复核三次评审串行。切勿改成并发 |
| **`Start-Process` 起的常驻服务会被回收** | 用受管后台任务启动（run_in_background），或让老吴手动 `venv\Scripts\python.exe server.py` |
| **字符串里的中文引号** | deepread.py 有大段中文 prompt：单引号字符串内不要嵌 ASCII `"`，中文引号用 `“”`，否则 SyntaxError |

## 3. 服务与 API

- 启动：`cd E:\Workbuddy\DeepRead && venv\Scripts\python.exe server.py`（端口 8790，env 可改 DEEPREAD_PORT / DEEPREAD_MODEL）
- 接口一览（v0.3）：
  - `POST /api/read` {mode,title,text,critique,story} → 单模式任务（mode 可为 book）
  - `POST /api/compare` {title,text} → 四模式对比任务（含共用故事梗概）
  - `POST /api/upload` multipart（单文件；parse_only=1 只解析；支持图片类）
  - `POST /api/batch` multipart（**多文件批量入队**）→ {id,total,items}
  - `GET /api/batch/status?id=` → {state,done,total,items[{name,state,stage,file,error,nchar}]}
  - `POST /api/recommend` {text} → {mode,reason}
  - `GET /api/status?id=` / `GET /api/report?id=`（对比任务多 reports + summary 字段）
  - `GET /api/file?name=`（reports/ 白名单读取） / `GET /api/history`
  - `GET /api/health` → {ok,model,modes,ocr_model,version}
- 模块级接入：`from deepread import deepread_text, deepread_compare, deepread_book, recommend_mode`
  （注意 `deepread_compare` v0.3 起返回**三元组** `(reports, merged_md, story_summary)`）

## 4. 代码结构速查

- `deepread.py`：
  - readers：`read_any/read_pdf/read_docx/ocr_pdf/ocr_image`（PDF 按"每页平均字数<60"判扫描件 → 视觉 OCR）
  - `narrative_signals/is_narrative/recommend_mode`：叙事检测（引号密度/对话动词/时间词/人称密度/章节标记，≥2500字，阈值 3 分）
  - `verify_quote/check_quotes/harvest_quotes/annotate_quote_section`：金句三级校验 + 候选原句池 + 成稿后回标
  - `pipeline_core`（A切块/B逐块JSON/C间隔汇总）→ `compose_longform_summary`/`compose_story_summary`（梗概：分批→合成→`_expand_if_short` 字数硬校验）→ `compose_final`（D成稿，`FINAL_SPEC` 管输出结构）
  - `adversarial_review`（三视角评审 + 主编回应）；`split_book/deepread_book`（整书）
  - 入口：`deepread_text / deepread_compare / deepread_book`，CLI 在 `main()`
- `server.py`：ThreadingHTTPServer + JOBS（单任务）+ BATCHES/QUEUE（批量队列，`_queue_worker` 单 worker）+ `_save_report`（同秒重名自动加序号）
- `web/index.html`：单文件无依赖；`render()` 极简 MD 渲染器；对比页 `.cmp` 2x2 + `.storybox` 共用梗概；批量队列面板 `#queue`
- **改模式输出结构只动 `FINAL_SPEC`；改梗概字数阈值只动文件顶部常量/环境变量**

## 5. 待办（老吴已明确暂不做的除外）

- [ ] **info-hub 摘要子系统接入**（news 每日8:00 / AI-builders / invnews 换 `deepread_text` quick 模式）—— **老吴指示本轮不做**
- [ ] **开机自启**（wb-local-service-ops 规范）—— **老吴指示本轮不做**
- [ ] **代码 git 化**：建仓推 Gitee（landlord2003/DeepRead），.gitignore 排除 venv/reports，远端零令牌（内联用完即清）
- [ ] 可选增强：梗概人物身份准确度（把原文片段一并喂入，成本翻倍）；book 模式断点续跑（长任务中断即全废）

## 6. 验收标准（改完任何东西）

1. `curl /api/health` 通（应返回 version=0.3、modes 含 book）
2. 500 字文本单模式跑通（~60s）
3. 改过渲染器/模式结构：跑四模式对比看 2x2 页（含顶部梗概区块）
4. **改过梗概逻辑：必须跑一遍 3 万字级叙事文本，确认梗概 600-1400 字、无"起初/随后"式开头、结尾有核心思想归纳**
5. 汇报附实测数据，不猜
