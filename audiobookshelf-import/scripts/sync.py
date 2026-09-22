#!/usr/bin/env python3
"""Push downloaded Audible books into Audiobookshelf via the ABS API.

Reads Libation's library manifest (`export -j`, library.json) for authoritative
metadata, uploads each audio file through POST /api/upload (which places it into
<folder>/<author>/<series>/<title>/ itself), waits for the ABS watcher to create
the library item, then pins the metadata with POST /api/items/:id/match using
the Audible ASIN the manifest already knows.

State is kept per-ASIN so re-runs are idempotent.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from concurrent import futures
from pathlib import Path

import requests

# One Abs per worker thread: requests.Session is not documented as thread-safe.
_tls = threading.local()


def thread_abs(url, token):
    api = getattr(_tls, "abs", None)
    if api is None:
        api = Abs(url, token)
        _tls.abs = api
    return api

# Libation reports a locale name; ABS wants a provider id. Anything unlisted
# falls through to "us" -> the plain "audible" provider.
LIBATION_LOCALES = {
    "us": "us", "uk": "uk", "canada": "ca", "australia": "au", "france": "fr",
    "germany": "de", "japan": "jp", "italy": "it", "india": "in", "spain": "es",
    "brazil": "us",   # no audible.br provider in ABS; US catalogue is the closest
}

REGION_PROVIDERS = {
    "us": "audible",
    "ca": "audible.ca",
    "uk": "audible.uk",
    "gb": "audible.uk",
    "au": "audible.au",
    "fr": "audible.fr",
    "de": "audible.de",
    "jp": "audible.jp",
    "it": "audible.it",
    "in": "audible.in",
    "es": "audible.es",
}

# Extensions worth shipping alongside the audio file. ABS files these as
# supplementary (pdf/ebook) or picks the image up as a cover.
COMPANION_EXTS = {".pdf", ".epub", ".jpg", ".jpeg", ".png"}

def log(msg):
    print(msg, flush=True)


def norm(s):
    """Loose comparison key: strip accents, punctuation, case, whitespace.

    '&' becomes 'and' first: the same book is spelled "Leadership &
    Self-Deception" by Audible and "Leadership and Self-Deception" by ABS, and
    stripping the ampersand instead of expanding it makes those differ.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "", s)


class Abs:
    def __init__(self, url, token, timeout=60):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _req(self, method, path, **kw):
        kw.setdefault("timeout", self.timeout)
        r = self.s.request(method, f"{self.url}{path}", **kw)
        if r.status_code == 401:
            raise SystemExit("ABS rejected the token (401). Check ABS_TOKEN.")
        r.raise_for_status()
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def libraries(self):
        return (self._req("GET", "/api/libraries") or {}).get("libraries", [])

    def resolve_library(self, wanted):
        """Accept a library id or name; return (library, folder)."""
        libs = self.libraries()
        if not libs:
            raise SystemExit("ABS reports no libraries.")
        match = None
        for lib in libs:
            if lib.get("id") == wanted or norm(lib.get("name")) == norm(wanted):
                match = lib
                break
        if not match:
            names = ", ".join(f"{l.get('name')!r} ({l.get('id')})" for l in libs)
            raise SystemExit(f"No ABS library matching {wanted!r}. Available: {names}")
        folders = match.get("folders") or []
        if not folders:
            raise SystemExit(f"ABS library {match.get('name')!r} has no folders configured.")
        return match, folders[0]

    @staticmethod
    def folder_path(folder):
        # ABS 2.36 serializes this as fullPath; older builds used path
        return folder.get("fullPath") or folder.get("path") or "<folder>"

    def provider_lookup(self, provider, asin):
        """Confirm the ASIN resolves at the provider before uploading anything."""
        q = f"?provider={provider}&asin={asin}"
        return self._req("GET", f"/api/search/books{q}")

    def upload(self, library_id, folder_id, title, author, series, files):
        """POST /api/upload. ABS derives the destination folders from these fields."""
        data = {"library": library_id, "folder": folder_id, "title": title}
        if author:
            data["author"] = author
        if series:
            data["series"] = series
        handles, payload = [], {}
        try:
            for i, path in enumerate(files):
                fh = open(path, "rb")
                handles.append(fh)
                payload[f"{i}"] = (path.name, fh, "application/octet-stream")
            # long timeout: this streams the whole audio file
            self._req("POST", "/api/upload", data=data, files=payload, timeout=3600)
        finally:
            for fh in handles:
                fh.close()

    def recent_items(self, library_id, limit=50):
        q = f"?sort=addedAt&desc=1&limit={limit}&minified=1"
        return (self._req("GET", f"/api/libraries/{library_id}/items{q}") or {}).get("results", [])

    def all_items(self, library_id):
        """limit=0 returns the whole library in one response."""
        return (self._req("GET", f"/api/libraries/{library_id}/items?limit=0",
                          timeout=300) or {}).get("results", [])

    def scan(self, library_id, force=False):
        self._req("POST", f"/api/libraries/{library_id}/scan" + ("?force=1" if force else ""))

    def match(self, item_id, provider, asin, title=None, author=None,
              override_cover=True, override_details=True):
        body = {
            "provider": provider,
            "asin": asin,
            "overrideCover": override_cover,
            "overrideDetails": override_details,
        }
        # title/author act as fallback hints if the ASIN lookup comes up empty
        if title:
            body["title"] = title
        if author:
            body["author"] = author
        return self._req("POST", f"/api/items/{item_id}/match", json=body)

    def item(self, item_id):
        return self._req("GET", f"/api/items/{item_id}?expanded=1")


