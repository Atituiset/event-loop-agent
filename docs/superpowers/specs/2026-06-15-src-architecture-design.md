# event-loop-agent src/ 架构重构设计

## 背景

当前项目根目录下散乱放置了 8 个 Python 源码文件：`orchestrator.py`、`finding_parser.py`、`finding_store.py`、`function_splitter.py`、`skill_loader.py`、`config_loader.py`、`web_server.py`、`benchmark_cache.py`，以及 `tests/`、`skills/`、`vscode-opencode-flywheel/`、`docs/`、`knowleage/` 等目录。

随着多 Skill YAML 配置系统的引入，代码边界和职责变得模糊。本次重构目标是在迁移代码到 `src/` 的同时，重新设计模块边界，使项目具备清晰的包结构、依赖方向和可测试性。

## 目标

1. 将所有功能源码迁移到 `src/opencode_agent/` 下，形成一个标准 Python package。
2. 按职责拆分子包，避免单个文件过大、职责混杂。
3. 明确依赖方向：`core` 不依赖其他包，`scanner` 依赖 `core`/`skills`/`findings`/`utils`，`cli` 统筹组装。
4. 保留根目录 `orchestrator.py` 作为薄 shim，兼容旧命令 `python orchestrator.py ...`。
5. 内置 skill YAML 文件随 package 一起发布，项目自定义 skill 仍通过 `.opencode/skills/` 加载。
6. 删除或迁移一次性脚本和冗余目录（`benchmark_cache.py`、`knowleage/`）。

## 非目标

- 不修改 VS Code 扩展目录 `vscode-opencode-flywheel/` 的结构。
- 不修改 `docs/` 文档目录结构。
- 不改变 nga 子进程交互协议和 Markdown 报告格式。
- 不改变 SQLite schema。

## 目录结构

```
event-loop-agent/
├── orchestrator.py              # 薄 shim: from opencode_agent.cli import main
├── pyproject.toml
├── requirements.txt
├── scripts/
│   └── benchmark_cache.py       # 一次性 benchmark 脚本
├── skills/                      # 迁移后清空或删除
├── src/
│   └── opencode_agent/
│       ├── __init__.py
│       ├── __main__.py          # python -m opencode_agent
│       ├── cli/
│       │   ├── __init__.py
│       │   └── main.py          # argparse + 入口
│       ├── core/
│       │   ├── __init__.py
│       │   ├── config.py        # AgentConfig, SkillConfig dataclasses
│       │   └── models.py        # ScanTask, ProgressTracker
│       ├── findings/
│       │   ├── __init__.py
│       │   ├── parser.py        # finding_parser.py → Finding + parse
│       │   └── store.py         # finding_store.py → SQLite
│       ├── scanner/
│       │   ├── __init__.py
│       │   ├── orchestrator.py  # OpenCodeOrchestrator
│       │   ├── reporter.py      # generate_report / generate_summary
│       │   ├── slot.py          # SlotManager
│       │   ├── splitter.py      # function_splitter.py → tree-sitter 切分
│       │   └── task.py          # ScanTask 相关辅助（可选）
│       ├── skills/
│       │   ├── __init__.py
│       │   ├── config_loader.py # 全局/项目配置合并
│       │   ├── loader.py        # YAML skill 解析
│       │   ├── registry.py      # SkillRegistry + prompt 构建
│       │   ├── wireless-scan.yaml
│       │   ├── memory-safety.yaml
│       │   ├── concurrency-safety.yaml
│       │   └── cpp-modernization.yaml
│       ├── utils/
│       │   ├── __init__.py
│       │   ├── ansi.py          # ANSI_ESCAPE
│       │   └── logging.py       # LOG_FORMAT / logger 配置
│       └── web/
│           ├── __init__.py
│           └── server.py        # FastAPI debug server
└── tests/
    ├── test_*.py                # 从 src.opencode_agent.xxx 导入
    └── conftest.py              # 共享 fixture（可选）
```

## 模块职责

| 包 | 文件 | 职责 |
|---|---|---|
| `cli` | `main.py` | 解析 `argparse`，构造 `OpenCodeOrchestrator` 和 `SkillRegistry`/`ConfigLoader`，调用 `main()`。 |
| `core` | `models.py` | 纯数据类：`ScanTask`、`ProgressTracker`（未来可加 `ScanSession`）。 |
| `core` | `config.py` | `AgentConfig`、`SkillConfig`、配置常量（全局/项目路径）。 |
| `scanner` | `orchestrator.py` | `OpenCodeOrchestrator`：只保留调度、并发、任务生命周期、与外部 `nga` 进程的交互。 |
| `scanner` | `reporter.py` | 报告生成：`generate_report`、`generate_log`、`generate_summary`。 |
| `scanner` | `slot.py` | `SlotManager`：并发槽位分配。 |
| `scanner` | `splitter.py` | `function_splitter.py`：tree-sitter 函数切分。 |
| `skills` | `loader.py` | YAML skill 解析为 `Skill`/`Rule` dataclass。 |
| `skills` | `registry.py` | `SkillRegistry`：加载内置+项目 skill、规则命名空间、prompt section 构建。 |
| `skills` | `config_loader.py` | `ConfigLoader`：加载并合并全局/项目配置，判断启用 skill/rule。 |
| `findings` | `parser.py` | `Finding` dataclass + Markdown 解析。 |
| `findings` | `store.py` | `FindingStore`：SQLite 持久化 + 反馈标签。 |
| `web` | `server.py` | FastAPI debug 服务器（SSE + finding API）。 |
| `utils` | `logging.py` | 全局 logger 配置、`LOG_FORMAT`。 |
| `utils` | `ansi.py` | ANSI 转义正则。 |

