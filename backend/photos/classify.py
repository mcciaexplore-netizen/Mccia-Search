"""What a cropped region is: a photograph, an advertisement, an illustration or just text. CLIP zero-shot, free; the maths is pure so it is tested alone."""

import numpy as np

KINDS = ("photo", "advertisement", "illustration", "text")
# Several wordings per kind: the average of their similarities is steadier than any single sentence.
PROMPTS = {
    "photo": ["a photograph", "a black and white photograph of people", "a photograph of a building or a machine", "a portrait photograph"],
    "advertisement": ["an advertisement", "a print advertisement with a product and a slogan", "an advertisement with a logo and company address"],
    "illustration": ["an illustration", "a line drawing", "a cartoon or a sketch"],
    "text": ["a page of printed text", "columns of newspaper text", "a table of numbers", "a title in large letters"],
}
TEMPERATURE = 100.0  # CLIP's own logit scale


def classify(image_vectors: np.ndarray, text_vectors: dict[str, np.ndarray]) -> list[tuple[str, float, dict[str, float]]]:
    """For each unit-length image vector: (the likeliest kind, its probability, every kind's probability).

    `text_vectors[kind]` is the unit-length vectors of that kind's prompts. A kind's score is the mean cosine similarity to its prompts, and the scores
    go through a softmax at CLIP's temperature.
    """
    if image_vectors.size == 0:
        return []
    scores = np.stack([(image_vectors @ text_vectors[kind].T).mean(axis=1) for kind in KINDS], axis=1) * TEMPERATURE
    scores -= scores.max(axis=1, keepdims=True)
    probabilities = np.exp(scores)
    probabilities /= probabilities.sum(axis=1, keepdims=True)
    results = []
    for row in probabilities:
        best = int(row.argmax())
        results.append((KINDS[best], float(row[best]), {kind: float(row[i]) for i, kind in enumerate(KINDS)}))
    return results
