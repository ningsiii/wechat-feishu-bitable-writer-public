#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
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
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        os.environ.setdefault(key, value)


def post_json(url: str, payload: Dict[str, Any], timeout_s: int) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise SystemExit(f"HTTP {e.code} posting to WeCom webhook: {detail[:500]}")
    except Exception as e:
        raise SystemExit(f"Failed posting to WeCom webhook: {e}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def get_digest(ledger_script: str) -> Dict[str, Any]:
    cmd = [sys.executable, ledger_script, "digest", "--format", "json"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"Failed generating digest: {res.stderr.strip() or res.stdout.strip()}")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"ledger digest did not return JSON: {res.stdout[:300]}")


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
    ap.add_argument(
        "--webhook-url",
        default="",
        help="WeCom group bot webhook URL. If empty, uses WECOM_WEBHOOK_URL env.",
    )
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--msgtype", choices=["markdown", "text"], default=os.environ.get("WECOM_MSGTYPE") or "markdown")
    args = ap.parse_args(argv)

    if args.env_file:
        load_dotenv(Path(args.env_file))

    webhook = (args.webhook_url or os.environ.get("WECOM_WEBHOOK_URL") or "").strip()
    if not webhook:
        raise SystemExit(
            "Missing WeCom webhook.\n"
            "Provide one of:\n"
            "  - --webhook-url 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...'\n"
            "  - env WECOM_WEBHOOK_URL=...\n"
            "  - or put WECOM_WEBHOOK_URL=... in solutions/formflow-agent/.env"
        )

    digest = get_digest(args.ledger_script)
    digest_text = str(digest.get("digest_text") or "").strip()
    if not digest_text:
        # Fallback to a compact JSON if digest_text missing.
        digest_text = json.dumps(digest.get("summary") or {}, ensure_ascii=False, indent=2)

    if args.msgtype == "markdown":
        payload = {"msgtype": "markdown", "markdown": {"content": digest_text}}
    else:
        payload = {"msgtype": "text", "text": {"content": digest_text}}

    resp = post_json(webhook, payload, timeout_s=args.timeout)
    print(json.dumps({"ok": True, "wecom_response": resp}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])

