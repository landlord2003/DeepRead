# DeepRead · 长文精读引擎

> 立项：2026-10-16 ｜ Owner：老吴 ｜ 执行：WorkBuddy 伙计
> 当前版本 **v0.6**（2026-10-24：三 skill 交叉整合补全——persona RIA 化 + nuwa 三重验证 + darwin 共享评测闭环（作答→打分→低分重生成），详见 HANDOVER §0.6）｜ 定位：一长段文字 → 高质量摘要 + 要点提炼，本机 LLM 推理，零外部 API

## 一、为什么自研（决策记录）

调研结论（2026-10-16）：
- **dsh-deepread**（59⭐ MIT）：五模式精读质量设计最好（观点-证据-数据-关系地图、置信度区分原意/推断），但绑 DeepSeek Harness/Claude Code 生态
- **ai-book-reader**（12⭐）：流水线骨架标准（TOC→chunk→间隔摘要→元摘要）但停更
- **SpineDigest**：对抗式多智能体总结，架构先进但极新
- GraphRAG/RAPTOR：为 RAG 问答设计，太重

**决策**：自研。MapReduce 流水线复用 Comic_Book step2_deepread 已验证架构，prompt 设计吸收 dsh-deepread 的质量思想（证据引用+置信度标注+无证据不编造），对抗式复核借鉴 SpineDigest。

## 二、架构（v0.4）

```
输入(txt/md/pdf/docx/图片/粘贴)
  → 图片型 PDF / 图片文件：qwen3-vl:8b 视觉 OCR 兜底
  → classify_text 类型识别（三级：规则启发式 → 低置信走 qwen2.5:7b 采样 → 失败回退）
     类型集：novel小说/prose散文/comment时评/history历史/academic学术/tech技术/report报告/speech演讲/general
  → 切块(~1500字) + TYPE_PROFILES[type].stageb 提取侧重注入
  → Stage B 逐块提取(JSON: 要点/实体/金句/数据/关系, 置信度标注)     ┐
  → Stage C 间隔汇总(每5块一段小结)                                 ├─ pipeline_core 公共深读
  └─ 金句 verbatim 校验(回原文三级匹配) + 程序侧候选原句池            ┘
       ↓
  ┌ novel → Stage S 故事梗概：原文分批叙述(≤6000字/批) → 合成 → 字数校验(600-1400 字，不足补写/超限 3 轮精简+摘录重组兜底)
  │         梗概定位=归纳大意：故事轮廓(3-5阶段)+核心思想归纳收尾；禁"起初/随后/此后/最终"机械开头
  │         防编造双守卫：①引语回原文逐字校验，未命中→改写修复 ②逐句 4-gram 回素材抽查，依据率低→删除/改写修复
  │         主题段保底：LLM 判定末段若为情节叙述则自动补写核心思想归纳段
  └ 成稿 Stage D  compose_final × N
        · deep 模式按类型选专属结构（TYPE_SPECS：人物表/论证链/时间线/五段法…）
        · 素材走【原文通道】（两遍读核心）：故事梗概 + 预筛原文段落 + 数据聚合 + 分段小结
        · num_ctx 16384 起步 + 超限自动提升(≤24576) + 显式报警（杜绝静默截断）
        └ 可选 Stage R 对抗式复核：四视角评审（证据/逻辑/反方/深度审查员）+ 主编回应
  book 模式：split_book 分章分部 → 每部重复上述 → 总编汇总(脉络/人物/主题) + 全书梗概
```

**两遍读（v0.4 质量核心）**：长文本 31k→成稿的压缩比曾达 15:1，成稿只见 6% 原文，细节靠模型脑补（幻觉根源）。v0.4 成稿素材改为：①novel 的「故事梗概」（原文批次叙述合成，忠实全文）②「预筛原文段落」（程序按类型特征正则海选：novel 意象句/comment 论据段/history 年代段…top-10~12 段）③数据/实体聚合 ④分段小结。四路合流，预算 15k 字，超限先裁预筛段。