def _head(title):
    """The part before a subtitle, using the same split as title_keys."""
    return norm(re.split(r"\s+[-–—]\s+|\s*:\s+", str(title or "").strip())[0])


def _series_prefix_clash(a_title, b_title):
    """True when two titles agree only on a shared prefix, with each keeping a
    different remainder after it.

    "Red Company: Steel Rain" and "Red Company: Invasion" are two books whose
    common ground is a series name. "Leadership & Self-Deception" and
    "Leadership and Self-Deception: Getting Out of the Box" are one book, and the
    difference is that there the shared head IS one side's whole title. So the
    clash needs a remainder on both sides, and those remainders must disagree.
    """
    fa, fb = norm(a_title), norm(b_title)
    if fa == fb:
        return False
    ha, hb = _head(a_title), _head(b_title)
    return fa != ha and fb != hb


def title_keys(title):
    """Comparison keys for a title, including the part before a subtitle.

    Audible's catalogue title often concatenates the subtitle: "Dust - The Silo
    Saga, Book 3", "Armada - A Novel", "Elizabeth II: Life of a Monarch - An
    Audible Original". Audiobookshelf may store either the bare title ("Dust") or
    its own "Title: Subtitle" form. Comparing only the full string reports books
    as missing when ABS already has them, so index the head as well.

    The dash split requires whitespace on both sides, or hyphenated words like
    "Self-Deception" would be truncated. A colon does not: "Title: Subtitle" with
    no space before the colon is the usual convention, and requiring one meant
    ABS-side titles never produced a head key at all - which let
    "Leadership & Self-Deception" miss "Leadership and Self-Deception: Getting
    Out of the Box" and re-import it as a duplicate.
    """
    t = str(title or "").strip()
    if not t:
        return set()
    keys = {norm(t)}
    head = re.split(r"\s+[-–—]\s+|\s*:\s+", t)[0]
    if norm(head):
        keys.add(norm(head))
    # also drop trailing marketing suffixes Audible appends
    stripped = re.sub(
        r"\s+[-:–—]\s+(an?\s+audible\s+original(\s+drama)?|a\s+novel"
        r"|unabridged|abridged)\s*$", "", t, flags=re.I)
    if norm(stripped):
        keys.add(norm(stripped))
    return {k for k in keys if k}


