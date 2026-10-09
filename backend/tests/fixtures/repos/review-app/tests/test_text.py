from app.text import slugify


def test_slugify_joins_words_with_dashes() -> None:
    assert slugify("  Hello, World!  ") == "hello-world"
