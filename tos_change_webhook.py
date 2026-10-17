#!/usr/bin/env python3
"""
tos-change-webhook: monitor vendor Terms of Service / privacy policy pages and
fire a webhook when the wording changes.

Config file (JSON): maps vendor names to URLs, e.g.
    {"OpenAI Terms": "https://openai.com/policies/terms-of-use/"}

A top-level object with a "vendors" key is also accepted; in that form an
optional "webhook_url" key sets the default webhook:
    {"webhook_url": "https://example.com/hook",
     "vendors": {"OpenAI Terms": "https://openai.com/policies/terms-of-use/"}}

Commands:
    baseline   fetch every vendor page and record the current text (no alerts)
    watch      fetch every page; on change print a unified diff and POST a
               JSON webhook payload
    diff NAME  show the most recently recorded change for one vendor

Stdlib only: urllib, hashlib, difflib, json, argparse.
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_CONFIG = "vendors.json"
DEFAULT_STATE = ".tos-state.json"

USER_AGENT = "tos-change-webhook/1.0 (+https://github.com/Iceman2050/tos-change-webhook)"
FETCH_TIMEOUT = 30
DIFF_EXCERPT_LINES = 15
DIFF_EXCERPT_CHARS = 1500


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------
#
# EXTRACTION HEURISTIC (plain and honest):
#   1. Delete <script>, <style>, <nav>, <footer>, <header>, <aside> and
#      <noscript> blocks entirely (regex, case-insensitive, dotall). These
#      almost always hold boilerplate / navigation / legal chrome, not the
#      policy body.
#   2. Delete HTML comments.
#   3. Strip every remaining HTML tag.
#   4. Unescape the common entities (&amp; &lt; &gt; &quot; &#39; &nbsp;).
#   5. Collapse every run of whitespace (including newlines) to a single
#      space and strip leading/trailing space.
#
# This is intentionally naive. It does NOT run JavaScript, so pages that
# render their policy text client-side will hash to boilerplate and may
# produce noise. It can also mis-handle unusual markup. What it gives you is
# a stable, deterministic fingerprint of the *visible static text* — good
# enough to catch wording changes, which is the whole point.

_STRIP_BLOCKS_RE = re.compile(
    r"<(script|style|nav|footer|header|aside|noscript)\b.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENTS_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAGS_RE = re.compile(r"<[^>]+>")
_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&nbsp;": " ",
}
_WS_RE = re.compile(r"\s+")


def extract_text(html):
    """Extract the main visible text from HTML using the documented heuristic."""
    text = _STRIP_BLOCKS_RE.sub(" ", html)
    text = _COMMENTS_RE.sub(" ", text)
    text = _TAGS_RE.sub(" ", text)
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    text = _WS_RE.sub(" ", text).strip()
    return text


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch(url):
    """Fetch a URL and return its decoded body text. Raises on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        raw = resp.read()
    charset = "utf-8"
    content_type = ""
    try:
        content_type = resp.headers.get("Content-Type", "")
    except Exception:
        pass
    m = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    if m:
        charset = m.group(1)
    return raw.decode(charset, errors="replace")


# ---------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------

def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    if "vendors" in data and isinstance(data["vendors"], dict):
        vendors = data["vendors"]
        webhook_url = data.get("webhook_url")
    else:
        vendors = data
        webhook_url = data.get("webhook_url") if isinstance(data.get("webhook_url"), str) else None
        if "webhook_url" in vendors:
            vendors = {k: v for k, v in vendors.items() if k != "webhook_url"}
    return vendors, webhook_url


def load_state(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Diffing / webhook
# ---------------------------------------------------------------------------

def unified_diff(old_text, new_text, name):
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    return list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="%s (previous)" % name,
            tofile="%s (current)" % name,
            lineterm="",
        )
    )


def diff_summary(diff_lines):
    added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
    return "+%d/-%d lines" % (added, removed)


