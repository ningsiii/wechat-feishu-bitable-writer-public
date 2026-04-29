#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_SIDECAR_PATH = Path(
    os.environ.get("SMALLBIZ_SIDECAR_PATH") or "solutions/formflow-agent/data/structured_sidecar.jsonl"
)
DEFAULT_BINDINGS_PATH = Path(os.environ.get("SMALLBIZ_BINDINGS_FILE") or "solutions/formflow-agent/config/bindings.json")
DEFAULT_ENV_FILE = Path(os.environ.get("SMALLBIZ_ENV_FILE") or "solutions/formflow-agent/.env")
SYNC_RECORD_PATH = Path("solutions/formflow-agent/skills/formflow-feishu/scripts/sync_record.py")
INTAKE_PATH = Path("solutions/formflow-agent/skills/formflow-intake/scripts/intake.py")
DEFAULT_MODEL_BASE_URL = os.environ.get("SMALLBIZ_MODEL_BASE_URL") or "https://api.deepseek.com"
DEFAULT_MODEL = os.environ.get("SMALLBIZ_MODEL") or "deepseek-chat"

_CN_NUM_MAP = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT_MAP = {"十": 10, "百": 100, "千": 1000, "万": 10000}

_RE_DATE_FULL = re.compile(r"(?P<y>20\d{2})[-/年](?P<m>\d{1,2})[-/月](?P<d>\d{1,2})日?")
_RE_DATE_MD = re.compile(r"(?P<m>\d{1,2})[-/月](?P<d>\d{1,2})日?")
_RE_WEEKDAY = re.compile(r"周([一二三四五六日天])")
_RE_EXPLICIT_ITEM = re.compile(
    r"(?P<name>[\u4e00-\u9fffA-Za-z]+?)(?P<qty>\d+(?:\.\d+)?|[零一二两三四五六七八九十百千万半]+)\s*(?P<unit>斤|盒|袋|个|件|份|只|箱|包|桶|瓶|支|把|根|串|盘|kg|KG|千克|克)?"
)
SOURCE_TEXT_FIELD_ALIASES = ["内容", "文本", "商品/服务", "商品", "品名", "产品", "服务", "项目", "订单内容"]
SOURCE_TIME_FIELD_ALIASES = ["时间", "送达时间", "配送时间", "预约时间", "到货时间", "预定日期", "日期", "送货时间", "取货时间"]
SOURCE_NOTE_FIELD_ALIASES = ["备注", "说明", "留言", "要求", "口味", "备注信息"]
CREATED_AT_ALIASES = ["创建时间", "created_at"]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def source_hash(text: str) -> str:
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _load_sync_module():
    if not SYNC_RECORD_PATH.exists():
        raise SystemExit(f"Missing sync_record.py: {SYNC_RECORD_PATH}")
    return _load_module(SYNC_RECORD_PATH, "smallbiz_sync_record_for_sidecar")


def _load_bindings(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"active_binding": "", "bindings": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"active_binding": "", "bindings": []}
    if not isinstance(data, dict):
        return {"active_binding": "", "bindings": []}
    if not isinstance(data.get("bindings"), list):
        data["bindings"] = []
    return data


def _resolve_binding(data: Dict[str, Any], name: str = "") -> Dict[str, Any]:
    bindings = data.get("bindings") if isinstance(data.get("bindings"), list) else []
    target = name or str(data.get("active_binding") or "").strip()
    if not target:
        raise SystemExit("Missing binding name and no active binding configured.")
    for b in bindings:
        if isinstance(b, dict) and str(b.get("name") or "").strip() == target:
            return b
    raise SystemExit(f"Binding not found: {target}")


def _cn_int(text: str) -> Optional[int]:
    s = (text or "").strip()
    if not s:
        return None
    if s == "十":
        return 10
    total = 0
    section = 0
    number = 0
    seen = False
    for ch in s:
        if ch in _CN_NUM_MAP:
            number = _CN_NUM_MAP[ch]
            seen = True
        elif ch in _CN_UNIT_MAP:
            unit = _CN_UNIT_MAP[ch]
            seen = True
            if unit == 10000:
                section = (section + (number or 0)) * unit
                total += section
                section = 0
                number = 0
            else:
                if number == 0:
                    number = 1
                section += number * unit
                number = 0
        else:
            return None
    if not seen:
        return None
    return total + section + number


