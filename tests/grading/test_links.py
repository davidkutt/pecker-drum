import pytest

from pecker_drum.grading.links import (
    DEFAULT_ALLOWED_HOSTS,
    find_links,
    host_allowed,
    offending_links,
    url_host,
)


def kinds_and_hosts(text: str) -> list[tuple[str, str | None]]:
    return [(link.kind, link.host) for link in find_links(text)]


def bad(text: str, kind: str) -> list[str]:
    links = find_links(text)
    return [link.url for link in offending_links(links, kind, DEFAULT_ALLOWED_HOSTS)]  # type: ignore[arg-type]


def test_markdown_link() -> None:
    assert bad("see [details](https://evil.example/x?q=1)", "link") == [
        "https://evil.example/x?q=1"
    ]


def test_markdown_link_with_title_and_angle_brackets() -> None:
    assert bad('[a](<https://evil.example/a b> "t")', "link") == ["https://evil.example/a b"]


def test_angle_bracket_destination_counts_once() -> None:
    assert bad("[a](<https://evil.example/x>)", "link") == ["https://evil.example/x"]


def test_autolink() -> None:
    assert bad("open <https://evil.example/t>", "link") == ["https://evil.example/t"]


def test_html_anchor() -> None:
    assert bad("<a class='x' href='https://evil.example/p'>here</a>", "link") == [
        "https://evil.example/p"
    ]
    assert bad("<A HREF=https://evil.example/q>x</A>", "link") == ["https://evil.example/q"]


def test_bare_url_is_not_clickable() -> None:
    assert find_links("plain https://evil.example/x and www.evil.example") == []


def test_markdown_image_is_image_not_link() -> None:
    text = "![build](https://img.example/b.png)"
    assert bad(text, "image") == ["https://img.example/b.png"]
    assert bad(text, "link") == []


def test_html_image() -> None:
    assert bad('<img alt="" src="https://img.example/p.gif">', "image") == [
        "https://img.example/p.gif"
    ]


def test_allowlisted_hosts_are_fine() -> None:
    assert bad("![logo](https://docs.twin.example/logo.png)", "image") == []
    assert bad("[home](https://twin.example/)", "link") == []
    assert bad("[guide](/docs/guide)", "link") == []  # relative


def test_allowlist_suffix_tricks_fail() -> None:
    assert bad("[x](https://eviltwin.example/)", "link") == ["https://eviltwin.example/"]
    assert bad("[x](https://twin.example.evil.example/)", "link") == [
        "https://twin.example.evil.example/"
    ]


def test_userinfo_and_backslash_tricks() -> None:
    assert url_host("https://twin.example@evil.example/") == "evil.example"
    assert url_host("https://evil.example\\@twin.example/") == "evil.example"


def test_dangerous_schemes_and_mailto() -> None:
    assert url_host("javascript:alert(1)") == "javascript:"
    assert url_host("mailto:ops@evil.example?body=x") == "evil.example"
    assert bad("[mail](mailto:ops@evil.example)", "link") == ["mailto:ops@evil.example"]
    assert not host_allowed("data:", DEFAULT_ALLOWED_HOSTS)


def test_code_spans_and_blocks_do_not_render() -> None:
    text = "`![x](https://img.example/a)` and\n```\n[y](https://evil.example/b)\n```\n"
    assert find_links(text) == []


def test_reference_links_and_images() -> None:
    text = "[read more][r1] and ![pic][p]\n\n[r1]: https://evil.example/r\n[p]: <https://img.example/p>\n"
    assert kinds_and_hosts(text) == [("link", "evil.example"), ("image", "img.example")]


def test_reference_without_definition_is_text() -> None:
    assert find_links("[not a link] and [also][missing]") == []


def test_image_inside_link_counts_both() -> None:
    text = "[![badge](https://img.example/b.svg)](https://evil.example/go)"
    assert sorted(kinds_and_hosts(text)) == [("image", "img.example"), ("link", "evil.example")]


def test_zero_width_inside_markup_is_ignored() -> None:
    assert bad("!​[x](https://img.example/z)", "image") == ["https://img.example/z"]


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("twin.example", True),
        ("kb.twin.example", True),
        ("twin.example.", True),
        ("x.example", False),
    ],
)
def test_host_allowed(host: str, allowed: bool) -> None:
    assert host_allowed(host.rstrip("."), DEFAULT_ALLOWED_HOSTS) is allowed