def authors_overlap(a, b):
    """True if two author strings plausibly denote the same person(s)."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    # multi-author credits can be ordered differently; require one shared surname
    pa = {norm(x) for x in re.split(r"[,&/]| and ", str(a)) if norm(x)}
    pb = {norm(x) for x in re.split(r"[,&/]| and ", str(b)) if norm(x)}
    return bool(pa & pb)


class AbsIndex:
    """What Audiobookshelf already has, so we never import a duplicate.

    Two things defeat naive matching, both seen in this library:
      * the same audiobook carries DIFFERENT ASINs in ABS vs Audible's current
        catalogue (reissues and regional variants get new ids), so ASIN equality
        alone under-reports;
      * a title may carry its subtitle on one side and not the other, so exact
        title equality under-reports too.
    Hence: ASIN, then title-or-title-head combined with an author check.
    """

    def __init__(self, items):
        self.asins = {}
        self.by_title = {}      # key -> [items]
        self._count = 0
        for it in items:
            md = ((it.get("media") or {}).get("metadata") or {})
            asin = str(md.get("asin") or "").strip().upper()
            if asin:
                self.asins[asin] = it
            # Deliberately NOT indexing the folder name. Folders get named after
            # a series ("Nick Cole, Jason Anspach/Galaxy's Edge" actually holds
            # "Galaxy's Edge Part VI"), which falsely matches any other book in
            # that series and silently skips a real import.
            keys = title_keys(md.get("title"))
            if keys:
                self._count += 1
            for k in keys:
                self.by_title.setdefault(k, []).append(it)

    def __len__(self):
        return self._count

    def find(self, asin, title, author, loose=False):
        """Return (item, how) if ABS already has this book."""
        a = str(asin or "").strip().upper()
        if a and a in self.asins:
            return self.asins[a], "asin"

        keys = title_keys(title)
        cands, seen = [], set()
        for k in keys:
            for it in self.by_title.get(k, []):
                if id(it) not in seen:
                    seen.add(id(it))
                    cands.append(it)
        if not cands:
            return None, None

        # A non-exact hit means the head key matched, not the whole title. For a
        # series named "Series: Book" every entry shares that head key, so
        # "Red Company: Steel Rain" matched the owned "Red Company: Invasion",
        # was declared already-imported, and had its only local copy pruned.
        # Two distinct ASINs are two distinct books, so a head-key hit is
        # trusted only when the ABS side has no ASIN to contradict it. An exact
        # full-title hit still wins, since the same book legitimately carries
        # different ASINs across editions and regions.
        for it in cands:
            md = ((it.get("media") or {}).get("metadata") or {})
            if not authors_overlap(author, md.get("authorName") or md.get("author")):
                continue
            if norm(md.get("title")) == norm(title):
                return it, "title+author"
            other = str(md.get("asin") or "").strip().upper()
            if a and other and other != a and _series_prefix_clash(md.get("title"), title):
                continue
            return it, "title/subtitle+author"
        if loose:
            return cands[0], "title-only"
        return None, None


def author_spellings(items):
    """Map punctuation-insensitive author key -> the spelling ABS uses most.

    ABS derives author records from the metadata of whatever is uploaded, so
    importing "B.V. Larson" into a library that already holds "B. V. Larson"
    creates a SECOND author record and splits that author across two pages. That
    is not hypothetical: Red Company: Steel Rain arrived with the manifest's
    spelling and left a 1-book Larson beside the existing 51-book one, and
    W.E.B. Griffin split 47/4 the same way.

    The most-used spelling wins, because that is the record a merge would keep.
    """
    counts = {}
    for it in items:
        md = ((it.get("media") or {}).get("metadata") or {})
        name = str(md.get("authorName") or md.get("author") or "").strip()
        if not name:
            continue
        k = re.sub(r"[^a-z0-9]", "", name.lower())
        if not k:
            continue
        counts.setdefault(k, {})
        counts[k][name] = counts[k].get(name, 0) + 1
    return {k: max(v.items(), key=lambda kv: kv[1])[0] for k, v in counts.items()}


def prune(paths, dry_run, say=None):
    say = say or log
    freed = 0
    dirs = set()
    for p in paths:
        try:
            size = p.stat().st_size
            if dry_run:
                say(f"   [dry-run] would delete {p} ({size/1e6:.0f} MB)")
            else:
                p.unlink()
                say(f"   deleted {p} ({size/1e6:.0f} MB)")
            freed += size
            dirs.add(p.parent)
        except OSError as e:
            say(f"   !! could not delete {p}: {e}")
    for d in dirs:
        freed += tidy_dir(d, dry_run, say=say)
    return freed


def tidy_dir(d, dry_run, say=None):
    """Clean up after pruning a book's audio.

    Libation gives each book its own folder, so removing just the audio leaves a
    directory holding a cover image - one per imported book, accumulating for as
    long as the timer runs.

    Cover art goes, because ABS holds its own. A PDF does NOT: if the book was
    skipped as already-present, that PDF was never uploaded anywhere, and it is
    the only copy. Leaving it also leaves the directory, which is the visible
    signal that something was kept.
    """
    say = say or log
    freed = 0
    try:
        if not d.is_dir():
            return 0
        entries = list(d.iterdir())
        if any(e.is_dir() or e.suffix.lower() not in (".jpg", ".jpeg", ".png")
               for e in entries):
            return 0    # audio, a PDF, or a subfolder is still here - leave it be
        for e in entries:
            size = e.stat().st_size
            if dry_run:
                say(f"   [dry-run] would delete leftover cover {e.name}")
            else:
                e.unlink()
            freed += size
        if dry_run:
            say(f"   [dry-run] would remove empty {d}")
        else:
            d.rmdir()
            say(f"   removed empty {d}")
    except OSError as e:
        say(f"   !! could not tidy {d}: {e}")
    return freed


# `LibationCli export -j` writes an array of ExportDto records. The fields used
# here are AudibleProductId, Title, Subtitle, AuthorNames, SeriesNames,
# SeriesOrder, LengthInMinutes, Locale and BookStatus.
#
# Note there is no file path in the export. Libation locates a book's audio by
# ASIN (AudibleFileStorage.GetPath), which works because the naming templates
# embed "[<id>]" in both the folder and file name. resolve_audio below relies on
# the same convention, so do not remove <id> from FileTemplate - see
# config/Settings.json.

def load_books(root):
    """Read Libation's library export and normalise it to the internal shape.

    Expects library.json in `root`, written by `./libation.sh export -j -p
    /data/library.json`. Only Liberated books are returned: anything else has no
    audio file on disk yet, so there is nothing to import.
    """
    lj = root / "library.json"
    if not lj.is_file():
        raise SystemExit(
            f"No library.json at {lj}. Generate it with:\n"
            f"  ./libation.sh export -j -p /data/library.json")
    data = json.loads(lj.read_text(encoding="utf-8", errors="replace"))
    if isinstance(data, dict):
        for key in ("books", "items", "data", "Books"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise SystemExit(f"Unexpected library.json shape: {type(data).__name__}")

    out = []
    for r in data:
        if not isinstance(r, dict):
            continue
        status = str(r.get("BookStatus") or "").strip().lower()
        if status != "liberated":
            continue
        minutes = r.get("LengthInMinutes")
        locale = str(r.get("Locale") or "").strip().lower()
        out.append({
            "asin": str(r.get("AudibleProductId") or "").strip(),
            "title": str(r.get("Title") or "").strip(),
            "subtitle": str(r.get("Subtitle") or "").strip(),
            "author": str(r.get("AuthorNames") or "").strip(),
            "narrator": str(r.get("NarratorNames") or "").strip(),
            "series_name": str(r.get("SeriesNames") or "").strip(),
            "series_order": str(r.get("SeriesOrder") or "").strip(),
            # parse_duration takes a raw number as seconds. Minutes is coarse,
            # but the tolerance check is a percentage.
            "duration": (float(minutes) * 60) if isinstance(minutes, (int, float)) else None,
            "region": LIBATION_LOCALES.get(locale, "us"),
        })
    return out


def resolve_audio(book, root, ext):
    """Find the liberated audio file by ASIN.

    The naming templates put "[ASIN]" in the file name, so a glob on that is
    both exact and independent of how the title was sanitised for the
    filesystem. Falls back to a title match for files named by an older
    template.
    """
    ext = ext.lower().lstrip(".")
    asin = str(book.get("asin") or "").strip()
    if not root.is_dir():
        return None

    if asin:
        # Prefer the shortest path: a chapter-split or multi-part set would put
        # several matches under one folder, and the top-level file sorts first.
        hits = sorted(root.rglob(f"*[[]{asin}[]]*.{ext}"), key=lambda p: (len(p.parts), len(p.name)))
        if hits:
            return hits[0]
        # ASIN present but not bracketed (custom template)
        for hit in root.rglob(f"*{asin}*.{ext}"):
            return hit

    key = norm(book.get("title"))
    if key:
        for hit in root.rglob(f"*.{ext}"):
            if norm(hit.stem) == key:
                return hit
    return None


def companions(audio_path):
    """Adjacent PDFs / covers worth uploading with the book."""
    out = []
    for sib in sorted(audio_path.parent.iterdir()):
        if sib.is_file() and sib.suffix.lower() in COMPANION_EXTS:
            if norm(sib.stem) == norm(audio_path.stem) or norm(sib.stem) in ("cover", "folder"):
                out.append(sib)
    return out


def parse_duration(v):
    """A raw number is seconds; 'HH:MM:SS' and 'MM:SS' are also accepted."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    parts = str(v).strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    return None


