# OpenCode Agent - Knowledge-Driven Scanning 改造计划

> 基于 Meta "AI Mapping Tribal Knowledge in Large-Scale Data Pipelines" 思想，将当前的事件循环扫描器升级为**知识驱动的智能审查系统**。
>
> 核心转变：从"每次扫描都是独立的 LLM 调用" → "每次扫描都基于历史知识上下文进行"

---

## 1. 背景与问题

### 1.1 Meta 文章核心思想

| 概念 | 含义 | 对我们项目的映射 |
|------|------|-----------------|
| **Tribal Knowledge** | 未文档化的领域知识，存在于资深工程师的审查评论中 | 每次 `nga` 审查发现的问题 + 修复建议，目前散落在 Markdown 报告里，无法复用 |
| **Knowledge Extraction** | 从 MR 评论、讨论中自动提取结构化知识 | 每次扫描后，自动从 `nga` 输出中提取"问题模式"并入库 |
| **Knowledge Graph** | 知识条目之间的关联关系（概念→规则→代码→修复） | 将 `knowleage/wireless-radio.md` 从平面文档升级为可查询的图结构 |
| **Impact Surface** | 变更代码的影响范围分析 | 当前只扫变更文件，头文件改了结构体但引用文件不被扫描 |
| **Embedding Matching** | 用向量相似度关联知识条目与新代码变更 | 用 code embedding 匹配历史问题和新代码，实现"模糊规则" |
| **Feedback Loop** | 扫描结果反馈回知识库，持续改进 | 误报标记 → 调整置信度；新问题 → 生成新知识条目 |

### 1.2 当前系统的关键短板

1. **知识是死的**：`knowleage/wireless-radio.md` 是静态文档，不会从扫描历史中自动生长
2. **扫描是孤立的**：每个文件一个 `nga` session，没有"影响面"概念，头文件变更不触发关联文件扫描
3. **规则是硬编码的**：10 条规则写死在 `skills/` 里，没有"从历史问题中自动归纳新规则"的能力
4. **没有记忆**：上次扫描发现"这个函数容易溢出"，下次变更这个函数时，系统不会提醒
5. **结果不结构化**：Markdown 适合人读，但无法被后续流程消费（CI 门禁、趋势分析、知识沉淀）

---

## 2. 改造目标

### 2.1 总体目标

将当前系统从**"无状态的单文件扫描器"**改造为**"知识驱动的上下文感知审查系统"**。

```
当前流程（无状态）：
  文件队列 ──▶ 独立 nga 进程 ──▶ Markdown 报告（看完即丢）

目标流程（知识驱动）：
  文件队列 ──▶ 影响面分析 ──▶ 知识上下文注入 ──▶ nga 审查
                  │                                   │
                  ▼                                   ▼
            关联文件识别                          结果结构化提取
                  │                                   │
                  ▼                                   ▼
            调用链提取 ───────────────────────▶ 知识图谱入库（反馈闭环）
```

### 2.2 量化目标

| 指标 | 当前 | 目标（Phase 4 结束） |
|------|------|---------------------|
| 规则覆盖 | 10 条（硬编码） | 30+ 条（硬编码 + 动态生成） |
| 历史知识复用 | 0% | 每次扫描自动注入 Top-5 相关历史问题 |
| 跨文件分析 | 无 | 头文件级联、调用链上下游自动扩展 |
| LLM 调用比例 | 100% | < 30%（本地 SAST 处理明确模式） |
| 结果结构化 | Markdown  only | Markdown + SARIF + SQLite |
| 知识增长 | 手动编辑 | 自动从扫描结果中提取并去重入库 |

---

## 3. 架构改造设计

### 3.1 新增核心模块

```
orchestrator.py（现有，主调度器）
    │
    ├──▶ knowledge_graph.py（新增）◄───────┐
    │   ├── 知识图谱存储（SQLite）          │
    │   ├── 知识查询接口                   │
    │   ├── 知识去重（embedding 相似度）    │
    │   └── 知识注入（生成 prompt 上下文）  │
    │                                      │
    ├──▶ impact_analyzer.py（新增）        │
    │   ├── 变更类型分类                   │
    │   ├── 头文件级联分析                 │
    │   ├── 调用链提取                     │
    │   └── 影响面扩展                     │
    │                                      │
    ├──▶ sast_engine.py（新增）            │
    │   ├── Semgrep 规则引擎               │
    │   ├── Cppcheck 补充分析              │
    │   └── 结果聚合（SARIF 中间格式）      │
    │                                      │
    ├──▶ knowledge_extractor.py（新增）────┘
    │   └── 从 nga/SAST 输出中提取结构化知识
    │
    └──▶ output_formats.py（新增）
        ├── Markdown 生成（兼容现有）
        ├── SARIF 2.1.0 输出
        └── SQLite 结果持久化
```

