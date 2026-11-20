"""
בונה את מסד הנתונים של ירושלים מתוך קובץ ה-GTFS הרשמי של משרד התחבורה.

הקובץ הגולמי (כ-140MB) נשמר מחוץ ל-OneDrive, ב-%LOCALAPPDATA%\\jlm-bus\\gtfs.zip
ורק הנתונים המסוננים של ירושלים נכתבים ל-data/jerusalem.db בתיקיית הפרויקט.

הרצה:
    python build_data.py            # משתמש בקובץ שכבר הורד
    python build_data.py --download # מוריד קובץ עדכני ואז בונה
"""
import csv
import io
import os
import sqlite3
import sys
import time
import urllib.request
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa
    pass

GTFS_URL = "https://gtfs.mot.gov.il/gtfsfiles/israel-public-transportation.zip"
CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "jlm-bus")
ZIP_PATH = os.path.join(CACHE_DIR, "gtfs.zip")
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "jerusalem.db")

# ירושלים רבתי, כולל מבשרת ציון, הר הצופים, רמות, פסגת זאב, גילה וקצה מעלה אדומים
BBOX = (31.70, 35.09, 31.88, 35.30)  # min_lat, min_lon, max_lat, max_lon


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def download():
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = ZIP_PATH + ".part"
    log("מוריד", GTFS_URL)
    with urllib.request.urlopen(GTFS_URL, timeout=120) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done * 100 // total}% ", end="", flush=True)
    print()
    os.replace(tmp, ZIP_PATH)


def reader(z, name):
    f = z.open(name)
    return csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))


def tsec(s):
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


def parse_desc(desc):
    """stop_desc בישראל נראה כמו 'רחוב:הרצל 10 עיר: ירושלים רציף:  קומה:'"""
    out = {}
    if not desc:
        return out
    keys = ["רחוב:", "עיר:", "רציף:", "קומה:"]
    pos = sorted((desc.find(k), k) for k in keys if desc.find(k) >= 0)
    for i, (p, k) in enumerate(pos):
        end = pos[i + 1][0] if i + 1 < len(pos) else len(desc)
        out[k[:-1]] = desc[p + len(k):end].strip()
    return out