## 依赖方向

```
cli
├── scanner
│   ├── core
│   ├── skills
│   ├── findings.parser
│   └── utils
├── skills
├── findings.store
├── web
└── utils

findings.parser ──独立
findings.store ──依赖 findings.parser
web ──依赖 findings.store
```

约束：

- `core` 不依赖任何其他包。
- `findings.parser` 不依赖 `findings.store`（解析可以独立使用）。
- `scanner` 不直接读取 YAML 或配置文件，只接收已合并好的 prompt 和启用 rule 列表。
- `web` 只暴露接口，不参与扫描流程。

## 数据流

一次扫描的主流程：

1. `cli/main.py` 解析命令行参数。
2. `cli/main.py` 构造 `ConfigLoader`（读取 `~/.config/opencode/config.yaml` + `.opencode/skills.yaml`）。
3. `cli/main.py` 构造 `SkillRegistry`（加载内置 skill + 项目自定义 skill）。
4. 根据配置确定启用的 skill/rule，构建合并 prompt。
5. `cli/main.py` 构造 `OpenCodeOrchestrator`，传入 prompt 和启用 rule 列表。
6. `OpenCodeOrchestrator.scan_full()` / `scan_diff()` 被调用。
7. 对每个文件调用 `scanner.splitter.extract_functions()` 切分函数。
8. 为每个函数创建 `ScanTask`。
9. `SlotManager` 分配并发槽位，启动 `nga` 子进程。
10. 收集 `nga` stdout（Markdown 报告）。
11. `scanner.reporter.generate_report()` 生成 `.md` 报告和 `.log`。
12. `findings.parser.parse_findings_from_markdown()` 解析为 `list[Finding]`。
13. （可选）`findings.store.FindingStore.save_findings()` 写入 SQLite。
14. `web.server` 提供 SSE 和 finding API 供调试。

## Skill 与配置加载

- 内置 skill 位于 `src/opencode_agent/skills/*.yaml`，使用 `importlib.resources` 读取，确保安装后可用。
- 项目自定义 skill 位于被扫描项目的 `.opencode/skills/*.yaml`。
- `SkillRegistry` 加载内置 skill 后，再加载项目 skill；同名 skill 由项目版本覆盖。
- 规则全局 ID 格式为 `skill-id:RULE-XXX`，本地 ID 仍为 `RULE-XXX`。
- `ConfigLoader` 先加载全局配置，再加载项目配置；项目配置覆盖全局配置。

## 错误处理

1. **Skill/Config 加载失败**：单个文件失败记录 `logger.warning`，不中断扫描；缺失 skill/rule 返回空 prompt，CLI 提示"无可用规则"。
2. **外部进程失败**：保持现有软/硬超时机制（SIGTERM → 30s → SIGKILL），失败 task 状态标记为 `failed`，仍生成部分报告。
3. **Finding 解析失败**：异常捕获后记录日志，不影响其他文件；单文件返回空 findings。
4. **Store 初始化失败**：`output_json` 模式下 store 初始化失败降级为 warning，不终止扫描。

## 测试策略

- `tests/test_skill_loader.py`：从 `src.opencode_agent.skills.loader` / `registry` 导入。
- `tests/test_config_loader.py`：从 `src.opencode_agent.skills.config_loader` 导入。
- 新增 `tests/test_scanner_reporter.py`、`tests/test_scanner_slot.py` 拆分原有 orchestrator 测试。
- 保留 `tests/test_finding_parser.py`、`tests/test_finding_store.py`、`tests/test_function_splitter.py` 等，仅更新 import。
- 根目录 `orchestrator.py` shim 用一个简单测试确保它还能被 `python orchestrator.py --help` 调用。

## 根目录保留文件

- `orchestrator.py`：薄 shim，内容如下：

```python
#!/usr/bin/env python3
from opencode_agent.cli.main import main

if __name__ == "__main__":
    main()
```

## 迁移路径

1. 创建 `src/opencode_agent/` 目录结构和 `__init__.py`。
2. 按模块职责逐个迁移源码，同步更新 import。
3. 将内置 skill YAML 移动到 `src/opencode_agent/skills/`，`SkillRegistry` 改用 `importlib.resources` 读取。
4. 将 `benchmark_cache.py` 移动到 `scripts/`。
5. 将 `knowleage/wireless-radio.md` 内容合并到 `skills/wireless-scan.yaml` 后删除 `knowleage/`。
6. 更新 `pyproject.toml`，配置 package 和 `src` 布局。
7. 更新 `tests/` 中的 import。
8. 运行完整测试套件验证。

## 决策记录

- **Python package vs 扁平模块**：选择 Python package，因为需要清晰的命名空间和分层。
- **tests/ 保留根目录**：遵循 Python 社区常见做法，便于 pytest 和社区工具识别。
- **skills/ 内置到 package**：确保安装后内置 skill 可用，避免运行时相对路径问题。
- **根目录保留 `orchestrator.py` shim**：兼容旧命令，降低迁移摩擦。
- **删除 `knowleage/`**：其内容可由 skill YAML 的 `agent.expertise` 替代，避免重复。