| 文件 | 职责 |
|---|---|
| `deepread.py` | 核心引擎：readers(txt/md/pdf/docx/图片+OCR) + Ollama 调用 + pipeline_core/compose_final/deepread_text/deepread_compare/deepread_book + 叙事检测/梗概/金句校验/对抗复核 + recommend_mode + CLI |
| `server.py` | Web 服务：纯标准库 ThreadingHTTPServer，端口 8790，含**单 worker 批量队列** |
| `web/index.html` | 界面：5 模式(含★推荐角标)/粘贴/拖拽上传(单文件只解析，多文件入队)/进度条/报告渲染/四模式对比 2x2+共用梗概/批量队列面板/历史点击打开/导出 |
| `reports/` | 输出报告（md），文件名=时间_标题_模式；对比任务落盘 `*_对比.md` |
| `venv/` | 依赖：pypdf + pypdfium2 + pillow（后两个仅 OCR 路径用） |

## 三、模式一览

| 模式 | 输出 | 适用 |
|---|---|---|
| deep 深度精读 | 论述文：一句话+核心论点(纯编号列表)+论据与置信度表(引用不重复)+论证结构+金句+实体数据表；**叙事文本**：人物表+主题分层(角度互斥)+情节关键转折+艺术手法与意象+金句 | 论述文/报告/白皮书/小说 |
| quick 快速要点 | 一句话+≤10条要点 | 新闻/快讯/短文 |
| map 知识地图 | 观点-证据-数据表+关系网络+Mermaid | 叙事文本/小说/研究资料 |
| feynman 大白话 | 类比讲解+术语翻译表+追问问题 | 学习新领域 |
| **distill 萃猫蒸馏** | 全书骨架(主旨/结构/核心主张/关键概念/盲点边界) → 场景聚类 → N 个 SKILL.md 技能包（落盘 reports/distill_*/技能包/）+ 怎么用。**v0.5 技能卡升级为 RIA 六段结构**（R 触发/I 步骤/A 案例/E 验收标准/B 常见错误/来源引文，嫁接 cangjie-skill），落盘时同步生成 darwin 兼容评测用例 `darwin_eval.json`（嫁接 darwin-skill，可直接喂 darwin 自动进化） | 方法/经验类书籍，要把书变成可调用的 AI 技能 |
| **book 整书精读** | 全书故事梗概(600-1400字)+章节脉络+人物谱系+主题母题+分部报告附录；非虚构类总编走五块蒸馏结构（章节脉络/核心框架/实体术语/结论/使用边界）；萃猫 R1 全书分析卡（术语称谓表）注入各部保证一致 | >4万字长书 |
| **notes 纪传+时间线** | 纯叙事/历史书的读书笔记：复用 pipeline_core 的 `cards`（entities/relations 三元组），一次 LLM 聚合出「实体+时间线」JSON，落盘 Obsidian 纪传卡片（`[[双向链接]]`）+ mermaid 时间线 + 索引，目录 `reports/notes_*/` | 纪传体/编年史/故事化王朝史/传记等只讲"发生了什么"的书（萃猫会空，这里正合适） |
| **persona 人物精读** | 蒸馏人而非书（嫁接 nuwa-skill）：访谈/传记/自述 → 认知操作系统卡（身份底色/思维框架 3-5 个/决策模式/表达 DNA/反共识观点/盲点争议/金句逐字回原文校验），落盘 `reports/persona_*/`（认知卡.md + 认知卡.json + 可导入的人物 SKILL.md） | 访谈实录/人物传记/自述/演讲稿——想把一个人的思维方式变成 AI 可调用的角色卡 |
| ⚖️ 模式对比 | 公共深读一次+按所选模式各自成稿（六选 N，含 distill/book），网格 + 顶部共用故事梗概 | 选不准模式时 |

**类型自适应（v0.4）**：`classify_text()` 三级识别（规则千字特征打分且需显著领先 → 低置信走 qwen2.5:7b 采样首中尾各400字 → 失败回退），实测金锁记→novel(0.88·规则)、书评→comment(0.57·规则)、学术论文→academic(0.75·LLM)。deep 模式按类型切换成稿结构：

| 类型 | 成稿结构 |
|---|---|
| novel 小说 | 一句话(转变轨迹+统摄意象+表层深层三问)+人物表+主题分层+情节转折+艺术手法 |
| comment 时评 | 立场基调+论证链条(论据类型标注)+事实主张可核性表+修辞话术 |
| history 历史 | 时间线大事记表+人物势力+因果分析(原文明说 vs 推断)+史料可信度 |
| prose 散文 | 文眼主题+情感脉络+意象语言特色 |
| academic | 问题-方法-数据-结论-局限五段法 |
| tech | 步骤清单(命令原样)+配置坑表 |
| report | 核心结论+关键数据表+建议风险 |

