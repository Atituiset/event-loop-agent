# OpenCode Agent - Event Loop 并行扫描系统

基于 Python asyncio 的异步事件驱动调度器，**为每个 C/C++ 文件启动独立的 `nga` (OpenCode CLI) 进程**进行审查，并发数可控（默认3个），处理完一个文件立即关闭 nga session，接着处理下一个。

支持两种输入模式：
- **Diff 模式**：自动提取从指定 commit 到 HEAD 的变更文件
- **文件列表模式**：手动指定要扫描的文件

## 架构

```
文件队列（每个文件一个任务）
        │
        ▼
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│ Semaphore(3)  │───▶│  nga #1       │───▶│  report_001.md│
│               │    │  review f1.c  │    │               │
└───────────────┘    └───────────────┘    └───────────────┘
        │
        ├──▶ nga #2  review f2.c  ──▶ report_002.md
        │
        └──▶ nga #3  review f3.c  ──▶ report_003.md
              │
              ▼
        处理完关闭 session，取下一个文件
```

**每个 nga session 的生命周期**：
1. 启动 nga 子进程（stdin/stdout/stderr 均为 PIPE）
2. 发送：`nga run 'review <file_path>'`
3. 等待 `--scan-delay` 秒（给 nga 时间审查）
4. 发送：`nga run '/exit'` 关闭进程
5. 收集 stdout/stderr，生成 Markdown 审查报告
6. 超时自动 kill 进程

## 组件说明

| 组件 | 说明 |
|------|------|
| `orchestrator.py` | 主调度器，控制并发、收集结果、生成报告 |
| `nga` | OpenCode CLI，通过 stdio 交互，审查单个文件 |
| `skills/` | 扫描规则定义（Claude/MCP/YAML 三种标准格式） |

## 快速开始

### 模式一：Diff 模式

扫描从指定 commit 到 HEAD 的所有变更文件：

```bash
cd /path/to/your/app  # 进入代码仓
python orchestrator.py --diff abc123 --repo . -c 3
```

**原理**：
1. 执行 `git diff abc123..HEAD --name-only` 获取变更文件
2. 过滤出 C/C++ 文件（`.c`, `.cc`, `.cpp`, `.h`, `.hpp`）
3. **每个文件一个独立的 nga session**
4. 并发控制为 3，处理完一个立即关闭，接着处理下一个

### 模式二：文件列表模式

手动指定要扫描的文件或目录（目录会自动递归扫描）：

```bash
# 指定文件
python orchestrator.py --files file1.c file2.c file3.c -c 3

# 指定目录（递归扫描目录及子目录下的所有 C/C++ 文件）
python orchestrator.py --files app/a app/b -c 3
```

### 终端输出示例

```
2025-04-29 14:30:00 [INFO] Output directory: reports/20250429_143052
2025-04-29 14:30:00 [INFO] Log file: reports/20250429_143052/orchestrator.log
2025-04-29 14:30:00 [INFO] === Starting scan: 25 files, concurrency=3, timeout=300s, scan_delay=10s ===
2025-04-29 14:30:00 [INFO] [task-001] START app/rrc/msg_parser.c
2025-04-29 14:30:00 [INFO] [task-002] START app/mac/pdu_handler.c
2025-04-29 14:30:00 [INFO] [task-003] START app/nas/conn_mgr.c
Progress: 3/25 (12%) | Running: 3 | Failed: 0 | Elapsed: 5s
...（进度实时更新，INFO 日志不再打断进度行）...
Progress: 25/25 (100%) | Running: 0 | Failed: 0 | Elapsed: 120s
2025-04-29 14:32:00 [INFO] Finished: 25/25 files | Success: 25 | Failed: 0 | Total time: 120.5s
2025-04-29 14:32:00 [INFO] Summary report: reports/20250429_143052/summary.md
```

## 输出结构

```
reports/20250429_143052/
├── orchestrator.log          # 详细运行日志
├── report_001_msg_parser.md  # 文件 #1 的 Markdown 审查报告
├── report_002_pdu_handler.md # 文件 #2 的 Markdown 审查报告
├── report_003_conn_mgr.md    # 文件 #3 的 Markdown 审查报告
├── ...                       # 更多文件报告
└── summary.md                # 汇总报告（所有文件结果一览）
```

**单个 Markdown 报告示例**：

```markdown
# 代码审查报告 - msg_parser.c

## 扫描信息
| 项目 | 值 |
|------|-----|
| 文件路径 | `app/rrc/msg_parser.c` |
| 任务ID | `task-001` |
| 扫描时间 | 2025-04-29 14:30:10 |
| 耗时 | 12.3s |
| 状态 | 完成 |

## STDOUT (审查输出)
```
[nga 的审查结果输出...]
```

## STDERR
```
[nga 的 stderr 输出...]
```
```

## 参数说明

```
usage: orchestrator.py [-h] [--files FILES [FILES ...] | --diff COMMIT]
                       [--repo REPO] [-c CONCURRENCY] [--nga NGA]
                       [--scan-delay SCAN_DELAY] [--timeout TIMEOUT]

输入模式（互斥）:
  --files FILES [FILES ...]  要扫描的文件路径列表
  --diff COMMIT              起始 commit hash，自动提取变更文件

可选参数:
  --repo REPO                Git 仓库路径（Diff 模式用，默认当前目录）
  -c, --concurrency          并发数，即同时运行的 nga 进程数（默认: 3）
  --nga NGA                  nga 可执行文件路径（默认: nga）
  --scan-delay               发送扫描命令后等待的秒数（默认: 10）
  --timeout                  单个 nga session 的总超时时间(秒)（默认: 300）
```

## 扫描规则（10条）

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

## 项目结构

```
.
├── orchestrator.py                 # 主调度器
├── skills/
│   ├── wireless-scan.claude.md     # Claude Code 标准格式
│   ├── wireless-scan.mcp.json      # MCP Tool 标准格式
│   └── wireless-scan.yaml          # 通用 YAML 标准格式
├── knowleage/
│   └── wireless-radio.md           # 知识库：无线通信低错问题
├── reports/                         # 扫描报告输出
│   └── YYYYMMDD_HHMMSS/
│       ├── orchestrator.log        # 运行日志
│       ├── report_001_*.md         # 文件审查报告
│       ├── report_002_*.md         # 文件审查报告
│       └── summary.md              # 汇总报告
└── README.md
```

## 扩展

添加新的扫描规则，编辑 `skills/` 下的对应格式文件。
