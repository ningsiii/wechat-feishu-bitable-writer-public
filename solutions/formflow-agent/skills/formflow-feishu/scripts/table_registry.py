#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_BINDINGS_PATH = Path(os.environ.get("SMALLBIZ_BINDINGS_FILE") or "solutions/formflow-agent/config/bindings.json")
DEFAULT_ENV_FILE = Path(os.environ.get("SMALLBIZ_ENV_FILE") or "solutions/formflow-agent/.env")
DEFAULT_SYNC_SCRIPT = Path(os.environ.get("SMALLBIZ_SYNC_SCRIPT") or "solutions/formflow-agent/skills/formflow-feishu/scripts/sync_record.py")
DEFAULT_SIDECAR_SCRIPT = Path("solutions/formflow-agent/skills/formflow-organize/scripts/sidecar.py")
FEATURE_FLAGS_PATH = Path("solutions/formflow-agent/config/feature-flags.json")

# NOTE: this regex is intentionally permissive. We only need to extract app_token from any
# Feishu base URL that contains `/base/<app_token>?...`. Use `\s` (whitespace) rather than `\\s`.
BASE_URL_RE = re.compile(r"https?://[^\s]*feishu\.cn/base/([A-Za-z0-9]+)\?[^\s]*")
WIKI_URL_RE = re.compile(r"https?://(?P<host>[^/\s]+)/*wiki/(?P<wiki_token>[A-Za-z0-9]+)(?:\?(?P<query>[^\s#]+))?")


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


def _ensure_control_fields_for_binding(*, bindings_path: Path, binding_name: str, env_file: Path, timeout_s: int) -> Dict[str, Any]:
    """
    Product rule: when a table is registered, auto-create the 2 system columns:
    - 确认状态
    - 记录ID

    We reuse sync_record.py to avoid duplicating Feishu Bitable API calls here.
    """
    cmd: List[str] = [
        sys.executable,
        str(DEFAULT_SYNC_SCRIPT),
        "--op",
        "ensure_control_fields",
        "--bindings",
        str(bindings_path),
        "--binding-name",
        str(binding_name),
        "--timeout",
        str(int(timeout_s)),
    ]
    if env_file and env_file.exists():
        cmd.extend(["--env-file", str(env_file)])
    p = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr.strip() or p.stdout.strip() or "unknown_error")}
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"ok": False, "error": f"non_json_sync_response: {p.stdout[:500]}"}


def _init_sidecar_for_binding(*, bindings_path: Path, binding_name: str, env_file: Path, timeout_s: int) -> Dict[str, Any]:
    cmd: List[str] = [
        sys.executable,
        str(DEFAULT_SIDECAR_SCRIPT),
        "backfill_binding",
        "--bindings",
        str(bindings_path),
        "--binding-name",
        str(binding_name),
        "--timeout",
        str(int(timeout_s)),
    ]
    if env_file and env_file.exists():
        cmd.extend(["--env-file", str(env_file)])
    p = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr.strip() or p.stdout.strip() or "unknown_error")}
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"ok": False, "error": f"non_json_sidecar_response: {p.stdout[:500]}"}


def post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout_s: int) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in headers.items():
        req.add_header(k, v)
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
    for k, v in headers.items():
        req.add_header(k, v)
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


def parse_base_url(url: str) -> Tuple[str, str, str]:
    """
    Returns (app_token, table_id, open_url).
    """
    url = url.strip()
    if not url:
        raise SystemExit("Empty url.")
    m = BASE_URL_RE.search(url)
    if not m:
        raise SystemExit("Not a Feishu base URL (expected ...feishu.cn/base/<app_token>?table=<table_id>...).")
    app_token = m.group(1).strip()
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    table_vals = qs.get("table") or []
    if not table_vals:
        raise SystemExit("Missing table= in URL.")
    table_id = str(table_vals[0]).strip()
    open_url = url
    return app_token, table_id, open_url


