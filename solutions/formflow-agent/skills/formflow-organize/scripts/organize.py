#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_BINDINGS_PATH = Path(os.environ.get("SMALLBIZ_BINDINGS_FILE") or "solutions/formflow-agent/config/bindings.json")
DEFAULT_ENV_FILE = Path(os.environ.get("SMALLBIZ_ENV_FILE") or "solutions/formflow-agent/.env")
DEFAULT_DRAFT_PATH = Path("solutions/formflow-agent/data/draft.json")
DEFAULT_DRAFT_SCRIPT = Path("solutions/formflow-agent/skills/formflow-draft/scripts/draft.py")
INTAKE_PATH = Path("solutions/formflow-agent/skills/formflow-intake/scripts/intake.py")
SYNC_RECORD_PATH = Path("solutions/formflow-agent/skills/formflow-feishu/scripts/sync_record.py")
DEFAULT_SIDECAR_PATH = Path(os.environ.get("SMALLBIZ_SIDECAR_PATH") or "solutions/formflow-agent/data/structured_sidecar.jsonl")
DEFAULT_SIDECAR_SCRIPT = Path("solutions/formflow-agent/skills/formflow-organize/scripts/sidecar.py")
DEFAULT_MODEL_BASE_URL = os.environ.get("SMALLBIZ_MODEL_BASE_URL") or "https://api.deepseek.com"
DEFAULT_MODEL = os.environ.get("SMALLBIZ_MODEL") or "deepseek-chat"

TIME_FIELD_ALIASES = [
    "时间", "送达时间", "配送时间", "预约时间", "到货时间", "预定日期", "日期", "送货时间", "取货时间"
]
GROUP_FIELD_ALIASES = [
    "商品/服务", "商品", "品名", "产品", "服务", "项目", "内容", "订单内容"
]
VALUE_FIELD_ALIASES = [
    "数量", "斤数", "件数", "份数", "数量（斤）", "数量(斤)", "量", "qty", "Qty", "QTY"
]
NAME_FIELD_ALIASES = [
    "姓名", "客户", "联系人", "客户名", "名字", "用户", "收件人"
]
CREATED_AT_ALIASES = ["创建时间", "created_at"]


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
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
    except Exception:
        return []
    return out


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


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _is_draft_active() -> bool:
    d = load_json(DEFAULT_DRAFT_PATH)
    return bool(d.get("active"))


def _auto_exit_draft() -> None:
    if not _is_draft_active():
        return
    if not DEFAULT_DRAFT_SCRIPT.exists():
        return
    cmd = [sys.executable, str(DEFAULT_DRAFT_SCRIPT), "exit"]
    try:
        subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy(), check=False)
    except Exception:
        return


def _load_sync_module():
    if not SYNC_RECORD_PATH.exists():
        raise SystemExit(f"Missing sync_record.py: {SYNC_RECORD_PATH}")
    return _load_module(SYNC_RECORD_PATH, "smallbiz_sync_record")


def _has_binding_sidecar(binding_name: str, path: Path) -> bool:
    if not path.exists():
        return False
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if str(row.get("binding_name") or "").strip() == binding_name and bool(row.get("valid", True)):
                return True
    except Exception:
        return False
    return False


def _ensure_binding_sidecar(binding_name: str, bindings_path: Path, env_file: Path, timeout_s: int) -> None:
    if _has_binding_sidecar(binding_name, DEFAULT_SIDECAR_PATH):
        return
    if not DEFAULT_SIDECAR_SCRIPT.exists():
        return
    cmd = [
        sys.executable,
        str(DEFAULT_SIDECAR_SCRIPT),
        "backfill_binding",
        "--bindings",
        str(bindings_path),
        "--binding-name",
        binding_name,
        "--env-file",
        str(env_file),
        "--timeout",
        str(int(timeout_s)),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy(), check=False)
    except Exception:
        return


