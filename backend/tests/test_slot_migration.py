"""槽位交换、迁移流水线的离线回归测试。

只依赖标准库 unittest，直接跑：
  backend/.venv/bin/python -I -m unittest discover -s backend/tests -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.seed import SEED_ROWS
from app.services import slot_codec
from app.services.slot_migration import (
    BATCH_COMPLETED,
    BATCH_PAUSED,
    BATCH_ROLLED_BACK,
    MigrationError,
    SlotMigrationPipeline,
)


def fresh_pipeline() -> SlotMigrationPipeline:
    nlu_rows = [dict(row) for row in SEED_ROWS["nlu_result"]]
    sentiment_rows = [dict(row) for row in SEED_ROWS["sentiment_tag"]]
    for row in sentiment_rows:
        if isinstance(row.get("结构化槽位"), str) and row["结构化槽位"]:
            row["结构化槽位"] = json.loads(row["结构化槽位"])
    return SlotMigrationPipeline(nlu_rows, sentiment_rows)


class SlotCodecTest(unittest.TestCase):
    def test_legacy_repr_converts_losslessly(self):
        doc = slot_codec.build_slot_exchange(
            semantic_id="nlu-x",
            payload="{'order_id': 'A-1', 'date_range': '3天'}",
            source_sentiment_id="sent-x",
            snippet="订单 A-1",
            verified=True,
        )
        self.assertEqual(doc["schema"], slot_codec.SCHEMA)
        self.assertEqual(doc["conversion"]["status"], slot_codec.STATUS_CONVERTED)
        self.assertTrue(doc["conversion"]["lossless"])
        self.assertEqual(
            {slot["slot_type"]: slot["value"] for slot in doc["slots"]},
            {"order_id": "A-1", "date_range": "3天"},
        )
        self.assertEqual(doc["original"]["raw"], "{'order_id': 'A-1', 'date_range': '3天'}")

    def test_tuple_value_is_lossy_but_original_kept(self):
        doc = slot_codec.build_slot_exchange(
            semantic_id="nlu-y",
            payload="{'date_range': (1, 2)}",
            source_sentiment_id="sent-y",
            snippet="近两周",
            verified=True,
        )
        self.assertEqual(doc["conversion"]["status"], slot_codec.STATUS_NEEDS_REVIEW)
        self.assertFalse(doc["conversion"]["lossless"])
        self.assertEqual(doc["conversion"]["error"]["reason"], "non_json_slot_value")
        self.assertEqual(doc["original"]["raw"], "{'date_range': (1, 2)}")
        self.assertTrue(doc["rollback"]["restorable"])

    def test_unparsable_payload_keeps_semantic_identity(self):
        doc = slot_codec.build_slot_exchange(
            semantic_id="nlu-z", payload="order_id=A-1; contact=电话", verified=True
        )
        self.assertEqual(doc["conversion"]["status"], slot_codec.STATUS_UNCONVERTIBLE)
        self.assertEqual(doc["semantic_id"], "nlu-z")
        self.assertEqual(doc["original"]["raw"], "order_id=A-1; contact=电话")

    def test_unverified_source_stays_pending(self):
        doc = slot_codec.build_slot_exchange(
            semantic_id="nlu-u",
            payload="{'order_id': 'A-1'}",
            source_sentiment_id="sent-u",
            snippet="订单",
            verified=False,
        )
        self.assertEqual(doc["conversion"]["status"], slot_codec.STATUS_NEEDS_REVIEW)
        self.assertTrue(doc["conversion"]["needs_correction"])
        self.assertEqual(doc["conversion"]["error"]["reason"], "source_not_verified")

    def test_exchange_id_is_deterministic(self):
        args = dict(
            semantic_id="nlu-d",
            payload="{'a': 1}",
            source_sentiment_id="sent-d",
            snippet="x",
            verified=True,
        )
        self.assertEqual(
            slot_codec.build_slot_exchange(**args)["exchange_id"],
            slot_codec.build_slot_exchange(**args)["exchange_id"],
        )

    def test_tampered_document_fails_integrity(self):
        doc = slot_codec.build_slot_exchange(
            semantic_id="nlu-i", payload="{'a': 1}", verified=True
        )
        ok, _ = slot_codec.verify_exchange(doc)
        self.assertTrue(ok)
        doc["slots"][0]["value"] = "tampered"
        ok, message = slot_codec.verify_exchange(doc)
        self.assertFalse(ok)
        self.assertIn("完整性", message)

    def test_provenance_points_at_sentiment_snippet(self):
        doc = slot_codec.build_slot_exchange(
            semantic_id="nlu-p",
            payload="{'a': 1}",
            source_sentiment_id="sent-p",
            snippet="原文片段",
            verified=True,
        )
        for slot in doc["slots"]:
            self.assertEqual(slot["provenance"]["sentiment_record_id"], "sent-p")
            self.assertEqual(
                slot["provenance"]["snippet_sha256"],
                slot_codec.sha256_text("原文片段"),
            )


class MigrationPipelineTest(unittest.TestCase):
    def setUp(self):
        self.pipeline = fresh_pipeline()

    def test_two_versions_are_distinguished(self):
        self.pipeline.begin("b-1")
        self.pipeline.migrate()
        docs = {doc["semantic_id"]: doc for doc in self.pipeline.list_exchanges()}
        self.assertEqual(docs["nlu-2001"]["original"]["version"], "1")
        self.assertEqual(docs["nlu-2004"]["original"]["version"], "2")
        self.assertEqual(
            docs["nlu-2004"]["conversion"]["status"],
            slot_codec.STATUS_ALREADY_STRUCTURED,
        )
        self.assertIn("slx-", docs["nlu-2001"]["exchange_id"])

    def test_pause_blocks_migrate_and_resume_continues(self):
        self.pipeline.begin("b-2")
        first = self.pipeline.migrate(limit=2)
        self.assertEqual(len(first["migrated"]), 2)
        self.pipeline.pause()
        with self.assertRaises(MigrationError):
            self.pipeline.migrate()
        self.pipeline.resume()
        rest = self.pipeline.migrate()
        self.assertEqual(rest["batch"]["status"], BATCH_COMPLETED)
        self.assertEqual(len(self.pipeline.list_exchanges()), 7)

    def test_partial_ingest_accepts_validated_records_only(self):
        self.pipeline.begin("b-3")
        self.pipeline.migrate()
        run = self.pipeline.ingest(run_id="run-1")
        accepted = {item["semantic_id"] for item in run["accepted"]}
        self.assertEqual(accepted, {"nlu-2001", "nlu-2004"})
        self.assertEqual(
            {item["semantic_id"] for item in run["needs_correction"]}, {"nlu-2002"}
        )
        versions = self.pipeline.acceptance_report()["active_by_input_version"]
        self.assertEqual(versions, {"1": 1, "2": 1})

    def test_ingest_before_migration_reports_not_migrated(self):
        self.pipeline.begin("b-4")
        run = self.pipeline.ingest(run_id="run-pre")
        self.assertEqual(run["accepted_count"], 0)
        reasons = {item["semantic_id"]: item["reason"] for item in run["skipped"]}
        self.assertEqual(reasons["nlu-2004"], "not_migrated")

    def test_retry_same_run_is_idempotent(self):
        self.pipeline.begin("b-5")
        self.pipeline.migrate()
        first = self.pipeline.ingest(run_id="run-1")
        again = self.pipeline.ingest(run_id="run-1")
        self.assertIs(first, again)
        self.assertEqual(again["accepted_count"], 2)
        self.assertEqual(len(self.pipeline.ingest_runs), 1)

    def test_second_run_does_not_duplicate(self):
        self.pipeline.begin("b-6")
        self.pipeline.migrate()
        self.pipeline.ingest(run_id="run-1")
        second = self.pipeline.ingest(run_id="run-2")
        self.assertEqual(second["accepted_count"], 0)
        reasons = {item["semantic_id"]: item["reason"] for item in second["skipped"]}
        self.assertEqual(reasons["nlu-2001"], "already_accepted")
        self.assertEqual(len(self.pipeline.acceptance_report()["active_acceptances"]), 2)

    def test_rollback_restores_rows_and_revokes_acceptances(self):
        source_row = {
            k: v for k, v in self.pipeline.nlu_rows[0].items() if k.startswith("槽位")
        }
        self.pipeline.begin("b-7")
        self.pipeline.migrate()
        self.pipeline.ingest(run_id="run-1")
        self.pipeline.rollback()
        self.assertEqual(self.pipeline.batch["status"], BATCH_ROLLED_BACK)
        restored = {
            k: v for k, v in self.pipeline.nlu_rows[0].items() if k.startswith("槽位")
        }
        self.assertEqual(restored, source_row)
        self.assertEqual(
            self.pipeline.acceptance_report()["active_acceptances"], []
        )
        self.assertEqual(len(self.pipeline.batch_history), 1)
        archived = self.pipeline.batch_history[0]
        self.assertTrue(all(a["status"] == "已撤销" for a in archived["acceptances"]))

    def test_legacy_readback_matches_original(self):
        self.pipeline.begin("b-8")
        self.pipeline.migrate()
        doc = self.pipeline.exchange_for_semantic("nlu-2001")
        payload = self.pipeline.exchange_to_legacy(doc["exchange_id"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["original_version"], "1")
        self.assertEqual(payload["original"], doc["original"]["raw"])
        self.assertEqual(
            slot_codec.parse_legacy_payload(payload["legacy_repr"]),
            slot_codec.parse_legacy_payload(doc["original"]["raw"]),
        )

    def test_historical_ingested_record_is_not_reingested(self):
        self.pipeline.begin("b-h")
        self.pipeline.migrate()
        run = self.pipeline.ingest(run_id="run-hist")
        accepted = {item["semantic_id"] for item in run["accepted"]}
        self.assertNotIn("nlu-2007", accepted)
        reasons = {item["semantic_id"]: item["reason"] for item in run["skipped"]}
        self.assertEqual(reasons["nlu-2007"], "already_ingested")
        row = next(r for r in self.pipeline.nlu_rows if r["记录ID"] == "nlu-2007")
        self.assertEqual(row["status"], "已入库")
        doc = self.pipeline.exchange_for_semantic("nlu-2007")
        self.assertEqual(doc["conversion"]["status"], slot_codec.STATUS_CONVERTED)

    def test_manifest_locates_pending_corrections_by_source(self):
        self.pipeline.begin("b-9")
        self.pipeline.migrate()
        manifest = self.pipeline.manifest()
        pending = {item["semantic_id"]: item for item in manifest if item["needs_correction"]}
        self.assertEqual(set(pending), {"nlu-2002", "nlu-2005", "nlu-2006"})
        self.assertEqual(pending["nlu-2002"]["sentiment_record_id"], "sent-1002")
        self.assertEqual(pending["nlu-2002"]["snippet"], "订单 A-1024 的退款什么时候到账？")
        converted = {item["semantic_id"] for item in manifest if not item["needs_correction"]}
        self.assertEqual(converted, {"nlu-2001", "nlu-2003", "nlu-2004", "nlu-2007"})

    def test_handover_written_back_to_sentiment_rows(self):
        self.pipeline.begin("b-10")
        self.pipeline.migrate()
        sentiment = {row["记录ID"]: row for row in self.pipeline.sentiment_rows}
        self.assertEqual(sentiment["sent-1002"]["槽位交接状态"], "待补正")
        self.assertTrue(sentiment["sent-1002"]["槽位补正说明"])
        self.assertEqual(sentiment["sent-1001"]["槽位交接状态"], "已转换")
        self.assertTrue(sentiment["sent-1001"]["槽位交换ID"].startswith("slx-"))

    def test_state_roundtrip(self):
        self.pipeline.begin("b-11")
        self.pipeline.migrate()
        self.pipeline.ingest(run_id="run-1")
        state = json.loads(json.dumps(self.pipeline.to_state()))
        loaded = fresh_pipeline()
        loaded.load_state(state)
        self.assertEqual(loaded.batch["status"], BATCH_COMPLETED)
        self.assertEqual(len(loaded.exchanges), 7)
        self.assertEqual(
            loaded.acceptance_report()["active_by_input_version"], {"1": 1, "2": 1}
        )
        retry = loaded.ingest(run_id="run-2")
        self.assertEqual(retry["accepted_count"], 0)


if __name__ == "__main__":
    unittest.main()
