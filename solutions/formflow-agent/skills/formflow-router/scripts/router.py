#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_BINDINGS_PATH = Path(os.environ.get("SMALLBIZ_BINDINGS_FILE") or "solutions/formflow-agent/config/bindings.json")
DEFAULT_DRAFT_PATH = Path(os.environ.get("SMALLBIZ_DRAFT_FILE") or "solutions/formflow-agent/data/draft.json")
DEFAULT_ENV_FILE = Path(os.environ.get("SMALLBIZ_ENV_FILE") or "solutions/formflow-agent/.env")
INTAKE_PATH = Path("solutions/formflow-agent/skills/formflow-intake/scripts/intake.py")
DEFAULT_BASE_URL = os.environ.get("SMALLBIZ_MODEL_BASE_URL") or "https://api.deepseek.com"
DEFAULT_MODEL = os.environ.get("SMALLBIZ_MODEL") or "deepseek-chat"


_RE_FEISHU_LINK = re.compile(r"https?://[^\s]*feishu\.cn/(?:base|wiki)/[^\s]+", re.I)


def _looks_organize_text(msg: str) -> bool:
    s = (msg or "").strip()
    if not s:
        return False
    keys = (
        "整理",
        "汇总",
        "备货",
        "备哪些",
        "备什么",
        "要备",
        "准备什么",
        "清单",
        "摘要",
        "统计",
        "谁订",
        "都有谁",
        "订了什么",
        "给谁",
        "今天要做哪些",
        "明天要做哪些",
    )
    return any(k in s for k in keys)


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


def _fallback_route(text: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    msg = (text or "").strip()
    if _RE_FEISHU_LINK.search(msg):
        return {"route": "register_table", "confidence": 1.0, "need_clarify": False, "clarify_question": "", "reason": "feishu_link"}
    if bool(ctx.get("draft_active")):
        if _looks_organize_text(msg):
            return {"route": "organize_query", "confidence": 0.85, "need_clarify": False, "clarify_question": "", "reason": "draft_active_auto_switch_to_organize"}
        return {"route": "draft_ingest", "confidence": 0.8, "need_clarify": False, "clarify_question": "", "reason": "draft_active_default"}
    if _looks_organize_text(msg):
        return {"route": "organize_query", "confidence": 0.75, "need_clarify": False, "clarify_question": "", "reason": "organize_keywords"}
    return {"route": "general_chat", "confidence": 0.6, "need_clarify": False, "clarify_question": "", "reason": "fallback_general"}


def run(text: str, bindings_path: Path, draft_path: Path, env_file: Path, base_url: str, model: str, timeout_s: int) -> Dict[str, Any]:
    if not INTAKE_PATH.exists():
        fb = _fallback_route(text, _runtime_context(bindings_path, draft_path, text))
        return {"ok": True, **fb, "signals": _runtime_context(bindings_path, draft_path, text)}
    intake = _load_module(INTAKE_PATH, "smallbiz_intake_router")
    if env_file.exists():
        intake.load_dotenv(env_file)
    ctx = _runtime_context(bindings_path, draft_path, text)
    try:
        planned = intake.call_deepseek_route_plan(
            text=text,
            runtime_context=ctx,
            base_url=base_url,
            model=model,
            timeout_s=timeout_s,
        )
    except BaseException:
        planned = {}

    allowed = {"register_table", "draft_ingest", "organize_query", "reminder_query", "general_chat", "clarify"}
    route = str(planned.get("route") or "").strip()
    if route not in allowed:
        fb = _fallback_route(text, ctx)
        return {"ok": True, **fb, "signals": ctx}

    conf = planned.get("confidence")
    try:
        confidence = float(conf)
    except Exception:
        confidence = 0.0
    if confidence < 0:
        confidence = 0.0
    if confidence > 1:
        confidence = 1.0
    out = {
        "ok": True,
        "route": route,
        "confidence": confidence,
        "need_clarify": bool(planned.get("need_clarify")),
        "clarify_question": str(planned.get("clarify_question") or "").strip(),
        "reason": str(planned.get("reason") or "").strip(),
        "signals": ctx,
    }
    # Safety override: if draft is active and message has strong organize semantics,
    # force organize route to avoid accidental write-in.
    if bool(ctx.get("draft_active")) and route == "draft_ingest" and _looks_organize_text(text):
        out["route"] = "organize_query"
        out["confidence"] = max(float(out.get("confidence") or 0.0), 0.85)
        out["reason"] = "override_organize_semantics"
        out["need_clarify"] = False
        out["clarify_question"] = ""
    return out


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
