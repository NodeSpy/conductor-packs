"""pr-review-team calibration: the deterministic half of review-calibration and
review-backfill.

    calibration.py collect-calibration   what happened since the last run
    calibration.py collect-backfill      a window of human review comments, by month
    calibration.py record                promote the run's state, append the log

The pack runs it as a `use: cli` step (`code: file:${pack.dir}/...` + args), so
it is one self-contained file. ctx arrives as JSON on stdin; the result is one
JSON object on stdout. Everything is configured through the environment the
pack's steps set from its settings — nothing here names a host, an org or a
person. It never writes memory: the pack's memory.remember step does that.
"""
import datetime as dt
import json
import os
import re
import secrets
import subprocess
import sys

PACK_DIR_NAME = "pr-review-team"

# owner/name, nothing else: a repo read from the audit log, the state file or
# --input reaches git URLs, gh API paths and directories under the cache only
# after it matches this (no "..", no extra "/", no spaces or URL syntax).
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def valid_repo(repo):
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        return False
    return all(part not in (".", "..") for part in repo.split("/"))


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return dt.datetime.fromisoformat(t.replace("Z", "+00:00"))


def env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def state_dir():
    """conductor's state directory: the setting, else XDG_STATE_HOME, else ~/.local/state."""
    d = (os.environ.get("STATE_DIR") or "").strip()
    if d:
        return os.path.expanduser(d)
    x = (os.environ.get("XDG_STATE_HOME") or "").strip()
    if x:
        return os.path.join(x, "conductor")
    return os.path.expanduser("~/.local/state/conductor")


def cache_dir():
    d = os.path.join(state_dir(), PACK_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def audit_files():
    """The audit log and its rotated predecessor, oldest first."""
    a = (os.environ.get("AUDIT_LOG") or "").strip()
    a = os.path.expanduser(a) if a else os.path.join(state_dir(), "audit.jsonl")
    return [a + ".1", a]


class NeedsGrant(Exception):
    """The step is running without what it needs (network, the state dir)."""


GRANT_HINT = (
    "this step needs the network (gh, git) and conductor's state directory, which a pack's "
    "code steps do not get by default. Grant it in your packs: block — "
    "steps: { review-calibration/collect: { trust: full }, review-calibration/record: { trust: full } } — "
    "and set the state_dir setting if your state is not in ~/.local/state/conductor. "
    "See the pack README (\"Arming calibration\")."
)


def require_audit():
    files = [f for f in audit_files() if os.path.exists(f)]
    if not files:
        raise NeedsGrant("no audit log at %s — %s" % (audit_files()[-1], GRANT_HINT))
    return files


def audit_events(needle=None):
    """Yield parsed audit events (oldest first), pre-filtered on a substring."""
    for name in audit_files():
        try:
            f = open(name)
        except OSError:
            continue
        with f:
            for line in f:
                if needle and not any(n in line for n in needle):
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def is_review_post(e):
    """A posted review: verb=submit_review from a workflow step, or uses=<conn>.submit_review
    when an agent posts it through a skill grant."""
    if e.get("event") != "verb" or e.get("outcome") != "ok":
        return False
    uses = e.get("uses") or ""
    return e.get("verb") == "submit_review" or uses.endswith(".submit_review")


def step_name(e):
    s = e.get("step") or e.get("id") or ""
    return s.rsplit("/", 1)[-1]


def gh(*args):
    res = subprocess.run(["gh", *args], capture_output=True, text=True)
    return res


def gh_login():
    res = gh("api", "user", "--jq", ".login")
    login = (res.stdout or "").strip()
    if res.returncode != 0 or not login:
        raise NeedsGrant("`gh api user` failed (%s) — %s" % ((res.stderr or "").strip()[:200], GRANT_HINT))
    return login


def gh_json(path):
    res = gh("api", path)
    try:
        return json.loads(res.stdout or "null")
    except ValueError:
        return None


def git(d, *a):
    return subprocess.run(["git", "-C", d, *a], capture_output=True, text=True).stdout


GIT_AUTH = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]


def clone(repo, fetched):
    """A partial (blob:none) clone of repo under the pack's cache dir, fetched once per run.
    Returns (dir, default-branch ref) or None when it can't be had."""
    if not valid_repo(repo):
        return None
    if repo in fetched:
        return fetched[repo]
    d = os.path.join(cache_dir(), "clones", *repo.split("/", 1))
    if not os.path.isdir(os.path.join(d, ".git")):
        os.makedirs(os.path.dirname(d), exist_ok=True)
        base = (os.environ.get("GIT_BASE") or "https://github.com").rstrip("/")
        r = subprocess.run(["git", *GIT_AUTH, "clone", "-q", "--filter=blob:none", "--no-checkout",
                            "%s/%s.git" % (base, repo), d], capture_output=True, text=True)
        if r.returncode != 0:
            fetched[repo] = None
            return None
    else:
        subprocess.run(["git", *GIT_AUTH, "-C", d, "fetch", "-q", "origin"], capture_output=True, text=True)
    head = git(d, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip() or "origin/main"
    fetched[repo] = (d, head)
    return fetched[repo]


def human_comments(repo, since, max_pages, skip_login=None, truncated=None, trusted=None, dropped=None, owner=None):
    """Top-level inline review comments on repo's PRs created after `since`, newest first,
    by humans (bots and replies dropped; skip_login's dropped too when given). With
    `trusted` (a set of author_association values), a comment counts only when its
    author's association is in it — or its author is `owner`, the reviewer's own
    account; everything else (bots included) is dropped and counted in `dropped`
    by reason. When the page cap ends the read before `since` is reached, repo is
    appended to `truncated`."""
    if not valid_repo(repo):
        return
    for page in range(1, max_pages + 1):
        batch = gh_json("repos/%s/pulls/comments?sort=created&direction=desc&per_page=100&page=%d" % (repo, page))
        if not isinstance(batch, list) or not batch:
            return
        for c in batch:
            ts = iso(c["created_at"])
            if ts <= since:
                return
            if c.get("in_reply_to_id"):
                continue
            u = c.get("user") or {}
            login = u.get("login", "")
            if u.get("type") == "Bot" or login.endswith("[bot]"):
                if dropped is not None:
                    dropped["bot"] = dropped.get("bot", 0) + 1
                continue
            if skip_login and login == skip_login:
                continue
            if trusted is not None and not (owner and login == owner):
                assoc = str(c.get("author_association") or "unknown")
                if assoc not in trusted:
                    if dropped is not None:
                        dropped[assoc] = dropped.get(assoc, 0) + 1
                    continue
            yield ts, login, c
        if len(batch) < 100:
            return
    if truncated is not None:
        truncated.append(repo)


# ---------------------------------------------------------------------------
# Training-data hygiene. What calibration and backfill read — PR titles and
# bodies, commit messages, patches, comments — is written by people the
# reviewer doesn't answer to, and whatever it learns is read by every later
# review. So: comments count only from trusted authors (the
# trusted_associations setting, by GitHub author_association), and every
# piece of that text reaches an agent fenced as data — control characters
# stripped, long items cut, inside <<<TOKEN … TOKEN>>> with a token drawn
# fresh each run, so text written before the run can't close the fence.
# ---------------------------------------------------------------------------

ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "MANNEQUIN", "NONE")
TRUSTED_DEFAULT = "OWNER,MEMBER,COLLABORATOR"
ITEM_CAP = 600          # chars of one fenced item
CTRL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]")
TOKEN_RE = re.compile(r"ev[0-9a-f]{16}", re.I)


