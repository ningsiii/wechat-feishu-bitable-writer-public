#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Tuple


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BINDINGS_PATH = "solutions/formflow-agent/config/bindings.json"
DEFAULT_SYNC_SCRIPT = "solutions/formflow-agent/skills/formflow-feishu/scripts/sync_record.py"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        # Only set if not already present in environment.
        os.environ.setdefault(key, value)


def _post_json(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout_s: int) -> Dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise SystemExit(f"HTTP {e.code} calling model API: {detail[:500]}")
    except Exception as e:
        raise SystemExit(f"Failed calling model API: {e}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(f"Model API returned non-JSON: {raw[:500]}")


def build_prompt(source_text: str) -> Tuple[str, str]:
    system = (
        "你是 smallbiz 助手。你的目标是把输入转为可追踪条目，避免漏单与漏跟进。"
        "\n硬性规则："
        "\n1) 只建议、不代发：你可以生成回复草稿，但不得自动发送给客户。"
        "\n2) 只输出 JSON，不要 markdown，不要多余解释。"
        "\n3) 信息不足就写空字符串/空数组/null，并把缺失信息列进 missing_info 与 missing_questions。"
        "\n4) 不要输出价格占位符（如 XX/YY/??）。如果价格未知，用“需要确认价格后再回复您/请问预算/我先确认价格”这类表述。"
    )

    user = (
        "请把下面这段文本转为 smallbiz 工作流 JSON。"
        "\n输出必须严格符合这个 JSON 模板字段："
        '\n{"item":{"title":"","source_text":"","category":"order","priority":"normal","contact_name":"","contact_handle":"","items":[{"name":"","qty":null,"unit":"","spec":""}],"amount":null,"delivery_method":"","delivery_address":"","delivery_time":"","due_at":"","follow_up_at":"","status":"new","next_action":""},"missing_info":[],"missing_questions":[],"reply_draft":""}'
        "\n要求："
        "\n- item.source_text 必须等于原文"
        "\n- item.title 要短且可读"
        "\n- item.items 必须是对象数组：name/qty/unit/spec；信息不足就留空/为 null"
        "\n- missing_questions 写成可直接复制给客户的问句（但不自动发送）"
        "\n原文：\n"
        + source_text
    )
    return system, user


def build_prompt_for_table(*, source_text: str, table_fields: list[dict[str, Any]]) -> Tuple[str, str]:
    """
    Dynamic-schema prompt: write values aligned to the current table header.
    """
    system = (
        "你是 smallbiz 助手。你需要把用户输入整理成“写入飞书多维表格的一行记录”。\n"
        "硬性规则：\n"
        "1) 只输出 JSON，不要 markdown，不要解释。\n"
        "2) 只能写入我给你的字段列表里出现的字段名（field_name）。\n"
        "3) 对于单选/多选字段，只能使用 options 里出现的 name。\n"
        "4) 无法确定的字段留空（空字符串/空数组/null），并把字段名放进 missing_fields。\n"
        "5) 尽可能把原文中的关键事实填入最匹配的字段（例如手机号/性别/姓名/时间/地址）。\n"
        "6) 不要“推断式”填写：除非原文明确出现，否则不要凭空生成【状态/下一步动作/类别/金额/标题】等内容。\n"
        "7) 输出 evidence：每个已填写字段对应的原文片段（不超过 20 字）。\n"
        "7) 给出 confidence（0~1），越不确定越低。\n"
    )

    # Keep prompt small: only field_name/type/options names.
    schema = []
    for f in table_fields or []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("field_name") or "").strip()
        if not name:
            continue
        t = f.get("type")
        opts = f.get("options") if isinstance(f.get("options"), list) else []
        opt_names = [str(o.get("name") or "").strip() for o in opts if isinstance(o, dict) and str(o.get("name") or "").strip()]
        schema.append({"field_name": name, "type": t, "options": opt_names[:30]})

    user = (
        "请根据下面的字段列表，把原文整理成一行记录。\n"
        "输出 JSON 模板：\n"
        '{"fields":{ },"evidence":{ },"missing_fields":[],"confidence":0.0}\n'
        "示例（不要求原文必须带“字段名：”）：\n"
        "- 原文：亮亮 男 13800138000\n"
        "  可能输出：fields.姓名=亮亮, fields.性别=男, fields.电话=13800138000\n"
        "字段列表（只可用这些 field_name）：\n"
        + json.dumps(schema, ensure_ascii=False)
        + "\n原文：\n"
        + source_text
    )
    return system, user


