#!/usr/bin/env python3
import sys

with open('orchestrator.py', 'r') as f:
    content = f.read()

old = '''    if task.stdout.strip():
        # 直接展示 nga 的审查结果，不加代码块包装
        lines.append(task.stdout)
    else:'''

new = '''    if task.stdout.strip():
        # 过滤思考过程（以 "Thinking:" 开头的行），只保留审查结果
        review_lines = [
            line for line in task.stdout.splitlines()
            if not line.startswith("Thinking:")
        ]
        lines.append("\n".join(review_lines))
    else:'''

if old not in content:
    print("FAIL: old text not found")
    sys.exit(1)

content = content.replace(old, new, 1)

with open('orchestrator.py', 'w') as f:
    f.write(content)

print("OK")
