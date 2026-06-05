"""Tests for function_splitter module using real C/C++ code from opencode-c-cpp-test."""

import pytest
from pathlib import Path
from function_splitter import extract_functions

TEST_DIR = Path("/home/atituiset/Projects/opencode-c-cpp-test")


def test_extract_c_functions():
    funcs = extract_functions(str(TEST_DIR / "utils" / "logger.c"))
    names = [f.name for f in funcs]
    assert "logger_init" in names
    assert "log_write" in names
    assert "logger_close" in names


def test_extract_cpp_functions():
    funcs = extract_functions(str(TEST_DIR / "memory_pool.cpp"))
    names = [f.name for f in funcs]
    assert "allocate" in names
    assert "deallocate" in names
    assert "buggy_function" in names
    assert "main" in names


def test_metadata_return_type():
    funcs = extract_functions(str(TEST_DIR / "utils" / "logger.c"))
    init = next(f for f in funcs if f.name == "logger_init")
    assert init.metadata["return_type"] == "int"

    close = next(f for f in funcs if f.name == "logger_close")
    assert close.metadata["return_type"] == "void"


def test_metadata_parameters():
    funcs = extract_functions(str(TEST_DIR / "utils" / "logger.c"))
    init = next(f for f in funcs if f.name == "logger_init")
    params = init.metadata["parameters"]
    assert len(params) == 2
    assert params[0]["name"] == "filename"
    assert "char" in params[0]["type"] or "const" in params[0]["type"]


def test_metadata_modifiers():
    funcs = extract_functions(str(TEST_DIR / "utils" / "logger.c"))
    # log_write should not have static modifier (it's not static)
    write = next(f for f in funcs if f.name == "log_write")
    assert "static" not in write.metadata["modifiers"]


def test_metadata_memory_ops():
    funcs = extract_functions(str(TEST_DIR / "utils" / "logger.c"))
    init = next(f for f in funcs if f.name == "logger_init")
    assert init.metadata["has_memory_ops"] is True
    assert "malloc" in init.metadata["memory_ops"]

    close = next(f for f in funcs if f.name == "logger_close")
    assert close.metadata["has_memory_ops"] is True
    assert "free" in close.metadata["memory_ops"]


def test_metadata_branch_count():
    funcs = extract_functions(str(TEST_DIR / "utils" / "logger.c"))
    write = next(f for f in funcs if f.name == "log_write")
    assert write.metadata["branch_count"] >= 1
    assert write.metadata["branch_breakdown"]["if"] >= 1


def test_line_numbers():
    funcs = extract_functions(str(TEST_DIR / "utils" / "logger.c"))
    for f in funcs:
        assert f.start_line > 0
        assert f.end_line > f.start_line


def test_code_text():
    funcs = extract_functions(str(TEST_DIR / "utils" / "logger.c"))
    init = next(f for f in funcs if f.name == "logger_init")
    assert "int logger_init" in init.code_text
    assert "fopen" in init.code_text


def test_tcp_handler_complex():
    """Test a more complex file with multiple functions."""
    funcs = extract_functions(str(TEST_DIR / "network" / "tcp_handler.c"))
    assert len(funcs) >= 2
    names = [f.name for f in funcs]
    # Actual function names in tcp_handler.c
    assert "init_server" in names


def test_all_files():
    """Smoke test: ensure all C/C++ files in test dir can be parsed."""
    files = list(TEST_DIR.rglob("*.c")) + list(TEST_DIR.rglob("*.cpp"))
    for f in files:
        funcs = extract_functions(str(f))
        assert isinstance(funcs, list)
        for func in funcs:
            assert func.name
            assert func.start_line > 0
            assert func.end_line >= func.start_line
            assert func.code_text
            assert "return_type" in func.metadata
