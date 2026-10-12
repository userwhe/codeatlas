"""Search terms from questions that mix CJK text with identifiers, words, and paths.

`\\w` matches CJK characters, so without separation a question such as "可以设置pulse shape吗"
yields the terms "可以设置pulse" and "shape吗", which match no code and no documentation.
"""

import re

from codeatlas.retrieval.code import fts_query, identifier_tokens, separate_scripts
from codeatlas.retrieval.docs import docs_query

CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]")
LATIN = re.compile(r"[A-Za-z0-9]")


def _terms(query: str) -> list[str]:
    return [part.strip().split("'")[1] for part in query.split("|")] if query else []


def test_separate_scripts_puts_spaces_around_cjk_runs() -> None:
    assert separate_scripts("可以设置pulse shape吗") == " 可以设置 pulse shape 吗 "
    assert separate_scripts("가능한가요pulse") == " 가능한가요 pulse"
    assert separate_scripts("パルスshape") == " パルス shape"


def test_separate_scripts_leaves_other_text_alone() -> None:
    for text in ("How do I set the pulse shape?", "café naïve", "src/app.py:12", ""):
        assert separate_scripts(text) == text


def test_code_terms_keep_the_english_words_of_a_mixed_question() -> None:
    terms = _terms(fts_query("可以设置pulse shape吗，这是怎么实现的"))

    assert "pulse" in terms
    assert "shape" in terms
    assert not [term for term in terms if CJK.search(term) and LATIN.search(term)]


def test_identifier_tokens_find_a_path_written_between_cjk_words() -> None:
    tokens = identifier_tokens("解释RFSoC4x2_commonclk_dc/timing_4x2.xdc的功能")

    assert "RFSoC4x2_commonclk_dc/timing_4x2.xdc" in tokens


def test_docs_terms_keep_the_english_words_of_a_mixed_question() -> None:
    terms = _terms(docs_query("可以设置pulse shape吗"))

    assert "pulse" in terms
    assert "shape" in terms
    assert not [term for term in terms if CJK.search(term) and LATIN.search(term)]


def test_english_questions_are_unchanged() -> None:
    question = "How do I set the pulse shape in qick/asm_v2.py?"

    assert fts_query(question) == fts_query(separate_scripts(question))
    assert docs_query(question) == docs_query(separate_scripts(question))
    assert "qick/asm_v2.py" in identifier_tokens(question)
