from pathlib import Path


PORTAL = Path(__file__).parents[1] / "static" / "portal.html"


def test_chat_message_whitespace_is_scoped_to_the_message_body():
    """Template indentation must not render as blank lines inside chat bubbles."""
    page = PORTAL.read_text(encoding="utf-8")

    assert ".m .body { white-space: pre-wrap;" in page
    assert ".m { max-width: 74%; padding: 11px 15px; border-radius: 18px; background: var(--soft);\n         white-space: pre-wrap;" not in page
    assert '<div class="n">${who}</div><div class="body">${esc(m.body)}</div><div class="tm">' in page