def trusted_associations():
    raw = (os.environ.get("TRUSTED_ASSOCIATIONS") or "").strip() or TRUSTED_DEFAULT
    if raw.lower() == "none":
        return set()
    out = set()
    for a in re.split(r"[\s,]+", raw.upper()):
        if not a:
            continue
        if a not in ASSOCIATIONS:
            raise SystemExit("pr-review-team: the trusted_associations setting takes GitHub author associations (%s) "
                             "or none, got %r" % (", ".join(ASSOCIATIONS), a))
        out.add(a)
    return out


class Fence:
    """Untrusted text as data: fence(text) -> "<<<TOKEN text TOKEN>>>" (lines=True keeps
    line breaks and puts the markers on lines of their own)."""

    def __init__(self, token=None):
        self.token = token or "ev" + secrets.token_hex(8)

    def clean(self, text, limit=ITEM_CAP, lines=False):
        t = CTRL_RE.sub(" ", str(text or ""))
        t = TOKEN_RE.sub("[fence]", t)   # this run's token, or one guessed or seen before
        t = t.replace("<<<", "<< <").replace(">>>", "> >>")
        t = "\n".join(l.rstrip() for l in t.splitlines()) if lines else " ".join(t.split())
        if len(t) > limit:
            t = t[:limit] + " …[cut at %d chars]" % limit
        return t

    def __call__(self, text, limit=ITEM_CAP, lines=False):
        t = self.clean(text, limit, lines)
        if lines:
            return "<<<%s\n%s\n%s>>>" % (self.token, t, self.token)
        return "<<<%s %s %s>>>" % (self.token, t, self.token)


def commit_trust(repo, sha, trusted, me):
    """Who a follow-up commit came from: the PR GitHub associates it with, judged by its
    author's association (or the reviewer's own account). A commit with no PR was pushed
    straight to the branch, which takes write access. -> (trusted?, reason)."""
    prs = gh_json("repos/%s/commits/%s/pulls" % (repo, sha)) if valid_repo(repo) and re.fullmatch(r"[0-9a-f]{7,40}", sha) else None
    if not isinstance(prs, list):
        return False, "unknown"
    if not prs:
        return True, "push"
    for p in prs:
        if (p.get("user") or {}).get("login") == me or str(p.get("author_association") or "") in trusted:
            return True, "pr"
    return False, str(prs[0].get("author_association") or "unknown")


def cap_items(items, limit, what):
    """Join items (each a line, or a fenced multi-line block) up to `limit` chars,
    dropping whole items past it — a fence is never cut in half."""
    out, size = [], 0
    for i, it in enumerate(items):
        if size + len(it) + 1 > limit:
            out.append("…[%d more item(s) cut: %s over %d chars]" % (len(items) - i, what, limit))
            break
        out.append(it)
        size += len(it) + 1
    return "\n".join(out) + "\n"


def norm(text):
    return " ".join((text or "").split())


def pack_posts():
    """Every review this pack posted, from the audit log: "repo#pr" -> [{ts, body, comments}].
    Reviews post AS the reviewer's account, so the audit log — not the author — is what
    tells the pack's reviews apart from what the owner wrote by hand."""
    out = {}
    for e in audit_events(['submit_review']):
        if not is_review_post(e):
            continue
        o = e.get("options") or {}
        repo = o.get("repo") or e.get("repo")
        try:
            pr = int(o.get("pr") or e.get("number") or 0)
        except (TypeError, ValueError):
            continue
        if not valid_repo(repo) or pr <= 0:
            continue
        out.setdefault("%s#%d" % (repo, pr), []).append(
            {"ts": iso(e["ts"]), "body": norm(o.get("body")), "comments": set(norm_comments(o.get("comments")).values())})
    return out


def audit_start():
    """The oldest timestamp still in the audit log (it rotates), or None."""
    for name in audit_files():
        try:
            with open(name) as f:
                for line in f:
                    try:
                        return iso(json.loads(line)["ts"])
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            continue
    return None


# A GitHub review is one this pack posted when the reviewer's account submitted it within
# MATCH_SECONDS of an audited submit_review on the same PR, with the same body (the
# connector posts the body verbatim). GitHub's review id is not in the audit log.
MATCH_SECONDS = 900


def is_pack_review(rv, posts):
    t = rv.get("submitted_at")
    if not t:
        return False
    t, body = iso(t), norm(rv.get("body"))
    return any(abs((t - p["ts"]).total_seconds()) <= MATCH_SECONDS and body == p["body"] for p in posts)


def gh_list(path, max_pages=10):
    out = []
    for page in range(1, max_pages + 1):
        batch = gh_json("%s%sper_page=100&page=%d" % (path, "&" if "?" in path else "?", page))
        if not isinstance(batch, list) or not batch:
            break
        out += batch
        if len(batch) < 100:
            break
    return out


def pack_review_ids(repo, pr, me, posts, cache):
    """GitHub ids of the reviews this pack posted on repo#pr (and the reviews list itself)."""
    key = "%s#%d" % (repo, pr)
    if key not in cache:
        reviews = gh_list("repos/%s/pulls/%d/reviews" % (repo, pr)) if valid_repo(repo) else []
        ids = {rv.get("id") for rv in reviews
               if (rv.get("user") or {}).get("login") == me and is_pack_review(rv, posts)}
        cache[key] = (ids, reviews)
    return cache[key]


