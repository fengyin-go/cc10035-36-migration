"""语义槽位迁移流水线：v1/v2 槽位 -> 带版本的交换数据 -> 语义入库 / 回滚。

设计要点
--------
* 只依赖内存行和纯函数编解码，可在离线脚本里复用，不需要起服务。
* 每处理一条语义记录都会写回语义行和对应情感记录的交接清单字段。
* 交换ID由「语义身份 + 情感来源 + 原值哈希」确定，重试不会产生第二条。
* 入库只接纳核对通过且无损转换的槽位；失败条目保留原值，可随时回滚。
* 入库记录（acceptance）保存实际接收的槽位、输入版本和片段出处，供事后核对。
"""
from __future__ import annotations

import copy
from typing import Any, Iterable

from app.services import slot_codec
from app.services.slot_codec import (
    CURRENT_VERSION,
    LEGACY_VERSION,
    PENDING_STATUSES,
    STATUS_ALREADY_STRUCTURED,
    STATUS_CONVERTED,
    STATUS_LABELS,
    STATUS_NEEDS_REVIEW,
    STATUS_UNCONVERTIBLE,
    utc_now,
)

BATCH_IDLE = "未开始"
BATCH_RUNNING = "迁移中"
BATCH_PAUSED = "已暂停"
BATCH_COMPLETED = "已完成"
BATCH_ROLLED_BACK = "已回滚"

ACTION_ALIASES = {
    "开始迁移": "start",
    "start": "start",
    "迁移": "migrate",
    "migrate": "migrate",
    "暂停": "pause",
    "pause": "pause",
    "恢复": "resume",
    "resume": "resume",
    "回滚": "rollback",
    "rollback": "rollback",
    "重置": "reset",
    "reset": "reset",
    "演练": "rehearse",
    "rehearse": "rehearse",
}

INGESTIBLE_STATUSES = frozenset({STATUS_CONVERTED, STATUS_ALREADY_STRUCTURED})
# 已确认的历史记录才允许进入语义入库；未验证的预测先走验证动作。
INGEST_ROW_STATUSES = frozenset({"已验证", "已入库"})


def _row_key(row: dict[str, Any], index: int) -> str:
    value = row.get("id")
    return str(value) if value is not None else f"idx-{index}"


def _sentiment_index(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("记录ID")): row
        for row in rows
        if row.get("记录ID") is not None
    }


class MigrationError(ValueError):
    """流水线状态不允许当前动作。"""


