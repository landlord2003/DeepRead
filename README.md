# 📖 DeepRead 长文精读引擎

本机 Ollama 驱动的长文/整书精读工具，**零外部 API、数据不出本机**。粘贴文本、拖入文件（txt/md/pdf/docx/扫描图片），
产出结构化精读报告、知识地图、故事梗概，或将非虚构书籍蒸馏成可直接导入 AI 助手的 SKILL.md 技能包。

## 功能模式（6 种 + 模式对比）

| 模式 | 说明 |
|---|---|
| ⚡ quick 快速要点 | 一句话总结 + 要点列表，最快 |
| 🔍 deep 深度精读 | 核心论点 + 论证结构 + 原文金句（逐字校验）+ 置信度标注 |
| 🗺️ map 知识地图 | 观点-证据-数据-关系 + Mermaid 导图 |
| 🎓 feynman 大白话 | 术语翻译 + 类比 + 追问问题 |
| 🧪 distill 萃猫蒸馏 | 全书骨架 → 逐块候选素材萃取（带原文逐字摘录）→ 场景聚类 → 产出可导入的 SKILL.md 技能包（含 怎么用.md 导入指南） |
| 📚 book 整书精读 | 按章节分部深读 → 总编汇总（全书梗概 + 章节脉络 + 主题） |

- **⚖️ 模式对比**：任选 2-6 种模式并行成稿（公共深读只跑一次，省算力）
- **类型自适应**：自动识别 小说/散文/时评/历史/学术/技术/报告/演讲 八类文体，按类型切换萃取侧重与成稿结构
- **故事梗概**：叙事文本自动前置 600-1400 字归纳式梗概（基于原文分批叙述，非卡片转述）
- **扫描件 OCR**：图片/扫描 PDF 自动调 qwen3-vl 视觉模型识别
- **对抗式复核**：长文本可开启四视角评审挑刺 + 主编回应
- **批量队列**：多文件串行处理（单 worker，避免 Ollama 争抢显存）

## 环境要求

- Python 3.10+（Windows / Linux / macOS 均可）
- [Ollama](https://ollama.com) 运行在本机，已拉取模型：

```bash
ollama pull qwen3:14b        # 主力生成（约 9.3GB，12GB 显存可跑）
ollama pull qwen2.5:7b       # 文体分类等轻任务（可选）
ollama pull qwen3-vl:8b      # 扫描件 OCR（可选，需处理图片时）
```

> 显存参考：RTX 5070 12GB 全链可跑；模型**串行访问**（管线已按单 worker 设计），并发会反复换页反而更慢。

## 安装部署

```bash
git clone https://github.com/landlord2003/DeepRead.git
cd DeepRead

# 1) 建虚拟环境（可选但推荐）
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Linux / macOS

# 2) 装依赖（仅 pypdfium2 + pillow，其余全是标准库）
pip install -r requirements.txt

# 3) 启动
python server.py               # 默认 127.0.0.1:8790
```

浏览器打开 `http://127.0.0.1:8790` → 左侧粘贴文本或拖入文件 → 选模式 → 开始精读。

### 命令行用法

```bash
python deepread.py 文本文件.txt --mode deep          # 单模式精读
python deepread.py 书籍.txt --mode distill           # 萃猫蒸馏
python deepread.py 书籍.txt --mode book              # 整书精读
python deepread.py 文本.txt --type speech            # 手动指定文体类型
```

## 产物结构

- `reports/*.md` — 各次精读报告（历史栏可查看 / 删除 / 打开所在文件夹）
- `reports/distill_时间_书名/` — 蒸馏产物：
  - `骨架.md` — 全书骨架（主旨/结构/核心主张/关键概念/盲点边界）
  - `候选/*.md` — 逐块萃取的原则/方法步骤/术语/禁忌反例（每条带原文逐字摘录与块号）
  - `技能包/<name>/SKILL.md` — 可导入支持 Skills 规范的 AI 助手
  - `怎么用.md` — 导入指南与技能分工

## 关键设计

- **MapReduce 管线**：切段(≈7500字) → 逐块提取卡片 → 分层汇总 → 按模式成稿
- **防编造守卫**：金句逐字校验回原文；引语守卫 + 逐句 4-gram 抽查回素材；候选素材 quote 强制逐字
- **显存友好**：分阶段分配上下文（map 4096 / reduce 8192 / 骨架与技能 12288），12GB 卡全程不换页
- **纯本地**：唯一外部依赖是本机 Ollama；win 下注意系统代理会劫持 urllib，引擎内已用空 ProxyHandler 直连

## License

仅供个人学习研究。
