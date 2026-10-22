"""A small, local iCalendar (.ics) reader for the Today page.

Reads exports from Apple Calendar, Outlook and Google: time zones, all-day events, recurring
events (daily / weekly / monthly / yearly, with INTERVAL, COUNT, UNTIL, BYDAY, BYMONTHDAY,
BYMONTH), cancelled or moved instances (EXDATE, RECURRENCE-ID) and cancelled events.
Nothing is sent anywhere: this is plain parsing. Stdlib only.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 has no zoneinfo; times are then read as local
    ZoneInfo = None

DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
JOIN = re.compile(r"https://(?:teams\.microsoft\.com/l/meetup-join|[\w.-]*zoom\.us/j|meet\.google\.com|[\w.-]*webex\.com/[\w./-]*meet)[^\s<>\"'\\)]*", re.I)
MAX_STEPS = 20000


def _tz(name: str):
    if not name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name.strip('"'))
    except Exception:
        return None


def _unfold(text: str) -> list:
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def _prop(line: str):
    """'DTSTART;TZID=Europe/Berlin:20261022T090000' -> ('DTSTART', {'TZID': 'Europe/Berlin'}, '2026...')."""
    m = re.match(r'([A-Za-z0-9-]+)((?:;[^:;=]+=(?:"[^"]*"|[^:;]*))*):(.*)', line)
    if not m:
        return None
    params = {}
    for p in re.findall(r';([^=;:]+)=("[^"]*"|[^;:]*)', m.group(2)):
        params[p[0].upper()] = p[1].strip('"')
    return m.group(1).upper(), params, m.group(3)


def _text(v: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


def _dt(value: str, params: dict):
    """Returns (datetime or date, all_day). Datetimes are timezone-aware (local time when floating)."""
    v = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        return date(int(v[:4]), int(v[4:6]), int(v[6:8])), True
    d = datetime(int(v[:4]), int(v[4:6]), int(v[6:8]), int(v[9:11]), int(v[11:13]), int(v[13:15] or 0))
    if v.endswith("Z"):
        return d.replace(tzinfo=timezone.utc), False
    tz = _tz(params.get("TZID", ""))
    return (d.replace(tzinfo=tz) if tz else d.astimezone()), False


def _duration(v: str) -> timedelta:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    if not m:
        return timedelta(hours=1)
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def parse(text: str) -> list:
    """Every VEVENT as a dict. Alarms, time-zone definitions and other components are skipped."""
    events, cur, depth = [], None, []
    for line in _unfold(text):
        if line.startswith("BEGIN:"):
            depth.append(line[6:].strip().upper())
            if depth[-1] == "VEVENT" and len(depth) >= 1:
                cur = {"exdates": [], "attendees": 0}
            continue
        if line.startswith("END:"):
            name = depth.pop() if depth else ""
            if name == "VEVENT" and cur is not None:
                if "start" in cur:
                    events.append(cur)
                cur = None
            continue
        if cur is None or (depth and depth[-1] != "VEVENT"):
            continue  # inside VALARM etc.
        p = _prop(line)
        if not p:
            continue
        name, params, value = p
        try:
            if name == "DTSTART":
                cur["start"], cur["all_day"] = _dt(value, params)
            elif name == "DTEND":
                cur["end"], _ = _dt(value, params)
            elif name == "DURATION":
                cur["duration"] = _duration(value)
            elif name == "SUMMARY":
                cur["title"] = _text(value).strip()
            elif name == "LOCATION":
                cur["location"] = _text(value).strip()
            elif name == "DESCRIPTION":
                cur["description"] = _text(value)
            elif name == "URL":
                cur["url"] = value.strip()
            elif name == "UID":
                cur["uid"] = value.strip()
            elif name == "STATUS":
                cur["status"] = value.strip().upper()
            elif name == "TRANSP":
                cur["free"] = value.strip().upper() == "TRANSPARENT"
            elif name == "RRULE":
                cur["rrule"] = {k.upper(): v for k, v in (x.split("=", 1) for x in value.split(";") if "=" in x)}
            elif name == "EXDATE":
                for x in value.split(","):
                    cur["exdates"].append(_dt(x, params)[0])
            elif name == "RECURRENCE-ID":
                cur["recurrence_id"], _ = _dt(value, params)
            elif name == "ATTENDEE":
                cur["attendees"] += 1
            elif name == "ORGANIZER":
                cur["organizer"] = params.get("CN", value.replace("mailto:", "")).strip()
        except (ValueError, IndexError):
            continue  # one malformed line shouldn't lose the whole calendar
    return events


def _length(ev: dict) -> timedelta:
    if "end" in ev:
        a, b = ev["start"], ev["end"]
        if isinstance(a, datetime) and isinstance(b, datetime):
            return b - a
        if not isinstance(a, datetime) and not isinstance(b, datetime):
            return b - a
    return ev.get("duration") or (timedelta(days=1) if ev.get("all_day") else timedelta(hours=1))


def _nth_weekdays(year: int, month: int, spec: str) -> list:
    """BYDAY entry within a month: 'MO' (every Monday), '2TU' (second Tuesday), '-1FR' (last Friday)."""
    m = re.fullmatch(r"([+-]?\d+)?([A-Z]{2})", spec)
    if not m or m.group(2) not in DAYS:
        return []
    wd = DAYS.index(m.group(2))
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    days = [first + timedelta(d) for d in range((nxt - first).days) if (first + timedelta(d)).weekday() == wd]
    if not m.group(1):
        return days
    n = int(m.group(1))
    return [days[n - 1]] if 0 < n <= len(days) else [days[n]] if -len(days) <= n < 0 else []


def _month_days(year: int, month: int, rule: dict, start_day: int) -> list:
    out = []
    if "BYMONTHDAY" in rule:
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        last = (nxt - timedelta(days=1)).day
        for x in rule["BYMONTHDAY"].split(","):
            n = int(x)
            n = last + n + 1 if n < 0 else n
            if 1 <= n <= last:
                out.append(date(year, month, n))
    elif "BYDAY" in rule:
        for spec in rule["BYDAY"].split(","):
            out += _nth_weekdays(year, month, spec.strip())
    else:
        try:
            out.append(date(year, month, start_day))
        except ValueError:
            pass  # e.g. the 31st in a 30-day month: skipped, as RFC 5545 says
    if "BYSETPOS" in rule and out:
        out.sort()
        out = [out[int(p) - 1 if int(p) > 0 else int(p)] for p in rule["BYSETPOS"].split(",") if -len(out) <= int(p) <= len(out) and int(p)]
    return sorted(set(out))


def _dates(ev: dict, until_day: date):
    """Yields the dates of a recurring event's occurrences, in order, from its first."""
    r, s = ev["rrule"], ev["start"]
    sd = s.date() if isinstance(s, datetime) else s
    freq, step = r.get("FREQ", "DAILY"), max(1, int(r.get("INTERVAL", "1") or 1))
    months = [int(x) for x in r["BYMONTH"].split(",")] if "BYMONTH" in r else None
    for i in range(MAX_STEPS):
        if freq == "DAILY":
            d = sd + timedelta(days=i * step)
            if d > until_day:
                return
            if (not months or d.month in months) and ("BYDAY" not in r or DAYS[d.weekday()] in r["BYDAY"]):
                yield d
        elif freq == "WEEKLY":
            week = sd - timedelta(days=sd.weekday()) + timedelta(weeks=i * step)
            if week > until_day:
                return
            wds = [DAYS.index(x.strip()[-2:]) for x in r["BYDAY"].split(",")] if "BYDAY" in r else [sd.weekday()]
            for wd in sorted(set(wds)):
                d = week + timedelta(days=wd)
                if d >= sd and (not months or d.month in months):
                    yield d
        elif freq in ("MONTHLY", "YEARLY"):
            if freq == "MONTHLY":
                k = sd.month - 1 + i * step
                y, mlist = sd.year + k // 12, [k % 12 + 1]
                if months and mlist[0] not in months:
                    continue
            else:
                y, mlist = sd.year + i * step, (months or [sd.month])
            if date(y, mlist[0], 1) > until_day:
                return
            for mo in mlist:
                for d in _month_days(y, mo, r, sd.day):
                    if d >= sd:
                        yield d
        else:
            return


