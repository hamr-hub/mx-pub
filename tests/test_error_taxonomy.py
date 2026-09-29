"""The error taxonomy and the queue's retry filter must not disagree.

There used to be two independent classifiers: the legacy TRANSIENT_ERROR_PATTERNS
list in publisher.py and the rich table-driven classify() in core/errors.py.
They reached opposite conclusions on the same string — `fatal_connection_closed`
was transient to one and `unknown` to the other, `safety:` was permanent to one
and `unknown/retry` to the other. A dead CDP transport therefore got retried
instead of aborting the platform, and a content violation looked retryable.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from core.errors import (  # noqa: E402
    Action,
    ErrorClass,
    classify,
    is_fatal_connection_text,
)
from publisher import is_retryable_failure  # noqa: E402

# Everything the driver actually writes into PublishResult.error.
DRIVER_MARKERS = [
    "fatal_connection_closed",
    "Connection closed",
    "Target page, context or browser has been closed",
    "timeout 20000ms exceeded",
    "upload: Locator.set_input_files: Timeout 30000ms exceeded",
    "upload_not_ready_timeout",
    "duration_2.10s_below_min_5s",
    "safety:heuristic flags: gambling",
    "account_banned",
    "content violation: prohibited",
]

# (error, expected class, expected action)
EXPECTED = [
    ("fatal_connection_closed", ErrorClass.TRANSIENT, Action.ABORT_PLATFORM),
    ("Fatal_Connection_Closed", ErrorClass.TRANSIENT, Action.ABORT_PLATFORM),
    ("Connection closed", ErrorClass.TRANSIENT, Action.ABORT_PLATFORM),
    ("TARGET_PAGE_CONTEXT_OR_BROWSER_HAS_BEEN_CLOSED", ErrorClass.TRANSIENT,
     Action.ABORT_PLATFORM),
    ("timeout 20000ms exceeded", ErrorClass.TRANSIENT, Action.RETRY),
    ("upload: Locator.set_input_files: Timeout 30000ms exceeded",
     ErrorClass.TRANSIENT, Action.RETRY),
    ("safety:gambling", ErrorClass.CONTENT, Action.SKIP_VIDEO),
    ("safety: heuristic flags: gambling", ErrorClass.CONTENT, Action.SKIP_VIDEO),
    ("duration_2.10s_below_min_5s", ErrorClass.CONTENT, Action.SKIP_VIDEO),
    ("upload_rejected: 视频违规", ErrorClass.CONTENT, Action.SKIP_VIDEO),
    ("作品审核不通过", ErrorClass.CONTENT, Action.SKIP_VIDEO),
    ("account_banned", ErrorClass.ACCOUNT, Action.ABORT_PLATFORM),
    ("chrome not found", ErrorClass.FATAL, Action.ABORT_BATCH),
]


@pytest.mark.parametrize("error,cls,action", EXPECTED)
def test_classification_is_stable_across_separator_styles(error, cls, action):
    got = classify(error, status="fail")
    assert got.cls is cls, f"{error!r} -> {got.cls.value}/{got.action.value}"
    assert got.action is action


@pytest.mark.parametrize("error", DRIVER_MARKERS)
def test_no_driver_marker_falls_through_to_unknown(error):
    """Every string the driver emits must have a real diagnosis, not `unknown`."""
    got = classify(error, status="fail")
    assert got.label != "unknown", f"{error!r} is unclassified"


@pytest.mark.parametrize("error", DRIVER_MARKERS)
def test_retry_filter_agrees_with_the_classifier(error):
    """The queue filter and the in-run retry must never disagree."""
    got = classify(error, status="fail")
    expected_retryable = got.cls in (ErrorClass.TRANSIENT, ErrorClass.RECOVERABLE,
                                     ErrorClass.PLATFORM, ErrorClass.UNKNOWN)
    assert is_retryable_failure(error) is expected_retryable, (
        f"{error!r}: classify says {got.cls.value}, "
        f"is_retryable_failure says {is_retryable_failure(error)}")


def test_dead_transport_is_never_merely_retryable():
    """Retrying against a dead CDP burns the per-video timeout budget."""
    for marker in ("fatal_connection_closed", "Connection closed",
                   "Target page, context or browser has been closed"):
        got = classify(marker, status="fail")
        assert got.is_fatal_connection is True
        assert got.action is Action.ABORT_PLATFORM
        assert is_fatal_connection_text(marker) is True


def test_separator_normalisation_is_case_and_style_insensitive():
    variants = ["fatal_connection_closed", "FATAL-CONNECTION-CLOSED",
                "Fatal Connection Closed", "fatal/connection/closed"]
    for v in variants:
        assert classify(v, status="fail").label == "fatal_connection", v


def test_content_violations_are_never_retryable():
    for marker in ("safety:gambling", "内容违规", "存在争议", "音乐侵权",
                   "作品审核不通过", "upload_rejected: 视频违规"):
        assert is_retryable_failure(marker) is False, marker


def test_a_crashed_renderer_is_a_dead_target():
    """Regression: a crashed Chrome scored unknown/retry.

    Production lost a whole run to 15 consecutive
    `Page.bring_to_front: Target crashed` rows, because the authoritative
    classifier had no crash markers even though the legacy list in
    publisher.py knew "page crashed". Both detectors must agree that a
    crashed target is terminal for the page, so the loop takes the session
    recovery path instead of retrying against a dead tab.
    """
    from core.watch import is_fatal_connection_error

    for marker in ("Target crashed",
                   "exception: Page.bring_to_front: Target crashed",
                   "Page crashed",
                   "Target has crashed",
                   "Renderer crashed"):
        got = classify(marker, status="fail")
        assert got.cls is ErrorClass.TRANSIENT, marker
        assert got.is_fatal_connection is True, marker
        assert got.action is Action.ABORT_PLATFORM, marker
        assert is_fatal_connection_text(marker) is True, marker
        assert is_fatal_connection_error(Exception(marker)) is True, marker

    # The neighbouring diagnoses must not be swept up with it.
    for marker in ("upload: Locator.set_input_files: Timeout 30000ms exceeded",
                   "upload: [Errno 2] No such file or directory: '/x.mp4'",
                   "safety:gambling"):
        assert is_fatal_connection_error(Exception(marker)) is False, marker