def _load_binding_sidecar(binding_name: str, path: Path) -> List[Dict[str, Any]]:
    rows = read_jsonl(path)
    out: List[Dict[str, Any]] = []
    exact = (binding_name or "").strip()
    legacy_prefix = f"{exact} /" if exact else ""
    for row in rows:
        row_name = str(row.get("binding_name") or "").strip()
        # Compatibility:
        # - v2 canonical: store internal binding name (exact match)
        # - legacy rows may store display label like "<binding> / 数据表"
        if not row_name:
            continue
        if row_name != exact and (not legacy_prefix or not row_name.startswith(legacy_prefix)):
            continue
        if not bool(row.get("valid", True)):
            continue
        out.append(row)
    return out


def _resolve_active_binding(bindings_path: Path) -> Dict[str, Any]:
    data = load_json(bindings_path)
    active = str(data.get("active_binding") or "").strip()
    bindings = data.get("bindings") if isinstance(data.get("bindings"), list) else []
    if not active:
        raise SystemExit("当前没有已选中的表，请先发送【列表】并回复序号选择表。")
    for b in bindings:
        if isinstance(b, dict) and str(b.get("name") or "").strip() == active:
            return b
    raise SystemExit("当前表配置不存在，请重新发送【列表】选择表。")


def _binding_label(binding: Dict[str, Any]) -> str:
    return str(binding.get("display_name") or binding.get("name") or "当前表").strip() or "当前表"


def _binding_open_url(binding: Dict[str, Any]) -> str:
    return str(binding.get("open_url") or "").strip()


@dataclass
class Plan:
    mode: str
    target_date: date
    target_date_label: str
    time_basis: str
    time_field: str
    name_field: str
    group_field: str
    value_field: str
    operation_fields: List[str]


def _find_field_name(actual_names: List[str], aliases: List[str]) -> str:
    actual = [str(n).strip() for n in actual_names if str(n).strip()]
    actual_set = set(actual)
    for alias in aliases:
        if alias in actual_set:
            return alias
    # fallback contains
    for alias in aliases:
        for name in actual:
            if alias in name:
                return name
    return ""


def _load_intake_module():
    if not INTAKE_PATH.exists():
        raise SystemExit(f"Missing intake.py: {INTAKE_PATH}")
    return _load_module(INTAKE_PATH, "smallbiz_intake_for_organize")


def _resolve_target_date(*, date_expr: str, target_date_raw: str) -> Tuple[date, str]:
    today = datetime.now().astimezone().date()
    s = (target_date_raw or "").strip()
    if s:
        parsed = _parse_datetime_like(s, ref_dt=datetime.now().astimezone())
        if parsed:
            if parsed == today:
                return parsed, "今天"
            if parsed == today + timedelta(days=1):
                return parsed, "明天"
            return parsed, parsed.isoformat()
    expr = (date_expr or "").strip().lower()
    if expr == "tomorrow":
        return today + timedelta(days=1), "明天"
    if expr == "explicit":
        return today, "今天（默认）"
    return today, "今天（默认）"


def _derive_plan_via_model(text: str, actual_names: List[str], table_label: str, timeout_s: int) -> Tuple[Optional[Plan], str]:
    try:
        intake = _load_intake_module()
    except Exception:
        return None, ""
    table_ctx = {
        "table_name": table_label,
        "fields": actual_names,
        "field_hints": {
            "time_candidates": [n for n in actual_names if any(a in n for a in TIME_FIELD_ALIASES)],
            "name_candidates": [n for n in actual_names if any(a in n for a in NAME_FIELD_ALIASES)],
            "group_candidates": [n for n in actual_names if any(a in n for a in GROUP_FIELD_ALIASES)],
            "value_candidates": [n for n in actual_names if any(a in n for a in VALUE_FIELD_ALIASES)],
        },
    }
    try:
        raw = intake.call_deepseek_organize_plan(
            text=text,
            table_context=table_ctx,
            base_url=DEFAULT_MODEL_BASE_URL,
            model=DEFAULT_MODEL,
            timeout_s=timeout_s,
        )
    except Exception:
        return None, ""
    if not isinstance(raw, dict):
        return None, ""
    if bool(raw.get("need_clarify")):
        q = str(raw.get("clarify_question") or "").strip() or "你这次想按哪种方式整理？"
        return None, q
    op = str(raw.get("operation") or "").strip()
    if op not in ("group_sum", "list_who_what", "raw_summary"):
        return None, ""
    target_date, target_label = _resolve_target_date(
        date_expr=str(raw.get("date_expr") or "").strip(),
        target_date_raw=str(raw.get("target_date") or "").strip(),
    )
    fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
    time_field = str(fields.get("time_field") or "").strip()
    name_field = str(fields.get("name_field") or "").strip()
    group_field = str(fields.get("group_field") or "").strip()
    value_field = str(fields.get("value_field") or "").strip()
    for k in (time_field, name_field, group_field, value_field):
        if k and k not in actual_names:
            return None, ""
    time_basis = str(raw.get("time_basis") or "").strip()
    if time_basis not in ("business_time", "record_time", "hybrid"):
        time_basis = "hybrid"
    op_fields = raw.get("operation_fields") if isinstance(raw.get("operation_fields"), list) else []
    operation_fields = [str(x).strip() for x in op_fields if str(x).strip() in actual_names]
    return Plan(
        mode=op,
        target_date=target_date,
        target_date_label=target_label,
        time_basis=time_basis,
        time_field=time_field,
        name_field=name_field,
        group_field=group_field,
        value_field=value_field,
        operation_fields=operation_fields,
    ), ""


