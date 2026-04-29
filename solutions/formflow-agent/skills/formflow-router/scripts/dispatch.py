#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


DEFAULT_BINDINGS_PATH = Path(os.environ.get("SMALLBIZ_BINDINGS_FILE") or "solutions/formflow-agent/config/bindings.json")
DEFAULT_DRAFT_PATH = Path(os.environ.get("SMALLBIZ_DRAFT_FILE") or "solutions/formflow-agent/data/draft.json")
DEFAULT_ENV_FILE = Path(os.environ.get("SMALLBIZ_ENV_FILE") or "solutions/formflow-agent/.env")
DEFAULT_LEDGER_PATH = Path(os.environ.get("SMALLBIZ_LEDGER_PATH") or "solutions/formflow-agent/data/ledger.jsonl")

ROUTER_SCRIPT_V1 = Path("solutions/formflow-agent/skills/formflow-router/scripts/router.py")
ROUTER_SCRIPT_V2 = Path("solutions/formflow-agent/skills/formflow-router/scripts/router_v2.py")
TABLE_REGISTRY_SCRIPT = Path("solutions/formflow-agent/skills/formflow-feishu/scripts/table_registry.py")
DRAFT_SCRIPT = Path("solutions/formflow-agent/skills/formflow-draft/scripts/draft.py")
ORGANIZE_SCRIPT = Path("solutions/formflow-agent/skills/formflow-organize/scripts/organize.py")
OPS_SCRIPT = Path("solutions/formflow-agent/skills/formflow-ops/scripts/ops.py")
INTAKE_SCRIPT = Path("solutions/formflow-agent/skills/formflow-intake/scripts/intake.py")
FEATURE_FLAGS_PATH = Path("solutions/formflow-agent/config/feature-flags.json")

_RE_FEISHU_LINK = re.compile(r"https?://[^\s]*feishu\.cn/(?:base|wiki)/[^\s]+", re.I)
_LIST_WORDS = {"列表", "表列表", "表清单", "我的表", "查表"}
# For V1: treat "台账/账本" as export-first to give users a usable artifact.
_LEDGER_EXPORT_WORDS = {"导出台账", "导出账本", "导出存折", "导出记录", "台账", "账本", "查看台账", "看看台账", "看台账"}
_LEDGER_PREVIEW_WORDS = {"台账预览", "预览台账", "最近记录"}
_CONFIRM_WORDS = {"确认", "确认全部", "全部确认"}
_VOID_WORDS = {"作废"}
_EXIT_WORDS = {"退出"}
_REPLACE_REPLY_WORDS = {"替换", "取消", "1", "2"}


def _normalize_compact(text: str) -> str:
    return "".join(str(text or "").strip().lower().split())


def _looks_old_record_action(text: str) -> bool:
    compact = _normalize_compact(text)
    if not compact:
        return False
    # High-risk references to an existing/previous record. Keep this guard narrow.
    has_ref = any(x in compact for x in ("刚那条", "刚才那条", "上一条", "那条", "那单", "那笔"))
    has_delete = any(x in compact for x in ("删掉", "删除", "撤回", "去掉"))
    has_modify = any(x in compact for x in ("加", "再加", "补", "改", "修改"))
    return (has_ref and has_delete) or (has_ref and has_modify)


def _unsupported_reply_text() -> str:
    return (
        "🤔【需要确认】\n"
        "这一步我暂时还不能直接处理。\n\n"
        "我现在可以帮你：\n"
        "• 登记表格或切换已登记表\n"
        "• 写入一条新记录到表格\n"
        "• 查看已登记表列表\n"
        "• 查看或导出台账\n"
        "• 回答通用问题"
    )


def _has_pending_bulk(draft: Path) -> bool:
    data = _load_json(draft)
    pending = data.get("pending_bulk")
    return isinstance(pending, dict) and bool(str(pending.get("stage") or "").strip())


def _has_pending_route(draft: Path) -> bool:
    data = _load_json(draft)
    pending = data.get("pending_route")
    return isinstance(pending, dict) and bool(str(pending.get("suggested") or "").strip())


