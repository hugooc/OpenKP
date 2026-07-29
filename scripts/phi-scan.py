#!/usr/bin/env python3
"""Scan git-tracked files for PHI and real Kaiser identifiers.

OpenKP is a public repo built by reading one person's medical record, so the
failure mode is specific: a real value gets pasted into a doc as an example
and nobody notices. That has happened twice.

  - `WISHLIST.md` named a real treating provider in a use-case sentence.
    Combined with the author's name in LICENSE and the ADRs, that discloses
    a care relationship.
  - `messages.md` pasted a real ~85-char document ID straight out of a HAR,
    with a line above it naming the file that was uploaded.

Both read as harmless illustrative detail. Neither is.

Usage:

    python3 scripts/phi-scan.py            # scan the working tree
    python3 scripts/phi-scan.py --history  # also scan every commit

Exits non-zero if anything is found. Not wired into CI on purpose: a linter
that fails a release on a false positive gets disabled, and this is meant to
be read by a human. Run it before publishing.

What it deliberately does NOT flag:

  - "Campos" — the author's own name, intentional attribution
  - 555-prefixed phone numbers and Example St addresses — synthetic fixtures
  - 888-218-6245 — Kaiser's public pharmacy line, a business number
  - Bare years, and prose mentions of "MRN" that carry no value
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

# Real values look different from fixtures. These target the former.
PATTERNS: list[tuple[str, str, str]] = [
    (
        "real WP- identifier",
        r"WP-(?![a-z]*\b(?:self|rec|prov|from-row|compose|conversation|bare|nope|padded|abc123|x\b))"
        r"[A-Za-z0-9\-_]{25,}",
        "A live token or record handle from an account. Replace with WP-<N chars>.",
    ),
    (
        "SSN",
        r"\b\d{3}-\d{2}-\d{4}\b",
        "Never belongs in the repo.",
    ),
    (
        "non-fixture phone",
        r"\b(?!\d{3}-555-)(?!888-218-6245)\d{3}-\d{3}-\d{4}\b",
        "Fixtures must use 555 numbers. Kaiser's public pharmacy line is allowlisted.",
    ),
    (
        "MRN value",
        r"\bMRN\s*[:=]\s*[\"']?\d{4,}",
        "A medical record number.",
    ),
    (
        "date of birth",
        r"\b(?:dob|date[ _-]?of[ _-]?birth)\b\s*[:=]\s*[\"']?\d{4}-\d{2}-\d{2}",
        "Fixtures should use 1970-01-01.",
    ),
    (
        "blood pressure reading",
        r"\b1[0-9]{2}/[5-9][0-9]\b(?!\s*(?:target|threshold))",
        "Real vitals. Only belongs in a message body, never in the repo.",
    ),
]

# Provider surnames that have appeared in this record. Add to this list rather
# than relying on memory — the point is that the next person does not have to
# know the history.
PROVIDER_NAMES = ["sheridan", "suarez"]

SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2", ".pdf", ".har")
SELF_NAME = "phi-scan.py"


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    return [f for f in out.split("\n") if f and not f.endswith(SKIP_SUFFIXES)]


def scan_tree() -> int:
    findings = 0
    for path in tracked_files():
        if path.endswith(SELF_NAME):
            continue  # This file names the patterns it hunts for.
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue

        for lineno, line in enumerate(lines, 1):
            for label, pattern, why in PATTERNS:
                for match in re.finditer(pattern, line, re.IGNORECASE):
                    findings += 1
                    print(f"{path}:{lineno}: [{label}] {match.group(0)[:60]}")
                    print(f"    {why}")
            lowered = line.lower()
            for name in PROVIDER_NAMES:
                if name in lowered:
                    findings += 1
                    print(f"{path}:{lineno}: [provider name] {name}")
                    print("    Discloses a care relationship. Use a role, not a name.")
    return findings


def scan_history() -> int:
    """Report commits that ever added a flagged string.

    History cannot be fixed by editing a file. Rewriting a public repo's
    history breaks every fork and clone, so this only reports — the call to
    rewrite belongs to a human.
    """
    findings = 0
    needles = PROVIDER_NAMES + ["passport.jpg"]
    for needle in needles:
        # --pickaxe-regex + -i, because plain -S is case-sensitive and the
        # first version of this script missed "Sheridan" while hunting for
        # "sheridan".
        out = subprocess.run(
            ["git", "log", "--all", "--oneline", "-i", "--pickaxe-regex", "-S", needle],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if out:
            findings += 1
            print(f"\n[history] '{needle}' appears in:")
            for line in out.split("\n"):
                print(f"    {line}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="also scan all commits")
    args = parser.parse_args()

    print("Scanning tracked files...")
    findings = scan_tree()

    if args.history:
        print("\nScanning history...")
        findings += scan_history()

    print()
    if findings:
        print(f"FAIL: {findings} finding(s). Nothing is published until these are resolved.")
        return 1
    print("PASS: no PHI or real identifiers found in tracked files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
