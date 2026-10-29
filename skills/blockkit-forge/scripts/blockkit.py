# SPDX-License-Identifier: Apache-2.0
# The copyright holder is named in the LICENSE file of this skill.
"""Purpose: ask Slack itself what Block Kit accepts: read the live enums, validate a payload, and dry-post it as a bot.
Usage: python3 -I blockkit.py schema [--type T]                 # the allowed values of each probed field
       python3 -I blockkit.py validate payload.json             # blocks.validate: no token, shape only
       python3 -I blockkit.py dry-post CHANNEL_ID payload.json < token-file   # each block alone, then all
       python3 -I blockkit.py cleanup CHANNEL_ID < token-file   # delete every dry-post message still queued
       python3 -I blockkit.py selftest                          # offline checks

Facts this script is built on (measured 2026-10-22 unless a doc is named):
- `blocks.validate` is public (no token). On an invalid enum value it answers with the allowed values,
  and those run ahead of the docs: its block-type list named 34 types, many on no official page.
- `blocks.validate` checks shape only: no surface check and no per-message totals.
- `dry-post` is a live write. Each payload goes to `chat.scheduleMessage` a week out and is deleted at
  once. Slack refuses to delete a message due within 60 seconds (chat.deleteScheduledMessage docs),
  and a message deleted minutes before its time was still delivered once, so a week is the margin.
  Every id is recorded the moment Slack returns it, a `finally` deletes what is left, and the queue
  is read back page by page. Any doubt exits non-zero and names the ids.
Standard library only.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

API = "https://slack.com/api/"
NOPE = "__nope__"
MARKER = "blockkit dry-post"  # the notification text of every dry-post message, and nothing more
WEEK = 7 * 86400
SPACING = 11  # seconds between post_at times: Slack allows 30 scheduled messages per 5 minutes per channel
TRIES = 5
SECTION = {"type": "section", "text": {"type": "mrkdwn", "text": "x"}}

# One probe per enum worth knowing. Each puts NOPE where the enum lives, so the error names it.
PROBES: dict[str, dict] = {
    "block types": {"type": NOPE},
    "text.type": {"type": "section", "text": {"type": NOPE, "text": "x"}},
    "header.level": {"type": "header", "text": {"type": "plain_text", "text": "t"}, "level": 9},
    "context.elements[].type": {"type": "context", "elements": [{"type": NOPE}]},
    "section.accessory.type": {
        "type": "section",
        "text": SECTION["text"],
        "accessory": {"type": NOPE},
    },
    "actions.elements[].type": {"type": "actions", "elements": [{"type": NOPE}]},
    "button.style": {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "b"},
                "action_id": "a",
                "style": NOPE,
            }
        ],
    },
    "rich_text_section.elements[].type": {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": [{"type": NOPE}]}],
    },
    "container.width": {
        "type": "container",
        "title": {"type": "plain_text", "text": "t"},
        "width": NOPE,
        "child_blocks": [SECTION],
    },
    "container.child_blocks[].type": {
        "type": "container",
        "title": {"type": "plain_text", "text": "t"},
        "child_blocks": [{"type": NOPE}],
    },
    "callout.background_color": {
        "type": "callout",
        "background_color": NOPE,
        "child_blocks": [SECTION],
    },
    "callout.child_blocks[].type": {
        "type": "callout",
        "background_color": "blue",
        "child_blocks": [{"type": NOPE}],
    },
    "card.slack_icon.name": {
        "type": "card",
        "slack_icon": {"type": "icon", "name": NOPE},
        "title": {"type": "mrkdwn", "text": "t"},
    },
    "condition.conditions[].type": {
        "type": "condition",
        "cases": [
            {"conditions": [{"type": NOPE, "op": "=", "value": "desktop"}], "blocks": [SECTION]}
        ],
        "default": [SECTION],
    },
    "condition.op": {
        "type": "condition",
        "cases": [
            {
                "conditions": [{"type": "client", "op": NOPE, "value": "desktop"}],
                "blocks": [SECTION],
            }
        ],
        "default": [SECTION],
    },
}


class SlackError(Exception):
    """A call that produced no Slack answer: an HTTP error, a redirect, or a rate limit past the retries."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib copies the Authorization header onto a redirect, whatever the new host.
    # Slack's Web API never redirects, so every redirect is refused.
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003 (urllib's own signature)
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def _wait(headers, attempt: int) -> float:
    try:
        return max(1.0, float(headers.get("Retry-After")))
    except (AttributeError, TypeError, ValueError):
        return float(2**attempt)


def post(method: str, params: dict, token: str | None = None, pause: float = 0.0) -> dict:
    """One Web API call. Retries a rate limit (HTTP 429 or ok:false ratelimited); raises SlackError otherwise."""
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for attempt in range(TRIES):
        request = urllib.request.Request(API + method, data=data, headers=headers)
        try:
            with OPENER.open(request, timeout=60) as response:
                answer_headers = response.headers
                body = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                time.sleep(_wait(error.headers, attempt))
                continue
            raise SlackError(f"{method}: HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise SlackError(f"{method}: {error}") from None
        if body.get("error") == "ratelimited":
            time.sleep(_wait(answer_headers, attempt))
            continue
        if pause:
            time.sleep(pause)
        return body
    raise SlackError(f"{method}: still rate limited after {TRIES} tries")


def validate(blocks: list) -> dict:
    return post("blocks.validate", {"blocks": json.dumps(blocks)}, pause=1.2)


def allowed_values(answer: dict) -> tuple[str | None, list | None]:
    """The pointer and the allowed enum from a blocks.validate answer, or (None, None)."""
    for error in answer.get("errors") or []:
        constraint = error.get("constraint") or {}
        if constraint.get("type") == "enum":
            return error.get("pointer"), constraint.get("expected")
    return None, None


def load_blocks(path: str) -> list:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    blocks = payload.get("blocks", payload) if isinstance(payload, dict) else payload
    if not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks):
        raise SystemExit(f"{path}: expected a list of blocks or {{'blocks': [...]}}")
    return blocks


def escape_mrkdwn(text: str) -> str:
    """Escape for a mrkdwn string: `&` FIRST, or the `&` of `&lt;` is escaped twice."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cut_then_escape(text: str, limit: int, mark: str = "…") -> str:
    """Cut the raw text, then escape it, so no entity is split. Escaping can grow the result past
    `limit` (each & < > adds 3 or 4 characters), so leave that margin below a hard Slack limit."""
    if len(text) > limit:
        text = text[: max(0, limit - len(mark))] + mark
    return escape_mrkdwn(text)


INVISIBLE_RE = re.compile(r"[\u200b-\u200d\u2060\ufeff\u202a-\u202e\u2066-\u2069]")
BLOCK_TAGS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "ul", "ol", "li", "tr", "article",
    "section", "header", "footer",
}


class _MrkdwnHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.ignore_depth = 0
        self.link_stack: list[dict] = []

    def _ensure_newline(self) -> None:
        if self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self.ignore_depth += 1
            return
        if self.ignore_depth > 0:
            return

        if tag == "br":
            self.out.append("\n")
        elif tag in ("b", "strong"):
            self.out.append("*")
        elif tag in ("i", "em"):
            self.out.append("_")
        elif tag == "li":
            self._ensure_newline()
            self.out.append("• ")
        elif tag == "a":
            attrs_dict = dict(attrs)
            href = INVISIBLE_RE.sub("", attrs_dict.get("href") or "").strip()
            is_valid = (
                href.startswith(("http://", "https://"))
                and ">" not in href
                and "<" not in href
            )
            if is_valid:
                clean_url = escape_mrkdwn(href.replace("|", "%7C"))
                self.link_stack.append({"url": clean_url, "start_idx": len(self.out)})
            else:
                self.link_stack.append({"url": None, "start_idx": len(self.out)})

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            if self.ignore_depth > 0:
                self.ignore_depth -= 1
            return
        if self.ignore_depth > 0:
            return

        if tag in ("b", "strong"):
            self.out.append("*")
        elif tag in ("i", "em"):
            self.out.append("_")
        elif tag == "a" and self.link_stack:
            link = self.link_stack.pop()
            url = link["url"]
            start_idx = link["start_idx"]
            link_text = "".join(self.out[start_idx:])
            if url:
                del self.out[start_idx:]
                if link_text:
                    self.out.append(f"<{url}|{link_text}>")
                else:
                    self.out.append(f"<{url}>")
        elif tag in BLOCK_TAGS:
            self._ensure_newline()

    def handle_data(self, data: str) -> None:
        if self.ignore_depth > 0:
            return
        cleaned = INVISIBLE_RE.sub("", data)
        if not cleaned:
            return
        if cleaned.isspace() and (not self.out or self.out[-1].endswith("\n")):
            return
        self.out.append(escape_mrkdwn(cleaned))


def html_to_mrkdwn(html_text: str) -> str:
    """Convert an HTML snippet into mrkdwn.

    Block tags end a line at their END only (<p>a</p><p>b</p> is two lines, not three),
    <br> breaks, <b>/<strong> becomes *, <i>/<em> becomes _, <li> becomes •,
    <a href> becomes <url|text> for http(s) only (hostile href never becomes a link),
    script and style are dropped, every text run is escaped, and invisible characters are removed.
    """
    parser = _MrkdwnHTMLParser()
    parser.feed(html_text)
    parser.close()
    return "".join(parser.out).strip("\n")


def printable(label: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in label)[:32]


def cmd_schema(only: str | None) -> int:
    if only is not None and only not in PROBES:
        print(f"unknown probe {only!r}; one of: {', '.join(PROBES)}", file=sys.stderr)
        return 2
    failed = 0
    for name in [only] if only else list(PROBES):
        answer = validate([PROBES[name]])
        pointer, values = allowed_values(answer)
        if values is None:
            failed += 1
            first = (answer.get("errors") or [{}])[0]
            print(f"{name}: FAILED, no enum ({answer.get('error')}: {first.get('message')})")
        else:
            print(f"{name} ({pointer}): {', '.join(map(str, values))}")
    return 1 if failed else 0


def cmd_validate(path: str) -> int:
    answer = validate(load_blocks(path))
    if answer.get("ok"):
        print("OK (shape only: no surface check, no per-message totals; dry-post checks the surface)")
        return 0
    print(f"FAILED: {answer.get('error')}")
    for error in answer.get("errors") or []:
        constraint = json.dumps(error.get("constraint") or {})[:300]
        print(f"{error.get('pointer')}: {error.get('message')} {constraint}")
    return 1


def read_token() -> str:
    if sys.stdin.isatty():
        raise SystemExit("give the bot token on stdin (a file or a secret manager), never as an argument")
    token = sys.stdin.read().strip()
    if not token.startswith("xox"):
        raise SystemExit("stdin carried no Slack token")
    return token


def _delete(token: str, channel: str, message_id: str) -> str | None:
    """None when the message is deleted (or already gone), else the error."""
    try:
        body = post(
            "chat.deleteScheduledMessage",
            {"channel": channel, "scheduled_message_id": message_id},
            token,
            pause=1.1,
        )
    except SlackError as error:
        return str(error)
    if body.get("ok") or body.get("error") == "invalid_scheduled_message_id":
        return None
    return str(body.get("error"))


def queued(token: str, channel: str) -> list[dict]:
    """Every scheduled message in the channel, all pages. Raises SlackError when the list is unreadable."""
    found: list[dict] = []
    cursor = None
    while True:
        body = post(
            "chat.scheduledMessages.list",
            {"channel": channel, "limit": "100", "cursor": cursor},
            token,
            pause=1.1,
        )
        if not body.get("ok") or not isinstance(body.get("scheduled_messages"), list):
            raise SlackError(
                f"chat.scheduledMessages.list: {body.get('error') or 'no list in the answer'}"
            )
        found += body["scheduled_messages"]
        cursor = (body.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return found


def settle(token: str, channel: str, created: dict[str, str], deleted: set[str]) -> int:
    """Delete what this run left, read the queue back, report. Returns how many ids are in doubt."""
    for message_id, where in created.items():
        if message_id not in deleted and _delete(token, where, message_id) is None:
            deleted.add(message_id)
    doubt = set(created) - deleted
    try:
        doubt |= {m.get("id") for m in queued(token, channel)} & set(created)
    except SlackError as error:
        print(f"queue NOT read back ({error}): every id of this run may still be queued")
        doubt = set(created)
    for message_id in sorted(doubt):
        print(f"STILL QUEUED {message_id}: run `blockkit.py cleanup {channel}` before it posts")
    if not doubt:
        print(f"queue read back: none of the {len(created)} scheduled messages is left")
    return len(doubt)


def cmd_dry_post(channel: str, path: str) -> int:
    token = read_token()
    blocks = load_blocks(path)
    jobs = [
        (printable(str(b.get("block_id") or f"{i:02d} {b.get('type')}")), [b])
        for i, b in enumerate(blocks)
    ]
    jobs.append(("ALL TOGETHER", blocks))
    created: dict[str, str] = {}  # scheduled_message_id -> the channel id Slack resolved
    deleted: set[str] = set()
    resolved = channel
    failed = 0
    base = int(time.time()) + WEEK
    try:
        for index, (label, payload) in enumerate(jobs):
            body = post(
                "chat.scheduleMessage",
                {
                    "channel": resolved,
                    "text": MARKER,
                    "blocks": json.dumps(payload),
                    "post_at": str(base + index * SPACING),
                    "unfurl_links": "false",
                    "unfurl_media": "false",
                },
                token,
            )
            if not body.get("ok"):
                failed += 1
                messages = (body.get("response_metadata") or {}).get("messages") or []
                print(f"{label:32s} {body.get('error')} {' | '.join(messages)[:300]}", flush=True)
                continue
            resolved = body.get("channel") or resolved
            message_id = body["scheduled_message_id"]
            created[message_id] = resolved
            error = _delete(token, resolved, message_id)
            if error is None:
                deleted.add(message_id)
                print(f"{label:32s} OK", flush=True)
            else:
                failed += 1
                print(f"{label:32s} accepted, but the delete failed: {error}", flush=True)
    finally:
        doubt = settle(token, resolved, created, deleted)
    return 1 if failed or doubt else 0


def cmd_cleanup(channel: str) -> int:
    token = read_token()
    left = [m for m in queued(token, channel) if m.get("text") == MARKER]
    errors = 0
    for message in left:
        error = _delete(token, message.get("channel_id") or channel, message["id"])
        errors += error is not None
        print(f"{message['id']}: {'deleted' if error is None else 'NOT deleted, ' + error}")
    print(f"dry-post messages found: {len(left)}, not deleted: {errors}")
    return 1 if errors else 0


def cmd_selftest() -> int:
    selftest_path = Path(__file__).resolve().parent / "selftest.py"
    return subprocess.run([sys.executable, "-I", str(selftest_path)]).returncode


def usage() -> int:
    print(__doc__.split("\n\n")[0], file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    match argv:
        case ["schema"]:
            return cmd_schema(None)
        case ["schema", "--type", probe]:
            return cmd_schema(probe)
        case ["validate", path]:
            return cmd_validate(path)
        case ["dry-post", channel, path]:
            return cmd_dry_post(channel, path)
        case ["cleanup", channel]:
            return cmd_cleanup(channel)
        case ["selftest"] | ["--selftest"]:
            return cmd_selftest()
    return usage()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SlackError as error:
        sys.exit(f"blockkit: {error}")
