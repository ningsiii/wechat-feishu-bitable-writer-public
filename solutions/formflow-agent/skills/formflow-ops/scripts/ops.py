#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_BINDINGS_PATH = Path(os.environ.get("SMALLBIZ_BINDINGS_FILE") or "solutions/formflow-agent/config/bindings.json")
DEFAULT_DRAFT_PATH = Path(os.environ.get("SMALLBIZ_DRAFT_FILE") or "solutions/formflow-agent/data/draft.json")
DEFAULT_LEDGER_PATH = Path(os.environ.get("SMALLBIZ_LEDGER_PATH") or "solutions/formflow-agent/data/ledger.jsonl")
DEFAULT_LOG_DIR = Path("/tmp/openclaw")
DEFAULT_EXPORT_DIR = Path(os.environ.get("SMALLBIZ_EXPORT_DIR") or "solutions/formflow-agent/exports")
_OPS_EVENT_PREFIX = "ops_"


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def tail_jsonl(path: Path, limit: int = 20) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if isinstance(item, dict):
            out.append(item)
        if len(out) >= limit:
            break
    return out


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _event_is_user_visible(ev: Dict[str, Any]) -> bool:
    t = str(ev.get("type") or "").strip()
    return bool(t) and not t.startswith(_OPS_EVENT_PREFIX)


def _event_label(ev_type: str) -> str:
    return {
        "pending_written": "录入待确认",
        "pending_write_failed": "写入失败",
        "pending_confirmed_one": "确认一条",
        "pending_confirmed_all": "确认全部",
        "pending_void_latest": "作废最新一条",
        "route_decided": "选表决策",
        "table_add": "登记表",
        "table_removed": "删除表",
        "table_replace_reply": "表替换确认",
    }.get(ev_type, ev_type or "未知事件")


def _format_event_line(index: int, ev: Dict[str, Any]) -> str:
    at = str(ev.get("event_at") or "").strip()
    ev_type = str(ev.get("type") or "").strip()
    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
    table = str(payload.get("table") or "").strip()
    record_id = str(payload.get("record_id") or "").strip()
    count = payload.get("count")
    err = humanize_error(str(payload.get("error") or payload.get("sync_error") or ""))

    pieces = [f"{index}) {at}", _event_label(ev_type)]
    if table:
        pieces.append(f"表：{table}")
    if ev_type == "pending_confirmed_all" and count not in (None, ""):
        pieces.append(f"本次 {int(count)} 条")
    if ev_type == "pending_write_failed":
        pieces.append(f"原因：{err}")
    elif record_id:
        pieces.append(f"记录ID：{record_id}")
    return " | ".join(pieces)


def humanize_error(raw: str) -> str:
    s = (raw or "").strip()
    low = s.lower()
    if "91403" in s or "forbidden" in low or "权限" in s:
        return "飞书应用没有该表的权限"
    if "timeout" in low or "timed out" in low or "超时" in s:
        return "网络超时或服务暂时不可用"
    if "table" in low and "missing" in low:
        return "表格链接或参数不完整"
    if not s:
        return "未知错误"
    line = s.splitlines()[0].strip()
    return line[:80] + ("…" if len(line) > 80 else "")


def current_table_info(bindings_path: Path) -> Dict[str, str]:
    data = load_json(bindings_path)
    active = str(data.get("active_binding") or "").strip()
    bindings = data.get("bindings") if isinstance(data.get("bindings"), list) else []
    for b in bindings:
        if not isinstance(b, dict):
            continue
        if str(b.get("name") or "").strip() == active:
            return {
                "name": str(b.get("display_name") or b.get("name") or "").strip() or "当前表",
                "open_url": str(b.get("open_url") or "").strip(),
                "active": active,
            }
    return {"name": "", "open_url": "", "active": active}


def cmd_ledger(args: argparse.Namespace) -> Dict[str, Any]:
    ledger_path = Path(args.ledger)
    recent_all = tail_jsonl(ledger_path, limit=max(int(args.limit) * 4, 50))
    visible_events = [ev for ev in recent_all if _event_is_user_visible(ev)]
    show = list(reversed(visible_events[: int(args.limit)]))
    display_cap = min(len(show), 10)
    display_rows = show[:display_cap]
    summary_counts = {
        "pending_written": 0,
        "pending_confirmed_all": 0,
        "pending_void_latest": 0,
        "pending_write_failed": 0,
    }
    for ev in visible_events:
        ev_type = str(ev.get("type") or "").strip()
        if ev_type in summary_counts:
            summary_counts[ev_type] += 1

    lines = [
        "📒【台账】",
        f"最近录入：{summary_counts['pending_written']} 条",
        f"最近确认：{summary_counts['pending_confirmed_all']} 次",
        f"最近作废：{summary_counts['pending_void_latest']} 次",
        f"最近失败：{summary_counts['pending_write_failed']} 次",
        "",
    ]
    if not show:
        lines.append("最近记录：无")
    else:
        lines.append("最近记录：")
        for i, ev in enumerate(display_rows, start=1):
            lines.append(_format_event_line(i, ev))
        hidden = len(show) - len(display_rows)
        if hidden > 0:
            lines.append(f"……其余 {hidden} 条已省略")
    append_jsonl(ledger_path, {"type": "ops_viewed_ledger", "event_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"), "payload": {"limit": int(args.limit)}})
    return {"ok": True, "reply_text": "\n".join(lines)}