_RE_DATE_FULL = re.compile(r"(?P<y>20\d{2})[-/年](?P<m>\d{1,2})[-/月](?P<d>\d{1,2})日?")
_RE_DATE_MD = re.compile(r"(?P<m>\d{1,2})[-/月](?P<d>\d{1,2})日?")


def _parse_datetime_like(value: str, *, ref_dt: Optional[datetime] = None) -> Optional[date]:
    s = (value or "").strip()
    if not s:
        return None
    # ISO datetime/date
    for parser in (
        lambda x: datetime.fromisoformat(x).date(),
        lambda x: date.fromisoformat(x),
    ):
        try:
            return parser(s)
        except Exception:
            pass
    m = _RE_DATE_FULL.search(s)
    if m:
        return date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
    m = _RE_DATE_MD.search(s)
    if m:
        year = (ref_dt or datetime.now().astimezone()).year
        return date(year, int(m.group("m")), int(m.group("d")))

    # relative words are only accepted relative to the record's own create time
    base = (ref_dt or datetime.now().astimezone()).date()
    if "后天" in s:
        return base + timedelta(days=2)
    if "明天" in s:
        return base + timedelta(days=1)
    if "今天" in s or "今日" in s:
        return base
    return None


_RE_ARABIC_NUM = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[^\d\s]+)?")
_CN_NUM_MAP = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT_MAP = {"十": 10, "百": 100, "千": 1000, "万": 10000}


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


def _parse_quantity(value: Any) -> Tuple[Optional[float], str]:
    if value is None:
        return None, ""
    if isinstance(value, (int, float)):
        return float(value), ""
    s = str(value).strip()
    if not s:
        return None, ""

    m = _RE_ARABIC_NUM.search(s)
    if m:
        try:
            return float(m.group("num")), str(m.group("unit") or "").strip()
        except Exception:
            pass

    # Handle things like "一斤半"
    if s.endswith("半"):
        prefix = s[:-1].strip()
        for unit in ("斤", "份", "个", "盒", "袋", "桶", "件", "支", "包", "kg", "KG", "千克"):
            if prefix.endswith(unit):
                base = prefix[: -len(unit)].strip()
                n = _cn_int(base)
                if n is not None:
                    return float(n) + 0.5, unit
        n = _cn_int(prefix)
        if n is not None:
            return float(n) + 0.5, ""

    for unit in ("斤", "份", "个", "盒", "袋", "桶", "件", "支", "包", "kg", "KG", "千克", "克"):
        if s.endswith(unit):
            base = s[: -len(unit)].strip()
            n = _cn_int(base)
            if n is not None:
                return float(n), unit

    n = _cn_int(s)
    if n is not None:
        return float(n), ""
    if s == "半":
        return 0.5, ""
    return None, ""