def _run_json(cmd: List[str]) -> Tuple[bool, Dict[str, Any], str]:
    p = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    if p.returncode != 0:
        err = (p.stderr.strip() or p.stdout.strip() or "unknown_error")
        return False, {}, err
    try:
        obj = json.loads(p.stdout)
        if isinstance(obj, dict):
            return True, obj, ""
        return False, {}, "non_json_dict_output"
    except Exception:
        return False, {}, f"non_json_output: {p.stdout[:300]}"


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _ledger_event(ev_type: str, payload: Dict[str, Any]) -> None:
    # Best-effort audit trail. Never block the user flow if logging fails.
    try:
        from datetime import datetime

        _append_jsonl(
            DEFAULT_LEDGER_PATH,
            {
                "type": str(ev_type or "").strip() or "unknown",
                "event_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "payload": payload if isinstance(payload, dict) else {},
            },
        )
    except Exception:
        return


def _runtime_context(bindings: Path, draft: Path, text: str) -> Dict[str, Any]:
    b = _load_json(bindings)
    d = _load_json(draft)
    active_binding = str(b.get("active_binding") or "").strip()
    bindings_arr = b.get("bindings") if isinstance(b.get("bindings"), list) else []
    binding_names: List[str] = []
    for it in bindings_arr:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if name:
            binding_names.append(name)
    msg = (text or "").strip()
    is_cmd = msg in ("列表", "确认", "作废", "退出") or msg.startswith("删除表")
    return {
        "draft_active": bool(d.get("active")),
        "active_binding": active_binding,
        "bindings_count": len(binding_names),
        "binding_names": binding_names[:12],
        "has_feishu_link": bool(_RE_FEISHU_LINK.search(msg)),
        "is_explicit_command": bool(is_cmd),
        "message_len": len(msg),
    }


def _load_intake_module(path: Path):
    spec = importlib.util.spec_from_file_location("smallbiz_intake_dispatch", str(path))
    if not spec or not spec.loader:
        raise RuntimeError(f"failed loading intake module: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _second_chance_route(
    *,
    text: str,
    bindings: Path,
    draft: Path,
    env_file: Path,
    timeout_s: int,
) -> Tuple[str, float]:
    if not INTAKE_SCRIPT.exists():
        return "", 0.0
    try:
        intake = _load_intake_module(INTAKE_SCRIPT)
        if env_file.exists():
            intake.load_dotenv(env_file)
        base_url = os.environ.get("SMALLBIZ_MODEL_BASE_URL") or "https://api.deepseek.com"
        model = os.environ.get("SMALLBIZ_MODEL") or "deepseek-chat"
        planned = intake.call_deepseek_route_plan(
            text=text,
            runtime_context=_runtime_context(bindings, draft, text),
            base_url=base_url,
            model=model,
            timeout_s=timeout_s,
        )
        if not isinstance(planned, dict):
            return "", 0.0
        route = str(planned.get("route") or "").strip()
        conf_raw = planned.get("confidence")
        try:
            conf = float(conf_raw)
        except Exception:
            conf = 0.0
        return route, max(0.0, min(1.0, conf))
    except Exception:
        return "", 0.0


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