def owner_activity(repo, pr, me, posts, cache):
    """What the reviewer's own account did on a PR this pack reviewed that the pack did NOT
    post: reviews (with their verdict), inline comments and replies, conversation comments —
    and the pack's own reviews that were since dismissed. [(ts or None, label, text)]."""
    if not valid_repo(repo):
        return []
    ids, reviews = pack_review_ids(repo, pr, me, posts, cache)
    audited = set().union(*[p["comments"] for p in posts]) if posts else set()
    out = []
    for rv in reviews:
        if rv.get("id") in ids:
            if rv.get("state") == "DISMISSED":
                out.append((None, "DISMISSED the pack's review", norm(rv.get("body"))[:300]))
            continue
        if (rv.get("user") or {}).get("login") != me or not rv.get("submitted_at"):
            continue
        state, body = rv.get("state") or "", norm(rv.get("body"))
        if not body and state not in ("CHANGES_REQUESTED",):
            continue   # the empty shell of inline comments (listed below), or a bare approval
        out.append((iso(rv["submitted_at"]), "REVIEW " + state, body[:600]))
    inline = gh_list("repos/%s/pulls/%d/comments" % (repo, pr))
    by_id = {c.get("id"): c for c in inline}
    for c in inline:
        if (c.get("user") or {}).get("login") != me or not c.get("created_at"):
            continue
        body = norm(c.get("body"))
        if c.get("pull_request_review_id") in ids or body in audited:
            continue   # the pack's own inline comment
        label = "INLINE %s:%s" % (c.get("path"), c.get("line") or c.get("original_line") or "")
        parent = by_id.get(c.get("in_reply_to_id"))
        if parent is not None:
            label += " (reply to %s: %s)" % (
                "the pack's comment" if parent.get("pull_request_review_id") in ids else (parent.get("user") or {}).get("login"),
                norm(parent.get("body"))[:200])
        out.append((iso(c["created_at"]), label, body[:600]))
    for c in gh_list("repos/%s/issues/%d/comments" % (repo, pr)):
        if (c.get("user") or {}).get("login") == me and c.get("created_at"):
            out.append((iso(c["created_at"]), "COMMENT", norm(c.get("body"))[:600]))
    return out


# ---------------------------------------------------------------------------
# Scope-key facts. The pack's learning scope is a template (the scope_key
# setting) over a review's facts; the lens panel's memory reads the key core
# renders for the review. Calibration maps every PR it shows the agent to its
# key the same way: these are that PR's facts as the review saw them, and the
# pack's `keys` step renders the template over them. Only the facts a past PR
# still has are rebuilt — the target (repo, owner, name, number/pr), the event
# kind, the review-team step's inputs, and — fetched only when the template
# reads them — title, labels, base and url (their CURRENT values).
# ---------------------------------------------------------------------------

def pr_facts(repo, pr, need_meta, cache):
    key = "%s#%d" % (repo, pr)
    if key in cache:
        return cache[key]
    owner, name = repo.split("/", 1)
    f = {"repo": repo, "owner": owner, "name": name, "number": pr, "pr": pr, "issue": 0,
         "kind": "review_requested", "inputs": {"repo": repo, "pr": pr}}
    if need_meta and valid_repo(repo):
        d = gh_json("repos/%s/pulls/%d" % (repo, pr))
        if isinstance(d, dict):
            f["title"] = d.get("title") or ""
            f["labels"] = [l.get("name") for l in (d.get("labels") or []) if isinstance(l, dict) and l.get("name")]
            f["base"] = (d.get("base") or {}).get("ref") or ""
            f["url"] = d.get("html_url") or ""
    cache[key] = f
    return f


def facts_for(refs):
    """[{ref, facts}] for every "owner/name#N" in refs (sorted, deduplicated)."""
    # the pack's keyspec step says whether scope_key reads a fact that needs the PR fetched
    need, cache, out = (os.environ.get("NEED_META") or "").strip() == "true", {}, []
    for ref in sorted(set(refs)):
        repo, _, n = ref.rpartition("#")
        if valid_repo(repo) and n.isdigit():
            out.append({"ref": ref, "facts": pr_facts(repo, int(n), need, cache)})
    return out


def cap(text, limit, what):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[%s truncated at %d bytes]\n" % (what, limit)


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# collect-calibration
# ---------------------------------------------------------------------------

REVIEWS_CAP = 120_000   # bytes of review/follow-up text handed to the agent
TEAM_CAP = 60_000       # bytes of team comments handed to the agent
OWNER_CAP = 60_000      # bytes of owner catches handed to the agent
PATCH_CAP = 3_000       # bytes of one follow-up commit's patch


def added_lines(d, sha, sign):
    """(file, stripped line) pairs a commit adds (+) or removes (-), ignoring short lines."""
    out, f = set(), None
    hdr = "+++ b/" if sign == "+" else "--- a/"
    for line in git(d, "show", "-U0", "--format=", sha).splitlines():
        if line.startswith(hdr):
            f = line[6:]
        elif line.startswith(sign) and not line.startswith(sign * 3) and f and len(line[1:].strip()) > 8:
            out.add((f, line[1:].strip()))
    return out


def norm_comments(cs):
    if isinstance(cs, str):
        try:
            cs = json.loads(cs)
        except ValueError:
            cs = []
    return {(c.get("path"), c.get("line")): " ".join((c.get("body") or "").split())
            for c in (cs or []) if isinstance(c, dict)}


