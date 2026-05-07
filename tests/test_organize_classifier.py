# tests/test_organize_classifier.py
"""Tests for fda.organize.classifier."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _entry(idx: int, ext=".txt", failed=False, size=10, summary="x", subdir=""):
    """Build a CatalogEntry under /tmp/target. `subdir` lets tests place
    entries under different first-level directories so stratified sampling
    by top-level dir is testable."""
    from fda.organize.models import CatalogEntry
    base = "/tmp/target"
    path = f"{base}/{subdir}/{idx:03d}{ext}".replace("//", "/") if subdir \
        else f"{base}/{idx:03d}{ext}"
    return CatalogEntry(
        path_id=f"f{idx:03d}",
        path=path,
        ext=ext,
        size_bytes=size,
        summary="" if failed else summary,
        type_label="" if failed else "doc",
        is_junk=False,
        summary_failed=failed,
        extract_status="ok",
    )


def _catalog(entries, target="/tmp/target"):
    from fda.organize.models import Catalog
    return Catalog(target=target, entries=tuple(entries), git_repos_skipped=())


def _taxonomy_payload(categories, fallback_name="Misc"):
    return json.dumps({
        "categories": [
            {"category_name": n, "subpath": f"S/{n}", "description": "d", "criteria": "c"}
            for n in categories
        ],
        "fallback_category": {
            "category_name": fallback_name, "subpath": f"S/{fallback_name}",
            "description": "fallback", "criteria": "could not categorize"
        },
    })


def _assignment_payload(assignments):
    return json.dumps({
        "assignments": [{"path_id": pid, "category_name": cat}
                        for pid, cat in assignments]
    })


@pytest.fixture
def logger(tmp_path):
    from fda.organize._logger import OrganizeLogger
    return OrganizeLogger(log_path=tmp_path / "c.log", target_basename="ws")


# ---- Stage A ---------------------------------------------------------------

class TestStageAHappyPath:
    def test_small_catalog_full_mode(self, logger):
        from fda.organize import classifier

        backend = MagicMock()
        backend.complete.side_effect = [
            _taxonomy_payload(["Texts"]),  # Stage A
            _assignment_payload([("f000", "Texts"), ("f001", "Texts")]),  # Stage B
        ]
        cat = _catalog([_entry(0), _entry(1)])
        result = classifier.classify(cat, "sort", backend=backend, logger=logger)
        names = sorted(g.category for g in result.items)
        assert names == ["Texts"]

    def test_full_catalog_mode_payload_contains_all_entries(self, logger):
        from fda.organize import classifier

        backend = MagicMock()
        backend.complete.side_effect = [
            _taxonomy_payload(["Texts"]),
            _assignment_payload([(f"f{i:03d}", "Texts") for i in range(50)]),
        ]
        cat = _catalog([_entry(i) for i in range(50)])
        classifier.classify(cat, "sort", backend=backend, logger=logger)
        stage_a_msg = backend.complete.call_args_list[0].kwargs["messages"][0]["content"]
        for i in range(50):
            assert f"f{i:03d}" in stage_a_msg


class TestSampling:
    def test_stratified_when_above_threshold(self, logger):
        from fda.organize import classifier

        # 1000 entries across 5 extensions and 4 top-level dirs
        exts = [".txt", ".md", ".pdf", ".csv", ".log"]
        subdirs = ["Finance", "HR", "Legal", "R&D"]
        entries = []
        for i in range(1000):
            entries.append(_entry(
                i, ext=exts[i % 5], subdir=subdirs[i % 4],
            ))
        # mark some as failed so they can be sampled
        for i in range(990, 995):
            entries[i] = _entry(i, ext=".txt", failed=True, subdir=subdirs[i % 4])

        cat = _catalog(entries)
        backend = MagicMock()
        backend.complete.side_effect = [
            _taxonomy_payload(["Bucket"]),
            *[_assignment_payload([("f000", "Bucket")]) for _ in range(20)],
        ]
        # We just want to assert sampling — patch Stage B to return everything:
        with patch.object(classifier, "_run_stage_b") as run_b:
            run_b.return_value = {}
            classifier.classify(cat, "", backend=backend, logger=logger)
        sample_msg = backend.complete.call_args_list[0].kwargs["messages"][0]["content"]
        # Each ext should appear at least twice
        for ext in exts:
            assert sample_msg.count(ext) >= 2
        # Each top-level dir under /tmp/target should also appear in the sample
        for sub in subdirs:
            assert sub in sample_msg

    def test_sampling_deterministic(self, logger):
        from fda.organize import classifier

        entries = [_entry(i) for i in range(500)]
        cat = _catalog(entries)
        backend = MagicMock()
        backend.complete.side_effect = [
            _taxonomy_payload(["X"]), _taxonomy_payload(["X"]),
        ]
        with patch.object(classifier, "_run_stage_b") as run_b:
            run_b.return_value = {}
            classifier.classify(cat, "", backend=backend, logger=logger)
            classifier.classify(cat, "", backend=backend, logger=logger)
        a = backend.complete.call_args_list[0].kwargs["messages"][0]["content"]
        b = backend.complete.call_args_list[1].kwargs["messages"][0]["content"]
        assert a == b


class TestStageASchemaValidation:
    @pytest.mark.parametrize("payload", [
        json.dumps({"categories": [], "fallback_category": {
            "category_name": "M", "subpath": "M", "description": "", "criteria": ""}}),
        json.dumps({"categories": [{"category_name": "A", "subpath": "../bad",
                                    "description": "", "criteria": ""}],
                    "fallback_category": {
                        "category_name": "M", "subpath": "M", "description": "", "criteria": ""}}),
        json.dumps({"categories": [{"category_name": "A", "subpath": "A",
                                    "description": "", "criteria": ""},
                                   {"category_name": "A", "subpath": "B",
                                    "description": "", "criteria": ""}],
                    "fallback_category": {
                        "category_name": "M", "subpath": "M", "description": "", "criteria": ""}}),
    ])
    def test_invalid_payloads_raise(self, payload, logger):
        from fda.organize import classifier

        backend = MagicMock()
        # The classifier retries once on parse failure but treats validation
        # failures as terminal. We give the same bad payload twice for safety.
        backend.complete.side_effect = [payload, payload]
        cat = _catalog([_entry(0)])
        with pytest.raises(classifier.ClassifierError):
            classifier.classify(cat, "", backend=backend, logger=logger)

    def test_missing_fallback_raises(self, logger):
        from fda.organize import classifier

        backend = MagicMock()
        bad = json.dumps({
            "categories": [{"category_name": "A", "subpath": "A",
                            "description": "", "criteria": ""}],
        })
        backend.complete.side_effect = [bad, bad]
        cat = _catalog([_entry(0)])
        with pytest.raises(classifier.ClassifierError):
            classifier.classify(cat, "", backend=backend, logger=logger)


class TestStageAJsonRetry:
    def test_one_retry_then_succeeds(self, logger):
        from fda.organize import classifier

        backend = MagicMock()
        backend.complete.side_effect = [
            "not-json",
            _taxonomy_payload(["Texts"]),
            _assignment_payload([("f000", "Texts")]),
        ]
        cat = _catalog([_entry(0)])
        result = classifier.classify(cat, "", backend=backend, logger=logger)
        assert result.items[0].category == "Texts"


class TestStageAOverflow:
    def test_oversized_sample_raises(self, logger, monkeypatch):
        from fda.organize import classifier

        monkeypatch.setattr(classifier, "CLASSIFIER_INPUT_TOKEN_BUDGET", 50)
        cat = _catalog([_entry(i, summary="x" * 5_000) for i in range(5)])
        backend = MagicMock()
        with pytest.raises(classifier.ClassifierOverflowError):
            classifier.classify(cat, "", backend=backend, logger=logger)
        backend.complete.assert_not_called()


# ---- Stage B ---------------------------------------------------------------

class TestStageBHappyPath:
    def test_assignments_round_trip(self, logger):
        from fda.organize import classifier

        backend = MagicMock()
        backend.complete.side_effect = [
            _taxonomy_payload(["A", "B"]),
            _assignment_payload([("f000", "A"), ("f001", "B")]),
        ]
        cat = _catalog([_entry(0), _entry(1)])
        result = classifier.classify(cat, "", backend=backend, logger=logger)
        names = sorted(g.category for g in result.items)
        assert names == ["A", "B"]


class TestTokenBatching:
    def test_no_batch_exceeds_input_budget(self, logger, monkeypatch):
        from fda.organize import classifier

        monkeypatch.setattr(classifier, "ASSIGNER_BATCH_TARGET_TOKENS", 200)
        monkeypatch.setattr(classifier, "MAX_ASSIGNER_INPUT_TOKENS", 250)

        # 100 entries, each summary ~50 chars -> several batches
        entries = [_entry(i, summary="word " * 10) for i in range(100)]
        cat = _catalog(entries)

        captured: list[int] = []

        def fake_complete(*, messages, **_):
            payload = messages[0]["content"]
            est = len(payload) // 4
            captured.append(est)
            parsed = json.loads(payload)
            if "CATALOG" in parsed:
                return _taxonomy_payload(["A"])
            ids = [e["path_id"] for e in parsed["BATCH"]]
            return _assignment_payload([(pid, "A") for pid in ids])

        backend = MagicMock()
        backend.complete.side_effect = fake_complete
        classifier.classify(cat, "", backend=backend, logger=logger)
        # Ignore the first call (Stage A) — assert each Stage B batch fits.
        for est in captured[1:]:
            assert est <= classifier.MAX_ASSIGNER_INPUT_TOKENS


class TestConcurrencyCap:
    def test_at_most_n_in_flight(self, logger, monkeypatch):
        from fda.organize import classifier

        monkeypatch.setattr(classifier, "MAX_CLASSIFIER_CONCURRENCY", 2)
        # Force many batches with a tiny budget.
        monkeypatch.setattr(classifier, "ASSIGNER_BATCH_TARGET_TOKENS", 1)

        entries = [_entry(i) for i in range(20)]
        cat = _catalog(entries)

        in_flight = 0
        peak = 0
        lock = threading.Lock()

        def fake_complete(*, messages, **_):
            nonlocal in_flight, peak
            payload = messages[0]["content"]
            parsed = json.loads(payload)
            if "CATALOG" in parsed:
                # Stage A
                return _taxonomy_payload(["A"])
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.05)
            with lock:
                in_flight -= 1
            ids = [e["path_id"] for e in parsed["BATCH"]]
            return _assignment_payload([(pid, "A") for pid in ids])

        backend = MagicMock()
        backend.complete.side_effect = fake_complete
        classifier.classify(cat, "", backend=backend, logger=logger)
        assert peak <= 2


class TestPerBatchValidation:
    @pytest.mark.parametrize("bad_payload_factory", [
        lambda: _assignment_payload([("f999", "A")]),  # missing real id
        lambda: _assignment_payload([("f000", "Bogus")]),  # unknown category
    ])
    def test_one_retry_then_eventually_succeeds(self, logger, bad_payload_factory):
        from fda.organize import classifier

        backend = MagicMock()
        backend.complete.side_effect = [
            _taxonomy_payload(["A"]),
            bad_payload_factory(),
            _assignment_payload([("f000", "A")]),
        ]
        cat = _catalog([_entry(0)])
        result = classifier.classify(cat, "", backend=backend, logger=logger)
        assert result.items[0].category == "A"


class TestBoundedFallbackCoercion:
    def test_small_residual_coerced(self, logger, monkeypatch):
        from fda.organize import classifier

        # Catalog of 100 files; 1 missing in the response -> within bound.
        entries = [_entry(i) for i in range(100)]
        cat = _catalog(entries)
        bad = _assignment_payload(
            [(f"f{i:03d}", "A") for i in range(99)]  # f099 missing
        )
        backend = MagicMock()
        backend.complete.side_effect = [
            _taxonomy_payload(["A"]),
            bad,
            bad,  # retry returns the same shape
        ]
        result = classifier.classify(cat, "", backend=backend, logger=logger)
        # All 100 files end up in groupings; the residual goes to Misc.
        ids = sorted(pid for g in result.items for pid in g.file_ids)
        assert len(ids) == 100


class TestHardAbort:
    def test_too_many_bad_assignments_raises(self, logger):
        from fda.organize import classifier

        # 50 files, only 10 returned -> 40 missing > 10 threshold AND > 2%.
        entries = [_entry(i) for i in range(50)]
        cat = _catalog(entries)
        bad = _assignment_payload([(f"f{i:03d}", "A") for i in range(10)])
        backend = MagicMock()
        backend.complete.side_effect = [_taxonomy_payload(["A"]), bad, bad]
        with pytest.raises(classifier.ClassifierBatchError):
            classifier.classify(cat, "", backend=backend, logger=logger)


class TestRateLimitBackoff:
    def test_429_then_success(self, logger):
        from fda.organize import classifier

        class FakeRateLimit(Exception):
            status_code = 429

        backend = MagicMock()
        calls = {"n": 0}

        def fake_complete(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                err = FakeRateLimit("rate limited")
                raise err
            payload = kwargs["messages"][0]["content"]
            parsed = json.loads(payload)
            if "CATALOG" in parsed:
                return _taxonomy_payload(["A"])
            return _assignment_payload([("f000", "A")])

        backend.complete.side_effect = fake_complete
        # Patch the classifier's "is rate limit?" check to recognize FakeRateLimit
        with patch.object(classifier, "_is_rate_limit", lambda e: isinstance(e, FakeRateLimit)):
            cat = _catalog([_entry(0)])
            result = classifier.classify(cat, "", backend=backend, logger=logger)
        assert result.items[0].category == "A"


# ---- Refinement -----------------------------------------------------------

class TestRefinement:
    def test_high_fallback_triggers_one_refinement(self, logger):
        from fda.organize import classifier

        entries = [_entry(i) for i in range(100)]
        cat = _catalog(entries)

        # Stage A v1 returns ["A"] + Misc fallback. Stage B sends 30 to Misc.
        a_v1 = _taxonomy_payload(["A"])
        b_v1 = _assignment_payload(
            [(f"f{i:03d}", "Misc" if i < 30 else "A") for i in range(100)]
        )
        # Refinement: A gets a v2 taxonomy with two categories.
        a_v2 = _taxonomy_payload(["A", "B"])
        b_v2 = _assignment_payload(
            [(f"f{i:03d}", "B" if i < 30 else "A") for i in range(100)]
        )
        backend = MagicMock()
        backend.complete.side_effect = [a_v1, b_v1, a_v2, b_v2]
        result = classifier.classify(cat, "", backend=backend, logger=logger)
        names = sorted(g.category for g in result.items)
        assert "B" in names

    def test_low_fallback_skips_refinement(self, logger):
        from fda.organize import classifier

        entries = [_entry(i) for i in range(100)]
        cat = _catalog(entries)
        a = _taxonomy_payload(["A"])
        # Only 5 Misc — well below 20% / 25 threshold.
        b = _assignment_payload(
            [(f"f{i:03d}", "Misc" if i < 5 else "A") for i in range(100)]
        )
        backend = MagicMock()
        backend.complete.side_effect = [a, b]
        classifier.classify(cat, "", backend=backend, logger=logger)
        # No refinement: only 2 calls were consumed.
        assert backend.complete.call_count == 2

    def test_at_most_one_refinement(self, logger):
        from fda.organize import classifier

        entries = [_entry(i) for i in range(100)]
        cat = _catalog(entries)
        a_v1 = _taxonomy_payload(["A"])
        b_high = _assignment_payload(
            [(f"f{i:03d}", "Misc" if i < 50 else "A") for i in range(100)]
        )
        a_v2 = _taxonomy_payload(["A"])
        b_high2 = _assignment_payload(
            [(f"f{i:03d}", "Misc" if i < 50 else "A") for i in range(100)]
        )
        backend = MagicMock()
        backend.complete.side_effect = [a_v1, b_high, a_v2, b_high2]
        result = classifier.classify(cat, "", backend=backend, logger=logger)
        # No third refinement attempt — exactly 4 calls.
        assert backend.complete.call_count == 4


# ---- Cross-cutting --------------------------------------------------------

class TestEmptyCatalog:
    def test_empty_returns_empty_groupings(self, logger):
        from fda.organize import classifier

        backend = MagicMock()
        cat = _catalog([])
        result = classifier.classify(cat, "", backend=backend, logger=logger)
        assert result.items == ()
        backend.complete.assert_not_called()


class TestJunkOnly:
    def test_junk_only_returns_empty(self, logger):
        from fda.organize.models import CatalogEntry, Catalog
        from fda.organize import classifier

        cat = Catalog(target="/tmp", entries=(CatalogEntry(
            path_id="f000", path="/tmp/.DS_Store", ext="", size_bytes=0,
            summary="", type_label="junk", is_junk=True,
            summary_failed=False, extract_status="no_extractor",
        ),), git_repos_skipped=())
        backend = MagicMock()
        result = classifier.classify(cat, "", backend=backend, logger=logger)
        assert result.items == ()
        backend.complete.assert_not_called()


class TestUnreliableInput:
    def test_high_failed_summary_rate_raises(self, logger):
        from fda.organize import classifier

        # 10 entries, 4 failed -> 40% > 25% threshold.
        entries = [_entry(i, failed=(i < 4)) for i in range(10)]
        cat = _catalog(entries)
        backend = MagicMock()
        with pytest.raises(classifier.ClassifierUnreliableInputError):
            classifier.classify(cat, "", backend=backend, logger=logger)
        backend.complete.assert_not_called()

    def test_low_failed_summary_rate_proceeds(self, logger):
        from fda.organize import classifier

        # 10 entries, 1 failed.
        entries = [_entry(i, failed=(i == 0)) for i in range(10)]
        cat = _catalog(entries)
        backend = MagicMock()
        backend.complete.side_effect = [
            _taxonomy_payload(["A"]),
            _assignment_payload([(f"f{i:03d}", "A") for i in range(10)]),
        ]
        result = classifier.classify(cat, "", backend=backend, logger=logger)
        assert len(result.items) >= 1


class TestSummaryTruncation:
    def test_summaries_truncated_to_200_chars_in_payload(self, logger):
        from fda.organize import classifier

        long = "x" * 5000
        cat = _catalog([_entry(i, summary=long) for i in range(3)])
        backend = MagicMock()
        backend.complete.side_effect = [
            _taxonomy_payload(["A"]),
            _assignment_payload([(f"f{i:03d}", "A") for i in range(3)]),
        ]
        classifier.classify(cat, "", backend=backend, logger=logger)
        msg_a = backend.complete.call_args_list[0].kwargs["messages"][0]["content"]
        # Original 5000-char summary must not appear; truncated version should.
        assert long not in msg_a
        assert "x" * 200 in msg_a


class TestFinalGroupings:
    def test_grouping_carries_subpath_and_reason(self, logger):
        from fda.organize import classifier

        backend = MagicMock()
        backend.complete.side_effect = [
            _taxonomy_payload(["A"]),
            _assignment_payload([("f000", "A")]),
        ]
        cat = _catalog([_entry(0)])
        result = classifier.classify(cat, "", backend=backend, logger=logger)
        g = result.items[0]
        assert g.subpath.startswith("S/")
        assert g.reason == "c"


class TestStageBJsonRetryRecovers:
    def test_malformed_json_first_then_valid(self, logger):
        from fda.organize import classifier

        backend = MagicMock()
        backend.complete.side_effect = [
            _taxonomy_payload(["A"]),
            "```json\n" + _assignment_payload([("f000", "A")]) + "\n```",  # fenced/malformed-ish
            _assignment_payload([("f000", "A")]),  # clean retry
        ]
        cat = _catalog([_entry(0)])
        result = classifier.classify(cat, "", backend=backend, logger=logger)
        assert result.items[0].category == "A"

    def test_totally_invalid_json_falls_back(self, logger):
        from fda.organize import classifier

        backend = MagicMock()
        # Stage A ok; Stage B returns garbage twice. Single-entry batch
        # within fallback bounds gets coerced to Misc.
        backend.complete.side_effect = [
            _taxonomy_payload(["A"]),
            "{not json at all",
            "still {not} json",
        ]
        cat = _catalog([_entry(0)])
        result = classifier.classify(cat, "", backend=backend, logger=logger)
        # The lone file ends up coerced to the fallback category.
        names = sorted(g.category for g in result.items)
        assert names == ["Misc"]


class TestNonDictAssignmentItem:
    def test_null_item_treated_as_violation(self, logger):
        from fda.organize import classifier

        backend = MagicMock()
        bad = json.dumps({"assignments": [None, {"path_id": "f000", "category_name": "A"}]})
        good = _assignment_payload([("f000", "A")])
        backend.complete.side_effect = [
            _taxonomy_payload(["A"]),
            bad,
            good,
        ]
        cat = _catalog([_entry(0)])
        result = classifier.classify(cat, "", backend=backend, logger=logger)
        assert result.items[0].category == "A"

    def test_string_item_does_not_crash(self, logger):
        from fda.organize import classifier

        backend = MagicMock()
        bad = json.dumps({"assignments": ["f000"]})
        good = _assignment_payload([("f000", "A")])
        backend.complete.side_effect = [
            _taxonomy_payload(["A"]),
            bad,
            good,
        ]
        cat = _catalog([_entry(0)])
        result = classifier.classify(cat, "", backend=backend, logger=logger)
        assert result.items[0].category == "A"


class TestStageARateLimitBackoff:
    def test_429_in_stage_a_then_success(self, logger, monkeypatch):
        from fda.organize import classifier

        class FakeRateLimit(Exception):
            status_code = 429

        # Make sleep a no-op so the test runs fast.
        monkeypatch.setattr(classifier.time, "sleep", lambda *_: None)

        backend = MagicMock()
        calls = {"n": 0}

        def fake_complete(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FakeRateLimit("rate limited")
            payload = kwargs["messages"][0]["content"]
            parsed = json.loads(payload)
            if "CATALOG" in parsed:
                return _taxonomy_payload(["A"])
            return _assignment_payload([("f000", "A")])

        backend.complete.side_effect = fake_complete
        with patch.object(classifier, "_is_rate_limit", lambda e: isinstance(e, FakeRateLimit)):
            cat = _catalog([_entry(0)])
            result = classifier.classify(cat, "", backend=backend, logger=logger)
        assert result.items[0].category == "A"
        # Stage A retried once, so total backend calls = 1 (429) + 1 (taxonomy) + 1 (assigner) = 3
        assert calls["n"] == 3


class TestPendingBatchesCanceledOnFailure:
    def test_first_failure_cancels_pending(self, logger, monkeypatch):
        from fda.organize import classifier

        # Force many batches of ~5 entries each; one batch always returns
        # no assignments (exceeds bounded-coercion threshold) so it raises
        # ClassifierBatchError. Remaining pending batches should be canceled.
        monkeypatch.setattr(classifier, "ASSIGNER_BATCH_TARGET_TOKENS", 200)
        monkeypatch.setattr(classifier, "MAX_CLASSIFIER_CONCURRENCY", 1)  # serialize for determinism

        entries = [_entry(i) for i in range(30)]
        cat = _catalog(entries)

        call_log: list[list[str]] = []
        lock = threading.Lock()

        def fake_complete(*, messages, **_):
            payload = messages[0]["content"]
            parsed = json.loads(payload)
            if "CATALOG" in parsed:
                return _taxonomy_payload(["A"])
            ids = [e["path_id"] for e in parsed["BATCH"]]
            with lock:
                call_log.append(ids)
            # The second distinct batch (by its ids) always returns no assignments,
            # causing violations that exceed the bounded-coercion threshold.
            batch_ids = frozenset(ids)
            if len(call_log) >= 2 and batch_ids == frozenset(call_log[1]):
                return _assignment_payload([])
            return _assignment_payload([(pid, "A") for pid in ids])

        backend = MagicMock()
        backend.complete.side_effect = fake_complete
        with pytest.raises(classifier.ClassifierBatchError):
            classifier.classify(cat, "", backend=backend, logger=logger)
        # Must NOT have processed all batches — cancellation limited further work.
        assert len(call_log) < 30


# ---------------------------------------------------------------------------
# Wire format: verbatim_head present in classifier payloads
# ---------------------------------------------------------------------------


class TestVerbatimHeadInPayload:
    def test_assigner_batch_payload_carries_verbatim_head(self, logger):
        """The Stage B per-batch JSON payload must include each entry's
        verbatim_head alongside path, summary, etc."""
        from fda.organize import classifier
        from fda.organize.models import CatalogEntry

        captured: dict[str, str] = {}

        def capture(*, system, messages, **kwargs):
            payload = messages[0]["content"]
            captured["payload"] = payload
            # Distinguish Stage A (CATALOG) from Stage B (BATCH).
            if '"BATCH"' in payload:
                return _assignment_payload([("f000", "Texts")])
            return _taxonomy_payload(["Texts"])

        backend = MagicMock()
        backend.complete.side_effect = capture

        e = CatalogEntry(
            path_id="f000",
            path="/tmp/target/0fa84d61b3158eaba46dee96.pdf",
            ext=".pdf",
            size_bytes=2686,
            summary="Order document with shipping sections.",
            type_label="order-document",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
            verbatim_head="Order ID: 10488\n\nShipping Details:\nShip Name: Frankenversand",
        )
        cat = _catalog([e])
        classifier.classify(cat, "sort", backend=backend, logger=logger)

        # The captured payload from the *last* call (Stage B) should mention
        # the verbatim_head field name AND the slice content.
        assert "verbatim_head" in captured["payload"]
        assert "Order ID: 10488" in captured["payload"]
        assert "Shipping Details" in captured["payload"]

    def test_proposer_sample_payload_carries_verbatim_head(self, logger):
        """The Stage A sample JSON payload must include verbatim_head per
        sampled entry."""
        from fda.organize import classifier
        from fda.organize.models import CatalogEntry

        captured: dict[str, str] = {}

        def capture(*, system, messages, **kwargs):
            payload = messages[0]["content"]
            if '"CATALOG"' in payload and '"BATCH"' not in payload:
                captured["stage_a"] = payload
                return _taxonomy_payload(["Texts"])
            return _assignment_payload([("f000", "Texts")])

        backend = MagicMock()
        backend.complete.side_effect = capture

        e = CatalogEntry(
            path_id="f000",
            path="/tmp/target/Invoice_10488.pdf",
            ext=".pdf",
            size_bytes=1024,
            summary="Order document for ACME Corp.",
            type_label="order-document",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
            verbatim_head="Order ID: 10488\nCustomer: ACME",
        )
        cat = _catalog([e])
        classifier.classify(cat, "sort", backend=backend, logger=logger)

        assert "verbatim_head" in captured["stage_a"]
        assert "Order ID: 10488" in captured["stage_a"]


# ---------------------------------------------------------------------------
# Behavioral regressions: conflict, informative-filename, hash-filename
#
# These verify the wire format AND the end-to-end routing produced by a
# backend that follows the assigner prompt's priority hierarchy.
# ---------------------------------------------------------------------------


class TestPriorityRegressions:
    """End-to-end tests for the prompt's signal-priority hierarchy.

    The fake backend in each test simulates an assigner that actually
    follows the prompt: it parses the BATCH payload, asserts the expected
    fields are present (so the wire format must include them for the
    assertion to pass), and routes by inspecting `path` / `verbatim_head`
    / `summary` per the documented priority. If a future change removes
    `verbatim_head` from `_entry_dict`, these tests fail immediately
    rather than passing on a tautology.
    """

    HASH_RE = re.compile(r"^[0-9a-f]{16,}$")  # informative-vs-hash discriminator

    def _make_backend(self, taxonomy_categories, decide):
        """Return a MagicMock backend that:
          - returns the given Stage A taxonomy on the first call (whose
            payload contains 'CATALOG' but not 'BATCH'),
          - delegates Stage B per-file assignment to `decide(parsed_batch)
            -> {pid: category}` where parsed_batch is the JSON-decoded
            list of entry dicts from the BATCH payload.
        """
        backend = MagicMock()

        def respond(*, system, messages, **kwargs):
            payload = messages[0]["content"]
            if '"BATCH"' in payload:
                parsed = json.loads(payload)
                batch = parsed["BATCH"]
                mapping = decide(batch)
                return _assignment_payload(list(mapping.items()))
            return _taxonomy_payload(taxonomy_categories)

        backend.complete.side_effect = respond
        return backend

    def test_conflict_summary_says_PO_but_verbatim_says_shipping(self, logger):
        """Reader summary calls it a purchase order, but verbatim_head
        clearly shows shipping-order content. Filename is a hash (ignored).
        A simulated assigner following the prompt picks
        Shipping-And-Fulfillment by reading verbatim_head over summary."""
        from fda.organize import classifier
        from fda.organize.models import CatalogEntry

        e = CatalogEntry(
            path_id="f000",
            path="/tmp/target/0fa84d61b3158eaba46dee96.pdf",
            ext=".pdf",
            size_bytes=2686,
            summary="Purchase order #10488 for Frankenversand in Munich.",
            type_label="purchase-order",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
            verbatim_head=(
                "Order ID: 10488\n\nShipping Details:\n"
                "Ship Name: Frankenversand\nShipper Name: United Package\n"
                "Shipped Date: 2017-04-02"
            ),
        )

        def decide(batch):
            assert len(batch) == 1
            entry = batch[0]
            # Wire-format preconditions: every field the prompt's priority
            # rule depends on MUST be in the payload.
            assert entry["path_id"] == "f000"
            assert "verbatim_head" in entry
            assert "path" in entry
            assert "summary" in entry
            basename = Path(entry["path"]).name
            head = entry["verbatim_head"]
            summary = entry["summary"]
            # Priority step 2: hash basename → ignore filename signal.
            assert self.HASH_RE.match(basename.split(".")[0]), \
                "expected a hash basename for this scenario"
            # Priority step 3: verbatim says shipping ('Shipping Details',
            # 'Shipper Name', 'Shipped Date'); summary says PO. Trust verbatim.
            assert "Shipping Details" in head
            assert "Purchase order" in summary
            return {"f000": "Shipping-And-Fulfillment"}

        backend = self._make_backend(
            ["Purchase-Orders", "Shipping-And-Fulfillment"], decide,
        )
        result = classifier.classify(
            _catalog([e]), "sort", backend=backend, logger=logger,
        )
        cats = {g.category for g in result.items}
        assert "Shipping-And-Fulfillment" in cats
        assert "Purchase-Orders" not in cats

    def test_informative_filename_drives_routing(self, logger):
        """Filename `Invoice_10488.pdf` is the strongest signal even though
        verbatim_head doesn't contain the literal word 'Invoice'."""
        from fda.organize import classifier
        from fda.organize.models import CatalogEntry

        e = CatalogEntry(
            path_id="f000",
            path="/tmp/target/Invoice_10488.pdf",
            ext=".pdf",
            size_bytes=1024,
            summary="Order document for ACME Corp with line items and total.",
            type_label="order-document",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
            verbatim_head="Order ID: 10488\nCustomer: ACME\nTotal: 1234.5",
        )

        def decide(batch):
            assert len(batch) == 1
            entry = batch[0]
            assert "verbatim_head" in entry
            basename = Path(entry["path"]).name
            head = entry["verbatim_head"]
            # Priority step 1: informative basename ('Invoice' word) → use it.
            stem = basename.split(".")[0]
            assert not self.HASH_RE.match(stem), \
                "expected an informative basename for this scenario"
            assert "invoice" in basename.lower(), \
                "scenario expects 'Invoice' in the filename"
            # Priority preserves: verbatim doesn't carry the literal type word.
            assert "Invoice" not in head and "invoice" not in head.lower(), \
                "scenario expects the verbatim slice to lack the type word"
            return {"f000": "Sales-Invoices"}

        backend = self._make_backend(
            ["Sales-Invoices", "Purchase-Orders"], decide,
        )
        result = classifier.classify(
            _catalog([e]), "sort", backend=backend, logger=logger,
        )
        assert any(g.category == "Sales-Invoices" for g in result.items)

    def test_hash_filename_ignored_routing_from_content(self, logger):
        """Hash filename carries no signal — routing must come from the
        verbatim slice ('Monthly Stock Report') and summary."""
        from fda.organize import classifier
        from fda.organize.models import CatalogEntry

        e = CatalogEntry(
            path_id="f000",
            path="/tmp/target/0fa84d61b3158eaba46dee96.pdf",
            ext=".pdf",
            size_bytes=1500,
            summary="Stock report for beverages with units sold and in stock.",
            type_label="stock-report",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
            verbatim_head=(
                "Monthly Stock Report\nCategory: Beverages\n"
                "Units Sold: 240\nUnits In Stock: 1200"
            ),
        )

        def decide(batch):
            assert len(batch) == 1
            entry = batch[0]
            assert "verbatim_head" in entry
            basename = Path(entry["path"]).name
            head = entry["verbatim_head"]
            # Priority step 2: hash basename → ignore filename.
            assert self.HASH_RE.match(basename.split(".")[0])
            # Priority step 3: verbatim_head literally says 'Monthly Stock
            # Report' → Stock-Reports.
            assert "Monthly Stock Report" in head
            return {"f000": "Stock-Reports"}

        backend = self._make_backend(
            ["Stock-Reports", "Sales-Invoices"], decide,
        )
        result = classifier.classify(
            _catalog([e]), "sort", backend=backend, logger=logger,
        )
        assert any(g.category == "Stock-Reports" for g in result.items)