def post_webhook(webhook_url, payload):
    """POST the payload as JSON. Returns (ok, status_or_error)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return True, resp.status
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s" % exc.code
    except Exception as exc:  # network errors, refused connections, ...
        return False, str(exc)


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_baseline(vendors, state, state_path):
    for name, url in vendors.items():
        try:
            text = extract_text(fetch(url))
        except Exception as exc:
            print("baseline: %-30s ERROR %s" % (name, exc), file=sys.stderr)
            continue
        state[name] = {
            "url": url,
            "hash": sha256_hex(text),
            "text": text,
            "prev_text": None,
            "prev_hash": None,
            "checked_at": utcnow_iso(),
        }
        print("baseline: %-30s recorded (%d chars, sha256 %s...)"
              % (name, len(text), state[name]["hash"][:12]))
    save_state(state_path, state)
    return 0


def cmd_watch(vendors, state, state_path, webhook_url):
    changes = 0
    errors = 0
    for name, url in vendors.items():
        try:
            text = extract_text(fetch(url))
        except Exception as exc:
            print("watch: %-30s ERROR %s" % (name, exc), file=sys.stderr)
            errors += 1
            continue
        new_hash = sha256_hex(text)
        entry = state.get(name)
        if entry is None:
            state[name] = {
                "url": url, "hash": new_hash, "text": text,
                "prev_text": None, "prev_hash": None,
                "checked_at": utcnow_iso(),
            }
            print("watch: %-30s first seen, recorded without alerting" % name)
            continue
        if new_hash == entry.get("hash"):
            entry["checked_at"] = utcnow_iso()
            print("watch: %-30s no change" % name)
            continue

        # --- change detected ---
        changes += 1
        diff_lines = unified_diff(entry.get("text", ""), text, name)
        print("watch: %-30s *** CHANGE DETECTED ***" % name)
        for line in diff_lines:
            print(line)
        summary = diff_summary(diff_lines)
        excerpt = "\n".join(diff_lines[:DIFF_EXCERPT_LINES])[:DIFF_EXCERPT_CHARS]
        detected_at = utcnow_iso()
        payload = {
            "vendor": name,
            "url": url,
            "detected_at": detected_at,
            "diff_summary": summary,
            "diff_excerpt": excerpt,
        }
        if webhook_url:
            ok, status = post_webhook(webhook_url, payload)
            if ok:
                print("watch: webhook POSTed to %s (HTTP %s)" % (webhook_url, status))
            else:
                print("watch: webhook POST FAILED (%s)" % status, file=sys.stderr)
        else:
            print("watch: no webhook configured; diff printed above only", file=sys.stderr)
        state[name] = {
            "url": url, "hash": new_hash, "text": text,
            "prev_text": entry.get("text", ""),
            "prev_hash": entry.get("hash"),
            "checked_at": detected_at,
        }
    save_state(state_path, state)
    print("watch: %d change(s), %d error(s)" % (changes, errors))
    return 0


def cmd_diff(state, name):
    entry = state.get(name)
    if entry is None:
        print("no state recorded for %r" % name, file=sys.stderr)
        return 1
    prev_text = entry.get("prev_text")
    if not prev_text:
        print("no recorded change for %r (only a baseline exists)" % name)
        return 0
    for line in unified_diff(prev_text, entry.get("text", ""), name):
        print(line)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="Monitor vendor Terms of Service / privacy policy pages; "
                    "fire a webhook when the wording changes."
    )
    p.add_argument("--config", default=DEFAULT_CONFIG, help="JSON config file (default: vendors.json)")
    p.add_argument("--state", default=DEFAULT_STATE, help="JSON state file (default: .tos-state.json)")
    sub = p.add_subparsers(dest="command", required=True)

    wb = sub.add_parser("watch", help="one monitoring pass; alert + webhook on change")
    wb.add_argument("--webhook", default=None,
                    help="webhook URL (overrides config; or TOS_CHANGE_WEBHOOK_URL env)")

    sub.add_parser("baseline", help="record current page text without alerting")

    dp = sub.add_parser("diff", help="show the most recently recorded change")
    dp.add_argument("name", help="vendor name as written in the config file")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        vendors, config_webhook = load_config(args.config)
    except FileNotFoundError:
        print("config file not found: %s" % args.config, file=sys.stderr)
        print('create one like: {"OpenAI Terms": "https://openai.com/policies/terms-of-use/"}',
              file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        print("bad config file: %s" % exc, file=sys.stderr)
        return 2
    if not vendors:
        print("config has no vendors", file=sys.stderr)
        return 2

    state = load_state(args.state)

    if args.command == "baseline":
        return cmd_baseline(vendors, state, args.state)
    if args.command == "watch":
        webhook_url = args.webhook or config_webhook or os.environ.get("TOS_CHANGE_WEBHOOK_URL")
        return cmd_watch(vendors, state, args.state, webhook_url)
    if args.command == "diff":
        return cmd_diff(state, args.name)
    return 2


if __name__ == "__main__":
    sys.exit(main())
