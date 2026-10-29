---
name: blockkit-forge
description: Use when building a Slack Block Kit message from real data that a bot will post, update or thread, such as a notification card, a ticket or case log, a digest, or a status that changes. Covers the live enums (read from blocks.validate, which knows more block types than the docs), a dry-post as the bot (a message scheduled a week out, deleted, the queue read back), Builder preview, text from another system made safe (escape order, cut before escape, verbatim, HTML to mrkdwn, URLs), scannable cards, collapsed long text, status pills, people tagged without a ping, live updates and thread timelines, fallbacks and read-back. Triggers on Block Kit, blocks, chat.postMessage, chat.update, mrkdwn, invalid_blocks, Block Kit Builder, a Slack card, a notification to Slack. Not for learning Block Kit from zero (Slack's own block-kit skill does that), modals or Home tabs, or a message written in a person's own voice.
license: Apache-2.0
compatibility: Python 3.10+, standard library only. schema and validate need no token, only HTTPS to slack.com. dry-post and cleanup need your own bot token with chat:write, and the bot in the target channel. scripts/make_illustration.py alone needs Pillow 10.1+ (tested on 12.3.0). Agent-agnostic.
metadata:
  version: "1.0.0"
  changeSummary: First version, built while shipping a production CRM-to-Slack notifier.
  isBreaking: "false"
---

# blockkit-forge: Block Kit from real data, proven before anyone sees it

A Block Kit message fails in three quiet ways. Slack refuses it (`invalid_blocks`) and the job dies at
3 a.m. It renders, but text from another system pings the channel or breaks a link. Or it is correct
and unreadable, because twenty of them stack into a wall. This skill handles all three, and it never
trusts memory for the schema: Slack ships new blocks every few weeks.

**It complements Slack's official `block-kit` skill** in
[slackapi/slack-skills-plugin](https://github.com/slackapi/slack-skills-plugin) (MIT), which teaches
surfaces and blocks from the live docs and also calls `blocks.validate`. Learn Block Kit there. This
skill starts where that one ends: a bot posting data it did not write, again and again, into one
channel.

![An invented notification card on the left: the header "Northwind Ltd: Migrate the nightly reporting jobs", a grey line with the ticket number and an italic created time, a purple "In Process" status pill, a two-by-three grid with Requester, Manager (cc) and Owner as blue tags and Priority, Category and Due as text, a collapsed full-width "Request" box with a one-line preview, and an "Open in the record" button. On the right, its thread: a status note with "New" and "In Process" as code chips joined by an arrow, a comment, and a reply, each with tagged names and an italic time](../../docs/assets/blockkit-forge-example.png)

The picture is in the repository's `docs/assets/`, so this folder stays text-only. Every name and
number in it is invented. `scripts/make_illustration.py` draws it, and
[examples/card.json](examples/card.json) is the same card as real blocks.

## What you need

- **For `schema` and `validate`**: nothing but HTTPS to slack.com. No account, no token.
- **For `dry-post` and `cleanup`**: a Slack app with the bot scope `chat:write`, installed to the
  workspace, and invited to the channel (`/invite @yourbot`). Its bot token (`xoxb-`) goes on stdin
  from a file or a secret manager. The channel is its id (`C...`, from the channel's About panel),
  not its name.
- **For a production bot built on these patterns**: `chat:write`, plus `channels:history` or
  `groups:history` (private channel) to read its own posts back, plus `users:read` and
  `users:read.email` if it turns names or emails into `<@U...>` tags.

## The method, in order

1. **Read the live enums, not the docs.** `scripts/blockkit.py schema` asks `blocks.validate` with a
   deliberately wrong value for each probed field, and Slack answers with the allowed values. On
   2026-10-22 its block-type enum named 34 types, about 14 of them on no official page. Three of
   those (`callout`, `condition`, `contact_card`) were also accepted in a bot message.
   **Undocumented blocks are for exploration, not for a production bot**: if you use one anyway, keep
   a documented fallback and look at it on desktop and phone.
   [references/catalogue.md](references/catalogue.md) has both lists.
2. **Design for scanning**, because nobody reads a channel: one card per item, the state as a coloured
   pill, a field grid with even rows, long text folded, one button, a divider.
   [references/patterns.md](references/patterns.md).
3. **Treat every string from another system as hostile.** Decode, cut, then escape `&` first, then
   `<` and `>`. Set `verbatim: true` on every `mrkdwn` text object that carries it, never send
   `link_names`, and prefer `plain_text` and `rich_text` where no formatting is needed. Escape the
   top-level `text` too. [references/untrusted-text.md](references/untrusted-text.md).
4. **Validate the shape**: `python3 -I scripts/blockkit.py validate payload.json`. No token. It checks
   shape only: not the surface, and not the per-message totals (two `data_visualization` blocks,
   12,000 `markdown` characters).
5. **Dry-post as the bot**:
   `op read op://vault/slack-bot/token | python3 -I scripts/blockkit.py dry-post C0123ABCD payload.json`
   (any secret manager, or `< token-file`; never `echo` the token, which writes it to shell
   history). Each block alone, then the whole message, scheduled a week out and deleted at once, then
   the queue read back. It is a live write with a safety margin, not a sandbox: every id is recorded,
   a crash still deletes and reads back, and any doubt exits non-zero and names the ids
   (`blockkit.py cleanup CHANNEL` removes them). It tests the blocks only. The top-level `text`,
   unfurl settings and metadata are yours to check.
6. **Look at it.** Block Kit Builder renders desktop and mobile. A payload in its URL hash did not load
   on 2026-10-22 (it reset to `{}`); paste the JSON into the editor instead. Then post, and look at
   the real channel at desktop and phone width.
7. **Post, then prove it.** Read the message back from the channel. Keep a minimal fallback card for
   `invalid_blocks`. Record the `ts` before the next post, so a crash cannot post twice.

## Rules worth knowing (2026-10-22; [docs] where Slack states it, [measured] where we saw it)

- [measured] `blocks.validate` returns allowed enums ahead of the docs. Errors carry a JSON pointer.
- [docs] `header.level` takes 1 to 4. [measured] Its error text says "must be less than 4" anyway.
- [docs] `container` with `is_collapsible` and `default_collapsed` folds, and `width: full` spans the
  message. [measured] Its `subtitle` stayed visible while it was folded, on desktop.
- [docs] A `<@U...>` in an app's message notifies the user only if they are already a channel member.
- [docs] `mrkdwn` text objects default to `verbatim: false`, which turns some bare names into mentions.
- [measured] A tag ADDED BY AN EDIT (`chat.update`) did not notify a channel member in one live test,
  so "post plain, then edit in the tag" can show real tags without a ping. Undocumented: test it on
  yourself once per workspace before you rely on it.
- [measured] `chat.postMessage` with `thread_ts` can land outside the thread if the parent is gone.
  Compare the response's `message.thread_ts` with the parent, and delete a misplaced reply.
- [docs] A scheduled message due within 60 seconds cannot be deleted. [measured] One deleted minutes
  before its time was still delivered, so schedule test messages a week out.
- [docs] Slack schedules at most 30 messages per 5 minutes per channel; `dry-post` spaces them.
- [measured] **Never post with a `username` or `icon_*` override if the bot must edit or delete that
  message later.** The bot's own `chat.delete` answered `cant_delete_message`. For a different name
  or picture, use a dedicated app. [references/patterns.md](references/patterns.md), "Posting
  identity".

## Scripts

- `scripts/blockkit.py schema [--type T]`, `validate FILE`, `dry-post CHANNEL FILE`, `cleanup CHANNEL`,
  `selftest` (token on stdin). Retries a rate limit, refuses redirects, and exits non-zero on any failure.
  `escape_mrkdwn`, `cut_then_escape` and `html_to_mrkdwn` are importable reference implementations of step 3.
- `scripts/selftest.py`: offline checks of the enum parser, loading, escaping, argument handling, the
  retry, the token guard and every dry-post failure path. No network.
- `scripts/make_illustration.py`: draws the example picture (Pillow).

## Related

- Slack's `block-kit` skill for Block Kit itself.
