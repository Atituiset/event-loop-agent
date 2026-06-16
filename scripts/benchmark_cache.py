"""验证 tree-sitter 解析结果缓存的收益

对比两种模式：
- 无缓存：每个函数 task 都重新调用 extract_functions（模拟优化前）
- 有缓存：第一次解析后存入 dict，后续复用（优化后）
"""

import time
from pathlib import Path
from opencode_agent.scanner.splitter import extract_functions

# 选一个函数比较多的测试文件
TEST_FILE = "/home/atituiset/Projects/opencode-c-cpp-test/mac/scheduler.c"


def benchmark_no_cache(file_path: str, iterations: int = 5):
    """模拟优化前：每个 task 都重新解析"""
    functions = extract_functions(file_path)
    n_funcs = len(functions)

    start = time.perf_counter()
    for _ in range(iterations):
        for _ in range(n_funcs):
            # 模拟 _build_full_scan_cmd 里的行为：每次重新解析
            _ = extract_functions(file_path)
    elapsed = time.perf_counter() - start

    return elapsed, n_funcs, iterations


def benchmark_with_cache(file_path: str, iterations: int = 5):
    """模拟优化后：只解析一次，后续从缓存读取"""
    functions = extract_functions(file_path)
    n_funcs = len(functions)
    cache = {file_path: functions}

    start = time.perf_counter()
    for _ in range(iterations):
        for _ in range(n_funcs):
            # 从缓存读取
            _ = cache[file_path]
    elapsed = time.perf_counter() - start

    return elapsed, n_funcs, iterations


def main():
    path = Path(TEST_FILE)
    if not path.exists():
        print(f"测试文件不存在: {TEST_FILE}")
        return

    print(f"测试文件: {TEST_FILE}")
    print(f"文件大小: {path.stat().st_size / 1024:.1f} KB")

    # 先预热，把 tree-sitter grammar 加载进内存
    print("\n[预热] 首次解析...")
    funcs = extract_functions(TEST_FILE)
    print(f"  共提取 {len(funcs)} 个函数:")
    for f in funcs:
        print(f"    - {f.name} ({f.start_line}-{f.end_line}, {len(f.code_text.splitlines())} lines)")

    print(f"\n{'='*60}")
    print("开始 benchmark（每个模式跑 5 轮 × 函数数量次访问）")
    print(f"{'='*60}")

    no_cache_t, n_funcs, iters = benchmark_no_cache(TEST_FILE)
    with_cache_t, _, _ = benchmark_with_cache(TEST_FILE)

    total_calls = n_funcs * iters

    print(f"\n无缓存模式:")
    print(f"  总耗时: {no_cache_t*1000:.2f} ms")
    print(f"  调用次数: {total_calls} 次 extract_functions()")
    print(f"  单次平均: {no_cache_t/total_calls*1000:.3f} ms")

    print(f"\n有缓存模式:")
    print(f"  总耗时: {with_cache_t*1000:.2f} ms")
    print(f"  调用次数: {total_calls} 次 dict 读取")
    print(f"  单次平均: {with_cache_t/total_calls*1000:.3f} ms")

    speedup = no_cache_t / with_cache_t if with_cache_t > 0 else float('inf')
    saved = (no_cache_t - with_cache_t) / no_cache_t * 100 if no_cache_t > 0 else 0

    print(f"\n{'='*60}")
    print("结论")
    print(f"{'='*60}")
    print(f"  加速比: {speedup:.1f}x")
    print(f"  节省时间: {saved:.1f}%")
    print(f"  每文件节省解析: {total_calls - iters} 次 (从 {total_calls} 降到 {iters})")


if __name__ == "__main__":
    main()
