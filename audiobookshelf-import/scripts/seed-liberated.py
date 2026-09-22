#!/usr/bin/env python3
"""Tell Libation it already has the books Audiobookshelf already holds.

WHY THIS EXISTS
---------------
A fresh Libation install scans Audible and sees the entire library as
NotLiberated. Every book Audiobookshelf already holds - here, hundreds - would
be downloaded again, because the local copies were pruned after import and
Libation has no record of them.

Libation decides "already downloaded" from its own database, not from the
filesystem (that is also why pruning is safe afterwards: it will not re-fetch a
book whose file you delete). So the fix is to seed that database once.

HOW
---
`LibationCli set-status --downloaded` marks a book downloaded if
AudibleFileStorage.GetPath(asin) finds a file, and that lookup keys off the ASIN
in the filename - which the default naming templates put there as "[<id>]". So:

  1. ask ABS what it holds, and match it against Libation's library using the
     same matcher sync.py uses (ASIN, then title+author, then title-head+author)
  2. drop a zero-byte "<name> [ASIN].m4b" stub for each matched book
  3. run set-status --downloaded
  4. re-export and verify each target actually flipped to Liberated
  5. delete the stubs

Step 4 is the point. If a future Libation validates files instead of trusting
the name, the stubs stop working and this reports exactly which books did not
flip rather than quietly leaving them queued for download.

Stubs are only ever created for an ASIN with no real file, and cleanup only ever
removes zero-byte files this run created.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync as S  # noqa: E402

HERE = Path(__file__).resolve().parent
STUB_SUFFIX = ".m4b"


def libation_cmd():
    """LibationCli directly when running inside the container, otherwise the
    host wrapper that starts a container to do it."""
    direct = os.environ.get("LIBATION_CLI", "/libation/LibationCli")
    if Path(direct).exists():
        return [direct]
    return [str(HERE / "libation.sh")]


def run_libation(args, *cli, mutating=True):
    """Run a LibationCli command. Read-only commands run even in a dry run -
    the report cannot be produced without exporting the library first."""
    cmd = [*libation_cmd(), *cli]
    print(f"  $ {' '.join(cmd)}")
    if mutating and args.dry_run:
        print("    [dry-run] not executed")
        return 0
    return subprocess.call(cmd)


def export_library(args):
    """Refresh library.json, then load it."""
    lj = args.books_root / "library.json"
    if not args.no_export:
        # In-container the books root IS the path Libation writes to; via the host
        # wrapper it is the same directory seen from the other side of the mount.
        out = str(lj) if Path("/libation/LibationCli").exists() else "/data/library.json"
        rc = run_libation(args, "export", "-j", "-p", out, mutating=False)
        if rc != 0:
            sys.exit(f"export failed (exit {rc})")
    if not lj.is_file():
        sys.exit(f"No library.json at {lj}. Run a scan first.")
    return json.loads(lj.read_text(encoding="utf-8", errors="replace"))


def status_by_asin(records):
    return {str(r.get("AudibleProductId") or "").strip():
            str(r.get("BookStatus") or "").strip()
            for r in records if isinstance(r, dict)}


def main():
    S.load_dotenv()   # before the parser: env vars supply the defaults
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--books-root", default=os.environ.get("LIB_BOOKS_ROOT"),
                   help="Libation books dir on the host (default: $LIB_ROOT/books)")
    p.add_argument("--abs-url", default=os.environ.get("ABS_URL", "http://localhost:13378"))
    p.add_argument("--abs-token", default=os.environ.get("ABS_TOKEN"))
    p.add_argument("--library", default=os.environ.get("ABS_LIBRARY", "Audiobooks"))
    p.add_argument("--loose-match", action="store_true",
                   help="also accept a title-only match (risks false positives; "
                        "a false positive here means a book is never downloaded)")
    p.add_argument("--apply", action="store_true",
                   help="actually seed. Without this, report only.")
    p.add_argument("--no-export", action="store_true",
                   help="reuse the existing library.json instead of re-exporting")
    p.add_argument("--keep-stubs", action="store_true",
                   help="leave stubs in place (debugging only - sync.py would "
                        "then see a 0-byte file as a book's audio)")
    args = p.parse_args()
    args.dry_run = not args.apply

    if not args.books_root:
        lib_root = os.environ.get("LIB_ROOT")
        if not lib_root:
            p.error("--books-root or LIB_ROOT is required")
        args.books_root = Path(lib_root) / "books"
    args.books_root = Path(args.books_root).expanduser().resolve()
    if not args.books_root.is_dir():
        sys.exit(f"books root {args.books_root} is not a directory")
    if not args.abs_token:
        sys.exit("ABS_TOKEN not set")

    # --- what does ABS hold, and what does Libation think it has? ----------
    abs_api = S.Abs(args.abs_url, args.abs_token)
    library, _ = abs_api.resolve_library(args.library)
    index = S.AbsIndex(abs_api.all_items(library["id"]))
    print(f"ABS holds {len(index)} books ({len(index.asins)} with an ASIN)")

    records = export_library(args)
    print(f"Libation library: {len(records)} entries")
    statuses = status_by_asin(records)

    targets, already, absent = [], [], []
    for r in records:
        if not isinstance(r, dict):
            continue
        asin = str(r.get("AudibleProductId") or "").strip()
        title = str(r.get("Title") or "").strip()
        author = str(r.get("AuthorNames") or "").strip()
        if not title:
            continue
        liberated = statuses.get(asin, "").lower() == "liberated"
        hit, how = index.find(asin, title, author, loose=args.loose_match)
        if not hit:
            absent.append((asin, title, author))
        elif liberated:
            already.append((asin, title, author))
        else:
            targets.append((asin, title, author, how))

    print(f"\n  in ABS, already Liberated in Libation : {len(already)}")
    print(f"  in ABS, needs seeding                 : {len(targets)}")
    print(f"  not in ABS - will download normally   : {len(absent)}")

    if absent:
        print("\n  these will be downloaded on the next liberate:")
        for asin, title, author in sorted(absent, key=lambda x: x[1])[:40]:
            print(f"    {asin:<12} {title[:58]:<58} {author[:24]}")
        if len(absent) > 40:
            print(f"    ... and {len(absent) - 40} more")

    if not targets:
        print("\nNothing to seed.")
        return

    print("\n  to be marked as already downloaded:")
    for asin, title, author, how in sorted(targets, key=lambda x: x[1])[:40]:
        print(f"    {asin:<12} {title[:52]:<52} {author[:20]:<20} [by {how}]")
    if len(targets) > 40:
        print(f"    ... and {len(targets) - 40} more")

    if args.dry_run:
        print(f"\n[dry run] would seed {len(targets)} book(s). Re-run with --apply.")
        return

    # --- create stubs, but never over a real file -------------------------
    created = []
    missed = []
    try:
        for asin, title, _a, _how in targets:
            if S.resolve_audio({"asin": asin, "title": title},
                               args.books_root, "m4b"):
                print(f"  ! {asin} already has a real file, not stubbing")
                continue
            stub = args.books_root / f"seed-stub [{asin}]{STUB_SUFFIX}"
            if stub.exists():
                continue
            stub.touch()
            created.append(stub)
        print(f"\ncreated {len(created)} stub file(s) in {args.books_root}")

        rc = run_libation(args, "set-status", "--downloaded")
        if rc != 0:
            print(f"  !! set-status exited {rc}")

        # --- verify it took ------------------------------------------------
        print("\nverifying...")
        after = status_by_asin(export_library(args))
        flipped = [t for t in targets if after.get(t[0], "").lower() == "liberated"]
        missed[:] = [t for t in targets if after.get(t[0], "").lower() != "liberated"]
        print(f"  now Liberated : {len(flipped)}/{len(targets)}")
        if missed:
            print(f"  DID NOT FLIP  : {len(missed)} - these would still be downloaded:")
            for asin, title, _a, _h in missed[:20]:
                print(f"    {asin:<12} {title[:60]} (status={after.get(asin, '?')})")
            print("\n  The stub trick did not work for these. Options: mark them in\n"
                  "  the Libation GUI, or accept the re-download.")
    finally:
        if args.keep_stubs:
            print(f"\n--keep-stubs: leaving {len(created)} stub(s) in place")
        else:
            removed = 0
            for stub in created:
                try:
                    # only ever remove an empty file we made
                    if stub.is_file() and stub.stat().st_size == 0:
                        stub.unlink()
                        removed += 1
                    else:
                        print(f"  ! {stub.name} is no longer empty, leaving it")
                except OSError as e:
                    print(f"  ! could not remove {stub}: {e}")
            print(f"cleaned up {removed} stub file(s)")

    if missed:
        # Non-zero so setup.sh / a wrapper stops here rather than liberating a
        # library the seeding did not actually cover.
        sys.exit(f"\n{len(missed)} book(s) did not flip - stopping. "
                 f"Liberating now would re-download them.")

    print("\nDone. Confirm with:  ./sync.py --reconcile")


if __name__ == "__main__":
    main()