# ---------------------------------------------------------------------------
# sections: visible in both Stage A (proposer) and Stage B (assigner) wire
# payloads; sampling reserves slots per distinct sections-shape
# ---------------------------------------------------------------------------


class TestSectionsWireFormat:
    def _make_entry(
        self, *, path_id: str, path: str = "/x.txt",
        sections: tuple[str, ...] = (),
        verbatim_head: str = "",
        summary: str = "summary",
    ):
        from fda.organize.models import CatalogEntry
        return CatalogEntry(
            path_id=path_id,
            path=path,
            ext=".txt",
            size_bytes=10,
            summary=summary,
            type_label="text",
            is_junk=False,
            summary_failed=False,
            extract_status="ok",
            verbatim_head=verbatim_head,
            sections=sections,
        )

    def test_entry_dict_includes_sections(self):
        from fda.organize.classifier import _entry_dict

        e = self._make_entry(
            path_id="f000",
            sections=("Shipping Details", "Customer Details"),
        )
        d = _entry_dict(e)
        assert d["sections"] == ("Shipping Details", "Customer Details")

    def test_entry_dict_sections_default_empty_tuple(self):
        from fda.organize.classifier import _entry_dict

        e = self._make_entry(path_id="f000")
        assert _entry_dict(e)["sections"] == ()

    def test_proposer_payload_carries_sections(self):
        import json
        from fda.organize.classifier import _build_proposer_prompt

        sample = [
            self._make_entry(
                path_id="f000",
                sections=("Shipping Details", "Customer Details"),
            ),
            self._make_entry(path_id="f001", sections=("Products",)),
        ]
        raw = _build_proposer_prompt(sample, "instructions")
        payload = json.loads(raw)
        assert payload["CATALOG"][0]["sections"] == [
            "Shipping Details", "Customer Details"
        ]
        assert payload["CATALOG"][1]["sections"] == ["Products"]

    def test_assigner_payload_carries_sections(self):
        import json
        from fda.organize.classifier import _build_assigner_prompt
        from fda.organize.models import Taxonomy, TaxonomyCategory

        taxonomy = Taxonomy(
            categories=(
                TaxonomyCategory(
                    category_name="Purchase-Orders",
                    subpath="POs",
                    description="Simple POs",
                    criteria="Documents with Products section.",
                ),
                TaxonomyCategory(
                    category_name="Shipping-Orders",
                    subpath="Shipping",
                    description="Detailed shipping docs",
                    criteria="Documents with Shipping Details, Customer Details, and Shipper section.",
                ),
            ),
            fallback_category=TaxonomyCategory(
                category_name="Misc",
                subpath="Misc",
                description="Catch-all",
                criteria="When no other category fits.",
            ),
        )
        batch = [
            self._make_entry(
                path_id="f000",
                sections=("Shipping Details", "Customer Details"),
            ),
        ]
        raw = _build_assigner_prompt(batch, taxonomy, "instructions")
        payload = json.loads(raw)
        assert payload["BATCH"][0]["sections"] == [
            "Shipping Details", "Customer Details"
        ]


