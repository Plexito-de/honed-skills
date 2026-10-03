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
One function, `_service()`, is the only place a credential is resolved, and it tries five things in
order so that neither a personal Google account nor a delegated service account needs a code change:

  1. a factory installed with `set_service_factory()`, which is how the tests substitute a fake;
  2. `$DRIVE_AUDIT_CREDENTIALS_COMMAND`, a command that prints a service-account JSON key, for a key
     kept in a secret manager rather than in a file (for example
     `doppler secrets get KEY --plain --project P --config C`). It is split with POSIX shell rules
     but runs without a shell, so `$VAR` and `~` are not expanded, its stdin is closed, and its
     output is never printed, not even in an error. When it is set, it wins over a key file;
  3. `$DRIVE_AUDIT_CREDENTIALS`, a service-account JSON key file;
  4. `$GOOGLE_APPLICATION_CREDENTIALS`, the same file under Google's own variable name. A key from
     2, 3 or 4 impersonates `$DRIVE_AUDIT_IMPERSONATE` when it is set (domain-wide delegation);
  5. Application Default Credentials, which is what `gcloud auth application-default login` leaves
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
import shlex
import subprocess
import sys
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
    moved: int = 0
    # reparent only: items it would not move, counted apart from `already` (a permission that is
    # already gone), because "skipped on purpose" and "nothing left to do" need different reading.
    skipped_filed: int = 0
    skipped_folders: int = 0
    # reparent only: items that were moved but whose permission is still there. The structure
    # changed and the access did not, which is the one state a reader must be shown by id.
    moved_not_revoked: list[str] = field(default_factory=list)
    # reparent only: one entry per item it touched, with the parents and EVERY permission the item
    # had before the move (approved grants included: a move drops all of them that were inherited),
    # and whether the move happened. It is what an undo needs, so the caller writes it even when
    # the run is interrupted.
    ledger: list[dict[str, Any]] = field(default_factory=list)
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


