---
name: drive-audit
description: >-
  Use when anything in Google Drive might be shared that should not be, or when a share has to be
  removed, reviewed or approved. Fires on "who can see this", "stop sharing", "make it private",
  "anyone with the link", "public", "shared with", "unshare", "revoke access", "sharing audit",
  "Freigabe", "nicht mehr teilen", "nur für mich". Owns the drive-audit CLI (scan, folder, revoke,
  downgrade, policy), how to read each finding class, what counts as an approved share, and how to
  keep a policy file so the next run is a diff rather than a re-read. Written after a folder whose
  own sharing said "private" turned out to hold thousands of publicly linked files.
license: Apache-2.0
compatibility: >-
  Requires Python 3.11+ with google-auth and google-api-python-client for anything that talks to
  Drive. The classifier and its 21-case selftest are standard library only and run with no
  credentials at all. Credentials come from Application Default Credentials or a service-account
  key named by an environment variable; no product-specific tooling and no agent-specific paths.
metadata:
  version: "1.0"
---

# drive-audit: who can see what in Drive, and which of that was approved

## The finding that made this exist

A folder in My Drive reported `shared: false` with exactly one permission, the owner. Inside it,
**all but two of its several thousand files carried `anyoneWithLink` reader**, readable by anyone
holding the URL: months of screenshots, published one per upload by a screenshot tool's "share to
Google Drive" output. Nobody had shared anything on purpose, and no check of the folder could have
found it.

Three lessons, and each one changes how you audit:
- **A clean folder proves nothing about its contents.** Exposure lives on the item.
- **Drive's own UI cannot answer this question.** There is no "show me everything that is shared" view, at any scale.
- **The cause usually keeps running.** Revoking is cleanup, not a fix, until the tool that published the links is changed. That folder grew while it was being audited.

## First run

One Python file plus its library, nothing packaged, so you can read every line before letting it near
your Drive.

```bash
# 0. the Google Cloud CLI, which step 5 of the auth recipe below needs. If you already have
#    credentials via $DRIVE_AUDIT_CREDENTIALS you can skip it.
#    https://cloud.google.com/sdk/docs/install   then:  gcloud init

# 1. dependencies, in a throwaway environment so nothing lands in a project env
python3 -m venv ~/.local/share/drive-audit/venv
~/.local/share/drive-audit/venv/bin/pip install google-auth google-api-python-client

# 2. check the classifier BEFORE granting any access. Needs no credentials at all.
python3 scripts/drive-audit --selftest
python3 scripts/example_report.py        # a full report built from invented findings

# 3. run it. The venv above is found automatically; or point DRIVE_AUDIT_PYTHON at any
#    interpreter that has those two libraries.
python3 scripts/drive-audit scan
```

Commands below are written as a bare `drive-audit` for brevity. Either call
`python3 scripts/drive-audit`, or put it on your PATH once with
`ln -s "$PWD/scripts/drive-audit" ~/.local/bin/drive-audit`.

## Run it

```bash
drive-audit scan                  # fast: the four public and domain visibility classes
drive-audit scan --deep           # + every owned item and every shared drive (slow)
drive-audit folder <FOLDER_ID>    # one folder and its DIRECT CHILDREN, not the subtree
drive-audit policy                # which policy file is in effect
drive-audit --selftest            # offline, no credentials, no Google libraries

# Mutating. Every one of these is a DRY RUN until --apply.
drive-audit revoke --under <ID>            # the DIRECT CHILDREN of that folder, PUBLIC shares only
drive-audit revoke --file <ID>             # one named item, EVERY grant on it, not just public
drive-audit revoke --principal <EMAIL>     # everything shared with that address
drive-audit revoke --under <ID> --apply    # snapshot first, then remove
drive-audit downgrade --principal <EMAIL> --to reader --apply   # keep access, drop write
```

Exit `0` clean, `1` unapproved shares found **or nothing was scanned**. An empty pass is a failure
on purpose: a scan that reached nothing and printed a clean bill is the one wrong answer that reads
like a right one.

**Three things decide whether a mutation happens, and none of them is a formality:**

| | |
|---|---|
| `--apply` | absent, nothing is changed. Every mutating command is a dry run by default. |
| `--all` | required to act with **no** scope flag, because that scope is every public share you own. A missing `--under` should not silently become the whole account. |
| `--max-changes N` | refuses above `N` (default 500). A number much larger than you expected means the scope is wrong, and re-reading a scope is cheaper than restoring from a snapshot. Raising it is a decision to make **after** reading the dry run, not a reflex when the tool objects: the cleanups this was written for ran to thousands of permissions, so hitting the ceiling is normal and being asked to look again is the point. |

**`--under` is the direct children of one folder, not its subtree**, and it targets public shares
only. To reach a non-public grant, name the item with `--file` (which covers every grant on it) or
the person with `--principal`. There is no recursive mode: a subtree sweep is exactly the operation
that should be assembled one folder at a time and looked at in between.

