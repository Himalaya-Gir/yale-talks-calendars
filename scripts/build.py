#!/usr/bin/env python3
"""Build subscribable calendar feeds for Yale talks.

* Wu Tsai Institute: wti.yale.edu publishes no iCal feed, so its event pages
  are scraped into public/wti.ics. Every event ever seen is kept in
  data/wti-events.json, so talks stay in the calendar once they are over.
* Psychology: psychology.yale.edu/events embeds public Google Calendars, which
  are subscribed to directly. This script records which ones the page embeds
  (data/psych-calendars.json) and writes watchdog.md when that list changes,
  so a new or retired calendar gets noticed.

public/ is what GitHub Pages serves: wti.ics, index.html (subscribe links)
and status.json. Standard library only, Python 3.9+.
"""

import base64
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public"
DATA = ROOT / "data"
WATCHDOG = ROOT / "watchdog.md"
TZ = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

WTI = "https://wti.yale.edu"
PSYCH_EVENTS = "https://psychology.yale.edu/events"
REPO = os.environ.get("GITHUB_REPOSITORY", "")
PAGES_URL = os.environ.get("PAGES_URL", "").rstrip("/")
USER_AGENT = "yale-talks-calendars" + (" (+https://github.com/%s)" % REPO if REPO else "")


# --- helpers ---------------------------------------------------------------

def log(level, message):
    """Print a message; on GitHub Actions it becomes an annotation."""
    if os.environ.get("GITHUB_ACTIONS"):
        print("::%s::%s" % (level, message))
    else:
        print("%s: %s" % (level, message))


