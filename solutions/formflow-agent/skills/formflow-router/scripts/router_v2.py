#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


DEFAULT_BINDINGS_PATH = Path(os.environ.get("SMALLBIZ_BINDINGS_FILE") or "solutions/formflow-agent/config/bindings.json")
DEFAULT_DRAFT_PATH = Path(os.environ.get("SMALLBIZ_DRAFT_FILE") or "solutions/formflow-agent/data/draft.json")
DEFAULT_ENV_FILE = Path(os.environ.get("SMALLBIZ_ENV_FILE") or "solutions/formflow-agent/.env")
INTAKE_PATH = Path("solutions/formflow-agent/skills/formflow-intake/scripts/intake.py")
DEFAULT_BASE_URL = os.environ.get("SMALLBIZ_MODEL_BASE_URL") or "https://api.deepseek.com"
DEFAULT_MODEL = os.environ.get("SMALLBIZ_MODEL") or "deepseek-chat"

_RE_FEISHU_LINK = re.compile(r"https?://[^\s]*feishu\.cn/(?:base|wiki)/[^\s]+", re.I)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _runtime_context(bindings_path: Path, draft_path: Path, text: str) -> Dict[str, Any]:
    b = _load_json(bindings_path)
    d = _load_json(draft_path)
    active_binding = str(b.get("active_binding") or "").strip()
    bindings = b.get("bindings") if isinstance(b.get("bindings"), list) else []
    binding_names: List[str] = []
    for it in bindings:
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


def _call_primary(intake: Any, *, text: str, ctx: Dict[str, Any], base_url: str, model: str, timeout_s: int) -> Dict[str, Any]:
    planned = intake.call_deepseek_route_plan(
        text=text,
        runtime_context=ctx,
        base_url=base_url,
        model=model,
        timeout_s=timeout_s,
    )
    return planned if isinstance(planned, dict) else {}


def _build_guard_prompt(*, text: str, ctx: Dict[str, Any], primary: Dict[str, Any]) -> Tuple[str, str]:
    system = (
        "你是 smallbiz 路由复核器（guard judge）。\n"
        "你只做复核，不做业务执行。\n"
        "只输出 JSON，不要解释。\n"
        "allowed_route 只能是 register_table / list_tables_query / current_table_query / ledger_view_query / ledger_export_query / draft_ingest / organize_query / reminder_query / unsupported_action_query / general_chat / clarify。\n"
        "如果主路由明显不合理，请给 corrected_route 和一句 reason。"
    )
    user = (
        "请复核下面主路由决策。\n"
        "输出 JSON 模板：\n"
        '{"agree":true,"allowed_route":"general_chat","corrected_route":"","confidence":0.0,"need_clarify":false,"clarify_question":"","reason":""}\n'
        "上下文：\n"
        + json.dumps(ctx, ensure_ascii=False)
        + "\n用户原文：\n"
        + text
        + "\n主路由：\n"
        + json.dumps(primary, ensure_ascii=False)
    )
    return system, user


def _call_guard(intake: Any, *, text: str, ctx: Dict[str, Any], primary: Dict[str, Any], base_url: str, model: str, timeout_s: int) -> Dict[str, Any]:
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("Missing DEEPSEEK_API_KEY.")
    system, user = _build_guard_prompt(text=text, ctx=ctx, primary=primary)
    url = base_url.rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    raw = intake._post_json(url, headers, payload, timeout_s=timeout_s)  # type: ignore[attr-defined]
    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise SystemExit("Empty model content.")
    out = intake.extract_json_from_model_reply(content)
    return out if isinstance(out, dict) else {}


def _clarify_due_to_unavailable(*, text: str, bindings_path: Path, draft_path: Path, reason: str) -> Dict[str, Any]:
    return {
        "ok": True,
        "route": "clarify",
        "confidence": 0.0,
        "need_clarify": True,
        "clarify_question": "我暂时无法稳定判断你的意图，请你补一句：是要录入、整理，还是提醒？",
        "reason": reason,
        "signals": _runtime_context(bindings_path, draft_path, text),
        "judge": {"primary": {}, "guard": {}},
    }