def probe_duration(path):
    """Actual playable duration, or None if the file is unreadable."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=180)
        return float(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def verify_audio(path, book, tolerance, floor_sec=120.0):
    """Guard against importing a truncated or corrupt conversion.

    This matters because --prune-source deletes the only local copy after the
    import. A conversion interrupted midway leaves a file that is old enough and
    stable enough to look ready, so compare its real duration against the
    catalogue's before trusting it. Returns (ok, message).
    """
    got = probe_duration(path)
    if got is None or got <= 0:
        return False, "ffprobe could not read it (corrupt or still being written)"
    want = parse_duration(book.get("duration"))
    if not want:
        return True, f"{got / 3600:.2f}h (no catalogue duration to compare)"
    # A percentage alone is unusably tight on short books: 36 seconds of intro
    # on a 9-minute children's book is 8% drift and failed the 5% limit, while
    # the same 36 seconds on a novel is noise. Allow whichever is larger.
    drift = abs(got - want) / want
    if abs(got - want) > floor_sec and drift > tolerance:
        return False, (f"duration {got / 3600:.2f}h vs catalogue {want / 3600:.2f}h "
                       f"({drift * 100:.1f}% off, limit {tolerance * 100:.0f}% "
                       f"or {floor_sec:.0f}s)")
    return True, f"{got / 3600:.2f}h matches catalogue ({drift * 100:.1f}% drift)"


def is_stable(path, min_age, settle):
    """Guard against uploading a file that is still being written.

    Still needed under Libation even though it decrypts into its own InProgress
    directory: that directory is inside the container while the books folder is a
    bind mount, so the final move is cross-device - a copy, not an atomic rename.
    A large book is therefore briefly visible in the books folder while still
    growing. Costs one cycle of latency on a fresh download, which is why the
    log says "leaving for next pass" rather than treating it as an error.
    """
    if time.time() - path.stat().st_mtime < min_age:
        return False
    if settle <= 0:
        return True
    first = path.stat().st_size
    time.sleep(settle)
    return path.stat().st_size == first


def await_item(abs_api, library_id, title, author, timeout, interval, scan_after,
               known_ids=None, dest=None, exact_only=False):
    """Wait for the ABS watcher to turn the uploaded folder into a library item.

    Do NOT identify it by title: ABS re-derives the title from the audio file's
    embedded tags, so what comes back is frequently not what we uploaded
    ("Elizabeth II: Life of a Monarch" vs "... - An Audible Original").

    `dest` is the author/series/title path we asked ABS to file the upload under,
    which makes identification exact. That matters most with several imports in
    flight: claiming the wrong item would pin this book's ASIN onto another book
    and then, since the match "verified", delete that book's only local copy.
    So with exact_only (set whenever workers > 1) nothing but an exact relPath
    match is accepted, and a slow watcher just means a retry next pass.

    Serially there is only ever one candidate, so the looser fallbacks below are
    safe and save a pass when ABS rewrites the path (character sanitising).
    """
    known_ids = known_ids or set()
    want_t, want_a = norm(title), norm(author)
    want_dest = norm(dest) if dest else ""
    deadline = time.time() + timeout
    scanned = False

    def exact(it):
        return want_dest and norm(it.get("relPath")) == want_dest

    def plausible(it):
        """Loose guard so an unrelated concurrent add isn't mistaken for ours."""
        md = ((it.get("media") or {}).get("metadata") or {})
        rel, got_t = norm(it.get("relPath")), norm(md.get("title"))
        got_a = norm(md.get("authorName") or md.get("author"))
        for a, b in ((want_t, got_t), (want_t, rel), (want_a, got_a), (want_a, rel)):
            if a and b and (a in b or b in a):
                return True
        return False

    while time.time() < deadline:
        fresh = [it for it in abs_api.recent_items(library_id, limit=50)
                 if it.get("id") not in known_ids]
        for it in fresh:
            if exact(it):
                return it
        if not exact_only:
            for it in fresh:
                if plausible(it):
                    return it
            if len(fresh) == 1:
                # sole new arrival right after our upload - take it
                return fresh[0]
        if not scanned and scan_after and time.time() - (deadline - timeout) > scan_after:
            log("      watcher quiet, nudging a library scan")
            try:
                abs_api.scan(library_id)
            except Exception as e:
                log(f"      scan request failed (non-fatal): {e}")
            scanned = True
        time.sleep(interval)
    return None


