"""Google-Drive sharing audit: who can see what, and which of that was approved.

WHY THIS EXISTS
---------------
A single folder in a My Drive held several thousand files carrying `anyoneWithLink` reader: months
of screenshots, published one per upload by a screenshot tool's "share to Google Drive" output. The
folder's own ACL was clean, so neither the Drive UI nor any check of the folder would ever have
shown it. Nobody had shared anything on purpose.

That is the shape of the problem this module exists for. Exposure accumulates per FILE, silently,
from a tool nobody is watching, and a Drive with 3 TB in it cannot be reviewed by eye.

WHAT MAKES IT RE-RUNNABLE RATHER THAN A ONE-OFF DUMP
----------------------------------------------------
A raw ACL listing of a real Drive is useless, because most shares are deliberate: an
accountant's folder, a partner, a shared drive's members. So every finding is diffed against a
POLICY file recording which principals and which published files were approved, each with a reason
and a date.
The yearly run then reports the residue, not the inventory. Without that, the second run is as
expensive to read as the first and gets skipped.

Prior art: GAM (`gam <user> print filelist fields permissions`) prints the same ACLs and does it
well. Checked against its command reference: it has no saved baseline of approved
shares for a later run to diff against, and `delete permissions` has no option to write out what it
removes, so a snapshot is a step you remember rather than a property of the revoke. Those two are
the whole reason this is not a GAM invocation, and they are not a criticism of GAM: it is a Drive
CLI, and this is a recurring audit.

NO GOOGLE IMPORT AT MODULE SCOPE
--------------------------------
The classifier, the policy loader and the renderer import nothing from Google, and every function
that talks to Drive resolves its client inside the call. That is what lets `--selftest` run under a
plain interpreter with no Google libraries and no credentials, which in turn means a reader can
verify the classification logic before granting this thing any access at all.

AUTH IS A SEAM, NOT A DEPENDENCY
--------------------------------
One function, `_service()`, is the only place a credential is resolved, and it tries four things in
order so that neither a personal Google account nor a delegated service account needs a code change:

  1. a factory installed with `set_service_factory()`, which is how the tests substitute a fake;
  2. `$DRIVE_AUDIT_CREDENTIALS`, a service-account JSON key, optionally impersonating
     `$DRIVE_AUDIT_IMPERSONATE` (domain-wide delegation);
  3. `$GOOGLE_APPLICATION_CREDENTIALS`, the same thing under Google's own variable name;
  4. Application Default Credentials, which is what `gcloud auth application-default login` leaves
     behind and the path most people already have.

The scope is the full `drive` scope, deliberately, and this is the single most common reason a
third-party Drive auditor silently reports nothing: `drive.file` restricts an app to files it made
itself, so it cannot see, let alone address, anything that already existed.

Delegation has one trap worth stating: Google matches the granted scope STRING EXACTLY and does not
honour hierarchy, so a scope that was not granted separately fails at token exchange with
`unauthorized_client` rather than per call.

TWO API FACTS THIS RELIES ON, BOTH MEASURED
-------------------------------------------
  * `visibility = 'anyoneWithLink'` (also `anyoneCanFind`, `domainWithLink`, `domainCanFind`) is a
    real `q` operator and returns results. It is the only cheap way to ask "what is public".
  * There is NO `q` operator for "shared with some specific person". `readers` / `writers` need the
    address in advance. That is why finding people-shares costs a full enumeration (`--deep`) and
    cannot be made fast.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

# Drive's own vocabulary, worst first: "CanFind" means listed in search, "WithLink" means
# reachable by URL only.
VISIBILITY_CLASSES = ("anyoneCanFind", "anyoneWithLink", "domainCanFind", "domainWithLink")

# Field mask for any listing whose ACLs we intend to classify.
#
# `permissionDetails` is requested and WILL NOT COME BACK. Measured: `files.list`
# silently drops it, while `permissions.list` on the very same file returns it. It stays in the mask
# because it costs nothing and because a future API fix would then just work, but nothing may rely
# on it: `mark_inherited` reconstructs inheritance from permission-id overlap instead, and an
# un-reconstructed finding is reported as direct, which over-reports rather than staying silent.
#
# `ownedByMe` is load-bearing, not decoration. A `visibility =` query also returns PUBLIC FILES
# OWNED BY STRANGERS that the account happened to open once (most `anyoneCanFind` hits in one
# measured run were other people's documents). Those are not this account's exposure, and counting
# them as findings would inflate the report with things nobody here can fix.
# The permission fields, on their own, for `permissions.list`. Shared-drive items need that call
# because `files.list` will not carry their ACLs.
PERMISSION_FIELDS = (
    "permissions(id, type, role, emailAddress, domain, allowFileDiscovery, deleted, "
    "expirationTime, view, permissionDetails(permissionType, role, inherited))"
)

SHARING_FIELDS = (
    "id, name, mimeType, parents, owners(emailAddress), ownedByMe, hasAugmentedPermissions, "
    "webViewLink, shared, "
    "permissions(id, type, role, emailAddress, domain, allowFileDiscovery, deleted, "
    "expirationTime, view, permissionDetails(permissionType, role, inherited))"
)

# Finding kinds, ordered worst to least bad. The order IS the report order, so this tuple is the
# one place the severity judgement lives.
KIND_ORDER = (
    "public_indexed",
    "public_link",
    "external_writer",
    "external_reader",
    "external_group",
    "internal_writer",
    "internal_reader",
    "domain_indexed",
    "domain_link",
)

KIND_LABEL = {
    "public_indexed": "PUBLIC and search-indexed (anyone can find it)",
    "public_link": "public by link (anyone with the URL can open it)",
    "external_writer": "a person outside the account can EDIT it",
    "external_reader": "a person outside the account can read it",
    "external_group": "a group can reach it",
    "internal_writer": "a named person in your own domain can EDIT it",
    "internal_reader": "a named person in your own domain can read it",
    "domain_indexed": "anyone in the domain can find it",
    "domain_link": "anyone in the domain can open it",
}

WRITE_ROLES = ("writer", "organizer", "fileOrganizer")

# A CONSUMER MAIL DOMAIN IS NOT "YOUR ORGANISATION", and treating it as one inverts the whole
# insider/outsider split. The internal classes exist to say "somebody inside your org", which is
# derived from the credential's own domain. On a personal account that domain is gmail.com, so every
# stranger on earth with a Gmail address would have classified as an insider and sorted BELOW real
# outsiders. Anything here is always external, and the internal classes stay empty, which is the
# truthful answer for an account that has no organisation.
CONSUMER_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "yahoo.co.uk",
        "icloud.com",
        "me.com",
        "mac.com",
        "proton.me",
        "protonmail.com",
        "gmx.de",
        "gmx.net",
        "web.de",
        "aol.com",
        "zoho.com",
        "yandex.ru",
        "mail.ru",
        "qq.com",
        "163.com",
    }
)


class IncompleteSearchError(RuntimeError):
    """Drive answered with `incompleteSearch: true`, so the result set is not the whole answer."""

# Drive refuses to remove or lower a permission on an item that inherits that access from a direct
# OR INDIRECT parent, with a 403 and this reason. It is not a failure and not a permission problem:
# it means the item is the wrong place to act, and the ancestor carrying the share is the right one.
# Measured: on one run, nearly every attempt in a batch of a few thousand came back this way,
# because the grant sat on a tree of folders and the files below them only inherited it. Counting
# these as errors hid the one fact that mattered: the whole batch was aimed at the wrong level.
# Two reasons, same family: the permission is not set at the level you are addressing.
#   cannotModifyInheritedPermission - a reachable ancestor grants it, so act on the ancestor.
#   cannotDeletePermission          - it is inherited and the ancestor is NOT reachable. Measured
#     on a real account: a large minority of the affected items were ORPHANED (no visible parent),
#     because the folder the grant was set on no longer exists. The access survives its own parent,
#     and no API call at this level can remove it. The only fix is to re-parent the items into a
#     folder the owner controls, which breaks the inheritance, and that is a structural change to
#     somebody's Drive rather than an audit action.
INHERIT_BLOCKED = ("cannotModifyInheritedPermission", "cannotDeletePermission")


@dataclass
class ChangeResult:
    """Outcome of a revoke or a downgrade, with the four cases kept apart.

    A single (count, errors) pair cannot express this: "already in the desired state" and "blocked
    because an ancestor decides it" are both non-failures, and lumping either into `errors` makes a
    correct run look broken and an exit code lie.
    """

    changed: int = 0
    already: int = 0
    blocked_by_ancestor: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.blocked_by_ancestor


@dataclass
class Finding:
    """One permission on one item that is not the owner's own."""

    file_id: str
    name: str
    mime_type: str
    parents: tuple[str, ...]
    link: str
    permission_id: str
    kind: str
    role: str
    principal: str
    inherited: bool
    # Both are needed to RESTORE, not to classify. A grant with an expiry restored without one comes
    # back permanent, which is a quiet privilege escalation performed by the undo path; and a
    # published-to-web permission is scoped to a view, which a restore must reproduce.
    expiration_time: str | None = None
    view: str | None = None
    approved_reason: str | None = None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME_TYPE

    @property
    def approved(self) -> bool:
        return self.approved_reason is not None

    @property
    def permission_type(self) -> str:
        if self.kind.startswith("public"):
            return "anyone"
        if self.kind.startswith("domain"):
            return "domain"
        return "group" if self.kind == "external_group" else "user"


