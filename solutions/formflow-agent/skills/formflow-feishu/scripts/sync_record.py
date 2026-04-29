#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_BINDINGS_PATH = Path("solutions/formflow-agent/config/bindings.json")
DEFAULT_ENV_FILE = Path(os.environ.get("SMALLBIZ_ENV_FILE") or "solutions/formflow-agent/.env")
_ENV_REF_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")

# Control columns for the "table-as-UI" workflow.
CONFIRM_STATUS_COL = "确认状态"
RECORD_ID_COL = "记录ID"
# Item key used by local workflow to store confirm-state.
# This is separate from the table column name (CONFIRM_STATUS_COL).
CONFIRM_STATE_KEY = "__confirm_state"
# Stored/display values for the confirm status field.
#
# We keep compatibility with older tables that may already contain plain values
# ("待确认"/"已确认"), but new writes use emoji-prefixed values so users can
# recognize pending rows without any conditional-format configuration.
CONFIRM_PENDING_RAW = "待确认"
CONFIRM_CONFIRMED_RAW = "已确认"
# Values written to the table column should stay "raw" so they work for:
# - Text fields
# - Single-select fields (options names must match)
CONFIRM_PENDING = CONFIRM_PENDING_RAW
CONFIRM_CONFIRMED = CONFIRM_CONFIRMED_RAW
# Human-visible label used in the primary field prefix (always a plain text field).
CONFIRM_PENDING_LABEL = "🟡待确认"
CONFIRM_CONFIRMED_LABEL = "✅已确认"

DEFAULT_ALIAS_MAP: Dict[str, List[str]] = {
    # Keep this conservative; avoid mapping business workflow fields like "状态" to payment-like columns.
    "客户": ["姓名", "联系人", "收货人", "下单人", "客户名"],
    "联系方式": ["手机", "手机号", "电话", "联系电话", "收货电话", "微信号", "Tel", "TEL", "tel"],
    "商品/服务": ["商品", "品名", "内容", "订单内容", "产品", "服务", "项目"],
    "时间": ["送达时间", "配送时间", "预约时间", "到货时间", "预定日期", "日期"],
    "地址": ["收货地址", "送货地址", "详细地址", "小区地址", "地址"],
    "备注": ["说明", "留言", "要求", "口味", "备注信息", "备注"],
    "金额": ["总价", "合计", "实收", "付款金额", "金额"],
}


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


