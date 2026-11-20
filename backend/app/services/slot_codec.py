"""槽位交换编解码：v1 旧 Python 字典字符串 <-> v2 带版本结构化交换数据。

这里只放纯函数，不依赖 FastAPI 和内存仓库，因此在线服务与离线迁移脚本
（backend/scripts/slot_migration.py）共用同一套转换规则，演练结果可复现。

版本约定
--------
v1: ``str(dict)`` 形式的历史槽位，例如 ``"{'order_id': 'A-1', 'date_range': '3天'}"``。
    外部读取端无法稳定解析，迁移时必须先做无损判定。
v2: ``nlu.slot-exchange`` 交换文档，槽位是带 ``slot_type`` / ``value`` /
    ``provenance`` 的结构化列表，并记录原值、转换版本与完整性校验。
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

SCHEMA = "nlu.slot-exchange"
CURRENT_VERSION = "2"
LEGACY_VERSION = "1"
CONVERTER = "slot-migration/v1-to-v2"

STATUS_ALREADY_STRUCTURED = "already_structured"
STATUS_CONVERTED = "converted"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_UNCONVERTIBLE = "unconvertible"

STATUS_LABELS = {
    STATUS_ALREADY_STRUCTURED: "已结构化",
    STATUS_CONVERTED: "已转换",
    STATUS_NEEDS_REVIEW: "待补正",
    STATUS_UNCONVERTIBLE: "不可转换",
}

# 只有这两种状态才允许被语义入库直接接纳。
INGESTIBLE_STATUSES = frozenset({STATUS_ALREADY_STRUCTURED, STATUS_CONVERTED})
PENDING_STATUSES = frozenset({STATUS_NEEDS_REVIEW, STATUS_UNCONVERTIBLE})


class SlotCodecError(ValueError):
    """槽位载荷无法进入交换格式。"""

    def __init__(self, reason: str, detail: str = "", payload: Any = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail
        self.payload = payload

    def as_dict(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail}


class UnconvertibleSlotPayload(SlotCodecError):
    """连语法层面都无法还原，只能回到情感标注补正。"""


class LossySlotPayload(SlotCodecError):
    """语法可还原，但转成 JSON 交换格式会改变类型或丢失信息。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_safe(value: Any, path: str) -> Any:
    """只接受可无损写入 JSON 的值；元组/集合/字节/NaN 都判为有损。"""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LossySlotPayload(
                "non_finite_number",
                f"槽位 {path} 的值 {value!r} 不能无损写入 JSON 交换格式",
            )
        return value
    if isinstance(value, list):
        return [_json_safe(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise LossySlotPayload(
                    "non_string_nested_key",
                    f"槽位 {path} 的嵌套键 {key!r} 不是字符串，JSON 交换会改变类型",
                )
            result[key] = _json_safe(item, f"{path}.{key}")
        return result
    raise LossySlotPayload(
        "non_json_slot_value",
        f"槽位 {path} 的值类型 {type(value).__name__} 不能无损写入 JSON 交换格式",
    )


def _primitive_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def parse_legacy_payload(raw: Any) -> dict[str, Any]:
    """把 v1 的 Python 字典字符串解析成可交换的字典。

    解析失败抛 ``UnconvertibleSlotPayload``；解析成功但含不可无损表达的值
    抛 ``LossySlotPayload``。两类失败都不会丢弃原值。
    """
    if not isinstance(raw, str):
        raise UnconvertibleSlotPayload(
            "legacy_payload_not_string",
            f"v1 槽位应为字符串，实际是 {type(raw).__name__}",
            raw,
        )
    text = raw.strip()
    if not text:
        raise UnconvertibleSlotPayload("legacy_payload_empty", "v1 槽位字符串为空", raw)
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError) as exc:
        raise UnconvertibleSlotPayload(
            "legacy_repr_not_literal",
            f"无法用 ast.literal_eval 还原：{type(exc).__name__}: {exc}",
            raw,
        ) from exc
    if not isinstance(value, dict):
        raise UnconvertibleSlotPayload(
            "legacy_payload_not_dict",
            f"v1 槽位还原后是 {type(value).__name__}，不是字典",
            raw,
        )
    for key in value:
        if not isinstance(key, str):
            raise LossySlotPayload(
                "legacy_non_string_key",
                f"槽位键 {key!r} 不是字符串，JSON 交换会改变类型",
                raw,
            )
    return {key: _json_safe(item, key) for key, item in value.items()}


def _decode_json_payload(text: str) -> tuple[bool, Any]:
    try:
        return True, json.loads(text)
    except (ValueError, TypeError):
        return False, None


def detect_payload_version(payload: Any) -> str:
    """识别槽位载荷的版本；未知格式交给调用方记为不可转换。

    v2 交换文档允许以 JSON 字符串形式落在列里（与 v1 的 Python repr 区分：
    前者能被 json 解析，后者只有单引号）。
    """
    if isinstance(payload, dict):
        return "2"
    if isinstance(payload, list):
        if all(isinstance(item, dict) for item in payload):
            return "2"
        raise UnconvertibleSlotPayload(
            "structured_slot_not_object",
            "结构化槽位列表里存在非对象元素",
            payload,
        )
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith("{"):
            decoded_ok, decoded = _decode_json_payload(stripped)
            if decoded_ok and isinstance(decoded, (dict, list)):
                return "2"
            return LEGACY_VERSION
        raise UnconvertibleSlotPayload(
            "unknown_slot_format",
            "槽位字符串既不是 Python 字典表示，也不是 v2 交换文档",
            payload,
        )
    raise UnconvertibleSlotPayload(
        "unknown_slot_format",
        f"无法识别的槽位载荷类型 {type(payload).__name__}",
        payload,
    )


def _normalize_slot_item(item: dict[str, Any], index: int) -> dict[str, Any]:
    slot_type = item.get("slot_type", item.get("槽位类型", item.get("type")))
    if slot_type is None:
        raise UnconvertibleSlotPayload(
            "slot_type_missing",
            f"第 {index + 1} 个结构化槽位缺少 slot_type",
            item,
        )
    if not isinstance(slot_type, str) or not slot_type.strip():
        raise LossySlotPayload(
            "slot_type_invalid",
            f"第 {index + 1} 个结构化槽位的类型不是非空字符串",
            item,
        )
    if "value" in item:
        value = item["value"]
    elif "槽位值" in item:
        value = item["槽位值"]
    else:
        raise UnconvertibleSlotPayload(
            "slot_value_missing",
            f"结构化槽位 {slot_type} 缺少 value",
            item,
        )
    value = _json_safe(value, slot_type)
    provenance = item.get("provenance")
    slot: dict[str, Any] = {
        "slot_type": slot_type.strip(),
        "value": value,
        "value_type": _primitive_type_name(value),
    }
    if isinstance(provenance, dict):
        slot["provenance"] = copy.deepcopy(provenance)
    return slot


def normalize_structured_payload(payload: Any) -> list[dict[str, Any]]:
    """把 v2 的多种结构化写法归一成槽位列表。"""
    if isinstance(payload, str):
        decoded_ok, decoded = _decode_json_payload(payload.strip())
        if not decoded_ok:
            raise UnconvertibleSlotPayload(
                "structured_json_invalid",
                "v2 槽位字符串不是合法 JSON",
                payload,
            )
        payload = decoded
    if isinstance(payload, dict) and payload.get("schema") == SCHEMA:
        payload = payload.get("slots", [])
    if isinstance(payload, dict) and isinstance(payload.get("slots"), list):
        payload = payload["slots"]
    if isinstance(payload, dict):
        items = [
            {"slot_type": key, "value": _json_safe(value, key)}
            for key, value in payload.items()
        ]
        return [_normalize_slot_item(item, index) for index, item in enumerate(items)]
    if isinstance(payload, list):
        normalized = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise UnconvertibleSlotPayload(
                    "structured_slot_not_object",
                    f"第 {index + 1} 个结构化槽位不是对象",
                    item,
                )
            normalized.append(_normalize_slot_item(item, index))
        return normalized
    raise UnconvertibleSlotPayload(
        "structured_payload_not_supported",
        f"不支持的结构化载荷类型 {type(payload).__name__}",
        payload,
    )


def render_legacy_repr(slots: list[dict[str, Any]] | dict[str, Any]) -> str:
    """把结构化槽位按 v1 习惯还原成 Python 字典字符串，供旧读取端回退。"""
    if isinstance(slots, dict):
        mapping = dict(slots)
    else:
        mapping = {str(item.get("slot_type")): item.get("value") for item in slots}
    return repr(dict(sorted(mapping.items())))


def make_exchange_id(
    semantic_id: str, sentiment_record_id: str, original_version: str, original_sha: str
) -> str:
    """确定性交换ID：同一语义身份 + 同一原值重试时不会产生第二条记录。"""
    seed = "|".join([semantic_id, sentiment_record_id, original_version, original_sha])
    return f"slx-{sha256_text(seed)[:12]}"


def _integrity_checksum(doc: dict[str, Any]) -> str:
    core = {
        "schema": doc["schema"],
        "schema_version": doc["schema_version"],
        "exchange_id": doc["exchange_id"],
        "semantic_id": doc["semantic_id"],
        "nlu_record_id": doc.get("nlu_record_id"),
        "intent": doc.get("intent", ""),
        "slots": doc["slots"],
        "original": doc["original"],
        "conversion": doc["conversion"],
        "source": doc["source"],
    }
    return sha256_text(canonical_json(core))


def reseal_exchange(doc: dict[str, Any]) -> dict[str, Any]:
    """在受控字段（片段出处等）补齐后重算完整性校验。"""
    doc.setdefault("integrity", {})["algorithm"] = "sha256"
    doc["integrity"]["checksum"] = _integrity_checksum(doc)
    return doc


def verify_exchange(doc: dict[str, Any]) -> tuple[bool, str]:
    """按文档自带的完整性校验复核交换数据。"""
    integrity = doc.get("integrity") or {}
    expected = integrity.get("checksum")
    if not expected:
        return False, "交换文档缺少完整性校验"
    actual = _integrity_checksum(doc)
    if actual != expected:
        return False, "交换文档完整性校验不一致，可能被外部修改"
    return True, ""


def build_slot_exchange(
    *,
    semantic_id: str,
    payload: Any,
    nlu_record_id: int | None = None,
    intent: str = "",
    source_sentiment_id: str = "",
    snippet: str = "",
    input_text: str = "",
    verified: bool = False,
    batch_id: str | None = None,
    origin: str = "migration",
    notes: list[str] | None = None,
    payload_version: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """构建 v2 交换文档；失败也返回文档，保留原值与语义身份供回滚。"""
    now = now or utc_now()
    notes = list(notes or [])
    version = payload_version
    version_error: SlotCodecError | None = None
    if version is None:
        try:
            version = detect_payload_version(payload)
        except SlotCodecError as exc:
            version = "unknown"
            version_error = exc
    original_sha = sha256_text(
        payload if isinstance(payload, str) else canonical_json(payload)
    )
    exchange_id = make_exchange_id(
        semantic_id, source_sentiment_id, version, original_sha
    )
    if version == LEGACY_VERSION:
        original_format = "python-dict-repr"
    elif version == "unknown":
        original_format = "unrecognized"
    else:
        original_format = "structured-slots"

    slots: list[dict[str, Any]] = []
    status = STATUS_UNCONVERTIBLE
    lossless = False
    error: dict[str, str] | None = None
    try:
        if version_error is not None:
            raise version_error
        if version == LEGACY_VERSION:
            mapping = parse_legacy_payload(payload)
            slot_items = [
                {"slot_type": key, "value": value} for key, value in mapping.items()
            ]
            status = STATUS_CONVERTED
        else:
            slot_items = normalize_structured_payload(payload)
            status = STATUS_ALREADY_STRUCTURED
        lossless = True
    except SlotCodecError as exc:
        slot_items = []
        error = exc.as_dict()
        status = (
            STATUS_NEEDS_REVIEW
            if isinstance(exc, LossySlotPayload)
            else STATUS_UNCONVERTIBLE
        )
        notes.append(exc.detail)

    if lossless and not verified:
        status = STATUS_NEEDS_REVIEW
        error = {
            "reason": "source_not_verified",
            "detail": "来源情感记录的片段槽位尚未核对，转换结果保留在待补正范围",
        }
        notes.append(error["detail"])

    provenance_kind = "sentiment_snippet" if source_sentiment_id else "nlu_input"
    provenance_base = {
        "type": provenance_kind,
        "sentiment_record_id": source_sentiment_id,
        "snippet": snippet or input_text,
        "snippet_sha256": sha256_text(snippet or input_text),
    }
    for index, item in enumerate(slot_items):
        slot = {
            "slot_id": f"{exchange_id}:{index}",
            "slot_type": item["slot_type"],
            "value": item["value"],
            "value_type": item.get("value_type", _primitive_type_name(item["value"])),
            "provenance": copy.deepcopy(item.get("provenance") or provenance_base),
        }
        slots.append(slot)

    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": CURRENT_VERSION,
        "exchange_id": exchange_id,
        "semantic_id": semantic_id,
        "nlu_record_id": nlu_record_id,
        "intent": intent,
        "slots": slots,
        "source": {
            "type": provenance_kind,
            "sentiment_record_id": source_sentiment_id,
            "snippet": snippet,
            "snippet_sha256": sha256_text(snippet),
            "input_text": input_text,
        },
        "original": {
            "format": original_format,
            "version": version,
            "raw": copy.deepcopy(payload),
            "sha256": original_sha,
        },
        "conversion": {
            "from_version": version,
            "version": CURRENT_VERSION,
            "converter": CONVERTER,
            "status": status,
            "status_label": STATUS_LABELS[status],
            "lossless": lossless,
            "verified": bool(verified) and lossless,
            "needs_correction": status in PENDING_STATUSES,
            "error": error,
            "notes": notes,
        },
        "migration": {
            "batch_id": batch_id,
            "origin": origin,
            "migrated_at": now,
        },
        "rollback": {
            "semantic_id": semantic_id,
            "original_version": version,
            "original": copy.deepcopy(payload),
            "original_sha256": original_sha,
            "restorable": True,
            "rolled_back_at": None,
        },
        "acceptance": [],
        "created_at": now,
        "updated_at": now,
    }
    doc["integrity"] = {
        "algorithm": "sha256",
        "checksum": _integrity_checksum(doc),
    }
    return doc


def source_payload_for_rollback(doc: dict[str, Any]) -> tuple[str, Any]:
    """返回回滚所需（原格式版本, 原值）。"""
    original = doc.get("original") or {}
    return str(original.get("version", LEGACY_VERSION)), copy.deepcopy(
        original.get("raw")
    )
