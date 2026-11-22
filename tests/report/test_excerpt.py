from pecker_drum.report.excerpt import EXCERPT_LIMIT, code_span, escape_excerpt


def test_short_text_is_unchanged() -> None:
    assert escape_excerpt("Status: open.") == "Status: open."


def test_limit_is_120_after_escaping() -> None:
    long = "a" * 500
    assert len(escape_excerpt(long)) == EXCERPT_LIMIT
    assert escape_excerpt(long).endswith("…")
    zero_width = "\u200b" * 100
    escaped = escape_excerpt(zero_width)
    assert len(escaped) <= EXCERPT_LIMIT
    assert escaped.startswith("\\u200b\\u200b")


def test_escapes_are_never_split() -> None:
    escaped = escape_excerpt("x" * 115 + "\u200b" + "y")
    assert escaped == "x" * 115 + "…"


def test_invisible_control_and_markdown_breakers_are_escaped() -> None:
    raw = "a\u200bb\u202ec\nd\te`f|g\\h\U000e0041"
    assert escape_excerpt(raw) == "a\\u200bb\\u202ec\\nd\\te\\x60f\\x7cg\\\\h\\U000e0041"


def test_markdown_image_is_inert_inside_code_span() -> None:
    span = code_span(escape_excerpt("![x](https://img.example/p?d=CANARY-0000000000aa)"))
    assert span == "`![x](https://img.example/p?d=CANARY-0000000000aa)`"
    assert code_span("") == ""


def test_full_answer_keeps_line_breaks_and_escapes_the_rest() -> None:
    from pecker_drum.report.excerpt import escape_answer

    zwsp = b"\\u200b".decode("unicode_escape")
    text = f"line 1\r\nline 2\twith {zwsp} and \\u200b typed, ```code``` | pipe"
    escaped = escape_answer(text)
    assert escaped == "line 1\\r\nline 2\\twith \\u200b and \\\\u200b typed, ```code``` | pipe"
    assert zwsp not in escaped
    long = "x" * 5000
    assert escape_answer(long) == long  # uncut


def test_fence_is_longer_than_any_backtick_run() -> None:
    from pecker_drum.report.excerpt import fenced_block

    assert fenced_block("plain") == "```\nplain\n```"
    assert fenced_block("a ``` b").startswith("````\n")
    block = fenced_block("x\n``````````\ny")  # a run of ten
    fence = "`" * 11
    assert block.startswith(fence + "\n")
    assert block.endswith("\n" + fence)
    assert block.count(fence) == 2
