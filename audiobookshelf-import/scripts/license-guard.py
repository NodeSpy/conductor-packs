#!/usr/bin/env python3
"""Stop retrying books Audible refuses to license.

THE PROBLEM
-----------
Some books sit in the Audible library listing but cannot be downloaded:

    Audible denied a content license (download not allowed for this account/title).
    Membership: Customer is not part of any plans
    AYCL (aka: Plus catalog): Asin: [B00LV6V4UW] is not eligible for AYCL

Typically Audible Plus titles added while a membership was active and left
behind when it lapsed, or returned purchases. Libation leaves them
`NotLiberated` rather than `Error`, and `liberate` exits 0 - "failures do not
fail liberation" - so nothing marks them as hopeless. A cycle therefore counts
them as pending and retries them forever: at a ten minute cadence, six such
books are ~860 denied license requests to Audible every day, indefinitely.

WHAT THIS DOES
--------------
Records every ASIN Audible denies, and holds it back from subsequent liberate
attempts for RETRY_DENIED_HOURS (default 24). Held-back books are retried once a
day rather than 144 times, so re-subscribing to Plus still recovers them without
hammering the API in the meantime.

  --record FILE   scan liberate output and record any denials
  --eligible      ASINs worth attempting now (one per line)
  --count         with --eligible, print just the count
  --report        what is being held back, and why
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync as S  # noqa: E402

# "Content License denied for asin: [B00LV6V4UW]" is the one line Libation always
# emits per denial; the ownership/membership lines that follow vary by cause.
DENIED_RE = re.compile(r"Content License denied for asin:\s*\[([A-Za-z0-9]+)\]", re.I)


def state_path(lib_root):
    return Path(lib_root) / ".license-denied.json"


def load(path):
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s):
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def titles(manifest):
    if not manifest.is_file():
        return {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, list):
        return {}
    return {str(r.get("AudibleProductId") or ""): (str(r.get("Title") or ""),
                                                   str(r.get("BookStatus") or ""),
                                                   r.get("AbsentFromLastScan") is True)
            for r in data if isinstance(r, dict)}


def main():
    S.load_dotenv()
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lib-root", default=os.environ.get("LIB_ROOT"))
    p.add_argument("--books-root", default=os.environ.get("LIB_BOOKS_ROOT"))
    p.add_argument("--record", metavar="FILE",
                   help="scan liberate output for denials and record them")
    p.add_argument("--eligible", action="store_true",
                   help="print ASINs worth attempting now")
    p.add_argument("--count", action="store_true", help="with --eligible, print a count")
    p.add_argument("--report", action="store_true")
    p.add_argument("--clear", metavar="ASIN", action="append",
                   help="stop holding back an ASIN (repeatable; 'all' for everything)")
    p.add_argument("--retry-hours", type=float,
                   default=float(os.environ.get("RETRY_DENIED_HOURS", "24")))
    a = p.parse_args()

    if not a.lib_root:
        sys.exit("LIB_ROOT not set")
    books_root = Path(a.books_root) if a.books_root else Path(a.lib_root) / "books"
    sp = state_path(a.lib_root)
    denied = load(sp)
    meta = titles(books_root / "library.json")

    if a.clear:
        if "all" in a.clear:
            denied = {}
            print("cleared all held-back books")
        else:
            for asin in a.clear:
                if denied.pop(asin, None) is not None:
                    print(f"cleared {asin}")
                else:
                    print(f"{asin} was not held back")
        save(sp, denied)
        return

    if a.record:
        text = Path(a.record).read_text(encoding="utf-8", errors="replace")
        hits = {m.group(1) for m in DENIED_RE.finditer(text)}
        for asin in sorted(hits):
            entry = denied.get(asin) or {"first_seen": now_iso(), "attempts": 0}
            entry["last_denied"] = now_iso()
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            title = meta.get(asin, ("", ""))[0]
            if title:
                entry["title"] = title
            denied[asin] = entry
        save(sp, denied)
        if hits:
            print(f"license-guard: recorded {len(hits)} denied book(s); "
                  f"held back for {a.retry_hours:g}h")
        return

    # Which NotLiberated books should we attempt right now?
    cutoff = datetime.now(timezone.utc) - timedelta(hours=a.retry_hours)
    eligible, held, withdrawn = [], [], []
    for asin, (title, status, absent) in sorted(meta.items(), key=lambda kv: kv[1][0]):
        if not asin or status.lower() == "liberated":
            continue
        if absent:
            # Audible's own library listing no longer returns this title, so it
            # is not "failing to download" - it is gone from the account, and a
            # license request for it can only ever be denied. Skipping on this
            # flag means no wasted request at all, and it self-heals: if the book
            # reappears in a scan the flag clears and it becomes eligible again.
            withdrawn.append((asin, title))
            continue
        entry = denied.get(asin)
        last = parse_iso(entry.get("last_denied")) if entry else None
        if last and last > cutoff:
            held.append((asin, title, entry))
        else:
            eligible.append((asin, title))

    if a.report:
        print(f"eligible to download : {len(eligible)}")
        for asin, title in eligible:
            print(f"   {asin:<12} {title[:60]}")

        print(f"\nno longer in your Audible library, never downloaded: {len(withdrawn)}")
        for asin, title in withdrawn:
            print(f"   {asin:<12} {title[:60]}")
        if withdrawn:
            print("   Audible's library listing no longer returns these, so a license")
            print("   request can only be denied. Usually an Audible Plus title that")
            print("   left the catalogue or a lapsed membership, or a returned purchase.")
            print("   Buying a title puts it back in the library and the next cycle")
            print("   picks it up automatically - no action needed here.")

        if held:
            print(f"\nheld back after a denial, retried every {a.retry_hours:g}h: {len(held)}")
            for asin, title, entry in held:
                print(f"   {asin:<12} {title[:44]:<44} "
                      f"attempts={entry.get('attempts', '?')} last={entry.get('last_denied', '?')}")
            print("   ./license-guard.py --clear all  to retry these immediately")
        return

    if a.eligible:
        if a.count:
            print(len(eligible))
        else:
            for asin, _t in eligible:
                print(asin)
        return

    p.error("nothing to do: pass --record, --eligible, --report or --clear")


if __name__ == "__main__":
    main()