def _organize_reminder_enabled() -> bool:
    """
    Feature switch for V1(single-table recorder) vs V1.5(organize/reminder).
    - Env: SMALLBIZ_ENABLE_ORGANIZE_REMINDER
      truthy: 1/true/yes/on
      falsy: 0/false/no/off
    - Default:
      * single profile -> disabled
      * otherwise -> enabled
    """
    raw = str(os.environ.get("SMALLBIZ_ENABLE_ORGANIZE_REMINDER") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return not _is_single_table_mode()


def _explicit_dispatch(text: str, bindings: Path, env_file: Path) -> Tuple[bool, Dict[str, Any]]:
    msg = (text or "").strip()
    if not msg:
        return False, {}
    # When a confirmation scene is active, user replies should go back to that scene first.
    # This lets semantic confirmation work on free-form answers like "按你推荐的来".
    # We still let fresh Feishu links bypass so users can explicitly switch context.
    if _has_pending_route(DEFAULT_DRAFT_PATH) and not _RE_FEISHU_LINK.search(msg):
        ok, out, err = _run_json(
            [
                sys.executable,
                str(DRAFT_SCRIPT),
                "ingest",
                "--text",
                msg,
                "--base-url",
                os.environ.get("SMALLBIZ_MODEL_BASE_URL") or "https://api.deepseek.com",
                "--model",
                os.environ.get("SMALLBIZ_MODEL") or "deepseek-chat",
                "--timeout",
                "60",
            ]
        )
        return True, out if ok else {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err}"}
    if _has_pending_route(DEFAULT_DRAFT_PATH):
        compact = msg.replace(" ", "")
        if compact in {"1", "2", "回复1", "回复2", "回复一", "回复二", "写入推荐表", "切到推荐表", "写到推荐表", "仍写当前表", "保持当前表", "保持原表", "取消"}:
            ok, out, err = _run_json(
                [
                    sys.executable,
                    str(DRAFT_SCRIPT),
                    "ingest",
                    "--text",
                    msg,
                    "--base-url",
                    os.environ.get("SMALLBIZ_MODEL_BASE_URL") or "https://api.deepseek.com",
                    "--model",
                    os.environ.get("SMALLBIZ_MODEL") or "deepseek-chat",
                    "--timeout",
                    "60",
                ]
            )
            return True, out if ok else {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err}"}
    if _has_pending_bulk(DEFAULT_DRAFT_PATH) and not _RE_FEISHU_LINK.search(msg):
        ok, out, err = _run_json(
            [
                sys.executable,
                str(DRAFT_SCRIPT),
                "ingest",
                "--text",
                msg,
                "--base-url",
                os.environ.get("SMALLBIZ_MODEL_BASE_URL") or "https://api.deepseek.com",
                "--model",
                os.environ.get("SMALLBIZ_MODEL") or "deepseek-chat",
                "--timeout",
                "60",
            ]
        )
        return True, out if ok else {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err}"}
    if msg in _REPLACE_REPLY_WORDS:
        ok, out, err = _run_json(
            [
                sys.executable,
                str(TABLE_REGISTRY_SCRIPT),
                "--bindings",
                str(bindings),
                "--env-file",
                str(env_file),
                "pending_reply",
                "--choice",
                msg,
            ]
        )
        if ok:
            _ledger_event(
                "table_replace_reply",
                {"choice": msg, "status": str(out.get("status") or "").strip(), "active_binding": _runtime_context(bindings, DEFAULT_DRAFT_PATH, msg).get("active_binding")},
            )
        return True, out if ok else {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err}"}
    if _RE_FEISHU_LINK.search(msg):
        ok, out, err = _run_json([sys.executable, str(TABLE_REGISTRY_SCRIPT), "--bindings", str(bindings), "--env-file", str(env_file), "add", "--url", msg])
        if ok:
            _ledger_event(
                "table_add",
                {
                    "status": str(out.get("status") or "").strip(),
                    "open_url": str(out.get("open_url") or "").strip(),
                    "table": str(out.get("display_name") or "").strip(),
                    "url": msg,
                },
            )
        return True, out if ok else {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err}"}
    if msg in _LIST_WORDS:
        ok, out, err = _run_json([sys.executable, str(TABLE_REGISTRY_SCRIPT), "--bindings", str(bindings), "list"])
        return True, out if ok else {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err}"}
    if msg.startswith("删除表"):
        target = msg[len("删除表"):].strip()
        if not target:
            return True, {"ok": True, "handled": True, "reply_text": "需要确认：请提供要删除的表名或序号"}
        ok, out, err = _run_json([sys.executable, str(TABLE_REGISTRY_SCRIPT), "--bindings", str(bindings), "remove", "--target", target])
        if ok:
            _ledger_event(
                "table_removed",
                {
                    "target": target,
                    "name": str(out.get("name") or "").strip(),
                    "table": str(out.get("display_name") or "").strip(),
                    "open_url": str(out.get("open_url") or "").strip(),
                },
            )
        return True, out if ok else {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err}"}
    if msg in _CONFIRM_WORDS:
        ok, out, err = _run_json([sys.executable, str(DRAFT_SCRIPT), "confirm_all"])
        return True, out if ok else {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err}"}
    if msg in _VOID_WORDS:
        ok, out, err = _run_json([sys.executable, str(DRAFT_SCRIPT), "void_latest"])
        return True, out if ok else {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err}"}
    if msg in _EXIT_WORDS:
        ok, out, err = _run_json([sys.executable, str(DRAFT_SCRIPT), "exit"])
        if not ok:
            return True, {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err}"}
        reply = str(out.get("reply_text") or out.get("receipt_text") or "").strip()
        if not reply:
            reply = "结果：已退出录入模式"
        out["reply_text"] = reply
        return True, out
    if msg in _LEDGER_PREVIEW_WORDS:
        ok, out, err = _run_json([sys.executable, str(OPS_SCRIPT), "ledger"])
        return True, out if ok else {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err}"}
    if msg in _LEDGER_EXPORT_WORDS:
        ok, out, err = _run_json([sys.executable, str(OPS_SCRIPT), "ledger_export", "--days", "0"])
        return True, out if ok else {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err}"}
    if msg in {"最近错误", "错误", "报错", "失败记录"}:
        ok, out, err = _run_json([sys.executable, str(OPS_SCRIPT), "recent_errors"])
        return True, out if ok else {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err}"}
    return False, {}


def run(text: str, bindings: Path, draft: Path, env_file: Path, timeout_s: int) -> Dict[str, Any]:
    explicit, out = _explicit_dispatch(text, bindings, env_file)
    if explicit:
        out.setdefault("handled", True)
        if "reply_text" not in out:
            # normalize across scripts (table_registry uses reply_text, draft may use receipt_text)
            reply = str(out.get("receipt_text") or out.get("reply_text") or "").strip()
            out["reply_text"] = reply
        return out

    # High-risk old-record actions should never drift into organize/reminder or draft ingest.
    if _looks_old_record_action(text):
        return {
            "ok": True,
            "handled": True,
            "route": "unsupported_action_query",
            "reply_text": _unsupported_reply_text(),
        }

    # Default to v2 for stable behavior; can be overridden explicitly.
    force_v2 = str(os.environ.get("SMALLBIZ_FORCE_V2") or "1").strip().lower() not in {"0", "false", "no"}
    mode = str(os.environ.get("SMALLBIZ_ROUTER_MODE") or ("v2" if force_v2 else "v1")).strip().lower()
    router_script = ROUTER_SCRIPT_V2 if mode == "v2" and ROUTER_SCRIPT_V2.exists() else ROUTER_SCRIPT_V1

    ok, route_out, err = _run_json(
        [
            sys.executable,
            str(router_script),
            "--text",
            text,
            "--bindings",
            str(bindings),
            "--draft",
            str(draft),
            "--env-file",
            str(env_file),
            "--timeout",
            str(int(timeout_s)),
        ]
    )
    if not ok:
        return {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err}"}

    route = str(route_out.get("route") or "").strip()
    need_clarify = bool(route_out.get("need_clarify"))
    clarify_q = str(route_out.get("clarify_question") or "").strip()
    if route == "clarify" or need_clarify:
        # In single-table mode, try one semantic rescue before asking user again.
        if _is_single_table_mode():
            ctx = _runtime_context(bindings, draft, text)
            has_active = bool(str(ctx.get("active_binding") or "").strip())
            if has_active and not bool(ctx.get("is_explicit_command")):
                sc_route, sc_conf = _second_chance_route(
                    text=text,
                    bindings=bindings,
                    draft=draft,
                    env_file=env_file,
                    timeout_s=timeout_s,
                )
                if sc_route == "draft_ingest" and sc_conf >= 0.55:
                    ok2, out2, err2 = _run_json([sys.executable, str(DRAFT_SCRIPT), "ingest", "--text", text])
                    if not ok2:
                        return {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err2}", "route": sc_route}
                    out2.setdefault("handled", True)
                    if "reply_text" not in out2:
                        out2["reply_text"] = str(out2.get("receipt_text") or "").strip()
                    return out2
                if sc_route in {"organize_query", "reminder_query"} and sc_conf >= 0.55:
                    _run_json([sys.executable, str(DRAFT_SCRIPT), "exit"])
                    ok2, out2, err2 = _run_json([sys.executable, str(ORGANIZE_SCRIPT), "--text", text])
                    if not ok2:
                        return {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err2}", "route": sc_route}
                    out2.setdefault("handled", True)
                    return out2
        return {"ok": True, "handled": True, "reply_text": (clarify_q or "需要确认：请补充你的目标（录入/整理/提醒）。"), "route": route}
    if route == "register_table":
        ok2, out2, err2 = _run_json([sys.executable, str(TABLE_REGISTRY_SCRIPT), "--bindings", str(bindings), "--env-file", str(env_file), "add", "--url", text])
        if not ok2:
            return {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err2}", "route": route}
        out2.setdefault("handled", True)
        return out2
    if route == "list_tables_query":
        ok2, out2, err2 = _run_json([sys.executable, str(TABLE_REGISTRY_SCRIPT), "--bindings", str(bindings), "list"])
        if not ok2:
            return {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err2}", "route": route}
        out2.setdefault("handled", True)
        return out2
    if route == "current_table_query":
        ok2, out2, err2 = _run_json([sys.executable, str(TABLE_REGISTRY_SCRIPT), "--bindings", str(bindings), "current"])
        if not ok2:
            return {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err2}", "route": route}
        out2.setdefault("handled", True)
        return out2
    if route == "ledger_view_query":
        ok2, out2, err2 = _run_json([sys.executable, str(OPS_SCRIPT), "ledger"])
        if not ok2:
            return {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err2}", "route": route}
        out2.setdefault("handled", True)
        return out2
    if route == "ledger_export_query":
        ok2, out2, err2 = _run_json([sys.executable, str(OPS_SCRIPT), "ledger_export", "--days", "0"])
        if not ok2:
            return {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err2}", "route": route}
        out2.setdefault("handled", True)
        return out2
    if route == "unsupported_action_query":
        return {"ok": True, "handled": True, "route": route, "reply_text": _unsupported_reply_text()}
    if route == "draft_ingest":
        ok2, out2, err2 = _run_json([sys.executable, str(DRAFT_SCRIPT), "ingest", "--text", text])
        if not ok2:
            return {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err2}", "route": route}
        out2.setdefault("handled", True)
        if "reply_text" not in out2:
            out2["reply_text"] = str(out2.get("receipt_text") or "").strip()
        return out2
    if route in {"organize_query", "reminder_query"}:
        if not _organize_reminder_enabled():
            return {
                "ok": True,
                "handled": True,
                "route": route,
                "reply_text": "当前版本仅支持单表登记与录入，暂不支持汇总/提醒功能。",
            }
        # auto switch: leave draft mode first, then organize/reminder
        _run_json([sys.executable, str(DRAFT_SCRIPT), "exit"])
        ok2, out2, err2 = _run_json([sys.executable, str(ORGANIZE_SCRIPT), "--text", text])
        if not ok2:
            return {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err2}", "route": route}
        out2.setdefault("handled", True)
        return out2

    # general_chat second chance:
    # In single-table mode, allow semantic auto-switch without forcing explicit "退出".
    # We only do this for non-command free text to avoid accidental hard-command override.
    if route == "general_chat" and _is_single_table_mode():
        ctx = _runtime_context(bindings, draft, text)
        has_active = bool(str(ctx.get("active_binding") or "").strip())
        if has_active and not bool(ctx.get("is_explicit_command")):
            sc_route, sc_conf = _second_chance_route(
                text=text,
                bindings=bindings,
                draft=draft,
                env_file=env_file,
                timeout_s=timeout_s,
            )
            if sc_route == "draft_ingest" and sc_conf >= 0.55:
                ok2, out2, err2 = _run_json([sys.executable, str(DRAFT_SCRIPT), "ingest", "--text", text])
                if not ok2:
                    return {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err2}", "route": sc_route}
                out2.setdefault("handled", True)
                if "reply_text" not in out2:
                    out2["reply_text"] = str(out2.get("receipt_text") or "").strip()
                return out2
            if sc_route in {"organize_query", "reminder_query"} and sc_conf >= 0.55 and _organize_reminder_enabled():
                _run_json([sys.executable, str(DRAFT_SCRIPT), "exit"])
                ok2, out2, err2 = _run_json([sys.executable, str(ORGANIZE_SCRIPT), "--text", text])
                if not ok2:
                    return {"ok": False, "handled": True, "reply_text": f"结果：执行失败\n原因：{err2}", "route": sc_route}
                out2.setdefault("handled", True)
                return out2

    # general_chat fallback: keep smallbiz path deterministic (no free-form chain-of-thought output)
    return {
        "ok": True,
        "handled": True,
        "route": route,
        "reply_text": (
            "🤔【需要确认】\n"
            "我还不能直接执行这句话。\n\n"
            "我可以做：\n"
            "• 登记表格或切换当前表（直接发送飞书链接即可）\n"
            "• 写入一条记录到当前表（直接发送要写入的完整信息即可）\n"
            "• 查看及导出台账（查看默认最近20条，导出默认最近7天）\n\n"
            "你可以这样说：\n"
            "• “登记这个表链接为当前表”\n"
            "• “写入：李芳 苹果2个 香蕉1根”\n"
            "• “查看台账”\n"
            "• “导出台账”"
        ),
    }


def main(argv: List[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--bindings", default=str(DEFAULT_BINDINGS_PATH))
    ap.add_argument("--draft", default=str(DEFAULT_DRAFT_PATH))
    ap.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    ap.add_argument("--timeout", type=int, default=20)
    args = ap.parse_args(argv)
    out = run(
        text=str(args.text),
        bindings=Path(args.bindings),
        draft=Path(args.draft),
        env_file=Path(args.env_file),
        timeout_s=int(args.timeout),
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