def build():
    if not os.path.exists(ZIP_PATH):
        download()
    z = zipfile.ZipFile(ZIP_PATH)
    names = set(z.namelist())
    log("קבצים ב-GTFS:", ", ".join(sorted(names)))

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    tmp_db = DB_PATH + ".tmp"
    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    db = sqlite3.connect(tmp_db)
    db.executescript(
        """
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE agencies(id INTEGER PRIMARY KEY, gtfs_id TEXT, name TEXT);
        CREATE TABLE stops(id INTEGER PRIMARY KEY, gtfs_id TEXT, code TEXT, name TEXT,
                           street TEXT, city TEXT, platform TEXT, lat REAL, lon REAL);
        CREATE TABLE routes(id INTEGER PRIMARY KEY, gtfs_id TEXT, agency INTEGER, short TEXT,
                            long TEXT, type INTEGER, color TEXT);
        CREATE TABLE trips(id INTEGER PRIMARY KEY, gtfs_id TEXT, route INTEGER, service TEXT,
                           headsign TEXT, direction INTEGER, shape TEXT);
        CREATE TABLE stop_times(trip INTEGER, seq INTEGER, stop INTEGER, arr INTEGER,
                                dep INTEGER, dist REAL, flags INTEGER);
        CREATE TABLE calendar(service TEXT PRIMARY KEY, mon INT, tue INT, wed INT, thu INT,
                              fri INT, sat INT, sun INT, start TEXT, end TEXT);
        CREATE TABLE calendar_dates(service TEXT, date TEXT, type INT);
        """
    )

    # --- תחנות ---
    min_lat, min_lon, max_lat, max_lon = BBOX
    stop_map = {}
    rows = []
    for r in reader(z, "stops.txt"):
        try:
            lat, lon = float(r["stop_lat"]), float(r["stop_lon"])
        except (ValueError, KeyError):
            continue
        if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
            continue
        if (r.get("location_type") or "0") not in ("0", ""):
            continue
        sid = len(stop_map)
        stop_map[r["stop_id"]] = sid
        d = parse_desc(r.get("stop_desc", ""))
        rows.append((sid, r["stop_id"], r.get("stop_code", ""), r["stop_name"].strip(),
                     d.get("רחוב", ""), d.get("עיר", ""), d.get("רציף", ""), lat, lon))
    db.executemany("INSERT INTO stops VALUES (?,?,?,?,?,?,?,?,?)", rows)
    log("תחנות באזור ירושלים:", len(rows))

    # --- זמני עצירה (הקובץ הגדול) ---
    log("קורא stop_times.txt (זה לוקח כמה דקות)...")
    trip_map = {}
    batch = []
    n = kept = 0
    for r in reader(z, "stop_times.txt"):
        n += 1
        sid = stop_map.get(r["stop_id"])
        if sid is None:
            continue
        tid = trip_map.get(r["trip_id"])
        if tid is None:
            tid = trip_map[r["trip_id"]] = len(trip_map)
        dist = r.get("shape_dist_traveled") or ""
        # flags: 1 = אסור לעלות בתחנה, 2 = אסור לרדת בתחנה
        flags = (1 if (r.get("pickup_type") or "0") == "1" else 0) | \
                (2 if (r.get("drop_off_type") or "0") == "1" else 0)
        batch.append((tid, int(r["stop_sequence"]), sid, tsec(r["arrival_time"]),
                      tsec(r["departure_time"]), float(dist) if dist else None, flags))
        kept += 1
        if len(batch) >= 200_000:
            db.executemany("INSERT INTO stop_times VALUES (?,?,?,?,?,?,?)", batch)
            batch.clear()
            log(f"  {n:,} שורות נקראו, {kept:,} נשמרו")
    db.executemany("INSERT INTO stop_times VALUES (?,?,?,?,?,?,?)", batch)
    log(f"stop_times: {n:,} שורות, {kept:,} בירושלים, {len(trip_map):,} נסיעות")

    # --- נסיעות, קווים, חברות ---
    route_ids = set()
    trows = []
    for r in reader(z, "trips.txt"):
        tid = trip_map.get(r["trip_id"])
        if tid is None:
            continue
        route_ids.add(r["route_id"])
        trows.append([tid, r["trip_id"], r["route_id"], r["service_id"],
                      (r.get("trip_headsign") or "").strip(), int(r.get("direction_id") or 0),
                      r.get("shape_id", "")])

    route_map, agency_ids, rrows = {}, set(), []
    for r in reader(z, "routes.txt"):
        if r["route_id"] not in route_ids:
            continue
        rid = route_map[r["route_id"]] = len(route_map)
        agency_ids.add(r["agency_id"])
        rrows.append([rid, r["route_id"], r["agency_id"], r["route_short_name"].strip(),
                      r.get("route_long_name", "").strip(), int(r.get("route_type") or 3),
                      r.get("route_color", "")])
    agency_map, arows = {}, []
    for r in reader(z, "agency.txt"):
        if r["agency_id"] in agency_ids:
            aid = agency_map[r["agency_id"]] = len(agency_map)
            arows.append((aid, r["agency_id"], r["agency_name"]))
    for rr in rrows:
        rr[2] = agency_map.get(rr[2])
    for t in trows:
        t[2] = route_map[t[2]]
    db.executemany("INSERT INTO agencies VALUES (?,?,?)", arows)
    db.executemany("INSERT INTO routes VALUES (?,?,?,?,?,?,?)", rrows)
    db.executemany("INSERT INTO trips VALUES (?,?,?,?,?,?,?)", trows)
    log("קווים:", len(rrows), "חברות:", len(arows))

    services = {t[3] for t in trows}
    if "calendar.txt" in names:
        crows = []
        for r in reader(z, "calendar.txt"):
            if r["service_id"] in services:
                crows.append((r["service_id"], r["monday"], r["tuesday"], r["wednesday"],
                              r["thursday"], r["friday"], r["saturday"], r["sunday"],
                              r["start_date"], r["end_date"]))
        db.executemany("INSERT INTO calendar VALUES (?,?,?,?,?,?,?,?,?,?)", crows)
    if "calendar_dates.txt" in names:
        cd = [(r["service_id"], r["date"], int(r["exception_type"]))
              for r in reader(z, "calendar_dates.txt") if r["service_id"] in services]
        db.executemany("INSERT INTO calendar_dates VALUES (?,?,?)", cd)

    log("בונה אינדקסים...")
    db.executescript(
        """
        CREATE INDEX st_trip ON stop_times(trip, seq);
        CREATE INDEX st_stop ON stop_times(stop);
        CREATE INDEX trips_service ON trips(service);
        CREATE INDEX trips_route ON trips(route);
        CREATE INDEX trips_gtfs ON trips(gtfs_id);
        """
    )
    info = z.getinfo("stop_times.txt")
    feed_date = "%04d-%02d-%02d" % info.date_time[:3]
    db.executemany("INSERT INTO meta VALUES (?,?)", [
        ("feed_date", feed_date), ("built_at", time.strftime("%Y-%m-%d %H:%M"))])
    db.commit()
    db.close()
    for attempt in range(30):
        try:
            os.replace(tmp_db, DB_PATH)
            break
        except PermissionError:
            if attempt == 0:
                log("הקובץ תפוס: סגרו את חלון השרת (הפעלה.bat) והעדכון יסתיים לבד...")
            time.sleep(2)
    else:
        sys.exit("לא הצלחתי להחליף את הקובץ. סגרו את השרת והריצו שוב את העדכון.")
    log("סיום. נשמר ל-", DB_PATH, f"({os.path.getsize(DB_PATH) / 1e6:.0f}MB)")


if __name__ == "__main__":
    if "--download" in sys.argv:
        download()
    build()