### 3.2 数据模型

#### 3.2.1 知识图谱 Schema

```python
@dataclass
class KnowledgeNode:
    """知识图谱节点基类"""
    id: str                # 全局唯一 ID，如 "PATTERN-001"
    type: str              # "pattern" | "rule" | "case" | "fix"
    content: str           # 自然语言描述
    embedding: list[float] # 用于模糊匹配的向量（384/768/1536 维）
    source: str            # "manual" | "extracted_from_scan" | "extracted_from_review"
    confidence: float      # 0.0~1.0，用于结果排序和过滤
    created_at: str        # ISO 8601
    updated_at: str        # ISO 8601
    metadata: dict         # 扩展字段

@dataclass
class KnowledgeEdge:
    """知识图谱边（关系）"""
    source_id: str         # 源节点 ID
    target_id: str         # 目标节点 ID
    relation: str          # "detected_by" | "instance_of" | "located_in" | "fixed_by"
    weight: float          # 关系强度
    evidence: str          # 证据描述
```

#### 3.2.2 节点类型详解

| 类型 | 说明 | 示例 |
|------|------|------|
| **Pattern** | 问题模式，可跨文件复用 | "TLV 解析缺少边界检查" |
| **Rule** | 检测规则，与 `skills/` 对应 | RULE-001 ~ RULE-030 |
| **Case** | 历史实例，绑定到具体代码位置 | "src/rr/pdu.c:89 的 memcpy 溢出" |
| **Fix** | 修复方案，绑定到 Case | "增加 `if (len > max) return ERROR;`" |
| **FileProfile** | 文件风险画像 | "src/rr/pdu.c: 历史发现 5 个问题，3 个与 TLV 相关" |

#### 3.2.3 边关系类型

| 关系 | 源类型 | 目标类型 | 说明 |
|------|--------|---------|------|
| `PATTERN -[detected_by]-> RULE` | Pattern | Rule | 该模式由哪条规则检测 |
| `CASE -[instance_of]-> PATTERN` | Case | Pattern | 该实例属于哪个模式 |
| `CASE -[located_in]-> FILE` | Case | FileProfile | 实例发生在哪个文件 |
| `CASE -[fixed_by]-> FIX` | Case | Fix | 该实例的修复方案 |
| `RULE -[depends_on]-> RULE` | Rule | Rule | 规则间的依赖关系 |

### 3.3 数据库 Schema（SQLite）

```sql
-- 节点表
CREATE TABLE knowledge_nodes (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('pattern', 'rule', 'case', 'fix', 'file_profile')),
    content TEXT NOT NULL,
    embedding BLOB,          -- JSON 序列化的 float 数组
    source TEXT NOT NULL,
    confidence REAL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    metadata TEXT            -- JSON 对象
);

-- 边表
CREATE TABLE knowledge_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    evidence TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (source_id) REFERENCES knowledge_nodes(id),
    FOREIGN KEY (target_id) REFERENCES knowledge_nodes(id)
);

-- 扫描运行记录
CREATE TABLE scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_hash TEXT,
    branch TEXT,
    timestamp TEXT DEFAULT (datetime('now')),
    total_files INTEGER,
    issues_found INTEGER,
    issues_confirmed INTEGER,
    issues_false_positive INTEGER,
    duration REAL,
    metadata TEXT
);

-- 问题实例（与 scan_runs 关联）
CREATE TABLE issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    line_number INTEGER,
    rule_id TEXT,
    severity TEXT CHECK (severity IN ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW')),
    message TEXT NOT NULL,
    code_snippet TEXT,
    confidence REAL,
    embedding BLOB,
    status TEXT DEFAULT 'open' CHECK (status IN ('open', 'confirmed', 'false_positive', 'fixed', 'suppressed')),
    knowledge_node_id TEXT,  -- 关联到 knowledge_nodes（如果是已知模式）
    FOREIGN KEY (scan_id) REFERENCES scan_runs(id),
    FOREIGN KEY (knowledge_node_id) REFERENCES knowledge_nodes(id)
);

-- 文件风险画像
CREATE TABLE file_profiles (
    file_path TEXT PRIMARY KEY,
    total_issues INTEGER DEFAULT 0,
    last_scan_id INTEGER,
    risk_score REAL DEFAULT 0.0,  -- 综合风险评分
    top_patterns TEXT,            -- JSON 数组，该文件最常见的模式 ID
    updated_at TEXT DEFAULT (datetime('now'))
);

-- 索引
CREATE INDEX idx_nodes_type ON knowledge_nodes(type);
CREATE INDEX idx_nodes_embedding ON knowledge_nodes(embedding);  -- 需要 sqlite-vss 扩展
CREATE INDEX idx_issues_scan_id ON issues(scan_id);
CREATE INDEX idx_issues_file_path ON issues(file_path);
CREATE INDEX idx_issues_status ON issues(status);
CREATE INDEX idx_edges_source ON knowledge_edges(source_id);
CREATE INDEX idx_edges_target ON knowledge_edges(target_id);
```

