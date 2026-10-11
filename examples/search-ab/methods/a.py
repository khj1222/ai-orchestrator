"""Rank documents by the number of shared unique tokens."""


def search(query, documents, k):
    """Return up to k results after validating every input document."""
    if not isinstance(k, int) or isinstance(k, bool) or k < 0:
        raise ValueError("k must be a nonnegative integer, excluding bool")
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    if not isinstance(documents, list):
        raise ValueError("documents must be a list")

    seen = set()
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("each document must be a dict")
        if not isinstance(document.get("doc_id"), str):
            raise ValueError("each document must have a string doc_id")
        if not isinstance(document.get("text"), str):
            raise ValueError("each document must have a string text")
        if document["doc_id"] in seen:
            raise ValueError("doc_id must be unique")
        seen.add(document["doc_id"])

    if k == 0 or not documents:
        return []

    query_tokens = set(query.lower().split())
    results = []
    for document in documents:
        document_tokens = set(document["text"].lower().split())
        results.append({
            "doc_id": document["doc_id"],
            "score": len(query_tokens & document_tokens),
        })
    results.sort(key=lambda result: (-result["score"], result["doc_id"]))
    return results[:k]
