import math

import pytest

from apps.knowledge_server.ingestion.chunking import chunk_text
from shared.embeddings.hashing import HashingEmbedder

E = HashingEmbedder()


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_vectors_have_the_declared_size_and_unit_length() -> None:
    (v,) = E.embed(["How many vacation days do I get?"])
    assert len(v) == E.dim == 4096
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-9)


def test_embedding_is_deterministic_across_calls() -> None:
    assert E.embed(["expense claims"]) == E.embed(["expense claims"])
    assert E.embed(["a", "b"])[0] == E.embed(["a"])[0]  # batching does not matter


def test_related_text_scores_higher_than_unrelated_text() -> None:
    query, related, unrelated = E.embed(
        [
            "how many vacation days carry over",
            "Unused vacation days carry over into the next year up to ten days.",
            "Hotel stays are capped at 180 dollars per night in major cities.",
        ]
    )
    assert cosine(query, related) > cosine(query, unrelated) + 0.1


def test_text_without_meaningful_words_embeds_to_zeros_not_nan() -> None:
    assert E.embed(["the of and"]) == [[0.0] * E.dim]
    assert E.embed([""]) == [[0.0] * E.dim]


def test_embedder_id_changes_with_the_dimension() -> None:
    assert HashingEmbedder(128).id != HashingEmbedder(256).id


def test_short_text_is_one_chunk_per_paragraph() -> None:
    assert chunk_text("First paragraph.\n\nSecond paragraph.") == [
        "First paragraph.",
        "Second paragraph.",
    ]


def test_long_paragraphs_are_split_on_sentences_within_the_size_limit() -> None:
    text = " ".join(f"This is sentence number {i} of a long paragraph." for i in range(60))
    chunks = chunk_text(text, size=200, overlap=40)
    assert len(chunks) > 5 and all(len(c) <= 200 + 40 for c in chunks)
    assert "sentence number 0 " in chunks[0] and "sentence number 59 " in chunks[-1]


def test_an_oversized_single_sentence_is_hard_split() -> None:
    chunks = chunk_text("x" * 1000, size=300, overlap=50)
    assert len(chunks) >= 3 and all(len(c) <= 300 for c in chunks)


def test_empty_and_whitespace_input_yield_no_chunks() -> None:
    assert chunk_text("") == [] and chunk_text("  \n\n  \n") == []


def test_overlap_must_be_smaller_than_size() -> None:
    with pytest.raises(ValueError):
        chunk_text("abc", size=10, overlap=10)