def _derive_plan(text: str, actual_names: List[str]) -> Tuple[Optional[Plan], List[str], str]:
    t = (text or "").strip()
    today = datetime.now().astimezone().date()
    if "明天" in t:
        target_date = today + timedelta(days=1)
        target_label = "明天"
    elif "今天" in t or "今日" in t:
        target_date = today
        target_label = "今天"
    else:
        m = _RE_DATE_FULL.search(t) or _RE_DATE_MD.search(t)
        if m:
            parsed = _parse_datetime_like(m.group(0), ref_dt=datetime.now().astimezone())
            if parsed is None:
                return None, [], "我暂时没识别出你要查询的日期，请直接写今天、明天，或具体日期。"
            target_date = parsed
            target_label = parsed.isoformat()
        else:
            # No explicit date: default to today.
            target_date = today
            target_label = "今天（默认）"

    mode = ""
    if any(k in t for k in ("谁订", "都有谁", "给谁", "订了什么", "谁要了", "谁要")):
        mode = "list_who_what"
    elif any(k in t for k in ("备哪些货", "备货", "汇总", "合计", "总共", "准备哪些", "清单", "列表", "摘要", "整理")):
        mode = "group_sum"
    if not mode:
        # Fallback to raw summary instead of hard reject.
        mode = "raw_summary"

    time_field = _find_field_name(actual_names, TIME_FIELD_ALIASES)
    name_field = _find_field_name(actual_names, NAME_FIELD_ALIASES)
    group_field = _find_field_name(actual_names, GROUP_FIELD_ALIASES)
    value_field = _find_field_name(actual_names, VALUE_FIELD_ALIASES)
    missing: List[str] = []
    if mode == "group_sum" and (not time_field or not group_field or not value_field):
        if not time_field:
            missing.append("时间列")
        if not group_field:
            missing.append("商品列")
        if not value_field:
            missing.append("数量列")

    return Plan(
        mode=mode,
        target_date=target_date,
        target_date_label=target_label,
        time_basis="hybrid",
        time_field=time_field,
        name_field=name_field,
        group_field=group_field,
        value_field=value_field,
        operation_fields=[x for x in (time_field, name_field, group_field, value_field) if x],
    ), missing, ""


def _record_ref_dt(fields: Dict[str, Any], record: Dict[str, Any]) -> datetime:
    for k in CREATED_AT_ALIASES:
        v = str(fields.get(k) or "").strip()
        if v:
            try:
                return datetime.fromisoformat(v)
            except Exception:
                pass
    # bitable may return created_time ms
    ct = record.get("created_time")
    if isinstance(ct, (int, float)):
        try:
            return datetime.fromtimestamp(float(ct) / 1000.0).astimezone()
        except Exception:
            pass
    return datetime.now().astimezone()


def _record_target_date(plan: Plan, fields: Dict[str, Any], record: Dict[str, Any]) -> Optional[date]:
    ref_dt = _record_ref_dt(fields, record)
    if plan.time_basis == "record_time":
        return ref_dt.date()
    if plan.time_basis == "business_time":
        if not plan.time_field:
            return None
        dt_raw = str(fields.get(plan.time_field) or "").strip()
        return _parse_datetime_like(dt_raw, ref_dt=ref_dt)
    # hybrid
    if plan.time_field:
        dt_raw = str(fields.get(plan.time_field) or "").strip()
        parsed = _parse_datetime_like(dt_raw, ref_dt=ref_dt)
        if parsed is not None:
            return parsed
    return ref_dt.date()


def _sidecar_target_date(plan: Plan, row: Dict[str, Any]) -> Optional[date]:
    if plan.time_basis == "record_time":
        created = str(row.get("created_at") or "").strip()
        if created:
            try:
                return datetime.fromisoformat(created).date()
            except Exception:
                pass
        return None
    event = row.get("parsed_event_time") if isinstance(row.get("parsed_event_time"), dict) else {}
    event_raw = str(event.get("value") or "").strip()
    if plan.time_basis == "business_time":
        if not event_raw:
            return None
        return _parse_datetime_like(event_raw, ref_dt=datetime.now().astimezone())
    # hybrid
    if event_raw:
        parsed = _parse_datetime_like(event_raw, ref_dt=datetime.now().astimezone())
        if parsed is not None:
            return parsed
    created = str(row.get("created_at") or "").strip()
    if created:
        try:
            return datetime.fromisoformat(created).date()
        except Exception:
            pass
    return None