def _parse_qty_token(token: str) -> Optional[float]:
    s = (token or "").strip()
    if not s:
        return None
    if s == "半":
        return 0.5
    try:
        return float(s)
    except Exception:
        pass
    n = _cn_int(s)
    if n is not None:
        return float(n)
    return None


def _normalize_items(text: str) -> List[Dict[str, Any]]:
    s = (text or "").strip()
    if not s:
        return []
    s = re.sub(r"[，,；;。]", " ", s)
    parts = [p.strip() for p in re.split(r"[、\n]|(?<=\S)\s{1,}(?=[\u4e00-\u9fffA-Za-z])", s) if p.strip()]
    out: List[Dict[str, Any]] = []
    for part in parts:
        m = _RE_EXPLICIT_ITEM.search(part)
        if not m:
            continue
        qty = _parse_qty_token(m.group("qty") or "")
        if qty is None:
            continue
        name = str(m.group("name") or "").strip()
        unit = str(m.group("unit") or "").strip()
        if not name:
            continue
        out.append({"name": name, "qty": qty, "unit": unit})
    return out


def _llm_parse_items(text: str, timeout_s: int = 20) -> List[Dict[str, Any]]:
    # Fallback path only, keep cost bounded.
    if not text.strip():
        return []
    if not INTAKE_PATH.exists():
        return []
    try:
        intake = _load_module(INTAKE_PATH, "smallbiz_intake_for_sidecar")
    except Exception:
        return []
    try:
        parsed = intake.call_deepseek_parse_items(
            text=text,
            base_url=DEFAULT_MODEL_BASE_URL,
            model=DEFAULT_MODEL,
            timeout_s=timeout_s,
        )
    except Exception:
        return []
    items = parsed.get("items") if isinstance(parsed, dict) else []
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        try:
            qty = float(it.get("qty"))
        except Exception:
            continue
        unit = str(it.get("unit") or "").strip()
        out.append({"name": name, "qty": qty, "unit": unit})
    return out


def _weekday_delta(target: str, base: date) -> Optional[int]:
    mapping = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    if target not in mapping:
        return None
    want = mapping[target]
    cur = base.weekday()
    delta = (want - cur) % 7
    return 7 if delta == 0 else delta


def _parse_event_date(text: str, created_at: str) -> Dict[str, Any]:
    base_dt: datetime
    try:
        base_dt = datetime.fromisoformat(created_at)
    except Exception:
        base_dt = datetime.now().astimezone()
    base = base_dt.date()
    s = (text or "").strip()
    if not s:
        return {"value": "", "source": ""}
    m = _RE_DATE_FULL.search(s)
    if m:
        d = date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
        return {"value": d.isoformat(), "source": "text_absolute"}
    m = _RE_DATE_MD.search(s)
    if m:
        d = date(base.year, int(m.group("m")), int(m.group("d")))
        return {"value": d.isoformat(), "source": "text_absolute"}
    if "后天" in s:
        return {"value": (base + timedelta(days=2)).isoformat(), "source": "text_relative"}
    if "明天" in s:
        return {"value": (base + timedelta(days=1)).isoformat(), "source": "text_relative"}
    if "今天" in s or "今日" in s:
        return {"value": base.isoformat(), "source": "text_relative"}
    m = _RE_WEEKDAY.search(s)
    if m:
        delta = _weekday_delta(m.group(1), base)
        if delta is not None:
            return {"value": (base + timedelta(days=delta)).isoformat(), "source": "text_relative"}
    return {"value": base.isoformat(), "source": "record_created_at"}