def parse_wiki_url(url: str) -> Dict[str, str]:
    """
    Parse a Feishu wiki URL.

    Example:
      https://<host>.feishu.cn/wiki/<wiki_token>?table=<table_id>&view=<view_id>

    Returns:
      {
        "host": "...feishu.cn",
        "wiki_token": "...",
        "table_id": "tbl...",
        "view_id": "vew...",
        "open_url": "<original>"
      }
    """
    url = (url or "").strip()
    m = WIKI_URL_RE.search(url)
    if not m:
        raise SystemExit("Not a Feishu wiki URL.")
    host = (m.group("host") or "").strip()
    wiki_token = (m.group("wiki_token") or "").strip()
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    table_id = str((qs.get("table") or [""])[0] or "").strip()
    view_id = str((qs.get("view") or [""])[0] or "").strip()
    return {"host": host, "wiki_token": wiki_token, "table_id": table_id, "view_id": view_id, "open_url": url}


def get_wiki_node(*, wiki_token: str, access_token: str, timeout_s: int) -> Dict[str, Any]:
    """
    Resolve a wiki node token to its underlying object.

    Uses:
      GET https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?obj_type=wiki&token=<wiki_token>
    """
    safe_token = urllib.parse.quote(wiki_token, safe="")
    url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?obj_type=wiki&token={safe_token}"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = get_json(url, headers=headers, timeout_s=timeout_s)
    if int(res.get("code", -1)) != 0:
        raise SystemExit(f"Feishu wiki get_node failed: {res.get('msg') or res}")
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    node = data.get("node") if isinstance(data.get("node"), dict) else {}
    return node


def _base_open_url_from_parts(*, host: str, app_token: str, table_id: str, view_id: str) -> str:
    base = f"https://{host}/base/{app_token}?table={urllib.parse.quote(table_id, safe='')}"
    if view_id:
        base += f"&view={urllib.parse.quote(view_id, safe='')}"
    return base


def resolve_wiki_to_bitable(
    *, url: str, access_token: str, timeout_s: int
) -> Tuple[str, str, str, str]:
    """
    Best-effort: accept wiki URL and resolve it to a Bitable app_token/table_id.

    Returns:
      (app_token, table_id, open_url_base, node_title)
    """
    info = parse_wiki_url(url)
    host = info["host"]
    wiki_token = info["wiki_token"]
    table_id = info["table_id"]
    view_id = info["view_id"]

    node = get_wiki_node(wiki_token=wiki_token, access_token=access_token, timeout_s=timeout_s)
    obj_type = str(node.get("obj_type") or "").strip()
    obj_token = str(node.get("obj_token") or "").strip()
    title = str(node.get("title") or "").strip()

    if obj_type != "bitable" or not obj_token:
        # v1: only support wiki nodes that directly point to a bitable.
        raise SystemExit(
            f"该链接是知识库（wiki）节点，但不是多维表格节点（obj_type={obj_type or 'unknown'}）。"
            "请在多维表格本体页面复制 base 链接（地址包含 /base/ 且有 table=）。"
        )

    app_token = obj_token
    if not table_id:
        # If user copied a wiki node without table=, pick the first visible table for this bitable.
        tables = list_tables(app_token, access_token, timeout_s=timeout_s)
        if not tables:
            raise SystemExit("该多维表格下没有可用数据表（table）。请在飞书里确认表是否存在。")
        table_id = str(tables[0].get("table_id") or "").strip()
        if not table_id:
            raise SystemExit("无法从飞书返回结果中解析 table_id。")

    open_url_base = _base_open_url_from_parts(host=host, app_token=app_token, table_id=table_id, view_id=view_id)
    return app_token, table_id, open_url_base, title


def list_tables(app_token: str, access_token: str, timeout_s: int) -> List[Dict[str, Any]]:
    safe_app_token = urllib.parse.quote(app_token, safe="")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{safe_app_token}/tables?page_size=100"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = get_json(url, headers=headers, timeout_s=timeout_s)
    if int(res.get("code", -1)) != 0:
        raise SystemExit(f"Feishu list tables failed: {res.get('msg') or res}")
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    out: List[Dict[str, Any]] = []
    for it in items:
        if isinstance(it, dict):
            out.append(it)
    return out