def extract_json_from_model_reply(text: str) -> Dict[str, Any]:
    # Expect pure JSON, but be defensive if the model wraps it.
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last <= first:
        raise SystemExit(f"Model reply does not contain JSON object: {text[:200]}")
    blob = text[first : last + 1]
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Failed parsing model JSON: {e}: {blob[:300]}")
    if not isinstance(parsed, dict):
        raise SystemExit("Model JSON must be an object.")
    return parsed


_ITEM_RE = re.compile(
    r"(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>斤|kg|KG|千克|克|份|盒|袋|桶|个)?\s*(?P<spec>麻辣|十三香|蒜蓉|清蒸|微辣|中辣|重辣)?\s*(?P<name>.+)"
)


def normalize_items(value: Any) -> list[dict[str, Any]]:
    """
    Ensure item.items is a list of objects: {name, qty, unit, spec}.
    Accepts either a list of dicts (preferred) or a list of strings (fallback).
    """
    if isinstance(value, list) and all(isinstance(x, dict) for x in value):
        out: list[dict[str, Any]] = []
        for d in value:
            out.append(
                {
                    "name": str(d.get("name") or "").strip(),
                    "qty": d.get("qty", None),
                    "unit": str(d.get("unit") or "").strip(),
                    "spec": str(d.get("spec") or "").strip(),
                }
            )
        return out

    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        out2: list[dict[str, Any]] = []
        for s in value:
            s = s.strip()
            if not s:
                continue
            m = _ITEM_RE.match(s)
            if not m:
                out2.append({"name": s, "qty": None, "unit": "", "spec": ""})
                continue
            qty_raw = m.group("qty")
            qty = None
            try:
                qty = int(qty_raw) if qty_raw.isdigit() else float(qty_raw)
            except Exception:
                qty = None
            unit = (m.group("unit") or "").strip()
            spec = (m.group("spec") or "").strip()
            name = (m.group("name") or "").strip()
            out2.append({"name": name, "qty": qty, "unit": unit, "spec": spec})
        return out2

    return []


def default_follow_up_at(item: Dict[str, Any], missing_info: Any) -> str:
    """
    Product default (simple & explainable):
    - If missing info exists: follow up in ~2 hours (but not later than 21:00).
    - Otherwise: follow up next day at 10:00.
    - If done/closed: empty.
    """
    from datetime import datetime, timedelta, time

    status = str(item.get("status") or "")
    if status in ("done", "closed"):
        return ""

    now = datetime.now().astimezone()
    has_missing = isinstance(missing_info, list) and any(str(x).strip() for x in missing_info)

    if has_missing:
        candidate = now + timedelta(hours=2)
        if candidate.hour >= 21:
            next_day = (now + timedelta(days=1)).date()
            candidate = datetime.combine(next_day, time(10, 0)).astimezone(now.tzinfo)
        return candidate.isoformat(timespec="seconds")

    next_day = (now + timedelta(days=1)).date()
    candidate2 = datetime.combine(next_day, time(10, 0)).astimezone(now.tzinfo)
    return candidate2.isoformat(timespec="seconds")