def _find_first_field(fields: Dict[str, Any], aliases: List[str]) -> str:
    actual = [str(k).strip() for k in fields.keys() if str(k).strip()]
    actual_set = set(actual)
    for alias in aliases:
        if alias in actual_set:
            return alias
    for alias in aliases:
        for name in actual:
            if alias in name:
                return name
    return ""


def _record_created_at(fields: Dict[str, Any], record: Dict[str, Any]) -> str:
    for k in CREATED_AT_ALIASES:
        v = str(fields.get(k) or "").strip()
        if v:
            return v
    ct = record.get("created_time")
    if isinstance(ct, (int, float)):
        try:
            return datetime.fromtimestamp(float(ct) / 1000.0).astimezone().isoformat(timespec="seconds")
        except Exception:
            pass
    return now_iso()


def _compose_source_text(fields: Dict[str, Any]) -> str:
    parts: List[str] = []
    for aliases in (SOURCE_TEXT_FIELD_ALIASES, SOURCE_TIME_FIELD_ALIASES, SOURCE_NOTE_FIELD_ALIASES):
        name = _find_first_field(fields, aliases)
        if not name:
            continue
        value = str(fields.get(name) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return " ".join(parts).strip()


def build_entry(
    *,
    record_id: str,
    binding_name: str,
    source_text_raw: str,
    created_at: str,
    updated_at: str = "",
) -> Dict[str, Any]:
    entry_updated_at = updated_at or now_iso()
    items = _normalize_items(source_text_raw)
    if not items:
        # LLM fallback for patterns not covered by regex, e.g. "两盒草莓".
        items = _llm_parse_items(source_text_raw, timeout_s=20)
    event_time = _parse_event_date(source_text_raw, created_at)
    return {
        "record_id": record_id,
        "binding_name": binding_name,
        "source_text": source_text_raw,
        "source_hash": source_hash(source_text_raw),
        "created_at": created_at,
        "updated_at": entry_updated_at,
        "valid": True,
        "deleted_at": "",
        "parsed_event_time": event_time,
        "parsed_items": items,
    }


def upsert_entry(path: Path, entry: Dict[str, Any]) -> Dict[str, Any]:
    rows = read_jsonl(path)
    rid = str(entry.get("record_id") or "").strip()
    kept: List[Dict[str, Any]] = []
    replaced = False
    for row in rows:
        if str(row.get("record_id") or "").strip() == rid:
            kept.append(entry)
            replaced = True
        else:
            kept.append(row)
    if not replaced:
        kept.append(entry)
    write_jsonl(path, kept)
    return {"ok": True, "op": "upsert", "record_id": rid, "replaced": replaced}


def mark_deleted(path: Path, record_id: str) -> Dict[str, Any]:
    rid = str(record_id or "").strip()
    if not rid:
        return {"ok": False, "error": "missing_record_id"}
    rows = read_jsonl(path)
    changed = False
    for row in rows:
        if str(row.get("record_id") or "").strip() == rid:
            row["valid"] = False
            row["deleted_at"] = now_iso()
            row["updated_at"] = now_iso()
            changed = True
    if changed:
        write_jsonl(path, rows)
    return {"ok": True, "op": "mark_deleted", "record_id": rid, "changed": changed}


def backfill_binding(path: Path, *, bindings_path: Path, binding_name: str, env_file: Path, timeout_s: int) -> Dict[str, Any]:
    load_dotenv(env_file)
    sync = _load_sync_module()
    bdata = _load_bindings(bindings_path)
    binding = _resolve_binding(bdata, binding_name)
    app_id = str(os.environ.get("FEISHU_APP_ID") or "").strip()
    app_secret = str(os.environ.get("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        raise SystemExit("Missing FEISHU_APP_ID or FEISHU_APP_SECRET.")
    app_token = str(binding.get("app_token") or "").strip()
    table_id = str(binding.get("table_id") or "").strip()
    if not app_token or not table_id:
        raise SystemExit("Binding must include app_token and table_id.")

    access_token = sync.get_tenant_access_token(app_id, app_secret, timeout_s=timeout_s)
    records = sync.list_bitable_records(app_token, table_id, access_token, timeout_s=timeout_s, page_size=200)

    rows = read_jsonl(path)
    existing_by_id: Dict[str, Dict[str, Any]] = {}
    kept_other: List[Dict[str, Any]] = []
    for row in rows:
        if str(row.get("binding_name") or "").strip() == binding_name:
            rid = str(row.get("record_id") or "").strip()
            if rid:
                existing_by_id[rid] = row
        else:
            kept_other.append(row)

    seen_ids: set[str] = set()
    updated_rows: List[Dict[str, Any]] = []
    inserted = 0
    refreshed = 0
    skipped = 0

    for rec in records:
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("record_id") or "").strip()
        if not rid:
            continue
        seen_ids.add(rid)
        fields = rec.get("fields") if isinstance(rec.get("fields"), dict) else {}
        source_text_raw = _compose_source_text(fields)
        if not source_text_raw:
            skipped += 1
            existing = existing_by_id.get(rid)
            if existing:
                existing["valid"] = True
                existing["deleted_at"] = ""
                existing["updated_at"] = now_iso()
                updated_rows.append(existing)
            continue
        created_at = _record_created_at(fields, rec)
        entry = build_entry(
            record_id=rid,
            binding_name=binding_name,
            source_text_raw=source_text_raw,
            created_at=created_at,
            updated_at=now_iso(),
        )
        existing = existing_by_id.get(rid)
        if existing:
            if str(existing.get("source_hash") or "") != str(entry.get("source_hash") or "") or not bool(existing.get("valid", True)):
                refreshed += 1
            else:
                # Keep timestamps stable when nothing changed.
                entry["updated_at"] = str(existing.get("updated_at") or entry["updated_at"])
            updated_rows.append(entry)
        else:
            inserted += 1
            updated_rows.append(entry)

    soft_deleted = 0
    for rid, row in existing_by_id.items():
        if rid in seen_ids:
            continue
        row["valid"] = False
        row["deleted_at"] = now_iso()
        row["updated_at"] = now_iso()
        soft_deleted += 1
        updated_rows.append(row)

    write_jsonl(path, kept_other + updated_rows)
    return {
        "ok": True,
        "op": "backfill_binding",
        "binding_name": binding_name,
        "records_seen": len(seen_ids),
        "inserted": inserted,
        "refreshed": refreshed,
        "skipped": skipped,
        "soft_deleted": soft_deleted,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sidecar.py")
    p.add_argument("--file", default=str(DEFAULT_SIDECAR_PATH))
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("upsert")
    up.add_argument("--record-id", required=True)
    up.add_argument("--binding-name", required=True)
    up.add_argument("--source-text", required=True)
    up.add_argument("--created-at", required=True)
    up.add_argument("--updated-at", default="")

    dele = sub.add_parser("mark_deleted")
    dele.add_argument("--record-id", required=True)

    bf = sub.add_parser("backfill_binding")
    bf.add_argument("--bindings", default=str(DEFAULT_BINDINGS_PATH))
    bf.add_argument("--binding-name", default="")
    bf.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    bf.add_argument("--timeout", type=int, default=20)
    return p


def main(argv: List[str]) -> None:
    args = build_parser().parse_args(argv)
    path = Path(args.file)
    if args.cmd == "upsert":
        entry = build_entry(
            record_id=str(args.record_id),
            binding_name=str(args.binding_name),
            source_text_raw=str(args.source_text),
            created_at=str(args.created_at),
            updated_at=str(args.updated_at or ""),
        )
        out = upsert_entry(path, entry)
    elif args.cmd == "mark_deleted":
        out = mark_deleted(path, str(args.record_id))
    elif args.cmd == "backfill_binding":
        out = backfill_binding(
            path,
            bindings_path=Path(args.bindings),
            binding_name=str(args.binding_name or ""),
            env_file=Path(args.env_file),
            timeout_s=int(args.timeout),
        )
    else:
        raise SystemExit(f"Unknown cmd: {args.cmd}")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
