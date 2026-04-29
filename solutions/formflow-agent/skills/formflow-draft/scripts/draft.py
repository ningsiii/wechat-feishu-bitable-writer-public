#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DRAFT_PATH = Path("solutions/formflow-agent/data/draft.json")
DEFAULT_ENV_FILE = Path(os.environ.get("SMALLBIZ_ENV_FILE") or "solutions/formflow-agent/.env")
DEFAULT_BINDINGS_PATH = Path(os.environ.get("SMALLBIZ_BINDINGS_FILE") or "solutions/formflow-agent/config/bindings.json")
DEFAULT_SYNC_SCRIPT = Path(os.environ.get("SMALLBIZ_SYNC_SCRIPT") or "solutions/formflow-agent/skills/formflow-feishu/scripts/sync_record.py")
DEFAULT_LEDGER_PATH = Path(os.environ.get("SMALLBIZ_LEDGER_PATH") or "solutions/formflow-agent/data/ledger.jsonl")
DEFAULT_SIDECAR_SCRIPT = Path("solutions/formflow-agent/skills/formflow-organize/scripts/sidecar.py")
DEFAULT_SIDECAR_PATH = Path(os.environ.get("SMALLBIZ_SIDECAR_PATH") or "solutions/formflow-agent/data/structured_sidecar.jsonl")
INTAKE_PATH = Path("solutions/formflow-agent/skills/formflow-intake/scripts/intake.py")
FEATURE_FLAGS_PATH = Path("solutions/formflow-agent/config/feature-flags.json")

CONFIRM_STATE_KEY = "__confirm_state"
CONFIRM_STATE_PENDING = "待确认"

DEDUP_WINDOW_S = int(os.environ.get("SMALLBIZ_DEDUP_WINDOW_S") or 60)
IDEMP_WINDOW_S = int(os.environ.get("SMALLBIZ_IDEMP_WINDOW_S") or 15)
JIELONG_MIN_LINES = int(os.environ.get("SMALLBIZ_JIELONG_MIN_LINES") or 3)
JIELONG_MAX_ITEMS = int(os.environ.get("SMALLBIZ_JIELONG_MAX_ITEMS") or 10)