**`scan` and `scan --deep` are not the same question.** The fast scan uses Drive's `visibility =`
operator, which only knows the public and domain classes. **There is no query operator for "shared
with a specific person"**, so shares to named individuals are invisible until `--deep` enumerates
every owned item. Never report "nothing is shared" from a fast scan alone.

## Credentials

Resolved in this order, so neither a personal account nor a delegated service account needs a code
change:

1. `$DRIVE_AUDIT_CREDENTIALS`, a service-account JSON key, optionally impersonating `$DRIVE_AUDIT_IMPERSONATE` for domain-wide delegation.
2. `$GOOGLE_APPLICATION_CREDENTIALS`, the same thing under Google's own variable name.
3. Application Default Credentials.

### A personal Google account, in full

**A bare `gcloud auth application-default login` is not enough.** It mints credentials against
Google's own shared client, which carries no Drive scope, so every call returns an insufficient-scope
error. You need a client of your own, and these are the whole five steps:

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com/projectcreate).
2. Enable the Drive API at [console.cloud.google.com/apis/library/drive.googleapis.com](https://console.cloud.google.com/apis/library/drive.googleapis.com).
3. Configure the OAuth consent screen as **External** and add your own address under **Test users**. An unverified app is limited to its test users, which is what you want for a tool only you run.
   **But a project left in publishing status Testing issues a refresh token that expires after seven days**, so a yearly audit will find its credential dead every time. Either re-run step 5 before each run, or set the publishing status to **In production**, which for an app that only ever reads your own data is a status change rather than a review, and it stops the expiry.
4. Create an **OAuth client ID** of type **Desktop app**, and download the JSON.
5. Authorise with that client and the Drive scope:

```bash
gcloud auth application-default login \
  --client-id-file=/path/to/your-oauth-client.json \
  --scopes=https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/cloud-platform
```

The consent screen will warn that the app is unverified. The app is yours.

A service-account key is the wrong tool for a personal account: a service account has no access to
your files, and it cannot impersonate you without a Workspace domain to delegate from.

Two things that will otherwise cost you an afternoon:

- **The scope is the full `drive` scope, and it has to be.** `drive.file` restricts an app to files it created itself, so it cannot see, let alone address, anything that already existed. An auditor scoped that way reports a clean Drive and is simply blind.
- **A service account has its own empty Drive.** Without an impersonation subject, every call succeeds against nothing at all. That is the worst kind of clean run, and it is why the subject is a separate variable rather than an optional extra.

With delegation, Google matches the granted scope **string** exactly and does not honour hierarchy,
so a scope that was not granted separately fails at token exchange with `unauthorized_client` rather
than per call.

## Reading the report

Classes print worst first, and that order is the severity judgement:

| Class | What it means | Default action |
|---|---|---|
| `public_indexed` | `anyone` with `allowFileDiscovery: true`. Listed in search, findable without the URL. | revoke, unless deliberately published |
| `public_link` | `anyone`, link only. The URL alone is the credential. | revoke unless the link was handed out on purpose |
| `external_writer` | somebody outside the account can EDIT | verify the person, then approve or revoke |
| `external_reader` | somebody outside the account can read | often legitimate: approve it in the policy |
| `external_group` | a group can reach it | check the group's membership, which the ACL does not show |
| `internal_writer` / `internal_reader` | a NAMED person in your own domain | a deliberate grant to one person: approve or revoke by name |
| `domain_indexed` / `domain_link` | anybody in your domain, no name attached | decide once for the domain, not per file |

**`allowFileDiscovery` is the whole severity axis.** It is the difference between "somebody has to be
given the URL" and "it turns up in a search". Treating the two as one class understates the
exposure, which is the direction that costs.

**A named insider is not the same finding as a domain-wide grant**, and folding them together is
wrong on any domain with more than one account: a colleague granted access by name is one person and
a decision somebody made, while "anyone in the domain" is neither. On a single-account domain the
insider classes are simply empty, so the correct behaviour costs nothing there.

Two rows that are not findings to chase:
- **`inherited`** items are descendants of a share set on an ancestor. Fix the ancestor and they follow. The report collapses them on purpose, because listing four hundred children of one shared folder buries the row that matters.
- **`approved by policy`** items matched a policy entry. `--show-approved` lists them with the reason.

## The policy file is the point

A second run that re-reads the whole inventory gets skipped, and a skipped audit is worth nothing.
So every deliberate share goes into a policy file with **a reason and a date**, and the next run
reports the residue rather than the inventory.

`drive-audit policy` prints the resolution order: `$DRIVE_AUDIT_POLICY` first, because the file
records decisions and belongs in version control next to whatever else records decisions, then
`~/.config/drive-audit/policy.toml`.

```toml
[[approved_principal]]        # a person or service that may keep access
email = "advisor@example.com"
reason = "outside accountant, the folder they work in"
since = "2026-05-26"
roles = ["reader"]            # optional: an approval for reader does NOT cover writer

[[approved_tree]]             # a folder whose direct children may stay shared
folder_id = "0B_EXAMPLE_FOLDER_ID"
reason = "why this folder is shared"
since = "2026-05-26"

[[approved_public]]           # one file that is published on purpose
file_id = "0B_EXAMPLE_FILE_ID"
reason = "linked from a public page"
since = "2026-01-01"

[[approved_domain]]           # a whole domain that may keep access
domain = "example.com"
reason = "why the domain is trusted"
since = "2026-01-01"
```

**`roles` is the escalation guard.** Without it, an approval for `reader` keeps covering the same
principal after somebody upgrades them to `writer`, which is exactly the change an audit exists to
notice. Pin it on anyone who should never gain write.

**A policy entry with no reason is a blanket suppression nobody can review next year.** That is how a
baseline stops being a baseline. A missing policy file is never an error, though: the first run has
nothing to diff against and must still work.

## Revoking

`revoke` is a dry run unless `--apply`. With `--apply` it writes
`~/.local/share/drive-audit/drive-acl-snapshot-<timestamp>.json` **before the first delete**, holding
the file id, permission id, principal, role and discovery flag of everything it removes. That file is
what makes the operation reversible: one row restores with a single `permissions.create`.

**Say the consequence before running it, not after.** A revoked link is dead everywhere that URL was
ever pasted: a doc, a ticket, a chat message, an email. For a few thousand files that is the real
price of the fix, and the snapshot is the only way back.

Three outcomes are **not** errors, and a tool that counts them as errors trains you to ignore its
numbers:

- **`404 Permission not found` is success.** It means the permission is already gone, which is the state you asked for. On any resumed sweep this is the common case, because a listing is stale the moment the previous attempt removed anything. Measured at roughly a third of one re-run batch.
- **`403 cannotModifyInheritedPermission` / `cannotDeletePermission`** means the item is the wrong target. See below.
- **A finding already in the desired state.**

Deletes are paced, a pause every hundred, because Drive allows roughly 12.000 queries per 60 s per
user and a few thousand deletes fired flat out earns `rateLimitExceeded` partway through. Half done
with no record of where it stopped is the worst available outcome.

## When a revoke reports "decided by an ancestor folder"

Not an error, and the exit code will not be 1. The grant is not set at the level you addressed, and
there are two cases. The tool prints which:

1. **The carrying folder is reachable.** Re-run with `--file <FOLDER_ID>` against that folder and every descendant follows in one call. Acting on descendants individually is not merely wasteful, Drive refuses it. Use `--file` rather than `--under`: an item you name explicitly is treated as explicit intent and covers **every** grant on it, while `--under` and the no-scope case stay public-only, because they can sweep thousands of items. For a named person, `--principal <EMAIL>`.
2. **The item is ORPHANED.** `files.get` returns no parent, because the folder the grant was set on no longer exists. The access outlived its own parent and **no API call at any reachable level can remove it.**

**Case 2 is a real dead end, and saying so beats retrying.** Measured: a former collaborator still
held write on several hundred items, about a third of them orphaned, and **every** delete attempt
returned `403 cannotDeletePermission`. The only fix is to move the items into a folder the account
controls, which breaks the inheritance chain, and then revoke. **That reorganises somebody's Drive,
so propose it and let them decide; never do it as part of a cleanup.**

Google does not document this case. The error string is real and reproducible, but we found no page
describing an orphaned item whose grant cannot be removed at any reachable level, so read the
paragraph above as our own measurement rather than as vendor guidance. Related reading, covering
inherited permissions generally and not this case:
[permissions.delete](https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions/delete).

The same rule applies to `downgrade`: a role cannot be lowered below what an ancestor grants, so an
over-privileged share on an orphaned item cannot be reduced either.

## Two API traps worth knowing before you write your own

- **`files.list` silently drops `permissionDetails`**, while `permissions.list` on the same file returns it. No error either way. That field carries `inherited`, so without it a shared folder and its several hundred children look like as many independent decisions. A per-item `permissions.list` is correct and costs one request per item, which is not viable across a whole Drive.
  **Do not reach for the cheap reconstruction, and this is the trap rather than the tip.** It is tempting to treat "the child carries the same permission id as an ancestor" as proof of inheritance. It is not proof of anything: **a Drive permission id identifies the GRANTEE, not the grant.** Verified on a live account, one person's id was identical on every item they could reach, in unrelated folders. So the test cannot tell an inherited grant from a second, independent grant to the same person, and using it to suppress a row hides a real share while printing a clean bill. This tool keeps it strictly as an ORDERING hint (act on the folder first, because Drive removes descendants' inherited access when the folder's grant goes) and lets Drive arbitrate: it rejects a genuinely inherited target with a 403, which is a correct answer from the only party that actually knows.
- **A `visibility =` query also returns other people's public files.** It answers "what public thing can I see", not "what am I exposing", and Drive returns no usable `permissions` for files you do not own, so they classify as nothing while still inflating the count. In one measured run, most `anyoneCanFind` hits were strangers' documents. Split on `ownedByMe`.

Shared drives need `supportsAllDrives=True` **and** `includeItemsFromAllDrives=True`, plus
`corpora='drive'` with `driveId` to scope to one. Miss either flag and whole drives are invisible
with no error at all.

**A shared drive also costs extra requests, unavoidably.** `files.list` does not populate
`permissions` for a shared-drive item, so a listing alone reports a clean drive while having read no
ACL whatsoever. This tool asks Drive who needs a closer look: `hasAugmentedPermissions` marks the
items carrying a grant beyond what the drive itself gives, and only those get a `permissions.list`.
The drive's own top-level ACL is fetched once per drive, because that is where its membership lives
and no file inside it carries that grant.

## After a revoke, close the source

A revoke that leaves the publisher running is a chore you have scheduled forever.

- **A screenshot or capture tool with a Drive output.** Several create a public link per upload, because a link is what they hand you. Turn that setting off, or point the tool at a local folder.
- **Any app with Drive write access**, at `myaccount.google.com/permissions`. An app that published on upload once will do it again.
- **Legacy sync folders.** Very old file ids often carry a domain-wide grant from a default that no longer applies.

## Verify before you believe it

```bash
drive-audit --selftest      # 21 cases, no credentials, no network, no Google libraries
```

That it runs with no credentials is the point: the classification logic is checkable before this
tool is granted any access to anything. `scripts/example_report.py` prints a full report from
invented findings, so the output format can be read without a Drive at all.

## What this does not do yet

Stated here rather than discovered by you, because an audit tool that is quiet about its blind spots
is worse than one that has none:

- **A file published to the web is not detected.** That permission is scoped to a separate view, and reading it needs a request parameter this tool does not pass, so a published Doc appears in no class at all. If publish-to-web is a risk you care about, check it by hand.
- **There is no `restore` command.** The snapshot carries everything needed (`type`, `role`, `allowFileDiscovery`, the address, `expirationTime`, `view`), and restoring a row is one `permissions.create` per row with `sendNotificationEmail=false`, but you write that loop yourself today. Undo is therefore a manual operation, which is a reason to read a dry run rather than to rely on the snapshot.
- **`--under` is one folder's direct children, never a subtree.** There is deliberately no recursive mode.
- **Shared-drive coverage is newer than the rest**, and costs a request per item that carries its own grant. Treat a first run against a large shared drive as something to check rather than trust.
- **The inheritance signal is an ordering hint and nothing more.** See the traps section: it cannot prove inheritance, so it never suppresses a row.

## Prior art

[GAM](https://github.com/GAM-team/GAM) prints the same ACLs
(`gam <user> print filelist fields permissions`, and `print drivefileacls` per file) and is the
better tool for any ad-hoc Drive question or Workspace-wide reporting. Prefer it there.

Two gaps are why this exists, both checked against GAM's own command reference rather than assumed.
**It has no saved baseline of approved shares** for a later run to diff against, so every run
re-reads the whole inventory. And while its documented workflow is to get the current ACLs before
deleting, **`delete permissions` has no option to write out what it removes**, so the snapshot is a
step you remember rather than a property of the revoke. Neither is a criticism: GAM is a Drive CLI,
and this is a recurring audit.

## Status

Built and verified against the Drive API v3 in September 2026, on a Workspace account holding tens of
thousands of items. The measurements in this file were taken on real runs rather than estimated: the
inheritance-id finding, the 404 rate on a resumed sweep, the orphaned-grant dead end, and the share
of a visibility query that turns out to be other people's files.

What ships here is verifiable without credentials: `scripts/drive-audit --selftest` is 21 offline
cases over the classifier, the policy engine and the ordering, and `scripts/example_report.py` prints
a full report from invented findings. Both the severity split and the policy escalation guard were
falsified by mutation rather than only confirmed, which is a stronger claim than a passing suite: the
old behaviour was reintroduced and the cases went red.

Two limits worth knowing before you rely on it. **Shared-drive coverage costs extra requests and is
newer than the rest**, so treat a first run against a large shared drive as something to check rather
than trust. And **a file published to the web is not yet detected**: that permission is scoped to a
separate view, and reading it needs a request parameter this tool does not yet pass, so a published
Doc will not appear in any class. If publishing-to-web is a risk you care about, check it by hand
until that is fixed.
