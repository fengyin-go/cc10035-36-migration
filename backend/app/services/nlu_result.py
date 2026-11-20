"""语义结果(NLU)领域服务：管理自然语言理解的语义结构化输出。"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from app.services import slot_codec
from app.services.base import BaseModuleService
from app.services.slot_migration import pipeline
from app.store import store


INTENT_LABELS = [
    "查询产品信息", "对比价格", "咨询售后", "投诉反馈",
    "订单查询", "物流追踪", "功能咨询", "操作指导", "通用对话",
]
SLOT_TYPES = ["product_name", "order_id", "date_range", "price_range", "contact_method"]

# 对外列表仍保留「槽位列表」作为可读展示列，但结构化交换值单独成列，
# 避免读取端再从 Python 字典字符串里做不稳定解析。
LEGACY_SLOT_PATTERN = re.compile(r"^\{.*\}$", re.DOTALL)


class NluResultService(BaseModuleService):
    MODULE = "nlu_result"
    ENTITY = "语义记录"
    OPERATION_TERM = "入库"
    LIST_FIELDS = ["记录ID", "输入文本", "意图标签", "槽位列表", "槽位状态", "语义框架", "语言模型", "预测分数"]
    REQUIRED_FIELDS = ["记录ID", "输入文本", "意图标签"]
    STATUS_ORDER = ["已预测", "已验证", "已入库", "已驳回"]
    ACTION_RULES = {"验证结果": "已预测", "确认入库": "已验证", "驳回结果": "已预测"}
    NEGATIVE_ACTIONS = ["驳回结果"]

    def nlu_predict(self, text_chunk: str, source_page: str = "") -> dict[str, Any]:
        """对文本块做NLU预测：直接产出 v2 结构化槽位，不再落回字典字符串。"""
        text_hash = int(hashlib.md5(text_chunk.encode()).hexdigest()[:4], 16)
        intent_idx = text_hash % len(INTENT_LABELS)
        intent = INTENT_LABELS[intent_idx]
        slots = []
        for slot_type in SLOT_TYPES:
            slot_hash = int(hashlib.md5(f"{text_chunk}|{slot_type}".encode()).hexdigest()[:4], 16)
            if slot_hash % 3 == 0:
                continue
            slots.append({"slot_type": slot_type, "value": f"value_from_{intent_idx}_{slot_hash % 100}"})
        score = round(0.65 + (text_hash % 300) / 1000.0, 2)
        record_id = hashlib.sha256(text_chunk.encode()).hexdigest()[:12]

        return {
            "记录ID": record_id,
            "输入文本": text_chunk[:200],
            "意图标签": intent,
            "槽位列表": slot_codec.render_legacy_repr(slots),
            "语义框架": "intent_slot_framework_v3",
            "语言模型": "nlu-transformer-zh",
            "预测分数": str(score),
            "slot_proposal": {"schema": slot_codec.SCHEMA, "schema_version": slot_codec.CURRENT_VERSION, "slots": slots},
            "source_page": source_page,
        }

    def consume_sentiment_and_segment(self, sentiment_record_id: str) -> dict[str, Any]:
        """消费情感标注的输出，用片段原文作为槽位出处接入NLU预测链路。"""
        sentiment_row = next(
            (r for r in store.rows("sentiment_tag") if r.get("记录ID") == sentiment_record_id),
            None
        )
        if sentiment_row is None:
            return {"ok": False, "reason": "情感记录不存在"}

        text = sentiment_row.get("文本片段", "")
        sentiment = sentiment_row.get("情感倾向", "neutral")
        nlu_result = self.nlu_predict(text)
        verified = str(sentiment_row.get("槽位核对状态") or "") == "已核对"
        doc = slot_codec.build_slot_exchange(
            semantic_id=nlu_result["记录ID"],
            payload=nlu_result["slot_proposal"],
            intent=nlu_result["意图标签"],
            source_sentiment_id=sentiment_record_id,
            snippet=text,
            input_text=nlu_result["输入文本"],
            verified=verified,
            origin="online",
        )
        doc["source"]["snippet_source"] = sentiment_row.get("片段出处", "")
        for slot in doc["slots"]:
            slot["provenance"]["snippet_source"] = sentiment_row.get("片段出处", "")
        slot_codec.reseal_exchange(doc)
        nlu_result["slot_exchange"] = doc
        nlu_result["source_sentiment"] = sentiment
        nlu_result["source_intensity"] = sentiment_row.get("强烈程度", "")
        nlu_result["source_snippet"] = text
        return nlu_result

    def read_legacy_slots(self, entry_id: int) -> dict[str, Any]:
        """按旧版读取接口返回已核对槽位：优先交换数据中的原值，其次行内列。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": "语义记录不存在"}
        semantic_id = str(entry.get("记录ID") or "")
        doc = pipeline.exchange_for_semantic(semantic_id)
        if doc is not None:
            payload = pipeline.exchange_to_legacy(doc["exchange_id"])
            payload["legacy_repr"] = (
                slot_codec.render_legacy_repr(doc["slots"])
                if doc["conversion"]["lossless"]
                else payload.get("original")
            )
            return payload
        raw = entry.get("槽位列表", "")
        original_raw = entry.get("原槽位列表") or raw
        if isinstance(original_raw, str) and LEGACY_SLOT_PATTERN.match(original_raw.strip()):
            try:
                parsed = slot_codec.parse_legacy_payload(original_raw)
            except slot_codec.UnconvertibleSlotPayload as exc:
                return {
                    "ok": False,
                    "reason": "旧槽位无法按字典还原",
                    "detail": exc.detail,
                    "semantic_id": semantic_id,
                    "original_version": str(
                        entry.get("原槽位版本")
                        or entry.get("槽位版本")
                        or slot_codec.LEGACY_VERSION
                    ),
                    "original": original_raw,
                }
            except slot_codec.LossySlotPayload as exc:
                # 语法能还原但有损：旧读取端仍可拿到原值，同时标出不可无损交换。
                return {
                    "ok": True,
                    "semantic_id": semantic_id,
                    "original_version": str(
                        entry.get("原槽位版本")
                        or entry.get("槽位版本")
                        or slot_codec.LEGACY_VERSION
                    ),
                    "original": original_raw,
                    "legacy_repr": original_raw,
                    "verified": entry.get("槽位核验") == "已核对",
                    "lossless": False,
                    "status": "lossy",
                    "status_label": "有损待补正",
                    "detail": exc.detail,
                }
            return {
                "ok": True,
                "semantic_id": semantic_id,
                "original_version": str(
                    entry.get("原槽位版本")
                    or entry.get("槽位版本")
                    or slot_codec.LEGACY_VERSION
                ),
                "original": original_raw,
                "legacy_repr": slot_codec.render_legacy_repr(parsed),
                "verified": entry.get("槽位核验") == "已核对",
                "lossless": True,
                "status": "unmigrated",
                "status_label": "未迁移",
            }
        return {
            "ok": False,
            "reason": "槽位不是可解析的旧字典表示",
            "semantic_id": semantic_id,
            "original": original_raw,
        }

    def compute_nlu_stats(self) -> dict[str, Any]:
        """NLU统计指标。"""
        rows = store.rows(self.MODULE)
        intent_dist: dict[str, int] = {}
        total_score = 0.0
        for row in rows:
            intent = row.get("意图标签", "未知")
            intent_dist[intent] = intent_dist.get(intent, 0) + 1
            try:
                total_score += float(row.get("预测分数", "0"))
            except (ValueError, TypeError):
                pass
        return {
            "total_predictions": len(rows),
            "intent_distribution": intent_dist,
            "avg_score": round(total_score / len(rows), 2) if rows else 0.0,
            "validated_count": sum(1 for r in rows if r.get("status") == "已验证"),
            "ingested_count": sum(1 for r in rows if r.get("status") == "已入库"),
            "slot_pending_count": sum(
                1 for r in rows
                if str(r.get("槽位状态") or "") in ("待迁移", "待补正", "不可转换")
            ),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["nlu_stats"] = self.compute_nlu_stats()
        base["slot_migration"] = {
            "status_counts": pipeline.state()["status_counts"],
            "batch_status": pipeline.batch["status"],
        }
        return base