def collect_calibration():
    WINDOW_DAYS = env_int("WINDOW_DAYS", 14)
    BOOTSTRAP_DAYS = env_int("BOOTSTRAP_DAYS", 30)
    UNMERGED_DAYS = env_int("UNMERGED_DAYS", 30)
    DISCARD_DAYS = env_int("DISCARD_DAYS", 7)
    MIN_LINES = env_int("MIN_LINES", 3)
    TEAM_PAGES = env_int("TEAM_PAGES", 15)

    require_audit()
    trusted = trusted_associations()
    me = gh_login()
    fence, untrusted = Fence(), {}
    cache = cache_dir()
    mark = os.path.join(cache, "calibration.json")
    t_now = now()
    st = {}
    try:
        with open(mark) as f:
            st = json.load(f)
    except (OSError, ValueError):
        pass
    first = "last_run" not in st
    last = iso(st["last_run"]) if not first else t_now - dt.timedelta(days=BOOTSTRAP_DAYS)
    tracked = st.get("tracked") or {}   # "repo#pr" -> {repo, pr, at, event, body, comments, merged, own}
    day = lambda d: iso(d + "T00:00:00+00:00")

    # 1. Reviews posted since the last run — the repos calibration covers are
    #    exactly the repos these name (plus the ones still tracked).
    new_keys, invalid = [], set()
    for e in audit_events(['submit_review']):
        if not is_review_post(e):
            continue
        ts = iso(e["ts"])
        if ts <= last:
            continue
        o = e.get("options") or {}
        repo = o.get("repo") or e.get("repo")
        try:
            pr = int(o.get("pr") or e.get("number") or 0)
        except (TypeError, ValueError):
            pr = 0
        if not valid_repo(repo) or pr <= 0:
            invalid.add(str(repo))
            continue
        key = "%s#%d" % (repo, pr)
        comments = norm_comments(o.get("comments"))
        prev = tracked.get(key, {})
        tracked[key] = {"repo": repo, "pr": pr, "at": e["ts"][:10], "event": o.get("event", ""),
                        "body": (o.get("body") or "")[:200],
                        "comments": [{"path": k[0], "line": k[1], "body": v[:200]} for k, v in comments.items()][:8],
                        "merged": prev.get("merged"), "own": prev.get("own", [])}
        if key not in new_keys:
            new_keys.append(key)

    # 2. Follow-ups: for every tracked PR still inside its window, commits that
    #    landed since the last run and change or remove lines the PR added.
    fetched = {}
    followups = {}
    for key, r in list(tracked.items()):
        if not isinstance(r, dict) or not valid_repo(r.get("repo")):
            invalid.add(str(r.get("repo") if isinstance(r, dict) else key))
            del tracked[key]
            continue
        c = clone(r["repo"], fetched)
        if not c:
            if day(r["at"]) < t_now - dt.timedelta(days=UNMERGED_DAYS):
                del tracked[key]
            continue
        d, head = c
        if not r.get("merged"):
            own = git(d, "log", head, "--no-merges", "--format=%H", "-F", "--grep", "(#%d)" % r["pr"],
                      "--since", (day(r["at"]) - dt.timedelta(days=60)).date().isoformat()).split()
            own += git(d, "log", head, "--merges", "--format=%H", "-F", "--grep",
                       "Merge pull request #%d " % r["pr"]).split()[:1]
            if not own:
                if day(r["at"]) < t_now - dt.timedelta(days=UNMERGED_DAYS):
                    del tracked[key]   # never merged (or merged under another title)
                continue
            r["own"], r["merged"] = own, git(d, "log", "-1", "--format=%cI", own[0]).strip()
        merged = iso(r["merged"])
        if merged < t_now - dt.timedelta(days=WINDOW_DAYS) and merged < last - dt.timedelta(days=WINDOW_DAYS):
            del tracked[key]           # window closed before this run's range
            continue
        added = set()
        for h in r["own"]:
            added |= added_lines(d, h, "+")
        files = sorted({f for f, _ in added})
        if not files:
            continue
        lo, hi = max(merged, last), merged + dt.timedelta(days=WINDOW_DAYS)
        hits = []
        for h in git(d, "log", head, "--no-merges", "--format=%H", "--since", lo.isoformat(),
                     "--until", hi.isoformat(), "--", *files).split():
            if h in r["own"]:
                continue
            n = len(added_lines(d, h, "-") & added)
            if n >= MIN_LINES:
                touched = sorted({f for f, _ in added_lines(d, h, "-") & added})
                hits.append({"sha": h[:10], "full": h, "lines": n,
                             "message": git(d, "log", "-1", "--format=%s%n%n%b", h).strip()[:600],
                             "patch": cap(git(d, "show", "-U3", "--format=", h, "--", *touched), PATCH_CAP, "patch")})
        kept = []
        for hit in sorted(hits, key=lambda x: -x["lines"])[:4]:
            ok, why = commit_trust(r["repo"], hit.pop("full"), trusted, me)
            if ok:
                kept.append(hit)
            else:   # learn only from trusted authors: a commit from an outside PR is not evidence
                k = "follow-up commit (%s)" % why
                untrusted[k] = untrusted.get(k, 0) + 1
        if kept:
            r_subjects = [git(d, "log", "-1", "--format=%h %s", h).strip() for h in r["own"][:5]]
            followups[key] = {"own": r_subjects, "hits": kept}

    # 2b. Hand-off outcomes: a draft handed to a human instead of posted,
    #     compared with what was actually posted (or not, within DISCARD_DAYS).
    pending = st.get("handoffs") or {}
    drafts, posts, opened = {}, {}, {}
    for e in audit_events(['"review"', '"gate"', 'submit_review']):
        repo, num, ts = e.get("repo"), e.get("number"), e.get("ts", "")
        if not repo or not num or not ts:
            continue
        if not valid_repo(repo) or not str(num).isdigit():
            invalid.add(str(repo))
            continue
        key = "%s#%d" % (repo, int(num))
        step, o = step_name(e), e.get("outputs") or {}
        if e.get("event") == "step" and step == "review" and isinstance(o.get("review"), str):
            try:
                drafts[key] = json.loads(o["review"])
            except ValueError:
                pass
        elif e.get("event") == "step" and step == "gate" and o.get("handoff") is True:
            if iso(ts) > last:
                opened[key] = {"at": ts, "draft": drafts.get(key)}
        elif is_review_post(e):
            posts.setdefault(key, []).append((ts, e.get("options") or {}))
    for key, h in opened.items():
        if key not in pending or pending[key]["at"] < h["at"]:
            pending[key] = h
    outcomes, handoff_repos = [], set()
    outcome_refs, owner_outcomes = set(), set()   # every outcome is the owner's call; edits and discards correct the draft
    for key, h in sorted(pending.items()):
        if not valid_repo(key.rsplit("#", 1)[0]):
            invalid.add(key.rsplit("#", 1)[0])
            del pending[key]
            continue
        after = [p for t, p in posts.get(key, []) if t > h["at"]]
        if not after:
            if iso(h["at"]) < t_now - dt.timedelta(days=DISCARD_DAYS):
                outcomes.append("%s (handed off %s): DISCARDED — not posted within %d days" % (key, h["at"][:10], DISCARD_DAYS))
                outcome_refs.add(key)
                owner_outcomes.add(key)
                handoff_repos.add(key.split("#")[0])
                del pending[key]
            continue
        del pending[key]
        handoff_repos.add(key.split("#")[0])
        outcome_refs.add(key)
        d, posted = h.get("draft") or {}, after[0]
        dc, pc = norm_comments(d.get("comments")), norm_comments(posted.get("comments"))
        diff = []
        if (d.get("event") or "") != (posted.get("event") or ""):
            diff.append("verdict %s -> %s" % (fence.clean(d.get("event"), 40), fence.clean(posted.get("event"), 40)))
        if " ".join((d.get("body") or "").split()) != " ".join((posted.get("body") or "").split()):
            diff.append("body: %s -> %s" % (fence(d.get("body"), 200), fence(posted.get("body"), 200)))
        for k in dc.keys() - pc.keys():
            diff.append("DROPPED %s" % fence("%s:%s %s" % (k[0], k[1], dc[k]), 400))
        for k in pc.keys() - dc.keys():
            diff.append("ADDED %s" % fence("%s:%s %s" % (k[0], k[1], pc[k]), 400))
        for k in dc.keys() & pc.keys():
            if dc[k] != pc[k]:
                diff.append("REWORDED %s -> %s" % (fence("%s:%s %s" % (k[0], k[1], dc[k]), 300), fence(pc[k], 300)))
        if not d:
            outcomes.append("%s (handed off %s): posted, draft not recorded" % (key, h["at"][:10]))
        elif not diff:
            outcomes.append("%s (handed off %s): POSTED AS-IS" % (key, h["at"][:10]))
        else:
            outcomes.append("%s (handed off %s): EDITED before posting" % (key, h["at"][:10]))
            owner_outcomes.add(key)
            outcomes += ["    - " + x for x in diff]

    # 2c. OWNER CATCHES. Reviews post as the reviewer's own account, so what that account
    #     wrote on a reviewed PR by hand — after the pack's review, through the follow-up
    #     window — is the owner correcting it: the strongest miss signal there is, and
    #     the only direct one in auto mode, where nothing is handed off. Told apart from
    #     the pack's own posts by the audit log, never by author.
    posts_by_pr = pack_posts()
    review_cache, catches = {}, {}
    for key, r in sorted(tracked.items()):
        posts = posts_by_pr.get(key)
        if not posts:
            continue   # its audit records rotated away: the pack's posts can't be told apart
        first = min(p["ts"] for p in posts)
        if r.get("merged"):
            end = iso(r["merged"]) + dt.timedelta(days=WINDOW_DAYS)
        else:
            end = first + dt.timedelta(days=UNMERGED_DAYS)
        seen_dismissed = set(r.get("dismissed") or [])
        got = []
        for ts, label, text in owner_activity(r["repo"], r["pr"], me, posts, review_cache):
            if ts is None:   # a dismissal: reported once
                if text in seen_dismissed:
                    continue
                seen_dismissed.add(text)
            elif not (ts > first and ts > last and ts <= end):
                continue
            got.append("    - OWNER%s: %s" % ("" if ts is None else " (" + ts.strftime("%Y-%m-%d") + ")",
                                              fence("%s: %s" % (label, text), 900)))
        r["dismissed"] = sorted(seen_dismissed)
        if got:
            catches[key] = got

    # 3. Team comments created since the last run, on every repo calibration covers.
    repos = sorted({r["repo"] for r in tracked.values()} | handoff_repos)
    team = {}
    for repo in repos:
        for _, login, c in human_comments(repo, last, TEAM_PAGES, skip_login=me, trusted=trusted, dropped=untrusted):
            pr = int(c["pull_request_url"].rsplit("/", 1)[-1])
            team.setdefault("%s#%d" % (repo, pr), []).append({
                "by": fence.clean(login, 60), "path": c.get("path"), "body": c.get("body") or ""})

    def review_lines(key, head):
        r = tracked.get(key)
        if not r:
            return [head]
        out = ["%s (reviewed %s, %s): %s" % (head, r["at"], fence.clean(r["event"], 40), fence(r["body"], 300))]
        out += ["    - " + fence("%s:%s %s" % (c["path"], c["line"], c["body"]), 400) for c in r["comments"]]
        return out

    def followup_lines(key):
        fu = followups[key]
        out = ["    PR commits: " + fence("; ".join(fu["own"]), 600)]
        for h in fu["hits"]:
            out.append("    FOLLOW-UP %s changed %d of its lines: %s" % (h["sha"], h["lines"], fence(h["message"].replace("\n", " / "), 600)))
            out.append(fence(h["patch"], PATCH_CAP + 200, lines=True))
        return out

    out = ["# NEW REVIEWS since the last run"]
    for key in new_keys:
        out += review_lines(key, key)
        if key in followups:
            out += followup_lines(key)
    out += ["", "# HAND-OFF OUTCOMES (drafts handed to a human instead of posted, resolved since the last run)"]
    out += outcomes
    out += ["", "# NEW FOLLOW-UPS on PRs reviewed earlier (commits since the last run)"]
    for key in sorted(k for k in followups if k not in new_keys):
        out += review_lines(key, key)
        out += followup_lines(key)

    owner_lines = []
    for key in sorted(catches):
        owner_lines += review_lines(key, key)
        owner_lines += catches[key]

    team_lines, both = [], 0
    for key, cs in sorted(team.items(), key=lambda kv: ((kv[0] not in tracked), kv[0])):
        if key in tracked:
            both += 1
            team_lines += review_lines(key, key + " [ALSO REVIEWED BY THE BOT — compare]")
        else:
            team_lines.append(key)
        team_lines += ["    - TEAM %s %s" % (c["by"], fence("%s: %s" % (c["path"], c["body"]), 400)) for c in cs[:6]]

    # The next state is promoted by `record` only once the memory writes have
    # landed, so a failed calibration re-reads the same window next time.
    write_json_atomic(os.path.join(cache, "calibration.next.json"),
                      {"last_run": t_now.isoformat(), "tracked": tracked, "handoffs": pending})
    return {
        "count": len(new_keys), "flagged": len(followups), "since": last.date().isoformat(),
        "handoffs": sum(1 for o in outcomes if not o.startswith("    ")),
        "repos": repos, "reviewer": me,
        "reviews": cap_items(out, REVIEWS_CAP, "reviews"),
        "owner": cap_items(owner_lines, OWNER_CAP, "owner catches"),
        "owner_count": sum(len(v) for v in catches.values()), "owner_prs": len(catches),
        "team": cap_items(team_lines, TEAM_CAP, "team comments"),
        "team_count": sum(len(v) for v in team.values()), "team_prs": len(team), "team_both": both,
        "invalid_repos": sorted(invalid),
        # the fence token the evidence above is wrapped in, what was dropped as
        # untrusted (by reason), and which PRs carry trusted evidence: a learned
        # entry must cite one of trusted_refs, and only owner_refs (an owner catch,
        # or a hand-off the owner edited or discarded) let one that would make the
        # reviewer go easier on something be read by reviews (the pack's plan step)
        "fence": fence.token, "untrusted": untrusted, "trusted_associations": sorted(trusted),
        "trusted_refs": sorted(set(followups) | set(catches) | set(team) | set(owner_outcomes) | set(outcome_refs)),
        "owner_refs": sorted(set(catches) | set(owner_outcomes)),
        # every PR the agent is shown, with its facts, for the pack's `keys` step
        "prs": facts_for(list(new_keys) + list(followups) + list(catches) + list(team)
                         + [o.split(" ", 1)[0] for o in outcomes if not o.startswith(" ")]),
    }