def occurrences(events: list, start: date, end: date) -> list:
    """Every occurrence that overlaps [start, end), as a flat list sorted by start (local time)."""
    # .astimezone() with no argument uses the system zone's offset *on that date*, so meetings
    # after a clock change land at the right local time
    win_a = datetime.combine(start, time()).astimezone()
    win_b = datetime.combine(end, time()).astimezone()
    moved = {}  # (uid, original start) -> True for instances replaced by a RECURRENCE-ID event
    for ev in events:
        if "recurrence_id" in ev and ev.get("uid"):
            moved[(ev["uid"], _key(ev["recurrence_id"]))] = True
    out = []

    def add(ev, s, length):
        if isinstance(s, datetime):
            a = s.astimezone()
            b = (s + length).astimezone()  # wall-clock length in the event's own zone
        else:
            a = datetime.combine(s, time()).astimezone()
            b = datetime.combine(s + length, time()).astimezone()
        if b > win_a and a < win_b:
            out.append((a, b, ev))

    for ev in events:
        if ev.get("status") == "CANCELLED":
            continue
        length = _length(ev)
        if "rrule" not in ev or "recurrence_id" in ev:
            add(ev, ev["start"], length)
            continue
        r = ev["rrule"]
        until = _dt(r["UNTIL"], {})[0] if "UNTIL" in r else None
        count = int(r["COUNT"]) if r.get("COUNT", "").isdigit() else None
        ex = {_key(x) for x in ev["exdates"]}
        s0 = ev["start"]
        n = 0
        for d in _dates(ev, end + timedelta(days=1)):
            if isinstance(s0, datetime):
                inst = datetime.combine(d, s0.timetz().replace(tzinfo=None), s0.tzinfo)  # same wall-clock time, DST-correct
            else:
                inst = d
            if until is not None and _cmp(inst) > _cmp(until):
                break
            n += 1
            if count is not None and n > count:
                break
            if _key(inst) in ex or (ev.get("uid"), _key(inst)) in moved:
                continue
            add(ev, inst, length)
    out.sort(key=lambda x: (x[0], x[2].get("title", "")))
    return [_public(a, b, ev) for a, b, ev in out]


