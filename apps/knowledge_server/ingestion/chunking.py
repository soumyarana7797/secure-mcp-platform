"""Split text into overlapping chunks, preferring paragraph and sentence boundaries."""

import re

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def chunk_text(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    if size <= overlap:
        raise ValueError("size must be larger than overlap")
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = " ".join(paragraph.split())
        if not paragraph:
            continue
        if len(paragraph) <= size:
            pieces.append(paragraph)
            continue
        # A long paragraph: pack whole sentences, hard-splitting any single oversized sentence.
        current = ""
        for sentence in _SENTENCE_END.split(paragraph):
            while len(sentence) > size:
                head, sentence = sentence[:size], sentence[size - overlap :]
                if current:
                    pieces.append(current)
                    current = ""
                pieces.append(head)
            if current and len(current) + 1 + len(sentence) > size:
                pieces.append(current)
                current = current[-overlap:].split(" ", 1)[-1] + " " + sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            pieces.append(current)
    return pieces
