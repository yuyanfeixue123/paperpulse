"""终端配置向导：绘制、输入、步骤编排。"""

from __future__ import annotations

import io

from app.setup import console


def test_color_helpers_do_not_leak_repr():
    """回归：颜色常量曾是 lambda，f-string 里 {GREEN} 会输出函数 repr。"""
    assert "function" not in console._w(console.GREEN, "ok")
    assert "function" not in console._w(console.BOLD, "ok")
    assert console.GREEN == "32"  # 是字符串常量，不是函数


def test_draw_helpers_run_without_tty(capsys):
    console.rule("标题")
    console.line("内容")
    console.ok("a")
    console.warn("b")
    console.fail("c")
    console.info("d")
    out = capsys.readouterr().out
    assert "function" not in out  # 关键：不能出现函数 repr
    assert "标题" in out and "内容" in out
    for mark in ("✓", "!", "✗", "·"):
        assert mark in out


def test_step_bar_marks_progress(capsys):
    console.step_bar(3)
    out = capsys.readouterr().out
    assert "管理员" in out and "完成" in out
    assert "▶" in out


def test_ask_reads_default(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert console.ask("站点名称", default="PaperPulse") == "PaperPulse"


def test_ask_retries_when_required(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n\nx\n"))
    assert console.ask("邮箱", required=True) == "x"


def test_ask_int_rejects_garbage(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("abc\n-5\n42\n"))
    assert console.ask_int("日额度", 250, minimum=1) == 42
    err = capsys.readouterr().out
    assert "请输入整数" in err
    assert "不能小于 1" in err


def test_ask_choice_by_index_and_key(monkeypatch):
    options = [("a", "Alpha"), ("b", "Beta")]
    monkeypatch.setattr("sys.stdin", io.StringIO("2\n"))
    assert console.ask_choice("选一个", options) == "b"
    monkeypatch.setattr("sys.stdin", io.StringIO("a\n"))
    assert console.ask_choice("选一个", options) == "a"


def test_ask_choice_retries_on_invalid(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("99\n1\n"))
    assert console.ask_choice("选", [("only", "唯一")]) == "only"
    assert "无效选项" in capsys.readouterr().out


def test_ask_yes_no(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    assert console.ask_yes_no("继续") is True
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert console.ask_yes_no("继续", default=False) is False
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))
    assert console.ask_yes_no("继续", default=True) is False


def test_secret_falls_back_when_not_tty(monkeypatch):
    """非 TTY 下不能用 getpass，否则管道/自动化会阻塞。"""
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret\n"))
    monkeypatch.setattr(console.sys.stdin, "isatty", lambda: False, raising=False)
    assert console.ask("Key", secret=True) == "s3cret"


def test_all_six_steps_registered():
    assert [name for name, _ in console.STEPS] == [
        "管理员账号",
        "站点信息",
        "LLM 接入",
        "邮件通道",
        "数据源确认",
        "完成",
    ]


def test_presets_cover_major_vendors():
    keys = {k for k, _ in console.LLM_PRESETS}
    assert {"deepseek", "qwen", "openai", "anthropic", "gemini", "ollama", "custom"} <= keys
    assert all(k in console._PROVIDER_OF for k in keys)
    mail = {k for k, _ in console.MAIL_PRESETS}
    assert {"brevo", "resend", "ses", "smtp", "postfix"} == mail


def test_short_truncates_detail():
    assert len(console._short({"a": "x" * 200})) <= 60
    assert console._short("y" * 200).__len__() <= 60