def report_reconcile(books, src_root, index, args):
    """Downloaded books vs ABS: what is present and what still needs importing.
    Read-only - uploads and deletes nothing."""
    present, need_import, need_download = [], [], []

    for book in books:
        asin = str(book.get("asin") or book.get("product_id") or "").strip()
        title = str(book.get("title") or "").strip()
        if not title:
            continue
        author = str(book.get("author") or "").strip()
        hit, how = index.find(asin, title, author, loose=args.loose_match)
        audio = resolve_audio(book, src_root, args.ext)
        row = (title, author, asin, audio)
        if hit:
            present.append(row + (how,))
        elif audio:
            need_import.append(row)
        else:
            need_download.append(row)

    def dump(label, rows, show_file=False, limit=None):
        log(f"\n{label}: {len(rows)}")
        if not rows:
            return
        shown = rows if limit is None else rows[:limit]
        for r in shown:
            extra = ""
            if show_file and r[3]:
                extra = f"  <- {r[3].name} ({r[3].stat().st_size/1e6:.0f} MB)"
            if len(r) > 4 and r[4]:
                extra += f"  [by {r[4]}]"
            log(f"   {r[2] or '----------':<12} {r[0][:58]:<58} {r[1][:26]:<26}{extra}")
        if limit and len(rows) > limit:
            log(f"   ... and {len(rows) - limit} more")

    log("\n" + "=" * 78)
    log("RECONCILE  (Audible library vs Audiobookshelf)")
    log("=" * 78)
    dump("Already in ABS - nothing to do", present, limit=15)
    dump("Downloaded locally, NOT in ABS - would import", need_import, show_file=True)
    # Only Liberated books are read from the manifest, so this bucket does not
    # mean "not downloaded yet" - it means Libation believes it has the book, the
    # file is gone, and ABS does not have it either. Worth seeing: normally it is
    # empty, and anything in it was deleted without being imported. What is
    # genuinely still to download is license-guard's business, not this report's.
    dump("Marked downloaded, but no local file and not in ABS", need_download, limit=40)

    to_fetch_gb = None
    log("\n" + "-" * 78)
    log(f"{'library.json entries':<23} : {len(books)}")
    log(f"already in ABS          : {len(present)}")
    log(f"ready to import now     : {len(need_import)}")
    log(f"missing (see above)     : {len(need_download)}")
    if need_import:
        gb = sum(r[3].stat().st_size for r in need_import if r[3]) / 1e9
        log(f"import payload          : {gb:.1f} GB")
    log("-" * 78)
    log("\nRun without --reconcile to import the 'would import' set.")
    return


