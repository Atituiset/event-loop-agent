# OpenCode Agent - Knowledge-Driven C/C++ Scanner

基于 Python asyncio 的异步事件驱动调度器，**为每个 C/C++ 文件启动独立的 `nga` (OpenCode CLI) 进程**进行审查，并发数可控（默认3个），处理完一个文件立即关闭 nga session，接着处理下一个。

核心升级：**知识驱动的智能审查系统** —— 每次扫描都基于历史知识上下文进行，自动扩展影响面，本地 SAST 预过滤减少 LLM 调用。

## 架构

```
                         Knowledge Graph (SQLite)
                    ┌─────────────────────────────┐
                    │  Pattern  Case  FileProfile │
                    └─────────────┬───────────────┘
                                  │
    文件队列 ──▶ 影响面分析 ──▶ 知识上下文注入 ──▶ 审查路由决策
        │           │                │                  │
        │           │                │         ┌────────┴────────┐
        │           │                │         ▼                 ▼
        │           │                │   SAST直接输出      LLM深度分析
        │           │                │   (高置信度)        (复杂场景)
        │           │                │         │                 │
        │           │                │         └────────┬────────┘
        │           │                │                  │
        │           │                ▼                  ▼
        │           │         知识提取入库 ◄────  结构化报告输出
        │           │         (反馈闭环)           (SARIF/JSON/Markdown)
        │           │
        ▼           ▼
   ┌─────────┐  ┌──────────────┐
   │ Diff    │  │ 头文件级联   │
   │ 解析    │  │ 符号引用追踪 │
   └─────────┘  └──────────────┘
```

### 4-Phase 改造路线

| Phase | 功能 | 状态 |
|-------|------|------|
| **Phase 0** | 知识图谱：SQLite 存储 Pattern/Case/FileProfile，prompt 注入历史上下文 | ✅ |
| **Phase 1** | 本地 SAST：Semgrep + Cppcheck 预过滤，高置信度问题直接输出 | ✅ |
| **Phase 2** | 影响面分析：头文件级联、符号引用追踪，自动扩展扫描队列 | ✅ |
| **Phase 3** | Embedding 匹配：关键词相似度模糊匹配，关联历史问题模式 | ✅ |
| **Phase 4** | 结构化输出：SARIF 2.1.0 / JSON / Markdown，CI/CD 集成 | ✅ |

## 组件说明

| 组件 | 说明 | 对应 Phase |
|------|------|-----------|
| `orchestrator.py` | 主调度器，控制并发、收集结果、生成报告 | All |
| `knowledge_graph.py` | SQLite 知识图谱，存储 Pattern/Case/FileProfile | Phase 0 |
| `sast_engine.py` | 本地 SAST 引擎（Semgrep + Cppcheck），结果分流 | Phase 1 |
| `impact_analyzer.py` | 影响面分析器，grep 头文件级联和符号引用 | Phase 2 |
| `output_formats.py` | SARIF 2.1.0 / JSON / Markdown 多格式输出 | Phase 4 |
| `web_server.py` | FastAPI 实时调试界面（SSE 流） | - |
| `nga` | OpenCode CLI，通过 stdio 交互，审查单个文件 | - |
| `skills/` | 扫描规则定义（Claude/MCP/YAML/Semgrep 四种格式） | - |
| `knowleage/` | 知识库源文档（导入知识图谱的原始素材） | Phase 0 |

## 快速开始

### 初始化知识库

```bash
python init_knowledge.py
```

将 `knowleage/wireless-radio.md` 中的知识导入 SQLite 知识图谱。

### Diff 模式（推荐）

扫描从指定 commit 到 HEAD 的所有变更文件：

```bash
cd /path/to/your/app
python orchestrator.py --diff abc123 --repo . -c 3
```

**影响面分析自动生效**：如果变更包含头文件，系统会自动找到所有 `#include` 该头文件的源文件并加入扫描队列。

**只扫描指定目录下的变更文件**：

```bash
python orchestrator.py --diff abc123 --paths src/rr,src/mac --repo . -c 3
```

### 文件列表模式

```bash
# 指定文件
python orchestrator.py --files file1.c file2.c file3.c -c 3

# 指定目录（递归扫描）
python orchestrator.py --files app/a app/b -c 3
```