class SlotMigrationPipeline:
    def __init__(
        self,
        nlu_rows: list[dict[str, Any]],
        sentiment_rows: list[dict[str, Any]],
    ) -> None:
        self.nlu_rows = nlu_rows
        self.sentiment_rows = sentiment_rows
        self.batch: dict[str, Any] = {
            "batch_id": None,
            "name": "",
            "status": BATCH_IDLE,
            "created_at": None,
            "updated_at": None,
            "cursor": 0,
            "total": 0,
            "processed": [],
        }
        self.exchanges: dict[str, dict[str, Any]] = {}
        self.acceptances: list[dict[str, Any]] = []
        self.ingest_runs: list[dict[str, Any]] = []
        self.batch_history: list[dict[str, Any]] = []
        self.snapshots: dict[str, dict[str, dict[str, Any]]] = {
            "nlu_result": {},
            "sentiment_tag": {},
        }

    # ------------------------------------------------------------------ 快照
    def _snapshot(self) -> None:
        for name, rows in (
            ("nlu_result", self.nlu_rows),
            ("sentiment_tag", self.sentiment_rows),
        ):
            self.snapshots[name] = {
                _row_key(row, index): copy.deepcopy(row)
                for index, row in enumerate(rows)
            }

    def _restore_snapshot(self) -> None:
        for name, rows in (
            ("nlu_result", self.nlu_rows),
            ("sentiment_tag", self.sentiment_rows),
        ):
            for index, row in enumerate(rows):
                snapshot = self.snapshots[name].get(_row_key(row, index))
                if snapshot is None:
                    continue
                row.clear()
                row.update(copy.deepcopy(snapshot))

    # ------------------------------------------------------------------ 批次
    def begin(self, batch_id: str | None = None, name: str = "") -> dict[str, Any]:
        if self.batch["status"] == BATCH_RUNNING:
            raise MigrationError("上一批迁移仍在进行，请先暂停或完成")
        if self.batch["status"] in (BATCH_PAUSED, BATCH_COMPLETED):
            raise MigrationError("当前批次尚未回滚，请先回滚再开启新批次")
        now = utc_now()
        batch_id = batch_id or f"mb-{now.replace('-', '').replace(':', '')}"
        self.snapshots = {"nlu_result": {}, "sentiment_tag": {}}
        self._snapshot()
        self.batch = {
            "batch_id": batch_id,
            "name": name or batch_id,
            "status": BATCH_RUNNING,
            "created_at": now,
            "updated_at": now,
            "cursor": 0,
            "total": len(self.nlu_rows),
            "processed": [],
        }
        self.exchanges.clear()
        self.acceptances.clear()
        self.ingest_runs.clear()
        return self.batch

    def pause(self) -> dict[str, Any]:
        if self.batch["status"] != BATCH_RUNNING:
            raise MigrationError("只有迁移中的批次可以暂停")
        self.batch["status"] = BATCH_PAUSED
        self.batch["updated_at"] = utc_now()
        return self.batch

    def resume(self) -> dict[str, Any]:
        if self.batch["status"] != BATCH_PAUSED:
            raise MigrationError("只有已暂停的批次可以恢复")
        self.batch["status"] = BATCH_RUNNING
        self.batch["updated_at"] = utc_now()
        return self.batch

    def rollback(self) -> dict[str, Any]:
        if self.batch["status"] in (BATCH_IDLE, BATCH_ROLLED_BACK):
            raise MigrationError("当前没有可回滚的迁移批次")
        if self.batch["status"] == BATCH_RUNNING:
            raise MigrationError("迁移仍在进行，请先暂停再回滚")
        now = utc_now()
        for doc in self.exchanges.values():
            if doc.get("migration", {}).get("batch_id") == self.batch["batch_id"]:
                doc.setdefault("rollback", {})["rolled_back_at"] = now
                doc["rollback_state"] = "已回滚"
                doc["updated_at"] = now
        revoked = 0
        for acceptance in self.acceptances:
            if (
                acceptance.get("batch_id") == self.batch["batch_id"]
                and acceptance.get("status") == "已接纳"
            ):
                acceptance["status"] = "已撤销"
                acceptance["revoked_at"] = now
                revoked += 1
        self._restore_snapshot()
        self.batch["status"] = BATCH_ROLLED_BACK
        self.batch["updated_at"] = now
        self.batch["revoked_acceptances"] = revoked
        # 批次与交换数据摘要归档，回滚后仍可核对这次迁移接纳过什么。
        self.batch_history.append(
            {
                "batch": copy.deepcopy(self.batch),
                "exchanges": [copy.deepcopy(doc) for doc in self.exchanges.values()],
                "acceptances": copy.deepcopy(self.acceptances),
                "snapshots": copy.deepcopy(self.snapshots),
                "archived_at": now,
            }
        )
        return self.batch

    def reset(self) -> dict[str, Any]:
        if self.batch["status"] in (BATCH_RUNNING, BATCH_PAUSED, BATCH_COMPLETED):
            self.rollback()
        self.exchanges.clear()
        self.acceptances.clear()
        self.ingest_runs.clear()
        self.batch_history.clear()
        self.snapshots = {"nlu_result": {}, "sentiment_tag": {}}
        self.batch = {
            "batch_id": None,
            "name": "",
            "status": BATCH_IDLE,
            "created_at": None,
            "updated_at": None,
            "cursor": 0,
            "total": 0,
            "processed": [],
        }
        return self.batch

    # ------------------------------------------------------------------ 迁移
    def migrate(self, limit: int | None = None) -> dict[str, Any]:
        status = self.batch["status"]
        if status in (BATCH_IDLE,):
            raise MigrationError("请先开始迁移批次")
        if status == BATCH_PAUSED:
            raise MigrationError("批次已暂停，恢复后才能继续迁移")
        if status in (BATCH_COMPLETED, BATCH_ROLLED_BACK):
            raise MigrationError("当前批次已结束，不能继续迁移")
        processed_ids = {str(item) for item in self.batch["processed"]}
        sentiment_by_id = _sentiment_index(self.sentiment_rows)
        migrated_now: list[dict[str, Any]] = []
        cursor = int(self.batch["cursor"])
        for index in range(cursor, len(self.nlu_rows)):
            row = self.nlu_rows[index]
            row_id = str(row.get("记录ID") or row.get("id") or index)
            existing_exchange_id = row.get("槽位交换ID")
            if row_id in processed_ids and existing_exchange_id in self.exchanges:
                self.batch["cursor"] = index + 1
                continue
            if limit is not None and len(migrated_now) >= limit:
                break
            doc = self._migrate_row(row, sentiment_by_id)
            self.exchanges[doc["exchange_id"]] = doc
            row["槽位交换ID"] = doc["exchange_id"]
            if row_id not in processed_ids:
                processed_ids.add(row_id)
                self.batch["processed"].append(row_id)
            self.batch["cursor"] = index + 1
            migrated_now.append(self._exchange_summary(doc))
        self.batch["updated_at"] = utc_now()
        if self.batch["cursor"] >= len(self.nlu_rows):
            self.batch["status"] = BATCH_COMPLETED
        return {
            "batch": dict(self.batch),
            "migrated": migrated_now,
            "remaining": max(len(self.nlu_rows) - self.batch["cursor"], 0),
        }

    def _migrate_row(
        self, row: dict[str, Any], sentiment_by_id: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        sentiment_id = str(row.get("来源情感记录") or "")
        sentiment_row = sentiment_by_id.get(sentiment_id, {})
        snippet = str(sentiment_row.get("文本片段") or "")
        snippet_source = str(sentiment_row.get("片段出处") or "")
        verified = str(sentiment_row.get("槽位核对状态") or "") == "已核对"

        structured = row.get("结构化槽位")
        if structured:
            payload: Any = structured
            declared_version = str(row.get("槽位版本") or CURRENT_VERSION)
        else:
            payload = row.get("槽位列表", "")
            declared_version = str(row.get("槽位版本") or LEGACY_VERSION)

        semantic_id = str(row.get("记录ID") or f"nlu-{row.get('id')}")
        doc = slot_codec.build_slot_exchange(
            semantic_id=semantic_id,
            payload=payload,
            nlu_record_id=row.get("id"),
            intent=str(row.get("意图标签") or ""),
            source_sentiment_id=sentiment_id,
            snippet=snippet,
            input_text=str(row.get("输入文本") or ""),
            verified=verified,
            batch_id=self.batch["batch_id"],
            origin="migration",
        )
        doc["source"]["snippet_source"] = snippet_source
        doc["source"]["declared_version"] = declared_version
        for slot in doc["slots"]:
            slot["provenance"]["snippet_source"] = snippet_source
        slot_codec.reseal_exchange(doc)
        self._write_back_nlu(row, doc, declared_version)
        self._write_back_handover(sentiment_row, doc, snippet_source)
        return doc

    def _write_back_nlu(
        self, row: dict[str, Any], doc: dict[str, Any], declared_version: str
    ) -> None:
        conversion = doc["conversion"]
        lossless = conversion["lossless"]
        row["原槽位版本"] = doc["original"]["version"]
        row["槽位交换ID"] = doc["exchange_id"]
        row["槽位状态"] = conversion["status_label"]
        row["迁移批次"] = doc["migration"]["batch_id"]
        row["槽位出处"] = doc["source"]["snippet_source"] or doc["source"]["sentiment_record_id"]
        original_display = str(row.get("原槽位列表") or row.get("槽位列表") or "")
        row["原槽位列表"] = original_display
        if lossless:
            row["槽位版本"] = CURRENT_VERSION
            row["结构化槽位"] = slot_codec.canonical_json(doc)
            row["槽位列表"] = "; ".join(
                f"{slot['slot_type']}={slot['value']}" for slot in doc["slots"]
            ) or "{}"
        else:
            # 有损/不可转换时保持原读取方式，原值可继续被旧读取端按 v1 读取。
            row["槽位版本"] = declared_version
            row["结构化槽位"] = ""
        row["槽位核验"] = "已核对" if conversion["verified"] else "待核对"

    def _write_back_handover(
        self, sentiment_row: dict[str, Any], doc: dict[str, Any], snippet_source: str
    ) -> None:
        if not sentiment_row:
            return
        conversion = doc["conversion"]
        sentiment_row["槽位交接状态"] = conversion["status_label"]
        sentiment_row["槽位交接批次"] = doc["migration"]["batch_id"]
        sentiment_row["槽位交接版本"] = doc["schema_version"]
        sentiment_row["槽位交换ID"] = doc["exchange_id"]
        sentiment_row["槽位语义记录"] = doc["semantic_id"]
        sentiment_row["槽位核对状态"] = (
            "已核对" if conversion["verified"] else sentiment_row.get("槽位核对状态", "未核对")
        )
        sentiment_row["槽位补正说明"] = (
            (conversion.get("error") or {}).get("detail", "")
            if conversion["needs_correction"]
            else ""
        )
        sentiment_row["片段出处"] = sentiment_row.get("片段出处") or snippet_source

    # ------------------------------------------------------------------ 交换数据
    @staticmethod
    def _exchange_summary(doc: dict[str, Any]) -> dict[str, Any]:
        return {
            "exchange_id": doc["exchange_id"],
            "semantic_id": doc["semantic_id"],
            "source_sentiment_id": doc["source"].get("sentiment_record_id", ""),
            "version": doc["original"]["version"],
            "schema_version": doc["schema_version"],
            "status": doc["conversion"]["status"],
            "status_label": doc["conversion"]["status_label"],
            "slot_count": len(doc["slots"]),
            "needs_correction": doc["conversion"]["needs_correction"],
        }

    def list_exchanges(
        self,
        status: str | None = None,
        source_sentiment_id: str | None = None,
        semantic_id: str | None = None,
        version: str | None = None,
    ) -> list[dict[str, Any]]:
        items = []
        for doc in self.exchanges.values():
            if status and doc["conversion"]["status"] != status:
                continue
            if (
                source_sentiment_id
                and doc["source"].get("sentiment_record_id") != source_sentiment_id
            ):
                continue
            if semantic_id and doc["semantic_id"] != semantic_id:
                continue
            if version and str(doc["original"]["version"]) != str(version):
                continue
            items.append(doc)
        items.sort(
            key=lambda item: (item["conversion"]["needs_correction"], item["semantic_id"])
        )
        return items

    def find_exchange(self, exchange_id: str) -> dict[str, Any] | None:
        return self.exchanges.get(exchange_id)

    def exchange_for_semantic(self, semantic_id: str) -> dict[str, Any] | None:
        for doc in self.exchanges.values():
            if doc["semantic_id"] == semantic_id:
                return doc
        return None

    def manifest(self, status: str | None = None) -> list[dict[str, Any]]:
        """标注交接清单：按来源汇总已转换槽位与仍要补正的记录。"""
        accepted_by_exchange = {
            item["exchange_id"]
            for item in self.acceptances
            if item.get("status") == "已接纳"
        }
        entries: list[dict[str, Any]] = []
        for doc in self.list_exchanges(status=status):
            source = doc["source"]
            entries.append(
                {
                    "sentiment_record_id": source.get("sentiment_record_id", ""),
                    "snippet": source.get("snippet", ""),
                    "snippet_sha256": source.get("snippet_sha256", ""),
                    "snippet_source": source.get("snippet_source", ""),
                    "semantic_id": doc["semantic_id"],
                    "nlu_record_id": doc.get("nlu_record_id"),
                    "exchange_id": doc["exchange_id"],
                    "original_version": doc["original"]["version"],
                    "conversion_version": doc["schema_version"],
                    "status": doc["conversion"]["status"],
                    "status_label": doc["conversion"]["status_label"],
                    "needs_correction": doc["conversion"]["needs_correction"],
                    "reason": (doc["conversion"].get("error") or {}).get("reason", ""),
                    "detail": (doc["conversion"].get("error") or {}).get("detail", ""),
                    "batch_id": doc["migration"].get("batch_id"),
                    "accepted": doc["exchange_id"] in accepted_by_exchange,
                    "slot_types": [slot["slot_type"] for slot in doc["slots"]],
                }
            )
        entries.sort(
            key=lambda item: (not item["needs_correction"], item["sentiment_record_id"])
        )
        return entries

    def exchange_to_legacy(self, exchange_id: str) -> dict[str, Any]:
        """按 v1 读取已验证的原值，供旧读取端在切换期回退。"""
        doc = self.exchanges.get(exchange_id)
        if doc is None:
            return {"ok": False, "reason": "交换记录不存在", "exchange_id": exchange_id}
        conversion = doc["conversion"]
        original = slot_codec.source_payload_for_rollback(doc)
        return {
            "ok": True,
            "exchange_id": exchange_id,
            "semantic_id": doc["semantic_id"],
            "original_version": original[0],
            "original": original[1],
            "legacy_repr": (
                slot_codec.render_legacy_repr(doc["slots"])
                if conversion["lossless"]
                else original[1]
            ),
            "verified": bool(conversion.get("verified")),
            "lossless": bool(conversion.get("lossless")),
            "status": conversion["status"],
            "status_label": conversion["status_label"],
            "rollback": copy.deepcopy(doc.get("rollback", {})),
        }

    # ------------------------------------------------------------------ 入库
    def ingest(self, run_id: str | None = None) -> dict[str, Any]:
        """执行一次语义入库；同一交换记录不会被重复接纳。"""
        if run_id:
            for existing in self.ingest_runs:
                if existing["run_id"] == run_id:
                    return existing
        run_id = run_id or f"ing-{utc_now().replace('-', '').replace(':', '')}"
        active_exchanges = {
            item["exchange_id"]
            for item in self.acceptances
            if item.get("status") == "已接纳"
        }
        accepted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in self.nlu_rows:
            semantic_id = str(row.get("记录ID") or f"nlu-{row.get('id')}")
            row_status = str(row.get("status") or "")
            exchange_id = str(row.get("槽位交换ID") or "")
            doc = self.exchanges.get(exchange_id) or self.exchange_for_semantic(semantic_id)
            if row_status not in INGEST_ROW_STATUSES:
                skipped.append(
                    {
                        "semantic_id": semantic_id,
                        "exchange_id": exchange_id,
                        "reason": "not_validated",
                        "detail": f"语义记录状态为「{row_status}」，尚未通过验证",
                        "needs_sentiment_correction": False,
                    }
                )
                continue
            if doc is None:
                skipped.append(
                    {
                        "semantic_id": semantic_id,
                        "exchange_id": exchange_id,
                        "reason": "not_migrated",
                        "detail": "尚无带版本交换数据，请先执行迁移",
                        "needs_sentiment_correction": False,
                    }
                )
                continue
            if doc.get("rollback_state") == "已回滚":
                skipped.append(
                    {
                        "semantic_id": semantic_id,
                        "exchange_id": exchange_id,
                        "reason": "rolled_back",
                        "detail": "交换记录已回滚，需重跑迁移",
                        "needs_sentiment_correction": False,
                    }
                )
                continue
            conversion = doc["conversion"]
            if (
                conversion["status"] not in INGESTIBLE_STATUSES
                or not conversion["lossless"]
            ):
                skipped.append(
                    {
                        "semantic_id": semantic_id,
                        "exchange_id": exchange_id,
                        "original_version": doc["original"]["version"],
                        "status": conversion["status"],
                        "status_label": conversion["status_label"],
                        "reason": (conversion.get("error") or {}).get(
                            "reason", "needs_review"
                        ),
                        "detail": (conversion.get("error") or {}).get(
                            "detail", "槽位转换未经核对，需回到情感标注补正"
                        ),
                        "source_sentiment_id": doc["source"].get(
                            "sentiment_record_id", ""
                        ),
                        "snippet": doc["source"].get("snippet", ""),
                        "rollback_semantic_id": doc["semantic_id"],
                        "original": copy.deepcopy(doc["original"]),
                        "needs_sentiment_correction": True,
                    }
                )
                continue
            if exchange_id in active_exchanges:
                skipped.append(
                    {
                        "semantic_id": semantic_id,
                        "exchange_id": exchange_id,
                        "reason": "already_accepted",
                        "detail": "该交换记录已被语义入库接纳，重试不重复计入",
                        "needs_sentiment_correction": False,
                    }
                )
                continue
            if row_status == "已入库":
                # 兼容历史已确认并已入库的记录：不重复接纳，但保留可核对信息。
                skipped.append(
                    {
                        "semantic_id": semantic_id,
                        "exchange_id": exchange_id,
                        "input_version": doc["original"]["version"],
                        "reason": "already_ingested",
                        "detail": "历史已入库记录保持原状，不重复入库",
                        "needs_sentiment_correction": False,
                    }
                )
                continue
            acceptance = self._accept(doc, run_id)
            self.acceptances.append(acceptance)
            active_exchanges.add(exchange_id)
            row["status"] = "已入库"
            row["pending"] = False
            row["入库槽位版本"] = doc["original"]["version"]
            row["入库批次"] = doc["migration"].get("batch_id")
            row["入库时间"] = acceptance["accepted_at"]
            accepted.append(acceptance)
        run = {
            "run_id": run_id,
            "batch_id": self.batch["batch_id"],
            "executed_at": utc_now(),
            "accepted": accepted,
            "skipped": skipped,
            "accepted_count": len(accepted),
            "skipped_count": len(skipped),
            "needs_correction": [
                item for item in skipped if item.get("needs_sentiment_correction")
            ],
        }
        self.ingest_runs.append(run)
        return run

    def _accept(self, doc: dict[str, Any], run_id: str) -> dict[str, Any]:
        source = doc["source"]
        return {
            "acceptance_id": f"acc-{doc['exchange_id']}-{run_id}",
            "run_id": run_id,
            "batch_id": doc["migration"].get("batch_id"),
            "exchange_id": doc["exchange_id"],
            "semantic_id": doc["semantic_id"],
            "nlu_record_id": doc.get("nlu_record_id"),
            "input_version": doc["original"]["version"],
            "accepted_version": doc["schema_version"],
            "status": "已接纳",
            "accepted_at": utc_now(),
            "slot_count": len(doc["slots"]),
            "slots": [
                {
                    "slot_id": slot["slot_id"],
                    "slot_type": slot["slot_type"],
                    "value": copy.deepcopy(slot["value"]),
                    "value_type": slot["value_type"],
                    "provenance_type": slot["provenance"].get("type", ""),
                    "sentiment_record_id": slot["provenance"].get(
                        "sentiment_record_id", ""
                    ),
                    "snippet_sha256": slot["provenance"].get("snippet_sha256", ""),
                    "snippet_source": slot["provenance"].get("snippet_source", ""),
                }
                for slot in doc["slots"]
            ],
            "source_sentiment_id": source.get("sentiment_record_id", ""),
            "snippet": source.get("snippet", ""),
            "snippet_sha256": source.get("snippet_sha256", ""),
        }

    def acceptance_report(self) -> dict[str, Any]:
        active = [item for item in self.acceptances if item.get("status") == "已接纳"]
        by_version: dict[str, int] = {}
        for item in active:
            key = str(item.get("input_version"))
            by_version[key] = by_version.get(key, 0) + 1
        return {
            "runs": self.ingest_runs,
            "active_acceptances": active,
            "revoked_acceptances": [
                item for item in self.acceptances if item.get("status") != "已接纳"
            ],
            "active_by_input_version": by_version,
        }

    # ------------------------------------------------------------------ 状态
    def state(self) -> dict[str, Any]:
        manifest = self.manifest()
        return {
            "batch": dict(self.batch),
            "total_records": len(self.nlu_rows),
            "exchange_count": len(self.exchanges),
            "status_counts": {
                status: sum(
                    1
                    for doc in self.exchanges.values()
                    if doc["conversion"]["status"] == status
                )
                for status in (
                    STATUS_ALREADY_STRUCTURED,
                    STATUS_CONVERTED,
                    STATUS_NEEDS_REVIEW,
                    STATUS_UNCONVERTIBLE,
                )
            },
            "manifest_count": len(manifest),
            "needs_correction": [item for item in manifest if item["needs_correction"]],
            "acceptance_count": sum(
                1 for item in self.acceptances if item.get("status") == "已接纳"
            ),
            "acceptance_revoked": sum(
                1 for item in self.acceptances if item.get("status") == "已撤销"
            ),
            "last_ingest_run": self.ingest_runs[-1] if self.ingest_runs else None,
            "batch_history": self.batch_history,
        }

    # ------------------------------------------------------------------ 持久化
    def to_state(self) -> dict[str, Any]:
        rows = {}
        for index, row in enumerate(self.nlu_rows):
            rows.setdefault("nlu_result", {})[_row_key(row, index)] = copy.deepcopy(row)
        for index, row in enumerate(self.sentiment_rows):
            rows.setdefault("sentiment_tag", {})[_row_key(row, index)] = copy.deepcopy(row)
        return {
            "batch": copy.deepcopy(self.batch),
            "exchanges": copy.deepcopy(self.exchanges),
            "acceptances": copy.deepcopy(self.acceptances),
            "ingest_runs": copy.deepcopy(self.ingest_runs),
            "batch_history": copy.deepcopy(self.batch_history),
            "snapshots": copy.deepcopy(self.snapshots),
            "rows": rows,
        }

    def load_state(self, state: dict[str, Any]) -> None:
        self.batch = copy.deepcopy(state.get("batch") or self.batch)
        self.exchanges = copy.deepcopy(state.get("exchanges") or {})
        self.acceptances = copy.deepcopy(state.get("acceptances") or [])
        self.ingest_runs = copy.deepcopy(state.get("ingest_runs") or [])
        self.batch_history = copy.deepcopy(state.get("batch_history") or [])
        self.snapshots = copy.deepcopy(
            state.get("snapshots") or {"nlu_result": {}, "sentiment_tag": {}}
        )
        rows = state.get("rows") or {}
        self.nlu_rows[:] = [
            copy.deepcopy(row) for row in rows.get("nlu_result", {}).values()
        ]
        self.sentiment_rows[:] = [
            copy.deepcopy(row) for row in rows.get("sentiment_tag", {}).values()
        ]

    def rehearsal_summary(self) -> dict[str, Any]:
        """离线演练报告：迁移/入库全流程结果与旧版本回读校验。"""
        legacy_checks = []
        for doc in self.exchanges.values():
            check = {
                "exchange_id": doc["exchange_id"],
                "semantic_id": doc["semantic_id"],
                "original_version": doc["original"]["version"],
                "verified": bool(doc["conversion"].get("verified")),
                "roundtrip_ok": None,
            }
            if doc["conversion"]["lossless"] and doc["original"]["version"] == LEGACY_VERSION:
                parsed = slot_codec.parse_legacy_payload(doc["original"]["raw"])
                rendered = slot_codec.render_legacy_repr(parsed)
                check["roundtrip_ok"] = rendered == slot_codec.render_legacy_repr(
                    slot_codec.parse_legacy_payload(rendered)
                )
            legacy_checks.append(check)
        return {
            "batch": dict(self.batch),
            "state": self.state(),
            "manifest": self.manifest(),
            "legacy_read_checks": legacy_checks,
        }


# ---------------------------------------------------------------------- 在线单例
def _default_pipeline() -> SlotMigrationPipeline:
    from app.store import store

    return SlotMigrationPipeline(
        store.rows("nlu_result"), store.rows("sentiment_tag")
    )


pipeline = _default_pipeline()


def reset_pipeline() -> SlotMigrationPipeline:
    """测试/演练重置：恢复种子行并清空迁移状态。"""
    global pipeline
    pipeline = _default_pipeline()
    return pipeline