def load_state(path):
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except ValueError:
            log(f"warn: state file {path} is corrupt, starting fresh")
    return {}


def save_state(path, state):
    # create the parent if it's missing: losing state after a successful import
    # and prune would leave no record that the book was already handled
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def env_flag(name, default=False):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def load_dotenv():
    """Load .env from this script's directory into the environment.

    The shell entry points source .env themselves; without this, running
    ./sync.py directly would silently get different configuration than a cycle
    does - including falling back to a stale SOURCE_ROOT. Real environment
    variables win, so `SOURCE_ROOT=/tmp/x ./sync.py` still overrides it.
    """
    p = Path(__file__).resolve().parent / ".env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            os.environ.setdefault(key, val)


def main():
    # Before building the parser: argparse reads env vars for its defaults.
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source-root", dest="source_root",
                   default=os.environ.get("SOURCE_ROOT"),
                   help="Libation's books dir, holding library.json")
    p.add_argument("--abs-url", default=os.environ.get("ABS_URL", "http://localhost:13378"))
    p.add_argument("--abs-token", default=os.environ.get("ABS_TOKEN"))
    p.add_argument("--library", default=os.environ.get("ABS_LIBRARY", "Audiobooks"),
                   help="ABS library name or id")
    p.add_argument("--ext", default=os.environ.get("AUDIO_EXT", "m4b"))
    p.add_argument("--state", default=os.environ.get("STATE_FILE"),
                   help="default: <source-root>/.abs-synced.json")
    p.add_argument("--asin", action="append",
                   help="only sync these ASINs (repeatable) - handy for a first test")
    p.add_argument("--limit", type=int, default=int(os.environ.get("LIMIT", "0")),
                   help="stop after N successful uploads (0 = no limit)")
    p.add_argument("--workers", type=int, default=int(os.environ.get("WORKERS", "1")),
                   help="import this many books at once (default 1). Above 1, an ABS "
                        "item is only claimed on an exact destination-path match, so a "
                        "slow watcher costs a retry rather than risking a mis-claim.")
    p.add_argument("--no-series-folders", action="store_true",
                   default=env_flag("NO_SERIES_FOLDERS"),
                   help="use Author/Title instead of Author/Series/Title")
    p.add_argument("--no-match", action="store_true", default=env_flag("NO_MATCH"),
                   help="upload only, skip the ASIN match step")
    p.add_argument("--no-companions", action="store_true", default=env_flag("NO_COMPANIONS"),
                   help="do not upload adjacent pdf/cover files")
    p.add_argument("--reconcile", action="store_true",
                   help="report only: what ABS already has vs what is still to "
                        "download. Uploads nothing.")
    p.add_argument("--prune-source", action="store_true", default=env_flag("PRUNE_SOURCE"),
                   help="delete the local file after a VERIFIED match")
    p.add_argument("--no-verify-duration", action="store_true",
                   default=env_flag("NO_VERIFY_DURATION"),
                   help="skip the ffprobe integrity check before uploading")
    p.add_argument("--duration-tolerance", type=float,
                   default=float(os.environ.get("DURATION_TOLERANCE", "0.05")),
                   help="allowed drift vs the catalogue duration (default 0.05)")
    p.add_argument("--loose-match", action="store_true", default=env_flag("LOOSE_MATCH"),
                   help="also treat a title-only hit as 'already in ABS' (risks false "
                        "positives: distinct books share titles)")
    p.add_argument("--no-skip-existing", action="store_true", default=env_flag("NO_SKIP_EXISTING"),
                   help="upload even if ABS already has the book (default: skip)")
    p.add_argument("--keep-cover", action="store_true", default=env_flag("KEEP_COVER"),
                   help="do not let the match overwrite an existing cover")
    p.add_argument("--min-age", type=int, default=int(os.environ.get("MIN_AGE", "60")),
                   help="seconds a file must be untouched before uploading")
    p.add_argument("--settle", type=int, default=int(os.environ.get("SETTLE", "3")),
                   help="seconds to re-check file size for growth")
    p.add_argument("--wait", type=int, default=int(os.environ.get("WAIT", "240")),
                   help="seconds to wait for ABS to create the item")
    p.add_argument("--poll", type=int, default=int(os.environ.get("POLL", "4")))
    p.add_argument("--rescan-after", type=int, default=int(os.environ.get("RESCAN_AFTER", "45")),
                   help="if the watcher hasn't fired after N s, trigger a scan (0 = never)")
    p.add_argument("--interval", type=int, default=int(os.environ.get("SYNC_INTERVAL", "0")),
                   help="loop forever, sleeping N seconds between passes (0 = run once)")
    p.add_argument("--dry-run", action="store_true", default=env_flag("DRY_RUN"))
    args = p.parse_args()

    if not args.source_root:
        p.error("--source-root / SOURCE_ROOT is required")
    if not args.abs_token and not (args.dry_run and not args.reconcile):
        p.error("--abs-token / ABS_TOKEN is required (Settings -> API Keys in ABS)")

    src_root = Path(args.source_root).expanduser().resolve()
    if not src_root.is_dir():
        raise SystemExit(f"SOURCE_ROOT {src_root} is not a directory")
    state_path = Path(args.state) if args.state else src_root / ".abs-synced.json"

    while True:
        try:
            run_pass(args, src_root, state_path)
        except SystemExit:
            raise
        except Exception as e:
            log(f"pass failed: {type(e).__name__}: {e}")
            if not args.interval:
                raise
        if not args.interval:
            break
        log(f"\nsleeping {args.interval}s\n")
        time.sleep(args.interval)


