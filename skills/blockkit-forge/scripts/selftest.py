# SPDX-License-Identifier: Apache-2.0
# The copyright holder is named in the LICENSE file of this skill.
"""Purpose: offline checks of blockkit.py: enum parsing, payload loading, escaping, argument handling,
the rate-limit retry, the refused redirect, the stdin token guard, and every dry-post failure path. No network.
Usage: python3 -I selftest.py

Exits non-zero on the first failure, and prints how many checks ran, so a silent pass is visible.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True  # a __pycache__ in a skill folder is a HIGH scan finding
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import blockkit  # noqa: E402

CHECKS = 0


def check(name: str, condition: bool) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise SystemExit(f"FAIL {name}")


def raises(kind: type[BaseException], call) -> bool:
    try:
        call()
    except kind:
        return True
    return False


def quiet(call):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return call()


# A real blocks.validate answer for an invalid callout colour, recorded 2026-10-22.
RECORDED = {
    "ok": False,
    "error": "invalid_blocks",
    "errors": [
        {
            "code": "failed_constraint",
            "constraint": {
                "type": "enum",
                "expected": ["indigo", "blue", "jade", "pink", "gray", "purple", "orange", "brown", "green"],
            },
            "message": "must be a valid enum value",
            "pointer": "/0/background_color",
        }
    ],
}

pointer, values = blockkit.allowed_values(RECORDED)
check("enum pointer", pointer == "/0/background_color")
check("enum values", values is not None and "jade" in values and len(values) == 9)
check("no enum in an ok answer", blockkit.allowed_values({"ok": True}) == (None, None))
check(
    "a non-enum error yields nothing",
    blockkit.allowed_values({"ok": False, "errors": [{"constraint": {"type": "one_of"}}]}) == (None, None),
)

# Payload loading, and the shipped example.
with tempfile.TemporaryDirectory() as tmp:
    as_list = Path(tmp) / "a.json"
    as_list.write_text(json.dumps([{"type": "divider"}]))
    as_object = Path(tmp) / "b.json"
    as_object.write_text(json.dumps({"blocks": [{"type": "divider"}, {"type": "divider"}]}))
    bad = Path(tmp) / "c.json"
    bad.write_text(json.dumps({"blocks": "nope"}))
    check("load a bare list", len(blockkit.load_blocks(str(as_list))) == 1)
    check("load a blocks object", len(blockkit.load_blocks(str(as_object))) == 2)
    check("refuse a non-list", raises(SystemExit, lambda: blockkit.load_blocks(str(bad))))
example = blockkit.load_blocks(str(HERE.parent / "examples" / "card.json"))
check("the example card loads", [b["type"] for b in example][:3] == ["header", "context", "rich_text"])

# Escaping: `&` first, and cut before escape.
check("escape order", blockkit.escape_mrkdwn("&lt;") == "&amp;lt;")
check("a mention is neutralised", blockkit.escape_mrkdwn("<!here> <@U1>") == "&lt;!here&gt; &lt;@U1&gt;")
cut = blockkit.cut_then_escape("abcd&efgh", 6)
check("cut before escape keeps entities whole", cut == "abcd&amp;…")
check("no cut when it fits", blockkit.cut_then_escape("a<b", 3) == "a&lt;b")

# HTML to mrkdwn conversion.
check("html paragraph break", blockkit.html_to_mrkdwn("<p>a</p><p>b</p>") == "a\nb")
check("html two paragraphs is two lines", len(blockkit.html_to_mrkdwn("<p>a</p><p>b</p>").splitlines()) == 2)
check("html paragraphs with newline whitespace", blockkit.html_to_mrkdwn("<p>a</p>\n<p>b</p>") == "a\nb")
check("html bold conversion", blockkit.html_to_mrkdwn("<b>bold</b> and <strong>strong</strong>") == "*bold* and *strong*")
check("html italic conversion", blockkit.html_to_mrkdwn("<i>italic</i> and <em>em</em>") == "_italic_ and _em_")
check("html inline tags separated by space", blockkit.html_to_mrkdwn("<b>a</b> <i>b</i>") == "*a* _b_")
check("html br tag breaks line", blockkit.html_to_mrkdwn("line 1<br>line 2") == "line 1\nline 2")
check("html list item bullets", blockkit.html_to_mrkdwn("<ul><li>one</li><li>two</li></ul>") == "• one\n• two")
check("html list with indented items", blockkit.html_to_mrkdwn("<ul>\n  <li>one</li>\n  <li>two</li>\n</ul>") == "• one\n• two")
check("html valid link", blockkit.html_to_mrkdwn('<a href="https://example.com/page?x=1&y=2">click</a>') == "<https://example.com/page?x=1&amp;y=2|click>")
check("html link encodes pipe", blockkit.html_to_mrkdwn('<a href="https://example.com/a|b">pipe</a>') == "<https://example.com/a%7Cb|pipe>")
check("html hostile href javascript", blockkit.html_to_mrkdwn('<a href="javascript:alert(1)">evil</a>') == "evil")
check("html hostile href closing tag", blockkit.html_to_mrkdwn('<a href="https://example.com/foo>bar">evil</a>') == "evil")
check("html link strips invisible bidi in href", blockkit.html_to_mrkdwn('<a href="https://example.com/\u202etest">link</a>') == "<https://example.com/test|link>")
check("html script and style dropped", blockkit.html_to_mrkdwn("hi<script>alert(1)</script><style>b{color:red}</style> there") == "hi there")
check("html text run escaping", blockkit.html_to_mrkdwn("<p>&lt;!channel&gt; &amp; Tom & Jerry</p>") == "&lt;!channel&gt; &amp; Tom &amp; Jerry")
check("html invisible characters stripped", blockkit.html_to_mrkdwn("a\u200bb\ufeffc\u202ad") == "abcd")

# Probes and argument handling.
check(
    "every probe carries the sentinel or level 9",
    all(blockkit.NOPE in json.dumps(p) or '"level": 9' in json.dumps(p) for p in blockkit.PROBES.values()),
)
for argv in ([], ["schema", "--type"], ["schema", "bogus"], ["validate"], ["dry-post", "C1"], ["nope"]):
    check(f"usage exits 2 for {argv}", quiet(lambda: blockkit.main(argv)) == 2)
check("an unknown probe exits 2", quiet(lambda: blockkit.cmd_schema("nope")) == 2)

# A redirect is refused, so the token never follows it.
check("redirects refused", blockkit._NoRedirect().redirect_request(None, None, 302, "", {}, "x") is None)


class FakeResponse(io.BytesIO):
    def __init__(self, body: dict, headers: dict | None = None):
        super().__init__(json.dumps(body).encode())
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


real_open, real_sleep = blockkit.OPENER.open, blockkit.time.sleep
blockkit.time.sleep = lambda seconds: None
try:
    answers = iter([FakeResponse({"ok": False, "error": "ratelimited"}, {"Retry-After": "3"}), FakeResponse({"ok": True})])
    blockkit.OPENER.open = lambda request, timeout: next(answers)
    check("a ratelimited answer is retried", blockkit.post("x", {}) == {"ok": True})
    blockkit.OPENER.open = lambda request, timeout: FakeResponse({"ok": False, "error": "ratelimited"})
    check("rate limit past the retries raises", raises(blockkit.SlackError, lambda: blockkit.post("x", {})))
finally:
    blockkit.OPENER.open, blockkit.time.sleep = real_open, real_sleep

# schema exits non-zero when a probe yields no enum.
real_validate = blockkit.validate
blockkit.validate = lambda blocks: {"ok": False, "error": "invalid_auth"}
check("a failed probe exits 1", quiet(lambda: blockkit.cmd_schema("container.width")) == 1)
blockkit.validate = lambda blocks: RECORDED
check("a probe with an enum exits 0", quiet(lambda: blockkit.cmd_schema("container.width")) == 0)
blockkit.validate = real_validate

# The stdin token guard.
real_stdin = sys.stdin
sys.stdin = io.StringIO("not a token")
check("stdin without a token is refused", raises(SystemExit, blockkit.read_token))
sys.stdin = io.StringIO("xoxb-test\n")
check("a token on stdin is read", blockkit.read_token() == "xoxb-test")
sys.stdin = real_stdin


def fake_slack(delete_ok: bool = True, list_ok: bool = True, queue: tuple = (), schedule_raises_at: int | None = None):
    """A stand-in for blockkit.post that records every call."""
    state = {"calls": [], "scheduled": 0}

    def post(method: str, params: dict, token=None, pause=0.0) -> dict:
        state["calls"].append((method, params.get("scheduled_message_id"), params.get("channel")))
        if method == "chat.scheduleMessage":
            state["scheduled"] += 1
            if schedule_raises_at == state["scheduled"]:
                raise blockkit.SlackError("chat.scheduleMessage: HTTP 500")
            return {"ok": True, "channel": "C9", "scheduled_message_id": f"Q{state['scheduled']}"}
        if method == "chat.deleteScheduledMessage":
            return {"ok": True} if delete_ok else {"ok": False, "error": "channel_not_found"}
        if method == "chat.scheduledMessages.list":
            if not list_ok:
                return {"ok": False, "error": "missing_scope"}
            return {"ok": True, "scheduled_messages": [{"id": i} for i in queue]}
        raise AssertionError(method)

    return post, state


def dry_run(post) -> int:
    real_post, real_token = blockkit.post, blockkit.read_token
    blockkit.post, blockkit.read_token = post, lambda: "xoxb-test"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "p.json"
            payload.write_text(json.dumps([{"type": "divider"}, {"type": "divider"}]))
            return quiet(lambda: blockkit.cmd_dry_post("C1", str(payload)))
    finally:
        blockkit.post, blockkit.read_token = real_post, real_token


post, state = fake_slack()
check("a clean dry-post exits 0", dry_run(post) == 0)
check("a clean dry-post scheduled block, block, all", state["scheduled"] == 3)
check(
    "deletes use the channel id Slack resolved",
    {c for m, _, c in state["calls"] if m == "chat.deleteScheduledMessage"} == {"C9"},
)
check("a failed delete exits 1", dry_run(fake_slack(delete_ok=False)[0]) == 1)
check("an unreadable queue exits 1", dry_run(fake_slack(list_ok=False)[0]) == 1)
check("an id of this run still in the queue exits 1", dry_run(fake_slack(queue=("Q2",))[0]) == 1)
check("another run's message in the queue is not ours", dry_run(fake_slack(queue=("ZZ",))[0]) == 0)
post, state = fake_slack(schedule_raises_at=2)
check("a crash mid-run still raises", raises(blockkit.SlackError, lambda: dry_run(post)))
check("a crash mid-run still reads the queue back", any(m == "chat.scheduledMessages.list" for m, _, _ in state["calls"]))

print(f"selftest: {CHECKS} checks passed")
