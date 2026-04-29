#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict


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


def get_json(url: str, timeout_s: int) -> Dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise SystemExit(f"HTTP {e.code} calling WeCom API: {detail[:800]}")
    except Exception as e:
        raise SystemExit(f"Failed calling WeCom API: {e}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(f"WeCom response was not JSON: {raw[:800]}")


def post_json(url: str, payload: Dict[str, Any], timeout_s: int) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise SystemExit(f"HTTP {e.code} calling WeCom API: {detail[:800]}")
    except Exception as e:
        raise SystemExit(f"Failed calling WeCom API: {e}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(f"WeCom response was not JSON: {raw[:800]}")


def get_digest(ledger_script: str) -> Dict[str, Any]:
    cmd = [sys.executable, ledger_script, "digest", "--format", "json"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"Failed generating digest: {res.stderr.strip() or res.stdout.strip()}")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"ledger digest did not return JSON: {res.stdout[:300]}")


def get_access_token(corp_id: str, app_secret: str, timeout_s: int) -> str:
    qs = urllib.parse.urlencode({"corpid": corp_id, "corpsecret": app_secret})
    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?{qs}"
    res = get_json(url, timeout_s=timeout_s)
    if int(res.get("errcode", -1)) != 0:
        raise SystemExit(f"WeCom gettoken failed: {res.get('errmsg') or res}")
    token = str(res.get("access_token") or "").strip()
    if not token:
        raise SystemExit("WeCom gettoken returned empty access_token.")
    return token


def send_message(
    access_token: str,
    agent_id: str,
    to_user: str,
    to_party: str,
    to_tag: str,
    msgtype: str,
    content: str,
    timeout_s: int,
) -> Dict[str, Any]:
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={urllib.parse.quote(access_token, safe='')}"
    payload: Dict[str, Any] = {
        "agentid": int(agent_id) if agent_id.isdigit() else agent_id,
        "msgtype": msgtype,
        # Enable basic duplicate check so accidental re-runs are less noisy.
        "enable_duplicate_check": 1,
        "duplicate_check_interval": 1800,
    }
    if to_user:
        payload["touser"] = to_user
    if to_party:
        payload["toparty"] = to_party
    if to_tag:
        payload["totag"] = to_tag

    if msgtype == "markdown":
        payload["markdown"] = {"content": content}
    else:
        payload["text"] = {"content": content}

    res = post_json(url, payload, timeout_s=timeout_s)
    if int(res.get("errcode", -1)) != 0:
        raise SystemExit(f"WeCom message/send failed: {res.get('errmsg') or res}")
    return res


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--env-file",
        default=os.environ.get("SMALLBIZ_ENV_FILE") or "solutions/formflow-agent/.env",
        help="Optional .env file path (default: solutions/formflow-agent/.env)",
    )
    ap.add_argument(
        "--ledger-script",
        default="solutions/formflow-agent/skills/formflow-ledger/scripts/ledger.py",
        help="Path to ledger.py",
    )
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--msgtype", choices=["markdown", "text"], default=os.environ.get("WECOM_MSGTYPE") or "markdown")
    ap.add_argument("--corp-id", default="", help="WeCom CorpID. If empty, uses WECOM_CORP_ID env.")
    ap.add_argument("--agent-id", default="", help="WeCom AgentId. If empty, uses WECOM_AGENT_ID env.")
    ap.add_argument("--app-secret", default="", help="WeCom app secret. If empty, uses WECOM_APP_SECRET env.")
    ap.add_argument("--touser", default="", help="Receiver userids, separated by |. If empty, uses WECOM_TOUSER env.")
    ap.add_argument("--toparty", default="", help="Receiver party ids, separated by |. If empty, uses WECOM_TOPARTY env.")
    ap.add_argument("--totag", default="", help="Receiver tag ids, separated by |. If empty, uses WECOM_TOTAG env.")
    args = ap.parse_args(argv)

    if args.env_file:
        load_dotenv(Path(args.env_file))

    corp_id = (args.corp_id or os.environ.get("WECOM_CORP_ID") or "").strip()
    agent_id = (args.agent_id or os.environ.get("WECOM_AGENT_ID") or "").strip()
    app_secret = (args.app_secret or os.environ.get("WECOM_APP_SECRET") or "").strip()
    if not corp_id or not agent_id or not app_secret:
        raise SystemExit(
            "Missing WeCom app credentials.\n"
            "Provide via env:\n"
            "  WECOM_CORP_ID=...\n"
            "  WECOM_AGENT_ID=...\n"
            "  WECOM_APP_SECRET=...\n"
            "Or pass --corp-id/--agent-id/--app-secret."
        )

    to_user = (args.touser or os.environ.get("WECOM_TOUSER") or "").strip()
    to_party = (args.toparty or os.environ.get("WECOM_TOPARTY") or "").strip()
    to_tag = (args.totag or os.environ.get("WECOM_TOTAG") or "").strip()
    if not (to_user or to_party or to_tag):
        raise SystemExit(
            "Missing receivers.\n"
            "Provide one of:\n"
            "  WECOM_TOUSER='userid1|userid2'  (or '@all' for broadcast)\n"
            "  WECOM_TOPARTY='partyid1|partyid2'\n"
            "  WECOM_TOTAG='tagid1|tagid2'\n"
            "Or pass --touser/--toparty/--totag."
        )

    digest = get_digest(args.ledger_script)
    digest_text = str(digest.get("digest_text") or "").strip()
    if not digest_text:
        digest_text = json.dumps(digest.get("summary") or {}, ensure_ascii=False, indent=2)

    access_token = get_access_token(corp_id, app_secret, timeout_s=args.timeout)
    resp = send_message(
        access_token=access_token,
        agent_id=agent_id,
        to_user=to_user,
        to_party=to_party,
        to_tag=to_tag,
        msgtype=args.msgtype,
        content=digest_text,
        timeout_s=args.timeout,
    )
    print(json.dumps({"ok": True, "wecom_response": resp}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])