def _is_single_table_mode() -> bool:
    try:
        if FEATURE_FLAGS_PATH.exists():
            data = json.loads(FEATURE_FLAGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "single_table_mode" in data:
                return bool(data.get("single_table_mode"))
    except Exception:
        pass
    profile = str(os.environ.get("SMALLBIZ_PROFILE") or "").strip().lower()
    return profile in {"single", "single_table", "single-table"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def make_record_id(prefix: str = "sb") -> str:
    ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(2)
    return f"{prefix}_{ts}_{rand}"


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _lookup_sidecar_source_text(record_id: str) -> str:
    rid = str(record_id or "").strip()
    if not rid or not DEFAULT_SIDECAR_PATH.exists():
        return ""
    try:
        for raw in DEFAULT_SIDECAR_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            if str(row.get("record_id") or "").strip() != rid:
                continue
            if not bool(row.get("valid", True)):
                continue
            txt = str(row.get("source_text") or "").strip()
            if txt:
                return txt
    except Exception:
        return ""
    return ""


def _compact_text(value: str, limit: int = 48) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _recent_samples_by_binding(limit_per_binding: int = 3) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    if not DEFAULT_SIDECAR_PATH.exists():
        return out
    try:
        rows = DEFAULT_SIDECAR_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return out
    # Read latest lines first so each table keeps the most recent few examples.
    for raw in reversed(rows):
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        if not bool(row.get("valid", True)):
            continue
        binding_name = str(row.get("binding_name") or "").strip()
        source_text = _compact_text(str(row.get("source_text") or "").strip())
        if not binding_name or not source_text:
            continue
        bucket = out.setdefault(binding_name, [])
        if source_text in bucket:
            continue
        if len(bucket) >= limit_per_binding:
            continue
        bucket.append(source_text)
    return out


def ensure_draft(path: Path) -> Dict[str, Any]:
    d = load_json(path)
    if not isinstance(d, dict):
        d = {}
    d.setdefault("active", False)
    d.setdefault("session", {})
    return d


def _active_table_label() -> str:
    try:
        data = load_json(DEFAULT_BINDINGS_PATH)
        active = str(data.get("active_binding") or "").strip()
        bindings = data.get("bindings") if isinstance(data.get("bindings"), list) else []
        for b in bindings:
            if not isinstance(b, dict):
                continue
            if str(b.get("name") or "").strip() == active:
                return str(b.get("display_name") or b.get("name") or "").strip() or "当前表"
    except Exception:
        pass
    return "当前表"


def _active_binding_name() -> str:
    try:
        data = load_json(DEFAULT_BINDINGS_PATH)
        return str(data.get("active_binding") or "").strip()
    except Exception:
        return ""


def _active_table_open_url() -> str:
    try:
        data = load_json(DEFAULT_BINDINGS_PATH)
        active = str(data.get("active_binding") or "").strip()
        bindings = data.get("bindings") if isinstance(data.get("bindings"), list) else []
        for b in bindings:
            if not isinstance(b, dict):
                continue
            if str(b.get("name") or "").strip() == active:
                return str(b.get("open_url") or "").strip()
    except Exception:
        pass
    return ""


def _humanize_sync_error(raw: str) -> str:
    s = (raw or "").strip()
    low = s.lower()
    if "missing feishu_app_id" in low or "missing feishu_app_secret" in low:
        return "写入失败：飞书应用密钥未配置（FEISHU_APP_ID/FEISHU_APP_SECRET）。"
    if "forbidden" in low or "permission" in low or "权限" in s or "access denied" in low:
        return "写入失败：飞书应用没有该表的编辑权限。"
    if "timeout" in low or "timed out" in low or "超时" in s:
        return "写入失败：请求超时或网络不稳定（稍后重试即可）。"
    if "not found" in low or "notexist" in low or "无效" in s:
        return "写入失败：表格链接无效或资源不存在（请重新复制链接并登记）。"
    first = s.splitlines()[0].strip() if s else "unknown_error"
    if len(first) > 120:
        first = first[:120] + "…"
    return f"写入失败：{first}"


def _fmt_success(
    *,
    result: str,
    table: str = "",
    link: str = "",
    actions: Optional[List[str]] = None,
    body_lines: Optional[List[str]] = None,
    written_text: str = "",
) -> str:
    lines: List[str] = ["✅【录入回执】", f"结果：{result}"]
    if table:
        lines.append(f"📍 当前表：{table}")
    if written_text:
        compact = " ".join(str(written_text).split())
        if len(compact) > 120:
            compact = compact[:117] + "..."
        lines.append(f"📝 写入内容：{compact}")
    if link:
        lines.append(f"🔗 链接：{link}")
    if body_lines:
        lines.extend([x for x in body_lines if str(x).strip()])
    if actions:
        lines.append("")
        if "草稿" in result or "待确认" in result:
            lines.append("可继续操作：")
            lines.append("• 确认：将全部待确认转为已确认")
            lines.append("• 作废：删除最新一条待确认")
        else:
            lines.extend([x for x in actions if str(x).strip()][:2])
    return "\n".join(lines)


def _fmt_error(*, reason: str, table: str = "", link: str = "", suggestion: str = "") -> str:
    lines = ["❌【执行失败】", f"原因：{reason}"]
    if table:
        lines.append("")
        lines.append(f"📍 当前表：{table}")
    if link:
        lines.append(f"🔗 链接：{link}")
    if suggestion:
        lines.append(f"💡 建议：{suggestion}")
    return "\n".join(lines)


def _fmt_clarify(*, question: str, table: str = "", link: str = "", options: Optional[List[str]] = None) -> str:
    lines = ["🤔【需要确认】", question]
    if table:
        lines.append(f"📍 当前表：{table}")
    if link:
        lines.append(f"🔗 链接：{link}")
    if options:
        lines.append("")
        lines.append("请回复：")
        lines.extend([x for x in options if str(x).strip()][:2])
    return "\n".join(lines)


def _normalize_bulk_choice(s: str) -> str:
    raw = (s or "").strip()
    compact = raw.replace(" ", "")
    mapping = {
        "1": "split",
        "回复1": "split",
        "回复一": "split",
        "拆单": "split",
        "拆单写入": "split",
        "2": "cancel",
        "回复2": "cancel",
        "回复二": "cancel",
        "取消": "cancel",
    }
    return mapping.get(compact, "")


def _normalize_route_choice(s: str) -> str:
    raw = (s or "").strip()
    compact = raw.replace(" ", "")
    mapping = {
        "1": "suggested",
        "回复1": "suggested",
        "回复一": "suggested",
        "写入推荐表": "suggested",
        "切到推荐表": "suggested",
        "写到推荐表": "suggested",
        "2": "current",
        "回复2": "current",
        "回复二": "current",
        "仍写当前表": "current",
        "保持当前表": "current",
        "保持原表": "current",
        "取消": "cancel",
    }
    return mapping.get(compact, "")


def _semantic_route_choice(*, text: str, scene: str, suggested_display: str = "", current_display: str = "", base_url: str, model: str, timeout_s: int) -> str:
    try:
        out = intake.call_deepseek_pending_choice(
            scene=scene,
            reply_text=text,
            context={"suggested_table": suggested_display, "current_table": current_display},
            base_url=base_url,
            model=model,
            timeout_s=min(timeout_s, 15),
        )
    except Exception:
        return ""
    intent = str((out.get("intent") if isinstance(out, dict) else "") or "").strip()
    return intent


def _semantic_split_choice(*, text: str, table_display: str = "", base_url: str, model: str, timeout_s: int) -> str:
    try:
        out = intake.call_deepseek_pending_choice(
            scene="bulk_split_confirm",
            reply_text=text,
            context={"table": table_display},
            base_url=base_url,
            model=model,
            timeout_s=min(timeout_s, 15),
        )
    except Exception:
        return ""
    intent = str((out.get("intent") if isinstance(out, dict) else "") or "").strip()
    return intent


def _normalize_compact(value: str) -> str:
    return "".join(str(value or "").strip().lower().split())


def _fallback_route_choice(*, text: str, suggested_display: str = "", current_display: str = "") -> str:
    compact = _normalize_compact(text)
    if not compact:
        return ""
    if any(x in compact for x in ("取消", "算了", "不弄了", "不用了")):
        return "cancel"
    suggested_tokens = [
        _normalize_compact(suggested_display),
        _normalize_compact(suggested_display.replace(" / 数据表", "")),
    ]
    current_tokens = [
        _normalize_compact(current_display),
        _normalize_compact(current_display.replace(" / 数据表", "")),
    ]
    if any(tok and tok in compact for tok in suggested_tokens):
        return "suggested"
    if any(tok and tok in compact for tok in current_tokens):
        return "current"
    if any(x in compact for x in ("按你推荐", "按推荐", "就那个", "就写那个", "听你的", "就按这个", "写到推荐表", "换吧")):
        return "suggested"
    if any(x in compact for x in ("不用换", "别换", "还是原来的", "保持原表", "保持当前表", "按原来的", "就现在这个", "原表", "当前表")):
        return "current"
    return ""


def _fallback_split_choice(text: str) -> str:
    compact = _normalize_compact(text)
    if not compact:
        return ""
    if any(x in compact for x in ("取消", "算了", "不拆", "别拆", "不拆了")):
        return "cancel"
    if any(x in compact for x in ("拆吧", "拆单", "分开", "一条条", "按这个拆")):
        return "split"
    return ""


def _entry_gate_reply(*, subtype: str, table_label: str, link: str) -> Dict[str, Any]:
    options = [
        "• 登记表格或切换已登记表",
        "• 写入一条新记录到表格",
        "• 查看已登记表列表",
        "• 查看或导出台账",
        "• 回答通用问题",
    ]
    if subtype == "record_modify":
        return {
            "ok": True,
            "receipt_text": _fmt_clarify(
                question="这一步我暂时还不能直接处理。",
                table=table_label,
                link=link,
                options=options,
            ),
        }
    if subtype == "pending_action":
        return {
            "ok": True,
            "receipt_text": _fmt_clarify(
                question="这一步我暂时还不能直接处理。",
                table=table_label,
                link=link,
                options=options,
            ),
        }
    return {
        "ok": True,
        "receipt_text": _fmt_clarify(
            question="这一步我暂时还不能直接处理。",
            table=table_label,
            link=link,
            options=options,
        ),
    }


def _semantic_entry_gate(*, text: str, bindings_data: Dict[str, Any], base_url: str, model: str, timeout_s: int) -> Dict[str, str]:
    try:
        out = intake.call_deepseek_record_entry_gate(
            text=text,
            table_candidates=_build_table_candidates_for_routing(bindings_data),
            base_url=base_url,
            model=model,
            timeout_s=min(timeout_s, 15),
        )
    except Exception:
        return {"verdict": "", "subtype": ""}
    if not isinstance(out, dict):
        return {"verdict": "", "subtype": ""}
    return {
        "verdict": str(out.get("verdict") or "").strip(),
        "subtype": str(out.get("subtype") or "").strip(),
    }


def _run_sync_script(
    *,
    op: str,
    item: Optional[Dict[str, Any]] = None,
    record_id: str = "",
    fields_json: Optional[Dict[str, Any]] = None,
    bindings_path: str,
    sync_script: str,
) -> Dict[str, Any]:
    cmd: List[str] = [sys.executable, sync_script, "--op", op, "--bindings", bindings_path]
    if record_id:
        cmd.extend(["--record-id", record_id])
    if item is not None:
        cmd.extend(["--item-json", json.dumps(item, ensure_ascii=False)])
    if fields_json is not None:
        cmd.extend(["--fields-json", json.dumps(fields_json, ensure_ascii=False)])
    if DEFAULT_ENV_FILE.exists():
        cmd.extend(["--env-file", str(DEFAULT_ENV_FILE)])
    p = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    if p.returncode != 0:
        return {"ok": False, "error": p.stderr.strip() or p.stdout.strip() or "unknown_error"}
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"ok": False, "error": f"non_json_sync_response: {p.stdout[:500]}"}


def _run_table_registry_select(*, target: str) -> Dict[str, Any]:
    cmd = [
        sys.executable,
        "solutions/formflow-agent/skills/formflow-feishu/scripts/table_registry.py",
        "--bindings",
        str(DEFAULT_BINDINGS_PATH),
    ]
    if DEFAULT_ENV_FILE.exists():
        cmd.extend(["--env-file", str(DEFAULT_ENV_FILE)])
    cmd.extend([
        "select",
        "--target",
        target,
    ])
    p = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    if p.returncode != 0:
        return {"ok": False, "error": p.stderr.strip() or p.stdout.strip() or "select_failed"}
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"ok": False, "error": f"non_json_select_response: {p.stdout[:500]}"}