def fetch(url, tries=3):
    for attempt in range(1, tries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", "replace")
        except Exception as exc:
            if attempt == tries or getattr(exc, "code", None) == 404:
                raise RuntimeError("cannot fetch %s: %s" % (url, exc))
            time.sleep(3 * attempt)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def to_text(fragment):
    """HTML fragment -> plain text, keeping paragraph breaks."""
    fragment = re.sub(r"(?is)<(script|style|svg)\b.*?</\1>", "", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?i)<li\b[^>]*>", "\n• ", fragment)
    fragment = re.sub(r"(?i)</(p|div|li|ul|ol|h[1-6]|tr|table)>", "\n\n", fragment)
    fragment = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    lines = (re.sub(r"[ \t ]+", " ", line).strip() for line in fragment.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def to_line(fragment):
    return re.sub(r"\s+", " ", to_text(fragment)).strip()


# --- dates and times as written on wti.yale.edu ----------------------------
# "Monday, October 12, 2026", "January 12, 2026", "10:00 - 11:15 am",
# "9:30 am -5:30 pm", "2:00 - 4:00 PM"; no time at all means an all-day event.

MONTH = (r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
         r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
DATE = re.compile(MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.I)
DATE_SPAN = re.compile(MONTH + r"\s+(\d{1,2})\s*[-–—]\s*(?:" + MONTH + r"\s+)?(\d{1,2}),?\s+(\d{4})", re.I)
MERIDIEM = r"\s*([ap])\.?\s?m\b\.?"
TIME_SPAN = re.compile(r"\b(\d{1,2})(?::(\d{2}))?(?:" + MERIDIEM + r")?\s*(?:-|–|—|to)\s*"
                       r"(\d{1,2})(?::(\d{2}))?" + MERIDIEM, re.I)
TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?" + MERIDIEM, re.I)


def month_number(name):
    return ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
            "dec"].index(name[:3].lower()) + 1


def parse_dates(text):
    """Return (first day, last day) or None."""
    span = DATE_SPAN.search(text)
    if span:
        year = int(span.group(5))
        first = dt.date(year, month_number(span.group(1)), int(span.group(2)))
        last = dt.date(year, month_number(span.group(3) or span.group(1)), int(span.group(4)))
        return first, max(first, last)
    days = [dt.date(int(y), month_number(m), int(d)) for m, d, y in DATE.findall(text)]
    return (days[0], max(days[0], days[-1])) if days else None


def to_minutes(hour, minute, meridiem):
    return (int(hour) % 12 + (12 if meridiem.lower() == "p" else 0)) * 60 + int(minute or 0)


def parse_times(text):
    """Return (start, end) in minutes after midnight, end possibly None; or None."""
    text = re.sub(r"(?i)\bnoon\b", "12:00 pm", text)
    span = TIME_SPAN.search(text)
    if span:
        end = to_minutes(span.group(4), span.group(5), span.group(6))
        meridiem = span.group(3) or span.group(6)
        start = to_minutes(span.group(1), span.group(2), meridiem)
        if not span.group(3) and start > end:  # "11:00 - 1:00 pm"
            start = to_minutes(span.group(1), span.group(2), "a" if meridiem.lower() == "p" else "p")
        return start, end
    single = TIME.search(text)
    if single:
        return to_minutes(single.group(1), single.group(2), single.group(3)), None
    return None


# --- Wu Tsai Institute -----------------------------------------------------

def keep_useful_links(fragment):
    """Keep the URL of links such as "Learn more", "Register" or Zoom links."""
    def replace(match):
        href, label = match.group(1), to_line(match.group(2))
        if href.startswith("/"):
            href = WTI + href
        if not href.startswith("http"):
            return match.group(2)
        if label.startswith("http"):
            return href
        if re.search(r"(?i)learn more|regist|rsvp|zoom|livestream|stream|watch|recording|details|here", label):
            return "%s: %s" % (label, href)
        return match.group(2)
    return re.sub(r'(?is)<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', replace, fragment)


def parse_wti_event(path, page):
    url = WTI + path
    heading = re.search(r"(?is)<h1\b[^>]*page-title__heading[^>]*>(.*?)</h1>", page)
    if not heading:
        raise ValueError("no title")
    title = to_line(heading.group(1))

    # The subheading holds, in order: optional series/speaker lines, the date,
    # the time, then optional notes such as "Reception to follow".
    sub = re.search(r"(?is)<h2\b[^>]*page-title__subheading[^>]*>(.*?)</h2>", page)
    paragraphs = [to_line(p) for p in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", sub.group(1))] if sub else []
    dates = times = None
    before, after = [], []
    for paragraph in filter(None, paragraphs):
        found_dates, found_times = parse_dates(paragraph), parse_times(paragraph)
        if found_dates or found_times:
            dates = dates or found_dates
            times = times or found_times
        else:
            (after if dates or times else before).append(paragraph)
    if not dates:
        raise ValueError("no date among %r" % paragraphs)

    first_day, last_day = dates
    if times:
        start_minutes, end_minutes = times
        start = dt.datetime.combine(first_day, dt.time(start_minutes // 60, start_minutes % 60), TZ)
        end = start + dt.timedelta(hours=1)
        if end_minutes is not None:
            stated_end = dt.datetime.combine(last_day, dt.time(end_minutes // 60, end_minutes % 60), TZ)
            if stated_end > start:
                end = stated_end
        start_value, end_value = start.isoformat(), end.isoformat()
    else:
        start_value, end_value = first_day.isoformat(), (last_day + dt.timedelta(days=1)).isoformat()

    address = re.search(r'(?is)<p\b[^>]*class="address"[^>]*>(.*?)</p>', page)
    lines = [to_line(x) for x in re.split(r"(?i)<br\s*/?>", address.group(1))] if address else []
    location = ", ".join(line for line in lines if line)
    if "college st" in location.lower() and "new haven" not in location.lower():
        location += ", New Haven, CT 06510"

    body = re.search(r'(?is)<div\b[^>]*class="components"[^>]*>(.*?)</article>', page)
    details = to_text(keep_useful_links(body.group(1))) if body else ""
    if len(details) > 3000:  # full conference programs; the event page has the rest
        details = details[:3000].rsplit(" ", 1)[0] + " […]"

    lead = " · ".join(before)
    if not lead:
        summary = title
    elif title.lower() in lead.lower():
        summary = lead
    else:
        summary = "%s — %s" % (lead, title)
    description = "\n".join(before + ([title] if before else []) + after)
    description = "\n\n".join(part for part in (description, details, url) if part)
    cancelled = bool(re.search(r"(?i)\bcancell?ed\b", " ".join([title] + before + after)))
    return {
        "url": url,
        "summary": summary,
        "start": start_value,
        "end": end_value,
        "location": location,
        "description": description,
        "cancelled": cancelled,
    }


def listing_links(url, max_pages):
    """Event paths listed on a wti.yale.edu listing, following its pager."""
    links, number = [], 0
    while True:
        page = fetch(url if number == 0 else "%s?page=%d" % (url, number))
        main = re.search(r"(?is)<main\b.*?</main>", page)
        found = re.findall(r'href="(?:https?://wti\.yale\.edu)?(/event/[^"#?]+)"', main.group(0) if main else page)
        new = [link for link in dict.fromkeys(found) if link not in links]
        links += new
        number += 1
        has_next = re.search(r'href="[^"]*[?&](?:amp;)?page=%d"' % number, page)
        if not new or not has_next or number >= max_pages:
            return links


def event_end(event):
    if "T" in event["end"]:
        return dt.datetime.fromisoformat(event["end"])
    return dt.datetime.combine(dt.date.fromisoformat(event["end"]), dt.time(0), TZ)


def update_wti(now):
    store_file = DATA / "wti-events.json"
    store = load_json(store_file, {})
    upcoming = listing_links(WTI + "/events", max_pages=10)
    # The first run seeds the history with every past event; later runs only
    # look at the most recent page of past events.
    past = listing_links(WTI + "/events/past", max_pages=1 if store else 30)

    todo = upcoming + [path for path in past if path not in store]
    failures = []
    for path in todo:
        try:
            event = parse_wti_event(path, fetch(WTI + path))
        except Exception as exc:
            failures.append("%s (%s)" % (path, exc))
            continue
        old = store.get(path, {})
        unchanged = all(old.get(key) == value for key, value in event.items())
        event["changed"] = old["changed"] if unchanged and "changed" in old else now.isoformat()
        store[path] = event
    for failure in failures:
        log("warning", "WTI event skipped: " + failure)
    if todo and len(failures) * 2 > len(todo):
        raise RuntimeError("could not read %d of %d WTI event pages; did the site layout change?"
                           % (len(failures), len(todo)))

    # Events still ahead that the site no longer lists were cancelled or removed.
    ahead = [path for path, event in store.items() if event_end(event) > now]
    if ahead and not upcoming:
        raise RuntimeError("wti.yale.edu/events lists no event while %d are still ahead; "
                           "did the site layout change?" % len(ahead))
    for path in ahead:
        if path not in upcoming and path not in past:
            log("notice", "WTI event no longer listed, removed: " + path)
            del store[path]

    store = dict(sorted(store.items(), key=lambda item: (item[1]["start"], item[0])))
    save_json(store_file, store)
    return store


# --- iCalendar output --------------------------------------------------------

def ics_text(value):
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\n").replace("\n", "\\n"))


def fold(line):
    """Fold a content line at 75 octets (RFC 5545, 3.1) without splitting characters."""
    parts, current, size = [], "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > 75:
            parts.append(current)
            current, size = " ", 1
        current += char
        size += width
    parts.append(current)
    return "\r\n".join(parts)


def ics_time(value):
    if "T" in value:
        return ":" + dt.datetime.fromisoformat(value).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return ";VALUE=DATE:" + value.replace("-", "")


def write_wti_ics(store, filename, description, skip=()):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//yale-talks-calendars//Wu Tsai Institute//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Wu Tsai Institute",
        "X-WR-CALDESC:" + ics_text(description),
        "X-WR-TIMEZONE:America/New_York",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for path, event in store.items():
        if path in skip:
            continue
        stamp = dt.datetime.fromisoformat(event["changed"]).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        summary = event["summary"]
        if event["cancelled"] and "cancel" not in summary.lower():
            summary = "CANCELLED: " + summary
        lines += [
            "BEGIN:VEVENT",
            "UID:wti-%s@yale-talks-calendars" % hashlib.sha1(path.encode("utf-8")).hexdigest()[:16],
            "DTSTAMP:" + stamp,
            "LAST-MODIFIED:" + stamp,
            "DTSTART" + ics_time(event["start"]),
            "DTEND" + ics_time(event["end"]),
            "SUMMARY:" + ics_text(summary),
        ]
        if event["location"]:
            lines.append("LOCATION:" + ics_text(event["location"]))
        lines += [
            "DESCRIPTION:" + ics_text(event["description"]),
            "URL:" + event["url"],
            "STATUS:" + ("CANCELLED" if event["cancelled"] else "CONFIRMED"),
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    (PUBLIC / filename).write_bytes("".join(fold(line) + "\r\n" for line in lines).encode("utf-8"))


# --- Psychology ----------------------------------------------------------------

def decode_calendar_id(value):
    """Embed URLs carry calendar ids either plainly or base64-encoded."""
    value = value.strip().replace(" ", "+")
    if "@" in value:
        return value
    try:
        padded = value.replace("-", "+").replace("_", "/") + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
    except ValueError:
        return None
    return decoded if "@" in decoded else None


def google_calendar_ids(page):
    ids = []
    for query in re.findall(r"calendar\.google\.com/calendar/(?:u/\d+/)?embed\?([^\"'\s>]+)", page):
        for value in urllib.parse.parse_qs(html.unescape(query)).get("src", []):
            ids.append(decode_calendar_id(value))
    return [i for i in dict.fromkeys(ids) if i and "#holiday@" not in i]


def google_ics_url(calendar_id):
    return "https://calendar.google.com/calendar/ical/%s/public/basic.ics" % urllib.parse.quote(calendar_id)


DTSTART = re.compile(r"(?m)^DTSTART(;[^:\r\n]*)?:(\d{8}T\d{4})\d{0,2}(Z?)")


def timed_events(blocks):
    """Map UTC start ("YYYYMMDDTHHMM") -> summaries, for the timed VEVENT blocks of a feed."""
    events = {}
    for block in blocks:
        start = DTSTART.search(block)
        if not start:
            continue  # all-day event
        params, stamp, utc = start.groups()
        moment = dt.datetime.strptime(stamp, "%Y%m%dT%H%M")
        if utc:
            moment = moment.replace(tzinfo=UTC)
        else:
            tzid = re.search(r"TZID=([^;:]+)", params or "")
            try:
                zone = ZoneInfo(tzid.group(1)) if tzid else TZ
            except Exception:
                zone = TZ
            moment = moment.replace(tzinfo=zone).astimezone(UTC)
        summary = re.search(r"(?m)^SUMMARY[^:\r\n]*:(.*?)\r?$", block)
        events.setdefault(moment.strftime("%Y%m%dT%H%M"), []).append(summary.group(1) if summary else "")
    return events


def read_google_calendar(calendar_id, today):
    """A public Google Calendar's name, whether it is still used, and its timed events."""
    ics = re.sub(r"\r?\n[ \t]", "", fetch(google_ics_url(calendar_id)))
    name = re.search(r"(?m)^X-WR-CALNAME:(.*?)\r?$", ics)
    # Only events count: VTIMEZONE blocks carry never-ending yearly rules too.
    blocks = re.findall(r"(?s)BEGIN:VEVENT.*?END:VEVENT", ics)
    events = "\n".join(blocks)
    starts = re.findall(r"(?m)^DTSTART[^:\r\n]*:(\d{8})", events)
    rules = re.findall(r"(?m)^RRULE:(.*?)\r?$", events)
    cutoff = (today - dt.timedelta(days=365)).strftime("%Y%m%d")

    def rule_reaches_cutoff(rule):
        until = re.search(r"UNTIL=(\d{8})", rule)
        return until.group(1) >= cutoff if until else "COUNT=" not in rule

    active = any(start >= cutoff for start in starts) or any(rule_reaches_cutoff(r) for r in rules)
    record = {"id": calendar_id, "name": name.group(1).strip() if name else calendar_id, "active": active}
    return record, timed_events(blocks)


# Words that say nothing about who is speaking, so they can't tell two talks apart.
GENERIC_WORDS = {"current", "works", "human", "neuroscience", "talk", "talks", "seminar", "speaker",
                 "inspiring", "series", "yale", "university", "institute", "department", "psychology",
                 "with", "from", "about", "the", "and", "for", "this", "that"}


def name_words(text):
    return {w for w in re.findall(r"[a-zà-ÿ]{4,}", text.lower()) if w not in GENERIC_WORDS}


def listed_by_psychology(event, psych_talks):
    """True when a Psychology calendar has the same talk: same start time, a shared name."""
    if "T" not in event["start"]:
        return False
    key = dt.datetime.fromisoformat(event["start"]).astimezone(UTC).strftime("%Y%m%dT%H%M")
    words = name_words(event["summary"])
    return any(words & name_words(summary) for summary in psych_talks.get(key, []))


def update_psych(today):
    baseline_file = DATA / "psych-calendars.json"
    previous = {c["id"]: c for c in load_json(baseline_file, [])}
    ids = google_calendar_ids(fetch(PSYCH_EVENTS))
    if not ids:
        raise RuntimeError("no Google Calendar embedded on %s any more; did the page change?" % PSYCH_EVENTS)
    calendars, talks = [], {}
    for calendar_id in ids:
        try:
            record, events = read_google_calendar(calendar_id, today)
        except Exception as exc:
            log("warning", "cannot read Google Calendar %s: %s" % (calendar_id, exc))
            calendars.append(previous.get(calendar_id) or {"id": calendar_id, "name": calendar_id, "active": True})
            continue
        calendars.append(record)
        for start, summaries in events.items():
            talks.setdefault(start, []).extend(summaries)

    current = {c["id"]: c for c in calendars}
    changes = []
    if previous:
        changes += ["- **New calendar:** %s (`%s`)" % (c["name"], i) for i, c in current.items() if i not in previous]
        changes += ["- **No longer on the page:** %s (`%s`)" % (c["name"], i)
                    for i, c in previous.items() if i not in current]
        changes += ["- **%s:** %s (`%s`)" % ("Active again" if c["active"] else "Inactive (no event for a year)",
                                            c["name"], i)
                    for i, c in current.items() if i in previous and previous[i]["active"] != c["active"]]
    if changes:
        WATCHDOG.write_text(
            "The Google Calendars embedded on %s changed:\n\n%s\n\n"
            "Subscribe or unsubscribe in your calendar app accordingly%s.\n"
            % (PSYCH_EVENTS, "\n".join(changes), " — links on " + PAGES_URL + "/" if PAGES_URL else ""),
            encoding="utf-8")
    save_json(baseline_file, calendars)
    return calendars, talks


# --- index page ----------------------------------------------------------------

def when(event):
    if "T" not in event["start"]:
        return dt.date.fromisoformat(event["start"]).strftime("%a %b %-d, %Y")
    start = dt.datetime.fromisoformat(event["start"]).astimezone(TZ)
    end = dt.datetime.fromisoformat(event["end"]).astimezone(TZ)
    return "%s · %s–%s" % (start.strftime("%a %b %-d, %Y"), start.strftime("%-I:%M"),
                           end.strftime("%-I:%M %p").lower())


def calendar_card(name, note, webcal, google, ics, muted=False):
    esc = html.escape
    return (
        '<div class="cal%s"><div><div class="name">%s</div><div class="note">%s</div></div>'
        '<div class="actions"><a class="primary" href="%s">Apple Calendar</a>'
        '<a href="%s" target="_blank" rel="noopener">Google Calendar</a>'
        '<a href="%s" title="Raw iCalendar feed (for Outlook and other apps)">.ics</a></div></div>'
        % (" muted" if muted else "", esc(name), esc(note), esc(webcal), esc(google), esc(ics)))


def wti_card(filename, note):
    https = (PAGES_URL + "/" + filename) if PAGES_URL else filename
    webcal = re.sub(r"^https?://", "webcal://", https)
    google = "https://calendar.google.com/calendar/r?cid=" + urllib.parse.quote(webcal, safe="")
    return calendar_card("Wu Tsai Institute", note, webcal, google, https)


def write_index(wti, psych, now, duplicates):
    esc = html.escape
    upcoming = [e for path, e in wti.items() if path not in duplicates and event_end(e) > now][:8]
    talks = "".join(
        '<li><div class="when">%s</div><a href="%s">%s</a></li>' % (esc(when(e)), esc(e["url"]), esc(e["summary"]))
        for e in upcoming) or "<li>No upcoming event listed yet.</li>"

    def psych_card(c):
        ics = google_ics_url(c["id"])
        cid = base64.b64encode(c["id"].encode("utf-8")).decode("ascii").rstrip("=")
        note = "Public Google Calendar of the department" if c["active"] else "No event for over a year"
        return calendar_card(c["name"], note, re.sub(r"^https://", "webcal://", ics),
                             "https://calendar.google.com/calendar/u/0?cid=" + cid, ics, muted=not c["active"])

    active = "".join(psych_card(c) for c in psych if c["active"])
    inactive = "".join(psych_card(c) for c in psych if not c["active"])
    if inactive:
        inactive = "<details><summary>Inactive calendars</summary>%s</details>" % inactive

    page = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Yale Talks Calendars</title>
<style>
:root { --bg:#fbfaf7; --fg:#1d1d1f; --muted:#6e6e73; --card:#fff; --line:#e4e1da; --accent:#00356b; --accent-fg:#fff; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#131314; --fg:#f2f2f2; --muted:#a1a1a6; --card:#1d1d1f; --line:#2f2f33; --accent:#8cb8ff; --accent-fg:#0b1a2e; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width:760px; margin:0 auto; padding:48px 16px 64px; }
h1 { font-size:30px; line-height:1.2; margin:0 0 8px; letter-spacing:-.01em; }
.lede { color:var(--muted); margin:0 0 8px; }
h2 { font-size:13px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); margin:40px 0 12px; }
.cal { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin:0 0 10px;
  display:flex; gap:12px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
.cal.muted { opacity:.6; }
.name { font-weight:600; }
.note { color:var(--muted); font-size:14px; }
.actions { display:flex; gap:8px; flex-wrap:wrap; }
.actions a { font-size:14px; text-decoration:none; padding:6px 12px; border-radius:999px; border:1px solid var(--line);
  color:var(--fg); white-space:nowrap; }
.actions a.primary { background:var(--accent); border-color:var(--accent); color:var(--accent-fg); }
ul.talks { list-style:none; padding:0; margin:16px 0 0; }
ul.talks li { padding:10px 0; border-top:1px solid var(--line); }
.when { color:var(--muted); font-size:14px; font-variant-numeric:tabular-nums; }
a { color:var(--accent); }
details summary { cursor:pointer; color:var(--muted); margin:8px 0 12px; }
footer { margin-top:48px; color:var(--muted); font-size:13px; }
</style>
</head>
<body>
<main>
<h1>Yale talks calendars</h1>
<p class="lede">Subscribe once and the talks appear in your calendar, kept up to date.
On a Mac or iPhone, tap <em>Apple Calendar</em> and confirm; choose the iCloud location to sync across devices.</p>

<h2>Wu Tsai Institute</h2>
%(wti_card)s
<details><summary>Not subscribed to the Psychology calendars? Take the complete feed</summary>%(wti_all_card)s</details>
<ul class="talks">%(talks)s</ul>

<h2>Department of Psychology</h2>
%(active)s
%(inactive)s

<footer>
<p>Unofficial. The Wu Tsai Institute publishes no calendar feed, so this one is rebuilt every 6 hours from
<a href="https://wti.yale.edu/events">wti.yale.edu/events</a>. The Psychology calendars are the department's own
public Google Calendars, as embedded on <a href="https://psychology.yale.edu/events">psychology.yale.edu/events</a>.</p>
<p>Last checked: <span id="checked">%(checked)s</span>%(repo)s</p>
</footer>
</main>
<script>
fetch("status.json", {cache: "no-store"}).then(r => r.json()).then(s => {
  document.getElementById("checked").textContent = new Date(s.checked).toLocaleString();
}).catch(() => {});
</script>
</body>
</html>
""" % {
        "wti_card": wti_card("wti.ics", "Inspiring Speakers, symposia and more; talks already on the "
                                        "Psychology calendars (such as Current Works) are left out"),
        "wti_all_card": wti_card("wti-all.ics", "Every event, Current Works in Human Neuroscience included"),
        "talks": talks,
        "active": active,
        "inactive": inactive,
        "checked": esc(now.isoformat()),
        "repo": (' · <a href="https://github.com/%s">source</a>' % esc(REPO)) if REPO else "",
    }
    (PUBLIC / "index.html").write_text(page, encoding="utf-8")


# --- main ------------------------------------------------------------------------

def main():
    now = dt.datetime.now(UTC).replace(microsecond=0)
    PUBLIC.mkdir(exist_ok=True)
    DATA.mkdir(exist_ok=True)
    errors = []
    try:
        wti = update_wti(now)
    except Exception as exc:
        errors.append("Wu Tsai Institute: %s" % exc)
        wti = load_json(DATA / "wti-events.json", {})
    try:
        psych, psych_talks = update_psych(now.astimezone(TZ).date())
    except Exception as exc:
        errors.append("Psychology: %s" % exc)
        psych, psych_talks = load_json(DATA / "psych-calendars.json", []), {}

    # wti.ics leaves out talks a Psychology calendar already lists (the Current
    # Works in Human Neuroscience, mostly), so they don't show up twice for
    # someone subscribed to both; wti-all.ics keeps everything.
    duplicates = {path for path, event in wti.items() if listed_by_psychology(event, psych_talks)}

    # Publish whatever is known, even after an error: the last good data stays online.
    write_wti_ics(wti, "wti.ics", skip=duplicates, description=(
        "Talks and events of the Wu Tsai Institute at Yale, from wti.yale.edu/events, leaving out talks "
        "already on the Psychology department calendars. Unofficial feed, refreshed every 6 hours."))
    write_wti_ics(wti, "wti-all.ics", description=(
        "All talks and events of the Wu Tsai Institute at Yale, from wti.yale.edu/events. "
        "Unofficial feed, refreshed every 6 hours."))
    write_index(wti, psych, now, duplicates)
    upcoming = sum(1 for e in wti.values() if event_end(e) > now)
    save_json(PUBLIC / "status.json", {
        "checked": now.isoformat(),
        "errors": errors,
        "wti_events": len(wti),
        "wti_upcoming": upcoming,
        "wti_also_on_psychology": len(duplicates),
        "psych_calendars": len(psych),
    })
    for error in errors:
        log("error", error)
    print("WTI: %d events (%d upcoming, %d also on Psychology calendars); Psychology: %d calendars (%d active)" % (
        len(wti), upcoming, len(duplicates), len(psych), sum(1 for c in psych if c["active"])))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