def _key(v) -> str:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return v.strftime("%Y%m%d")


def _cmp(v) -> datetime:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc)
    return datetime.combine(v, time(23, 59, 59), timezone.utc)


def _public(a: datetime, b: datetime, ev: dict) -> dict:
    blob = " ".join(str(ev.get(k, "")) for k in ("url", "location", "description"))
    m = JOIN.search(blob)
    return {"uid": ev.get("uid", ""), "title": ev.get("title") or "(no title)", "start": a.strftime("%Y-%m-%dT%H:%M"),
            "end": b.strftime("%Y-%m-%dT%H:%M"), "all_day": bool(ev.get("all_day")), "location": (ev.get("location") or "")[:160],
            "join": m.group(0) if m else "", "attendees": ev.get("attendees", 0), "organizer": (ev.get("organizer") or "")[:80],
            "free": bool(ev.get("free"))}


_CACHE: dict = {}


def load(path) -> list:
    """Parsed events for a file, re-read only when it changes."""
    p = Path(path).expanduser()
    st = p.stat()
    key = str(p)
    if key in _CACHE and _CACHE[key][0] == (st.st_mtime, st.st_size):
        return _CACHE[key][1]
    events = parse(p.read_text(errors="replace"))
    _CACHE[key] = ((st.st_mtime, st.st_size), events)
    return events