def _run_sidecar_upsert(*, record_id: str, binding_name: str, source_text: str, created_at: str) -> Dict[str, Any]:
    if not DEFAULT_SIDECAR_SCRIPT.exists():
        return {"ok": False, "error": f"missing_sidecar_script: {DEFAULT_SIDECAR_SCRIPT}"}
    cmd = [
        sys.executable,
        str(DEFAULT_SIDECAR_SCRIPT),
        "upsert",
        "--record-id",
        record_id,
        "--binding-name",
        binding_name,
        "--source-text",
        source_text,
        "--created-at",
        created_at,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    if p.returncode != 0:
        return {"ok": False, "error": p.stderr.strip() or p.stdout.strip() or "sidecar_upsert_failed"}
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"ok": False, "error": f"non_json_sidecar_response: {p.stdout[:500]}"}


def _run_sidecar_mark_deleted(*, record_id: str) -> Dict[str, Any]:
    if not DEFAULT_SIDECAR_SCRIPT.exists():
        return {"ok": False, "error": f"missing_sidecar_script: {DEFAULT_SIDECAR_SCRIPT}"}
    cmd = [
        sys.executable,
        str(DEFAULT_SIDECAR_SCRIPT),
        "mark_deleted",
        "--record-id",
        record_id,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    if p.returncode != 0:
        return {"ok": False, "error": p.stderr.strip() or p.stdout.strip() or "sidecar_mark_deleted_failed"}
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"ok": False, "error": f"non_json_sidecar_response: {p.stdout[:500]}"}


def _import_intake_module() -> Any:
    if not INTAKE_PATH.exists():
        raise SystemExit(f"Missing intake script: {INTAKE_PATH}")
    import importlib.util

    spec = importlib.util.spec_from_file_location("smallbiz_intake", str(INTAKE_PATH))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def draft_start(path: Path) -> Dict[str, Any]:
    d = ensure_draft(path)
    d["active"] = True
    d["session"] = {"entered_at": now_iso(), "last_record_id": "", "open_url": _active_table_open_url()}
    d.pop("pending_route", None)
    d.pop("pending_switch", None)
    save_json(path, d)
    return {"ok": True, "active": True}


def draft_exit(path: Path) -> Dict[str, Any]:
    d = ensure_draft(path)
    d["active"] = False
    d.pop("pending_route", None)
    d.pop("pending_bulk", None)
    d.pop("pending_dup", None)
    d.pop("pending_switch", None)
    save_json(path, d)
    return {"ok": True, "active": False}


def _normalize_text_for_dedup(s: str) -> str:
    s = (s or "").strip()
    # collapse whitespace
    return " ".join(s.split())


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


_JIELONG_LINE_RE = __import__("re").compile(r"^\s*(\d{1,3})[\.、]\s*(.+?)\s*$")


