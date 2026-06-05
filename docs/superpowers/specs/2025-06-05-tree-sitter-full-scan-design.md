# Tree-sitter 函数级 Full 扫描设计

> 为 event-loop-agent 引入 tree-sitter，在 Full 扫描模式下按函数切分 C/C++ 源码，每个函数作为独立的 nga 审查入口。

---

## 背景

当前 orchestrator 支持两种扫描粒度：
- **Diff 模式** (`--diff`)：扫描变更文件，每个文件一个 nga session
- **文件列表模式** (`--files`)：扫描指定文件/目录，每个文件一个 nga session

Full 扫描模式的目标：对源文件中的**每个函数**启动一个独立 nga session，实现更细粒度的代码审查。函数切分由 tree-sitter 完成，确保 AST 级精确提取。

---

## 设计目标

| 目标 | 说明 |
|------|------|
| 函数级切分 | 使用 tree-sitter 按 AST 节点提取函数定义，不是基于正则的粗糙切分 |
| C/C++ 全覆盖 | 支持 C 函数 + C++ 方法（含模板函数、类内 inline 定义） |
| 最小上下文注入 | 每个 nga session 只发送函数本体代码，由 nga 按需搜索上下文 |
| 降级兼容 | tree-sitter 解析失败时自动回退到"整文件一个 session" |
| 复用现有机制 | 超时、并发控制 (Semaphore)、信号处理、web debug、清理逻辑全部复用 |

---

## CLI 设计

`--full` 作为与 `--files` / `--diff` 互斥的独立模式：

```bash
# Full 扫描指定目录（递归收集 .c/.cc/.cpp）
python orchestrator.py --full app/ src/ -c 3

# Full 扫描 + --paths 过滤（只保留匹配 cared_paths 的文件）
python orchestrator.py --full . --paths src/rr,src/mac -c 3

# Full 扫描 + Web 调试
python orchestrator.py --full app/ --debug --web-port 8080
```

### 互斥关系

```
输入模式（三选一）:
  --files FILES [FILES ...]  文件列表模式：每文件一个 nga session
  --diff COMMIT              Diff 模式：扫描变更文件
  --full FILES [FILES ...]   Full 模式：按函数切分，每函数一个 nga session
```

其他参数（`--paths`, `-c`, `--timeout`, `--debug`, `--web-port`, `--nga`）全部复用。

---

## 文件收集（Full 模式）

复用现有 `setup_file_mode` 的文件收集逻辑，但**只保留有函数体的源文件**：

| 扩展名 | 是否收集 | 原因 |
|--------|---------|------|
| `.c` | ✅ | C 源文件，包含函数定义 |
| `.cc` | ✅ | C++ 源文件 |
| `.cpp` | ✅ | C++ 源文件 |
| `.h` | ❌ | 头文件，通常只有声明 |
| `.hpp` | ❌ | 头文件，通常只有声明 |

文件按相对路径排序去重，与现有模式一致。

---

## Tree-sitter 切分模块

### 依赖

```
tree-sitter
tree-sitter-c
tree-sitter-cpp
```

### 切分策略

对每份源文件解析 AST，提取以下节点类型：

| AST 节点类型 | 语言 | 说明 |
|-------------|------|------|
| `function_definition` | C / C++ | 有函数体的函数定义 |
| `method_definition` | C++ | 类方法定义 |
| `template_declaration` → `function_definition` | C++ | 模板函数定义 |

**排除的节点**：
- `function_declaration`（无函数体的声明）
- `field_declaration`（类成员变量声明）
- `namespace_definition` 本身（但提取其内部的函数定义）

### 提取字段

对每个匹配的函数节点，提取：
- `function_name`：函数名（从 `declarator` 子节点提取）
- `start_line`：函数起始行号（1-based）
- `end_line`：函数结束行号（1-based）
- `code_text`：函数完整代码文本（**包含前置注释**，如 doxygen / 函数头说明）

**前置注释包含策略**：检查 `function_definition` 节点之前是否紧跟 `comment` 节点（允许中间有空白），如果是，则将该注释块也包含进 `code_text`。这对审查很重要——注释中常包含接口契约、前置条件、边界说明。

### 函数名提取规则

```
普通函数:     int foo(int x) { ... }        → "foo"
类方法:       void MyClass::bar() { ... }   → "MyClass::bar"
构造函数:     MyClass::MyClass() { ... }    → "MyClass::MyClass"
析构函数:     MyClass::~MyClass() { ... }   → "MyClass::~MyClass"
模板函数:     template<typename T> void f() → "f"
匿名 lambda:  跳过（无有意义名称，暂不审查）
```

函数名用于生成报告文件名和 task_id，需要保证文件系统安全（替换非法字符为 `_`）。

**重载函数处理**：同一文件内可能存在同名重载函数（C++），报告文件名会冲突。采用 `{函数名}_{起始行号}` 作为报告文件名，如 `process_pdu_145.md`，保证唯一性。

### Fallback 机制

tree-sitter 解析失败时（如语法不完整、编码问题），将该文件降级为**整文件一个 session**，行为与 `--files` 模式完全一致。记录 warning 日志，不中断扫描流程。