---

## 4. 模块详细设计

### 4.1 knowledge_graph.py

职责：知识图谱的存储、查询、去重、注入。

#### 4.1.1 核心接口

```python
class KnowledgeGraph:
    """知识图谱管理器"""

    def __init__(self, db_path: str = ".claude/knowledge.db"):
        """初始化数据库连接，自动建表"""

    # ── 写入 ──
    def add_node(self, node: KnowledgeNode) -> str:
        """添加节点，自动去重（embedding 相似度 > 0.92 则合并）"""

    def add_edge(self, edge: KnowledgeEdge) -> int:
        """添加关系边"""

    def find_duplicate(self, node: KnowledgeNode) -> Optional[str]:
        """查找相似节点，返回已有节点 ID 或 None"""

    # ── 查询（用于 prompt 注入）──
    def find_relevant_patterns(
        self,
        file_path: str,
        code_snippet: str,
        top_k: int = 5,
        min_confidence: float = 0.6,
    ) -> list[KnowledgeNode]:
        """
        为给定代码片段找到最相关的历史模式。
        策略：
        1. 先按 file_path 精确匹配该文件的历史 Case
        2. 再用 code_snippet 的 embedding 做相似度匹配全局 Pattern
        3. 合并结果，按 confidence 排序返回 Top-K
        """

    def get_file_profile(self, file_path: str) -> Optional[FileProfile]:
        """获取文件风险画像"""

    def get_last_scan_issues(self, file_path: str) -> list[Issue]:
        """获取该文件上次扫描发现的问题列表"""

    # ── 反馈闭环 ──
    def mark_false_positive(self, issue_id: int, reason: str = ""):
        """标记误报，降低关联 Pattern 的 confidence"""

    def mark_confirmed(self, issue_id: int):
        """标记有效，提升关联 Pattern 的 confidence"""

    def update_pattern_confidence(self, pattern_id: str, delta: float):
        """调整模式置信度（delta 可为负）"""
```

#### 4.1.2 Embedding 策略

| 场景 | 模型选择 | 维度 | 存储方式 |
|------|---------|------|---------|
| 本地轻量（默认） | `sentence-transformers/all-MiniLM-L6-v2` | 384 | numpy 数组序列化到 BLOB |
| 高精度（可选） | OpenAI `text-embedding-3-small` | 1536 | 同上 |
| 代码专用（可选） | `microsoft/codebert-base` | 768 | 同上 |

**相似度计算**：余弦相似度，阈值 0.92 视为重复。

**向量检索实现**：
- 方案 A（推荐）：`sqlite-vss` 扩展（SQLite Vector Similarity Search）
- 方案 B（fallback）：暴力搜索（节点数 < 10K 时性能可接受）
- 方案 C：`faiss` 内存索引（定期持久化到磁盘）

### 4.2 impact_analyzer.py

职责：分析代码变更的影响范围，自动扩展扫描队列。

#### 4.2.1 变更类型分类器