### 启用 Web 调试界面

```bash
python orchestrator.py --diff abc123 --repo . --debug --web-port 8080
# 打开 http://localhost:8080 查看实时 NGA 输出
```

### 安装 SAST 工具（可选，推荐）

```bash
# Semgrep（Phase 1）
pip install semgrep

# Cppcheck（Phase 1，系统包）
# macOS: brew install cppcheck
# Ubuntu: apt-get install cppcheck
```

安装后，高置信度问题将直接输出，无需 LLM，大幅减少扫描时间和成本。

## 输出结构

```
reports/20250429_143052/
├── summary.md                    # 汇总报告（Markdown）
├── scan-results.sarif            # SARIF 2.1.0（CI/CD 集成）
├── scan-results.json             # JSON（内部消费）
├── orchestrator.log              # 全局执行日志
├── src/rr/abc/cde/efg/
│   ├── Hello.md                  # 审查报告（nga 结果 + SAST 结果）
│   ├── Hello.log                 # 运行日志
│   └── Hello.diff                # diff 内容（Diff 模式）
└── src/mac/scheduler/
    ├── scheduler.md
    ├── scheduler.log
    └── scheduler.diff
```

## 知识图谱工作机制

### 初始化导入

```bash
$ python init_knowledge.py
Initializing knowledge graph: .claude/knowledge.db
Imported 5 patterns from knowleage/wireless-radio.md
Knowledge graph stats: {'patterns': 5, 'cases': 0, 'file_profiles': 0, 'scan_runs': 0}
```

### 自动知识沉淀

每次扫描完成后，系统会自动：
1. 从 nga 输出中提取 `[RULE-XXX]` 标记的问题
2. 存入知识图谱的 `cases` 表（关联到对应 `pattern`）
3. 更新文件风险画像 (`file_profiles`)
4. 记录扫描运行 (`scan_runs`)

### Prompt 上下文注入

扫描新文件时，系统会自动查询知识图谱并注入以下内容到 LLM prompt：
- **已知风险模式**：该文件历史最常见的 3 个模式
- **文件风险画像**：历史发现问题数、风险评分
- **上次扫描遗留问题**：未修复的 open 状态问题
- **SAST 预扫描问题**：需要 LLM 验证的补充分析
- **影响范围**：本次变更可能影响的其他文件

## CI/CD 集成

### GitHub Actions

已内置 `.github/workflows/code-scan.yml`，开启后：
- 每次 PR 自动扫描变更文件
- SARIF 结果上传至 GitHub Advanced Security
- 扫描报告作为 artifact 保留 30 天

### GitLab CI

已内置 `.gitlab-ci.yml`，开启后：
- 每次 MR 自动扫描
- SARIF 结果接入 GitLab SAST 仪表板
- 支持 MR 门禁配置

## 扫描规则

### 无线通信专用（4条）

| 规则 | ID | 严重等级 | 说明 |
|------|-----|----------|------|
| TLV解析边界检查 | RULE-001 | **CRITICAL** | 指针偏移前是否有 `remaining_len` 校验 |
| 结构体强转内存安全 | RULE-002 | **HIGH** | `memcpy`/强转前是否有 `sizeof` 校验 |
| Switch-Case默认分支 | RULE-003 | **MEDIUM** | switch 是否缺少安全的 `default` |
| ASN.1 Optional字段 | RULE-004 | **HIGH** | Optional 字段访问前是否有存在性检查 |

### C/C++ 通用低错（6条）

| 规则 | ID | 严重等级 | 说明 |
|------|-----|----------|------|
| 相似变量名混淆 | RULE-005 | **MEDIUM** | `buf`/`buff`、`len`/`length` 等相似名字误用 |
| 重复/冗余代码 | RULE-006 | **LOW** | 复制粘贴后未修改、相同功能代码重复 |
| 未初始化变量使用 | RULE-007 | **HIGH** | 局部变量/结构体使用前未初始化 |
| 内存泄漏 | RULE-008 | **HIGH** | `malloc` 后缺少 `free`，异常路径遗漏释放 |
| 空指针解引用 | RULE-009 | **CRITICAL** | 未检查 NULL 就解引用 |
| 数组越界 | RULE-010 | **CRITICAL** | 索引未校验、循环条件错误、缓冲区溢出 |

