# OpenCode Agent - Event Loop 并行扫描系统

基于 Python asyncio 的异步事件驱动调度器，**并行运行多个 `nga` (OpenCode CLI) 实例**扫描 MR，每个 MR 一个独立 session，并发数可控。

## 架构

```
┌──────────────────────────────────────────────────────────────┐
│                    Orchestrator (Python)                      │
│                         Event Loop                            │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐      │
│  │ Semaphore   │───▶│ Worker #1   │    │ Worker #2   │      │
│  │ max=3       │    │ nga <mr1>   │    │ nga <mr2>   │      │
│  └─────────────┘    └─────────────┘    └─────────────┘      │
│         │                   │                   │             │
│         ▼                   ▼                   ▼             │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐      │
│  │ MR Queue    │    │ MCP/GitHub  │    │ Output Log  │      │
│  │ (pending)   │    │ (fetch src) │    │ (per MR)    │      │
│  └─────────────┘    └─────────────┘    └─────────────┘      │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │   skills/            │
                   │   ├── scan_skill.json│
                   │   └── scan_prompt.md │
                   │   (扫描规则定义)      │
                   └──────────────────────┘
```

## 组件说明

| 组件 | 说明 |
|------|------|
| `orchestrator.py` | 主调度器，控制并发、收集结果 |
| `nga` | OpenCode CLI，内置 MCP 工具，自动拉取 MR 源码 |
| `skills/scan_skill.json` | 扫描规则定义（JSON格式） |
| `skills/scan_prompt.md` | 扫描 Prompt（可直接喂给 nga） |

## 快速开始

### 1. 并行扫描 MR 列表

```bash
python orchestrator.py \
    https://github.corp/xx/yy/pull/100 \
    https://github.corp/xx/yy/pull/101 \
    https://github.corp/xx/yy/pull/102 \
    https://github.corp/xx/yy/pull/103 \
    -c 3
```

输出：
```
======================================================================
SCAN SUMMARY
======================================================================
Total MRs : 4
Success   : 4
Failed    : 0
CPU Time  : 45.2s
Output Dir: reports/20250429_143052
----------------------------------------------------------------------
✓ #01 [done  ]  12.3s  https://github.corp/xx/yy/pull/100
✓ #02 [done  ]  11.8s  https://github.corp/xx/yy/pull/101
✓ #03 [done  ]  10.5s  https://github.corp/xx/yy/pull/102
✓ #04 [done  ]  10.6s  https://github.corp/xx/yy/pull/103
======================================================================
```

### 2. 指定 skill 文件

```bash
python orchestrator.py \
    --skill skills/scan_skill.json \
    https://github.corp/xx/yy/pull/100 \
    https://github.corp/xx/yy/pull/101
```

### 3. 参数说明

```
usage: orchestrator.py [-h] [-c CONCURRENCY] [--nga NGA] [--skill SKILL] mrs [mrs ...]

positional arguments:
  mrs                   MR 链接列表

optional arguments:
  -h, --help            show this help message and exit
  -c, --concurrency     并发数 (默认: 3)
  --nga NGA             nga 可执行文件路径 (默认: nga)
  --skill SKILL         扫描 skill 文件路径
```

## 扫描规则（4个核心规则）

| 规则 | ID | 严重等级 | 说明 |
|------|-----|----------|------|
| TLV解析边界检查 | RULE_001 | **CRITICAL** | 指针偏移前是否有 `remaining_len` 校验 |
| 结构体强转内存安全 | RULE_002 | **HIGH** | `memcpy`/强转前是否有 `sizeof` 校验 |
| Switch-Case默认分支 | RULE_003 | **MEDIUM** | switch 是否缺少安全的 `default` |
| ASN.1 Optional字段 | RULE_004 | **HIGH** | Optional 字段访问前是否有存在性检查 |

## 项目结构

```
.
├── orchestrator.py           # 主调度器
├── skills/
│   ├── scan_skill.json       # 扫描规则定义 (JSON)
│   └── scan_prompt.md        # 扫描 Prompt (Markdown)
├── knowleage/
│   └── wireless-radio.md     # 知识库：无线通信低错问题
├── reports/                   # 扫描报告输出
│   └── YYYYMMDD_HHMMSS/
│       ├── mr_01.log         # MR #1 扫描日志
│       ├── mr_02.log         # MR #2 扫描日志
│       └── summary.json      # 汇总报告
└── README.md
```

## 扩展

添加新的扫描规则，只需编辑 `skills/scan_skill.json` 和 `skills/scan_prompt.md`。