def _key_from_command(command: str) -> dict[str, Any]:
    """Run the credentials command and return the service-account key it prints.

    Every failure exits with a message that names the command and never its output, because the
    output is the private key.
    """
    # stdin is closed so a secret manager that wants to prompt fails fast instead of hanging, and
    # every SystemExit below is raised `from None`: a TimeoutExpired carries the captured output,
    # and a chained exception would print it in the traceback.
    try:
        done = subprocess.run(
            shlex.split(command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(
            f"DRIVE_AUDIT_CREDENTIALS_COMMAND could not run: {type(exc).__name__}"
        ) from None
    if done.returncode != 0:
        raise SystemExit(
            f"DRIVE_AUDIT_CREDENTIALS_COMMAND exited {done.returncode}; its output is not shown "
            "because it may hold the key."
        )
    try:
        info = json.loads(done.stdout)
    except json.JSONDecodeError:
        info = None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise SystemExit(
            "DRIVE_AUDIT_CREDENTIALS_COMMAND did not print a service-account JSON key."
        )
    return info


@cache
def _service() -> Any:
    """The Drive client. Cached, because rebuilding it per call re-does discovery every time."""
    if _SERVICE_FACTORY is not None:
        return _SERVICE_FACTORY()

    from google.oauth2 import service_account  # noqa: PLC0415
    from googleapiclient.discovery import build  # noqa: PLC0415

    command = os.environ.get("DRIVE_AUDIT_CREDENTIALS_COMMAND")
    key_path = os.environ.get("DRIVE_AUDIT_CREDENTIALS") or os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )
    subject = os.environ.get("DRIVE_AUDIT_IMPERSONATE")

    if command:
        try:
            creds = service_account.Credentials.from_service_account_info(
                _key_from_command(command), scopes=list(DRIVE_SCOPES)
            )
        except ValueError:
            # The library's message can quote the key's fields, so it is replaced, not chained.
            raise SystemExit(
                "DRIVE_AUDIT_CREDENTIALS_COMMAND printed a service-account key the Google library "
                "could not load (a field is missing or malformed)."
            ) from None
        if subject:
            creds = creds.with_subject(subject)
    elif key_path:
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


def all_permissions(service: Any, file_id: str) -> list[dict[str, Any]]:
    """Every permission on one item, across pages. The list embedded in files.get is not used for
    a safety decision, because it is not documented as complete."""
    out: list[dict[str, Any]] = []
    token = None
    while True:
        page = (
            service.permissions()
            .list(
                fileId=file_id,
                fields=f"nextPageToken,{PERMISSION_FIELDS}",
                supportsAllDrives=True,
                pageToken=token,
            )
            .execute(num_retries=3)
        )
        out.extend(page.get("permissions") or [])
        token = page.get("nextPageToken")
        if not token:
            return out


def describe_file(file_id: str) -> dict[str, Any]:
    """Canonical id, name, type, drive, trash state, owners and ALL permissions of one item, for a
    guard run BEFORE a mutation: can items be moved into it without anybody gaining access?"""
    service = _service()
    info = (
        service.files()
        .get(
            fileId=file_id,
            fields="id,name,mimeType,driveId,trashed,owners(emailAddress)",
            supportsAllDrives=True,
        )
        .execute(num_retries=3)
    )
    info["permissions"] = all_permissions(service, info.get("id") or file_id)
    return info


def destination_problem(dest: dict[str, Any], owner: str) -> str | None:
    """Why `dest` is not a safe folder to re-parent into, or None when it is.

    A moved item INHERITS every permission on its new parent, and the permissions Drive lists on a
    folder include the ones it inherits from its own ancestors. So the only safe destination is a
    My Drive folder this account owns that nobody else can see. Anything else turns a verb meant
    to remove access into one that grants it.
    """
    name = dest.get("name") or dest.get("id") or "?"
    if dest.get("mimeType") != FOLDER_MIME_TYPE:
        return f"{name} is not a folder ({dest.get('mimeType')})"
    if dest.get("trashed"):
        return f"{name} is in the trash"
    if dest.get("driveId"):
        return (
            f"{name} is in a shared drive, where every drive member gets access to what moves in. "
            "Use a private folder in My Drive"
        )
    if not any(o.get("emailAddress") == owner for o in dest.get("owners") or []):
        return f"{name} is not owned by {owner}. Use a folder you own"
    permissions = dest.get("permissions") or []
    # An owned folder always lists its owner, so an empty or ownerless list means Drive did not
    # tell us who can see it, and "nobody" would be a guess in the direction that costs.
    if not any(p.get("role") == "owner" and p.get("emailAddress") == owner for p in permissions):
        return f"{name}: Drive did not return who can see it, so it cannot be proven private"
    others = [
        p.get("emailAddress") or p.get("domain") or p.get("type") or "?"
        for p in permissions
        if not (p.get("role") == "owner" and p.get("emailAddress") == owner)
    ]
    if others:
        return (
            f"{name} is shared ({', '.join(sorted(set(others)))}), so every moved item would gain "
            "that access. Use a private folder nobody else can see"
        )
    return None


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


def _rate_limited(exc: BaseException) -> bool:
    """Drive's own reasons (rateLimitExceeded, userRateLimitExceeded) or a bare 429."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status == 429 or "ratelimitexceeded" in str(exc).lower()


def reparent_and_revoke(
    findings: Iterable[Finding],
    *,
    destination: str,
    dry_run: bool = True,
    orphans_only: bool = True,
    include_folders: bool = False,
    pause_every: int = 50,
    pause_seconds: float = 1.0,
    check_every: int = 10,
    report: Callable[[str], None] = print,
    check_destination: Callable[[], str | None] | None = None,
    on_ledger_change: Callable[[ChangeResult], None] | None = None,
    result: ChangeResult | None = None,
    service: Any = None,
    http_error: type[BaseException] | None = None,
) -> ChangeResult:
    """Free a grant that `revoke` cannot remove, by giving its item a parent this account owns.

    WHY THIS EXISTS. A Drive permission can outlive the folder it was granted on. The item then
    has no parent, and `permissions.delete` returns 403 `cannotDeletePermission` at every level.
    Re-parenting breaks the inheritance chain, which is what should make the permission
    removable.

    EACH GRANT IS TRIED WITH A PLAIN DELETE FIRST. Only a delete Drive refuses with
    `cannotDeletePermission` leads to a move, so an item whose grant can simply be removed is
    never reorganised. Then: move, VERIFY the move, delete again, and READ THE PERMISSIONS BACK.
    Only a grant that is absent from that read-back counts as freed, whatever the delete answered.

    A MOVE CAN REMOVE EVERY INHERITED GRANT, NOT ONLY THE ONE IN SCOPE: approved ones too. So before
    each move the item's parents and full permission list go into `result.ledger`, which is what
    an undo needs. `on_ledger_change` is called after every ledger change, so the caller can write
    it ahead of the next step; a move is recorded as "unknown" while its request is in flight.

    THE DESTINATION MUST BE CHECKED BY THE CALLER FIRST (`destination_problem()`), with
    `destination` the CANONICAL id from that check. A moved item inherits every grant on its new
    parent. `check_destination` re-runs the check before the first move and every `check_every`
    moves after it, so a destination that becomes shared stops the run.

    `orphans_only` (default True) skips an item that has a parent: it is filed on purpose. An item
    in a shared drive is never moved. `include_folders` (default False) is needed to move a
    folder, which takes everything inside it along. Every Drive call is paced (`pause_every`), and
    a rate limit that outlasts the client's retries stops the run.

    A DRY RUN IMPORTS NOTHING FROM GOOGLE, matching `revoke`, so it runs on a machine with no venv.
    It applies the skip rules from the listing, where an item at My Drive root also reads as
    parentless, and it cannot know which deletes Drive would refuse, so its count is an upper
    bound. `service` and `http_error` let the self-test drive the apply path with a fake.
    """
    import time  # noqa: PLC0415

    if not dry_run:
        service = service if service is not None else _service()
        http_error = http_error if http_error is not None else _http_error_class()

    def status_of(exc: BaseException) -> int | None:
        return getattr(getattr(exc, "resp", None), "status", None)

    def saved() -> None:
        if on_ledger_change is not None:
            on_ledger_change(result)

    result = result if result is not None else ChangeResult()
    moved_files: set[str] = set()
    skipped_files: set[str] = set()  # skipped on purpose: their grants stay, counted once per item
    failed_files: set[str] = set()   # a step failed: every further grant on them is reported
    attempts = 0
    processed = 0

    for finding in findings:
        where = f"{finding.file_id} ({finding.name})"
        if finding.file_id in skipped_files:
            continue
        if finding.file_id in failed_files:
            result.errors.append(f"{where}: grant {finding.permission_id} not attempted, an "
                                 "earlier step on this item failed")
            continue
        if finding.is_folder and not include_folders:
            result.skipped_folders += 1
            skipped_files.add(finding.file_id)
            continue
        if dry_run:
            if orphans_only and finding.parents:
                result.skipped_filed += 1
                skipped_files.add(finding.file_id)
                continue
            report(
                f"  [would try, then re-parent + revoke] {finding.principal:24s} "
                f"{finding.role:8s} {finding.name}"
            )
            result.changed += 1
            continue

        processed += 1
        if processed > 1 and processed % pause_every == 1:
            report(
                f"  … {processed - 1} processed, {result.moved} moved, {result.changed} freed, "
                f"{len(result.moved_not_revoked)} moved but not revoked, "
                f"{len(result.errors)} error(s)"
            )
            time.sleep(pause_seconds)

        if finding.file_id not in moved_files:
            # 1. The plain delete. Most grants go here, and nothing moves.
            try:
                service.permissions().delete(
                    fileId=finding.file_id, permissionId=finding.permission_id,
                    supportsAllDrives=True,
                ).execute(num_retries=3)
                result.changed += 1
                continue
            except http_error as exc:
                if status_of(exc) == 404 and "file not found" not in str(exc).lower():
                    result.already += 1
                    continue
                if _rate_limited(exc):
                    result.errors.append(f"run stopped at {where}: Drive's rate limit "
                                         "outlasted the retries")
                    return result
                if _http_reason(exc) != "cannotDeletePermission":
                    if reason := _http_reason(exc):
                        result.blocked_by_ancestor.append(f"{reason}: {where}")
                    else:
                        result.errors.append(f"{where}: {exc}")
                    continue
            # 2. Refused as orphaned. Read the item as it really is, not as the listing said.
            try:
                current = (
                    service.files()
                    .get(fileId=finding.file_id, fields="id,parents,driveId",
                         supportsAllDrives=True)
                    .execute(num_retries=3)
                )
                before = all_permissions(service, finding.file_id)
            except http_error as exc:
                result.errors.append(f"{where}: could not read the item: {exc}")
                failed_files.add(finding.file_id)
                if _rate_limited(exc):
                    result.errors.append("run stopped: Drive's rate limit outlasted the retries")
                    return result
                continue
            parents = current.get("parents") or []
            if current.get("driveId") or (orphans_only and parents):
                result.skipped_filed += 1
                skipped_files.add(finding.file_id)
                continue
            # 3. The destination, before the first move and every check_every moves after it.
            if attempts % check_every == 0 and check_destination is not None:
                if problem := check_destination():
                    result.errors.append(
                        f"run stopped before {where}: destination {problem}. Items moved before "
                        "this point are in the ledger; move them out of the destination first"
                    )
                    return result
            attempts += 1
            entry: dict[str, Any] = {
                "file_id": finding.file_id, "name": finding.name, "parents_before": parents,
                "permissions_before": before,
                "moved": "unknown: the move request was sent and has not returned",
            }
            result.ledger.append(entry)
            saved()
            # 4. Move, once per file. Any failure leaves the move "unknown", never "no".
            try:
                service.files().update(
                    fileId=finding.file_id, addParents=destination,
                    removeParents=",".join(parents) if parents else None,
                    fields="id,parents", supportsAllDrives=True,
                ).execute(num_retries=3)
            except http_error as exc:
                entry["moved"] = "unknown: the request failed, a retry may still have applied it"
                saved()
                result.errors.append(f"{where}: the move request failed, check the item: {exc}")
                failed_files.add(finding.file_id)
                if _rate_limited(exc):
                    result.errors.append("run stopped: Drive's rate limit outlasted the retries")
                    return result
                continue
            # 5. Verify. An unproven move is never followed by a revoke.
            try:
                after = (
                    service.files()
                    .get(fileId=finding.file_id, fields="id,parents", supportsAllDrives=True)
                    .execute(num_retries=3)
                )
            except Exception as exc:  # noqa: BLE001 - any failure here leaves the move unproven
                entry["moved"] = "unknown: the move could not be verified"
                saved()
                result.moved_not_revoked.append(f"{where}: move not verified ({exc})")
                failed_files.add(finding.file_id)
                if _rate_limited(exc):
                    result.errors.append("run stopped: Drive's rate limit outlasted the retries")
                    return result
                continue
            if destination not in (after.get("parents") or []):
                entry["moved"] = f"no: parents are {after.get('parents')}"
                saved()
                result.errors.append(
                    f"{where}: move did not take, parents are {after.get('parents')}, "
                    "so the permission was left alone"
                )
                failed_files.add(finding.file_id)
                continue
            entry["moved"] = True
            saved()
            moved_files.add(finding.file_id)
            result.moved += 1

        # 6. The delete after the move, then a read-back: only an absent grant counts as freed.
        try:
            service.permissions().delete(
                fileId=finding.file_id, permissionId=finding.permission_id,
                supportsAllDrives=True,
            ).execute(num_retries=3)
        except Exception as exc:  # noqa: BLE001 - the item has moved; every failure must be shown
            if not (http_error is not None and isinstance(exc, http_error) and status_of(exc) == 404):
                result.moved_not_revoked.append(f"{where}: moved, but the revoke failed: {exc}")
                if _rate_limited(exc):
                    result.errors.append("run stopped: Drive's rate limit outlasted the retries")
                    return result
                continue
        try:
            still = any(
                p.get("id") == finding.permission_id
                for p in all_permissions(service, finding.file_id)
            )
        except Exception as exc:  # noqa: BLE001
            result.moved_not_revoked.append(f"{where}: moved, revoke not confirmed ({exc})")
            if _rate_limited(exc):
                result.errors.append("run stopped: Drive's rate limit outlasted the retries")
                return result
            continue
        if still:
            result.moved_not_revoked.append(f"{where}: moved, but the grant is still listed")
        else:
            result.changed += 1

    return result


def write_ledger(result: ChangeResult, path: Path, destination: str) -> Path:
    """Write what a reparent run did, item by item, so it can be undone.

    Rewritten in full after every change through a temporary file and an atomic rename, so the
    file on disk is always a complete earlier or later state, never a half-written one.
    """
    payload = {
        "written": datetime.now().isoformat(timespec="seconds"),
        "destination": destination,
        "note": "One entry per item this run read before moving. To undo a move: "
        f"files.update(fileId, addParents=<parents_before>, removeParents={destination}). An "
        "item with no parents_before was orphaned and has nowhere to return to. permissions_before "
        "lists EVERY grant the item had, approved ones included, with permissionDetails saying "
        "which were inherited; a move can drop all of those, so re-create any you still want with "
        "permissions.create (sendNotificationEmail=false). A `moved` value starting with "
        "'unknown' means: check that item by hand.",
        "moved": result.moved,
        "moved_not_revoked": result.moved_not_revoked,
        "items": result.ledger,
    }
    _private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    # 0600 from the first byte: the file lists who can see what.
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
                   encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


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


def _private_dir(directory: Path) -> None:
    """Create `directory` as 0700, and tighten it if it already exists and is ours: mkdir's mode
    applies only to a directory it creates, and an older install left this one 0755."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid == os.getuid():
        os.chmod(directory, 0o700)


def write_snapshot(findings: Iterable[Finding], path: Path, *, moved_to: str | None = None) -> Path:
    """Record every permission about to be removed, so all of it can be re-added.

    This is what makes a several-thousand-file revoke a decision rather than a gamble: the ids,
    permission ids, the principals and the roles are all here, and restoring one row is a single
    `permissions.create`. Each row also carries the item's parents AS LISTED, before anything ran.
    For a re-parent (`moved_to`), the run ledger written by `write_ledger` is the record of what
    each item really had and whether it moved; this file only says what was in scope.
    """
    rows = [
        {
            "file_id": f.file_id,
            "name": f.name,
            "parents": list(f.parents),
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
    if moved_to:
        payload["moved_to"] = moved_to
        payload["move_note"] = (
            f"These permissions were IN SCOPE for a re-parent into {moved_to}, written before the "
            "run. Which items moved, the parents they really had and every grant the move removed "
            "are in the run ledger written next to this file (*-ledger.json)."
        )
    _private_dir(path.parent)
    # O_EXCL so a collision RAISES instead of overwriting the only copy of an ACL (the caller then
    # aborts rather than deleting permissions it can no longer restore), and 0600 because the file
    # lists who can see what.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w",
                   encoding="utf-8") as fh:
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

    # reparent_and_revoke: the dry run must count without importing anything from Google, which is
    # what keeps it runnable on a machine with no venv. A dry run that needed the service would
    # fail here with ImportError rather than returning a count.
    rp_findings = classify([item(id="orph", parents=[], permissions=[found])], Policy(), me)
    cases.append(
        (
            "reparent dry run counts every finding",
            reparent_and_revoke(rp_findings, destination="dest", dry_run=True, report=lambda _: None).changed,
            len(rp_findings),
        )
    )
    # And it must not silently report success on an empty scope, because "0 freed" and "nothing was
    # in scope" are different answers and only one of them means the work is done.
    cases.append(
        (
            "reparent dry run on nothing changes nothing",
            reparent_and_revoke([], destination="dest", dry_run=True, report=lambda _: None).changed,
            0,
        )
    )
    # The dry run applies the same skip rules as apply, per ITEM, or it promises what apply will not.
    other = {"id": "p2", "type": "user", "role": "writer", "emailAddress": "x@example.com"}
    filed = classify([item(id="filed", parents=["p1"], permissions=[found, other])], Policy(), me)
    a_folder = classify(
        [item(id="dir", parents=[], mimeType=FOLDER_MIME_TYPE, permissions=[found])], Policy(), me
    )
    dry = reparent_and_revoke(
        rp_findings + filed + a_folder, destination="dest", dry_run=True, report=lambda _: None
    )
    cases.append(
        (
            "reparent dry run skips filed items and folders, counted once per item",
            (dry.changed, dry.skipped_filed, dry.skipped_folders),
            (len(rp_findings), 1, 1),
        )
    )

    # destination_problem: the only safe destination is a private My Drive folder you own.
    def dest(**kw: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "id": "d",
            "name": "Recovered",
            "mimeType": FOLDER_MIME_TYPE,
            "owners": [{"emailAddress": me}],
            "permissions": [{"type": "user", "role": "owner", "emailAddress": me}],
        }
        base.update(kw)
        return base

    shared_perms = [
        {"type": "user", "role": "owner", "emailAddress": me},
        {"type": "domain", "role": "reader", "domain": "example.com"},
    ]
    cases.append(("private folder you own is a safe destination", destination_problem(dest(), me), None))
    for label, bad in (
        ("a shared destination is refused", dest(permissions=shared_perms)),
        ("an anyone-with-link destination is refused", dest(permissions=[*dest()["permissions"], link])),
        ("a shared-drive destination is refused", dest(driveId="0Axyz")),
        ("a trashed destination is refused", dest(trashed=True)),
        ("a destination owned by someone else is refused", dest(owners=[{"emailAddress": "x@example.com"}])),
        ("a file as destination is refused", dest(mimeType="image/png")),
        ("a destination whose permissions Drive did not return is refused", dest(permissions=[])),
    ):
        cases.append((label, destination_problem(bad, me) is not None, True))

    # The apply path, against a fake Drive that models inheritance the way Drive behaves: an
    # inherited grant on a parentless item refuses deletion (cannotDeletePermission), one under a
    # live parent refuses too (cannotModifyInheritedPermission), and a move drops every inherited
    # grant, so a later delete of one answers 404.
    class FakeHttpError(Exception):
        def __init__(self, status: int, reason: str = "") -> None:
            super().__init__(f"HTTP {status} {reason}".strip())
            self.resp = type("Resp", (), {"status": status})()

    class Call:
        def __init__(self, fn: Callable[[], Any]) -> None:
            self.fn = fn

        def execute(self, num_retries: int = 0) -> Any:
            return self.fn()

    class FakeDrive:
        def __init__(self, items: dict[str, dict[str, Any]], move_ok: bool = True,
                     fail_after_move: tuple[str, ...] = (), raise_on_update: bool = False,
                     sticky: tuple[str, ...] = (), rate_limited: bool = False,
                     rate_limited_on_update: bool = False) -> None:
            self.items, self.move_ok = items, move_ok
            self.fail_after_move, self.raise_on_update = fail_after_move, raise_on_update
            self.sticky, self.rate_limited = sticky, rate_limited
            self.rate_limited_on_update = rate_limited_on_update
            self.updates: list[str] = []

        def files(self) -> FakeDrive:
            return self

        def permissions(self) -> FakeDrive:
            return self

        def get(self, fileId: str, **_: Any) -> Call:
            it = self.items[fileId]
            return Call(lambda: {"id": fileId, "parents": list(it["parents"])})

        def list(self, fileId: str, **_: Any) -> Call:
            perms = self.items[fileId]["perms"]
            return Call(lambda: {"permissions": [
                {"id": p, "permissionDetails": [{"inherited": i}]} for p, i in perms.items()
            ]})

        def update(self, fileId: str, addParents: str, **_: Any) -> Call:
            def run() -> dict[str, Any]:
                if self.raise_on_update:
                    raise RuntimeError("connection reset")
                if self.rate_limited_on_update:
                    raise FakeHttpError(403, "userRateLimitExceeded")
                self.updates.append(fileId)
                it = self.items[fileId]
                if self.move_ok:
                    it["parents"] = [addParents]
                    it["perms"] = {p: i for p, i in it["perms"].items()
                                   if not i or p in self.sticky}
                return {}
            return Call(run)

        def delete(self, fileId: str, permissionId: str, **_: Any) -> Call:
            def run() -> dict[str, Any]:
                it = self.items[fileId]
                if self.rate_limited:
                    raise FakeHttpError(429)
                if permissionId in self.sticky and "dest" in it["parents"]:
                    return {}  # answers success, yet the grant stays: only a read-back sees it
                if permissionId in self.fail_after_move and "dest" in it["parents"]:
                    raise FakeHttpError(500)
                if permissionId not in it["perms"]:
                    raise FakeHttpError(404)
                if it["perms"][permissionId]:
                    raise FakeHttpError(403, "cannotModifyInheritedPermission" if it["parents"]
                                        else "cannotDeletePermission")
                del it["perms"][permissionId]
                return {}
            return Call(run)

    def orphan(**perms: bool) -> dict[str, dict[str, Any]]:
        return {"orph": {"parents": [], "perms": dict(perms)}}

    two_grants = classify([item(id="orph", parents=[], permissions=[found, other])], Policy(), me)

    def apply(drive: FakeDrive, findings: list[Finding], **kw: Any) -> ChangeResult:
        return reparent_and_revoke(
            findings, destination="dest", dry_run=False, report=lambda _: None,
            service=drive, http_error=FakeHttpError, **kw,
        )

    drive = FakeDrive(orphan(anyone=False))
    done = apply(drive, rp_findings)
    cases.append(
        ("a grant a plain delete removes is never moved", (done.changed, drive.updates), (1, []))
    )
    drive = FakeDrive(orphan())
    done = apply(drive, rp_findings)
    cases.append(
        (
            "a grant that is already gone counts as already, with no move and no error",
            (done.already, done.errors, drive.updates),
            (1, [], []),
        )
    )
    drive = FakeDrive(orphan(anyone=True, p2=True, appr=True))
    writes: list[int] = []
    moved_run = done = apply(drive, two_grants, on_ledger_change=lambda r: writes.append(1))
    cases.append(
        (
            "two refused grants on one orphan: one move, both freed, every grant in the ledger",
            (done.moved, done.changed, drive.updates, len(done.ledger[0]["permissions_before"])),
            (1, 2, ["orph"], 3),
        )
    )
    cases.append(("the ledger is written ahead of the move and after it", len(writes) >= 2, True))
    drive = FakeDrive(orphan(anyone=True), sticky=("anyone",))
    done = apply(drive, rp_findings)
    cases.append(
        (
            "a grant still listed after the revoke is moved-not-revoked, never freed",
            (done.changed, len(done.moved_not_revoked)),
            (0, 1),
        )
    )
    drive = FakeDrive(orphan(anyone=True, p2=True), rate_limited=True)
    done = apply(drive, two_grants)
    cases.append(
        ("a rate limit on a plain delete stops the run", (done.changed, len(done.errors)), (0, 1))
    )
    two_orphans = {f"o{n}": {"parents": [], "perms": {"anyone": True}} for n in range(2)}
    queued = [
        f for i in two_orphans
        for f in classify([item(id=i, parents=[], permissions=[found])], Policy(), me)
    ]
    drive = FakeDrive(two_orphans, rate_limited_on_update=True)
    done = apply(drive, queued)
    cases.append(
        (
            "a rate limit on a move stops the run, and the move stays unknown in the ledger",
            (len(done.ledger), str(done.ledger[0]["moved"]).startswith("unknown")),
            (1, True),
        )
    )
    # The scan path reads permissions.list with this constant, so it must stay a wrapped field list;
    # a bare one returns no `permissions` key and every shared-drive ACL reads as empty.
    cases.append(
        ("PERMISSION_FIELDS stays a wrapped permissions(...) list", PERMISSION_FIELDS.startswith("permissions("), True)
    )
    drive = FakeDrive({"filed": {"parents": ["p1"], "perms": {"anyone": True, "p2": True}}})
    done = apply(drive, filed)
    cases.append(
        (
            "a grant inherited from a LIVE folder is reported, never moved",
            (len(done.blocked_by_ancestor), drive.updates),
            (2, []),
        )
    )
    drive = FakeDrive(orphan(anyone=True, p2=True), move_ok=False)
    done = apply(drive, two_grants)
    cases.append(
        (
            "a move that does not take revokes nothing, and the next grant is reported, not tried",
            (done.changed, drive.updates, len(done.errors)),
            (0, ["orph"], 2),
        )
    )
    drive = FakeDrive(orphan(anyone=True), fail_after_move=("anyone",))
    done = apply(drive, rp_findings)
    cases.append(
        (
            "a failed revoke after a move is reported as moved-not-revoked",
            (done.moved, done.changed, len(done.moved_not_revoked)),
            (1, 0, 1),
        )
    )
    drive = FakeDrive(orphan(anyone=True))
    done = apply(drive, rp_findings, check_destination=lambda: "became shared")
    cases.append(
        (
            "a destination that fails its check stops the run before the first move",
            (drive.updates, len(done.errors), len(done.ledger)),
            ([], 1, 0),
        )
    )
    checks: list[int] = []
    many = {f"o{n}": {"parents": [], "perms": {"anyone": True}} for n in range(3)}
    many["f1"] = {"parents": ["p1"], "perms": {"anyone": True}}
    mixed = [
        f for i in ("o0", "f1", "o1", "o2")
        for f in classify([item(id=i, parents=many[i]["parents"], permissions=[found])], Policy(), me)
    ]
    apply(FakeDrive(many), mixed, orphans_only=False, check_every=2, pause_seconds=0,
          check_destination=lambda: checks.append(1) and None)
    cases.append(("the destination is re-checked per moves, whatever is skipped between", len(checks), 2))
    interrupted = ChangeResult()
    try:
        apply(FakeDrive(orphan(anyone=True), raise_on_update=True), rp_findings, result=interrupted)
    except RuntimeError:
        pass
    cases.append(
        (
            "an interrupted move is in the caller's ledger as unknown, never as not moved",
            (len(interrupted.ledger), str(interrupted.ledger[0]["moved"]).startswith("unknown")),
            (1, True),
        )
    )

    # What an undo needs reaches disk.
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        snap = write_snapshot(filed, Path(tmp) / "s.json", moved_to="dest")
        data = json.loads(snap.read_text(encoding="utf-8"))
        led = write_ledger(moved_run, Path(tmp) / "s-ledger.json", "dest")
        write_ledger(moved_run, led, "dest")  # rewritten in place, as after every change
        ledger = json.loads(led.read_text(encoding="utf-8"))
        loose = Path(tmp) / "older"
        loose.mkdir(mode=0o755)
        write_ledger(moved_run, loose / "l.json", "dest")
        modes = (snap.stat().st_mode & 0o777, led.stat().st_mode & 0o777,
                 loose.stat().st_mode & 0o777)
    cases.append(
        ("snapshot, ledger and an older looser directory end owner-only", modes, (0o600, 0o600, 0o700))
    )
    cases.append(
        (
            "the snapshot names the destination and the ledger records parents and grants",
            (data["moved_to"], ledger["destination"], ledger["items"][0]["parents_before"],
             len(ledger["items"][0]["permissions_before"])),
            ("dest", "dest", [], 3),
        )
    )

    # The credentials command: it must return the key it printed, and every failure must exit
    # WITHOUT the output in the message, because that output is a private key.
    def exit_text(fn: Callable[[], Any]) -> str | None:
        try:
            fn()
        except SystemExit as exc:
            return str(exc)
        return None

    py = shlex.quote(sys.executable)
    fake = '{"type": "service_account", "private_key": "SECRET-MARKER"}'
    prints_key = shlex.quote("print(" + repr(fake) + ")")
    cases.append(
        (
            "credentials command returns its key",
            _key_from_command(f"{py} -c {prints_key}").get("private_key"),
            "SECRET-MARKER",
        )
    )
    exits_3 = shlex.quote("import sys; print('SECRET-MARKER'); sys.exit(3)")
    failing = f"{py} -c {exits_3}"
    cases.append(
        (
            "a failing credentials command exits without its output",
            (lambda t: t is not None and "SECRET-MARKER" not in t and "exited 3" in t)(
                exit_text(lambda: _key_from_command(failing))
            ),
            True,
        )
    )
    reads_stdin = shlex.quote("import sys; sys.stdin.read(); print(" + repr(fake) + ")")
    cases.append(
        (
            "a command that reads stdin gets end-of-file, not a hang",
            _key_from_command(f"{py} -c {reads_stdin}").get("private_key"),
            "SECRET-MARKER",
        )
    )

    def suppressed(fn: Callable[[], Any]) -> bool:
        try:
            fn()
        except SystemExit as exc:
            return exc.__suppress_context__ and exc.__cause__ is None
        return False

    cases.append(
        (
            "a missing command exits cleanly, with no chained exception",
            suppressed(lambda: _key_from_command("/nonexistent/drive-audit-no-such-command")),
            True,
        )
    )
    prints_marker = shlex.quote("print('SECRET-MARKER')")
    not_a_key = f"{py} -c {prints_marker}"
    cases.append(
        (
            "output that is not a service-account key is refused, unprinted",
            (lambda t: t is not None and "SECRET-MARKER" not in t)(
                exit_text(lambda: _key_from_command(not_a_key))
            ),
            True,
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
    "all_permissions",
    "describe_file",
    "destination_problem",
    "reparent_and_revoke",
    "revoke",
    "selftest",
    "set_service_factory",
    "snapshot_path",
    "whoami_email",
    "write_ledger",
    "write_snapshot",
]
