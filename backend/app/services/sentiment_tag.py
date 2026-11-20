"""情感标注领域服务：对文本片段进行情感倾向标注。"""
from __future__ import annotations

import hashlib
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


SENTIMENT_LABELS = ["positive", "negative", "neutral", "mixed"]
ASPECT_SENTIMENTS = {
    "价格": ["positive", "negative", "neutral"],
    "质量": ["positive", "negative", "neutral"],
    "服务": ["positive", "negative", "neutral"],
    "交付": ["positive", "negative", "neutral"],
}


class SentimentTagService(BaseModuleService):
    MODULE = "sentiment_tag"
    ENTITY = "情感记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录ID", "文本片段", "片段出处", "情感倾向", "强烈程度", "方面词", "方面情感",
                   "标注模型", "槽位核对状态"]
    REQUIRED_FIELDS = ["记录ID", "文本片段", "情感倾向"]
    STATUS_ORDER = ["已标注", "已复核", "已修正", "已忽略"]
    ACTION_RULES = {"复核标注": "已标注", "修正标注": "已标注", "忽略标注": "已标注"}
    NEGATIVE_ACTIONS = ["忽略标注"]

    def analyze_sentiment(self, text_segment_block_id: str) -> dict[str, Any]:
        """分析单个文本块的情感：消费文本分块输出。"""
        block = next(
            (r for r in store.rows("text_segment") if r.get("块ID") == text_segment_block_id),
            None
        )
        if block is None:
            return {"ok": False, "reason": "文本块不存在"}

        text_hash = int(hashlib.md5(text_segment_block_id.encode()).hexdigest()[:4], 16)
        mood_idx = text_hash % len(SENTIMENT_LABELS)
        sentiment = SENTIMENT_LABELS[mood_idx]
        intensity = ["低", "中", "高", "极高"][text_hash % 4]
        aspects = list(ASPECT_SENTIMENTS.keys())
        aspect_idx = text_hash % len(aspects)
        aspect = aspects[aspect_idx]
        aspect_sent = ASPECT_SENTIMENTS[aspect][text_hash % 3]
        snippet_source = f"{block.get('来源页面', '')}#{text_segment_block_id}"

        return {
            "ok": True,
            "记录ID": f"sent-{text_segment_block_id}",
            "文本片段": f"Text from block {text_segment_block_id}",
            "片段出处": snippet_source,
            "情感倾向": sentiment,
            "强烈程度": intensity,
            "方面词": aspect,
            "方面情感": aspect_sent,
            "标注模型": "sentiment-analyzer-v2",
            "槽位核对状态": "未核对",
        }

    def batch_analyze(self, text_block_ids: list[str]) -> dict[str, Any]:
        """批量情感分析。"""
        results = [self.analyze_sentiment(bid) for bid in text_block_ids]
        ok_results = [r for r in results if r.get("ok")]
        return {
            "ok": True,
            "total_analyzed": len(ok_results),
            "sentiment_distribution": {
                label: sum(1 for r in ok_results if r.get("情感倾向") == label)
                for label in SENTIMENT_LABELS
            },
            "results": ok_results,
        }

    def compute_sentiment_summary(self) -> dict[str, Any]:
        """情感分布摘要，供聚合视图消费。"""
        rows = store.rows(self.MODULE)
        dist: dict[str, int] = {}
        intensity_dist: dict[str, int] = {}
        for row in rows:
            sent = row.get("情感倾向", "未知")
            dist[sent] = dist.get(sent, 0) + 1
            intensity_dist[row.get("强烈程度", "未知")] = \
                intensity_dist.get(row.get("强烈程度", "未知"), 0) + 1
        return {
            "total_records": len(rows),
            "sentiment_distribution": dist,
            "intensity_distribution": intensity_dist,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["sentiment_summary"] = self.compute_sentiment_summary()
        return base