类型识别结果写入报告头（置信度+判定方式）；`--type` 可手动覆盖；Stage B 提取侧重随类型注入。

**叙事文本自动增强**：novel 类型时 deep/map/book 自动前置故事梗概。字数规格（老吴 2026-10-17 修订）：**600-1400 字**（>500 且 <1500），定位是**归纳大意**——故事轮廓（3-5 个自然阶段）+ 结尾 2-4 句核心思想归纳，写主干不堆细节，禁"起初/随后/此后/最终"式机械开头；不足补写、超限自动精简（`STORY_CONDENSE_PROMPT`）。`--no-story` 关闭。

**智能推荐**（recommend_mode，纯启发式零成本）：<800字→quick；叙事文本→map；>6万字+章节体→book；<3000字→quick；更长论述文→deep。

## 四、三种用法

### 1. Web（推荐）
`python server.py` → http://127.0.0.1:8790
- 单文件拖入 = 只解析不启动；**多文件拖入 = 自动进入批量队列**（单 worker 串行，逐项状态灯，完成后点击打开）
- 两个开关：**对抗式复核**（+4 次模型调用）、**关闭故事梗概**

### 2. CLI
```bash
venv\Scripts\python.exe deepread.py 文件 --mode deep            # 默认叙事自动加梗概
venv\Scripts\python.exe deepread.py 书.txt --mode book          # 整书精读
venv\Scripts\python.exe deepread.py 扫描件.pdf --mode quick     # 自动 OCR 兜底
venv\Scripts\python.exe deepread.py 文章.txt --critique         # 附对抗式复核
venv\Scripts\python.exe deepread.py - --mode quick < 文章.txt   # stdin
```
其他参数：`--no-ocr` `--no-story` `--story-min 2000` `--model qwen2.5:7b`

### 3. 接入现有系统（模块级 API）
```python
from deepread import deepread_text, deepread_compare, deepread_book, recommend_mode
md, st = deepread_text(text, title="标题", mode="deep", critique=False)
reports, merged_md, story = deepread_compare(text, title="标题")   # v0.3: 返回三元组
md, st = deepread_book(text, title="书名")                          # 整书
mode, reason = recommend_mode(text)
# st 含 cards/intervals/report/is_narrative/story_summary/quote_check
```
可接入：video_distill 摘要环节、info-hub news/AI-builders 摘要子系统（**P1 未做，见路线图**）、电子图书馆书摘要。

## 五、环境要点（踩坑记录）

- **Windows 注册表死代理**（127.0.0.1:64309）劫持 pip/urllib：引擎内置 `ProxyHandler({})` 直连；**自写测试脚本请求本机服务也必须用空代理 opener**（否则 502）；pip 装 `--proxy ""`，tuna 源不通时用阿里云源
- git-bash `/tmp` ≠ Windows Python 的 `/tmp`：传文件用显式盘符路径或管道直通
- qwen3 思考内容：`chat()` 已做 `<think>` 剥离兜底
- OCR 需要 pypdfium2（渲染页）+ Pillow（PNG 编码），已装入 venv；缺失时 `read_pdf` 会**明确报错原因**而不是静默返回空文本
- **Ollama 必须串行访问**：qwen3:14b(9.3GB) + qwen3-vl:8b(6.1GB) 同抢 12GB VRAM 会导致反复换页。批量队列因此设计成单 worker；对抗式复核的三次评审也串行
- 模型可用环境变量切换：`DEEPREAD_MODEL`(默认 qwen3:14b)、`DEEPREAD_OCR_MODEL`(qwen3-vl:8b)；批处理/梗概/OCR 的阈值同理见 deepread.py 顶部常量

## 六、性能实测（RTX 5070 12GB，qwen3:14b）

| 输入 | 模式 | 耗时 | 产物 |
|---|---|---|---|
| 347 字 | 四模式对比 | ~2 min | 2x2 |
| 4,000 字 | quick | 84s | — |
| **31,119 字（金锁记）** | **deep + 梗概** | **714s** | **故事梗概 2,827 字** ✅ 人物出身/关系符合原著 |
| 26,737 字 / 切 3 部 | book 整书 | 846s | 全书梗概 2,844 字 + 章节脉络/人物谱系/主题 |
| 扫描件 PDF 2 页 | OCR + quick | OCR ~2 min | 转写 610 字，原句命中 **79%** |
| 1,600 字论述 | deep + 对抗复核 | 195s | 三视角评审逐条挑刺 + 主编回应 |
| 2 × 1,400 字 | 批量 quick（队列） | 95s | 单 worker 串行 2/2 成功 |

