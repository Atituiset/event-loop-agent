from opencode_agent.utils.ansi import strip_ansi


def test_strip_ansi_removes_color_codes():
    text = "\x1b[31mhello\x1b[0m"
    assert strip_ansi(text) == "hello"
