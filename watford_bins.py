#!/usr/bin/env python3
"""
Watford bin collections -> DAKboard

Queries Watford Borough Council's bin day look-up for one property (by UPRN)
and writes, into OUTPUT_DIR:

  bins.html          colour-coded "bin tile" for a DAKboard Website block
  bins.json          the raw next-collection data
  bins-black.ics     one calendar feed per bin, so each can have its own
  bins-blue.ics      colour in DAKboard
  bins-green.ics
  bins-food.ics
  bins-all.ics       everything in one feed (if you'd rather one calendar)

  index.html         same as bins.html, so the site's root URL works too

Runs in GitHub Actions (see .github/workflows/bins.yml), which then
publishes the output folder to GitHub Pages. The property's UPRN is read
from the UPRN environment variable (a repository secret), so the address
never appears in this public repo. Standard library only.
"""

import datetime as dt
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

# ---- settings -------------------------------------------------------------
UPRN = os.environ.get("UPRN", "").strip()  # set as a repository secret, never in code
HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "site")
WEEKS_AHEAD = 8                            # how far to project calendar entries
# ---------------------------------------------------------------------------

BASE = "https://watfordbc-self.achieveservice.com"
LOOKUP_ID = "5e79edf15b2ec"

# key -> how to recognise it in the council's text, display name, colour, interval (days)
BINS = {
    "black": {"match": "black", "name": "Black bin", "detail": "General waste",
              "colour": "#2b2b2b", "lid": "#2b2b2b", "every": 14},
    "blue":  {"match": "blue",  "name": "Blue bin",  "detail": "Recycling",
              "colour": "#2b2b2b", "lid": "#1e6fd9", "every": 14},
    "green": {"match": "green", "name": "Green bin", "detail": "Garden waste",
              "colour": "#2f8f3e", "lid": "#2f8f3e", "every": 14},
    "food":  {"match": "food",  "name": "Food bin",  "detail": "Food waste",
              "colour": "#7a4a22", "lid": "#7a4a22", "every": 7},
}
CAL_COLOURS = {"black": "#444444", "blue": "#1e6fd9", "green": "#2f8f3e", "food": "#8b5a2b"}


def fetch_collections(uprn):
    """Return {bin_key: date} for the next collection of each bin."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    ua = {"User-Agent": "Mozilla/5.0 (home bin display)"}

    auth_url = (BASE + "/authapi/isauthenticated?uri="
                + urllib.parse.quote(urllib.parse.quote(BASE + "/en/service/Bin_collections", safe=""), safe="")
                + "&hostname=watfordbc-self.achieveservice.com&withCredentials=true")
    with opener.open(urllib.request.Request(auth_url, headers=ua), timeout=30) as r:
        sid = json.load(r)["auth-session"]

    params = urllib.parse.urlencode({
        "id": LOOKUP_ID, "repeat_against": "", "noRetry": "true",
        "getOnlyTokens": "undefined", "log_id": "", "app_name": "AF-Renderer::Self",
        "_": str(int(time.time() * 1000)), "sid": sid,
    })
    body = json.dumps({"formValues": {"Address": {"echoUprn": {"value": uprn}}}}).encode()
    req = urllib.request.Request(
        BASE + "/apibroker/runLookup?" + params, data=body, method="POST",
        headers={**ua, "Content-Type": "application/json", "Accept": "application/json",
                 "X-Requested-With": "XMLHttpRequest",
                 "Referer": BASE + "/fillform/?iframe_id=fillform-frame-1&db_id="})
    with opener.open(req, timeout=30) as r:
        data = json.load(r)

    if data.get("status") == "error":
        raise RuntimeError("council said: " + str(data.get("error", {}).get("message", data)))
    disp = data["integration"]["transformed"]["rows_data"]["0"]["dispHTML"]
    return parse_disp_html(disp)


def parse_disp_html(disp):
    found = {}
    for item in re.findall(r"<li[^>]*>(.*?)</li>", disp, re.S | re.I):
        title = re.search(r"<h3[^>]*>(.*?)</h3>", item, re.S | re.I)
        date = re.search(r"<strong[^>]*>\s*(\d{2}/\d{2}/\d{4})\s*</strong>", item, re.S | re.I)
        if not (title and date):
            continue
        t = html.unescape(re.sub(r"<[^>]+>", "", title.group(1))).lower()
        for key, b in BINS.items():
            if b["match"] in t:
                found[key] = dt.datetime.strptime(date.group(1), "%d/%m/%Y").date()
                break
    if not found:
        raise RuntimeError("No collections found in council response - layout may have changed")
    return found


def projected(collections, weeks):
    """Next date from the council, then repeat at the bin's normal interval."""
    out = []
    horizon = dt.date.today() + dt.timedelta(weeks=weeks)
    for key, first in collections.items():
        d, n = first, 0
        while d <= horizon:
            out.append((d, key, n == 0))   # n == 0 -> confirmed by council
            d += dt.timedelta(days=BINS[key]["every"])
            n += 1
    return sorted(out)