def post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout_s: int) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise SystemExit(f"Feishu HTTP {e.code}: {detail[:800]}")
    except Exception as e:
        raise SystemExit(f"Feishu request failed: {e}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(f"Feishu response was not JSON: {raw[:800]}")


def get_json(url: str, headers: Dict[str, str], timeout_s: int) -> Dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise SystemExit(f"Feishu HTTP {e.code}: {detail[:800]}")
    except Exception as e:
        raise SystemExit(f"Feishu request failed: {e}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(f"Feishu response was not JSON: {raw[:800]}")


def load_bindings(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Bindings config not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"Invalid bindings JSON: {e}")
    if not isinstance(data, dict):
        raise SystemExit("Bindings config must be a JSON object.")
    return data


def resolve_binding(data: Dict[str, Any], name: str = "") -> Dict[str, Any]:
    bindings = data.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        raise SystemExit("Bindings config must include a non-empty 'bindings' array.")
    target_name = name or str(data.get("active_binding") or "").strip()
    if not target_name:
        if len(bindings) == 1 and isinstance(bindings[0], dict):
            return bindings[0]
        raise SystemExit("Missing binding name and no active_binding configured.")
    for binding in bindings:
        if isinstance(binding, dict) and str(binding.get("name") or "").strip() == target_name:
            return binding
    raise SystemExit(f"Binding not found: {target_name}")


def expand_env_refs(value: Any) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1), "")
        return _ENV_REF_RE.sub(repl, value)
    if isinstance(value, list):
        return [expand_env_refs(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env_refs(v) for k, v in value.items()}
    return value


def get_tenant_access_token(app_id: str, app_secret: str, timeout_s: int) -> str:
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = {"app_id": app_id, "app_secret": app_secret}
    res = post_json(url, payload, headers={}, timeout_s=timeout_s)
    if int(res.get("code", -1)) != 0:
        raise SystemExit(f"Feishu auth failed: {res.get('msg') or res}")
    token = str(res.get("tenant_access_token") or "").strip()
    if not token:
        raise SystemExit("Feishu auth returned empty tenant_access_token.")
    return token


def stringify_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                qty = str(item.get("qty") or "").strip()
                unit = str(item.get("unit") or "").strip()
                spec = str(item.get("spec") or "").strip()
                text = name
                if qty:
                    text += f" x{qty}{unit}"
                if spec:
                    text += f" ({spec})"
                parts.append(text.strip() or json.dumps(item, ensure_ascii=False))
            else:
                text = stringify_value(item)
                if text:
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def build_record_fields(item: Dict[str, Any], columns: Dict[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for column_name, source_key in columns.items():
        if not isinstance(column_name, str) or not isinstance(source_key, str):
            continue
        value = stringify_value(item.get(source_key))
        # Normalize confirm-state display.
        if column_name.strip() == CONFIRM_STATUS_COL:
            v = value.strip()
            if v == CONFIRM_PENDING_RAW:
                value = CONFIRM_PENDING
            elif v == CONFIRM_CONFIRMED_RAW:
                value = CONFIRM_CONFIRMED
        fields[column_name] = value
    return fields


def resolve_columns_for_table(
    *,
    binding_columns: Dict[str, Any],
    actual_field_names: List[str],
    alias_map: Dict[str, List[str]],
    auto_map: bool,
) -> tuple[Dict[str, Any], Dict[str, str], List[str]]:
    """
    Resolve binding column names against a target table's actual columns.
    - auto_map=True: if binding column is missing but an alias exists in the table, write into the alias column.
    Returns:
    - resolved columns mapping (column_name_in_table -> source_key)
    - used_mappings (binding_column_name -> resolved_column_name)
    - missing binding column names (after alias attempt)
    """
    actual = {str(n).strip() for n in actual_field_names if str(n).strip()}
    resolved: Dict[str, Any] = {}
    used: Dict[str, str] = {}
    missing: List[str] = []

    for want_col, source_key in binding_columns.items():
        if not isinstance(want_col, str) or not isinstance(source_key, str):
            continue
        want = want_col.strip()
        if not want:
            continue
        if want in actual:
            resolved[want] = source_key
            continue
        if auto_map:
            for alias in alias_map.get(want, []):
                alias = str(alias).strip()
                if alias and alias in actual:
                    resolved[alias] = source_key
                    used[want] = alias
                    break
            else:
                missing.append(want)
        else:
            missing.append(want)

    return resolved, used, missing


def create_bitable_record(app_token: str, table_id: str, record_fields: Dict[str, Any], access_token: str, timeout_s: int) -> Dict[str, Any]:
    safe_app_token = urllib.parse.quote(app_token, safe="")
    safe_table_id = urllib.parse.quote(table_id, safe="")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{safe_app_token}/tables/{safe_table_id}/records/batch_create"
    payload = {"records": [{"fields": record_fields}]}
    headers = {"Authorization": f"Bearer {access_token}"}
    res = post_json(url, payload, headers=headers, timeout_s=timeout_s)
    if int(res.get("code", -1)) != 0:
        raise SystemExit(f"Feishu bitable write failed: {res.get('msg') or res}")
    return res


def update_bitable_record(app_token: str, table_id: str, record_id: str, record_fields: Dict[str, Any], access_token: str, timeout_s: int) -> Dict[str, Any]:
    safe_app_token = urllib.parse.quote(app_token, safe="")
    safe_table_id = urllib.parse.quote(table_id, safe="")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{safe_app_token}/tables/{safe_table_id}/records/batch_update"
    payload = {"records": [{"record_id": record_id, "fields": record_fields}]}
    headers = {"Authorization": f"Bearer {access_token}"}
    res = post_json(url, payload, headers=headers, timeout_s=timeout_s)
    if int(res.get("code", -1)) != 0:
        raise SystemExit(f"Feishu bitable update failed: {res.get('msg') or res}")
    return res


def delete_bitable_records(app_token: str, table_id: str, record_ids: List[str], access_token: str, timeout_s: int) -> Dict[str, Any]:
    safe_app_token = urllib.parse.quote(app_token, safe="")
    safe_table_id = urllib.parse.quote(table_id, safe="")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{safe_app_token}/tables/{safe_table_id}/records/batch_delete"
    payload = {"records": record_ids}
    headers = {"Authorization": f"Bearer {access_token}"}
    res = post_json(url, payload, headers=headers, timeout_s=timeout_s)
    if int(res.get("code", -1)) != 0:
        raise SystemExit(f"Feishu bitable delete failed: {res.get('msg') or res}")
    return res


def list_bitable_records(app_token: str, table_id: str, access_token: str, timeout_s: int, page_size: int = 200) -> List[Dict[str, Any]]:
    """
    Minimal record listing for demo / control ops.
    We avoid relying on server-side filter syntax; we fetch pages and filter client-side.
    """
    safe_app_token = urllib.parse.quote(app_token, safe="")
    safe_table_id = urllib.parse.quote(table_id, safe="")
    headers = {"Authorization": f"Bearer {access_token}"}
    page_token = ""
    out: List[Dict[str, Any]] = []
    for _ in range(20):  # hard cap pages to avoid runaway
        qs = {"page_size": str(page_size)}
        if page_token:
            qs["page_token"] = page_token
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{safe_app_token}/tables/{safe_table_id}/records?{urllib.parse.urlencode(qs)}"
        res = get_json(url, headers=headers, timeout_s=timeout_s)
        if int(res.get("code", -1)) != 0:
            raise SystemExit(f"Feishu bitable list records failed: {res.get('msg') or res}")
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        for it in items:
            if isinstance(it, dict):
                out.append(it)
        page_token = str(data.get("page_token") or "").strip()
        if not page_token:
            break
    return out


def _is_effectively_empty_fields(fields: Dict[str, Any]) -> bool:
    """
    A placeholder record typically has an empty fields object {}.
    Be conservative: treat any non-empty value as "non-empty record".
    """
    if not isinstance(fields, dict) or not fields:
        return True
    for _, v in fields.items():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, list) and len(v) == 0:
            continue
        if isinstance(v, dict) and len(v) == 0:
            continue
        return False
    return True


def cleanup_placeholder_records_if_safe(app_token: str, table_id: str, access_token: str, timeout_s: int) -> Dict[str, Any]:
    """
    A1 safe default:
    - Scan a small prefix of records.
    - If we find ANY non-empty record, do nothing (avoid deleting user data).
    - If all scanned records are empty placeholders, delete them.
    This reduces the "new writes start at row 6/7" visual issue on fresh tables.
    """
    scanned = list_bitable_records(app_token, table_id, access_token, timeout_s=timeout_s, page_size=50)
    if not scanned:
        return {"ok": True, "deleted": 0, "scanned": 0, "reason": "no_records"}
    non_empty = False
    empty_ids: List[str] = []
    for r in scanned[:50]:
        if not isinstance(r, dict):
            continue
        fields = r.get("fields") if isinstance(r.get("fields"), dict) else {}
        if _is_effectively_empty_fields(fields):
            rid = str(r.get("record_id") or "").strip()
            if rid:
                empty_ids.append(rid)
        else:
            non_empty = True
            break
    if non_empty:
        return {"ok": True, "deleted": 0, "scanned": len(scanned[:50]), "reason": "table_has_data"}
    if not empty_ids:
        return {"ok": True, "deleted": 0, "scanned": len(scanned[:50]), "reason": "no_empty_placeholders"}
    ids = empty_ids[:50]
    delete_bitable_records(app_token, table_id, ids, access_token, timeout_s=timeout_s)
    return {"ok": True, "deleted": len(ids), "scanned": len(scanned[:50]), "reason": "deleted_placeholders"}


def list_bitable_fields(app_token: str, table_id: str, access_token: str, timeout_s: int) -> Dict[str, str]:
    safe_app_token = urllib.parse.quote(app_token, safe="")
    safe_table_id = urllib.parse.quote(table_id, safe="")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{safe_app_token}/tables/{safe_table_id}/fields"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = get_json(url, headers=headers, timeout_s=timeout_s)
    if int(res.get("code", -1)) != 0:
        raise SystemExit(f"Feishu bitable list fields failed: {res.get('msg') or res}")
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    name_to_id: Dict[str, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        field_id = str(it.get("field_id") or "").strip()
        field_name = str(it.get("field_name") or "").strip()
        if field_id and field_name:
            name_to_id[field_name] = field_id
    return name_to_id


def list_bitable_fields_meta(app_token: str, table_id: str, access_token: str, timeout_s: int) -> List[Dict[str, Any]]:
    """
    Return raw field objects (sanitized) for dynamic-schema writing.
    We intentionally keep only the essentials to keep prompts small.
    """
    safe_app_token = urllib.parse.quote(app_token, safe="")
    safe_table_id = urllib.parse.quote(table_id, safe="")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{safe_app_token}/tables/{safe_table_id}/fields"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = get_json(url, headers=headers, timeout_s=timeout_s)
    if int(res.get("code", -1)) != 0:
        raise SystemExit(f"Feishu bitable list fields failed: {res.get('msg') or res}")
    items = res.get("data", {}).get("items", [])
    if not isinstance(items, list):
        items = []
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        field_name = str(it.get("field_name") or "").strip()
        if not field_name:
            continue
        t = int(it.get("type") or 0)
        prop = it.get("property") if isinstance(it.get("property"), dict) else {}
        options = prop.get("options") if isinstance(prop.get("options"), list) else []
        opt_out: List[Dict[str, Any]] = []
        for op in options:
            if not isinstance(op, dict):
                continue
            n = str(op.get("name") or "").strip()
            if not n:
                continue
            opt_out.append({"name": n, "id": str(op.get("id") or "").strip(), "color": op.get("color")})
        out.append(
            {
                "field_name": field_name,
                "type": t,
                "is_primary": bool(it.get("is_primary") is True),
                "options": opt_out,
            }
        )
    return out


def get_primary_field_name(app_token: str, table_id: str, access_token: str, timeout_s: int) -> str:
    safe_app_token = urllib.parse.quote(app_token, safe="")
    safe_table_id = urllib.parse.quote(table_id, safe="")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{safe_app_token}/tables/{safe_table_id}/fields"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = get_json(url, headers=headers, timeout_s=timeout_s)
    if int(res.get("code", -1)) != 0:
        return ""
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("is_primary") is True:
            return str(it.get("field_name") or "").strip()
    return ""


def map_field_names_to_ids(record_fields_by_name: Dict[str, Any], name_to_id: Dict[str, str]) -> tuple[Dict[str, Any], List[str]]:
    out: Dict[str, Any] = {}
    missing: List[str] = []
    for name, value in record_fields_by_name.items():
        fid = name_to_id.get(str(name))
        if not fid:
            missing.append(str(name))
            continue
        out[fid] = value
    return out, missing


def create_bitable_field(app_token: str, table_id: str, field_name: str, access_token: str, timeout_s: int) -> Dict[str, Any]:
    safe_app_token = urllib.parse.quote(app_token, safe="")
    safe_table_id = urllib.parse.quote(table_id, safe="")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{safe_app_token}/tables/{safe_table_id}/fields"
    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {"field_name": field_name, "type": 1}  # 1 = Text
    return post_json(url, payload, headers=headers, timeout_s=timeout_s)


def create_bitable_single_select_field(app_token: str, table_id: str, field_name: str, option_names: List[str], access_token: str, timeout_s: int) -> Dict[str, Any]:
    """
    Create a single-select field (type=3) with options.
    We only set option names; Feishu will assign ids/colors if omitted.
    """
    safe_app_token = urllib.parse.quote(app_token, safe="")
    safe_table_id = urllib.parse.quote(table_id, safe="")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{safe_app_token}/tables/{safe_table_id}/fields"
    headers = {"Authorization": f"Bearer {access_token}"}
    options = [{"name": str(n).strip()} for n in (option_names or []) if str(n).strip()]
    payload: Dict[str, Any] = {"field_name": field_name, "type": 3, "property": {"options": options}}  # 3 = SingleSelect
    return post_json(url, payload, headers=headers, timeout_s=timeout_s)


def ensure_bitable_fields(app_token: str, table_id: str, desired_field_names: List[str], access_token: str, timeout_s: int) -> Dict[str, Any]:
    name_to_id = list_bitable_fields(app_token, table_id, access_token, timeout_s=timeout_s)
    missing = [n for n in desired_field_names if str(n).strip() and str(n).strip() not in name_to_id]
    created: List[str] = []
    for name in missing:
        name = str(name).strip()
        if not name:
            continue
        res = create_bitable_field(app_token, table_id, name, access_token, timeout_s=timeout_s)
        if int(res.get("code", -1)) == 0:
            created.append(name)
        else:
            # If creation fails (e.g., duplicated), we just proceed; next list call will decide.
            pass
    # Refresh
    name_to_id = list_bitable_fields(app_token, table_id, access_token, timeout_s=timeout_s)
    return {"name_to_id": name_to_id, "created": created}


def ensure_control_fields(app_token: str, table_id: str, access_token: str, timeout_s: int) -> Dict[str, Any]:
    """
    Always ensure the 2 control columns exist. This does not create business columns.
    - 确认状态: prefer single-select (pill UI), but tolerate existing text field.
    - 记录ID: text field
    """
    created: List[str] = []
    name_to_id = list_bitable_fields(app_token, table_id, access_token, timeout_s=timeout_s)

    if CONFIRM_STATUS_COL not in name_to_id:
        res = create_bitable_single_select_field(
            app_token,
            table_id,
            CONFIRM_STATUS_COL,
            option_names=[CONFIRM_PENDING_RAW, CONFIRM_CONFIRMED_RAW],
            access_token=access_token,
            timeout_s=timeout_s,
        )
        if int(res.get("code", -1)) == 0:
            created.append(CONFIRM_STATUS_COL)
        name_to_id = list_bitable_fields(app_token, table_id, access_token, timeout_s=timeout_s)

    if RECORD_ID_COL not in name_to_id:
        res2 = create_bitable_field(app_token, table_id, RECORD_ID_COL, access_token, timeout_s=timeout_s)
        if int(res2.get("code", -1)) == 0:
            created.append(RECORD_ID_COL)
        name_to_id = list_bitable_fields(app_token, table_id, access_token, timeout_s=timeout_s)

    return {"name_to_id": name_to_id, "created": created}


def require_bitable_fields(app_token: str, table_id: str, desired_field_names: List[str], access_token: str, timeout_s: int) -> Dict[str, Any]:
    """
    Strict mode: do not create fields. Fail fast if the target table doesn't match the expected header names.
    This is safer for "template delivery" and avoids polluting the user's table with unexpected columns.
    """
    name_to_id = list_bitable_fields(app_token, table_id, access_token, timeout_s=timeout_s)
    missing = [n for n in desired_field_names if str(n).strip() and str(n).strip() not in name_to_id]
    if missing:
        raise SystemExit(
            "Feishu table header mismatch (missing columns). "
            "Please create/rename these columns to match your bindings.columns keys: "
            + ", ".join(missing[:30])
            + (" ..." if len(missing) > 30 else "")
        )
    return {"name_to_id": name_to_id, "created": []}


def sync_item_to_feishu(item: Dict[str, Any], binding: Dict[str, Any], timeout_s: int) -> Dict[str, Any]:
    binding = expand_env_refs(binding)
    if str(binding.get("provider") or "").strip() != "feishu":
        raise SystemExit(f"Unsupported provider: {binding.get('provider')}")
    app_id = str(os.environ.get("FEISHU_APP_ID") or "").strip()
    app_secret = str(os.environ.get("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        raise SystemExit("Missing FEISHU_APP_ID or FEISHU_APP_SECRET.")
    app_token = str(binding.get("app_token") or "").strip()
    table_id = str(binding.get("table_id") or "").strip()
    columns = binding.get("columns")
    auto_create = binding.get("auto_create_fields")
    if auto_create is None:
        auto_create = True  # backward-compatible default
    auto_map = binding.get("auto_map_fields")
    if auto_map is None:
        auto_map = False  # keep strict by default; enable per binding when desired
    alias_map = binding.get("alias_map") if isinstance(binding.get("alias_map"), dict) else {}
    # Merge defaults, allow binding to override/extend.
    merged_alias_map: Dict[str, List[str]] = {k: v[:] for k, v in DEFAULT_ALIAS_MAP.items()}
    for k, v in alias_map.items():
        if not isinstance(k, str):
            continue
        if isinstance(v, list):
            merged_alias_map[k.strip()] = [str(x) for x in v if str(x).strip()]
    if not app_token or not table_id:
        raise SystemExit("Binding must include app_token and table_id.")
    if not isinstance(columns, dict) or not columns:
        raise SystemExit("Binding must include a non-empty columns mapping.")
    access_token = get_tenant_access_token(app_id, app_secret, timeout_s=timeout_s)
    # Always ensure control columns exist for the table-as-UI workflow.
    ensure_control_fields(app_token, table_id, access_token, timeout_s=timeout_s)
    # Resolve columns against actual table header if requested.
    actual_names = list(list_bitable_fields(app_token, table_id, access_token, timeout_s=timeout_s).keys())
    resolved_columns, used_mappings, missing_cols = resolve_columns_for_table(
        binding_columns=columns,
        actual_field_names=actual_names,
        alias_map=merged_alias_map,
        auto_map=bool(auto_map),
    )
    # If the target table does not have all binding columns and we are not allowed
    # to auto-create or auto-map, we still proceed with the intersection.
    # This keeps the demo unblocked and avoids polluting the user's table.
    # We surface missing columns in the JSON output for troubleshooting.
    record_fields_by_name = build_record_fields(item, resolved_columns)
    # Always fill the primary field (first column) so the record doesn't look "blank" in the grid.
    primary_field = get_primary_field_name(app_token, table_id, access_token, timeout_s=timeout_s)
    if primary_field:
        existing = str(record_fields_by_name.get(primary_field) or "").strip()
        item_id = str(item.get("id") or "").strip()
        status = str(record_fields_by_name.get(CONFIRM_STATUS_COL) or "").strip()
        if status in {CONFIRM_PENDING_RAW, CONFIRM_PENDING}:
            status = CONFIRM_PENDING_LABEL
        elif status in {CONFIRM_CONFIRMED_RAW, CONFIRM_CONFIRMED}:
            status = CONFIRM_CONFIRMED_LABEL
        prefix = ""
        if status:
            prefix = status
        if item_id:
            prefix = f"{prefix} {item_id}".strip() if prefix else item_id
        if existing:
            # Make status/id visible in the first column without requiring column reorder.
            record_fields_by_name[primary_field] = f"{prefix} {existing}".strip() if prefix else existing
        else:
            record_fields_by_name[primary_field] = str(item.get("title") or item_id or "").strip() if not prefix else prefix
    if bool(auto_create):
        ensured = ensure_bitable_fields(
            app_token,
            table_id,
            desired_field_names=list(record_fields_by_name.keys()),
            access_token=access_token,
            timeout_s=timeout_s,
        )
    else:
        ensured = require_bitable_fields(
            app_token,
            table_id,
            desired_field_names=list(record_fields_by_name.keys()),
            access_token=access_token,
            timeout_s=timeout_s,
        )
    # Bitable write APIs accept field names as keys. We ensure fields exist before writing.
    response = create_bitable_record(app_token, table_id, record_fields_by_name, access_token, timeout_s=timeout_s)
    record_id = ""
    try:
        record_id = str(
            (
                response.get("data", {})
                .get("records", [{}])[0]
                .get("record_id", "")
            )
        ).strip()
    except Exception:
        record_id = ""
    open_url = str(binding.get("open_url") or "").strip()
    return {
        "ok": True,
        "provider": "feishu",
        "binding": binding.get("name"),
        "open_url": open_url,
        "record_id": record_id,
        "record_fields": record_fields_by_name,
        "used_column_mappings": used_mappings,
        "created_field_names": ensured.get("created") or [],
        "response": response,
    }


def confirm_all_pending(binding: Dict[str, Any], access_token: str, timeout_s: int) -> Dict[str, Any]:
    app_token = str(binding.get("app_token") or "").strip()
    table_id = str(binding.get("table_id") or "").strip()
    open_url = str(binding.get("open_url") or "").strip()

    ensure_control_fields(app_token, table_id, access_token, timeout_s=timeout_s)
    records = list_bitable_records(app_token, table_id, access_token, timeout_s=timeout_s)
    pending: List[Dict[str, Any]] = []
    for r in records:
        fields = r.get("fields") if isinstance(r.get("fields"), dict) else {}
        v = str(fields.get(CONFIRM_STATUS_COL) or "").strip()
        if v in {CONFIRM_PENDING_RAW, CONFIRM_PENDING, CONFIRM_PENDING_LABEL}:
            pending.append(r)

    ids = [str(r.get("record_id") or "").strip() for r in pending if str(r.get("record_id") or "").strip()]
    if not ids:
        return {"ok": True, "op": "confirm_all_pending", "count": 0, "record_ids": [], "open_url": open_url}

    # Batch update: set confirm status to 已确认
    safe_app_token = urllib.parse.quote(app_token, safe="")
    safe_table_id = urllib.parse.quote(table_id, safe="")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{safe_app_token}/tables/{safe_table_id}/records/batch_update"
    payload = {"records": [{"record_id": rid, "fields": {CONFIRM_STATUS_COL: CONFIRM_CONFIRMED_RAW}} for rid in ids]}
    headers = {"Authorization": f"Bearer {access_token}"}
    res = post_json(url, payload, headers=headers, timeout_s=timeout_s)
    if int(res.get("code", -1)) != 0:
        raise SystemExit(f"Feishu bitable confirm failed: {res.get('msg') or res}")
    return {"ok": True, "op": "confirm_all_pending", "count": len(ids), "record_ids": ids, "open_url": open_url}


def void_latest_pending(binding: Dict[str, Any], access_token: str, timeout_s: int, prefer_record_id: str = "") -> Dict[str, Any]:
    app_token = str(binding.get("app_token") or "").strip()
    table_id = str(binding.get("table_id") or "").strip()
    open_url = str(binding.get("open_url") or "").strip()

    ensure_control_fields(app_token, table_id, access_token, timeout_s=timeout_s)

    if prefer_record_id:
        delete_bitable_records(app_token, table_id, [prefer_record_id], access_token, timeout_s=timeout_s)
        return {"ok": True, "op": "void_latest_pending", "count": 1, "record_id": prefer_record_id, "open_url": open_url}

    records = list_bitable_records(app_token, table_id, access_token, timeout_s=timeout_s)
    pending: List[Dict[str, Any]] = []
    for r in records:
        fields = r.get("fields") if isinstance(r.get("fields"), dict) else {}
        v = str(fields.get(CONFIRM_STATUS_COL) or "").strip()
        if v in {CONFIRM_PENDING_RAW, CONFIRM_PENDING, CONFIRM_PENDING_LABEL}:
            pending.append(r)
    if not pending:
        return {"ok": True, "op": "void_latest_pending", "count": 0, "record_id": "", "open_url": open_url}

    def key_of(r: Dict[str, Any]) -> str:
        fields = r.get("fields") if isinstance(r.get("fields"), dict) else {}
        rid = str(fields.get(RECORD_ID_COL) or "").strip()
        return rid or str(r.get("record_id") or "").strip()

    latest = max(pending, key=key_of)
    rid = str(latest.get("record_id") or "").strip()
    if not rid:
        return {"ok": True, "op": "void_latest_pending", "count": 0, "record_id": "", "open_url": open_url}
    delete_bitable_records(app_token, table_id, [rid], access_token, timeout_s=timeout_s)
    return {"ok": True, "op": "void_latest_pending", "count": 1, "record_id": rid, "open_url": open_url}


def main(argv: List[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--op",
        default="create",
        choices=[
            "create",
            "update",
            "delete",
            "confirm_all_pending",
            "void_latest_pending",
            "ensure_control_fields",
            "fields_meta",
            "create_dynamic",
        ],
    )
    ap.add_argument("--item-json", default="")
    ap.add_argument("--fields-json", default="")
    ap.add_argument("--record-id", default="")
    ap.add_argument("--record-ids", default="")
    ap.add_argument("--prefer-record-id", default="")
    ap.add_argument("--bindings", default=str(DEFAULT_BINDINGS_PATH))
    ap.add_argument("--binding-name", default="")
    ap.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    ap.add_argument("--timeout", type=int, default=20)
    args = ap.parse_args(argv)

    if args.env_file:
        load_dotenv(Path(args.env_file))

    bindings = load_bindings(Path(args.bindings))
    binding = resolve_binding(bindings, name=args.binding_name)

    binding = expand_env_refs(binding)
    if str(binding.get("provider") or "").strip() != "feishu":
        raise SystemExit(f"Unsupported provider: {binding.get('provider')}")
    app_id = str(os.environ.get("FEISHU_APP_ID") or "").strip()
    app_secret = str(os.environ.get("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        raise SystemExit("Missing FEISHU_APP_ID or FEISHU_APP_SECRET.")
    app_token = str(binding.get("app_token") or "").strip()
    table_id = str(binding.get("table_id") or "").strip()
    columns = binding.get("columns")
    auto_create = binding.get("auto_create_fields")
    if auto_create is None:
        auto_create = True  # backward-compatible default
    auto_map = binding.get("auto_map_fields")
    if auto_map is None:
        auto_map = False  # keep strict by default; enable per binding when desired
    alias_map = binding.get("alias_map") if isinstance(binding.get("alias_map"), dict) else {}
    # Merge defaults, allow binding to override/extend.
    merged_alias_map: Dict[str, List[str]] = {k: v[:] for k, v in DEFAULT_ALIAS_MAP.items()}
    for k, v in alias_map.items():
        if not isinstance(k, str):
            continue
        if isinstance(v, list):
            merged_alias_map[k.strip()] = [str(x) for x in v if str(x).strip()]
    if not app_token or not table_id:
        raise SystemExit("Binding must include app_token and table_id.")
    if not isinstance(columns, dict) or not columns:
        raise SystemExit("Binding must include a non-empty columns mapping.")
    access_token = get_tenant_access_token(app_id, app_secret, timeout_s=int(args.timeout))

    op = str(args.op or "").strip()
    if op == "ensure_control_fields":
        # Only ensure the 2 system columns exist. This is used at "table registration" time
        # so the first write doesn't fail due to missing columns.
        ensured = ensure_control_fields(app_token, table_id, access_token, timeout_s=int(args.timeout))
        open_url = str(binding.get("open_url") or "").strip()
        out = {
            "ok": True,
            "op": "ensure_control_fields",
            "provider": "feishu",
            "binding": binding.get("name"),
            "open_url": open_url,
            "created_field_names": ensured.get("created") or [],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if op == "fields_meta":
        meta = list_bitable_fields_meta(app_token, table_id, access_token, timeout_s=int(args.timeout))
        out = {
            "ok": True,
            "op": "fields_meta",
            "provider": "feishu",
            "binding": binding.get("name"),
            "open_url": str(binding.get("open_url") or "").strip(),
            "fields": meta,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if op == "confirm_all_pending":
        out = confirm_all_pending(binding, access_token, timeout_s=int(args.timeout))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if op == "void_latest_pending":
        out = void_latest_pending(binding, access_token, timeout_s=int(args.timeout), prefer_record_id=str(args.prefer_record_id or "").strip())
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if op in ("create", "update", "create_dynamic"):
        if not str(args.item_json or "").strip():
            raise SystemExit("--item-json is required for create/update.")
        try:
            item = json.loads(args.item_json)
        except json.JSONDecodeError as e:
            raise SystemExit(f"Invalid --item-json: {e}")
        if not isinstance(item, dict):
            raise SystemExit("--item-json must decode to an object.")

        # Always ensure control columns exist.
        ensure_control_fields(app_token, table_id, access_token, timeout_s=int(args.timeout))
        # A1: clean empty placeholder rows on fresh tables to avoid "writes start at row 6/7" visual issue.
        if op in ("create", "create_dynamic"):
            try:
                cleanup_placeholder_records_if_safe(app_token, table_id, access_token, timeout_s=int(args.timeout))
            except Exception:
                # Best-effort only; never block writes.
                pass
        actual_names = list(list_bitable_fields(app_token, table_id, access_token, timeout_s=int(args.timeout)).keys())
        resolved_columns, used_mappings, missing_cols = resolve_columns_for_table(
            binding_columns=columns,
            actual_field_names=actual_names,
            alias_map=merged_alias_map,
            auto_map=bool(auto_map),
        )
        ignored_fields: List[str] = []
        if op == "create_dynamic":
            try:
                raw_fields = json.loads(args.fields_json or "{}")
            except json.JSONDecodeError as e:
                raise SystemExit(f"Invalid --fields-json: {e}")
            if not isinstance(raw_fields, dict):
                raise SystemExit("--fields-json must decode to an object.")
            actual_set = {str(n).strip() for n in actual_names if str(n).strip()}
            record_fields_by_name: Dict[str, Any] = {}
            for k, v in raw_fields.items():
                name = str(k or "").strip()
                if not name:
                    continue
                if name in actual_set:
                    record_fields_by_name[name] = v
                else:
                    ignored_fields.append(name)
            # Always include system fields.
            item_id = str(item.get("id") or "").strip()
            if item_id and RECORD_ID_COL in actual_set:
                record_fields_by_name.setdefault(RECORD_ID_COL, item_id)
            want_state = str(item.get(CONFIRM_STATE_KEY) or CONFIRM_PENDING_RAW).strip()
            # Normalize any legacy/emoji variants to raw option values.
            if want_state not in (CONFIRM_PENDING_RAW, CONFIRM_CONFIRMED_RAW):
                if "待确认" in want_state:
                    want_state = CONFIRM_PENDING_RAW
                elif "已确认" in want_state:
                    want_state = CONFIRM_CONFIRMED_RAW
                else:
                    want_state = CONFIRM_PENDING_RAW
            if CONFIRM_STATUS_COL in actual_set:
                record_fields_by_name.setdefault(CONFIRM_STATUS_COL, want_state)
        else:
            # If the table doesn't match the full binding header and we're not allowed to
            # auto-create/auto-map, proceed with the intersection to keep the demo unblocked.
            record_fields_by_name = build_record_fields(item, resolved_columns)

        # Make status/id visible in the first column without requiring column reorder.
        primary_field = get_primary_field_name(app_token, table_id, access_token, timeout_s=int(args.timeout))
        if primary_field:
            existing = str(record_fields_by_name.get(primary_field) or "").strip()
            item_id = str(item.get("id") or "").strip()
            status = str(record_fields_by_name.get(CONFIRM_STATUS_COL) or "").strip()
            if status in {CONFIRM_PENDING_RAW, CONFIRM_PENDING}:
                status = CONFIRM_PENDING_LABEL
            elif status in {CONFIRM_CONFIRMED_RAW, CONFIRM_CONFIRMED}:
                status = CONFIRM_CONFIRMED_LABEL
            prefix = ""
            if status:
                prefix = status
            if item_id:
                prefix = f"{prefix} {item_id}".strip() if prefix else item_id
            if existing:
                record_fields_by_name[primary_field] = f"{prefix} {existing}".strip() if prefix else existing
            else:
                record_fields_by_name[primary_field] = str(item.get("title") or item_id or "").strip() if not prefix else prefix
        if bool(auto_create):
            ensured = ensure_bitable_fields(
                app_token,
                table_id,
                desired_field_names=list(record_fields_by_name.keys()),
                access_token=access_token,
                timeout_s=int(args.timeout),
            )
        else:
            ensured = require_bitable_fields(
                app_token,
                table_id,
                desired_field_names=list(record_fields_by_name.keys()),
                access_token=access_token,
                timeout_s=int(args.timeout),
            )

        if op in ("create", "create_dynamic"):
            response = create_bitable_record(app_token, table_id, record_fields_by_name, access_token, timeout_s=int(args.timeout))
        else:
            rid = str(args.record_id or "").strip()
            if not rid:
                raise SystemExit("--record-id is required for update.")
            response = update_bitable_record(app_token, table_id, rid, record_fields_by_name, access_token, timeout_s=int(args.timeout))

        record_id = str(args.record_id or "").strip()
        if not record_id:
            try:
                record_id = str(response.get("data", {}).get("records", [{}])[0].get("record_id", "")).strip()
            except Exception:
                record_id = ""

        open_url = str(binding.get("open_url") or "").strip()
        out = {
            "ok": True,
            "provider": "feishu",
            "binding": binding.get("name"),
            "open_url": open_url,
            "record_id": record_id,
            "record_fields": record_fields_by_name,
            "used_column_mappings": used_mappings,
            "missing_binding_columns": missing_cols,
            "ignored_fields": ignored_fields,
            "created_field_names": ensured.get("created") or [],
            "response": response,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    if op == "delete":
        ids: List[str] = []
        if str(args.record_id or "").strip():
            ids.append(str(args.record_id).strip())
        if str(args.record_ids or "").strip():
            ids.extend([s.strip() for s in str(args.record_ids).split(",") if s.strip()])
        ids = [s for s in ids if s]
        if not ids:
            raise SystemExit("delete requires --record-id or --record-ids.")
        response = delete_bitable_records(app_token, table_id, ids, access_token, timeout_s=int(args.timeout))
        open_url = str(binding.get("open_url") or "").strip()
        out = {
            "ok": True,
            "provider": "feishu",
            "binding": binding.get("name"),
            "open_url": open_url,
            "record_ids": ids,
            "response": response,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    raise SystemExit(f"Unknown op: {op}")


if __name__ == "__main__":
    main(sys.argv[1:])