# ---------------------------------------------------------------------------
# collect-backfill
# ---------------------------------------------------------------------------

MONTH_CAP = 100_000     # bytes of one month's comments handed to one agent


def parse_repos(raw):
    if isinstance(raw, list):
        items = raw
    else:
        items = re.split(r"[\s,]+", str(raw or ""))
    out = []
    for r in items:
        r = str(r).strip()
        if valid_repo(r) and r not in out:
            out.append(r)
    return out


def first_post_by_pack():
    first = None
    for e in audit_events(['submit_review']):
        if is_review_post(e):
            ts = iso(e["ts"])
            first = ts if first is None or ts < first else first
    return first


def collect_backfill(ctx):
    repos = parse_repos((ctx.get("inputs") or {}).get("repos") or os.environ.get("BACKFILL_REPOS"))
    if not repos:
        raise SystemExit("review-backfill: name the repos — conductor run <instance>/review-backfill --input repos=owner/a,owner/b")
    raw_days = (os.environ.get("BACKFILL_DAYS") or "").strip().lower()
    whole = raw_days == "all"
    if not whole and raw_days and not raw_days.isdigit():
        raise SystemExit("review-backfill: --input days= takes a number of days or `all`, got %r" % raw_days)
    days = int(raw_days) if raw_days.isdigit() else 365
    max_pages = env_int("MAX_PAGES", 300)
    min_chars = env_int("MIN_CHARS", 40)
    trusted = trusted_associations()
    me = gh_login()
    fence, untrusted = Fence(), {}
    since_raw = (os.environ.get("REVIEWER_SINCE") or "").strip()
    pack_since = iso(since_raw + "T00:00:00+00:00") if since_raw else first_post_by_pack()
    # Your own comments from the pack's era are yours unless the pack posted them: the
    # audit log names every review it posted, so only those (and their inline comments)
    # are dropped. Before the oldest audit record (rotated away) that can't be told, so
    # pack-era comments there are dropped as before.
    posts_by_pr, review_cache, start = pack_posts(), {}, audit_start()
    t_now = now()
    since = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) if whole else t_now - dt.timedelta(days=days)
    months, counts, truncated, pack_dropped = {}, {}, [], 0
    for repo in repos:
        for ts, login, c in human_comments(repo, since, max_pages, truncated=truncated, trusted=trusted, dropped=untrusted, owner=me):
            if login == me and pack_since and ts >= pack_since:
                pr = int(c["pull_request_url"].rsplit("/", 1)[-1])
                posts = posts_by_pr.get("%s#%d" % (repo, pr))
                if posts:
                    ids, _ = pack_review_ids(repo, pr, me, posts, review_cache)
                    if c.get("pull_request_review_id") in ids or norm(c.get("body")) in set().union(*[p["comments"] for p in posts]):
                        pack_dropped += 1
                        continue   # posted by this pack under your name
                elif start is None or ts < start:
                    pack_dropped += 1
                    continue       # the audit log no longer covers it: can't tell
            body = " ".join((c.get("body") or "").split())
            if len(body) < min_chars:
                continue
            who = "MINE" if login == me else "TEAM " + fence.clean(login, 60)
            m = ts.strftime("%Y-%m")
            pr = c["pull_request_url"].rsplit("/", 1)[-1]
            months.setdefault(m, {}).setdefault("%s#%s" % (repo, pr), []).append(
                "    - %s %s" % (who, fence("%s: %s" % (c.get("path"), body), 500)))
            k = counts.setdefault(m, {"mine": 0, "team": 0})
            k["mine" if login == me else "team"] += 1
    out = []
    for m in sorted(months):
        items = [line for key in sorted(months[m]) for line in [key] + months[m][key]]
        out.append({"month": m, "text": cap_items(items, MONTH_CAP, "month"), "mine": counts[m]["mine"], "team": counts[m]["team"]})
    if truncated:
        sys.stderr.write("pr-review-team: review-backfill read only %d pages for %s — older comments were NOT read. "
                         "Raise the backfill_max_pages setting to read the whole window.\n" % (max_pages, ", ".join(truncated)))
    return {"months": out, "month_count": len(out), "repos": repos, "reviewer": me,
            "since": "all" if whole else since.date().isoformat(),
            "mine": sum(o["mine"] for o in out), "team": sum(o["team"] for o in out),
            "truncated": truncated, "max_pages": max_pages, "pack_dropped": pack_dropped,
            # see collect-calibration: every comment kept here is from a trusted author, so
            # every PR it names is trusted evidence; none is an owner catch (backfill reads
            # comments, not corrections of the pack's reviews)
            "fence": fence.token, "untrusted": untrusted, "trusted_associations": sorted(trusted),
            "trusted_refs": sorted({k for m in months.values() for k in m}), "owner_refs": [],
            "prs": facts_for([k for m in months.values() for k in m])}


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------

