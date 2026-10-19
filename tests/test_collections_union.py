"""Kuaishou collection membership union.

`collection/update` REPLACES photoIdList, so a wrong union silently drops or
duplicates members. This path can only be exercised against a live creator
backend, so the decision logic is extracted into a pure function and tested
here. The specific hazard: photoIdList comes from collection/info while
candidates come from unrecorded/list/v2, and a plain `in` check misses an
existing member when one endpoint returns a number and the other a string.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mx_collections import union_photo_ids  # noqa: E402


def test_appends_only_new_ids():
    merged, n = union_photo_ids([1, 2], [3, 4])
    assert merged == [1, 2, 3, 4]
    assert n == 2


def test_is_idempotent():
    merged, n = union_photo_ids([1, 2, 3], [1, 2, 3])
    assert merged == [1, 2, 3]
    assert n == 0


def test_existing_members_are_never_removed():
    merged, _ = union_photo_ids([10, 20, 30], [40])
    assert merged[:3] == [10, 20, 30]


def test_numeric_and_string_forms_of_the_same_id_are_the_same_member():
    """The cross-endpoint hazard: 208946344748 vs '208946344748'."""
    merged, n = union_photo_ids([208946344748], ["208946344748", "208946344749"])
    assert n == 1, "string form of an existing numeric id must not be re-added"
    assert merged == [208946344748, "208946344749"]


def test_preserves_original_payload_types():
    """Only comparison is normalised — the API payload keeps what it sent."""
    existing = [123]
    candidates = ["456"]
    merged, _ = union_photo_ids(existing, candidates)
    assert isinstance(merged[0], int)
    assert isinstance(merged[1], str)


def test_order_is_stable_and_existing_comes_first():
    merged, _ = union_photo_ids([3, 1], [1, 2, 3, 4])
    assert merged == [3, 1, 2, 4]


def test_empty_inputs():
    assert union_photo_ids([], []) == ([], 0)
    assert union_photo_ids([], [1]) == ([1], 1)
    assert union_photo_ids([1], []) == ([1], 0)


def test_duplicate_candidates_within_one_batch_are_collapsed():
    merged, n = union_photo_ids([], ["7", "7", 7])
    assert merged == ["7"]
    assert n == 1


@pytest.mark.parametrize("existing,candidates,expected", [
    ([], [], 0),
    ([1], [], 0),
    ([], [1], 1),
    ([1, 2], [2, 3], 1),
])
def test_added_count(existing, candidates, expected):
    assert union_photo_ids(existing, candidates)[1] == expected