@dataclass
class Policy:
    """Approved shares, each carrying why and since when.

    Shape follows `.skillspector-baseline.yaml`: an exception without a reason and a date is how a
    baseline turns into a blanket suppression nobody can audit later.
    """

    principals: dict[str, dict[str, Any]] = field(default_factory=dict)
    public: dict[str, dict[str, Any]] = field(default_factory=dict)
    trees: dict[str, dict[str, Any]] = field(default_factory=dict)
    domains: dict[str, dict[str, Any]] = field(default_factory=dict)
    source: str = "(none)"

    @property
    def is_empty(self) -> bool:
        return not (self.principals or self.public or self.trees or self.domains)


def policy_candidates() -> tuple[Path, ...]:
    """Where a policy file may live, in resolution order.

    `$DRIVE_AUDIT_POLICY` first, because the file records decisions and therefore belongs in version
    control next to whatever else records decisions, which is somewhere only you know. The XDG-ish
    default is the fallback for a machine where nobody has chosen.
    """
    paths: list[Path] = []
    override = os.environ.get("DRIVE_AUDIT_POLICY")
    if override:
        paths.append(Path(override).expanduser())
    paths.append(Path.home() / ".config" / "drive-audit" / "policy.toml")
    return tuple(paths)


def load_policy(path: Path | None = None) -> Policy:
    """Read the policy, or return an empty one. A missing file is never an error.

    Failing closed on a missing policy would make the very first run useless, and that is the run
    that matters most.
    """
    import tomllib  # noqa: PLC0415 - stdlib, but keeps the import cost off the selftest path

    for candidate in (path,) if path else policy_candidates():
        if candidate and candidate.is_file():
            data = tomllib.loads(candidate.read_text(encoding="utf-8"))
            return policy_from_dict(data, str(candidate))
    return Policy()