```python
class ChangeType(Enum):
    LOG_ONLY = "log_only"           # 仅日志级别修改，影响极小
    COMMENT = "comment"             # 注释修改
    VARIABLE_RENAME = "var_rename"  # 变量重命名
    POINTER_ARITHMETIC = "ptr_arith" # 指针运算调整
    STRUCT_DEFINITION = "struct_def" # 结构体定义变更
    ENUM_DEFINITION = "enum_def"     # 枚举定义变更
    MACRO_DEFINITION = "macro_def"   # 宏定义变更
    FUNCTION_SIGNATURE = "func_sig"  # 函数签名变更
    GLOBAL_VARIABLE = "global_var"   # 全局变量变更
    LOCK_STRATEGY = "lock_strategy"  # 锁策略变更
    PROTOCOL_VERSION = "proto_ver"   # 协议版本号变更

class ChangeClassifier:
    """基于 AST/正则快速分类变更类型"""

    def classify(self, diff_content: str) -> ChangeType:
        """
        实现方式：
        1. 正则快速匹配（80% 场景）
           - LOG_ONLY: `^[+-].*LOG_\w+\s*\(` 模式变化
           - COMMENT: `^[+-]\s*//` 或 `^[+-]\s*/\*`
           - STRUCT_DEFINITION: `^[+-]\s*(struct|typedef struct)\b`
           - ...
        2. AST 精确匹配（复杂场景）
           - 使用 tree-sitter 解析 diff 片段，提取变更的 AST 节点类型
        """
```

#### 4.2.2 影响面扩展策略

```python
class ImpactAnalyzer:
    """影响面分析器"""

    def analyze(self, changed_files: list[str], repo: Path) -> ImpactResult:
        """
        对每个变更文件，分析其影响范围，返回需要额外关注的文件列表。
        """

    def expand_scan_queue(
        self,
        primary_files: list[str],
        repo: Path,
    ) -> tuple[list[str], dict[str, list[str]]]:
        """
        扩展扫描队列。

        返回：
        - expanded_files: 扩展后的完整文件列表
        - context_map: dict[primary_file, list[context_files]]
          每个主文件对应的上下文文件（用于 prompt 注入，不生成独立报告）
        """
```

| 变更类型 | 扩展策略 | 上下文注入方式 |
|---------|---------|--------------|
| `LOG_ONLY` | 不扩展 | 仅当前文件 |
| `COMMENT` | 不扩展 | 仅当前文件 |
| `STRUCT_DEFINITION` | 找到所有 `#include` 该头文件的 `.c` 文件 | 主文件 + 引用文件列表 |
| `FUNCTION_SIGNATURE` | 找到所有 caller（通过 ctags/grep） | 主文件 + caller 文件列表 + callee 文件列表 |
| `GLOBAL_VARIABLE` | 找到所有读写该变量的文件 | 主文件 + 访问文件列表 |
| `MACRO_DEFINITION` | 找到所有展开位置 | 主文件 + 展开文件列表 |
| `PROTOCOL_VERSION` | 整个协议栈模块 | 全模块文件列表 |

#### 4.2.3 调用链提取

```python
class CallGraphExtractor:
    """调用图提取器"""

    def __init__(self, compile_commands: Optional[Path] = None):
        """
        compile_commands: compile_commands.json 路径。
        如果存在，使用 clang LibTooling 精确提取；
        如果不存在，使用 ctags + grep 做近似提取。
        """

    def get_callers(self, function_name: str, repo: Path) -> list[str]:
        """获取函数的所有调用者（文件路径列表）"""

    def get_callees(self, function_name: str, file_path: str) -> list[str]:
        """获取函数调用的所有子函数"""

    def get_call_chain(
        self,
        entry_function: str,
        max_depth: int = 3,
    ) -> list[list[str]]:
        """获取从入口函数出发的调用链（截断到 max_depth）"""
```

**工具链选择**：

| 精度要求 | 工具 | 依赖 | 适用场景 |
|---------|------|------|---------|
| 高（推荐） | `clang-check` + `LibTooling` | compile_commands.json | 有完整构建系统的项目 |
| 中 | `universal-ctags` + `grep` | 无 | 快速启动，无需构建系统 |
| 低 | 纯 `grep` | 无 | 极简环境 |

### 4.3 sast_engine.py

职责：本地静态分析引擎，处理明确模式的问题，减少 LLM 调用。

#### 4.3.1 架构

```python
class SASTEngine:
    """本地静态分析引擎"""

    def __init__(self, rules_dir: Path = Path("skills/semgrep")):
        self.rules_dir = rules_dir
        self.tools: list[BaseSASTTool] = [
            SemgrepTool(rules_dir),
            CppcheckTool(),
        ]

    def scan(self, file_path: str) -> list[SASTIssue]:
        """
        对每个工具：
        1. 调用工具扫描文件
        2. 解析输出（SARIF / JSON / XML）
        3. 转换为统一的 SASTIssue 格式
        4. 合并结果（去重）
        """

    def scan_batch(self, file_paths: list[str]) -> dict[str, list[SASTIssue]]:
        """批量扫描，复用工具进程"""
```

#### 4.3.2 工具集成

| 工具 | 用途 | 规则来源 | 输出格式 | 集成方式 |
|------|------|---------|---------|---------|
| **Semgrep** | 自定义规则，模式匹配 | `skills/semgrep/*.yaml` | SARIF / JSON | subprocess |
| **Cppcheck** | 通用 C/C++ 分析 | 内置规则 + `--enable=all` | XML / JSON | subprocess |
| **Clang SA** | 深度路径敏感分析 | 内置 checkers | plist / SARIF | subprocess |

#### 4.3.3 统一问题格式

```python
@dataclass
class SASTIssue:
    """本地 SAST 工具输出的统一格式"""
    tool: str              # "semgrep" | "cppcheck" | "clang_sa"
    rule_id: str           # 规则 ID
    severity: str          # "CRITICAL" | "HIGH" | "MEDIUM" | "LOW"
    file_path: str
    line_number: int
    column: int
    message: str
    code_snippet: str
    confidence: float      # 工具给出的置信度
    fix_suggestion: Optional[str]
    metadata: dict         # 工具特定字段
```

#### 4.3.4 LLM 分流策略

```python
def route_issue(issue: SASTIssue) -> RouteDecision:
    """
    决定该问题如何处理：
    - 高置信度 + 明确模式 → 直接输出，无需 LLM
    - 中置信度 + 涉及跨文件 → 发给 LLM 深度分析
    - 低置信度 → 发给 LLM 验证
    """
    if issue.confidence >= 0.9 and issue.rule_id in PRECISE_RULES:
        return RouteDecision.DIRECT_OUTPUT
    elif issue.confidence >= 0.7:
        return RouteDecision.LLM_ENHANCE  # LLM 补充上下文分析
    else:
        return RouteDecision.LLM_VERIFY   # LLM 验证是否真实问题
```

### 4.4 knowledge_extractor.py

职责：从扫描结果（nga stdout / SAST 输出）中自动提取结构化知识。

#### 4.4.1 提取流程

```python
class KnowledgeExtractor:
    """知识提取器"""

    def extract_from_nga(
        self,
        task: ScanTask,
    ) -> list[KnowledgeNode]:
        """
        从 nga 的 stdout 中提取知识条目。

        策略：
        1. 基于规则 ID 正则提取：
           扫描 stdout 中 `RULE-\d{3}` 标记的段落
        2. 对每个匹配段落，提取：
           - 问题描述（自然语言）
           - 代码片段（```c ... ``` 内的内容）
           - 修复建议（"建议" / "修复" 后的内容）
           - 置信度（"置信度：X%" 或默认 0.8）
        3. 生成节点：
           - 如果匹配到已知 Pattern → 创建 Case 节点，关联到 Pattern
           - 如果未匹配 → 创建新 Pattern 节点 + Case 节点
        """

    def extract_from_sast(
        self,
        issues: list[SASTIssue],
    ) -> list[KnowledgeNode]:
        """从 SAST 输出中提取（主要用于补充 Pattern 的实例）"""

    def generate_embedding(self, text: str) -> list[float]:
        """生成文本的 embedding 向量"""
```

#### 4.4.2 提取 Prompt 模板（用于 nga 输出规范化）

为了让提取更可靠，可以在发给 `nga` 的 prompt 中要求按固定格式输出发现的问题：

```markdown
## 审查结果格式要求

对每个发现的问题，请按以下格式输出：

### 问题 [RULE-XXX]
- **文件**: `path/to/file.c`
- **行号**: 123
- **描述**: 问题描述（1-2 句话）
- **代码片段**:
  ```c
  // 有问题的代码
  ```
- **修复建议**: 如何修复
- **置信度**: 高/中/低

如果没有发现问题，输出 "未发现明显问题"。
```

这样 `knowledge_extractor.py` 可以用正则精确提取，无需再调用 LLM。

### 4.5 output_formats.py

职责：统一输出生成，支持多种格式。

#### 4.5.1 输出格式

```python
class OutputManager:
    """输出管理器"""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir

    def write_markdown(self, task: ScanTask) -> Path:
        """生成 Markdown 报告（兼容现有格式）"""

    def write_sarif(self, tasks: list[ScanTask], run_id: int) -> Path:
        """生成 SARIF 2.1.0 格式报告"""

    def write_json(self, tasks: list[ScanTask]) -> Path:
        """生成 JSON 格式（内部使用）"""

    def save_to_db(self, scan_run: ScanRun, tasks: list[ScanTask]):
        """保存到 SQLite 数据库"""
```

#### 4.5.2 SARIF 映射

| 我们的字段 | SARIF 字段 | 说明 |
|-----------|-----------|------|
| `rule_id` | `runs[].tool.driver.rules[].id` | 规则标识 |
| `severity` | `runs[].results[].level` | error/warning/note |
| `file_path:line_number` | `runs[].results[].locations[]` | 物理位置 |
| `message` | `runs[].results[].message.text` | 问题描述 |
| `code_snippet` | `runs[].results[].locations[].snippet` | 代码片段 |
| `confidence` | `runs[].results[].properties.confidence` | 扩展属性 |
| `fix_suggestion` | `runs[].results[].fixes[]` | 修复建议 |

---

## 5. Orchestrator 改造点

### 5.1 主流程改造

```python
class OpenCodeOrchestrator:
    def __init__(self, ...):
        # 新增依赖
        self.knowledge_graph = KnowledgeGraph()
        self.impact_analyzer = ImpactAnalyzer()
        self.sast_engine = SASTEngine()
        self.knowledge_extractor = KnowledgeExtractor()
        self.output_manager = OutputManager(self.output_dir)

    async def run(self):
        if not self.tasks:
            return

        # 1. 影响面分析（新增）
        self.tasks = self._expand_with_impact(self.tasks)

        # 2. 本地 SAST 预扫描（新增）
        sast_results = self._run_sast(self.tasks)

        # 3. 分流：高置信度直接输出，其余进入 LLM
        direct_tasks, llm_tasks = self._route_tasks(self.tasks, sast_results)

        # 4. 对 LLM 任务注入知识上下文（改造）
        for task in llm_tasks:
            task.enhanced_prompt = self._build_enhanced_prompt(task)

        # 5. 并发扫描（现有逻辑，小幅改造）
        coros = [self._scan_one(task, tracker) for task in llm_tasks]
        await asyncio.gather(*coros, return_exceptions=True)

        # 6. 合并结果
        all_results = self._merge_results(direct_tasks, llm_tasks)

        # 7. 知识提取与入库（新增）
        self._ingest_knowledge(all_results)

        # 8. 生成多格式报告（改造）
        self.output_manager.write_markdown_batch(all_results)
        self.output_manager.write_sarif(all_results, scan_run_id)
        self.output_manager.save_to_db(scan_run_id, all_results)
```

### 5.2 Prompt 构建改造

```python
def _build_enhanced_prompt(self, task: ScanTask) -> str:
    """构建增强版审查 prompt，注入知识上下文"""

    parts = []

    # 1. 基础指令
    parts.append(f"请审查文件 {task.file_path} 的代码变更。\n")

    # 2. 知识上下文（新增）
    relevant = self.knowledge_graph.find_relevant_patterns(
        task.file_path, task.diff_content, top_k=5
    )
    if relevant:
        parts.append("## 历史风险模式（请重点检查）\n")
        for pattern in relevant:
            parts.append(f"- **{pattern.id}**: {pattern.content} "
                        f"(置信度: {pattern.confidence:.0%})\n")
        parts.append("\n")

    # 3. 文件风险画像（新增）
    profile = self.knowledge_graph.get_file_profile(task.file_path)
    if profile and profile.risk_score > 0:
        parts.append(f"## 文件风险画像\n")
        parts.append(f"该文件历史发现 {profile.total_issues} 个问题，"
                    f"风险评分: {profile.risk_score:.1f}\n")
        parts.append(f"常见模式: {', '.join(profile.top_patterns[:3])}\n\n")

    # 4. 影响上下文（新增）
    if task.impacted_files:
        parts.append(f"## 影响范围\n")
        parts.append(f"本次变更可能影响以下文件:\n")
        for f in task.impacted_files:
            parts.append(f"- `{f}`\n")
        parts.append("\n")

    # 5. 历史问题（新增）
    last_issues = self.knowledge_graph.get_last_scan_issues(task.file_path)
    if last_issues:
        parts.append(f"## 上次扫描遗留问题\n")
        for issue in last_issues:
            if issue.status == "open":
                parts.append(f"- [{issue.severity}] {issue.message} "
                            f"(行 {issue.line_number})\n")
        parts.append("\n")

    # 6. 审查要求（现有）
    parts.append("## 审查要求\n")
    parts.append("1. 应用无线通信安全编码规则（RULE-001~RULE-030）\n")
    parts.append("2. 如果变更在函数内部，请审查完整实现、caller、callee\n")
    parts.append("3. 如果变更涉及全局符号，请找到所有使用点一并审查\n")
    parts.append("4. 对每个问题提供：文件路径、行号、描述、代码片段、修复建议、置信度\n")
    parts.append("5. 参考上述历史风险模式，特别注意类似问题是否再次发生\n")

    return "".join(parts)
```

### 5.3 任务模型扩展

```python
@dataclass
class ScanTask:
    # 现有字段...
    file_path: str
    task_id: str
    report_file: str
    log_file: str
    status: str = "pending"
    # ...

    # 新增字段
    impacted_files: list[str] = field(default_factory=list)
    """影响面分析识别的关联文件（作为上下文，不独立扫描）"""

    enhanced_prompt: str = ""
    """注入知识上下文后的完整 prompt"""

    sast_issues: list[SASTIssue] = field(default_factory=list)
    """本地 SAST 预扫描发现的问题"""

    knowledge_context: list[KnowledgeNode] = field(default_factory=list)
    """注入该任务的知识上下文（用于追溯）"""
```

---

## 6. 实施路线图

### Phase 0: 基础设施（1 周）

**目标**：建立知识存储和基础查询能力，验证最小闭环。

| 任务 | 说明 | 交付物 |
|------|------|--------|
| 0.1 设计数据库 Schema | 按本文 3.3 节建表 | `schema.sql` |
| 0.2 实现 `knowledge_graph.py` | 基础 CRUD + 查询 | `knowledge_graph.py` |
| 0.3 数据迁移 | 将 `wireless-radio.md` 手动导入为 Pattern 节点 | 初始知识库 |
| 0.4 改造 `_build_diff_scan_cmd` | 注入历史问题上下文（先不加 embedding，直接按文件路径匹配） | 代码改动 |
| 0.5 改造扫描后处理 | 扫描完成后提取 RULE-XXX 标记的问题入库 | 代码改动 |
| 0.6 验证 | 同一文件扫描两次，观察第二次是否因为历史上下文而表现不同 | 测试报告 |

**验收标准**：
- 同一文件连续扫描时，第二次扫描的 prompt 包含第一次发现的问题
- 知识库能正确存储和查询 Pattern / Case 节点
- 不影响现有并发和输出格式

### Phase 1: 本地 SAST 引入（1-2 周）

**目标**：建立本地快速扫描能力，减少 70% 以上的 LLM 调用。

| 任务 | 说明 | 交付物 |
|------|------|--------|
| 1.1 编写 Semgrep 规则 | 将 RULE-001~RULE-010 翻译为 YAML 规则 | `skills/semgrep/` |
| 1.2 实现 `sast_engine.py` | 封装 Semgrep + Cppcheck 调用 | `sast_engine.py` |
| 1.3 结果聚合 | 统一 Semgrep / Cppcheck / nga 的结果格式 | 聚合逻辑 |
| 1.4 分流策略 | 高置信度直接输出，其余给 LLM | `route_issue()` |
| 1.5 性能基准 | 对比纯 LLM vs SAST+LLM 的耗时和检出率 | 基准测试报告 |

**验收标准**：
- 明确模式（如 `strcpy` 使用）由 Semgrep 检出，无需 LLM
- 全量扫描时间从"小时级"降至"分钟级"
- LLM 调用量下降 > 50%

### Phase 2: 影响面分析（2 周）

**目标**：解决头文件级联、调用链分析等跨文件场景。

| 任务 | 说明 | 交付物 |
|------|------|--------|
| 2.1 实现 `ChangeClassifier` | 正则 + tree-sitter 分类变更类型 | `impact_analyzer.py` |
| 2.2 头文件级联分析 | `grep -r "#include"` 或 `clangd-index` | 级联扫描逻辑 |
| 2.3 调用链提取 | ctags / compile_commands.json + clang | `CallGraphExtractor` |
| 2.4 影响面扩展集成 | 修改 `setup_diff_mode`，自动扩展扫描队列 | orchestrator 改造 |
| 2.5 上下文打包 | 将关联文件内容打包为 prompt 上下文 | prompt 构建改造 |

**验收标准**：
- 修改头文件中的结构体定义后，引用该头文件的 `.c` 文件自动加入扫描队列
- 修改函数签名后，caller 文件作为上下文注入 prompt

### Phase 3: Embedding 驱动（2 周）

**目标**：实现模糊知识匹配和历史问题复用。

| 任务 | 说明 | 交付物 |
|------|------|--------|
| 3.1 接入 embedding 模型 | `sentence-transformers` 或 OpenAI API | embedding 服务 |
| 3.2 向量存储 | `sqlite-vss` 或 `faiss` | 向量索引 |
| 3.3 相似度匹配 | 实现 `find_relevant_patterns()` | 匹配算法 |
| 3.4 Prompt 增强 | 将 Top-K 匹配结果注入审查 prompt | prompt 改造 |
| 3.5 去重机制 | 相似度 > 0.92 自动合并 | 去重逻辑 |

**验收标准**：
- 新代码与历史问题模式相似时，自动在 prompt 中提示
- 知识库自动去重，相同模式不重复入库
- 向量检索延迟 < 100ms

### Phase 4: 结构化输出与度量（1-2 周）

**目标**：建立度量体系和 CI/CD 集成能力。

| 任务 | 说明 | 交付物 |
|------|------|--------|
| 4.1 SARIF 输出 | 实现 SARIF 2.1.0 格式生成 | `output_formats.py` |
| 4.2 SQLite 持久化 | 扫描结果入数据库 | 数据库写入逻辑 |
| 4.3 趋势分析 | 按 commit 统计缺陷趋势 | 查询接口 |
| 4.4 误报反馈 | 支持标记误报，调整置信度 | 反馈接口 |
| 4.5 Web 看板扩展 | 在现有 `web_server.py` 上增加知识库浏览 | 前端页面 |
| 4.6 CI/CD 模板 | GitLab CI + GitHub Actions 模板 | `.gitlab-ci.yml` / `action.yml` |

**验收标准**：
- 输出 SARIF 可被 GitHub Advanced Security 消费
- 能生成缺陷趋势报告（按 commit）
- 提供 CI 门禁配置（CRITICAL 阻塞合并）

---

## 7. 风险评估与应对

| 风险 | 可能性 | 影响 | 应对措施 |
|------|--------|------|---------|
| Embedding 模型过大，本地部署困难 | 中 | 高 | 优先使用 API（OpenAI），后期再考虑本地量化模型 |
| sqlite-vss 扩展安装复杂 | 中 | 中 | 提供 fallback 方案（暴力搜索），节点数 < 10K 时性能可接受 |
| compile_commands.json 生成困难 | 高 | 中 | 优先使用 ctags 方案，clang 方案作为可选增强 |
| 知识提取准确率不足 | 中 | 高 | 先基于规则 ID 正则提取（高准确），再逐步引入 LLM 提取 |
| 改造期间影响现有功能 | 低 | 高 | 每个 Phase 独立分支开发，通过 flag 开关，不影响主干 |
| Semgrep 对 C/C++ 支持有限 | 中 | 中 | Semgrep 处理明确模式，复杂场景仍走 LLM，不追求 100% 覆盖 |

---

## 8. 与现有 `OPTIMIZATION.md` 的关系

| 本文 Phase | OPTIMIZATION.md 对应 | 补充说明 |
|-----------|---------------------|---------|
| Phase 0 | 无（新增） | 知识图谱化是本文核心新增方向 |
| Phase 1 | Phase 1: 规则扩展 + 本地引擎 | 完全对齐，本文增加 SAST 结果入知识库的细节 |
| Phase 2 | Phase 2: 跨文件分析 | 完全对齐，本文细化调用链提取工具链选型 |
| Phase 3 | 无（新增） | Embedding 驱动是本文核心新增方向 |
| Phase 4 | Phase 3 + Phase 4: 度量 + CI/CD | 完全对齐，本文增加 SARIF 输出和反馈闭环细节 |

**建议**：两个文档并行使用。`OPTIMIZATION.md` 侧重**技术选型**和**工具对比**，本文侧重**数据架构**和**知识流动**。

---

## 9. 附录

### 9.1 项目结构（改造后）

```
.
├── orchestrator.py              # 主调度器（改造）
├── web_server.py                # Web 调试界面（现有）
├── knowledge_graph.py           # 知识图谱管理（新增）
├── impact_analyzer.py           # 影响面分析（新增）
├── sast_engine.py               # 本地 SAST 引擎（新增）
├── knowledge_extractor.py       # 知识提取器（新增）
├── output_formats.py            # 多格式输出（新增）
├── skills/
│   ├── wireless-scan.claude.md  # Claude 标准格式（现有）
│   ├── wireless-scan.mcp.json   # MCP Tool 标准格式（现有）
│   ├── wireless-scan.yaml       # 通用 YAML 标准格式（现有）
│   └── semgrep/                 # Semgrep 规则（新增）
│       ├── rule-001-tlv-bound-check.yaml
│       ├── rule-002-struct-cast.yaml
│       └── ...
├── knowleage/
│   └── wireless-radio.md        # 源知识文档（保留，作为初始导入源）
├── .claude/
│   └── knowledge.db             # SQLite 知识库（新增，gitignore）
├── .claude/
│   └── knowledge.db             # SQLite 知识库（新增，gitignore）
├── reports/                      # 扫描报告输出（现有）
│   └── YYYYMMDD_HHMMSS/
│       ├── summary.md
│       ├── summary.sarif        # SARIF 输出（新增）
│       ├── scan_result.json     # JSON 输出（新增）
│       └── ...
├── schema.sql                    # 数据库建表脚本（新增）
├── README.md                     # 使用文档（更新）
├── SPEC.md                       # 项目规格（现有）
├── OPTIMIZATION.md               # C/C++ 扫描优化计划（现有）
└── KNOWLEDGE_GRAPH_PLAN.md       # 本文档
```

### 9.2 关键依赖

```
# 新增依赖
sentence-transformers>=2.2.0   # 本地 embedding 模型（可选）
semgrep>=1.50.0                # 本地 SAST 引擎
sqlite-vss>=0.1.0              # SQLite 向量扩展（可选）
tree-sitter>=0.20.0            # AST 解析（可选）
tree-sitter-c>=0.20.0          # C 语言 grammar

# 现有依赖（不变）
fastapi>=0.100.0
uvicorn>=0.23.0
gunicorn>=21.0.0
httpx>=0.24.0
```

### 9.3 术语表

| 术语 | 说明 |
|------|------|
| **Knowledge Graph** | 知识图谱，由节点（知识条目）和边（关系）组成的图结构 |
| **Pattern** | 问题模式，可跨文件复用的抽象知识 |
| **Case** | 历史实例，绑定到具体代码位置的具象知识 |
| **Impact Surface** | 变更代码的影响范围 |
| **Embedding** | 文本的高维向量表示，用于相似度计算 |
| **SARIF** | Static Analysis Results Interchange Format，静态分析结果交换格式 |
| **SAST** | Static Application Security Testing，静态应用安全测试 |
| **Tribal Knowledge** | 部落知识，未文档化的隐性领域知识 |
| **Feedback Loop** | 反馈闭环，扫描结果回流知识库的持续改进机制 |

---

*Plan Version: 1.0*
*Date: 2026-05-02*
*Based on: Meta Engineering Blog "How Meta used AI to map tribal knowledge in large-scale data pipelines" (2026-04-06)*