def cmd_recent_errors(args: argparse.Namespace) -> Dict[str, Any]:
    events = tail_jsonl(Path(args.ledger), limit=100)
    fails = [e for e in events if str(e.get("type") or "") == "pending_write_failed"][: int(args.limit)]
    latest_log = ""
    if DEFAULT_LOG_DIR.exists():
        files = sorted(DEFAULT_LOG_DIR.glob("openclaw-*.log"))
        if files:
            latest_log = str(files[-1])
    lines = []
    if latest_log:
        lines.append(f"网关日志：{latest_log}")
    if not fails:
        lines.append("❎【最近错误】")
        lines.append("最近错误：无")
    else:
        lines.append("❎【最近错误】")
        lines.append("最近错误：")
        for i, ev in enumerate(reversed(fails), start=1):
            at = str(ev.get("event_at") or "").strip()
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            table = str(payload.get("table") or "").strip()
            err = humanize_error(str(payload.get("error") or payload.get("sync_error") or ""))
            lines.append(f"{i}) {at} {table} {err}".strip())
    append_jsonl(Path(args.ledger), {"type": "ops_viewed_errors", "event_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"), "payload": {"limit": int(args.limit), "log": latest_log}})
    return {"ok": True, "reply_text": "\n".join(lines)}


def _parse_event_at(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s))
    except Exception:
        return None


def _filter_events_by_days(events: List[Dict[str, Any]], days: int) -> List[Dict[str, Any]]:
    if days <= 0:
        return events
    now = datetime.now().astimezone()
    start = now - timedelta(days=days)
    out: List[Dict[str, Any]] = []
    for ev in events:
        dt = _parse_event_at(str(ev.get("event_at") or ""))
        if dt is None:
            continue
        if dt >= start:
            out.append(ev)
    return out


def cmd_ledger_export(args: argparse.Namespace) -> Dict[str, Any]:
    ledger_path = Path(args.ledger)
    events = tail_jsonl(ledger_path, limit=int(args.max_events))
    rows = list(reversed(events))
    rows = [ev for ev in rows if _event_is_user_visible(ev)]
    rows = _filter_events_by_days(rows, int(args.days))

    export_dir = Path(args.out_dir)
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # Some environments (e.g., restricted mounts) may deny creating directories under the repo.
        # Fall back to /tmp so export still works.
        export_dir = Path("/tmp/formflow-agent/exports")
        export_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    out_path = export_dir / f"ledger_{ts}.csv"

    fields = ["时间", "事件", "表", "记录ID", "原始信息", "决策", "表链接", "错误"]
    key_map = {
        "时间": "event_at",
        "事件": "type",
        "表": "table",
        "记录ID": "record_id",
        "原始信息": "source_text",
        "决策": "decision",
        "表链接": "open_url",
        "错误": "error",
    }
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for ev in rows:
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            decision = str(payload.get("choice") or payload.get("decision") or "").strip()
            raw_err = str(payload.get("error") or payload.get("sync_error") or "").strip()
            row = {
                "event_at": str(ev.get("event_at") or ""),
                "type": _event_label(str(ev.get("type") or "")),
                "table": str(payload.get("table") or ""),
                "record_id": str(payload.get("record_id") or ""),
                "open_url": str(payload.get("open_url") or ""),
                "error": humanize_error(raw_err) if raw_err else "",
                "source_text": str(payload.get("source_text") or ""),
                "decision": decision,
            }
            w.writerow({k: row.get(v, "") for k, v in key_map.items()})
    append_jsonl(
        ledger_path,
        {
            "type": "ops_ledger_exported",
            "event_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "payload": {"days": int(args.days), "rows": len(rows), "file": str(out_path)},
        },
    )

    lines = [
        "【台账导出】",
        "结果：导出完成",
        f"文件：{out_path}",
            ]
    if int(args.days) > 0:
        tail = f"条数：{len(rows)} | 范围：最近 {int(args.days)} 天"
    else:
        tail = f"条数：{len(rows)} | 范围：全部"
    lines.append(tail)

    # Some clients collapse a single newline; keep the format simple and line-based.
    return {"ok": True, "reply_text": "\n\n".join(lines), "rows": len(rows)}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ops.py")
    p.add_argument("--bindings", default=str(DEFAULT_BINDINGS_PATH))
    p.add_argument("--draft", default=str(DEFAULT_DRAFT_PATH))
    p.add_argument("--ledger", default=str(DEFAULT_LEDGER_PATH))
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ledger = sub.add_parser("ledger")
    p_ledger.add_argument("--limit", type=int, default=20)

    p_errors = sub.add_parser("recent_errors")
    p_errors.add_argument("--limit", type=int, default=3)

    p_export = sub.add_parser("ledger_export")
    p_export.add_argument("--days", type=int, default=7)
    p_export.add_argument("--max-events", type=int, default=5000)
    p_export.add_argument("--out-dir", default=str(DEFAULT_EXPORT_DIR))

    return p


def main(argv: List[str]) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd == "ledger":
        out = cmd_ledger(args)
    elif args.cmd == "recent_errors":
        out = cmd_recent_errors(args)
    elif args.cmd == "ledger_export":
        out = cmd_ledger_export(args)
    else:
        raise SystemExit(f"Unknown cmd: {args.cmd}")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(__import__("sys").argv[1:])
