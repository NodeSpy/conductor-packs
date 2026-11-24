#!/usr/bin/env python3
"""A small fake of the Google Calendar API v3 — enough for calendar-busy-sync's
e2e: events list (singleEvents expansion of WEEKLY/DAILY rules with UNTIL and
EXDATE, showDeleted, paging), insert (client-chosen ids), patch, delete, plus
an OAuth token endpoint and a /_fake control API.

Every connector's bearer token names its calendar ("tok-<calendar>"), so one
server holds several accounts' calendars.

    python3 gcalfake.py PORT
    GET  /_fake/state                 every calendar's raw events
    POST /_fake/seed   {cal: [events]} replace calendars
    POST /_fake/put    {cal, event}    add or replace one event
    POST /_fake/del    {cal, id}       remove one event outright
    GET  /_fake/log                    every write request, in order
    GET  /_fake/channels               open watch channels, by calendar
    POST /_fake/push   {cal, listen}   post a change notification for cal's
                                       channel to http://<listen><path of its address>
"""
import json
import re
import urllib.request
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.Lock()
CALS = {}    # cal -> {id: event}
LOG = []     # write requests
CHANNELS = {}  # cal -> the open watch channel {id, token, address, resourceId}


def parse_dt(t):
    if "dateTime" in t:
        return datetime.fromisoformat(t["dateTime"].replace("Z", "+00:00"))
    return datetime.fromisoformat(t["date"] + "T00:00:00+00:00")