def policy_from_dict(data: dict[str, Any], source: str) -> Policy:
    def keyed(rows: Iterable[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
        return {str(row[key]): row for row in rows if row.get(key)}

    return Policy(
        principals=keyed(data.get("approved_principal", []), "email"),
        public=keyed(data.get("approved_public", []), "file_id"),
        trees=keyed(data.get("approved_tree", []), "folder_id"),
        domains=keyed(data.get("approved_domain", []), "domain"),
        source=source,
    )


def _reason(entry: dict[str, Any]) -> str:
    reason = str(entry.get("reason") or "approved").strip()
    since = entry.get("since")
    return f"{reason} (since {since})" if since else reason


def _is_inherited(permission: dict[str, Any]) -> bool:
    """True when the permission comes from an ancestor rather than from this item.

    An absent `permissionDetails` counts as NOT inherited, which over-reports rather than
    under-reports. Silence about a real exposure is the expensive direction.
    """
    return any(detail.get("inherited") for detail in permission.get("permissionDetails") or ())


def _kind(permission: dict[str, Any], owner_domain: str) -> str | None:
    """Map one Drive permission onto a finding kind, or None when it is not an exposure."""
    ptype = permission.get("type")
    role = permission.get("role", "")
    discoverable = bool(permission.get("allowFileDiscovery"))

    if ptype == "anyone":
        return "public_indexed" if discoverable else "public_link"
    if ptype == "domain":
        return "domain_indexed" if discoverable else "domain_link"

    if ptype == "group":
        return "external_group"
    if ptype == "user":
        if role == "owner":
            return None
        email = (permission.get("emailAddress") or "").lower()
        organisational = bool(owner_domain) and owner_domain not in CONSUMER_DOMAINS
        if organisational and email.endswith("@" + owner_domain):
            # A NAMED person inside the same domain, kept as its own class rather than folded into
            # `domain_link`. The fold was the original behaviour and it is wrong anywhere the domain
            # has more than one account: a colleague granted access by name is a decision somebody
            # made about one file, and reporting it as "anyone in the domain" both overstates the
            # reach and hides who actually has it. On a one-person domain the class is just empty,
            # so the correct behaviour costs nothing there.
            return "internal_writer" if role in WRITE_ROLES else "internal_reader"
        return "external_writer" if role in WRITE_ROLES else "external_reader"
    return None


def classify(
    items: Iterable[dict[str, Any]],
    policy: Policy,
    owner_email: str,
) -> list[Finding]:
    """Every permission on every item that is not the owner's own, with the policy applied.

    Approved findings are returned too, flagged rather than dropped, so the report can say how many
    were suppressed and by which rule. A suppression nobody can count is a suppression nobody
    reviews.
    """
    owner_email = owner_email.lower()
    owner_domain = owner_email.partition("@")[2]
    findings: list[Finding] = []

    for item in items:
        parents = tuple(item.get("parents") or ())
        for permission in item.get("permissions") or ():
            if permission.get("deleted"):
                continue
            email = (permission.get("emailAddress") or "").lower()
            if permission.get("type") == "user" and email == owner_email:
                continue

            kind = _kind(permission, owner_domain)
            if kind is None:
                continue

            finding = Finding(
                file_id=item.get("id", ""),
                name=item.get("name", "(unnamed)"),
                mime_type=item.get("mimeType", ""),
                parents=parents,
                link=item.get("webViewLink", ""),
                permission_id=permission.get("id", ""),
                kind=kind,
                role=permission.get("role", ""),
                principal=email or permission.get("domain") or "anyone",
                inherited=_is_inherited(permission),
                expiration_time=permission.get("expirationTime"),
                view=permission.get("view"),
            )
            finding.approved_reason = _approval(finding, policy)
            findings.append(finding)

    findings.sort(key=lambda f: (KIND_ORDER.index(f.kind), not f.is_folder, f.name))
    return findings


def _http_reason(exc: BaseException) -> str:
    """The Drive error `reason` we care about, or "".

    Matched against the string rather than parsed out of the JSON body, because `HttpError` exposes
    the body differently across client versions and a parse that works today would fail silently
    later: a reason we stop recognising would go back to counting as a hard error.
    """
    text = str(exc)
    return next((reason for reason in INHERIT_BLOCKED if reason in text), "")


def drop_covered_by_ancestor(
    findings: Iterable[Finding],
    items: Iterable[dict[str, Any]],
) -> tuple[list[Finding], int]:
    """Keep only the topmost finding per principal in each folder tree. Returns (kept, dropped).

    A grant on a folder reaches every descendant, and Drive will not let you remove or lower it
    below the level it was set at. So aiming at 3.190 inheriting files is not merely wasteful, it is
    refused; aiming at the root folders achieves the same thing and is allowed.

    THIS IS A HINT AND MUST NEVER SUPPRESS. The inference it rests on is UNSOUND, and the
    measurement
    that settles it: a Drive permission id is the GRANTEE's global identifier, not a per-grant
    token.
    Verified against a live account: one grantee carried a single permission id on every item it
    appeared on, spread across many unrelated folders. So identical ids on a parent and a child
    prove only that the same person appears on both, never that the child's grant came from the
    parent. Two independent grants to the
    same person look identical to this function.

    Suppressing on that basis fails in the SILENT direction: a real, independently-set share would
    be
    dropped from the report and from the revoke set, and the run would still print a clean bill. So
    callers order by this and never filter by it. Drive itself is the arbiter on the act path: it
    rejects a genuinely inherited target with a 403 that `revoke` already classifies as
    not-an-error, which is a correct answer obtained from the only party that actually knows.
    """
    findings = list(findings)
    parent_of = {item["id"]: tuple(item.get("parents") or ()) for item in items}
    carries: set[tuple[str, str]] = {(f.file_id, f.principal) for f in findings}

    def covered(finding: Finding) -> bool:
        seen: set[str] = set()
        queue = list(finding.parents)
        while queue:
            node = queue.pop()
            if node in seen:
                continue
            seen.add(node)
            if (node, finding.principal) in carries:
                return True
            queue.extend(parent_of.get(node, ()))
        return False

    kept = [f for f in findings if not covered(f)]
    return kept, len(findings) - len(kept)


def owned_only(
    items: Iterable[dict[str, Any]],
    *,
    absent_is_mine: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a listing into (this account's items, somebody else's items).

    A `visibility =` query answers "what is public that I can see", which is not the same question
    as "what am I exposing". Other people's public documents come back too, and Drive returns no
    usable `permissions` for them, so they would silently classify as nothing while still inflating
    the scanned count. Splitting makes both numbers honest.

    `absent_is_mine` EXISTS BECAUSE THE FIELD IS NOT ALWAYS THERE. Google documents `ownedByMe` as
    not populated for items in a shared drive, where the drive owns the file and no user does. So an
    absent value is genuinely ambiguous, and the two callers want opposite defaults:

      * REPORTING wants `True`. Over-reporting an item that turns out not to be ours costs a line a
        human dismisses; dropping one costs the finding the audit exists for.
      * ACTING wants `False`. Deleting a permission on something we do not own is the one mistake
        with a blast radius outside this account, so the mutating paths pass `absent_is_mine=False`
        and an ambiguous item is simply not touched.
    """
    owned: list[dict[str, Any]] = []
    foreign: list[dict[str, Any]] = []
    for item in items:
        mine = item.get("ownedByMe", absent_is_mine)
        (owned if mine else foreign).append(item)
    return owned, foreign


def mark_inherited(findings: Iterable[Finding]) -> int:
    """HINT ONLY: flag findings whose grantee also appears on a parent folder in the result set.

    Read the warning on `drop_covered_by_ancestor` before using this for anything but ordering. The
    same-permission-id test cannot distinguish an inherited grant from a second independent grant to
    the same person, so this may never remove a row from a report, a revoke set or an exit code.

    This exists because `files.list` does not return `permissionDetails` (see `SHARING_FIELDS`).
    Drive reuses one permission id per principal across a tree, so a child carrying the same
    permission id as its parent got it from the parent. Returns how many were marked.

    Limits, stated because a partial signal read as a complete one is worse than none: it can only
    see parents that are themselves in the scanned set, and only direct parents. So a zero here
    means "no inheritance was provable", never "nothing is inherited".
    """
    findings = list(findings)
    folder_permissions: dict[str, set[str]] = {}
    for finding in findings:
        if finding.is_folder:
            folder_permissions.setdefault(finding.file_id, set()).add(finding.permission_id)

    marked = 0
    for finding in findings:
        if finding.inherited or finding.is_folder:
            continue
        for parent in finding.parents:
            if finding.permission_id in folder_permissions.get(parent, ()):
                finding.inherited = True
                marked += 1
                break
    return marked


def _approval(finding: Finding, policy: Policy) -> str | None:
    """The policy rule that covers this finding, or None.

    A principal entry may pin `roles`. An approval for `reader` must NOT silently cover `writer`: a
    role escalation on an already-approved share is exactly the change worth catching.
    """
    entry = policy.principals.get(finding.principal)
    if entry:
        roles = entry.get("roles")
        if not roles or finding.role in roles:
            return _reason(entry)
        # A PINNED ROLE IS AUTHORITATIVE, and this return is the whole guarantee. Falling through
        # from here let an `approved_tree` covering the same item re-approve a principal who had
        # just been upgraded to writer, which is precisely the escalation the pin exists to catch.
        # Documenting the hole would have been the wrong fix: a guard with an exception nobody
        # remembers is not a guard.
        return None

    if finding.kind in ("public_indexed", "public_link"):
        entry = policy.public.get(finding.file_id)
        if entry:
            return _reason(entry)

    if finding.kind in ("domain_indexed", "domain_link"):
        entry = policy.domains.get(finding.principal)
        if entry:
            return _reason(entry)

    for parent in finding.parents:
        entry = policy.trees.get(parent)
        if entry:
            return _reason(entry)

    return None


DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive",)

_SERVICE_FACTORY: Callable[[], Any] | None = None


def set_service_factory(factory: Callable[[], Any] | None) -> None:
    """Install the callable that returns a Drive v3 client, or None to go back to auto-detection.

    The seam exists so that an estate with its own credential plumbing can hand one in, and so the
    tests can substitute a fake without touching the network. Everything else in this module reaches
    Drive only through `_service()`.
    """
    global _SERVICE_FACTORY  # noqa: PLW0603 - one process-wide client is the point
    _SERVICE_FACTORY = factory
    _service.cache_clear()


@cache
def _service() -> Any:
    """The Drive client. Cached, because rebuilding it per call re-does discovery every time."""
    if _SERVICE_FACTORY is not None:
        return _SERVICE_FACTORY()

    from google.oauth2 import service_account  # noqa: PLC0415
    from googleapiclient.discovery import build  # noqa: PLC0415

    key_path = os.environ.get("DRIVE_AUDIT_CREDENTIALS") or os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )
    subject = os.environ.get("DRIVE_AUDIT_IMPERSONATE")

    if key_path:
        resolved = Path(key_path).expanduser()
        if not resolved.is_file():
            raise SystemExit(
                f"The credential named by the environment does not exist: {resolved}\n"
                "Point DRIVE_AUDIT_CREDENTIALS at a service-account JSON key, or unset it to use "
                "Application Default Credentials."
            )
        creds = service_account.Credentials.from_service_account_file(
            str(resolved), scopes=list(DRIVE_SCOPES)
        )
        # A service account has its OWN empty Drive. Without a subject, every call here would
        # succeed against nothing at all, which is the worst kind of clean run.
        if subject:
            creds = creds.with_subject(subject)
    else:
        import google.auth  # noqa: PLC0415

        creds, _ = google.auth.default(scopes=list(DRIVE_SCOPES))

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _http_error_class() -> type[BaseException]:
    from googleapiclient.errors import HttpError  # noqa: PLC0415

    return HttpError


def fetch_visibility(visibility: str, *, page_size: int = 1000) -> list[dict[str, Any]]:
    """Every item in every reachable drive matching one `visibility =` class."""
    return _page(
        _service(),
        q=f"visibility = '{visibility}' and trashed = false",
        page_size=page_size,
    )


def fetch_owned(
    *,
    page_size: int = 1000,
    progress: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    """Every non-trashed item the impersonated user OWNS, with its ACLs.

    Only owned items can leak the user's own data through the user's own ACLs, so this is a real
    reduction against "everything visible" rather than an arbitrary filter.
    """
    return _page(
        _service(),
        q="'me' in owners and trashed = false",
        page_size=page_size,
        progress=progress,
    )


def fetch_children(folder_id: str, *, page_size: int = 1000) -> list[dict[str, Any]]:
    """Every non-trashed direct child of one folder, with its ACLs."""
    return _page(
        _service(),
        q=f"'{folder_id}' in parents and trashed = false",
        page_size=page_size,
    )


def fetch_principal(email: str, *, page_size: int = 1000) -> list[dict[str, Any]]:
    """Every item shared with one named address, with its ACLs.

    This is the one case where a people-share IS cheaply queryable: `in readers` / `in writers` need
    the address up front, which is useless for discovery but exactly right once a `--deep` scan has
    told you who to look for. Revoking a former collaborator therefore costs two queries, not a full
    enumeration.
    """
    safe = email.replace("'", "\\'")
    return _page(
        _service(),
        q=f"('{safe}' in readers or '{safe}' in writers) and trashed = false",
        page_size=page_size,
    )


def fetch_files(file_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Specific items by id, with their ACLs. One `files.get` each.

    Needed because a share set ON a folder is not a share on any of its children, so `--under`
    cannot reach it. The folder itself has to be addressed directly.
    """
    service = _service()
    out: list[dict[str, Any]] = []
    for file_id in file_ids:
        out.append(
            service.files()
            .get(fileId=file_id, fields=SHARING_FIELDS, supportsAllDrives=True)
            .execute()
        )
    return out


def change_role(
    findings: Iterable[Finding],
    new_role: str,
    *,
    dry_run: bool = True,
    pause_every: int = 100,
    pause_seconds: float = 1.0,
    report: Callable[[str], None] = print,
) -> ChangeResult:
    """Lower a permission's role in place, keeping access.

    Kept separate from `revoke` because it is a different decision: revoking removes somebody's
    access, downgrading keeps a working relationship and only takes away write. For a share that is
    legitimate but over-privileged, revoking would break something a person actually uses, so the
    two must not share a code path or a flag.

    Drive refuses to lower a role below what an ancestor grants (`cannotModifyInheritedPermission`),
    so the same ancestor rule as `revoke` applies: aim at the folder that carries the grant.
    """
    import time  # noqa: PLC0415

    service = None
    http_error: type[BaseException] = Exception
    if not dry_run:
        service = _service()
        http_error = _http_error_class()

    result = ChangeResult()

    for index, finding in enumerate(findings, start=1):
        if finding.role == new_role:
            result.already += 1
            continue
        if dry_run:
            report(
                f"  [would downgrade] {finding.principal:24s} {finding.role:8s} -> "
                f"{new_role:8s} {finding.name}"
            )
            result.changed += 1
            continue
        try:
            service.permissions().update(
                fileId=finding.file_id,
                permissionId=finding.permission_id,
                body={"role": new_role},
                supportsAllDrives=True,
            ).execute()
            result.changed += 1
        except http_error as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status == 404:
                result.already += 1
            elif (reason := _http_reason(exc)):
                # Carry the REASON, not just the id. The two reasons mean different next actions,
                # and the documentation claimed the tool said which; discarding it here was what
                # made that claim false.
                result.blocked_by_ancestor.append(
                    f"{reason}: {finding.file_id} ({finding.name})"
                )
            else:
                result.errors.append(f"{finding.file_id} ({finding.name}): {exc}")
        if index % pause_every == 0:
            report(
                f"  … {index} processed, {result.changed} changed, "
                f"{len(result.blocked_by_ancestor)} owned by an ancestor, "
                f"{len(result.errors)} error(s)"
            )
            time.sleep(pause_seconds)

    return result


def fetch_shared_drive(
    drive_id: str,
    *,
    page_size: int = 1000,
    progress: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    """Every non-trashed item in one shared drive, WITH its ACLs filled in separately.

    `files.list` DOES NOT RETURN `permissions` FOR A SHARED-DRIVE ITEM. Google states it plainly:
    the field is "not populated for items in shared drives". A listing therefore comes back with no
    ACLs at all, every item classifies as carrying nothing, and the run reports a clean shared drive
    while having looked at nothing. That is the exact failure this tool exists to prevent, so it is
    not acceptable to paper over with a caveat in the documentation.

    The fix costs requests, and the cost is bounded by asking Drive who needs one.
    `hasAugmentedPermissions` tells us whether an item carries any permission BEYOND what the drive
    itself grants, so only those items need a `permissions.list`. Everything else is covered by the
    drive's own top-level ACL, which is fetched once and attached to the drive root rather than
    copied onto thousands of files.
    """
    from googleapiclient.errors import HttpError  # noqa: PLC0415

    service = _service()
    items = _page(
        service,
        q="trashed = false",
        page_size=page_size,
        drive_id=drive_id,
        progress=progress,
    )

    augmented = [i for i in items if i.get("hasAugmentedPermissions")]
    for index, item in enumerate(augmented, start=1):
        try:
            item["permissions"] = (
                service.permissions()
                .list(
                    fileId=item["id"],
                    fields=PERMISSION_FIELDS,
                    supportsAllDrives=True,
                    pageSize=100,
                )
                .execute()
                .get("permissions", [])
            )
        except HttpError:
            # Leaving it absent would read as "no permissions", so mark it instead: an item whose
            # ACL could not be read is a gap in the audit, not a clean row.
            item["permissions"] = []
            item["_acl_unreadable"] = True
        if progress and index % 100 == 0:
            progress(index)

    return items


def fetch_drive_acl(drive_id: str) -> list[dict[str, Any]]:
    """The shared drive's OWN top-level permissions, which is where most of its access lives.

    Read once per drive rather than per item. Without this the members of a shared drive are
    invisible, because no file inside it carries their grant.
    """
    from googleapiclient.errors import HttpError  # noqa: PLC0415

    try:
        return (
            _service()
            .permissions()
            .list(
                fileId=drive_id,
                fields=PERMISSION_FIELDS,
                supportsAllDrives=True,
                useDomainAdminAccess=False,
                pageSize=100,
            )
            .execute()
            .get("permissions", [])
        )
    except HttpError:
        return []


def list_shared_drives() -> list[dict[str, Any]]:
    return (
        _service()
        .drives()
        .list(pageSize=100, fields="drives(id, name, createdTime)")
        .execute()
        .get("drives", [])
    )


def whoami_email() -> str:
    """The address the credential actually acts as.

    Read from the live client rather than from configuration, because with delegation the two can
    disagree, and everything downstream (which permission is "the owner's", which domain counts as
    internal) is decided from this value.
    """
    return (
        _service()
        .about()
        .get(fields="user(emailAddress)")
        .execute()
        .get("user", {})
        .get("emailAddress", "")
    )


def _page(
    service: Any,
    *,
    q: str,
    page_size: int,
    drive_id: str | None = None,
    progress: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        params: dict[str, Any] = {
            "q": q,
            "fields": f"nextPageToken, incompleteSearch, files({SHARING_FIELDS})",
            "pageSize": page_size,
            "pageToken": token,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if drive_id:
            params.update(corpora="drive", driveId=drive_id)
        response = service.files().list(**params).execute()
        # A partial search must never render as a clean bill. Drive sets this when it did not reach
        # every document, and the field was previously masked away, so a truncated result and a
        # genuinely empty one were indistinguishable: the one wrong answer that reads like a right
        # one. Raising is correct here rather than warning, because every caller's output is a
        # statement about what exists.
        if response.get("incompleteSearch"):
            raise IncompleteSearchError(
                "Drive reported incompleteSearch=true for the query "
                f"{q!r}: some results were not searched, so this run cannot be read as complete. "
                "Narrow the query (a single drive, a smaller corpus) and re-run."
            )
        out.extend(response.get("files", []))
        if progress:
            progress(len(out))
        token = response.get("nextPageToken")
        if not token:
            return out


def revoke(
    findings: Iterable[Finding],
    *,
    dry_run: bool = True,
    pause_every: int = 100,
    pause_seconds: float = 1.0,
    report: Callable[[str], None] = print,
) -> ChangeResult:
    """Delete the permission behind each finding.

    Paced rather than looped flat out: Drive allows roughly 12.000 queries per 60 s per user, and
    a few thousand deletes fired back to back earns a 403 `rateLimitExceeded` part of the way in,
    which is the worst outcome (half done, no record of where it stopped).

    THREE OUTCOMES ARE NOT ERRORS AND ARE COUNTED APART, each measured on a real run:
      * 404 `Permission not found` means it is already gone, which is the state we asked for. On a
        resumed sweep this is the common case (37% of one batch), because any listing is stale the
        moment the previous attempt removed something.
      * 403 `cannotModifyInheritedPermission` means the item inherits the access from an ancestor,
        so the item is the wrong target rather than a failure. Nearly a whole batch came back this
        way once, where the grant sat on a tree of folders. Feed these back through
        `drop_covered_by_ancestor` and aim at the roots.
      * a finding already in the desired state.

    A DRY RUN IMPORTS NOTHING FROM GOOGLE, so it stays runnable on a machine with no venv.
    """
    import time  # noqa: PLC0415

    service = None
    http_error: type[BaseException] = Exception
    if not dry_run:
        service = _service()
        http_error = _http_error_class()

    result = ChangeResult()

    for index, finding in enumerate(findings, start=1):
        if dry_run:
            report(f"  [would revoke] {finding.principal:24s} {finding.role:8s} {finding.name}")
            result.changed += 1
            continue
        try:
            service.permissions().delete(
                fileId=finding.file_id,
                permissionId=finding.permission_id,
                supportsAllDrives=True,
            ).execute()
            result.changed += 1
        except http_error as exc:  # keep going: one locked item must not abort the batch
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status == 404:
                result.already += 1
            elif (reason := _http_reason(exc)):
                # Carry the REASON, not just the id. The two reasons mean different next actions,
                # and the documentation claimed the tool said which; discarding it here was what
                # made that claim false.
                result.blocked_by_ancestor.append(
                    f"{reason}: {finding.file_id} ({finding.name})"
                )
            else:
                result.errors.append(f"{finding.file_id} ({finding.name}): {exc}")
        if index % pause_every == 0:
            report(
                f"  … {index} processed, {result.changed} removed, {result.already} already gone, "
                f"{len(result.blocked_by_ancestor)} owned by an ancestor, "
                f"{len(result.errors)} error(s)"
            )
            time.sleep(pause_seconds)

    return result


def snapshot_path(directory: Path, now: datetime | None = None) -> Path:
    """A path that cannot collide with another run's.

    Seconds and the pid are both here on purpose. The stamp used to stop at minutes, and
    `write_text` truncates, so two revokes inside one minute left only the second batch restorable
    while the tool still printed that everything could be re-created. Seconds alone are not enough
    either: two processes can start in the same second, and this tool is one somebody runs twice in
    a hurry when the first scope was wrong.
    """
    stamp = (now or datetime.now()).strftime("%Y-%m-%d-%H%M%S")
    return directory / f"drive-acl-snapshot-{stamp}-{os.getpid()}.json"


def write_snapshot(findings: Iterable[Finding], path: Path) -> Path:
    """Record every permission about to be removed, so all of it can be re-added.

    This is what makes a several-thousand-file revoke a decision rather than a gamble: the ids,
    permission ids, the principals and the roles are all here, and restoring one row is a single
    `permissions.create`.
    """
    rows = [
        {
            "file_id": f.file_id,
            "name": f.name,
            "permission_id": f.permission_id,
            "type": f.permission_type,
            "role": f.role,
            "principal": f.principal,
            "allow_file_discovery": f.kind.endswith("_indexed"),
            "expiration_time": f.expiration_time,
            "view": f.view,
            "kind": f.kind,
            "link": f.link,
        }
        for f in findings
    ]
    payload = {
        "written": datetime.now().isoformat(timespec="seconds"),
        "note": "Restore a row with permissions.create(fileId, body={type, role, "
        "allowFileDiscovery, emailAddress, expirationTime}); pass view= when it is set, and "
        "sendNotificationEmail=false so a restore does not mail the grantee.",
        "count": len(rows),
        "permissions": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # "x" so a collision RAISES instead of overwriting somebody's only copy of an ACL. The caller
    # aborts rather than deleting permissions it can no longer restore, which is the right trade.
    with path.open("x", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    return path


def render(
    findings: list[Finding],
    *,
    scanned: int,
    owner_email: str,
    policy: Policy,
    show_approved: bool = False,
    limit: int = 20,
) -> list[str]:
    """The report. Folders first inside every class, because a folder carries its descendants.

    A count of what was scanned is printed above a measurement of what was actually reached, because
    a count alone reads as evidence when it is only a claim. The CALLER decides the exit status, and
    it follows the unapproved findings rather than this measurement, so do not read the pairing here
    as a guarantee about the exit code: the one place that is guaranteed is `scanned == 0`, which
    the CLI treats as a failed audit rather than a clean one.
    """
    lines: list[str] = []
    unapproved = [f for f in findings if not f.approved]
    approved = [f for f in findings if f.approved]

    lines.append(f"account: {owner_email}")
    # "no file" and "a file that approves nothing" are different facts and were printed as one.
    # The first is a setup gap, the second is a deliberate state, and conflating them told a reader
    # their policy file had not been found when it had.
    if policy.source == "(none)":
        suffix = "  (no policy file found: everything is reported)"
    elif policy.is_empty:
        suffix = "  (found, but approves nothing yet: everything is reported)"
    else:
        suffix = ""
    lines.append(f"policy:  {policy.source}{suffix}")
    lines.append("")

    # Inherited rows are descendants of a share set somewhere above them. Reporting the source once
    # beats reporting the same share 400 times.
    sources = [f for f in unapproved if not f.inherited]
    inherited = [f for f in unapproved if f.inherited]

    for kind in KIND_ORDER:
        rows = [f for f in sources if f.kind == kind]
        if not rows:
            continue
        folders = sum(1 for f in rows if f.is_folder)
        lines.append(f"=== {kind}: {len(rows)} item(s), {folders} folder(s) -- {KIND_LABEL[kind]}")
        for f in rows[:limit]:
            marker = "DIR " if f.is_folder else "    "
            lines.append(
                f"  ! {marker}{f.name[:56]:56s} {f.role:8s} {f.principal[:32]:32s} {f.file_id}"
            )
        if len(rows) > limit:
            lines.append(f"    … {len(rows) - limit} more in this class (use --json for all)")
        lines.append("")

    if inherited:
        lines.append(
            f"=== inherited: {len(inherited)} item(s) reachable through a share set on an ancestor"
        )
        lines.append("    (not listed one by one: fix the ancestor and these follow)")
        lines.append("")

    if approved:
        lines.append(f"=== approved by policy: {len(approved)} item(s) suppressed")
        if show_approved:
            for f in approved:
                lines.append(f"    {f.name[:56]:56s} {f.principal[:32]:32s} {f.approved_reason}")
        lines.append("")

    reached = len({f.file_id for f in findings})
    lines.append(
        f"scanned {scanned} item(s); reached {reached} item(s) carrying a non-owner permission; "
        f"{len(sources)} unapproved share(s) set directly, {len(inherited)} inherited, "
        f"{len(approved)} approved"
    )
    return lines


def selftest() -> int:
    """Offline checks: no credentials, no network, no Google libs. Returns the failure count."""
    me = "owner@example.com"
    cases: list[tuple[str, Any, Any]] = []

    def item(**kw: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "id": "f1",
            "name": "x",
            "mimeType": "image/png",
            "parents": ["p1"],
            "permissions": [],
        }
        base.update(kw)
        return base

    owner_perm = {"id": "o", "type": "user", "role": "owner", "emailAddress": me}
    link = {"id": "anyoneWithLink", "type": "anyone", "role": "reader", "allowFileDiscovery": False}
    found = {"id": "anyone", "type": "anyone", "role": "reader", "allowFileDiscovery": True}

    # allowFileDiscovery is the whole difference between "reachable by URL" and "listed in search".
    # Getting it backwards understates the exposure, which is the direction that costs.
    cases.append(
        (
            "anyoneWithLink -> public_link",
            [f.kind for f in classify([item(permissions=[link])], Policy(), me)],
            ["public_link"],
        )
    )
    cases.append(
        (
            "anyone+discovery -> public_indexed",
            [f.kind for f in classify([item(permissions=[found])], Policy(), me)],
            ["public_indexed"],
        )
    )
    cases.append(
        ("owner permission ignored", classify([item(permissions=[owner_perm])], Policy(), me), [])
    )

    folder = item(mimeType=FOLDER_MIME_TYPE, permissions=[link])
    cases.append(
        ("folder detected", [f.is_folder for f in classify([folder], Policy(), me)], [True])
    )

    # Inheritance: a child of a shared folder is flagged, so the report can collapse it.
    child = item(
        permissions=[{**link, "permissionDetails": [{"inherited": True, "role": "reader"}]}]
    )
    cases.append(
        ("inherited flagged", [f.inherited for f in classify([child], Policy(), me)], [True])
    )

    pol = policy_from_dict(
        {
            "approved_principal": [
                {
                    "email": "advisor@example.com",
                    "reason": "advisor",
                    "since": "2026-01-01",
                    "roles": ["reader"],
                }
            ]
        },
        "test",
    )
    reader = {"id": "p", "type": "user", "role": "reader", "emailAddress": "advisor@example.com"}
    writer = {"id": "p", "type": "user", "role": "writer", "emailAddress": "advisor@example.com"}
    cases.append(
        (
            "approved reader suppressed",
            [f.approved for f in classify([item(permissions=[reader])], pol, me)],
            [True],
        )
    )
    cases.append(
        (
            "escalated writer NOT suppressed",
            [f.approved for f in classify([item(permissions=[writer])], pol, me)],
            [False],
        )
    )

    tree = policy_from_dict(
        {"approved_tree": [{"folder_id": "p1", "reason": "client folder, deliberately shared",
        "since": "2026-05-26"}]}, "test"
    )
    cases.append(
        (
            "approved tree suppresses child",
            [f.approved for f in classify([item(permissions=[link])], tree, me)],
            [True],
        )
    )

    # A NAMED insider is its own class, not "anyone in the domain". Folding the two overstates the
    # reach and hides who actually holds the grant, on any domain with more than one account.
    same = {"id": "p", "type": "user", "role": "reader", "emailAddress": "other@example.com"}
    cases.append(
        (
            "named same-domain reader -> internal_reader",
            [f.kind for f in classify([item(permissions=[same])], Policy(), me)],
            ["internal_reader"],
        )
    )
    same_w = {"id": "p", "type": "user", "role": "writer", "emailAddress": "other@example.com"}
    cases.append(
        (
            "named same-domain writer -> internal_writer",
            [f.kind for f in classify([item(permissions=[same_w])], Policy(), me)],
            ["internal_writer"],
        )
    )
    # And a real domain-wide grant still classifies as one, so the two are genuinely distinguished.
    dom = {"id": "d", "type": "domain", "role": "reader", "domain": "example.com"}
    cases.append(
        (
            "domain grant stays domain_link",
            [f.kind for f in classify([item(permissions=[dom])], Policy(), me)],
            ["domain_link"],
        )
    )
    # An insider outranks a domain-wide row in the report, because it names somebody.
    mixed_in = classify(
        [item(id="a", permissions=[dom]), item(id="b", permissions=[same])], Policy(), me
    )
    cases.append(
        (
            "named insider sorts above a domain grant",
            [f.kind for f in mixed_in],
            ["internal_reader", "domain_link"],
        )
    )

    cases.append(
        (
            "deleted permission ignored",
            classify([item(permissions=[{**link, "deleted": True}])], Policy(), me),
            [],
        )
    )

    outsider = {"id": "p", "type": "user", "role": "writer", "emailAddress": "who@elsewhere.com"}
    cases.append(
        (
            "outside writer -> external_writer",
            [f.kind for f in classify([item(permissions=[outsider])], Policy(), me)],
            ["external_writer"],
        )
    )

    # The snapshot has to carry enough to re-create the permission.
    public_finding = classify([item(permissions=[link])], Policy(), me)[0]
    cases.append(("snapshot type for a public link", public_finding.permission_type, "anyone"))

    # Worst class sorts first, so the report cannot bury the indexed-public rows.
    mixed = classify(
        [item(id="a", permissions=[link]), item(id="b", permissions=[found])], Policy(), me
    )
    cases.append(
        ("worst class sorts first", [f.kind for f in mixed], ["public_indexed", "public_link"])
    )

    # Somebody else's public document is not this account's exposure. 21 of 26 real `anyoneCanFind`
    # hits were other people's files, so counting them would inflate the report with the unfixable.
    owned, foreign = owned_only(
        [item(id="mine", ownedByMe=True), item(id="theirs", ownedByMe=False)]
    )
    cases.append(
        (
            "foreign items split out",
            ([i["id"] for i in owned], [i["id"] for i in foreign]),
            (["mine"], ["theirs"]),
        )
    )
    cases.append(("absent ownedByMe counts as mine", owned_only([item(id="q")])[0][0]["id"], "q"))

    # Inheritance reconstructed from permission-id overlap, because files.list drops
    # permissionDetails. Same id on the parent folder means the child got it from there.
    shared_dir = item(id="dir", mimeType=FOLDER_MIME_TYPE, parents=[], permissions=[link])
    inside = item(id="kid", parents=["dir"], permissions=[link])
    tree_findings = classify([shared_dir, inside], Policy(), me)
    marked = mark_inherited(tree_findings)
    cases.append(("child of shared folder marked inherited", marked, 1))
    cases.append(
        (
            "the folder itself stays direct",
            [f.inherited for f in tree_findings if f.is_folder],
            [False],
        )
    )
    # A different principal on the child is its own decision, not an inheritance.
    other = item(id="kid2", parents=["dir"], permissions=[found])
    cases.append(
        (
            "unrelated permission not marked",
            mark_inherited(classify([shared_dir, other], Policy(), me)),
            0,
        )
    )

    failed = 0
    for label, actual, expected in cases:
        if actual == expected:
            print(f"ok   {label}")
        else:
            failed += 1
            print(f"FAIL {label}: got {actual!r}, want {expected!r}")
    print(f"gdrive_sharing selftest: {len(cases) - failed} passed, {failed} failed")
    return failed


__all__ = [
    "FOLDER_MIME_TYPE",
    "INHERIT_BLOCKED",
    "KIND_LABEL",
    "KIND_ORDER",
    "PERMISSION_FIELDS",
    "SHARING_FIELDS",
    "VISIBILITY_CLASSES",
    "Finding",
    "IncompleteSearchError",
    "Policy",
    "ChangeResult",
    "change_role",
    "DRIVE_SCOPES",
    "classify",
    "drop_covered_by_ancestor",
    "fetch_children",
    "fetch_drive_acl",
    "fetch_files",
    "fetch_principal",
    "fetch_owned",
    "fetch_shared_drive",
    "fetch_visibility",
    "list_shared_drives",
    "load_policy",
    "mark_inherited",
    "owned_only",
    "policy_candidates",
    "policy_from_dict",
    "render",
    "revoke",
    "selftest",
    "set_service_factory",
    "snapshot_path",
    "whoami_email",
    "write_snapshot",
]