def ics(events, calname, colour):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//home//watford-bins//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{calname}",
             f"X-APPLE-CALENDAR-COLOR:{colour}", "REFRESH-INTERVAL;VALUE=DURATION:PT12H"]
    for d, key, confirmed in events:
        b = BINS[key]
        note = "Confirmed by Watford Council" if confirmed else \
               "Projected from usual schedule - check around bank holidays"
        lines += ["BEGIN:VEVENT",
                  f"UID:{key}-{d:%Y%m%d}@watford-bins",
                  f"DTSTAMP:{d:%Y%m%d}T000000Z",   # stable, so unchanged runs commit nothing
                  f"DTSTART;VALUE=DATE:{d:%Y%m%d}",
                  f"DTEND;VALUE=DATE:{d + dt.timedelta(days=1):%Y%m%d}",
                  f"SUMMARY:{b['name']} ({b['detail'].lower()})",
                  f"DESCRIPTION:{note}. Out the night before or by 6am.",
                  "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


STALE_DAYS = 7   # how long the council can be unreachable before the run fails (and GitHub emails)


def last_good(path):
    """Next dates as last confirmed by the council. Kept as-is (not rolled forward),
    so staleness can be measured; the tile rolls them forward in the browser."""
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)["bins"]
    return {k: dt.date.fromisoformat(v["next"]) for k, v in saved.items() if k in BINS}


def main():
    if not re.fullmatch(r"\d{6,12}", UPRN):
        raise RuntimeError("UPRN secret is missing or not a number - see setup guide")
    ok = True
    try:
        collections = fetch_collections(UPRN)
    except Exception as e:
        ok = False
        # Council look-up down: rebuild the tile from the last good dates so layout
        # changes still publish. Fail only once the data is more than a week stale.
        saved = os.path.join(OUTPUT_DIR, "bins.json")
        if not os.path.exists(saved):
            raise
        collections = last_good(saved)
        oldest = min(collections.values())
        if (dt.date.today() - oldest).days > STALE_DAYS:
            raise RuntimeError(f"Council look-up has failed for over {STALE_DAYS} days "
                               f"({e!r}); last confirmed date {oldest:%a %d %b}") from e
        print(f"::warning::Council look-up failed ({e!r}). "
              f"Rebuilt the tile from the last good dates.")
    write_site(collections, ok)


def write_site(collections, ok=True):
    events = projected(collections, WEEKS_AHEAD)

    payload = {
        # Status dot on the tile: green = last council look-up worked, red = it failed.
        # A plain flag (no timestamp) so the file only changes when the status flips.
        "ok": ok,
        "bins": {k: {"name": BINS[k]["name"], "detail": BINS[k]["detail"],
                     "body": BINS[k]["colour"], "lid": BINS[k]["lid"],
                     "every": BINS[k]["every"], "next": d.isoformat()}
                 for k, d in collections.items()},
    }
    files = {"bins.json": json.dumps(payload, indent=2)}
    for key in collections:
        files[f"bins-{key}.ics"] = ics([e for e in events if e[1] == key],
                                       f"Bins - {BINS[key]['name']}", CAL_COLOURS[key])
    files["bins-all.ics"] = ics(events, "Bins", "#2f8f3e")
    with open(os.path.join(HERE, "bins_template.html"), encoding="utf-8") as f:
        files["bins.html"] = f.read().replace("/*__DATA__*/null", json.dumps(payload))
    files["index.html"] = files["bins.html"]

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for name, text in files.items():
        write(os.path.join(OUTPUT_DIR, name), text)

    summary = ", ".join(f"{k} {d:%a %d %b}" for k, d in sorted(collections.items(), key=lambda x: x[1]))
    print("OK:", summary)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:      # the previous published version stays live if this fails
        print("FAILED:", e, file=sys.stderr)
        sys.exit(1)
