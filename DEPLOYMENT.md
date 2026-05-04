# 部署与测试指南

## 1. 获取代码

```bash
git clone https://github.com/Atituiset/event-loop-agent.git
cd event-loop-agent
git checkout maybe-harness
```

## 2. 安装依赖

```bash
# 核心依赖（必须）
pip install -r requirements.txt

# 可选：Phase 1 SAST 本地引擎（强烈推荐）
pip install semgrep

# 系统包：Cppcheck（可选补充）
# Ubuntu/Debian: apt-get install cppcheck
# macOS: brew install cppcheck
# CentOS/RHEL: yum install cppcheck
```

## 3. 验证环境

```bash
# 检查 nga 是否可用
nga --version

# 检查 semgrep 是否可用（可选）
semgrep --version

# 验证所有模块可导入
python3 -c "import orchestrator, knowledge_graph, sast_engine, impact_analyzer"
```

## 4. 初始化知识库

```bash
python3 init_knowledge.py
```

预期输出：
```
Initializing knowledge graph: .claude/knowledge.db
Imported 5 patterns from knowleage/wireless-radio.md
Knowledge graph stats: {'patterns': 5, 'cases': 0, 'file_profiles': 0, 'scan_runs': 0}
```

## 5. 运行扫描测试

### 5.1 文件列表模式（最简单）

```bash
# 扫描单个文件
python3 orchestrator.py --files src/rr/pdu_parser.c

# 扫描多个文件
python3 orchestrator.py --files src/rr/*.c src/mac/*.c

# 扫描目录（递归）
python3 orchestrator.py --files src/rr src/mac
```

### 5.2 Diff 模式（推荐，完整功能）

```bash
# 进入你的代码仓库
cd /path/to/your/wireless-project

# 扫描从 abc123 到 HEAD 的所有变更
python3 /path/to/event-loop-agent/orchestrator.py \
    --diff abc123 \
    --repo . \
    -c 3

# 只扫描指定目录的变更
python3 /path/to/event-loop-agent/orchestrator.py \
    --diff abc123 \
    --paths src/rr,src/mac \
    --repo . \
    -c 3
```

### 5.3 启用 Web 调试界面

```bash
python3 orchestrator.py \
    --diff abc123 \
    --repo . \
    --debug \
    --web-port 8080

# 浏览器打开 http://localhost:8080
```

## 6. 查看结果

```bash
# 查看最新报告目录
ls -lt reports/ | head -5

# 查看汇总报告
cat reports/20250504_*/summary.md

# 查看 SARIF 结果（CI/CD 用）
cat reports/20250504_*/scan-results.sarif

# 查看 JSON 结果
cat reports/20250504_*/scan-results.json

# 查询知识库状态
python3 query_knowledge.py stats
python3 query_knowledge.py patterns
```

## 7. 验证知识图谱工作

运行两次同一文件的扫描，观察第二次的 prompt 是否包含第一次发现的问题：

```bash
# 第一次扫描
python3 orchestrator.py --files src/rr/test.c

# 查看知识库，应该有了新的 case
python3 query_knowledge.py cases src/rr/test.c

# 第二次扫描同一文件，日志中应显示 "知识上下文注入"
python3 orchestrator.py --files src/rr/test.c
```

## 8. CI/CD 集成

### GitHub Actions

复制 `.github/workflows/code-scan.yml` 到你的仓库 `.github/workflows/` 目录即可。

### GitLab CI

复制 `.gitlab-ci.yml` 到你的仓库根目录即可。

## 常见问题

### Q: semgrep 未安装，SAST 不工作？
不影响。系统会自动降级为纯 LLM 模式，只是扫描时间会稍长。

### Q: nga 进程超时或残留？
orchestrator 已实现：
- 软超时（SIGTERM）+ 硬超时（SIGKILL）
- 超时后自动清理子进程
- 每次任务前清理残留锁文件

### Q: 扫描结果为空？
检查：
1. `nga` 是否正常运行（单独执行 `nga run 'review test.c'` 测试）
2. 文件路径是否正确
3. diff 范围是否有 C/C++ 文件变更

### Q: 影响面分析找不到关联文件？
影响面分析依赖 `grep`，在大型仓库中可能需要几秒。如果仓库路径特殊，检查 `impact_analyzer.py` 中的 `repo_path` 是否正确。