def call_deepseek(text: str, base_url: str, model: str, timeout_s: int) -> Dict[str, Any]:
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit(
            "Missing DEEPSEEK_API_KEY.\n"
            "Provide it in one of these ways:\n"
            "  1) Temporary:  DEEPSEEK_API_KEY='...' python3 .../intake.py --text '...'\n"
            "  2) Session:    export DEEPSEEK_API_KEY='...'\n"
            "  3) Project:    create solutions/formflow-agent/.env with DEEPSEEK_API_KEY=...\n"
            "  4) File:       pass --api-key-file solutions/formflow-agent/.secrets/deepseek_api_key\n"
        )
    system, user = build_prompt(text)
    url = base_url.rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    raw = _post_json(url, headers, payload, timeout_s=timeout_s)
    content = (
        raw.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    if not isinstance(content, str) or not content.strip():
        raise SystemExit(f"Empty model content. Raw response keys: {list(raw.keys())}")
    return extract_json_from_model_reply(content)


def call_deepseek_for_table(*, text: str, table_fields: list[dict[str, Any]], base_url: str, model: str, timeout_s: int) -> Dict[str, Any]:
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("Missing DEEPSEEK_API_KEY.")
    system, user = build_prompt_for_table(source_text=text, table_fields=table_fields)
    url = base_url.rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    raw = _post_json(url, headers, payload, timeout_s=timeout_s)
    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise SystemExit("Empty model content.")
    return extract_json_from_model_reply(content)


def build_prompt_pick_table(*, source_text: str, candidates: list[dict[str, Any]]) -> Tuple[str, str]:
    system = (
        "你是 smallbiz 助手。你需要判断“这段原文更适合写入哪一张表”。\n"
        "硬性规则：\n"
        "1) 只输出 JSON，不要解释，不要 markdown。\n"
        "2) best 必须等于 candidates 里某个 name。\n"
        "3) 如果无法判断，选择 current.name 并把 confidence 降低。\n"
        "4) reason 用一句话说明依据。\n"
        "5) 判断时优先综合：表名、字段、以及 recent_examples 里的真实历史样本。\n"
        "6) 如果某张表的 recent_examples 与原文在商品类型、信息结构（如是否带电话/地址）上明显更接近，应优先考虑那张表。\n"
        "7) 不要使用固定词典规则；请基于 candidates 中给出的动态上下文做语义判断。\n"
    )
    user = (
        "根据候选表信息，选择最适合写入的表。\n"
        "输出 JSON 模板：\n"
        '{"best":"","confidence":0.0,"reason":""}\n'
        "candidates：\n"
        + json.dumps(candidates, ensure_ascii=False)
        + "\n原文：\n"
        + source_text
    )
    return system, user


def call_deepseek_pick_table(*, text: str, candidates: list[dict[str, Any]], base_url: str, model: str, timeout_s: int) -> Dict[str, Any]:
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("Missing DEEPSEEK_API_KEY.")
    system, user = build_prompt_pick_table(source_text=text, candidates=candidates)
    url = base_url.rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    raw = _post_json(url, headers, payload, timeout_s=timeout_s)
    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise SystemExit("Empty model content.")
    return extract_json_from_model_reply(content)


def build_prompt_organize_plan(*, source_text: str, table_context: dict[str, Any]) -> Tuple[str, str]:
    system = (
        "你是 smallbiz 的整理规划器。你的任务是把用户自然语言问题，转成可执行的整理计划 JSON。\n"
        "硬性规则：\n"
        "1) 只输出 JSON，不要解释，不要 markdown。\n"
        "2) operation 只能是 group_sum / list_who_what / raw_summary。\n"
        "3) time_basis 只能是 business_time / record_time / hybrid。\n"
        "4) 只有在明确无法安全判断时才 need_clarify=true，并给出一句简短追问。\n"
        "5) fields 里只填表里真实存在的列名；不存在则留空字符串。\n"
    )
    user = (
        "根据下面信息生成计划。\n"
        "输出 JSON 模板：\n"
        '{"intent":"organize","operation":"group_sum","date_expr":"today","target_date":"","time_basis":"hybrid","fields":{"time_field":"","name_field":"","group_field":"","value_field":""},"operation_fields":[],"confidence":0.0,"need_clarify":false,"clarify_question":""}\n'
        "说明：\n"
        "- date_expr 可用 today/tomorrow/explicit/unspecified\n"
        "- target_date 用 YYYY-MM-DD；若无法确定可留空\n"
        "- operation_fields 是本次操作涉及的列名数组\n"
        "表上下文：\n"
        + json.dumps(table_context, ensure_ascii=False)
        + "\n用户原文：\n"
        + source_text
    )
    return system, user


def call_deepseek_organize_plan(*, text: str, table_context: dict[str, Any], base_url: str, model: str, timeout_s: int) -> Dict[str, Any]:
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("Missing DEEPSEEK_API_KEY.")
    system, user = build_prompt_organize_plan(source_text=text, table_context=table_context)
    url = base_url.rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    raw = _post_json(url, headers, payload, timeout_s=timeout_s)
    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise SystemExit("Empty model content.")
    return extract_json_from_model_reply(content)


def build_prompt_route_plan(*, source_text: str, runtime_context: dict[str, Any]) -> Tuple[str, str]:
    system = (
        "你是 smallbiz 路由规划器。你只负责把用户这句话路由到正确能力，不执行具体业务。\n"
        "硬性规则：\n"
        "1) 只输出 JSON，不要 markdown，不要解释。\n"
        "2) route 只能是 register_table / list_tables_query / current_table_query / ledger_view_query / ledger_export_query / draft_ingest / organize_query / reminder_query / unsupported_action_query / general_chat / clarify。\n"
        "2.1) 当前表格工作流的有限能力只有：登记表格、查看已登记表列表、查看当前表、普通记录录入、接龙录入、多表换表确认、确认全部待确认、作废最新一条待确认、查看/导出台账。\n"
        "2.2) 当前不支持的高敏感表格动作包括：修改指定历史记录、删除指定历史记录、更细粒度确认/删除、对已录入记录做追加改写。\n"
        "2.3) 如果一句话不属于 2.1，也不属于 2.2，那么优先把它理解为 general_chat（普通对话或通用问答）。\n"
        "3) 如果是飞书表链接，优先 register_table。\n"
        "3.1) 如果用户是在问“有哪些表/我登记了什么表/表列表/表清单”等，优先 list_tables_query。\n"
        "3.2) 如果用户是在问“当前使用哪张表/现在是什么表/当前表”等，优先 current_table_query。\n"
        "3.3) 如果用户是在问“查看台账/看看台账/账本/最近记录/最近都记了什么”等，优先 ledger_view_query。\n"
        "3.4) 如果用户是在说“导出台账/导出存折/导出账本/导出记录”，优先 ledger_export_query。\n"
        "4) 如果当前 draft_active=true 且语义明显是录入内容，优先 draft_ingest。\n"
        "5) 如果当前 draft_active=true 但语义明显是整理/提醒，优先 organize_query/reminder_query（自动切换优先）。\n"
        "6) 如果用户是在要求更细的待确认处理（例如只确认一条、删除指定记录、全部删除、撤回某条、把刚才那条删掉），优先 unsupported_action_query。\n"
        "7) 如果用户是在要求修改已录入历史记录（例如给某人的那条再加一项、修改已写入内容、在刚才那条里再加一项），优先 unsupported_action_query。\n"
        "8) 如果用户是在闲聊、问天气新闻等通用问题，优先 general_chat，不要误判成表格能力。\n"
        "9) 只有低置信度时 route=clarify，并给一句简短追问。\n"
    )
    user = (
        "请根据上下文生成路由决策。\n"
        "输出 JSON 模板：\n"
        '{"route":"general_chat","confidence":0.0,"need_clarify":false,"clarify_question":"","reason":"","subtype":"","signals":{"has_feishu_link":false,"draft_active":false,"active_binding":"","is_explicit_command":false}}\n'
        "上下文：\n"
        + json.dumps(runtime_context, ensure_ascii=False)
        + "\n用户原文：\n"
        + source_text
    )
    return system, user


def call_deepseek_route_plan(*, text: str, runtime_context: dict[str, Any], base_url: str, model: str, timeout_s: int) -> Dict[str, Any]:
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("Missing DEEPSEEK_API_KEY.")
    system, user = build_prompt_route_plan(source_text=text, runtime_context=runtime_context)
    url = base_url.rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    raw = _post_json(url, headers, payload, timeout_s=timeout_s)
    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise SystemExit("Empty model content.")
    return extract_json_from_model_reply(content)


def build_prompt_pending_choice(*, scene: str, reply_text: str, context: dict[str, Any]) -> Tuple[str, str]:
    if scene in {"route_confirm", "bulk_route_confirm"}:
        intent_desc = "intent 只能是 suggested / current / cancel / unclear。"
        examples = (
            "例子：\n"
            '- 回答“按你推荐的来” -> suggested\n'
            '- 回答“就写那个” -> suggested\n'
            '- 回答“写到水果表”且推荐表就是水果表 -> suggested\n'
            '- 回答“不用换” -> current\n'
            '- 回答“还是原来的” -> current\n'
            '- 回答“取消这次” -> cancel\n'
        )
    else:
        intent_desc = "intent 只能是 split / cancel / unclear。"
        examples = (
            "例子：\n"
            '- 回答“拆吧” -> split\n'
            '- 回答“按这个拆” -> split\n'
            '- 回答“不拆了” -> cancel\n'
            '- 回答“取消这次” -> cancel\n'
        )
    system = (
        "你是 smallbiz 确认回复理解器。你只负责理解用户对当前确认问题的回答，不执行任何业务。\n"
        "硬性规则：\n"
        "1) 只输出 JSON，不要解释，不要 markdown。\n"
        f"2) {intent_desc}\n"
        "3) 结合当前正在问的问题来理解用户回答，不要脱离上下文。\n"
        "4) 如果语义不够清楚，就返回 unclear。\n"
        "5) 优先理解用户的真实意图，不要因为回答里没有出现固定按钮词就返回 unclear。\n"
        + examples
    )
    user = (
        "请根据当前确认场景和用户回答，输出 JSON。\n"
        "输出 JSON 模板：\n"
        '{"intent":"unclear","confidence":0.0,"reason":""}\n'
        "当前场景：\n"
        + scene
        + "\n场景上下文：\n"
        + json.dumps(context, ensure_ascii=False)
        + "\n请注意：用户回答可能不会重复按钮词，但仍然是在回答当前这道确认题。\n"
        + "\n用户回答：\n"
        + reply_text
    )
    return system, user


def call_deepseek_pending_choice(*, scene: str, reply_text: str, context: dict[str, Any], base_url: str, model: str, timeout_s: int) -> Dict[str, Any]:
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("Missing DEEPSEEK_API_KEY.")
    system, user = build_prompt_pending_choice(scene=scene, reply_text=reply_text, context=context)
    url = base_url.rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    raw = _post_json(url, headers, payload, timeout_s=timeout_s)
    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise SystemExit("Empty model content.")
    return extract_json_from_model_reply(content)


def build_prompt_record_entry_gate(*, source_text: str, table_candidates: list[dict[str, Any]]) -> Tuple[str, str]:
    system = (
        "你是 smallbiz 的录入资格判断器。"
        "你只判断这句话是不是一条“新的表格记录内容”，不要执行任何业务。"
        "\n硬性规则："
        "\n1) 只输出 JSON，不要解释，不要 markdown。"
        '\n2) verdict 只能是 entry / not_entry / unclear。'
        '\n3) subtype 只能是 record_modify / pending_action / table_query / general_chat / unclear。'
        "\n4) 如果这句话更像是在删除、撤回、修改上一条/某条已录入记录，不是新增记录，verdict=not_entry。"
        "\n5) 如果这句话更像是在处理待确认动作（尤其是更细粒度的确认/删除），不是新增记录，verdict=not_entry。"
        "\n6) 如果这句话更像是在问表、问台账、闲聊，也不是新增记录，verdict=not_entry。"
        "\n7) 只有当这句话明显像一条新的订单/记录/接龙内容，且至少与某张已登记表的用途/字段/近期样本相匹配时，才 verdict=entry。"
        "\n8) 如果它看起来根本不像任何已登记表会接收的一条新增记录，优先 verdict=not_entry。"
        "\n9) 结合表名、字段和最近样本理解，但不要因为出现个别商品词就忽略整句其实是在操作旧记录。"
    )
    user = (
        "请根据下面候选表上下文，判断这句话是不是一条新的表格记录。\n"
        "输出 JSON 模板：\n"
        '{"verdict":"unclear","subtype":"unclear","confidence":0.0,"reason":""}\n'
        "候选表上下文：\n"
        + json.dumps(table_candidates, ensure_ascii=False)
        + "\n用户原文：\n"
        + source_text
    )
    return system, user


def call_deepseek_record_entry_gate(*, text: str, table_candidates: list[dict[str, Any]], base_url: str, model: str, timeout_s: int) -> Dict[str, Any]:
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("Missing DEEPSEEK_API_KEY.")
    system, user = build_prompt_record_entry_gate(source_text=text, table_candidates=table_candidates)
    url = base_url.rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    raw = _post_json(url, headers, payload, timeout_s=timeout_s)
    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise SystemExit("Empty model content.")
    return extract_json_from_model_reply(content)


def build_prompt_parse_items(*, source_text: str) -> Tuple[str, str]:
    system = (
        "你是信息结构化助手。"
        "你只负责从原文中提取“商品/数量/单位”列表。"
        "只输出 JSON，不要解释。"
        "若无法确定，不要猜测，返回空数组。"
    )
    user = (
        "请抽取原文中的商品条目。\n"
        "输出 JSON 模板：\n"
        '{"items":[{"name":"","qty":0,"unit":""}]}\n'
        "要求：\n"
        "1) qty 必须是数字（可小数）。\n"
        "2) 只提取原文明确出现的内容。\n"
        "3) 单位可为空字符串。\n"
        "原文：\n"
        + source_text
    )
    return system, user


def call_deepseek_parse_items(*, text: str, base_url: str, model: str, timeout_s: int) -> Dict[str, Any]:
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("Missing DEEPSEEK_API_KEY.")
    system, user = build_prompt_parse_items(source_text=text)
    url = base_url.rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    raw = _post_json(url, headers, payload, timeout_s=timeout_s)
    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise SystemExit("Empty model content.")
    out = extract_json_from_model_reply(content)
    if not isinstance(out, dict):
        return {"items": []}
    items = out.get("items")
    if not isinstance(items, list):
        return {"items": []}
    cleaned: list[dict[str, Any]] = []
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
        cleaned.append({"name": name, "qty": qty, "unit": unit})
    return {"items": cleaned}


def save_to_ledger(parsed: Dict[str, Any], ledger_script: str) -> Dict[str, Any]:
    item = parsed.get("item")
    if not isinstance(item, dict):
        raise SystemExit("Output JSON must include object field: item")

    # Normalize items to structured objects for the ledger.
    item["items"] = normalize_items(item.get("items"))

    title = str(item.get("title") or "").strip()
    source_text = str(item.get("source_text") or "").strip()
    if not title or not source_text:
        raise SystemExit("item.title and item.source_text are required.")

    # Put everything except title/source_text into --fields for ledger.py
    fields = item.copy()
    fields.pop("title", None)
    fields.pop("source_text", None)

    # Persist missing info and drafts for audit (still “no auto send”).
    if isinstance(parsed.get("missing_info"), list):
        fields["missing_info"] = parsed["missing_info"]
    if isinstance(parsed.get("missing_questions"), list):
        fields["missing_questions"] = parsed["missing_questions"]
    if isinstance(parsed.get("reply_draft"), str):
        fields["reply_draft"] = parsed["reply_draft"]

    # Product default: if follow_up_at missing, set one so the system can remind.
    if not str(fields.get("follow_up_at") or "").strip():
        fields["follow_up_at"] = default_follow_up_at(item, parsed.get("missing_info"))

    cmd = [
        sys.executable,
        ledger_script,
        "add",
        "--title",
        title,
        "--source-text",
        source_text,
        "--fields",
        json.dumps(fields, ensure_ascii=False),
        "--format",
        "json",
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"Failed saving to ledger: {res.stderr.strip() or res.stdout.strip()}")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return {"ok": True, "raw": res.stdout}


def sync_saved_item(saved: Dict[str, Any], sync_script: str, bindings_path: str, env_file: str) -> Dict[str, Any]:
    if not sync_script.strip() or not bindings_path.strip():
        return {"ok": False, "skipped": True, "reason": "sync_not_configured"}

    sync_script_path = Path(sync_script)
    bindings_file_path = Path(bindings_path)
    if not sync_script_path.exists():
        return {"ok": False, "skipped": True, "reason": f"sync_script_missing:{sync_script}"}
    if not bindings_file_path.exists():
        return {"ok": False, "skipped": True, "reason": f"bindings_missing:{bindings_path}"}

    item = saved.get("item")
    if not isinstance(item, dict):
        return {"ok": False, "skipped": True, "reason": "saved_item_missing"}

    cmd = [
        sys.executable,
        sync_script,
        "--item-json",
        json.dumps(item, ensure_ascii=False),
        "--bindings",
        bindings_path,
    ]
    if env_file.strip():
        cmd.extend(["--env-file", env_file])

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return {"ok": False, "provider": "feishu", "error": res.stderr.strip() or res.stdout.strip()}
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "provider": "feishu", "error": f"non_json_sync_response: {res.stdout[:300]}"}


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True, help="Raw pasted conversation text")
    ap.add_argument("--base-url", default=os.environ.get("SMALLBIZ_MODEL_BASE_URL") or DEFAULT_BASE_URL)
    ap.add_argument("--model", default=os.environ.get("SMALLBIZ_MODEL") or DEFAULT_MODEL)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument(
        "--env-file",
        default=os.environ.get("SMALLBIZ_ENV_FILE") or "solutions/formflow-agent/.env",
        help="Optional .env file path (default: solutions/formflow-agent/.env)",
    )
    ap.add_argument(
        "--api-key-file",
        default=os.environ.get("SMALLBIZ_API_KEY_FILE") or "",
        help="Optional file containing only the DeepSeek API key",
    )
    ap.add_argument(
        "--ledger-script",
        default="solutions/formflow-agent/skills/formflow-ledger/scripts/ledger.py",
    )
    ap.add_argument(
        "--sync-script",
        default=os.environ.get("SMALLBIZ_SYNC_SCRIPT") or DEFAULT_SYNC_SCRIPT,
    )
    ap.add_argument(
        "--bindings",
        default=os.environ.get("SMALLBIZ_BINDINGS_FILE") or DEFAULT_BINDINGS_PATH,
    )
    args = ap.parse_args(argv)

    # Allow project-local config without leaking keys into shell history.
    if args.env_file:
        load_dotenv(Path(args.env_file))
    if args.api_key_file and not (os.environ.get("DEEPSEEK_API_KEY") or "").strip():
        key_path = Path(args.api_key_file)
        if not key_path.exists():
            raise SystemExit(f"--api-key-file not found: {args.api_key_file}")
        os.environ["DEEPSEEK_API_KEY"] = key_path.read_text(encoding="utf-8").strip()

    parsed = call_deepseek(args.text, base_url=args.base_url, model=args.model, timeout_s=args.timeout)
    if isinstance(parsed.get("item"), dict):
        parsed["item"].setdefault("source_text", args.text)
    saved = save_to_ledger(parsed, ledger_script=args.ledger_script)
    synced = sync_saved_item(
        saved,
        sync_script=str(args.sync_script),
        bindings_path=str(args.bindings),
        env_file=str(args.env_file or ""),
    )
    print(json.dumps({"parsed": parsed, "saved": saved, "synced": synced}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