def _run_group_sum(plan: Plan, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    buckets: Dict[Tuple[str, str], float] = defaultdict(float)
    matched = 0
    skipped_qty = 0

    for rec in records:
        if not isinstance(rec, dict):
            continue
        fields = rec.get("fields") if isinstance(rec.get("fields"), dict) else {}
        dt = _record_target_date(plan, fields, rec)
        if dt != plan.target_date:
            continue
        matched += 1
        group_name = str(fields.get(plan.group_field) or "").strip()
        if not group_name:
            group_name = "未命名"
        qty, unit = _parse_quantity(fields.get(plan.value_field))
        if qty is None:
            skipped_qty += 1
            continue
        buckets[(group_name, unit)] += qty

    rows = sorted(
        [{"name": name, "unit": unit, "qty": qty} for (name, unit), qty in buckets.items()],
        key=lambda x: (-x["qty"], x["name"]),
    )
    return {"matched": matched, "rows": rows, "skipped_qty": skipped_qty}


def _run_group_sum_sidecar(plan: Plan, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    buckets: Dict[Tuple[str, str], float] = defaultdict(float)
    matched = 0
    skipped_qty = 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        dt = _sidecar_target_date(plan, row)
        if dt != plan.target_date:
            continue
        matched += 1
        items = row.get("parsed_items") if isinstance(row.get("parsed_items"), list) else []
        if not items:
            skipped_qty += 1
            continue
        parsed_any = False
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            qty = item.get("qty")
            try:
                num = float(qty)
            except Exception:
                skipped_qty += 1
                continue
            unit = str(item.get("unit") or "").strip()
            buckets[(name, unit)] += num
            parsed_any = True
        if not parsed_any:
            skipped_qty += 1

    data = [{"name": name, "unit": unit, "qty": qty} for (name, unit), qty in buckets.items()]
    data.sort(key=lambda x: (-x["qty"], x["name"]))
    return {"matched": matched, "rows": data, "skipped_qty": skipped_qty}


def _run_list_who_what(plan: Plan, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows: List[Dict[str, str]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        fields = rec.get("fields") if isinstance(rec.get("fields"), dict) else {}
        dt = _record_target_date(plan, fields, rec)
        if dt != plan.target_date:
            continue
        who = str(fields.get(plan.name_field) or "").strip() if plan.name_field else ""
        what = str(fields.get(plan.group_field) or "").strip() if plan.group_field else ""
        if not who:
            who = "未命名客户"
        if not what:
            continue
        rows.append({"who": who, "what": what})
    return {"rows": rows}


def _run_list_who_what_sidecar(plan: Plan, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: List[Dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        dt = _sidecar_target_date(plan, row)
        if dt != plan.target_date:
            continue
        who = str(row.get("parsed_contact") or "").strip() or "未命名客户"
        raw = str(row.get("source_text") or "").strip()
        if not raw:
            continue
        out.append({"who": who, "what": raw})
    return {"rows": out}


def _run_raw_summary(plan: Plan, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows: List[str] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        fields = rec.get("fields") if isinstance(rec.get("fields"), dict) else {}
        dt = _record_target_date(plan, fields, rec)
        if dt != plan.target_date:
            continue
        if plan.group_field:
            raw = str(fields.get(plan.group_field) or "").strip()
        else:
            raw = ""
        if raw:
            rows.append(raw)
    return {"rows": rows}


def _run_raw_summary_sidecar(plan: Plan, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        dt = _sidecar_target_date(plan, row)
        if dt != plan.target_date:
            continue
        raw = str(row.get("source_text") or "").strip()
        if raw:
            out.append(raw)
    return {"rows": out}


def _format_number(n: float) -> str:
    if abs(n - int(n)) < 1e-9:
        return str(int(n))
    return f"{n:.2f}".rstrip("0").rstrip(".")


def _fmt_success(*, result: str, table: str = "", link: str = "", body_lines: Optional[List[str]] = None) -> str:
    lines: List[str] = [f"结果：{result}"]
    if table:
        lines.append(f"表格：{table}")
    if link:
        lines.append(f"链接：{link}")
    if body_lines:
        lines.append("———")
        lines.extend([x for x in body_lines if str(x).strip()])
    return "\n".join(lines)


def _fmt_error(*, reason: str, table: str = "", link: str = "", suggestion: str = "") -> str:
    lines = ["结果：执行失败", f"原因：{reason}"]
    if table:
        lines.append(f"表格：{table}")
    if link:
        lines.append(f"链接：{link}")
    if suggestion:
        lines.append(f"建议：{suggestion}")
    return "\n".join(lines)


def _fmt_clarify(*, question: str, table: str = "", link: str = "", options: Optional[List[str]] = None) -> str:
    lines = [f"需要确认：{question}"]
    if table:
        lines.append(f"当前表：{table}")
    if link:
        lines.append(f"链接：{link}")
    if options:
        lines.append("———")
        lines.extend([x for x in options if str(x).strip()][:2])
    return "\n".join(lines)


def run(text: str, bindings_path: Path, env_file: Path, timeout_s: int) -> Dict[str, Any]:
    load_dotenv(env_file)
    _auto_exit_draft()

    sync = _load_sync_module()
    binding = _resolve_active_binding(bindings_path)
    label = _binding_label(binding)
    open_url = _binding_open_url(binding)
    binding_name = str(binding.get("name") or "").strip()
    if binding_name:
        _ensure_binding_sidecar(binding_name, bindings_path, env_file, timeout_s)
    sidecar_rows = _load_binding_sidecar(binding_name, DEFAULT_SIDECAR_PATH) if binding_name else []

    app_id = str(os.environ.get("FEISHU_APP_ID") or "").strip()
    app_secret = str(os.environ.get("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        return {
            "ok": False,
            "reply_text": _fmt_error(
                reason="缺少飞书应用配置（FEISHU_APP_ID/FEISHU_APP_SECRET）",
                table=label,
                link=open_url,
                suggestion="请补齐飞书应用配置后重试。",
            ),
        }

    access_token = sync.get_tenant_access_token(app_id, app_secret, timeout_s=timeout_s)
    app_token = str(binding.get("app_token") or "").strip()
    table_id = str(binding.get("table_id") or "").strip()
    actual_names = list(sync.list_bitable_fields(app_token, table_id, access_token, timeout_s=timeout_s).keys())

    plan, clarify_q = _derive_plan_via_model(text, actual_names, label, timeout_s)
    missing_fields: List[str] = []
    if not plan and clarify_q:
        return {
            "ok": True,
            "reply_text": _fmt_clarify(
                question=clarify_q,
                table=label,
                link=open_url,
            ),
        }
    if not plan:
        # fallback compatibility for low-risk rollout
        plan, missing_fields, err = _derive_plan(text, actual_names)
        if not plan:
            return {
                "ok": True,
                "reply_text": _fmt_clarify(
                    question=(err or "我暂时没理解你的整理需求。"),
                    table=label,
                    link=open_url,
                ),
            }
    if plan.mode == "group_sum" and not missing_fields:
        if not plan.time_field:
            missing_fields.append("时间列")
        if not plan.group_field:
            missing_fields.append("商品列")
        if not plan.value_field:
            missing_fields.append("数量列")

    records: List[Dict[str, Any]] = []
    if plan.mode == "group_sum":
        use_sidecar = bool(sidecar_rows) and bool(missing_fields)
        if use_sidecar:
            res = _run_group_sum_sidecar(plan, sidecar_rows)
        else:
            if missing_fields:
                return {
                    "ok": True,
                    "reply_text": _fmt_error(
                        reason=f"当前表缺少可用于整理的{'、'.join(missing_fields)}",
                        table=label,
                        link=open_url,
                        suggestion="可先用原文列表或简单摘要方式整理。",
                    ),
                }
            records = sync.list_bitable_records(app_token, table_id, access_token, timeout_s=timeout_s, page_size=200)
            res = _run_group_sum(plan, records)
        rows = res.get("rows") if isinstance(res, dict) else []
        matched = int(res.get("matched") or 0) if isinstance(res, dict) else 0
        skipped_qty = int(res.get("skipped_qty") or 0) if isinstance(res, dict) else 0

        if not rows:
            body_lines = [f"未找到 {plan.target_date_label} 符合条件的可汇总记录。"]
            if matched > 0 and skipped_qty > 0:
                if use_sidecar:
                    body_lines.append("有记录命中，但辅助解析里的数量不够稳定。")
                else:
                    body_lines.append("有记录命中，但数量列无法稳定解析。")
            return {
                "ok": True,
                "reply_text": _fmt_success(
                    result=f"{plan.target_date_label}汇总完成（0条）",
                    table=label,
                    link=open_url,
                    body_lines=body_lines,
                ),
            }

        lines = [f"{plan.target_date_label}需备货："]
        for row in rows:
            lines.append(f"- {row['name']} {_format_number(float(row['qty']))}{row['unit']}")
        if skipped_qty:
            if use_sidecar:
                lines.append(f"另有 {skipped_qty} 条记录命中，但辅助解析里的数量不够稳定，未计入汇总。")
            else:
                lines.append(f"另有 {skipped_qty} 条记录命中，但数量列无法稳定解析，未计入汇总。")
        if use_sidecar:
            lines.append("以上基于已解析内容整理。")
        else:
            lines.append(f"以上按字段「{plan.time_field}」整理。")
        return {
            "ok": True,
            "reply_text": _fmt_success(
                result=f"{plan.target_date_label}汇总完成",
                table=label,
                link=open_url,
                body_lines=lines,
            ),
        }

    if plan.mode == "list_who_what":
        use_sidecar = bool(sidecar_rows) and (not plan.name_field or not plan.group_field)
        if use_sidecar:
            res = _run_list_who_what_sidecar(plan, sidecar_rows)
        else:
            records = sync.list_bitable_records(app_token, table_id, access_token, timeout_s=timeout_s, page_size=200)
            res = _run_list_who_what(plan, records)
        rows = res.get("rows") if isinstance(res, dict) else []
        if not rows:
            return {
                "ok": True,
                "reply_text": _fmt_success(
                    result=f"{plan.target_date_label}明细完成（0条）",
                    table=label,
                    link=open_url,
                    body_lines=[f"未找到 {plan.target_date_label} 对应记录。"],
                ),
            }
        lines = [f"{plan.target_date_label} 订购明细："]
        for row in rows[:100]:
            lines.append(f"- {row['who']}：{row['what']}")
        return {
            "ok": True,
            "reply_text": _fmt_success(
                result=f"{plan.target_date_label}明细完成",
                table=label,
                link=open_url,
                body_lines=lines,
            ),
        }

    if plan.mode == "raw_summary":
        use_sidecar = bool(sidecar_rows) and not plan.group_field
        if use_sidecar:
            res = _run_raw_summary_sidecar(plan, sidecar_rows)
        else:
            records = sync.list_bitable_records(app_token, table_id, access_token, timeout_s=timeout_s, page_size=200)
            res = _run_raw_summary(plan, records)
        rows = res.get("rows") if isinstance(res, dict) else []
        if not rows:
            return {
                "ok": True,
                "reply_text": _fmt_success(
                    result=f"{plan.target_date_label}列表完成（0条）",
                    table=label,
                    link=open_url,
                    body_lines=[f"未找到 {plan.target_date_label} 对应记录。"],
                ),
            }
        lines = [f"{plan.target_date_label} 列表："]
        for raw in rows[:100]:
            lines.append(f"- {raw}")
        return {
            "ok": True,
            "reply_text": _fmt_success(
                result=f"{plan.target_date_label}列表完成",
                table=label,
                link=open_url,
                body_lines=lines,
            ),
        }

    return {
        "ok": True,
        "reply_text": _fmt_clarify(
            question="我暂时还不支持这类即时整理。",
            table=label,
            link=open_url,
        ),
    }


def main(argv: List[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--bindings", default=str(DEFAULT_BINDINGS_PATH))
    ap.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    ap.add_argument("--timeout", type=int, default=20)
    args = ap.parse_args(argv)
    out = run(args.text, Path(args.bindings), Path(args.env_file), args.timeout)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