def get_app_meta(app_token: str, access_token: str, timeout_s: int) -> Dict[str, Any]:
    safe_app_token = urllib.parse.quote(app_token, safe="")
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{safe_app_token}"
    headers = {"Authorization": f"Bearer {access_token}"}
    res = get_json(url, headers=headers, timeout_s=timeout_s)
    if int(res.get("code", -1)) != 0:
        raise SystemExit(f"Feishu get app meta failed: {res.get('msg') or res}")
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    app = data.get("app") if isinstance(data.get("app"), dict) else {}
    return app


def load_bindings(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"active_binding": "", "bindings": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"active_binding": "", "bindings": []}
    if not isinstance(data, dict):
        return {"active_binding": "", "bindings": []}
    data.setdefault("active_binding", "")
    data.setdefault("bindings", [])
    data.setdefault("pending_replace", {})
    if not isinstance(data.get("bindings"), list):
        data["bindings"] = []
    if not isinstance(data.get("pending_replace"), dict):
        data["pending_replace"] = {}
    return data


def save_bindings(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def default_columns_mapping() -> Dict[str, str]:
    # Standard smallbiz header keys (A-template). B-mode can map these keys into alias columns.
    return {
        "记录ID": "id",
        "创建时间": "created_at",
        "确认状态": "__confirm_state",
        "标题": "title",
        "类别": "category",
        "客户": "contact_name",
        "联系方式": "contact_handle",
        "商品/服务": "items",
        "时间": "delivery_time",
        "地址": "delivery_address",
        "金额": "amount",
        "状态": "status",
        "下一步动作": "next_action",
        "跟进时间": "follow_up_at",
        "备注": "notes",
    }


def _make_unique_name(existing_names: set[str], base: str, suffix: str) -> str:
    base = (base or "").strip() or "未命名表"
    candidate = base
    if candidate in existing_names:
        candidate = f"{base}-{suffix}"
    i = 2
    while candidate in existing_names:
        candidate = f"{base}-{suffix}-{i}"
        i += 1
    return candidate


def _binding_display_name(b: Dict[str, Any]) -> str:
    # Prefer base(file) title, then fallback to binding name.
    dn = str(b.get("display_name") or "").strip()
    if dn:
        return dn
    n = str(b.get("name") or "").strip()
    return n or "未命名表"


def _render_table_list(bindings: List[Dict[str, Any]], active_name: str) -> str:
    lines: List[str] = []
    for i, b in enumerate(bindings, start=1):
        name = _binding_display_name(b)
        url = str(b.get("open_url") or "").strip()
        lines.append(f"{i}) {name}")
        if url:
            lines.append(f"   链接：{url}")
    return "\n".join(lines) if lines else "(空)"


def _reply_add(display_name: str, index: int, open_url: str, list_text: str) -> str:
    # receipt kept concise; caller decides single-table vs multi-table semantics
    return _fmt_success(
        result="已添加表单",
        table=display_name,
        link=open_url,
    )


def _fmt_success(*, result: str, table: str = "", link: str = "", extra: Optional[List[str]] = None) -> str:
    lines: List[str] = ["✅【表单回执】", f"结果：{result}"]
    t = (table or "").strip()
    if t:
        lines.append(f"📍 当前表：{t}")
    u = (link or "").strip()
    if u:
        lines.append(f"🔗 链接：{u}")
    if extra:
        lines.append("")
        for e in extra:
            e2 = str(e or "").strip()
            if e2:
                lines.append(e2)
    return "\n".join(lines)


def _fmt_clarify(question: str) -> str:
    return "\n".join(
        [
            "🤔【需要确认】",
            str(question or "").strip(),
            "",
            "请回复：",
            "• 1",
            "• 2",
        ]
    )


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


def _reply_add_multi(display_name: str, open_url: str, list_text: str) -> str:
    lines = [
        "✅【表单回执】",
        "结果：已登记表格",
        f"📄 表格：{display_name}",
        f"🔗 链接：{open_url}",
        "",
        "📋 已登记表：",
        list_text or "(空)",
    ]
    return "\n".join(lines)


def _reply_existing_multi(display_name: str, open_url: str, list_text: str) -> str:
    lines = [
        "✅【表单回执】",
        "结果：这张表已登记",
        f"📄 表格：{display_name}",
        f"🔗 链接：{open_url}",
        "",
        "📋 已登记表：",
        list_text or "(空)",
    ]
    return "\n".join(lines)


def _humanize_registry_error(raw: str) -> str:
    s = (raw or "").strip()
    low = s.lower()
    if "not a feishu base url" in low or "not a feishu wiki url" in low:
        return "❌【执行失败】\n原因：这不是可用的飞书多维表链接\n💡 建议：请发送包含 /base/ 或 /wiki/ 的飞书多维表链接重试。"
    if "missing table=" in low:
        return "❌【执行失败】\n原因：链接里缺少 table 参数\n💡 建议：请重新复制完整的飞书多维表链接重试。"
    if "obj_type=" in s or "不是多维表格节点" in s:
        return f"❌【执行失败】\n原因：{s}"
    if "forbidden" in low or "permission" in low or "权限" in s:
        return "❌【执行失败】\n原因：当前飞书应用没有读取该链接对应资源的权限\n💡 建议：请确认知识库节点可读、多维表可编辑后重试。"
    return f"❌【执行失败】\n原因：{s or '未知错误'}"


def _maybe_replace_legacy_name(current_name: str, new_name: str) -> bool:
    """
    Older demos used placeholder binding names (e.g. smallbiz_order_demo).
    We only auto-replace if it looks like a legacy placeholder.
    """
    cur = (current_name or "").strip().lower()
    if not cur:
        return True
    if cur in {"smallbiz_order_demo", "smallbizorderdemo"}:
        return True
    if cur.startswith("表-") or cur.startswith("未命名表"):
        return True
    if cur.startswith("tbl") or cur.startswith("basc"):
        return True
    return False


def cmd_add(args: argparse.Namespace) -> Dict[str, Any]:
    single_table_mode = _is_single_table_mode()
    if args.env_file:
        load_dotenv(Path(args.env_file))
    app_id = str(os.environ.get("FEISHU_APP_ID") or "").strip()
    app_secret = str(os.environ.get("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        raise SystemExit("Missing FEISHU_APP_ID or FEISHU_APP_SECRET.")

    access_token = get_tenant_access_token(app_id, app_secret, timeout_s=int(args.timeout))
    try:
        app_token, table_id, open_url = parse_base_url(args.url)
    except SystemExit:
        # B2: also accept wiki links (knowledge base nodes) if they point to a bitable.
        app_token, table_id, open_url, _node_title = resolve_wiki_to_bitable(
            url=args.url, access_token=access_token, timeout_s=int(args.timeout)
        )

    app_name = ""
    table_name = ""
    try:
        app = get_app_meta(app_token, access_token, timeout_s=int(args.timeout))
        # API returns app.name; be defensive.
        app_name = str(app.get("name") or app.get("title") or "").strip()
    except Exception:
        app_name = ""
    try:
        tables = list_tables(app_token, access_token, timeout_s=int(args.timeout))
        for t in tables:
            if str(t.get("table_id") or "").strip() == table_id:
                table_name = str(t.get("name") or t.get("table_name") or "").strip()
                break
    except Exception:
        table_name = ""
    if not table_name:
        table_name = f"表-{table_id}"
    if not app_name:
        # Fallback: app title unknown, use table name.
        app_name = table_name

    # Prefer showing the "file/base" title. If multiple tables exist, append table name.
    display_name = app_name
    if table_name and table_name != app_name:
        display_name = f"{app_name} / {table_name}"

    data = load_bindings(Path(args.bindings))
    bindings = data.get("bindings") if isinstance(data.get("bindings"), list) else []
    existing_names = {str(b.get("name") or "") for b in bindings if isinstance(b, dict)}

    # In single-table mode, a new table should require explicit replace confirmation
    # instead of silently replacing the current active table.
    if single_table_mode:
        active_name = str(data.get("active_binding") or "").strip()
        current_binding: Optional[Dict[str, Any]] = None
        for b in bindings:
            if isinstance(b, dict) and str(b.get("name") or "").strip() == active_name:
                current_binding = b
                break
        if current_binding:
            cur_app = str(current_binding.get("app_token") or "").strip()
            cur_tbl = str(current_binding.get("table_id") or "").strip()
            # New target differs from current active table -> ask confirm.
            if not (cur_app == app_token and cur_tbl == table_id):
                cur_display = _binding_display_name(current_binding)
                cur_url = str(current_binding.get("open_url") or "").strip()
                pending_name = _make_unique_name(existing_names, app_name, suffix=table_id[-4:])
                pending_binding = {
                    "name": pending_name,
                    "display_name": display_name,
                    "app_name": app_name,
                    "table_name": table_name,
                    "provider": "feishu",
                    "app_token": app_token,
                    "table_id": table_id,
                    "open_url": open_url,
                    "auto_create_fields": True,
                    "auto_map_fields": True,
                    "alias_map": {},
                    "columns": default_columns_mapping(),
                }
                data["pending_replace"] = {
                    "mode": "single_table",
                    "current_name": str(current_binding.get("name") or "").strip(),
                    "current_display_name": cur_display,
                    "current_open_url": cur_url,
                    "new_binding": pending_binding,
                }
                save_bindings(Path(args.bindings), data)
                reply_text = "\n".join(
                    [
                        "⚠️【表替换确认】",
                        "结果：检测到新的飞书表链接",
                        "当前版本一次只维护 1 张主工作表。",
                        "",
                        f"旧表：{cur_display}",
                        f"新表：{display_name}",
                        "",
                        "请回复：",
                        "• 1 或 替换：切换到新表",
                        "• 2 或 取消：继续使用旧表",
                    ]
                )
                return {
                    "ok": True,
                    "op": "add",
                    "status": "pending_replace_confirm",
                    "reply_text": reply_text,
                }

    # If already registered for this app_token+table_id, just activate it.
    for idx, b in enumerate(bindings, start=1):
        if not isinstance(b, dict):
            continue
        if str(b.get("provider") or "").strip() != "feishu":
            continue
        if str(b.get("app_token") or "").strip() == app_token and str(b.get("table_id") or "").strip() == table_id:
            b["open_url"] = open_url
            b["display_name"] = display_name
            b["app_name"] = app_name
            b["table_name"] = table_name

            # Auto-fix legacy placeholder binding name to real base title (safe demo UX).
            cur_name = str(b.get("name") or "")
            if _maybe_replace_legacy_name(cur_name, display_name):
                # Ensure uniqueness among current bindings
                new_unique = _make_unique_name(existing_names, app_name, suffix=table_id[-4:])
                b["name"] = new_unique
                cur_name = new_unique
            # NOTE:
            # - single_table mode: always keep this table as the only active binding.
            # - multi_table mode: registration updates the internal default target to this table.
            if single_table_mode:
                data["bindings"] = [b]
                data["active_binding"] = str(b.get("name") or "").strip()
            else:
                data["active_binding"] = str(b.get("name") or "").strip()

            save_bindings(Path(args.bindings), data)
            ensured = _ensure_control_fields_for_binding(
                bindings_path=Path(args.bindings),
                binding_name=str(b.get("name") or "").strip(),
                env_file=Path(args.env_file) if args.env_file else DEFAULT_ENV_FILE,
                timeout_s=int(args.timeout),
            )
            _init_sidecar_for_binding(
                bindings_path=Path(args.bindings),
                binding_name=str(b.get("name") or "").strip(),
                env_file=Path(args.env_file) if args.env_file else DEFAULT_ENV_FILE,
                timeout_s=int(args.timeout),
            )
            # Re-load to ensure ordering + active are up-to-date
            data2 = load_bindings(Path(args.bindings))
            bindings2 = [x for x in (data2.get("bindings") or []) if isinstance(x, dict)]
            active2 = str(data2.get("active_binding") or "").strip()
            list_text = _render_table_list(bindings2, active2)
            # Keep receipts minimal for demo; do not add extra system-column notes here.
            syscol_note = ""
            syscol_line = ""
            reply_text = (
                f"{_reply_add(str(b.get('display_name') or b.get('name') or ''), idx, open_url, list_text)}\n{syscol_line}".strip()
                if single_table_mode
                else _reply_existing_multi(str(b.get("display_name") or b.get("name") or "").strip(), open_url, list_text)
            )
            return {
                "ok": True,
                "op": "add",
                "status": "already_registered",
                "index": idx,
                "name": b.get("name"),
                "display_name": f"{str(b.get('display_name') or b.get('name') or '').strip()}{syscol_note}",
                "open_url": open_url,
                "reply_text": reply_text,
            }

    unique_name = _make_unique_name(existing_names, app_name, suffix=table_id[-4:])
    binding = {
        "name": unique_name,
        "display_name": display_name,
        "app_name": app_name,
        "table_name": table_name,
        "provider": "feishu",
        "app_token": app_token,
        "table_id": table_id,
        "open_url": open_url,
        # Product rule: allow auto-creating system columns (确认状态/记录ID).
        # We still only write business values to existing columns (intersection).
        "auto_create_fields": True,
        "auto_map_fields": True,
        "alias_map": {},
        "columns": default_columns_mapping(),
    }
    bindings.append(binding)
    if single_table_mode:
        # Single-table product mode: replace current registry with this binding.
        data["bindings"] = [binding]
        data["active_binding"] = unique_name
    else:
        data["bindings"] = bindings
        # In multi-table mode, the newly registered table becomes the internal default target.
        data["active_binding"] = unique_name
    save_bindings(Path(args.bindings), data)
    ensured = _ensure_control_fields_for_binding(
        bindings_path=Path(args.bindings),
        binding_name=unique_name,
        env_file=Path(args.env_file) if args.env_file else DEFAULT_ENV_FILE,
        timeout_s=int(args.timeout),
    )
    _init_sidecar_for_binding(
        bindings_path=Path(args.bindings),
        binding_name=unique_name,
        env_file=Path(args.env_file) if args.env_file else DEFAULT_ENV_FILE,
        timeout_s=int(args.timeout),
    )
    data2 = load_bindings(Path(args.bindings))
    bindings2 = [x for x in (data2.get("bindings") or []) if isinstance(x, dict)]
    active2 = str(data2.get("active_binding") or "").strip()
    list_text = _render_table_list(bindings2, active2)
    # Keep receipts minimal for demo; do not add extra system-column notes here.
    syscol_note = ""
    syscol_line = ""
    reply_text = (
        f"{_reply_add(display_name, len(bindings), open_url, list_text)}\n{syscol_line}".strip()
        if single_table_mode
        else _reply_add_multi(display_name, open_url, list_text)
    )
    return {
        "ok": True,
        "op": "add",
        "status": "registered",
        "index": len(bindings),
        "name": unique_name,
        "display_name": f"{display_name}{syscol_note}",
        "open_url": open_url,
        "reply_text": reply_text,
    }


def cmd_list(args: argparse.Namespace) -> Dict[str, Any]:
    data = load_bindings(Path(args.bindings))
    active = str(data.get("active_binding") or "").strip()
    bindings = [b for b in (data.get("bindings") or []) if isinstance(b, dict)]
    out: List[Dict[str, Any]] = []
    for i, b in enumerate(bindings, start=1):
        out.append(
            {
                "index": i,
                "name": b.get("name"),
                "display_name": _binding_display_name(b),
                "active": str(b.get("name") or "").strip() == active,
                "open_url": b.get("open_url") or "",
            }
        )
    active_display = ""
    active_url = ""
    for b in bindings:
        if str(b.get("name") or "").strip() == active:
            active_display = _binding_display_name(b)
            active_url = str(b.get("open_url") or "").strip()
            break
    if _is_single_table_mode():
        reply_text = "\n".join(
            [
                "📋【表单清单】",
                "结果：已登记表单",
                f"📍 当前表：{active_display or '(未设置)'}",
                f"🔗 链接：{active_url or '(空)'}",
            ]
        )
    else:
        list_text = _render_table_list(bindings, active)
        reply_text = "\n".join(
            [
                "📋【表单清单】",
                "结果：已登记表格",
                list_text or "(空)",
            ]
        )
    return {"ok": True, "op": "list", "count": len(out), "bindings": out, "active_binding": active, "reply_text": reply_text}


def cmd_select(args: argparse.Namespace) -> Dict[str, Any]:
    data = load_bindings(Path(args.bindings))
    bindings = [b for b in (data.get("bindings") or []) if isinstance(b, dict)]
    if not bindings:
        raise SystemExit("No registered tables yet. Send a Feishu base URL first.")

    target = str(args.target or "").strip()
    if not target:
        raise SystemExit("Missing target.")

    chosen: Optional[Dict[str, Any]] = None
    if target.isdigit():
        idx = int(target)
        if idx < 1 or idx > len(bindings):
            raise SystemExit(f"Invalid index: {idx}. Current table count: {len(bindings)}")
        chosen = bindings[idx - 1]
    else:
        # name fragment match
        hits = [b for b in bindings if target in str(b.get("name") or "")]
        if len(hits) == 1:
            chosen = hits[0]
        elif len(hits) == 0:
            raise SystemExit(f"No table matches: {target}")
        else:
            return {
                "ok": False,
                "op": "select",
                "status": "ambiguous",
                "message": "Multiple tables match. Please reply with a number.",
                "candidates": [{"index": i + 1, "name": b.get("name")} for i, b in enumerate(bindings)],
                "reply_text": _fmt_clarify("匹配到多张表，请回复序号。"),
            }

    assert chosen is not None
    data["active_binding"] = str(chosen.get("name") or "")
    save_bindings(Path(args.bindings), data)
    display = _binding_display_name(chosen)
    url = chosen.get("open_url") or ""
    reply_text = _fmt_success(
        result=f"已进入录入：{display}",
        link=url,
        extra=["提示：直接发送要记录的信息", "提示：发送【退出】返回普通对话"],
    )
    return {
        "ok": True,
        "op": "select",
        "status": "selected",
        "name": chosen.get("name"),
        "display_name": display,
        "open_url": chosen.get("open_url") or "",
        "reply_text": reply_text,
    }


def cmd_current(args: argparse.Namespace) -> Dict[str, Any]:
    data = load_bindings(Path(args.bindings))
    active = str(data.get("active_binding") or "").strip()
    bindings = [b for b in (data.get("bindings") or []) if isinstance(b, dict)]
    for b in bindings:
        if str(b.get("name") or "").strip() == active:
            display = _binding_display_name(b)
            url = b.get("open_url") or ""
            return {
                "ok": True,
                "op": "current",
                "name": active,
                "display_name": display,
                "open_url": url,
                "reply_text": _fmt_success(result=f"当前表：{display}", link=url),
            }
    return {
        "ok": True,
        "op": "current",
        "name": active,
        "display_name": active,
        "open_url": "",
        "reply_text": _fmt_success(result=f"当前表：{active or '(空)'}", link=""),
    }


def cmd_remove(args: argparse.Namespace) -> Dict[str, Any]:
    data = load_bindings(Path(args.bindings))
    bindings = [b for b in (data.get("bindings") or []) if isinstance(b, dict)]
    if not bindings:
        raise SystemExit("没有已登记的表可删除。")

    target = str(args.target or "").strip()
    if not target:
        raise SystemExit("Missing target.")

    idx: Optional[int] = None
    chosen: Optional[Dict[str, Any]] = None
    if target.isdigit():
        i = int(target)
        if i < 1 or i > len(bindings):
            raise SystemExit(f"Invalid index: {i}. Current table count: {len(bindings)}")
        idx = i - 1
        chosen = bindings[idx]
    else:
        hits = [b for b in bindings if target in str(b.get("name") or "") or target in str(b.get("display_name") or "")]
        if len(hits) == 1:
            chosen = hits[0]
            idx = bindings.index(chosen)
        elif len(hits) == 0:
            raise SystemExit(f"No table matches: {target}")
        else:
            return {
                "ok": False,
                "op": "remove",
                "status": "ambiguous",
                "reply_text": _fmt_clarify("匹配到多张表，请回复更具体的序号或表名。"),
            }

    assert chosen is not None and idx is not None
    removed = bindings.pop(idx)
    active = str(data.get("active_binding") or "").strip()
    if active == str(removed.get("name") or "").strip():
        data["active_binding"] = ""
    data["bindings"] = bindings
    save_bindings(Path(args.bindings), data)

    display = _binding_display_name(removed)
    url = str(removed.get("open_url") or "").strip()
    reply_text = _fmt_success(result=f"已删除表：{display}", link=url)
    return {
        "ok": True,
        "op": "remove",
        "status": "removed",
        "name": removed.get("name"),
        "display_name": display,
        "open_url": url,
        "reply_text": reply_text,
    }


def cmd_pending_reply(args: argparse.Namespace) -> Dict[str, Any]:
    data = load_bindings(Path(args.bindings))
    pending = data.get("pending_replace") if isinstance(data.get("pending_replace"), dict) else {}
    if not pending:
        return {
            "ok": True,
            "op": "pending_reply",
            "status": "no_pending",
            "reply_text": "结果：当前没有待处理的表替换请求",
        }

    choice = str(args.choice or "").strip()
    if choice in {"替换", "1"}:
        new_binding = pending.get("new_binding") if isinstance(pending.get("new_binding"), dict) else {}
        if not new_binding:
            data["pending_replace"] = {}
            save_bindings(Path(args.bindings), data)
            return {"ok": False, "op": "pending_reply", "status": "invalid_pending", "reply_text": "结果：执行失败\n原因：待替换数据缺失，请重新发送表链接"}

        # Replace bindings with the chosen table in single-table mode.
        data["bindings"] = [new_binding]
        data["active_binding"] = str(new_binding.get("name") or "").strip()
        data["pending_replace"] = {}
        save_bindings(Path(args.bindings), data)

        ensured = _ensure_control_fields_for_binding(
            bindings_path=Path(args.bindings),
            binding_name=str(new_binding.get("name") or "").strip(),
            env_file=Path(args.env_file) if args.env_file else DEFAULT_ENV_FILE,
            timeout_s=int(args.timeout),
        )
        _init_sidecar_for_binding(
            bindings_path=Path(args.bindings),
            binding_name=str(new_binding.get("name") or "").strip(),
            env_file=Path(args.env_file) if args.env_file else DEFAULT_ENV_FILE,
            timeout_s=int(args.timeout),
        )
        _ = ensured  # keep for future diagnostics, no extra user noise in receipt

        disp = _binding_display_name(new_binding)
        url = str(new_binding.get("open_url") or "").strip()
        return {
            "ok": True,
            "op": "pending_reply",
            "status": "replaced",
            "reply_text": _fmt_success(result="已切换到新表", table=disp, link=url),
        }

    if choice in {"取消", "2"}:
        cur_disp = str(pending.get("current_display_name") or "").strip()
        cur_url = str(pending.get("current_open_url") or "").strip()
        data["pending_replace"] = {}
        save_bindings(Path(args.bindings), data)
        return {
            "ok": True,
            "op": "pending_reply",
            "status": "canceled",
            "reply_text": _fmt_success(result="已取消替换", table=(cur_disp or "当前表"), link=cur_url),
        }

    return {
        "ok": True,
        "op": "pending_reply",
        "status": "need_choice",
        "reply_text": "需要确认：请回复“替换（或 1）”或“取消（或 2）”。",
    }


def main(argv: List[str]) -> None:
    ap = argparse.ArgumentParser(prog="table_registry.py")
    ap.add_argument("--bindings", default=str(DEFAULT_BINDINGS_PATH))
    ap.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    ap.add_argument("--timeout", type=int, default=20)
    sub = ap.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add")
    add.add_argument("--url", required=True)

    sub.add_parser("list")

    sel = sub.add_parser("select")
    sel.add_argument("--target", required=True)

    sub.add_parser("current")

    rm = sub.add_parser("remove")
    rm.add_argument("--target", required=True)

    pr = sub.add_parser("pending_reply")
    pr.add_argument("--choice", required=True)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "add":
            out = cmd_add(args)
        elif args.cmd == "list":
            out = cmd_list(args)
        elif args.cmd == "select":
            out = cmd_select(args)
        elif args.cmd == "current":
            out = cmd_current(args)
        elif args.cmd == "remove":
            out = cmd_remove(args)
        elif args.cmd == "pending_reply":
            out = cmd_pending_reply(args)
        else:
            raise SystemExit(f"Unknown cmd: {args.cmd}")
    except SystemExit as e:
        msg = str(e) if str(e) else "未知错误"
        out = {"ok": False, "op": args.cmd, "reply_text": _humanize_registry_error(msg), "error": msg}

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