### Semgrep 本地规则（Phase 1）

安装 Semgrep 后，以下规则由本地引擎直接检出，无需 LLM：

| 规则 | ID | 检测能力 |
|------|-----|---------|
| memcpy 无 sizeof 检查 | RULE-002 | `memcpy` 调用缺少长度校验 |
| switch 缺少 default | RULE-003 | switch 语句无 default 分支 |
| 未初始化变量 | RULE-007 | 变量声明后直接使用 |
| 空指针解引用 | RULE-009 | 指针未检查 NULL 就解引用 |
| 数组越界 | RULE-010 | 数组索引缺少边界检查 |
| 不安全函数 | RULE-024 | `strcpy`/`sprintf`/`gets` 等 |

## 项目结构

```
.
├── orchestrator.py                 # 主调度器
├── knowledge_graph.py              # 知识图谱管理（Phase 0）
├── sast_engine.py                  # 本地 SAST 引擎（Phase 1）
├── impact_analyzer.py              # 影响面分析器（Phase 2）
├── output_formats.py               # 多格式输出（Phase 4）
├── web_server.py                   # Web 调试界面
├── init_knowledge.py               # 知识库初始化脚本
├── skills/
│   ├── wireless-scan.claude.md     # Claude Code 标准格式
│   ├── wireless-scan.mcp.json      # MCP Tool 标准格式
│   ├── wireless-scan.yaml          # 通用 YAML 标准格式
│   └── semgrep/
│       └── wireless-rules.yaml     # Semgrep 规则（Phase 1）
├── knowleage/
│   └── wireless-radio.md           # 知识库源文档
├── .claude/
│   └── knowledge.db                # SQLite 知识库（自动生成）
├── reports/                         # 扫描报告输出
│   └── YYYYMMDD_HHMMSS/
│       ├── summary.md
│       ├── scan-results.sarif      # SARIF 2.1.0
│       ├── scan-results.json       # JSON
│       └── <relative_path>/
│           ├── <file>.md
│           ├── <file>.log
│           └── <file>.diff
├── .github/workflows/
│   └── code-scan.yml               # GitHub Actions（Phase 4）
├── .gitlab-ci.yml                  # GitLab CI（Phase 4）
├── README.md
├── SPEC.md
├── OPTIMIZATION.md                 # C/C++ 扫描优化计划
├── KNOWLEDGE_GRAPH_PLAN.md         # 知识图谱改造计划
└── .gitignore
```

## 参数说明

```
usage: orchestrator.py [-h] [--files FILES [FILES ...] | --diff COMMIT]
                       [--paths PATHS] [--repo REPO] [-c CONCURRENCY]
                       [--nga NGA] [--timeout TIMEOUT]
                       [--debug] [--web-port WEB_PORT]

输入模式（互斥）:
  --files FILES [FILES ...]  要扫描的文件或目录列表（目录会自动递归扫描）
  --diff COMMIT              起始 commit hash，自动提取变更文件

可选参数:
  --paths PATHS              关注的相对目录，逗号分隔（如 app/a,app/b）
  --repo REPO                Git 仓库路径（Diff 模式用，默认当前目录）
  -c, --concurrency          并发数（默认: 3）
  --nga NGA                  nga 可执行文件路径（默认: nga）
  --timeout                  单个 nga session 的总超时时间(秒)（默认: 300）
  --debug                    启动 Web 调试界面
  --web-port                 Web 调试界面端口（默认: 8080）
```

## 扩展

### 添加新规则

1. **LLM 规则**：编辑 `skills/` 下的 `.claude.md` / `.mcp.json` / `.yaml` 文件
2. **本地 SAST 规则**：在 `skills/semgrep/` 下添加新的 `.yaml` 规则文件

### 自定义知识库

1. 编辑 `knowleage/` 下的知识文档
2. 运行 `python init_knowledge.py` 重新导入
3. 或直接调用 `knowledge_graph.py` 的 API 添加 Pattern

### 接入 Embedding 模型（Phase 3 增强）

当前使用关键词相似度作为轻量级 embedding。如需更高精度：

```python
# 安装 sentence-transformers
pip install sentence-transformers

# 在 knowledge_graph.py 中替换 _text_similarity
# 使用 all-MiniLM-L6-v2 等预训练模型
```