---

## 任务模型扩展

`ScanTask` 数据类新增 `function_name` 字段：

```python
@dataclass
class ScanTask:
    file_path: str
    task_id: str
    report_file: str
    log_file: str
    function_name: str = ""      # ← 新增：函数名（Full 模式用）
    status: str = "pending"
    ...
```

### Task ID 格式

```
Full 模式:  task-{file_idx:03d}-{func_idx:03d}
示例:       task-003-007  → 第3个文件的第7个函数
```

### 非 Full 模式

`function_name` 为空字符串，task_id 保持现有格式 `task-{idx:03d}`。

---

## NGA Prompt 设计

Full 模式下发送给 nga 的 prompt 格式：

```
请审查以下 C/C++ 函数，应用无线通信安全编码规则（RULE-001~RULE-010）进行全面检查：

文件: {file_path}
函数名: {function_name}
行号: {start_line}-{end_line}

```c
{code_text}
```

审查要求：
1. 检查函数内所有变量的定义和声明，是否存在未初始化使用
2. 检查内存操作（malloc/free、memcpy 等）是否存在泄漏或越界
3. 检查指针操作和输入参数的边界校验
4. 检查 switch-case 是否有安全的 default 分支
5. 检查 TLV/ASN.1 解析是否有边界校验
6. 对每个发现的问题提供：问题描述、相关代码片段、修复建议、置信度等级

如果需要查看更多上下文（如调用者、被调用函数、结构体/宏定义），请使用工具搜索相关符号。
```

---

## 输出结构

Full 模式下，报告按"文件目录 → 函数文件"组织：

```
reports/20250605/
├── src/
│   └── rr/
│       └── main.c/                 ← 以源文件名创建目录
│           ├── process_pdu.md      ← 函数审查报告
│           ├── process_pdu.log     ← 运行日志
│           ├── decode_tlv.md
│           └── decode_tlv.log
│       └── utils.c/
│           ├── parse_header.md
│           └── parse_header.log
├── summary.md                      ← 按文件→函数层级汇总
└── orchestrator.log                ← 全局执行日志
```

### 路径计算

```
原始文件:   src/rr/main.c
函数名:     process_pdu
报告文件:   reports/20250605/src/rr/main.c/process_pdu.md
日志文件:   reports/20250605/src/rr/main.c/process_pdu.log
```

### summary.md 结构

```markdown
# 扫描汇总报告

## 统计
| 总函数数 | 成功 | 失败 | 总耗时 |
|----------|------|------|--------|

## 详细结果
### src/rr/main.c
| # | 函数 | 状态 | 耗时 | 报告 | 日志 |
|---|------|------|------|------|------|
| 1 | process_pdu | ✅ done | 12.3s | [process_pdu.md](...) | [process_pdu.log](...) |
| 2 | decode_tlv  | ✅ done | 8.5s  | [decode_tlv.md](...)  | [decode_tlv.log](...) |

### src/rr/utils.c
| # | 函数 | 状态 | 耗时 | ... |
```

---

## 模块划分

新增模块及其职责：

```
orchestrator.py         # 主调度器：新增 --full CLI、setup_full_mode、_build_full_scan_cmd
function_splitter.py    # tree-sitter 函数切分模块（纯函数，无状态）
    ├── extract_functions(file_path, language_hint) -> list[FunctionInfo]
    ├── _get_language(file_path) -> tree_sitter.Language
    └── _sanitize_name(name) -> str
```

`function_splitter.py` 不依赖 orchestrator 的任何状态，仅通过纯函数接口被调用。

---

## 错误处理

| 场景 | 处理策略 |
|------|---------|
| tree-sitter 解析失败 | 记录 warning，降级为整文件一个 session |
| 文件内无函数定义 | 记录 warning，跳过该文件 |
| 函数名提取失败（如匿名） | 使用 `anonymous_{line}` 作为 fallback 名 |
| 函数代码文本提取失败 | 跳过该函数，记录 error，继续处理其他函数 |
| nga session 超时/失败 | 复用现有机制：SIGTERM → SIGKILL，记录诊断信息 |

---

## 性能考虑

| 项 | 分析 |
|---|---|
| tree-sitter 解析速度 | 本地操作，毫秒级，不构成瓶颈 |
| 函数数量 | 大型项目可能有数千个函数，task 数量级增长 |
| 并发控制 | 复用现有 Semaphore，并发数由 `-c` 控制（默认 3） |
| 队列管理 | task 列表变长但内存占用可控（只存元数据，不存函数代码） |
| 超时 | 函数级 session 通常比文件级更快，动态超时公式可保持现有逻辑 |

---

## 实现范围

本设计**不包含**以下内容（后续可扩展）：
- 头文件（.h/.hpp）的函数声明审查
- Lambda 表达式审查
- 跨文件调用链自动打包（caller/callee 聚合到一个 session）
- Diff + Full 的组合模式（先 diff 找变更文件，再对变更函数做 full 扫描）
- SARIF / JSON 结构化输出

---

*Spec version: 1.0 | Date: 2025-06-05*
