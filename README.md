# OpenCode Agent - Event Loop 并行扫描系统

基于 Python asyncio 的异步事件驱动调度器，**并行运行多个 `nga` (OpenCode CLI) 实例**扫描代码，每个任务一个独立 session，并发数可控。

支持两种模式：
- **MR 模式**：扫描指定 MR 链接
- **Diff 模式**：扫描从指定 commit 到 HEAD 的所有变更文件（自动分片并行）

## 架构

```
┌──────────────────────────────────────────────────────────────┐
│                    Orchestrator (Python)                      │
│                         Event Loop                            │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐      │
│  │ Semaphore   │───▶│ Worker #1   │    │ Worker #2   │      │
│  │ max=3       │    │ nga <task>  │    │ nga <task>  │      │
│  └─────────────┘    └─────────────┘    └─────────────┘      │
│         │                   │                   │             │
│         ▼                   ▼                   ▼             │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐      │
│  │ Task Queue  │    │ MCP/GitHub  │    │ Output Log  │      │
│  │ (pending)   │    │ (fetch src) │    │ (per task)  │      │
│  └─────────────┘    └─────────────┘    └─────────────┘      │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │   skills/            │
                   │   ├── wireless-scan. │
                   │   │   claude.md      │
                   │   ├── wireless-scan. │
                   │   │   mcp.json       │
                   │   └── wireless-scan. │
                   │       yaml           │
                   └──────────────────────┘
```

## 组件说明

| 组件 | 说明 |
|------|------|
| `orchestrator.py` | 主调度器，控制并发、收集结果 |
| `nga` | OpenCode CLI，内置 MCP 工具，自动获取源码 |
| `skills/` | 扫描规则定义（Claude/MCP/YAML 三种标准格式） |

## 快速开始

### 模式一：MR 模式

扫描指定 MR 链接：

```bash
python orchestrator.py \
    https://github.corp/xx/yy/pull/100 \
    https://github.corp/xx/yy/pull/101 \
    -c 3
```

### 模式二：Diff 模式

扫描从指定 commit 到 HEAD 的所有变更文件，**自动按目录分片并行**：

```bash
# 扫描从 abc123 到当前 HEAD 的所有变更
python orchestrator.py --diff abc123 --repo ./app -c 3

# 每批 5 个文件（默认 10 个）
python orchestrator.py --diff abc123 --repo ./app --batch 5 -c 3

# 自定义 nga 命令模板
python orchestrator.py --diff abc123 --cmd "review --files {target}"
```

**Diff 模式原理**：
1. 执行 `git diff abc123..HEAD --name-only` 获取变更文件
2. 过滤出 C/C++ 文件（`.c`, `.cc`, `.cpp`, `.h`, `.hpp`）
3. 按 `--batch` 大小分组成多个任务（默认 10 个文件一组）
4. 每组起一个 nga session 并行扫描

**输出示例**：
```
======================================================================
SCAN SUMMARY
======================================================================
Total Tasks : 5
Success     : 5
Failed      : 0
CPU Time    : 45.2s
Output Dir  : reports/20250429_143052
----------------------------------------------------------------------
✓ [diff-01] [done  ]  12.3s  app/rrc/msg_parser.c app/rrc/...
✓ [diff-02] [done  ]  11.8s  app/mac/pdu_handler.c app/mac/...
✓ [diff-03] [done  ]  10.5s  app/nas/conn_mgr.c ...
✓ [diff-04] [done  ]  10.6s  app/common/utils.c ...
✓ [diff-05] [done  ]   9.2s  include/protocol.h ...
======================================================================
```

## 参数说明

```
usage: orchestrator.py [-h] [--diff COMMIT] [--repo REPO] [--batch BATCH]
                       [-c CONCURRENCY] [--nga NGA] [--cmd CMD]
                       [--timeout TIMEOUT]
                       [mrs ...]

两种模式（互斥）:
  mrs                   MR 链接列表（MR 模式）
  --diff COMMIT         起始 commit hash（Diff 模式）

可选参数:
  --repo REPO           Git 仓库路径（Diff 模式用，默认当前目录）
  --batch BATCH         Diff 模式下每批文件数（默认: 10）
  -c, --concurrency     并发数（默认: 3）
  --nga NGA             nga 可执行文件路径（默认: nga）
  --cmd CMD             命令模板，{target} 会被替换（默认: 'review {target}'）
  --timeout TIMEOUT     单个任务超时(秒)（默认: 600）
```

## 扫描规则（4个核心规则）

| 规则 | ID | 严重等级 | 说明 |
|------|-----|----------|------|
| TLV解析边界检查 | RULE-001 | **CRITICAL** | 指针偏移前是否有 `remaining_len` 校验 |
| 结构体强转内存安全 | RULE-002 | **HIGH** | `memcpy`/强转前是否有 `sizeof` 校验 |
| Switch-Case默认分支 | RULE-003 | **MEDIUM** | switch 是否缺少安全的 `default` |
| ASN.1 Optional字段 | RULE-004 | **HIGH** | Optional 字段访问前是否有存在性检查 |

## 项目结构

```
.
├── orchestrator.py                 # 主调度器
├── skills/
│   ├── wireless-scan.claude.md     # Claude Code 标准格式
│   ├── wireless-scan.mcp.json      # MCP Tool 标准格式
│   ├── wireless-scan.yaml          # 通用 YAML 标准格式
├── knowleage/
│   └── wireless-radio.md           # 知识库：无线通信低错问题
├── reports/                         # 扫描报告输出
│   └── YYYYMMDD_HHMMSS/
│       ├── diff_01.log             # Diff 批次 #1 扫描日志
│       ├── mr_01.log               # MR #1 扫描日志
│       └── summary.json            # 汇总报告
└── README.md
```

## 工作流程

```
用户指定 --diff <commit>
        │
        ▼
┌───────────────┐
│ orchestrator  │──▶ git diff --name-only <commit>..HEAD
│   (Python)    │      获取变更文件列表
└───────────────┘
        │
        ▼
按 --batch 分片（默认10个文件/组）
        │
        ▼
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│ Semaphore(3)  │───▶│  nga task#1   │    │  nga task#2   │
│               │    │  文件1~10     │    │  文件11~20    │
└───────────────┘    └───────────────┘    └───────────────┘
        │                    │                    │
        │                    ▼                    │
        │           通过 stdin pipe 发送命令       │
        │                    │                    │
        │                    ▼                    │
        │           nga 内部调用 MCP 拉取源码      │
        │                    │                    │
        │                    ▼                    │
        │           nga 按 skill 规则扫描          │
        │                    │                    │
        └────────────────────┴────────────────────┘
                             │
                             ▼
                    收集 stdout 输出
                    保存到 reports/
```

## 扩展

添加新的扫描规则，编辑 `skills/` 下的对应格式文件。
