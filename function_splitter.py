"""Tree-sitter 函数切分模块

从 C/C++ 源文件中提取所有函数定义，包括：
- C 函数定义
- C++ 函数定义（含类方法、构造函数、析构函数）
- 模板函数定义

提取的信息：
- 函数名（含类名前缀，如 MyClass::foo）
- 起始行号和结束行号
- 函数代码文本（含前置注释）
- AST 元数据（返回类型、参数、修饰符、内存操作、分支复杂度）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tree_sitter import Language, Node, Parser

# tree-sitter grammar imports (installed from pip)
import tree_sitter_c as tsc
import tree_sitter_cpp as tscpp


@dataclass
class FunctionInfo:
    """单个函数的信息"""
    name: str
    start_line: int
    end_line: int
    code_text: str
    metadata: dict = field(default_factory=dict)


def _get_language(file_path: str) -> Language:
    """根据文件扩展名选择 tree-sitter 语言"""
    ext = Path(file_path).suffix.lower()
    if ext in (".cpp", ".cc", ".hpp"):
        return Language(tscpp.language())
    return Language(tsc.language())


def _extract_name_from_declarator(node: Node) -> str:
    """递归从 declarator 树中提取函数名"""
    if node.type in ("identifier", "field_identifier"):
        return node.text.decode("utf-8")
    if node.type == "destructor_name":
        return "~" + node.text.decode("utf-8")
    if node.type == "qualified_identifier":
        parts = []
        for child in node.children:
            if child.type in ("identifier", "field_identifier", "destructor_name", "operator_name"):
                parts.append(child.text.decode("utf-8"))
            elif child.type == "::":
                parts.append("::")
        return "".join(parts)
    for child in node.children:
        name = _extract_name_from_declarator(child)
        if name:
            return name
    return ""


def _get_function_name(node: Node) -> str:
    """从 function_definition 节点提取函数名"""
    for child in node.children:
        if child.type in ("function_declarator", "pointer_declarator", "reference_declarator"):
            return _extract_name_from_declarator(child)
    return ""


def _get_preceding_comment(node: Node, source_bytes: bytes) -> str:
    """获取 function_definition 之前紧邻的注释块"""
    parent = node.parent
    if parent is None:
        return ""

    children = list(parent.children)
    try:
        idx = children.index(node)
    except ValueError:
        return ""

    if idx == 0:
        return ""

    prev = children[idx - 1]
    if prev.type == "comment":
        return prev.text.decode("utf-8")

    # 检查 declaration 节点中的注释（某些 grammar 会把注释包装在 declaration 中）
    if prev.type == "declaration":
        for c in prev.children:
            if c.type == "comment":
                return c.text.decode("utf-8")

    return ""


def _get_return_type(node: Node) -> str:
    """从 function_definition 提取返回类型文本"""
    parts = []
    for child in node.children:
        if child.type in ("type_identifier", "primitive_type", "sized_type_specifier"):
            parts.append(child.text.decode("utf-8"))
        elif child.type in ("pointer_declarator", "reference_declarator"):
            # 返回类型包含 * 或 &
            ptr_text = child.text.decode("utf-8")
            # 只取 * 或 & 部分，不要函数名
            if "*" in ptr_text:
                parts.append("*")
            if "&" in ptr_text:
                parts.append("&")
        elif child.type in ("enum_specifier", "class_specifier", "struct_specifier"):
            parts.append(child.text.decode("utf-8"))
        elif child.type == "function_declarator":
            break  # 遇到函数声明符就停止
    return " ".join(parts) if parts else "void"


def _get_parameters(node: Node) -> list[dict]:
    """从 function_definition 提取参数列表"""
    params = []
    # 找到 parameter_list
    param_list = None
    for child in node.children:
        if child.type == "function_declarator":
            for gc in child.children:
                if gc.type == "parameter_list":
                    param_list = gc
                    break
            break
        elif child.type in ("pointer_declarator", "reference_declarator"):
            # 返回类型是指针/引用，需要深入到 function_declarator
            for gc in child.children:
                if gc.type == "function_declarator":
                    for ggc in gc.children:
                        if ggc.type == "parameter_list":
                            param_list = ggc
                            break
                    break
            break

    if param_list is None:
        return params

    for param in param_list.children:
        if param.type == "parameter_declaration":
            ptype = ""
            pname = ""
            is_pointer = False
            for pc in param.children:
                if pc.type in ("type_identifier", "primitive_type", "sized_type_specifier"):
                    ptype += pc.text.decode("utf-8") + " "
                elif pc.type in ("enum_specifier", "class_specifier", "struct_specifier"):
                    ptype += pc.text.decode("utf-8") + " "
                elif pc.type in ("pointer_declarator", "reference_declarator"):
                    is_pointer = True
                    for pcc in pc.children:
                        if pcc.type in ("identifier", "field_identifier"):
                            pname = pcc.text.decode("utf-8")
                elif pc.type in ("identifier", "field_identifier"):
                    pname = pc.text.decode("utf-8")
                elif pc.type == "array_declarator":
                    is_pointer = True
                    for pcc in pc.children:
                        if pcc.type in ("identifier", "field_identifier"):
                            pname = pcc.text.decode("utf-8")
            ptype = ptype.strip()
            if is_pointer:
                ptype += "*"
            if ptype or pname:
                params.append({"type": ptype or "unknown", "name": pname or "_"})

    return params


def _get_modifiers(node: Node) -> list[str]:
    """提取函数修饰符（static, inline, virtual, const, constexpr）"""
    mods = []
    for child in node.children:
        if child.type in ("storage_class_specifier", "function_specifier",
                          "type_qualifier", "virtual"):
            mods.append(child.text.decode("utf-8"))
    return mods


_MEMORY_FUNCS = frozenset({
    "malloc", "calloc", "realloc", "free",
    "memcpy", "memmove", "memset", "strcpy",
    "strncpy", "strcat", "sprintf", "snprintf",
    "strdup", "strndup",
})


def _has_memory_ops(node: Node) -> list[str]:
    """扫描函数体中是否包含内存操作函数调用"""
    found = set()

    def _scan(n: Node):
        if n.type == "call_expression":
            func_name = ""
            for c in n.children:
                if c.type in ("identifier", "field_identifier"):
                    func_name = c.text.decode("utf-8")
                    break
                elif c.type == "field_expression":
                    for cc in c.children:
                        if cc.type == "field_identifier":
                            func_name = cc.text.decode("utf-8")
                            break
            if func_name in _MEMORY_FUNCS:
                found.add(func_name)
        elif n.type == "new_expression":
            found.add("new")
        elif n.type == "delete_expression":
            found.add("delete")
        for c in n.children:
            _scan(c)

    _scan(node)
    return sorted(found)


def _count_branches(node: Node) -> dict[str, int]:
    """统计函数体中的控制流节点数量"""
    counts = {"if": 0, "switch": 0, "while": 0, "for": 0, "do": 0, "try": 0}

    def _scan(n: Node):
        t = n.type
        if t in counts:
            counts[t] += 1
        for c in n.children:
            _scan(c)

    _scan(node)
    return counts


def _extract_metadata(node: Node) -> dict:
    """从 function_definition 节点提取 AST 元数据"""
    return_type = _get_return_type(node)
    parameters = _get_parameters(node)
    modifiers = _get_modifiers(node)
    memory_ops = _has_memory_ops(node)
    branch_counts = _count_branches(node)
    total_branches = sum(branch_counts.values())

    return {
        "return_type": return_type,
        "parameters": parameters,
        "modifiers": modifiers,
        "has_memory_ops": bool(memory_ops),
        "memory_ops": memory_ops,
        "branch_count": total_branches,
        "branch_breakdown": branch_counts,
    }


def _walk_functions(node: Node, functions: list[FunctionInfo],
                    source_bytes: bytes, source_text: str) -> None:
    """递归遍历 AST，收集函数定义节点"""
    if node.type == "function_definition":
        name = _get_function_name(node)
        if not name:
            name = f"anonymous_{node.start_point[0] + 1}"

        start_line = node.start_point[0] + 1  # 0-based → 1-based
        end_line = node.end_point[0] + 1

        # 提取 AST 元数据
        metadata = _extract_metadata(node)

        # 提取代码文本（含前置注释）
        comment = _get_preceding_comment(node, source_bytes)
        code_text = source_text[node.start_byte:node.end_byte]
        if comment:
            code_text = comment + "\n" + code_text

        functions.append(FunctionInfo(
            name=name,
            start_line=start_line,
            end_line=end_line,
            code_text=code_text,
            metadata=metadata,
        ))

    for child in node.children:
        _walk_functions(child, functions, source_bytes, source_text)


def extract_functions(file_path: str) -> list[FunctionInfo]:
    """从 C/C++ 源文件中提取所有函数定义

    Args:
        file_path: 源文件路径

    Returns:
        FunctionInfo 列表，按文件中出现的顺序排列

    Raises:
        FileNotFoundError: 文件不存在
        UnicodeDecodeError: 文件编码错误
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    source_text = path.read_text(encoding="utf-8")
    source_bytes = source_text.encode("utf-8")

    language = _get_language(file_path)
    parser = Parser(language)
    tree = parser.parse(source_bytes)

    functions: list[FunctionInfo] = []
    _walk_functions(tree.root_node, functions, source_bytes, source_text)

    return functions