class TestSamplingShapeBudget:
    def test_constant_exists(self):
        from fda.organize.classifier import TAXONOMY_SAMPLE_SHAPE_BUDGET

        assert TAXONOMY_SAMPLE_SHAPE_BUDGET == 10

    def test_rare_shapes_reserved_in_sample(self):
        """Construct a 200-entry catalog with 6 distinct sections-shapes,
        most concentrated in the dominant shape. The new shape-budget
        rule should reserve at least one entry per distinct shape, up
        to TAXONOMY_SAMPLE_SHAPE_BUDGET.

        Critical for the test's validity: every entry lives in the SAME
        flat directory so the existing top-level-dir + leaf-dir sampling
        rules cannot accidentally substitute for the shape-budget rule.
        Also use a single shared extension. The only diversity signal is
        the `sections` tuple — any rule that doesn't read `sections`
        cannot satisfy this test.
        """
        from fda.organize.classifier import _sample_for_taxonomy
        from fda.organize.models import CatalogEntry

        def _e(idx, shape):
            return CatalogEntry(
                path_id=f"f{idx:03d}",
                # Flat directory; same extension. Top-level-dir sampling
                # sees one bucket; extension sampling sees one bucket.
                path=f"/tmp/flat/file_{idx:03d}.txt",
                ext=".txt",
                size_bytes=10,  # uniform size; "largest" rule sees no signal
                summary="similar summary",
                type_label="text",
                is_junk=False,
                summary_failed=False,
                extract_status="ok",
                sections=shape,
            )

        shapes = [
            ("Products",),
            ("Shipping Details", "Customer Details", "Products"),
            ("Quote", "Items", "Valid Until"),
            ("Patient Name", "Diagnosis", "Treatment"),
            ("From", "To", "Subject"),
            ("Sheet:Q3", "Date", "Revenue"),
        ]
        # 150 entries of shape[0], 10 each of the other 5 — total 200.
        entries = [_e(i, shapes[0]) for i in range(150)]
        for s_idx, shape in enumerate(shapes[1:], start=1):
            entries += [_e(150 + s_idx * 10 + i, shape) for i in range(10)]

        sample = _sample_for_taxonomy(entries, "/tmp")
        sample_shapes = {e.sections for e in sample}
        # All 6 distinct shapes must appear. With every entry in the same
        # directory and same extension, only a rule that reads `sections`
        # can produce this result — the shape-budget rule.
        assert len(sample_shapes) == 6