def _detect_jielong_entries(text: str) -> List[str]:
    """
    Detect a numbered list (接龙-like) and return entry lines (without leading number).

    We only treat it as jielong if we find >= JIELONG_MIN_LINES consecutive numbered lines.
    """
    lines = (text or "").splitlines()
    # Find first "1." line
    start = -1
    for i, raw in enumerate(lines):
        m = _JIELONG_LINE_RE.match(raw)
        if m and m.group(1) == "1":
            start = i
            break
    if start < 0:
        return []

    out: List[str] = []
    expected = 1
    for raw in lines[start:]:
        m = _JIELONG_LINE_RE.match(raw)
        if not m:
            # stop at first non-numbered line once entries started
            if out:
                break
            continue
        num = int(m.group(1))
        body = (m.group(2) or "").strip()
        if num != expected:
            # allow gaps? for v1 we stop to avoid accidental capture
            break
        expected += 1
        if body:
            out.append(body)
    if len(out) < JIELONG_MIN_LINES:
        return []
    return out


def _build_table_candidates_for_routing(bindings_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    bindings = bindings_data.get("bindings") if isinstance(bindings_data.get("bindings"), list) else []
    active = str(bindings_data.get("active_binding") or "").strip()
    recent_samples = _recent_samples_by_binding(limit_per_binding=3)
    out: List[Dict[str, Any]] = []
    for b in bindings:
        if not isinstance(b, dict):
            continue
        name = str(b.get("name") or "").strip()
        if not name:
            continue
        display = str(b.get("display_name") or name).strip()
        cols = b.get("columns") if isinstance(b.get("columns"), dict) else {}
        prof = [str(k).strip() for k in cols.keys() if str(k).strip()]
        out.append(
            {
                "name": name,
                "display_name": display,
                "fields": prof[:12],
                "current": name == active,
                "recent_examples": recent_samples.get(name, []),
            }
        )
    return out


def draft_ingest(
    path: Path,
    text: str,
    base_url: str,
    model: str,
    timeout_s: int,
    *,
    bypass_dedup: bool = False,
    bypass_idemp: bool = False,
) -> Dict[str, Any]:
    d = ensure_draft(path)
    if not d.get("active"):
        d["active"] = True
    d.setdefault("session", {"entered_at": now_iso(), "last_record_id": "", "open_url": ""})

    intake = _import_intake_module()
    if DEFAULT_ENV_FILE.exists():
        intake.load_dotenv(DEFAULT_ENV_FILE)
    single_table_mode = _is_single_table_mode()

    # Pending duplicate confirm (light dedup prompt)
    pending_dup = d.get("pending_dup") if isinstance(d.get("pending_dup"), dict) else {}
    if pending_dup:
        choice = (text or "").strip()
        original = str(pending_dup.get("text") or "").strip()
        if choice == "继续":
            d.pop("pending_dup", None)
            save_json(path, d)
            return draft_ingest(path, original, base_url, model, timeout_s, bypass_dedup=True, bypass_idemp=True)
        if choice == "取消":
            d.pop("pending_dup", None)
            save_json(path, d)
            return {
                "ok": True,
                "receipt_text": _fmt_success(
                    result="已取消本次写入",
                    table=_active_table_label(),
                    link=_active_table_open_url(),
                ),
            }
        if choice == "退出":
            d.pop("pending_dup", None)
            d["active"] = False
            save_json(path, d)
            return {
                "ok": True,
                "receipt_text": _fmt_success(
                    result="已退出录入模式",
                    table=_active_table_label(),
                    link=_active_table_open_url(),
                ),
            }
        return {
            "ok": True,
            "receipt_text": _fmt_clarify(
                question="检测到可能重复（60秒内同内容）",
                table=_active_table_label(),
                link=_active_table_open_url(),
                options=["回复1：继续写入", "回复2：取消本次写入"],
            ),
        }

    # Short-window idempotency guard:
    # Prevent accidental double-ingest of the same user message in one handling round.
    if not bypass_idemp:
        bdata_idem = load_json(DEFAULT_BINDINGS_PATH)
        active_idem = str(bdata_idem.get("active_binding") or "").strip()
        norm_idem = _normalize_text_for_dedup(text)
        sess_idem = d.get("session") if isinstance(d.get("session"), dict) else {}
        idem_norm = str(sess_idem.get("idem_last_norm") or "").strip()
        idem_table = str(sess_idem.get("idem_last_table") or "").strip()
        idem_at = str(sess_idem.get("idem_last_at") or "").strip()
        idem_receipt = str(sess_idem.get("idem_last_receipt") or "").strip()
        ts_idem = _parse_iso(idem_at)
        if norm_idem and idem_norm and norm_idem == idem_norm and active_idem and active_idem == idem_table and ts_idem:
            delta_idem = (datetime.now().astimezone() - ts_idem).total_seconds()
            if 0 <= delta_idem <= IDEMP_WINDOW_S and idem_receipt:
                return {"ok": True, "receipt_text": idem_receipt}

    # Pending bulk (jielong) flow
    pending_bulk = d.get("pending_bulk") if isinstance(d.get("pending_bulk"), dict) else {}
    if pending_bulk:
        stage = str(pending_bulk.get("stage") or "").strip()
        original = str(pending_bulk.get("text") or "").strip()
        suggested = str(pending_bulk.get("suggested") or "").strip()
        current = str(pending_bulk.get("current") or "").strip()
        entries = pending_bulk.get("entries") if isinstance(pending_bulk.get("entries"), list) else []
        total = len([x for x in entries if isinstance(x, str) and x.strip()])
        if stage == "choose_table":
            choice = _normalize_route_choice(text)
            name_map = {c.get("name"): c.get("display_name") for c in _build_table_candidates_for_routing(load_json(DEFAULT_BINDINGS_PATH)) if isinstance(c, dict)}
            best_disp = str(name_map.get(suggested) or suggested or "推荐表")
            cur_disp = str(name_map.get(current) or current or _active_table_label())
            if not choice:
                choice = _semantic_route_choice(
                    text=text,
                    scene="bulk_route_confirm",
                    suggested_display=best_disp,
                    current_display=cur_disp,
                    base_url=base_url,
                    model=model,
                    timeout_s=timeout_s,
                )
            if not choice:
                choice = _fallback_route_choice(text=text, suggested_display=best_disp, current_display=cur_disp)
            if choice == "suggested" and suggested:
                sel = _run_table_registry_select(target=suggested)
                if isinstance(sel, dict) and sel.get("ok") is True:
                    pending_bulk["stage"] = "confirm_split"
                    d["pending_bulk"] = pending_bulk
                    save_json(path, d)
                    table_label = _active_table_label()
                    return {
                        "ok": True,
                        "receipt_text": _fmt_clarify(
                            question=f"我检测到这是一份接龙信息，共 {total} 条。",
                            table=table_label,
                            link=_active_table_open_url(),
                            options=["• 1 或 拆单：拆单写入当前表", "• 2 或 取消：放弃这次接龙写入"],
                        ),
                    }
                return {"ok": False, "receipt_text": f"切换表失败：{sel.get('error') or sel}"}
            if choice == "current":
                pending_bulk["stage"] = "confirm_split"
                d["pending_bulk"] = pending_bulk
                save_json(path, d)
                table_label = _active_table_label()
                return {
                    "ok": True,
                    "receipt_text": _fmt_clarify(
                        question=f"我检测到这是一份接龙信息，共 {total} 条。",
                        table=table_label,
                        link=_active_table_open_url(),
                            options=["• 1 或 拆单：拆单写入当前表", "• 2 或 取消：放弃这次接龙写入"],
                        ),
                    }
            if choice == "cancel":
                d.pop("pending_bulk", None)
                save_json(path, d)
                return {
                    "ok": True,
                    "receipt_text": _fmt_success(
                        result="已取消本次接龙写入",
                        table=_active_table_label(),
                        link=_active_table_open_url(),
                    ),
                }
            return {
                "ok": True,
                "receipt_text": _fmt_clarify(
                    question=f"这份接龙更像写入 {best_disp}，是否切换？",
                    table=cur_disp,
                    link=_active_table_open_url(),
                    options=[f"• 1 或 写入推荐表：写入 {best_disp}", "• 2 或 保持原表：仍按当前目标表写入", "• 取消：放弃这次接龙写入"],
                ),
            }

        if stage == "confirm_split":
            choice = _normalize_bulk_choice(text)
            if not choice:
                choice = _semantic_split_choice(
                    text=text,
                    table_display=_active_table_label(),
                    base_url=base_url,
                    model=model,
                    timeout_s=timeout_s,
                )
            if not choice:
                choice = _fallback_split_choice(text)
            if choice == "split":
                # perform batch writes (best-effort, capped)
                d.pop("pending_bulk", None)
                save_json(path, d)

                # Read current table fields once
                meta = _run_sync_script(op="fields_meta", bindings_path=str(DEFAULT_BINDINGS_PATH), sync_script=str(DEFAULT_SYNC_SCRIPT))
                table_fields = meta.get("fields") if isinstance(meta, dict) else []
                if not isinstance(table_fields, list):
                    table_fields = []

                table_label = _active_table_label()
                open_url = _active_table_open_url()
                written = 0
                failed = 0

                cap = min(total, JIELONG_MAX_ITEMS)
                for raw in entries[:cap]:
                    if not isinstance(raw, str) or not raw.strip():
                        continue
                    parsed = intake.call_deepseek_for_table(text=raw, table_fields=table_fields, base_url=base_url, model=model, timeout_s=min(timeout_s, 20))
                    fields = parsed.get("fields") if isinstance(parsed, dict) and isinstance(parsed.get("fields"), dict) else {}
                    item_id = make_record_id()
                    item = {"id": item_id, CONFIRM_STATE_KEY: CONFIRM_STATE_PENDING, "title": ""}
                    synced = _run_sync_script(
                        op="create_dynamic",
                        item=item,
                        fields_json=fields,
                        bindings_path=str(DEFAULT_BINDINGS_PATH),
                        sync_script=str(DEFAULT_SYNC_SCRIPT),
                    )
                    if isinstance(synced, dict) and synced.get("ok") is True:
                        written += 1
                        ou = str(synced.get("open_url") or "").strip()
                        if ou:
                            open_url = ou
                        rid = str(synced.get("record_id") or "").strip()
                        if rid:
                            _run_sidecar_upsert(
                                record_id=rid,
                                binding_name=_active_binding_name() or table_label,
                                source_text=raw,
                                created_at=now_iso(),
                            )
                        append_jsonl(
                            DEFAULT_LEDGER_PATH,
                            {
                                "type": "pending_written",
                                "event_at": now_iso(),
                                "payload": {
                                    "table": table_label,
                                    "open_url": open_url,
                                    "record_id": str(synced.get("record_id") or ""),
                                    "source_text": _compact_text(str(raw), 400),
                                },
                            },
                        )
                    else:
                        failed += 1
                        append_jsonl(
                            DEFAULT_LEDGER_PATH,
                            {
                                "type": "pending_write_failed",
                                "event_at": now_iso(),
                                "payload": {
                                    "table": table_label,
                                    "open_url": open_url,
                                    "error": str((synced or {}).get("error") if isinstance(synced, dict) else synced),
                                    "source_text": _compact_text(str(raw), 400),
                                },
                            },
                        )

                note = ""
                if total > cap:
                    note = f"\n（为避免超时，本次仅写入前 {cap} 条）"
                if failed:
                    note = f"{note}\n（其中 {failed} 条写入失败，可稍后分批重试）"

                body_lines = [f"📊 本次写入：{written} 条"]
                if note:
                    body_lines.extend([x for x in note.splitlines() if x.strip()])
                receipt_text = _fmt_success(
                    result="接龙已拆单写入（状态：待确认）",
                    table=table_label,
                    link=open_url,
                    body_lines=body_lines,
                    actions=["确认：将全部待确认转为已确认", "作废：删除最新一条待确认"],
                )
                return {"ok": True, "receipt_text": receipt_text}

            if choice == "cancel":
                d.pop("pending_bulk", None)
                save_json(path, d)
                return {
                    "ok": True,
                    "receipt_text": _fmt_success(
                        result="已取消本次接龙写入",
                        table=_active_table_label(),
                        link=_active_table_open_url(),
                    ),
                }
            return {
                "ok": True,
                "receipt_text": _fmt_clarify(
                    question=f"我检测到这是一份接龙信息，共 {total} 条。",
                    table=_active_table_label(),
                    link=_active_table_open_url(),
                    options=["• 1 或 拆单：拆单写入当前表", "• 2 或 取消：放弃这次接龙写入"],
                ),
            }

        # unknown stage: clear for safety
        d.pop("pending_bulk", None)
        save_json(path, d)

    # Pending routing confirm
    pending = d.get("pending_route") if isinstance(d.get("pending_route"), dict) else {}
    if single_table_mode and pending:
        # Single-table mode disables table-switch confirmation.
        d.pop("pending_route", None)
        save_json(path, d)
        pending = {}
    if pending:
        choice = _normalize_route_choice(text)
        suggested = str(pending.get("suggested") or "").strip()
        current = str(pending.get("current") or "").strip()
        original = str(pending.get("text") or "").strip()
        name_map = {c.get("name"): c.get("display_name") for c in _build_table_candidates_for_routing(load_json(DEFAULT_BINDINGS_PATH)) if isinstance(c, dict)}
        best_disp = str(name_map.get(suggested) or suggested or "推荐表")
        cur_disp = str(name_map.get(current) or current or _active_table_label())
        if not choice:
            choice = _semantic_route_choice(
                text=text,
                scene="route_confirm",
                suggested_display=best_disp,
                current_display=cur_disp,
                base_url=base_url,
                model=model,
                timeout_s=timeout_s,
            )
        if not choice:
            choice = _fallback_route_choice(text=text, suggested_display=best_disp, current_display=cur_disp)
        # Audit: record the final routing decision (user-confirm or fallback).
        append_jsonl(
            DEFAULT_LEDGER_PATH,
            {
                "type": "route_decided",
                "event_at": now_iso(),
                "payload": {
                    "suggested": suggested,
                    "current": current,
                    "choice": choice,
                    "source_text": _compact_text(original, 400),
                },
            },
        )
        if choice == "suggested" and suggested:
            d.pop("pending_route", None)
            save_json(path, d)
            sel = _run_table_registry_select(target=suggested)
            if isinstance(sel, dict) and sel.get("ok") is True:
                return draft_ingest(path, original, base_url, model, timeout_s)
            return {"ok": False, "receipt_text": f"切换表失败：{sel.get('error') or sel}"}
        if choice == "current":
            d.pop("pending_route", None)
            save_json(path, d)
            return draft_ingest(path, original, base_url, model, timeout_s)
        if choice == "cancel":
            d.pop("pending_route", None)
            save_json(path, d)
            return {
                "ok": True,
                "receipt_text": _fmt_success(
                    result="已取消本次写入",
                    table=_active_table_label(),
                    link=_active_table_open_url(),
                ),
            }
        return {
            "ok": True,
            "receipt_text": _fmt_clarify(
                question=f"这条信息更像写入 {best_disp}，是否切换？",
                table=cur_disp,
                link=_active_table_open_url(),
                options=[f"• 1 或 写入推荐表：写入 {best_disp}", f"• 2 或 保持原表：仍按当前目标表写入"],
            ),
        }

    # Light dedup (60s same table + same normalized text) -> prompt, do not block by default.
    bdata0 = load_json(DEFAULT_BINDINGS_PATH)
    active0 = str(bdata0.get("active_binding") or "").strip()
    norm = _normalize_text_for_dedup(text)
    sess0 = d.get("session") if isinstance(d.get("session"), dict) else {}
    last_norm = str(sess0.get("last_norm_text") or "").strip()
    last_at = str(sess0.get("last_norm_at") or "").strip()
    last_table = str(sess0.get("last_norm_table") or "").strip()
    ts0 = _parse_iso(last_at)
    if (not bypass_dedup) and norm and last_norm and norm == last_norm and active0 and active0 == last_table and ts0:
        delta = (datetime.now().astimezone() - ts0).total_seconds()
        if 0 <= delta <= DEDUP_WINDOW_S:
            d["pending_dup"] = {"text": text, "asked_at": now_iso(), "table": active0}
            save_json(path, d)
            return {
                "ok": True,
                "receipt_text": (
                    "检测到可能重复（60秒内同内容）\n"
                    "发送【继续】仍然写入\n"
                    "发送【取消】放弃这次写入"
                ),
            }

    # Entry qualification gate:
    # Even after a message enters draft flow, it should still prove it looks like
    # a fresh record/relay content before we attempt table routing or write-in.
    bdata_gate = load_json(DEFAULT_BINDINGS_PATH)
    gate = _semantic_entry_gate(text=text, bindings_data=bdata_gate, base_url=base_url, model=model, timeout_s=timeout_s)
    verdict = str(gate.get("verdict") or "").strip()
    subtype = str(gate.get("subtype") or "").strip()
    if verdict == "not_entry":
        return _entry_gate_reply(subtype=subtype, table_label=_active_table_label(), link=_active_table_open_url())

    # Detect jielong (numbered list). Ask to choose table (if needed), then confirm split.
    entries = _detect_jielong_entries(text)
    if entries:
        bdata = bdata_gate
        total_tables = len(bdata.get("bindings") or []) if isinstance(bdata.get("bindings"), list) else 0
        current = str(bdata.get("active_binding") or "").strip()
        total = len(entries)
        cap = min(total, JIELONG_MAX_ITEMS)

        # If multiple tables, try recommending a table first.
        if (not single_table_mode) and total_tables >= 2 and current:
            try:
                candidates = _build_table_candidates_for_routing(bdata)
                pick = intake.call_deepseek_pick_table(text=text, candidates=candidates, base_url=base_url, model=model, timeout_s=min(timeout_s, 20))
                best = str((pick.get("best") if isinstance(pick, dict) else "") or "").strip()
                conf = float(pick.get("confidence") or 0.0) if isinstance(pick, dict) else 0.0
                if best and best != current and conf >= 0.55:
                    name_map = {c.get("name"): c.get("display_name") for c in candidates if isinstance(c, dict)}
                    best_disp = str(name_map.get(best) or best)
                    cur_disp = str(name_map.get(current) or current)
                    d["pending_bulk"] = {
                        "stage": "choose_table",
                        "text": text,
                        "entries": entries[:cap],
                        "suggested": best,
                        "current": current,
                        "asked_at": now_iso(),
                    }
                    save_json(path, d)
                    return {
                        "ok": True,
                        "receipt_text": _fmt_clarify(
                            question=f"这份接龙更像写入 {best_disp}，是否切换？",
                            table=cur_disp,
                            link=_active_table_open_url(),
                            options=[f"• 1 或 写入推荐表：写入 {best_disp}", "• 2 或 保持原表：仍按当前目标表写入", "• 取消：放弃这次接龙写入"],
                        ),
                    }
            except Exception:
                pass

        # Default: current table (if any). Ask to confirm split.
        d["pending_bulk"] = {"stage": "confirm_split", "text": text, "entries": entries[:cap], "asked_at": now_iso()}
        save_json(path, d)
        table_label = _active_table_label()
        return {
            "ok": True,
            "receipt_text": _fmt_clarify(
                question=f"我检测到这是一份接龙信息，共 {total} 条。",
                table=table_label,
                link=_active_table_open_url(),
                options=["• 1 或 拆单：拆单写入当前表", "• 2 或 取消：放弃这次接龙写入"],
            ),
        }

    # Confirm-before-write: if multiple tables and model recommends other table, ask first.
    bdata = bdata_gate
    total_tables = len(bdata.get("bindings") or []) if isinstance(bdata.get("bindings"), list) else 0
    current = str(bdata.get("active_binding") or "").strip()
    if (not single_table_mode) and total_tables >= 2 and current:
        try:
            candidates = _build_table_candidates_for_routing(bdata)
            pick = intake.call_deepseek_pick_table(text=text, candidates=candidates, base_url=base_url, model=model, timeout_s=timeout_s)
            best = str((pick.get("best") if isinstance(pick, dict) else "") or "").strip()
            conf = float(pick.get("confidence") or 0.0) if isinstance(pick, dict) else 0.0
            if best and best != current and conf >= 0.55:
                d["pending_route"] = {"text": text, "suggested": best, "current": current, "asked_at": now_iso()}
                save_json(path, d)
                name_map = {c.get("name"): c.get("display_name") for c in candidates if isinstance(c, dict)}
                best_disp = str(name_map.get(best) or best)
                cur_disp = str(name_map.get(current) or current)
                return {
                    "ok": True,
                    "receipt_text": _fmt_clarify(
                        question=f"这条信息更像写入 {best_disp}，是否切换？",
                        table=cur_disp,
                        link=_active_table_open_url(),
                        options=[f"• 1 或 写入推荐表：写入 {best_disp}", f"• 2 或 保持原表：仍按当前目标表写入"],
                    ),
                }
        except Exception:
            pass

    # Read current table fields
    meta = _run_sync_script(op="fields_meta", bindings_path=str(DEFAULT_BINDINGS_PATH), sync_script=str(DEFAULT_SYNC_SCRIPT))
    table_fields = meta.get("fields") if isinstance(meta, dict) else []
    if not isinstance(table_fields, list):
        table_fields = []

    parsed = intake.call_deepseek_for_table(text=text, table_fields=table_fields, base_url=base_url, model=model, timeout_s=timeout_s)
    fields = parsed.get("fields") if isinstance(parsed, dict) and isinstance(parsed.get("fields"), dict) else {}

    item_id = make_record_id()
    item = {"id": item_id, CONFIRM_STATE_KEY: CONFIRM_STATE_PENDING, "title": ""}
    synced = _run_sync_script(
        op="create_dynamic",
        item=item,
        fields_json=fields,
        bindings_path=str(DEFAULT_BINDINGS_PATH),
        sync_script=str(DEFAULT_SYNC_SCRIPT),
    )

    table_label = _active_table_label()
    open_url = str(synced.get("open_url") or "").strip() if isinstance(synced, dict) else ""
    if not open_url:
        open_url = _active_table_open_url()
    if not open_url:
        open_url = str(d.get("session", {}).get("open_url") or "").strip() if isinstance(d.get("session"), dict) else ""

    ok = bool(isinstance(synced, dict) and synced.get("ok") is True)
    if not ok:
        human = _humanize_sync_error(str(synced.get("error") or "") if isinstance(synced, dict) else "unknown_error")
        receipt_text = _fmt_error(
            reason=human.replace("写入失败：", ""),
            table=table_label,
            link=open_url,
            suggestion="请在飞书里把该表授权给应用可编辑后重试。",
        )
        append_jsonl(
            DEFAULT_LEDGER_PATH,
            {
                "type": "pending_write_failed",
                "event_at": now_iso(),
                "payload": {
                    "table": table_label,
                    "open_url": open_url,
                    "error": str(synced.get("error") if isinstance(synced, dict) else ""),
                    "source_text": _compact_text(str(text), 400),
                },
            },
        )
        return {"ok": False, "receipt_text": receipt_text}

    record_id = str(synced.get("record_id") or "").strip() if isinstance(synced, dict) else ""
    if record_id:
        _run_sidecar_upsert(
            record_id=record_id,
            binding_name=_active_binding_name() or table_label,
            source_text=text,
            created_at=now_iso(),
        )
    sess = d.get("session") if isinstance(d.get("session"), dict) else {}
    sess = dict(sess)
    sess["last_record_id"] = record_id
    sess["last_source_text"] = str(text)
    sess["open_url"] = open_url
    # For light dedup (same table + same normalized text within window)
    sess["last_norm_text"] = _normalize_text_for_dedup(text)
    sess["last_norm_at"] = now_iso()
    sess["last_norm_table"] = str(load_json(DEFAULT_BINDINGS_PATH).get("active_binding") or "").strip()
    d["session"] = sess
    save_json(path, d)

    append_jsonl(
        DEFAULT_LEDGER_PATH,
        {
            "type": "pending_written",
            "event_at": now_iso(),
            "payload": {"table": table_label, "open_url": open_url, "record_id": record_id, "source_text": _compact_text(str(text), 400)},
        },
    )

    receipt_text = _fmt_success(
        result="草稿已录入（状态：待确认）",
        table=table_label,
        link=open_url,
        actions=["确认：确认全部待确认", "作废：删除最新一条待确认"],
        written_text=text,
    )
    # Keep a short-lived idempotency fingerprint to suppress accidental duplicate execution.
    sess["idem_last_norm"] = _normalize_text_for_dedup(text)
    sess["idem_last_table"] = str(load_json(DEFAULT_BINDINGS_PATH).get("active_binding") or "").strip()
    sess["idem_last_at"] = now_iso()
    sess["idem_last_receipt"] = receipt_text
    d["session"] = sess
    save_json(path, d)
    return {"ok": True, "receipt_text": receipt_text}


def draft_confirm_all(path: Path) -> Dict[str, Any]:
    out = _run_sync_script(op="confirm_all_pending", bindings_path=str(DEFAULT_BINDINGS_PATH), sync_script=str(DEFAULT_SYNC_SCRIPT))
    open_url = str(out.get("open_url") or "").strip() if isinstance(out, dict) else ""
    count = int(out.get("count") or 0) if isinstance(out, dict) else 0
    record_ids = out.get("record_ids") if isinstance(out, dict) else []
    if not isinstance(record_ids, list):
        record_ids = []
    table_label = _active_table_label()
    reply_text = _fmt_success(
        result=f"已确认待确认（本次 {count} 条）",
        table=table_label,
        link=open_url,
    )
    for rid in [str(x or "").strip() for x in record_ids if str(x or "").strip()]:
        src = _lookup_sidecar_source_text(rid)
        append_jsonl(
            DEFAULT_LEDGER_PATH,
            {
                "type": "pending_confirmed_one",
                "event_at": now_iso(),
                "payload": {"table": table_label, "open_url": open_url, "record_id": rid, "source_text": _compact_text(src, 400) if src else ""},
            },
        )
    append_jsonl(
        DEFAULT_LEDGER_PATH,
        {"type": "pending_confirmed_all", "event_at": now_iso(), "payload": {"count": count, "open_url": open_url, "record_ids": record_ids}},
    )
    return {"ok": True, "reply_text": reply_text}


def draft_void_latest(path: Path) -> Dict[str, Any]:
    d = ensure_draft(path)
    prefer = ""
    if isinstance(d.get("session"), dict):
        prefer = str(d["session"].get("last_record_id") or "").strip()
    cmd = [sys.executable, str(DEFAULT_SYNC_SCRIPT), "--op", "void_latest_pending", "--bindings", str(DEFAULT_BINDINGS_PATH)]
    if prefer:
        cmd.extend(["--prefer-record-id", prefer])
    if DEFAULT_ENV_FILE.exists():
        cmd.extend(["--env-file", str(DEFAULT_ENV_FILE)])
    p = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    if p.returncode != 0:
        raise SystemExit(p.stderr.strip() or p.stdout.strip())
    out = json.loads(p.stdout)
    open_url = str(out.get("open_url") or "").strip()
    record_id = str(out.get("record_id") or "").strip()
    void_text = ""
    sess = d.get("session") if isinstance(d.get("session"), dict) else {}
    if record_id and isinstance(sess, dict):
        if str(sess.get("last_record_id") or "").strip() == record_id:
            void_text = str(sess.get("last_source_text") or "").strip()
    if record_id and not void_text:
        void_text = _lookup_sidecar_source_text(record_id)
    if record_id:
        _run_sidecar_mark_deleted(record_id=record_id)
    table_label = _active_table_label()
    body_lines: List[str] = []
    if void_text:
        compact = " ".join(void_text.split())
        if len(compact) > 120:
            compact = compact[:117] + "..."
        body_lines.append(f"🗑️ 作废内容：{compact}")
    reply_text = _fmt_success(
        result="已作废最新一条待确认",
        table=table_label,
        link=open_url,
        body_lines=body_lines,
    )
    append_jsonl(
        DEFAULT_LEDGER_PATH,
        {
            "type": "pending_void_latest",
            "event_at": now_iso(),
            "payload": {"open_url": open_url, "record_id": record_id, "source_text": _compact_text(void_text, 400) if void_text else ""},
        },
    )
    return {"ok": True, "reply_text": reply_text}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="draft.py")
    p.add_argument("--draft", default=str(DEFAULT_DRAFT_PATH))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("start")
    sub.add_parser("exit")

    ing = sub.add_parser("ingest")
    ing.add_argument("--text", required=True)
    ing.add_argument("--base-url", default=os.environ.get("SMALLBIZ_MODEL_BASE_URL") or "https://api.deepseek.com")
    ing.add_argument("--model", default=os.environ.get("SMALLBIZ_MODEL") or "deepseek-chat")
    ing.add_argument("--timeout", type=int, default=60)

    sub.add_parser("confirm_all")
    sub.add_parser("void_latest")

    return p


def main(argv: List[str]) -> None:
    args = build_parser().parse_args(argv)
    draft_path = Path(args.draft)

    if args.cmd == "start":
        out = draft_start(draft_path)
    elif args.cmd == "exit":
        out = draft_exit(draft_path)
    elif args.cmd == "ingest":
        out = draft_ingest(draft_path, text=str(args.text), base_url=str(args.base_url), model=str(args.model), timeout_s=int(args.timeout))
    elif args.cmd == "confirm_all":
        out = draft_confirm_all(draft_path)
    elif args.cmd == "void_latest":
        out = draft_void_latest(draft_path)
    else:
        raise SystemExit(f"Unknown cmd: {args.cmd}")

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
