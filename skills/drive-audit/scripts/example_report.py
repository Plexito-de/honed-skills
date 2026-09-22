#!/usr/bin/env python3
"""Print a drive-audit report built from invented findings, with no Drive and no credentials.

WHY THIS SHIPS
  The output of this skill is a text report, so the honest illustration is the report itself rather
  than a screenshot of one. Everything below is invented: example.com addresses, made-up file names,
  file ids that are obviously placeholders. Run it and you can see exactly what the tool prints, and
  confirm that nothing real went into the example, which a picture could never let you do.

  It is also the cheapest possible check that the renderer still works after a change to the
  classifier, and it needs no network, no key and no Google libraries.

USAGE
  python3 example_report.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# See drive-audit for why: a .pyc inside the skill folder is a HIGH scanner finding on every
# machine that runs this, so nothing here may produce one.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gdrive_sharing as gs  # noqa: E402

OWNER = "you@example.com"

# One item per finding class, so the report shows the full severity ladder in the order it uses.
ITEMS: list[dict] = [
    {
        "id": "0B_EXAMPLE_INDEXED",
        "name": "Old proposal.pdf",
        "mimeType": "application/pdf",
        "parents": ["0B_EXAMPLE_ARCHIVE"],
        "ownedByMe": True,
        "webViewLink": "https://drive.example.com/file/d/0B_EXAMPLE_INDEXED/view",
        "permissions": [
            {"id": "owner", "type": "user", "role": "owner", "emailAddress": OWNER},
            {"id": "anyone", "type": "anyone", "role": "reader", "allowFileDiscovery": True},
        ],
    },
    {
        "id": "0B_EXAMPLE_SCREENSHOT",
        "name": "capture-0001.png",
        "mimeType": "image/png",
        "parents": ["0B_EXAMPLE_CAPTURES"],
        "ownedByMe": True,
        "webViewLink": "https://drive.example.com/file/d/0B_EXAMPLE_SCREENSHOT/view",
        "permissions": [
            {"id": "owner", "type": "user", "role": "owner", "emailAddress": OWNER},
            {
                "id": "anyoneWithLink",
                "type": "anyone",
                "role": "reader",
                "allowFileDiscovery": False,
            },
        ],
    },
    {
        "id": "0B_EXAMPLE_SHARED_DIR",
        "name": "Handover",
        "mimeType": gs.FOLDER_MIME_TYPE,
        "parents": [],
        "ownedByMe": True,
        "webViewLink": "https://drive.example.com/drive/folders/0B_EXAMPLE_SHARED_DIR",
        "permissions": [
            {"id": "owner", "type": "user", "role": "owner", "emailAddress": OWNER},
            {
                "id": "p-outsider",
                "type": "user",
                "role": "writer",
                "emailAddress": "contractor@elsewhere.example",
            },
        ],
    },
    {
        # A child of that folder, carrying the SAME permission id: the report collapses it as
        # inherited instead of repeating the finding once per descendant.
        "id": "0B_EXAMPLE_SHARED_CHILD",
        "name": "Notes.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "parents": ["0B_EXAMPLE_SHARED_DIR"],
        "ownedByMe": True,
        "webViewLink": "https://drive.example.com/file/d/0B_EXAMPLE_SHARED_CHILD/view",
        "permissions": [
            {"id": "owner", "type": "user", "role": "owner", "emailAddress": OWNER},
            {
                "id": "p-outsider",
                "type": "user",
                "role": "writer",
                "emailAddress": "contractor@elsewhere.example",
            },
        ],
    },
    {
        "id": "0B_EXAMPLE_ADVISOR",
        "name": "Filing 2024.xlsx",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": ["0B_EXAMPLE_ARCHIVE"],
        "ownedByMe": True,
        "webViewLink": "https://drive.example.com/file/d/0B_EXAMPLE_ADVISOR/view",
        "permissions": [
            {"id": "owner", "type": "user", "role": "owner", "emailAddress": OWNER},
            {
                "id": "p-advisor",
                "type": "user",
                "role": "reader",
                "emailAddress": "advisor@example.com",
            },
        ],
    },
    {
        "id": "0B_EXAMPLE_COLLEAGUE",
        "name": "Team plan",
        "mimeType": "application/vnd.google-apps.document",
        "parents": [],
        "ownedByMe": True,
        "webViewLink": "https://drive.example.com/file/d/0B_EXAMPLE_COLLEAGUE/view",
        "permissions": [
            {"id": "owner", "type": "user", "role": "owner", "emailAddress": OWNER},
            {
                "id": "p-colleague",
                "type": "user",
                "role": "writer",
                "emailAddress": "colleague@example.com",
            },
        ],
    },
    {
        "id": "0B_EXAMPLE_DOMAIN",
        "name": "Legacy sync folder",
        "mimeType": gs.FOLDER_MIME_TYPE,
        "parents": [],
        "ownedByMe": True,
        "webViewLink": "https://drive.example.com/drive/folders/0B_EXAMPLE_DOMAIN",
        "permissions": [
            {"id": "owner", "type": "user", "role": "owner", "emailAddress": OWNER},
            {
                "id": "p-domain",
                "type": "domain",
                "role": "reader",
                "domain": "example.com",
                "allowFileDiscovery": False,
            },
        ],
    },
    {
        # Somebody else's public file: a visibility query returns it, and it is not ours to fix.
        "id": "0B_EXAMPLE_FOREIGN",
        "name": "Somebody else's public doc",
        "mimeType": "application/vnd.google-apps.document",
        "parents": [],
        "ownedByMe": False,
        "webViewLink": "https://drive.example.com/file/d/0B_EXAMPLE_FOREIGN/view",
        "permissions": [],
    },
]

POLICY = gs.policy_from_dict(
    {
        "approved_principal": [
            {
                "email": "advisor@example.com",
                "reason": "outside accountant, filing folder",
                "since": "2026-05-26",
                "roles": ["reader"],
            }
        ]
    },
    "example policy (invented, not read from disk)",
)


def main() -> int:
    owned, foreign = gs.owned_only(ITEMS)
    findings = gs.classify(owned, POLICY, OWNER)
    collapsed = gs.mark_inherited(findings)

    print("This report is built from invented findings. No Drive was contacted.\n")
    print(f"note: {len(foreign)} item(s) are owned by somebody else and were not classified.")
    print(f"note: {collapsed} finding(s) proved inherited from a folder in this result set.\n")
    for line in gs.render(
        findings,
        scanned=len(ITEMS),
        owner_email=OWNER,
        policy=POLICY,
        show_approved=True,
    ):
        print(line)

    print("\n--- what a revoke would do, as a dry run ---")
    public = [f for f in findings if f.kind.startswith("public") and not f.approved]
    result = gs.revoke(public, dry_run=True)
    print(f"\n{result.changed} permission(s) would be removed. Nothing was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
