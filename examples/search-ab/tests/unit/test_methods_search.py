"""Protocol v1 examples, validation boundaries, and public dev-data smoke test."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from methods import a, b


@pytest.fixture(params=[a, b], ids=["a", "b"])
def method(request):
    return request.param


@pytest.fixture
def documents():
    return [
        {"doc_id": "d001", "text": "사과 바나나 사과"},
        {"doc_id": "d002", "text": "바나나 우유"},
        {"doc_id": "d003", "text": "포도"},
    ]


def assert_results(method, actual, ids, a_scores, b_scores):
    scores = a_scores if method is a else b_scores
    assert type(actual) is list
    assert actual == [
        {"doc_id": doc_id, "score": score}
        for doc_id, score in zip(ids, scores)
    ]
    for result in actual:
        assert type(result) is dict
        assert set(result) == {"doc_id", "score"}
        assert type(result["doc_id"]) is str
        assert type(result["score"]) is (int if method is a else float)


def test_example_1(method, documents):
    assert_results(method, method.search("사과 바나나 주스", documents, 2),
                   ["d001", "d002"], [2, 1], [0.6666666666666666, 0.25])


def test_example_2_ties_and_query_changes_order(method, documents):
    # Reverse input so an input-order tie breaker cannot pass.
    documents.reverse()
    first = method.search("사과 바나나 주스", documents, 3)
    second = method.search("우유", documents, 3)
    assert [row["doc_id"] for row in first] != [row["doc_id"] for row in second]
    assert_results(method, second, ["d002", "d001", "d003"],
                   [1, 0, 0], [0.5, 0.0, 0.0])


def test_example_3_k_exceeds_document_count(method, documents):
    assert_results(method, method.search("사과 바나나 주스", documents, 10),
                   ["d001", "d002", "d003"], [2, 1, 0],
                   [0.6666666666666666, 0.25, 0.0])


def test_example_4_whitespace_query(method, documents):
    assert_results(method, method.search("   ", documents, 2),
                   ["d001", "d002"], [0, 0], [0.0, 0.0])


def test_example_5_empty_union(method):
    assert_results(method, method.search("", [{"doc_id": "d001", "text": ""}], 1),
                   ["d001"], [0], [0.0])


def test_example_6_repeated_tokens(method, documents):
    assert_results(method, method.search("사과 사과 사과", documents, 3),
                   ["d001", "d002", "d003"], [1, 0, 0], [0.5, 0.0, 0.0])


def test_example_7_lowercase(method):
    documents = [{"doc_id": "d001", "text": "APPLE pie"}]
    assert_results(method, method.search("Apple apple", documents, 1),
                   ["d001"], [1], [0.5])


@pytest.mark.parametrize("k", [-1, True])
def test_example_8_invalid_k(method, documents, k):
    with pytest.raises(ValueError):
        method.search("사과 바나나 주스", documents, k)


def test_example_8_duplicate_ids(method):
    documents = [{"doc_id": "d001", "text": "x"}, {"doc_id": "d001", "text": "y"}]
    with pytest.raises(ValueError):
        method.search("x", documents, 1)


@pytest.mark.parametrize("k", [False, -10, 1.0, "1", None, [], {}])
def test_invalid_k_even_for_empty_documents(method, k):
    with pytest.raises(ValueError):
        method.search("", [], k)


@pytest.mark.parametrize("query", [None, 1, True, b"x", [], {}])
@pytest.mark.parametrize("k", [0, 1])
def test_invalid_query_even_for_empty_documents(method, query, k):
    with pytest.raises(ValueError):
        method.search(query, [], k)


@pytest.mark.parametrize("documents", [None, (), {}, "", 1])
@pytest.mark.parametrize("k", [0, 1])
def test_invalid_documents_container(method, documents, k):
    with pytest.raises(ValueError):
        method.search("", documents, k)


@pytest.mark.parametrize("bad_document", [
    None, [], "x", 1, {}, {"doc_id": "d002"}, {"text": "x"},
    {"doc_id": None, "text": "x"}, {"doc_id": 2, "text": "x"},
    {"doc_id": [], "text": "x"}, {"doc_id": "d002", "text": None},
    {"doc_id": "d002", "text": 2}, {"doc_id": "d002", "text": []},
    {"doc_id": "d001", "text": "duplicate"},
])
@pytest.mark.parametrize("k", [0, 1, 10])
def test_invalid_later_document_is_never_skipped(method, bad_document, k):
    documents = [{"doc_id": "d001", "text": "x"}, bad_document]
    before = deepcopy(documents)
    with pytest.raises(ValueError):
        method.search("x", documents, k)
    assert documents == before


def test_validation_finishes_before_tokenization(method):
    class TokenizationForbidden(str):
        def lower(self):
            pytest.fail("tokenization occurred before all documents were validated")

    documents = [{"doc_id": "d001", "text": TokenizationForbidden("x")}, {}]
    with pytest.raises(ValueError):
        method.search(TokenizationForbidden("x"), documents, 1)


@pytest.mark.parametrize("k", [0, 1, 10])
def test_empty_documents(method, k):
    assert method.search("x", [], k) == []


def test_zero_k(method, documents):
    assert method.search("사과", documents, 0) == []


@pytest.mark.parametrize("query", ["", "\t\n\u2003"])
def test_empty_query_sorts_all_zero_scores(method, documents, query):
    assert_results(method, method.search(query, documents[::-1], 10),
                   ["d001", "d002", "d003"], [0, 0, 0], [0.0, 0.0, 0.0])


def test_arbitrary_ids_use_code_point_order_and_extra_keys_are_ignored(method):
    ids = ["한글", "d2", "d10", "a", "A", "", " id "]
    documents = [{"doc_id": doc_id, "text": "x", "extra": {"ignored": [1]}}
                 for doc_id in ids]
    assert_results(method, method.search("x", documents, 20),
                   ["", " id ", "A", "a", "d10", "d2", "한글"],
                   [1] * 7, [1.0] * 7)


def test_input_is_unchanged_and_calls_are_independent(method, documents):
    documents.reverse()
    documents[0]["extra"] = {"nested": [1, 2]}
    before = deepcopy(documents)
    original_dicts = list(documents)
    first = method.search("사과 바나나 주스", documents, 3)
    expected = deepcopy(first)
    method.search("우유", documents, 1)
    assert first == expected
    first[0]["doc_id"] = "changed"
    assert method.search("사과 바나나 주스", documents, 3) == expected
    assert documents == before
    assert all(current is original for current, original in zip(documents, original_dicts))


@pytest.mark.parametrize("query,text,a_score,b_score", [
    ("방법 원두", "방법. 원두는", 0, 0.0),
    ("é", "e\u0301", 0, 0.0),
    ("STRASSE", "Straße", 0, 0.0),
    ("\tÄPFEL\u2003사과\n", "äpfel\n사과\t배", 2, 0.6666666666666666),
    ("x", "", 0, 0.0),
])
def test_literal_lower_split_tokenization(method, query, text, a_score, b_score):
    assert_results(method, method.search(query, [{"doc_id": "d001", "text": text}], 1),
                   ["d001"], [a_score], [b_score])


def test_jaccard_can_rank_differently_from_intersection():
    documents = [{"doc_id": "a", "text": "x y z"}, {"doc_id": "b", "text": "x"}]
    assert_results(a, a.search("x", documents, 2), ["a", "b"], [1, 1], [])
    assert_results(b, b.search("x", documents, 2), ["b", "a"], [],
                   [1.0, 0.3333333333333333])


def test_all_public_dev_queries_return_three_unique_documents():
    dev = Path(__file__).resolve().parents[2] / "data" / "dev"
    documents = [json.loads(line) for line in
                 (dev / "documents.jsonl").read_text(encoding="utf-8").splitlines()]
    queries = [json.loads(line) for line in
               (dev / "queries.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(documents) == 30
    assert len(queries) == 10
    for method in (a, b):
        for query in queries:
            results = method.search(query["text"], documents, 3)
            assert len(results) == 3
            assert len({result["doc_id"] for result in results}) == 3