> 梗概素材曾用 Stage B 卡片（v1：697s / 1,635 字），实测会把卡片的身份误读放大并诱发编造细节，已废弃；现以原文为素材（v2），代价是梗概阶段多约 3 次调用（+20~30s）。

## 七、已知边界（诚实标注）

1. **梗概素材以原文为准（v0.3 深夜修正）**：初版让梗概吃 Stage B 卡片，结果卡片的人物误读被放大、又被字数指标逼着凑内容 → 出现"姜季泽是七巧丈夫"这类错误和编造细节。现改为**直接分批喂原文**（每批 ≤6000 字）写情节叙述再合成，并明确"不得添加原文/素材没有的内容"。**若素材确实不足以在不编造的前提下写满目标字数，报告标题会直接标注实际字数，不做编造填充**。
2. **金句选取**：已强制逐字回原文校验（三级匹配：逐字/近似/转述），报告"原文金句"章节中未能逐字定位的行会自动标注 `⚠️（转述·非逐字原句）`；同时提供程序侧"候选原句池"防止 LLM 交不出原句。仍可能出现文学性偏弱的句子（已加选取偏好但非硬约束）。
3. **短文本 deep 退化**：<8 千字叙事文本跑 deep，"论点"易写成琐碎罗列 —— 已由智能推荐规避（叙事→map），且 prompt 内含叙事专用改写指令。
4. **book 模式耗时是线性叠加**：book ≈ 部数 × 单部成本，10 万字书约 1.5-2.5 小时（受 `DEEPREAD_BOOK_MAX_PARTS`/`DEEPREAD_BOOK_PART_CHARS` 控制），必须后台跑。
5. **批量队列是内存队列**：服务重启即清空（不落盘），正在处理的项会丢失，需重新入队。
6. **OCR 有页数上限**（默认 40 页，`DEEPREAD_OCR_MAXPAGES`），超限会在文本头部明确标注"仅 OCR 前 N 页"。
7. **外部知识不在职责内（v0.3.1 对标书评后明确）**：创作背景、文学史评价（如傅雷/夏志清评语）等超出原文的信息，引擎不会生成——编造风险不可控。需要此类内容时把权威书评/百科文本与原文一起送入或另行查阅。
8. **人物事实清单的覆盖面**：清单由 Stage B 逐段卡片聚合，出现少于 2 个段落块的人物不会入清单（防噪音）；极次要人物的身份仍以梗概与分段小结为准。

## 八、路线图

- **P0（完成 2026-10-16）**：四模式引擎 + CLI + Web 界面 + 智能推荐 + 四模式对比 + 侧栏注册
- **P0-fix（完成 2026-10-16 深夜）**：叙事文本故事梗概前置（初版 ≥1500 字；2026-10-17 依老吴反馈改为 600-1400 字归纳式梗概）
- **P1/P2（完成 2026-10-16 深夜）**：✅ 金句 verbatim 校验 ｜ ✅ 扫描件 OCR 兜底 ｜ ✅ book 整书模式 ｜ ✅ 对抗式总结 ｜ ✅ 批量队列 ｜ ⏸ **info-hub 接入 / 开机自启（按老吴指示暂不做）**
- **v0.3.1（完成 2026-10-17 上午）**：对标权威书评 → 叙事 deep 专属结构 + 人物事实清单 + 论据去重
- **v0.4（完成 2026-10-17）**：类型自适应精读（classify_text 三级识别 + TYPE_PROFILES 八类型 + 两遍读原文通道成稿 + num_ctx 16384/超限报警 + 深度审查员第四评审 + 长文本默认对抗复核 + golden 回归基准 tests/_golden_check.py）
- **v0.4 备选（未做）**：全读模式（qwen2.5:7b@32k 单次全文读，VRAM 需实测）｜ SeqGPT-560M 分类模型 ｜ 代码 git 化（建仓推 Gitee `landlord2003/DeepRead`，.gitignore 排除 venv/reports，远端零令牌）