def run_pass(args, src_root, state_path):
    books = load_books(src_root)
    state = load_state(state_path)
    log(f"library.json: {len(books)} importable entries | "
        f"already synced: {len(state)}")

    abs_api = None
    library = folder = None
    index = AbsIndex([])
    canon_author = {}
    if args.abs_token:
        abs_api = Abs(args.abs_url, args.abs_token)
        library, folder = abs_api.resolve_library(args.library)
        log(f"ABS library {library.get('name')!r} ({library['id']}) -> "
            f"{Abs.folder_path(folder)}")
        _items = abs_api.all_items(library["id"])
        index = AbsIndex(_items)
        canon_author = author_spellings(_items)
        log(f"ABS holds {len(index)} books ({len(index.asins)} with an ASIN)")

    if args.reconcile:
        return report_reconcile(books, src_root, index, args)

    only = {a.strip().upper() for a in (args.asin or [])} or None
    done = 0
    skipped_missing = []
    queue = []
    freed = 0

    for book in books:
        asin = str(book.get("asin") or book.get("product_id") or "").strip()
        title = str(book.get("title") or "").strip()
        if not title:
            continue
        if only and asin.upper() not in only:
            continue
        if not only:
            if asin and asin in state:
                continue

        author = str(book.get("author") or "").strip()
        existing = canon_author.get(re.sub(r"[^a-z0-9]", "", author.lower()))
        if existing and existing != author:
            log(f"  author {author!r} -> using existing ABS spelling {existing!r}")
            author = existing
        series = str(book.get("series_name") or "").strip()
        if args.no_series_folders:
            series = ""

        audio = resolve_audio(book, src_root, args.ext)
        if not audio:
            # No local file. If ABS already has the book, this is simply the
            # steady state after a successful import and prune - or a book seeded
            # as already-downloaded - so it is not worth reporting. Only a book
            # that is neither on disk nor in ABS is interesting.
            if not index.find(asin, title, author, loose=args.loose_match)[0]:
                skipped_missing.append(f"{title} [{asin or 'no asin'}]")
            continue

        # Already in ABS? Don't import a second copy - but do reclaim the space,
        # since ABS having it is the whole reason we keep a local copy.
        hit, how = index.find(asin, title, author, loose=args.loose_match)
        if hit and not args.no_skip_existing:
            log(f"  = {title}: already in ABS (matched by {how}), skipping import")
            # Deleting the only local copy is unrecoverable without a
            # re-download, so spend the disk unless the match actually
            # identified the book rather than merely agreeing on a title.
            if args.prune_source and how in ("asin", "title+author"):
                targets = [audio]
                freed += prune(targets, args.dry_run)
            elif args.prune_source:
                log(f"     source kept: {how} is too weak to delete on")
            continue

        if not is_stable(audio, args.min_age, args.settle):
            log(f"  ~ {title}: file still changing, leaving for next pass")
            continue

        if not args.no_verify_duration:
            ok, why = verify_audio(audio, book, args.duration_tolerance)
            if not ok:
                log(f"  !! {title}: FAILED integrity check - {why}")
                log("     not importing, source kept. Re-download it.")
                continue

        files = [audio]
        if not args.no_companions:
            files += companions(audio)

        queue.append({"book": book, "asin": asin, "title": title, "author": author,
                      "series": series, "audio": audio, "files": files})
        if args.limit and len(queue) >= args.limit:
            log(f"\nhit --limit {args.limit}, not queueing any more this pass")
            break

    # ---- import the queue, optionally several at a time ---------------------
    #
    # Everything above is local and cheap. What follows is upload, then a wait on
    # the ABS watcher, then match - and the wait is mostly idle, which is what
    # makes concurrency worth having on a backlog.
    workers = max(1, args.workers)
    lock = threading.Lock()
    totals = {"done": 0, "freed": 0}

    # One snapshot of pre-existing item ids for every worker. Safe because with
    # workers > 1 an item is only accepted on an exact destination-path match, so
    # a slightly stale set cannot cause a mis-claim - it only filters out items
    # that already existed.
    before = set()
    if queue and not args.dry_run:
        before = {it.get("id") for it in abs_api.recent_items(library["id"], limit=50)}

    def import_one(job):
        """Upload one book, match it, and prune on success. Thread-safe.

        Serially, lines are logged as they happen. In parallel they are collected
        and emitted as one block per book, because three interleaved uploads
        produce a log nobody can read.
        """
        book, asin = job["book"], job["asin"]
        title, author, series = job["title"], job["author"], job["series"]
        audio, files = job["audio"], job["files"]
        buf = []
        say = log if workers == 1 else buf.append
        # A Session is not documented as thread-safe, so each worker gets its own.
        api = abs_api if workers == 1 else thread_abs(args.abs_url, args.abs_token)

        try:
            dest = "/".join(x for x in (author, series, title) if x)
            say(f"\n>> {title}")
            say(f"   asin={asin or '-'} author={author or '-'} series={series or '-'}")
            say(f"   file={audio}  ({audio.stat().st_size / 1e6:.0f} MB)")
            say(f"   dest={Abs.folder_path(folder) if folder else '<folder>'}/{dest}/")
            if len(files) > 1:
                say(f"   plus: {', '.join(f.name for f in files[1:])}")
            if not args.no_verify_duration:
                say(f"   verified: {verify_audio(audio, book, args.duration_tolerance)[1]}")

            if args.dry_run:
                say("   [dry-run] would upload + match")
                with lock:
                    totals["done"] += 1
                return

            say("   uploading...")
            api.upload(library["id"], folder["id"], title, author, series, files)

            say("   waiting for ABS to index it...")
            item = await_item(api, library["id"], title, author,
                              args.wait, args.poll, args.rescan_after,
                              known_ids=before, dest=dest, exact_only=workers > 1)
            if not item:
                say("   !! ABS never indexed it. Files are on disk; check that the "
                    "library has 'Watch for file changes' enabled, then re-run.")
                return
            item_id = item["id"]
            say(f"   item {item_id}")

            matched = False
            if args.no_match or not asin:
                if not asin:
                    say("   no ASIN in the manifest, skipping match")
            else:
                provider = REGION_PROVIDERS.get(
                    str(book.get("region") or "us").lower(), "audible")
                say(f"   matching against {provider} asin={asin}")
                try:
                    api.match(item_id, provider, asin, title=title, author=author,
                              override_cover=not args.keep_cover, override_details=True)
                    full = api.item(item_id) or {}
                    md = ((full.get("media") or {}).get("metadata") or {})
                    got = str(md.get("asin") or "").strip()
                    matched = got.upper() == asin.upper()
                    if matched:
                        say(f"   matched: {md.get('title')} - {md.get('authorName') or author}")
                        if md.get("seriesName"):
                            say(f"            series: {md['seriesName']}")
                    else:
                        say(f"   !! match did not stick (item asin={got or 'empty'}, "
                            f"wanted {asin})")
                except requests.HTTPError as e:
                    say(f"   !! match failed: {e}")

            # Only reclaim space once ABS demonstrably has the book: the item
            # exists and, when an ASIN was available, the match round-tripped.
            pruned = []
            if args.prune_source:
                if matched or (args.no_match and item_id) or not asin:
                    targets = files
                    freed_here = prune(targets, args.dry_run, say=say)
                    pruned = [str(t) for t in targets]
                    with lock:
                        totals["freed"] += freed_here
                else:
                    say("   keeping source: match unverified, not safe to delete")

            with lock:
                if asin:
                    state[asin] = {
                        "title": title, "author": author, "itemId": item_id,
                        "source": str(audio), "matched": matched, "pruned": pruned,
                        "syncedAt": int(time.time()),
                    }
                    save_state(state_path, state)   # rewrites the whole file
                totals["done"] += 1
        except Exception as e:
            say(f"   !! {title}: {type(e).__name__}: {e}")
        finally:
            if buf:
                with lock:
                    for line in buf:
                        log(line)

    if queue:
        if workers > 1:
            log(f"\nimporting {len(queue)} book(s), {workers} at a time")
            with futures.ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(import_one, queue))
        else:
            for job in queue:
                import_one(job)

    done += totals["done"]
    freed += totals["freed"]

    if skipped_missing:
        log(f"\n{len(skipped_missing)} book(s) not in ABS and with no .{args.ext} on disk "
            f"(not converted yet):")
        for s in skipped_missing[:10]:
            log(f"   - {s}")
        if len(skipped_missing) > 10:
            log(f"   ... and {len(skipped_missing) - 10} more")

    log(f"\ndone: {done} uploaded this pass")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
