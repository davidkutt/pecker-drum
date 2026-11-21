from pecker_drum.report.excerpt import EXCERPT_LIMIT, code_span, escape_excerpt


def test_short_text_is_unchanged() -> None:
    assert escape_excerpt("Status: open.") == "Status: open."


def test_limit_is_120_after_escaping() -> None:
    long = "a" * 500
    assert len(escape_excerpt(long)) == EXCERPT_LIMIT
    assert escape_excerpt(long).endswith("…")
    zero_width = "​" * 100
    escaped = escape_excerpt(zero_width)
    assert len(escaped) <= EXCERPT_LIMIT
    assert escaped.startswith("\\u200b\\u200b")


def test_escapes_are_never_split() -> None:
    escaped = escape_excerpt("x" * 115 + "​" + "y")
    assert escaped == "x" * 115 + "…"


def test_invisible_control_and_markdown_breakers_are_escaped() -> None:
    raw = "a​b‮c\nd\te`f|g\\h\U000e0041"
    assert escape_excerpt(raw) == "a\\u200bb\\u202ec\\nd\\te\\x60f\\x7cg\\\\h\\U000e0041"


def test_markdown_image_is_inert_inside_code_span() -> None:
    span = code_span(escape_excerpt("![x](https://img.example/p?d=CANARY-0000000000aa)"))
    assert span == "`![x](https://img.example/p?d=CANARY-0000000000aa)`"
    assert code_span("") == ""
