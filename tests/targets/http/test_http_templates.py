import pytest

from pecker_drum.targets.http.templates import (
    TemplateError,
    check,
    placeholders,
    render,
    render_json,
    render_path,
)


def test_placeholders_and_literal_braces() -> None:
    assert placeholders("/c/{conversation_id}/m?x={tag}") == {"conversation_id", "tag"}
    assert render("{{literal}} {tag}", {"tag": "t"}) == "{literal} t"


@pytest.mark.parametrize(
    "template",
    ["{x.attr}", "{x[0]}", "{x!r}", "{x:>10}", "{X}", "{ x }", "{", "}", "a{b"],
)
def test_anything_but_a_plain_name_is_refused(template: str) -> None:
    with pytest.raises(TemplateError):
        placeholders(template)


def test_unknown_variables_are_named() -> None:
    with pytest.raises(TemplateError, match=r"unknown variable \{password\}"):
        check("/x/{password}", {"tag"}, "chat.send.path")


def test_path_values_stay_inside_one_segment() -> None:
    hostile = "../../admin?x=1#frag //evil.example/@"
    path = render_path("/api/documents/{document_id}", {"document_id": hostile})
    assert path.startswith("/api/documents/")
    assert "/" not in path.removeprefix("/api/documents/")
    assert not {"?", "#", "@"} & set(path)


def test_json_bodies_get_values_as_whole_strings() -> None:
    message = 'quote " brace } {tag} \\ newline\n'
    body = render_json(
        {"model": "m-1", "content": "{message}", "meta": ["{tag}", 3, None]},
        {"message": message, "tag": "drum-fixture-x"},
    )
    assert body == {"model": "m-1", "content": message, "meta": ["drum-fixture-x", 3, None]}