def run(text: str, bindings_path: Path, draft_path: Path, env_file: Path, base_url: str, model: str, timeout_s: int) -> Dict[str, Any]:
    # Hard boundary: links always register.
    if _RE_FEISHU_LINK.search(text or ""):
        ctx = _runtime_context(bindings_path, draft_path, text)
        return {
            "ok": True,
            "route": "register_table",
            "confidence": 1.0,
            "need_clarify": False,
            "clarify_question": "",
            "reason": "hard_boundary_feishu_link",
            "signals": ctx,
            "judge": {"primary": {"route": "register_table"}, "guard": {"agree": True, "allowed_route": "register_table"}},
        }

    if not INTAKE_PATH.exists():
        return _clarify_due_to_unavailable(
            text=text,
            bindings_path=bindings_path,
            draft_path=draft_path,
            reason="intake_missing",
        )

    intake = _load_module(INTAKE_PATH, "smallbiz_intake_router_v2")
    if env_file.exists():
        intake.load_dotenv(env_file)

    ctx = _runtime_context(bindings_path, draft_path, text)
    allowed = {"register_table", "list_tables_query", "current_table_query", "ledger_view_query", "ledger_export_query", "draft_ingest", "organize_query", "reminder_query", "unsupported_action_query", "general_chat", "clarify"}

    try:
        primary = _call_primary(intake, text=text, ctx=ctx, base_url=base_url, model=model, timeout_s=timeout_s)
    except BaseException:
        return _clarify_due_to_unavailable(
            text=text,
            bindings_path=bindings_path,
            draft_path=draft_path,
            reason="primary_unavailable",
        )

    p_route = str(primary.get("route") or "").strip()
    if p_route not in allowed:
        return _clarify_due_to_unavailable(
            text=text,
            bindings_path=bindings_path,
            draft_path=draft_path,
            reason="primary_invalid_route",
        )

    try:
        guard = _call_guard(intake, text=text, ctx=ctx, primary=primary, base_url=base_url, model=model, timeout_s=timeout_s)
    except BaseException:
        guard = {"agree": True, "allowed_route": p_route, "confidence": 0.0, "need_clarify": False, "clarify_question": "", "reason": "guard_unavailable"}

    g_agree = bool(guard.get("agree"))
    g_allowed = str(guard.get("allowed_route") or "").strip()
    g_corrected = str(guard.get("corrected_route") or "").strip()
    if g_allowed not in allowed:
        g_allowed = p_route
    if g_corrected and g_corrected not in allowed:
        g_corrected = ""

    route = p_route
    reason = str(primary.get("reason") or "").strip() or "primary"
    if not g_agree:
        route = g_corrected or g_allowed or p_route
        reason = str(guard.get("reason") or "").strip() or "guard_corrected"

    conf = primary.get("confidence")
    try:
        confidence = float(conf)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    need_clarify = bool(primary.get("need_clarify")) or bool(guard.get("need_clarify"))
    clarify_question = str(guard.get("clarify_question") or primary.get("clarify_question") or "").strip()

    return {
        "ok": True,
        "route": route,
        "confidence": confidence,
        "need_clarify": need_clarify,
        "clarify_question": clarify_question,
        "reason": reason,
        "signals": ctx,
        "judge": {"primary": primary, "guard": guard},
    }


def main(argv: List[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--bindings", default=str(DEFAULT_BINDINGS_PATH))
    ap.add_argument("--draft", default=str(DEFAULT_DRAFT_PATH))
    ap.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=int, default=20)
    args = ap.parse_args(argv)
    out = run(
        text=str(args.text),
        bindings_path=Path(args.bindings),
        draft_path=Path(args.draft),
        env_file=Path(args.env_file),
        base_url=str(args.base_url),
        model=str(args.model),
        timeout_s=int(args.timeout),
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