# ---- review-eval: memory on vs memory off ----

EVAL_COUNT = 5
EVAL_MAX = 50
EVAL_TARGET = 0.9   # memory-on should keep at least this share of the baseline findings
REF_RE = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)")


def eval_pick(ctx):
    """Which PRs review-eval replays: the ones named (prs=), else the most recent
    `count` this pack reviewed in `repos` (the audit log's submit_review records).
    Each becomes two runs, memory off then on."""
    inputs = ctx.get("inputs") or {}
    repos = parse_repos(inputs.get("repos") or "")
    raw_count = str(inputs.get("count") or EVAL_COUNT).strip()
    if not raw_count.isdigit() or not 0 < int(raw_count) <= EVAL_MAX:
        raise SystemExit("review-eval: --input count= takes a number from 1 to %d, got %r" % (EVAL_MAX, raw_count))
    count = int(raw_count)
    named = [x for x in re.split(r"[\s,]+", str(inputs.get("prs") or "")) if x]
    refs = []
    if named:
        for x in named:
            m = REF_RE.fullmatch(x)
            if not m or not valid_repo(m.group(1)) or int(m.group(2)) <= 0:
                raise SystemExit("review-eval: --input prs= takes owner/name#N refs, got %r" % x)
            if repos and m.group(1) not in repos:
                raise SystemExit("review-eval: %s is not in --input repos=%s" % (x, ",".join(repos)))
            ref = "%s#%d" % (m.group(1), int(m.group(2)))
            if ref not in refs:
                refs.append(ref)
    else:
        if not repos:
            raise SystemExit("review-eval: name the repos (--input repos=owner/app) or the PRs (--input prs=owner/app#12)")
        require_audit()
        latest = sorted(((max(p["ts"] for p in posts), key) for key, posts in pack_posts().items()
                         if key.rsplit("#", 1)[0] in repos), reverse=True)
        refs = [key for _, key in latest[:count]]
        if not refs:
            raise SystemExit("review-eval: the audit log names no review this pack posted in %s — "
                             "name PRs with --input prs=" % ",".join(repos))
    runs = []
    for ref in refs:
        repo, n = ref.rsplit("#", 1)
        runs += [{"repo": repo, "pr": int(n), "memory": "off"}, {"repo": repo, "pr": int(n), "memory": "on"}]
    return {"prs": refs, "runs": runs, "count": len(refs), "scope_key": str(inputs.get("scope_key") or "").strip()}


