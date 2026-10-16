#!/usr/bin/env python3
"""Send a radar report to a Feishu (Lark) group through a custom bot webhook.

    FEISHU_WEBHOOK_URL=... python3 scripts/feishu_notify.py report.md --run-url "$URL"

Environment:
  FEISHU_WEBHOOK_URL   the bot's webhook; keep it in GitHub Secrets, never inline
  FEISHU_SECRET        only if you turned the bot's signature check on

Three limits shape this file, and none of them are configurable:

  * The request body must stay under 20 KB. A full daily report does not fit, so we
    send the top rows of each table plus a link back to the run.
  * A text message renders nothing: no bold, no links, and above all no tables, which
    is what a radar report is made of. So it goes as an interactive card whose markdown
    element can draw tables. If the card is rejected for any reason we fall back to a
    flattened text message rather than lose the report.
  * One bot takes 100 messages a minute and 5 a second, and the busy hour and half hour
    get throttled (error 11232), so point the schedule off the hour.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

MAX_BYTES = 18_000          # the ceiling is 20 KB and it is metered on the whole body
TABLE_ROW = re.compile(r"^\|(.+)\|\s*$")
SEP_ROW = re.compile(r"^\|[\s:|-]+\|\s*$")


# --- reading the report -------------------------------------------------------

def tables(text: str, heading: str) -> tuple[str, list[list[str]]]:
    """The heading's markdown table as (header line, data rows), "" and [] if absent."""
    lines = text.splitlines()
    start = next((i for i, s in enumerate(lines) if s.startswith("###") and heading in s), None)
    if start is None:
        return "", []
    head, rows = "", []
    for line in lines[start + 1:]:
        if line.startswith("###") or line.startswith("## "):
            break
        m = TABLE_ROW.match(line.strip())
        if not m or SEP_ROW.match(line.strip()):
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if not head:
            head = line.strip()
        else:
            rows.append(cells)
    return head, rows


def bullets(text: str, heading: str) -> list[str]:
    """The list items under a heading, in order."""
    lines = text.splitlines()
    start = next((i for i, s in enumerate(lines) if s.startswith("###") and heading in s), None)
    if start is None:
        return []
    out = []
    for line in lines[start + 1:]:
        if line.startswith("###") or line.startswith("## "):
            break
        if line.startswith("- "):
            out.append(line[2:].strip())
    return out


def cell(row: list[str], i: int) -> str:
    return row[i] if i < len(row) else ""


def pick(text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[str]]:
    """What the group actually needs to read at a glance."""
    terms = []
    for _, rows in (tables(text, "候选新词"), tables(text, "Candidate new terms")):
        for r in rows:
            name = cell(r, 0).replace("**", "").replace("`", "").strip()
            terms.append((name, cell(r, 1)))
        if terms:
            break
    repos = []
    for key in ("Single-repo breakouts", "单仓库爆发"):
        _, rows = tables(text, key)
        for r in rows:
            link = re.search(r"\[([^\]]+)\]\(([^)]+)\)", cell(r, 0))
            repos.append((link.group(1) if link else cell(r, 0), f"{cell(r, 1)} · {cell(r, 2)}/day"))
        if repos:
            break
    status = bullets(text, "数据状态") or bullets(text, "Data status")
    return terms, repos, status


# --- building the message -----------------------------------------------------

def markdown_body(text: str, n: int, run_url: str) -> str:
    terms, repos, status = pick(text)
    md = []
    if terms:
        md.append("**候选新词**（共 %d 条，取前 %d）" % (len(terms), min(n, len(terms))))
        md.append("")
        md.append("| 词 | 信号 |")
        md.append("| --- | --- |")
        for name, signal in terms[:n]:
            md.append("| %s | %s |" % (name.replace("|", "/"), signal.replace("|", "/")))
        md.append("")
    if repos:
        md.append("**爆发仓库**（取前 %d）" % min(n, len(repos)))
        md.append("")
        md.append("| 仓库 | 星数 |")
        md.append("| --- | --- |")
        for name, stars in repos[:n]:
            md.append("| %s | %s |" % (name.replace("|", "/"), stars.replace("|", "/")))
        md.append("")
    for s in status[:4]:
        md.append("- " + s)
    if run_url:
        md.append("")
        md.append("[查看完整报告](" + run_url + ")")
    return "\n".join(md)


def flatten(md: str) -> str:
    """The same thing without tables, for the text fallback."""
    out = []
    for line in md.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            out.append(" · ".join(c for c in cells if c and not set(c) <= set("-: ")))
        else:
            out.append(re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 \2", line).replace("**", ""))
    return "\n".join(out).strip()


def card(title: str, run_url: str, md: str) -> dict:
    """Card JSON 2.0: its markdown element is the one that draws tables.

    No action button in here even though it costs us nothing to add one: schema 2.0
    rejects it with 11246 ("unsupported tag action"), and a rejected card means the
    report never lands. The link lives in the markdown instead.
    """
    return {"msg_type": "interactive", "card": {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "indigo"},
        "body": {"elements": [{"tag": "markdown", "content": md}]},
    }}


def as_text(title: str, run_url: str, md: str) -> dict:
    return {"msg_type": "text", "content": {"text": title + "\n\n" + flatten(md) + ("\n\n" + run_url if run_url else "")}}


# --- sending ------------------------------------------------------------------

def signed(body: dict) -> dict:
    secret = os.environ.get("FEISHU_SECRET", "").strip()
    if not secret:
        return body
    ts = str(int(time.time()))
    digest = hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()
    return {**body, "timestamp": ts, "sign": base64.b64encode(digest).decode()}


def post(url: str, body: dict, dry: bool) -> tuple[bool, str]:
    data = json.dumps(body, ensure_ascii=False).encode()
    if dry:
        return True, f"dry run, {len(data)} bytes"
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            got = json.loads(res.read())
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return got.get("code") == 0, f"code {got.get('code')} {got.get('msg')}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("report", type=Path, help="report.md")
    p.add_argument("--run-url", default=os.environ.get("RADAR_RUN_URL", ""), help="link back to the Actions run")
    p.add_argument("--title", default="")
    p.add_argument("--max-rows", type=int, default=8, help="rows per table to keep")
    p.add_argument("--text", action="store_true", help="send as text, not a card")
    p.add_argument("--dry-run", action="store_true", help="print what would be sent")
    a = p.parse_args()

    text = a.report.read_text()
    title = a.title or "New-term radar · " + time.strftime(
        "%Y-%m-%d", time.localtime(a.report.stat().st_mtime))
    url = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not url and not a.dry_run:
        print("feishu skipped: FEISHU_WEBHOOK_URL not set")
        return 0

    # Shrink to fit: fewer table rows, then a flattened text body, which is always shorter.
    attempts = []
    for rows in (a.max_rows, 6, 4, 3):
        md = markdown_body(text, rows, a.run_url)
        attempts.append(card(title, a.run_url, md) if not a.text else as_text(title, a.run_url, md))
        if len(json.dumps(attempts[-1], ensure_ascii=False).encode()) <= MAX_BYTES:
            break
    attempts.append(as_text(title, a.run_url, markdown_body(text, 3, a.run_url)))

    for i, body in enumerate(attempts):
        ok, msg = post(url, signed(body), a.dry_run)
        label = "card" if "card" in body else "text"
        print(f"{label}: {msg}")
        if ok:
            return 0
        if i == len(attempts) - 1:
            return 1
        print("falling back")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