def iso(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(d):
    return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def expand(master, lo, hi):
    """Occurrences of a WEEKLY/DAILY master inside [lo, hi)."""
    rule, exdates = {}, set()
    for line in master.get("recurrence", []):
        if line.startswith("RRULE:"):
            rule = dict(p.split("=", 1) for p in line[6:].split(";") if "=" in p)
        elif line.startswith("EXDATE"):
            for v in line.split(":", 1)[1].split(","):
                exdates.add(v.strip())
    freq = {"WEEKLY": timedelta(days=7), "DAILY": timedelta(days=1)}.get(rule.get("FREQ"))
    if not freq:
        return []
    allday = "date" in master["start"]
    start, end = parse_dt(master["start"]), parse_dt(master["end"])
    dur = end - start
    until = None
    if "UNTIL" in rule:
        u = rule["UNTIL"]
        until = datetime.strptime(u, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) if "T" in u else \
            datetime.strptime(u, "%Y%m%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    count = int(rule["COUNT"]) if "COUNT" in rule else None
    out, cur, n = [], start, 0
    while cur < hi and n < 2000:
        if until and cur > until:
            break
        if count is not None and n >= count:
            break
        n += 1
        key = cur.strftime("%Y%m%d") if allday else stamp(cur)
        if key not in exdates and cur + dur > lo:
            occ = {k: v for k, v in master.items() if k not in ("recurrence", "id")}
            occ["id"] = master["id"] + "_" + key
            occ["recurringEventId"] = master["id"]
            if allday:
                occ["start"] = {"date": cur.strftime("%Y-%m-%d")}
                occ["end"] = {"date": (cur + dur).strftime("%Y-%m-%d")}
                occ["originalStartTime"] = {"date": cur.strftime("%Y-%m-%d")}
            else:
                tz = master["start"].get("timeZone")
                occ["start"] = {"dateTime": iso(cur), **({"timeZone": tz} if tz else {})}
                occ["end"] = {"dateTime": iso(cur + dur), **({"timeZone": tz} if tz else {})}
                occ["originalStartTime"] = {"dateTime": iso(cur), **({"timeZone": tz} if tz else {})}
            out.append(occ)
        cur += freq
    return out


def overlaps(e, lo, hi):
    return parse_dt(e["start"]) < hi and parse_dt(e["end"]) > lo


def listing(cal, q):
    lo = datetime.fromisoformat(q.get("timeMin", ["1970-01-01T00:00:00Z"])[0].replace("Z", "+00:00"))
    hi = datetime.fromisoformat(q.get("timeMax", ["2100-01-01T00:00:00Z"])[0].replace("Z", "+00:00"))
    single = q.get("singleEvents", ["false"])[0] == "true"
    deleted = q.get("showDeleted", ["false"])[0] == "true"
    evs = list(CALS.get(cal, {}).values())
    out = []
    # exceptions (modified/cancelled instances) keyed by master+original start
    exc = {}
    for e in evs:
        if e.get("recurringEventId"):
            exc[(e["recurringEventId"], json.dumps(e.get("originalStartTime"), sort_keys=True))] = e
    for e in evs:
        if e.get("recurringEventId"):
            if not single:
                if e.get("status") == "cancelled" or overlaps(e, lo, hi):
                    out.append(e)
            continue
        if e.get("recurrence"):
            if single:
                if e.get("status") == "cancelled":
                    continue
                for o in expand(e, lo, hi):
                    x = exc.get((e["id"], json.dumps(o["originalStartTime"], sort_keys=True)))
                    if x is None:
                        out.append(o)
                    elif x.get("status") == "cancelled":
                        if deleted:
                            out.append(x)
                    elif overlaps(x, lo, hi):
                        out.append(x)
                # moved-in exceptions whose original start is outside
            else:
                if e.get("status") == "cancelled" and not deleted:
                    continue
                if expand(e, lo, hi) or e.get("status") == "cancelled":
                    out.append(e)
            continue
        if e.get("status") == "cancelled":
            if deleted:
                out.append(e)
            continue
        if overlaps(e, lo, hi):
            out.append(e)
    out.sort(key=lambda e: (parse_dt(e["start"]) if e.get("start") else lo, e["id"]))
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        return raw, (json.loads(raw) if raw and raw[:1] in (b"{", b"[") else None)

    def cal(self):
        a = self.headers.get("authorization", "")
        m = re.match(r"Bearer tok-(.+)$", a)
        return m.group(1) if m else None

    def route(self, method):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        raw, j = self.body()
        p = u.path
        if p == "/token":
            form = parse_qs(raw.decode())
            rt = form.get("refresh_token", [""])[0]   # "rt-<cal>"
            return self.send(200, {"access_token": "tok-" + rt[3:], "expires_in": 3600})
        if p.startswith("/_fake/"):
            with LOCK:
                if p == "/_fake/state":
                    return self.send(200, CALS)
                if p == "/_fake/log":
                    return self.send(200, LOG)
                if p == "/_fake/channels":
                    return self.send(200, CHANNELS)
                if p == "/_fake/seed":
                    CALS.clear()
                    for c, evs in j.items():
                        CALS[c] = {e["id"]: e for e in evs}
                    LOG.clear()
                    return self.send(200, {})
                if p == "/_fake/put":
                    CALS.setdefault(j["cal"], {})[j["event"]["id"]] = j["event"]
                    return self.send(200, {})
                if p == "/_fake/del":
                    CALS.get(j["cal"], {}).pop(j["id"], None)
                    return self.send(200, {})
            if p == "/_fake/push":
                ch = CHANNELS.get(j["cal"])
                if not ch:
                    return self.send(404, {"error": "no channel"})
                path = urlparse(ch["address"]).path or "/"
                req = urllib.request.Request("http://" + j["listen"] + path, data=b"", method="POST", headers={
                    "X-Goog-Channel-Id": ch["id"], "X-Goog-Channel-Token": ch["token"],
                    "X-Goog-Resource-State": "exists", "X-Goog-Message-Number": "2",
                    "X-Goog-Resource-Id": ch["resourceId"]})
                with urllib.request.urlopen(req, timeout=5) as r:
                    return self.send(200, {"status": r.status})
            return self.send(404, {})
        cal = self.cal()
        if p == "/calendar/v3/channels/stop":
            with LOCK:
                for c, ch in list(CHANNELS.items()):
                    if ch["id"] == (j or {}).get("id"):
                        del CHANNELS[c]
            self.send_response(204)
            self.end_headers()
            return
        if not cal:
            return self.send(401, {"error": {"code": 401, "message": "no token"}})
        if re.match(r"^/calendar/v3/calendars/[^/]+/events/watch$", p) and method == "POST":
            with LOCK:
                CHANNELS[cal] = {"id": j["id"], "token": j.get("token", ""), "address": j["address"],
                                 "resourceId": "res-" + cal}
            exp = str(int((datetime.now(timezone.utc) + timedelta(days=7)).timestamp() * 1000))
            return self.send(200, {"kind": "api#channel", "id": j["id"], "resourceId": "res-" + cal, "expiration": exp})
        m = re.match(r"^/calendar/v3/calendars/([^/]+)/events(?:/([^/]+))?$", p)
        if not m:
            return self.send(404, {"error": {"code": 404, "message": p}})
        with LOCK:
            evs = CALS.setdefault(cal, {})
            eid = m.group(2)
            if method == "GET" and not eid:
                items = listing(cal, q)
                size = int(q.get("maxResults", ["250"])[0])
                size = min(size, 7)   # force paging
                start = int(q.get("pageToken", ["0"])[0])
                page = {"items": items[start:start + size]}
                if start + size < len(items):
                    page["nextPageToken"] = str(start + size)
                return self.send(200, page)
            if method == "POST" and not eid:
                LOG.append({"cal": cal, "op": "insert", "event": j})
                e = dict(j)
                e.setdefault("id", uuid.uuid4().hex)
                if e["id"] in evs and evs[e["id"]].get("status") != "cancelled":
                    return self.send(409, {"error": {"code": 409, "message": "duplicate"}})
                e["status"] = "confirmed"
                e.setdefault("iCalUID", e["id"] + "@google.com")
                evs[e["id"]] = e
                return self.send(200, e)
            if eid not in evs:
                return self.send(404, {"error": {"code": 404, "message": "not found"}})
            if method == "PATCH":
                LOG.append({"cal": cal, "op": "patch", "id": eid, "event": j})
                evs[eid].update(j)
                return self.send(200, evs[eid])
            if method == "DELETE":
                LOG.append({"cal": cal, "op": "delete", "id": eid})
                if evs[eid].get("status") == "cancelled":
                    return self.send(410, {"error": {"code": 410, "message": "gone"}})
                evs[eid]["status"] = "cancelled"
                self.send_response(204)
                self.end_headers()
                return
        return self.send(405, {})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PATCH(self):
        self.route("PATCH")

    def do_DELETE(self):
        self.route("DELETE")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