def eval_dir():
    d = os.path.join(cache_dir(), "eval")
    os.makedirs(d, exist_ok=True)
    return d


def eval_summary(t, scope_key):
    pct = "%d%%" % round(100 * t.get("retention", 1))
    return ("review-eval: %d PR(s), %d learned note(s) read; baseline %d finding(s), %d retained with memory (%s), "
            "%d dropped, %d added (%d citing a note) — %s%s" % (
                t.get("prs", 0), t.get("notes", 0), t.get("baseline", 0), t.get("retained", 0), pct,
                t.get("dropped", 0), t.get("added", 0), t.get("added_citing_note", 0),
                {"ok": "notes steer, the review stands",
                 "watch": "some baseline findings lost — watch",
                 "crowding": "many baseline findings lost: the notes are crowding out the review"}.get(t.get("verdict"), "?"),
                (" [scope_key %s]" % scope_key) if scope_key else ""))


def eval_report(ctx):
    """Write <cache>/eval/<date>.md and latest.json; return the summary."""
    cmp = ctx.get("compare") or {}
    totals, prs = cmp.get("totals") or {}, cmp.get("prs") or []
    scope_key = (ctx.get("pick") or {}).get("scope_key") or ""
    summary = eval_summary(totals, scope_key)
    t = now()
    d = eval_dir()
    path = os.path.join(d, t.strftime("%Y-%m-%d") + ".md")
    n = 2
    while os.path.exists(path):
        path = os.path.join(d, "%s-%d.md" % (t.strftime("%Y-%m-%d"), n))
        n += 1
    fmt = lambda f: "%s:%s — %s" % (f.get("path"), f.get("line"), " ".join(str(f.get("body") or "").split())[:300])
    lines = ["# review-eval — %s" % t.strftime("%Y-%m-%d %H:%M UTC"), "",
             "Each PR was reviewed twice by the lens panel — memory off (the baseline), then memory on — "
             "with nothing posted and nothing written to memory.", "",
             "Target: memory-on retains nearly all baseline findings (%d%% or more). A large drop means the "
             "learned notes are crowding out the review. Model runs vary, so read a handful of PRs, "
             "not one." % round(100 * EVAL_TARGET), "",
             "## Overall", "", summary, "",
             "| | |", "|---|---|",
             "| PRs | %d |" % totals.get("prs", 0),
             "| learned notes read (memory on) | %d |" % totals.get("notes", 0),
             "| baseline findings (memory off) | %d |" % totals.get("baseline", 0),
             "| findings with memory | %d |" % totals.get("with_memory", 0),
             "| retained | %d (%d%%) |" % (totals.get("retained", 0), round(100 * totals.get("retention", 1))),
             "| dropped with memory | %d |" % totals.get("dropped", 0),
             "| added with memory | %d (%d citing a note) |" % (totals.get("added", 0), totals.get("added_citing_note", 0)),
             "| scope_key | %s |" % (scope_key or "(the setting)"), ""]
    for pr in prs:
        lines += ["## %s" % pr.get("ref"), "",
                  "%d note(s) read; baseline %d, with memory %d; retained %d (%d%%), dropped %d, added %d." % (
                      pr.get("notes", 0), pr.get("baseline", 0), pr.get("with_memory", 0), len(pr.get("retained") or []),
                      round(100 * pr.get("retention", 1)), len(pr.get("dropped") or []), len(pr.get("added") or [])), ""]
        if pr.get("dropped"):
            lines.append("Dropped with memory (the baseline raised these; memory-on did not):")
            lines += ["- " + fmt(f) for f in pr["dropped"]]
        if pr.get("added"):
            lines.append("Added with memory:")
            lines += ["- " + fmt(f) + ((" [note: %s]" % " ".join(str(f["note"]).split())[:120]) if f.get("note") else "")
                      for f in pr["added"]]
        if pr.get("retained"):
            lines.append("Retained:")
            lines += ["- %s -> %s (p=%.2f) — %s" % (r.get("base"), r.get("memory"), r.get("p", 0),
                                                      " ".join(str(r.get("body") or "").split())[:200]) for r in pr["retained"]]
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    write_json_atomic(os.path.join(d, "latest.json"), {"ts": iso_str(t), "summary": summary, "path": path, "totals": totals})
    print(summary, file=sys.stderr)
    return {"summary": summary, "path": path, "totals": totals}


