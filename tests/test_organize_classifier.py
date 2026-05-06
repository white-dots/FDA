# tests/test_organize_classifier.py
"""Tests for fda.organize.classifier."""

from __future__ import annotations

import json
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
