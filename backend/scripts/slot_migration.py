#!/usr/bin/env python3
"""离线槽位迁移流水线：演练、执行、入库、回滚与交换数据核对。

不依赖 FastAPI，也不需要起服务；数据来自仓库内存种子行（或 --state 读入的
JSON），因此可以在部署新版读取端之前完整演练一次切换。

用法
----
  python -I backend/scripts/slot_migration.py status
  python -I backend/scripts/slot_migration.py snapshot --out /tmp/mig.json
  python -I backend/scripts/slot_migration.py rehearse --out /tmp/rehearsal.json
  python -I backend/scripts/slot_migration.py migrate --limit 2 --out /tmp/mig.json
  python -I backend/scripts/slot_migration.py ingest --run-id dry-run-1 --state /tmp/mig.json
  python -I backend/scripts/slot_migration.py rollback --state /tmp/mig.json

每次运行都会打印计划执行的动作和核对结论，退出码非 0 表示流水线被拒绝。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.seed import SEED_ROWS  # noqa: E402
from app.services import slot_codec  # noqa: E402
from app.services.slot_migration import SlotMigrationPipeline  # noqa: E402


def build_pipeline() -> SlotMigrationPipeline:
    # 深拷贝种子行，保证离线演练不改动在线内存仓库。
    nlu_rows = [dict(row) for row in SEED_ROWS.get("nlu_result", [])]
    sentiment_rows = [dict(row) for row in SEED_ROWS.get("sentiment_tag", [])]
    for row in sentiment_rows:
        if isinstance(row.get("结构化槽位"), str) and row["结构化槽位"]:
            try:
                row["结构化槽位"] = json.loads(row["结构化槽位"])
            except json.JSONDecodeError:
                pass
    return SlotMigrationPipeline(nlu_rows, sentiment_rows)


def load_pipeline(state_path: str | None) -> SlotMigrationPipeline:
    pipeline = build_pipeline()
    if state_path:
        raw = json.loads(Path(state_path).read_text(encoding="utf-8"))
        # 命令输出有的把流水线状态放在顶层，有的包在 state 里，两种都能续跑。
        state = raw.get("state") if isinstance(raw.get("state"), dict) else raw
        pipeline.load_state(state)
    return pipeline


def dump(payload: dict, out_path: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
        print(f"已写入 {out_path}")
    else:
        print(text)


def dump_state(pipeline: SlotMigrationPipeline, summary: dict, out_path: str | None) -> None:
    """所有命令都带上可续跑的完整状态，保证暂停/恢复/回滚可以分进程演练。"""
    dump({**summary, "state": pipeline.to_state()}, out_path)


def cmd_status(args: argparse.Namespace) -> int:
    pipeline = load_pipeline(args.state)
    state = pipeline.state()
    state["exchanges"] = pipeline.list_exchanges()
    dump_state(pipeline, {"status": state}, args.out)
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    pipeline = load_pipeline(args.state)
    pipeline.begin(batch_id=args.batch_id, name=args.name or "")
    pipeline.migrate(limit=args.limit)
    dump_state(
        pipeline,
        {"batch": pipeline.batch, "manifest": pipeline.manifest()},
        args.out,
    )
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    pipeline = load_pipeline(args.state)
    if pipeline.batch["status"] == "未开始":
        pipeline.begin(batch_id=args.batch_id, name=args.name or "")
    result = pipeline.migrate(limit=args.limit)
    dump_state(
        pipeline,
        {"batch": result["batch"], "migrated": result["migrated"],
         "remaining": result["remaining"], "manifest": pipeline.manifest()},
        args.out,
    )
    return 0


def cmd_action(args: argparse.Namespace) -> int:
    pipeline = load_pipeline(args.state)
    action = args.action
    if action == "start":
        pipeline.begin(batch_id=args.batch_id, name=args.name or "")
    elif action == "pause":
        pipeline.pause()
    elif action == "resume":
        pipeline.resume()
    elif action == "rollback":
        pipeline.rollback()
    else:
        print(json.dumps({"ok": False, "reason": f"未知动作 {action}"}, ensure_ascii=False))
        return 2
    dump_state(pipeline, {"batch": pipeline.state()["batch"], "state_summary": pipeline.state()}, args.out)
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    pipeline = load_pipeline(args.state)
    run = pipeline.ingest(run_id=args.run_id)
    report = pipeline.acceptance_report()
    accepted = [
        {
            "semantic_id": item["semantic_id"],
            "exchange_id": item["exchange_id"],
            "input_version": item["input_version"],
            "slot_types": [slot["slot_type"] for slot in item["slots"]],
            "source_sentiment_id": item["source_sentiment_id"],
            "snippet_sha256": item["snippet_sha256"],
        }
        for item in run["accepted"]
    ]
    dump_state(
        pipeline,
        {
            "run_id": run["run_id"],
            "accepted": accepted,
            "accepted_count": run["accepted_count"],
            "skipped": run["skipped"],
            "needs_correction": run["needs_correction"],
            "active_by_input_version": report["active_by_input_version"],
        },
        args.out,
    )
    return 0


def cmd_legacy(args: argparse.Namespace) -> int:
    pipeline = load_pipeline(args.state)
    if args.exchange_id:
        payload = pipeline.exchange_to_legacy(args.exchange_id)
    else:
        doc = pipeline.exchange_for_semantic(args.semantic_id)
        if doc is None:
            payload = {"ok": False, "reason": "没有该语义身份的交换记录"}
        else:
            payload = pipeline.exchange_to_legacy(doc["exchange_id"])
    dump_state(pipeline, {"legacy_readback": payload}, args.out)
    return 0 if payload.get("ok") else 1


def cmd_rehearse(args: argparse.Namespace) -> int:
    """完整演练：迁移 -> 暂停 -> 恢复 -> 入库 -> 重试 -> 回滚，全程离线。"""
    pipeline = build_pipeline()
    events: list[dict] = []

    pipeline.begin(batch_id=args.batch_id or "rehearsal-1", name="离线演练")
    first = pipeline.migrate(limit=3)
    events.append({"step": "migrate-partial", "migrated": len(first["migrated"]),
                   "remaining": first["remaining"]})
    pipeline.pause()
    events.append({"step": "pause", "batch_status": pipeline.batch["status"]})
    pipeline.resume()
    second = pipeline.migrate()
    events.append({"step": "migrate-rest", "migrated": len(second["migrated"]),
                   "remaining": second["remaining"]})

    run_one = pipeline.ingest(run_id="rehearsal-ingest-1")
    events.append({
        "step": "ingest",
        "accepted": [item["semantic_id"] for item in run_one["accepted"]],
        "skipped": [item["semantic_id"] for item in run_one["skipped"]],
        "needs_correction": [
            item["semantic_id"] for item in run_one["needs_correction"]
        ],
    })
    run_retry = pipeline.ingest(run_id="rehearsal-ingest-1")
    events.append({
        "step": "ingest-retry-same-run-id",
        "same_receipt": run_retry is run_one,
        "accepted_count": run_retry["accepted_count"],
    })
    run_again = pipeline.ingest(run_id="rehearsal-ingest-2")
    events.append({
        "step": "ingest-second-run",
        "accepted": [item["semantic_id"] for item in run_again["accepted"]],
        # 第二次入库必须拦住上一轮已接纳的交换记录，不能重复计数。
        "duplicate_blocked": [
            item["semantic_id"]
            for item in run_again["skipped"]
            if item.get("reason") == "already_accepted"
        ],
        "accepted_count": run_again["accepted_count"],
    })

    legacy_readback = []
    for doc in pipeline.list_exchanges():
        payload = pipeline.exchange_to_legacy(doc["exchange_id"])
        legacy_readback.append({
            "exchange_id": doc["exchange_id"],
            "semantic_id": doc["semantic_id"],
            "original_version": payload["original_version"],
            "verified": payload["verified"],
            "lossless": payload["lossless"],
            "matches_original": payload["original"] == doc["original"]["raw"],
        })

    integrity = [
        {"exchange_id": doc["exchange_id"], **dict(zip(("ok", "message"),
                                                       slot_codec.verify_exchange(doc)))}
        for doc in pipeline.list_exchanges()
    ]

    before_rollback = pipeline.state()
    pipeline.rollback()
    after_rollback = pipeline.state()
    events.append({
        "step": "rollback",
        "batch_status": pipeline.batch["status"],
        "revoked": pipeline.batch.get("revoked_acceptances", 0),
        "acceptances_before": before_rollback["acceptance_count"],
        "acceptances_after": after_rollback["acceptance_count"],
    })

    report = {
        "ok": True,
        "batch": pipeline.batch,
        "events": events,
        "manifest": pipeline.manifest(),
        "legacy_readback": legacy_readback,
        "integrity": integrity,
        "state_before_rollback": before_rollback,
        "state_after_rollback": after_rollback,
        "rows_after_rollback": pipeline.to_state()["rows"],
    }
    dump(report, args.out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="语义槽位离线迁移流水线")
    parser.add_argument("--out", help="把结果写入 JSON 文件，便于与在线接口比对")
    parser.add_argument("--state", help="从上一次导出的流水线状态继续")
    parser.add_argument("--batch-id", dest="batch_id", help="批次ID")
    parser.add_argument("--name", help="批次名称")
    parser.add_argument("--limit", type=int, help="本次最多迁移的语义记录数")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_text: str, with_limit: bool = False,
            with_action: bool = False) -> None:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--out", default=argparse.SUPPRESS)
        sp.add_argument("--state", default=argparse.SUPPRESS)
        sp.add_argument("--batch-id", dest="batch_id", default=argparse.SUPPRESS)
        sp.add_argument("--name", default=argparse.SUPPRESS)
        if with_limit:
            sp.add_argument("--limit", type=int, default=argparse.SUPPRESS)
        if with_action:
            sp.add_argument("action", choices=["start", "pause", "resume", "rollback"])
        sp.set_defaults(func=func)

    add("status", cmd_status, "查看批次状态与交换数据摘要")
    add("snapshot", cmd_snapshot, "开始批次并迁移一次，导出可续跑状态", with_limit=True)
    add("migrate", cmd_migrate, "继续迁移（可限制条数）", with_limit=True)
    add("action", cmd_action, "暂停/恢复/开始/回滚", with_action=True)
    add("legacy", cmd_legacy, "按旧版本读取已核对原值")

    legacy = sub.choices["legacy"]
    legacy.add_argument("--exchange-id", dest="exchange_id")
    legacy.add_argument("--semantic-id", dest="semantic_id")

    ingest = sub.add_parser("ingest", help="执行一次语义入库并输出回执")
    ingest.add_argument("--run-id", dest="run_id")
    ingest.add_argument("--out", default=argparse.SUPPRESS)
    ingest.add_argument("--state", default=argparse.SUPPRESS)
    ingest.add_argument("--batch-id", dest="batch_id", default=argparse.SUPPRESS)
    ingest.add_argument("--name", default=argparse.SUPPRESS)
    ingest.set_defaults(func=cmd_ingest)

    rehearse = sub.add_parser("rehearse", help="离线完整演练并输出核对报告")
    rehearse.add_argument("--out", default=argparse.SUPPRESS)
    rehearse.add_argument("--state", default=argparse.SUPPRESS)
    rehearse.add_argument("--batch-id", dest="batch_id", default=argparse.SUPPRESS)
    rehearse.add_argument("--name", default=argparse.SUPPRESS)
    rehearse.set_defaults(func=cmd_rehearse)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - CLI 需要把拒绝原因原样打印
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