def shell_quote(v):
    return "'" + str(v or "").replace("'", "'\\''") + "'"


def iso_str(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_eval_since(last_run):
    """The latest review-eval summary, when one ran after `last_run` (or ever, with none)."""
    try:
        with open(os.path.join(cache_dir(), "eval", "latest.json")) as f:
            e = json.load(f)
        ts = iso(e["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if last_run is not None and ts <= last_run:
        return None
    return e


def record(ctx):
    """Promote calibration's next state (calibration mode) and append the run's
    section to <cache>/calibration-log.md."""
    mode = os.environ.get("MODE") or "calibration"
    cache = cache_dir()
    plan = ctx.get("plan") or {}
    written = ((ctx.get("write") or {}).get("items")) or []
    lines = ["", "## %s — review %s" % (now().strftime("%Y-%m-%d %H:%M UTC"), mode)]
    writes = plan.get("writes") or []
    held = [(i, w) for i, w in enumerate(writes) if w.get("quarantined")]
    lines.append("Written (%d):" % (len(writes) - len(held)))
    for i, w in enumerate(writes):
        if w.get("quarantined"):
            continue
        got = written[i] if i < len(written) and isinstance(written[i], dict) else {}
        lines.append("- %s [%s] %s" % (got.get("id") or "?", w.get("label") or w.get("scope") or "global", w.get("text", "")))
    if held:
        # an entry that would make reviews go easier on something, with no owner
        # catch behind it: written where no review reads it, for the owner to judge
        lines.append("QUARANTINED (%d) — each would tell reviewers to go easier on something (approve, skip, ignore, "
                     "trust, lower severity …) and no owner catch backs it, so it was written to quarantine:<key>, "
                     "which no review reads. Read the exact text; to use one, move it by hand:" % len(held))
        for i, w in held:
            got = written[i] if i < len(written) and isinstance(written[i], dict) else {}
            lines.append("- %s [%s, would be %s] %r" % (got.get("id") or "?", w.get("scope"), w.get("target"), w.get("text", "")))
            lines.append("    conductor memory rescope --scope %s --tag %s --to %s --apply" % (
                shell_quote(w.get("scope")), shell_quote(w.get("hold_tag")), shell_quote(w.get("target_scope") or "global")))
    collect = ctx.get("collect") or {}
    untrusted = collect.get("untrusted") or {}
    if untrusted:
        lines.append("Untrusted, not read (%d) — only %s count: %s" % (
            sum(untrusted.values()), ", ".join(collect.get("trusted_associations") or []) or "your own account",
            ", ".join("%s %d" % (k, v) for k, v in sorted(untrusted.items()))))
    injections = plan.get("injections") if "injections" in plan else ((ctx.get("patterns") or {}).get("injections") or [])
    if injections:
        lines.append("Instructions found inside the evidence (%d) — reported, not learned:" % len(injections))
        lines += ["- %r" % str(x) for x in injections]
    archived = ((ctx.get("archive") or {}).get("items")) or []
    if archived:
        lines.append("Archived (fresh train) (%d):" % len(archived))
        lines += ["- %s [%s] -> %s [archive:%s] %s" % (a.get("id"), a.get("scope"), a.get("to"), a.get("scope"), a.get("text", ""))
                  for a in archived]
    trunc = ((ctx.get("collect") or {}).get("truncated")) or []
    if trunc:
        lines.append("TRUNCATED by backfill_max_pages (older comments not read): " + ", ".join(trunc))
    for key, title in (("downgraded", "Downgraded (global resting on one key)"), ("superseded", "Superseded"),
                       ("rejected", "Rejected"), ("dropped", "Dropped")):
        items = plan.get(key) or []
        if items:
            lines.append("%s (%d):" % (title, len(items)))
            lines += ["- " + str(x) for x in items]
    summary = (ctx.get("calibrate") or ctx.get("merge") or {}).get("summary") or ""
    if summary:
        lines += ["", summary.strip()]
    if mode == "calibration":
        # A review-eval since the last calibration: its summary goes in the log
        # beside what was learned (a note to read — it gates nothing).
        try:
            with open(os.path.join(cache, "calibration.json")) as f:
                last = iso(json.load(f).get("last_run"))
        except (OSError, ValueError, TypeError, AttributeError):
            last = None
        ev = latest_eval_since(last)
        if ev:
            lines += ["", "Latest review-eval (%s): %s (report: %s)" % (ev.get("ts"), ev.get("summary"), ev.get("path"))]
    with open(os.path.join(cache, "calibration-log.md"), "a") as f:
        f.write("\n".join(lines) + "\n")
    promoted = False
    if mode == "calibration":
        nxt = os.path.join(cache, "calibration.next.json")
        if os.path.exists(nxt):
            os.replace(nxt, os.path.join(cache, "calibration.json"))
            promoted = True
    return {"written": len(writes) - len(held), "quarantined": len(held), "rejected": len(plan.get("rejected") or []),
            "downgraded": len(plan.get("downgraded") or []),
            "archived": len(archived), "truncated": trunc,
            "dropped": len(plan.get("dropped") or []), "promoted": promoted,
            "log": os.path.join(cache, "calibration-log.md")}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        ctx = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except ValueError:
        ctx = {}
    try:
        if cmd == "collect-calibration":
            out = collect_calibration()
        elif cmd == "collect-backfill":
            out = collect_backfill(ctx)
        elif cmd == "record":
            out = record(ctx)
        elif cmd == "eval-pick":
            out = eval_pick(ctx)
        elif cmd == "eval-report":
            out = eval_report(ctx)
        else:
            raise SystemExit("usage: calibration.py collect-calibration | collect-backfill | record | eval-pick | eval-report")
    except NeedsGrant as e:
        print("pr-review-team: " + str(e), file=sys.stderr)
        return 3
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
