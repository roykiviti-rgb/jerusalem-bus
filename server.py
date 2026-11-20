"""
שרת מקומי ל"אוטובוס ירושלים": תכנון נסיעות, לוח יציאות מתחנות ומיקום אוטובוסים בזמן אמת.

הרצה:  python server.py      ואז לפתוח  http://localhost:8765
"""
import bisect
import datetime as dt
from array import array
import heapq
import json
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "jerusalem.db")
STATIC = os.path.join(HERE, "static")
PORT = int(os.environ.get("PORT", 8765))
TZ = ZoneInfo("Asia/Jerusalem")
UA = "jlm-bus-local-app/1.0"
STRIDE = "https://open-bus-stride-api.hasadna.org.il"

WALK_MPS = 1.25        # מהירות הליכה
DETOUR = 1.3           # הליכה ברחובות ארוכה מקו אווירי
ACCESS_M = 900         # מרחק הליכה מקסימלי לתחנה הראשונה / מהתחנה האחרונה
TRANSFER_M = 400       # מרחק הליכה מקסימלי בין תחנות בהחלפה
BUFFER = 60            # זמן מינימלי להחלפה (שניות)
MAX_LEGS = 4           # עד 3 החלפות
WINDOW = 3 * 3600      # חלון חיפוש
INF = float("inf")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def dist_m(lat1, lon1, lat2, lon2):
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371000 * math.hypot(x, y)


def walk_secs(m):
    return int(m * DETOUR / WALK_MPS)


# ---------------------------------------------------------------- נתונים סטטיים
class Data:
    def __init__(self):
        if not os.path.exists(DB_PATH):
            sys.exit("לא נמצא data/jerusalem.db. יש להריץ קודם: python build_data.py")
        self.db_lock = threading.Lock()
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        q = self.db.execute
        self.meta = dict(q("SELECT key, value FROM meta"))
        agencies = dict(q("SELECT id, name FROM agencies"))
        self.stops = q("SELECT id, gtfs_id, code, name, street, city, platform, lat, lon "
                       "FROM stops ORDER BY id").fetchall()
        self.stop_lat = [s[7] for s in self.stops]
        self.stop_lon = [s[8] for s in self.stops]
        self.routes = []
        for rid, gid, ag, short, long_, typ, color in q(
                "SELECT id, gtfs_id, agency, short, long, type, color FROM routes ORDER BY id"):
            self.routes.append({"id": rid, "gtfs_id": gid, "agency": agencies.get(ag, ""),
                                "short": short, "long": long_, "type": typ,
                                "color": ("#" + color) if color else ""})
        self.route_by_gtfs = {r["gtfs_id"]: r["id"] for r in self.routes}
        self.trips = q("SELECT id, gtfs_id, route, service, headsign FROM trips "
                       "ORDER BY id").fetchall()
        self.trip_by_num = {}
        for t in self.trips:  # trip_id בישראל: "<מספר>_<ddmmyy>". ה-SIRI משתמש במספר
            self.trip_by_num.setdefault(t[1].split("_")[0], []).append(t[0])
        self.calendar = {}
        for row in q("SELECT service, mon, tue, wed, thu, fri, sat, sun, start, end FROM calendar"):
            self.calendar[row[0]] = ([int(x) for x in row[1:8]], row[8], row[9])
        self.cal_dates = {}
        for s, d, t in q("SELECT service, date, type FROM calendar_dates"):
            self.cal_dates[(s, d)] = t
        # רשת מרחבית לחיפוש תחנות קרובות
        self.grid = {}
        for s in self.stops:
            self.grid.setdefault(self._cell(s[7], s[8]), []).append(s[0])
        # הליכה בין תחנות קרובות (להחלפות)
        self.foot = [[] for _ in self.stops]
        for s in self.stops:
            for o, m in self.near(s[7], s[8], TRANSFER_M):
                if o != s[0]:
                    self.foot[s[0]].append((o, walk_secs(m)))
        self.days = {}
        self.days_lock = threading.Lock()
        log(f"נטענו {len(self.stops)} תחנות, {len(self.routes)} קווים, {len(self.trips)} נסיעות")

    @staticmethod
    def _cell(lat, lon):
        return (int(lat / 0.004), int(lon / 0.0047))

    def near(self, lat, lon, radius):
        cl, co = self._cell(lat, lon)
        r = int(radius / 440) + 1
        out = []
        for i in range(cl - r, cl + r + 1):
            for j in range(co - r, co + r + 1):
                for sid in self.grid.get((i, j), ()):
                    m = dist_m(lat, lon, self.stop_lat[sid], self.stop_lon[sid])
                    if m <= radius:
                        out.append((sid, m))
        out.sort(key=lambda x: x[1])
        return out

    def active_services(self, date):
        ymd = date.strftime("%Y%m%d")
        wd = date.weekday()  # 0 = שני
        out = set()
        for s, (days, start, end) in self.calendar.items():
            if start <= ymd <= end and days[wd]:
                out.add(s)
        for (s, d), t in self.cal_dates.items():
            if d == ymd:
                if t == 1:
                    out.add(s)
                else:
                    out.discard(s)
        return out

    def day(self, date):
        """כל הנסיעות של יום שירות אחד: נסיעות, קשרים ממוינים, יציאות לפי תחנה."""
        key = date.isoformat()
        with self.days_lock:
            if key in self.days:
                self.days[key] = self.days.pop(key)  # LRU
                return self.days[key]
            d = self._load_day(date)
            self.days[key] = d
            while len(self.days) > 8:
                self.days.pop(next(iter(self.days)))
            return d

    def _load_day(self, date):
        # שומרים בעמודות (array) ולא ב-tuples: פי עשרה פחות זיכרון
        t = time.time()
        services = self.active_services(date)
        st_trip, st_stop = array("i"), array("i")
        st_arr, st_dep, st_dist = array("i"), array("i"), array("f")
        st_flags = array("b")
        trip_range = {}
        if services:
            ph = ",".join("?" * len(services))
            with self.db_lock:
                cur = self.db.execute(
                    f"SELECT st.trip, st.stop, st.arr, st.dep, st.dist, st.flags FROM stop_times st "
                    f"JOIN trips t ON t.id = st.trip WHERE t.service IN ({ph}) "
                    f"ORDER BY st.trip, st.seq", list(services))
                last, start = -1, 0
                for i, (trip, stop, arr, dep, dist, flags) in enumerate(cur):
                    st_flags.append(flags or 0)
                    if trip != last:
                        if last >= 0:
                            trip_range[last] = (start, i)
                        last, start = trip, i
                    st_trip.append(trip)
                    st_stop.append(stop)
                    st_arr.append(arr)
                    st_dep.append(dep)
                    st_dist.append(dist if dist is not None else -1.0)
                if last >= 0:
                    trip_range[last] = (start, len(st_trip))
        n = len(st_trip)
        rows = [i for i in range(n - 1) if st_trip[i] == st_trip[i + 1]]
        rows.sort(key=st_dep.__getitem__)
        conn_row = array("i", rows)
        conn_dep = array("i", (st_dep[i] for i in rows))
        by_stop = {}
        for i in rows:  # כבר ממוין לפי שעת יציאה
            if not st_flags[i] & 1:  # רק יציאות שאפשר לעלות אליהן
                by_stop.setdefault(st_stop[i], array("i")).append(i)
        log(f"יום {date}: {len(trip_range)} נסיעות, {len(rows)} קשרים ({time.time() - t:.1f}s)")
        return {"st_trip": st_trip, "st_stop": st_stop, "st_arr": st_arr, "st_dep": st_dep,
                "st_dist": st_dist, "st_flags": st_flags, "trip_range": trip_range,
                "conn_row": conn_row,
                "conn_dep": conn_dep, "by_stop": by_stop}

    @staticmethod
    def trip_stops(day, trip):
        """[(stop, arr, dep, dist)] של נסיעה ביום שירות."""
        r = day["trip_range"].get(trip)
        if not r:
            return None
        s, a, de, di = day["st_stop"], day["st_arr"], day["st_dep"], day["st_dist"]
        return [(s[i], a[i], de[i], di[i] if di[i] >= 0 else None) for i in range(*r)]

    # ------------------------------------------------------------ עזרים לזמנים
    def stop_json(self, sid):
        s = self.stops[sid]
        return {"id": sid, "code": s[2], "name": s[3], "street": s[4], "city": s[5],
                "platform": s[6], "lat": s[7], "lon": s[8]}

    def route_json(self, trip):
        t = self.trips[trip]
        r = self.routes[t[2]]
        return dict(r, headsign=t[4] or dest_from_long(r["long"]), trip_gtfs_id=t[1], trip=trip)


def dest_from_long(long_name):
    """route_long_name נראה כמו 'מוצא-עיר<->יעד-עיר-1#'. מחלצים את היעד."""
    if "<->" not in long_name:
        return ""
    d = long_name.split("<->", 1)[1].rstrip("#")
    parts = d.split("-")
    if len(parts) >= 3 and parts[-1].strip().isdigit():
        parts = parts[:-1]
    if len(parts) >= 2:
        parts = parts[:-1]  # שם העיר
    return "-".join(parts).strip()


D = None


def parse_time(qs):
    """מחזיר (תאריך בסיס, שניות מחצות התאריך)."""
    s = qs.get("time", [""])[0]
    if s:
        now = dt.datetime.fromisoformat(s)
        if now.tzinfo is None:
            now = now.replace(tzinfo=TZ)
        now = now.astimezone(TZ)
    else:
        now = dt.datetime.now(TZ)
    base = now.date()
    return base, now.hour * 3600 + now.minute * 60 + now.second


HE_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]


def nice_time(ms):
    t = dt.datetime.fromtimestamp(ms / 1000, TZ)
    return f"יום {HE_DAYS[t.weekday()]} {t.strftime('%H:%M')}"


def epoch_ms(base, secs):
    midnight = dt.datetime.combine(base, dt.time(0), TZ)
    return int((midnight.timestamp() + secs) * 1000)


DAY_OFFSETS = (-1, 0, 1, 2)


def window_conns(base, t0, t1):
    """קשרים מכל ימי השירות הרלוונטיים, בשניות יחסית לחצות של base, ממוינים."""
    lists = []
    for k in DAY_OFFSETS:
        day = D.day(base + dt.timedelta(days=k))
        shift = k * 86400
        lo = bisect.bisect_left(day["conn_dep"], t0 - shift)
        hi = bisect.bisect_right(day["conn_dep"], t1 - shift)
        if lo >= hi:
            continue
        tk = k + 1
        st_trip, st_stop = day["st_trip"], day["st_stop"]
        st_arr, st_dep, tr = day["st_arr"], day["st_dep"], day["trip_range"]
        fl = day["st_flags"]
        out = []
        for i in day["conn_row"][lo:hi]:
            trip = st_trip[i]
            # 1 = אי אפשר לעלות בתחנת המוצא של הקשר, 2 = אי אפשר לרדת בתחנת היעד שלו
            out.append((st_dep[i] + shift, st_arr[i + 1] + shift, st_stop[i], st_stop[i + 1],
                        trip * 4 + tk, i - tr[trip][0], (fl[i] & 1) | (fl[i + 1] & 2)))
        lists.append(out)
    if len(lists) == 1:
        return lists[0]
    return list(heapq.merge(*lists))


def trip_of(tripkey, base):
    k = tripkey % 4 - 1
    return tripkey // 4, base + dt.timedelta(days=k), k * 86400


# ---------------------------------------------------------------- תכנון נסיעה
def csa(base, t0, access, egress, t_end):
    """Connection Scan עם ספירת קווים: מחזיר את הנסיעה המהירה ביותר לכל מספר קווים."""
    n = len(D.stops)
    K = MAX_LEGS
    S = [[INF] * n for _ in range(K + 1)]      # מוכן לעלות לאוטובוס בתחנה אחרי k נסיעות
    J = [[None] * n for _ in range(K + 1)]
    T = [dict() for _ in range(K)]
    best = [None] * (K + 1)                    # (זמן הגעה ליעד, תחנה, נסיעה)
    best_t = [INF] * (K + 1)
    for sid, w in access.items():
        S[0][sid] = t0 + w
        J[0][sid] = ("o", w)
    conns = window_conns(base, t0, t_end)
    foot = D.foot
    stop_after = INF
    for ci, (dep, arr, frm, to, trip, idx, fl) in enumerate(conns):
        if dep > stop_after:
            break
        for k in range(K):
            ride = T[k].get(trip)
            if ride is None:
                if S[k][frm] <= dep and not fl & 1:
                    ride = T[k][trip] = (ci, J[k][frm])
                else:
                    continue
            if fl & 2:
                continue  # אסור לרדת כאן; ממשיכים בנסיעה
            k1 = k + 1
            bound = min(best_t[: k1 + 1])
            if arr >= bound:
                continue
            info = ("r", trip, ride, ci)
            e = egress.get(to)
            if e is not None and arr + e < best_t[k1]:
                best_t[k1] = arr + e
                best[k1] = (arr + e, to, info)
                stop_after = min(stop_after, min(best_t) + 40 * 60)
            ready = arr + BUFFER
            Sk = S[k1]
            if ready < Sk[to]:
                Sk[to] = ready
                J[k1][to] = info
                for o, w in foot[to]:
                    if ready + w < Sk[o]:
                        Sk[o] = ready + w
                        J[k1][o] = ("f", to, w, info)
    return best, conns


def build_journey(base, conns, result, origin, dest, egress_m):
    arrival, last_stop, info = result
    legs = []
    node = info
    end_stop = last_stop
    while True:
        tag = node[0]
        if tag == "o":
            legs.append({"type": "walk", "secs": node[1]})
            break
        if tag == "f":
            _, frm_stop, w, ride_info = node
            legs.append({"type": "walk", "secs": w, "transfer": True,
                         "from": D.stop_json(frm_stop), "to_stop": None})
            node = ride_info
            continue
        _, trip, (ci_in, prev), ci_out = node
        c_in, c_out = conns[ci_in], conns[ci_out]
        tid, sdate, shift = trip_of(trip, base)
        lst = D.trip_stops(D.day(sdate), tid)
        seg = lst[c_in[5]: c_out[5] + 2]
        legs.append({
            "type": "ride", "route": D.route_json(tid), "service_date": sdate.isoformat(),
            "from": D.stop_json(c_in[2]), "to": D.stop_json(c_out[3]),
            "dep": epoch_ms(base, c_in[0]), "arr": epoch_ms(base, c_out[1]),
            "dep_secs": c_in[0], "arr_secs": c_out[1],
            "stops": [dict(D.stop_json(s), t=epoch_ms(base, a + shift)) for s, a, _, _ in seg],
        })
        node = prev
    legs.reverse()
    rides = [l for l in legs if l["type"] == "ride"]
    # זמני ההליכה: לפני הנסיעה הראשונה ואחרי האחרונה
    first, last = rides[0], rides[-1]
    w0 = legs[0]
    w0["from_point"] = origin
    w0["to"] = first["from"]
    w0["arr"] = first["dep"]
    w0["dep"] = first["dep"] - w0["secs"] * 1000
    w0["meters"] = int(w0["secs"] * WALK_MPS / DETOUR)
    for i, l in enumerate(legs):
        if l.get("transfer"):
            l["to"] = legs[i + 1]["from"]
            l["dep"] = legs[i - 1]["arr"]
            l["arr"] = l["dep"] + l["secs"] * 1000
            l["meters"] = int(l["secs"] * WALK_MPS / DETOUR)
    eg = egress_m[last_stop]
    legs.append({"type": "walk", "secs": walk_secs(eg), "meters": int(eg),
                 "from": last["to"], "to_point": dest,
                 "dep": last["arr"], "arr": last["arr"] + walk_secs(eg) * 1000})
    dep = legs[0]["dep"]
    arr = legs[-1]["arr"]
    return {"dep": dep, "arr": arr, "duration_min": round((arr - dep) / 60000),
            "transfers": len(rides) - 1, "walk_m": sum(l.get("meters", 0) for l in legs
                                                     if l["type"] == "walk"),
            "legs": legs, "first_board_secs": first["dep_secs"],
            "key": "|".join(f'{l["route"]["short"]}@{l["from"]["id"]}>{l["to"]["id"]}@{l["dep"]}'
                            for l in rides)}


def next_departure_from(base, t0, stops, horizon=40 * 3600):
    """הזמן הראשון שבו יוצא אוטובוס מאחת התחנות (למשל אחרי שבת)."""
    best = INF
    for k in DAY_OFFSETS:
        day = D.day(base + dt.timedelta(days=k))
        shift = k * 86400
        key = day["st_dep"].__getitem__
        for sid in stops:
            lst = day["by_stop"].get(sid)
            if not lst:
                continue
            i = bisect.bisect_left(lst, t0 - shift, key=key)
            if i < len(lst):
                best = min(best, key(lst[i]) + shift)
    return best if best <= t0 + horizon else None


def plan(qs):
    base, t0 = parse_time(qs)
    flat, flon = float(qs["flat"][0]), float(qs["flon"][0])
    tlat, tlon = float(qs["tlat"][0]), float(qs["tlon"][0])
    origin = {"lat": flat, "lon": flon}
    dest = {"lat": tlat, "lon": tlon}
    direct = dist_m(flat, flon, tlat, tlon)
    acc = D.near(flat, flon, ACCESS_M)
    egr = D.near(tlat, tlon, ACCESS_M)
    access = {s: walk_secs(m) for s, m in acc}
    egress = {s: walk_secs(m) for s, m in egr}
    egress_m = {s: m for s, m in egr}
    out = {"query_time": epoch_ms(base, t0), "journeys": [], "notice": None}
    walk_only = None
    if direct < 2000:
        ws = walk_secs(direct)
        walk_only = {"dep": epoch_ms(base, t0), "arr": epoch_ms(base, t0 + ws),
                     "duration_min": round(ws / 60), "transfers": -1, "walk_m": int(direct),
                     "legs": [{"type": "walk", "secs": ws, "meters": int(direct),
                               "from_point": origin, "to_point": dest,
                               "dep": epoch_ms(base, t0), "arr": epoch_ms(base, t0 + ws)}],
                     "key": "walk"}
    if not access or not egress:
        out["notice"] = "אין תחנות אוטובוס במרחק הליכה מנקודת המוצא או מהיעד."
        if walk_only:
            out["journeys"].append(walk_only)
        return out

    nd = next_departure_from(base, t0, access)
    if nd is None:
        out["notice"] = "לא נמצאו אוטובוסים ביממה הקרובה."
        return out
    if nd - t0 > 45 * 60:
        out["notice"] = ("אין כרגע שירות (שבת/לילה). מוצגות הנסיעות הראשונות מ"
                         + nice_time(epoch_ms(base, nd)))
        t0 = max(t0, nd - 20 * 60)

    seen, journeys = set(), []
    t = t0
    tstart = time.time()
    for _ in range(6):
        best, conns = csa(base, t, access, egress, t + WINDOW)
        found = [b for b in best if b]
        if not found:
            break
        cands = [build_journey(base, conns, b, origin, dest, egress_m) for b in found]
        # שומרים רק אפשרויות שלא נשלטות: יותר החלפות רק אם מגיעים מוקדם יותר ב-3 דק' לפחות
        cands.sort(key=lambda j: (j["transfers"], j["arr"]))
        kept = []
        for j in cands:
            if all(j["arr"] < k["arr"] - 180000 for k in kept):
                kept.append(j)
        for j in kept:
            if j["key"] not in seen:
                seen.add(j["key"])
                journeys.append(j)
        t = min(j["first_board_secs"] for j in kept) - min(access.values()) + 60
        if time.time() - tstart > 6:
            break
    journeys.sort(key=lambda j: (j["arr"], j["transfers"]))
    # מסננים נסיעות שיוצאות מאוחר יותר ומגיעות מאוחר יותר עם יותר החלפות
    final = []
    for j in journeys:
        if any(f["dep"] >= j["dep"] and f["arr"] <= j["arr"] and f["transfers"] <= j["transfers"]
               for f in final):
            continue
        final.append(j)
    final.sort(key=lambda j: j["dep"])
    if walk_only and (not final or walk_only["arr"] <= min(j["arr"] for j in final) + 5 * 60000):
        final.insert(0, walk_only)
    for j in final:
        j.pop("first_board_secs", None)
    out["journeys"] = final[:6]
    out["calc_ms"] = int((time.time() - tstart) * 1000)
    return out


# ---------------------------------------------------------------- יציאות מתחנות
def departures(qs):
    base, t0 = parse_time(qs)
    if "stop" in qs:
        stops = [(int(qs["stop"][0]), 0)]
    else:
        lat, lon = float(qs["lat"][0]), float(qs["lon"][0])
        stops = D.near(lat, lon, float(qs.get("radius", ["450"])[0]))[:10]
    horizon = int(qs.get("minutes", ["90"])[0]) * 60
    out = {"query_time": epoch_ms(base, t0), "stops": [], "notice": None}
    nd = next_departure_from(base, t0, [s for s, _ in stops])
    if nd is not None and nd - t0 > horizon:
        out["notice"] = ("אין כרגע שירות. האוטובוס הבא יוצא ב"
                         + nice_time(epoch_ms(base, nd)))
        t0 = nd - 60
    for sid, m in stops:
        groups = {}
        for k in DAY_OFFSETS:
            day = D.day(base + dt.timedelta(days=k))
            shift = k * 86400
            lst = day["by_stop"].get(sid, ())
            st_dep, st_trip = day["st_dep"], day["st_trip"]
            i = bisect.bisect_left(lst, t0 - shift, key=st_dep.__getitem__)
            while i < len(lst) and st_dep[lst[i]] + shift <= t0 + horizon:
                row = lst[i]
                dep, trip = st_dep[row], st_trip[row]
                idx = row - day["trip_range"][trip][0]
                tr = D.trips[trip]
                rt = D.routes[tr[2]]
                key = (rt["short"], rt["agency"], tr[4] or dest_from_long(rt["long"]))
                g = groups.get(key)
                if g is None:
                    g = groups[key] = {"route": D.route_json(trip), "times": []}
                if len(g["times"]) < 4:
                    g["times"].append({"t": epoch_ms(base, dep + shift), "trip": trip,
                                       "date": (base + dt.timedelta(days=k)).isoformat(),
                                       "idx": idx})
                i += 1
        lines = sorted(groups.values(), key=lambda g: g["times"][0]["t"])
        if lines:
            out["stops"].append(dict(D.stop_json(sid), dist=int(m), lines=lines))
    return out


def trip_detail(qs):
    trip = int(qs["trip"][0])
    sdate = dt.date.fromisoformat(qs["date"][0])
    base, _ = parse_time(qs)
    shift = (sdate - base).days * 86400
    lst = D.trip_stops(D.day(sdate), trip) or []
    return {"route": D.route_json(trip),
            "stops": [dict(D.stop_json(s), t=epoch_ms(base, a + shift)) for s, a, _, _ in lst]}


def search_stops(qs):
    q = qs.get("q", [""])[0].strip()
    if not q:
        return []
    words = q.split()
    res = []
    for s in D.stops:
        hay = f"{s[3]} {s[4]} {s[2]}"
        if all(w in hay for w in words):
            score = (0 if s[3].startswith(q) or s[2] == q else 1, len(s[3]))
            res.append((score, s[0]))
    res.sort()
    return [D.stop_json(sid) for _, sid in res[:15]]


def search_lines(qs):
    q = qs.get("q", [""])[0].strip()
    out = {}
    for r in D.routes:
        if r["short"] == q:
            out.setdefault((r["short"], r["agency"]), []).append(r)
    res = []
    for (short, agency), rs in out.items():
        for r in rs:
            with D.db_lock:
                row = D.db.execute("SELECT id, headsign FROM trips WHERE route=? LIMIT 1",
                                   (r["id"],)).fetchone()
            if row:
                res.append(dict(r, headsign=row[1], sample_trip=row[0]))
    return res


def line_pattern(qs):
    """המסלול של קו (לפי נסיעה לדוגמה) והיציאות הבאות שלו מכל תחנה."""
    base, t0 = parse_time(qs)
    route = int(qs["route"][0])
    # הנסיעה הבאה שיוצאת (עד יומיים קדימה); אם אין, הקרובה ביותר
    best = None
    tids = [t[0] for t in D.trips if t[2] == route]
    for k in (-1, 0, 1, 2):
        day = D.day(base + dt.timedelta(days=k))
        for tid in tids:
            r = day["trip_range"].get(tid)
            if not r:
                continue
            start = day["st_dep"][r[0]] + k * 86400
            end = day["st_arr"][r[1] - 1] + k * 86400
            if start >= t0:
                score = start - t0
            elif end >= t0:
                score = 5 * 3600 + (t0 - start)
            else:
                score = 10 ** 7 + (t0 - start)
            if best is None or score < best[0]:
                best = (score, tid, base + dt.timedelta(days=k))
    if not best:
        return {"route": D.routes[route], "stops": []}
    return trip_detail({"trip": [str(best[1])], "date": [best[2].isoformat()],
                        **({"time": qs["time"]} if "time" in qs else {})})


# ---------------------------------------------------------------- זמן אמת
_live_cache = {}
_live_lock = threading.Lock()


def fetch_live(line_ref=None):
    """מיקומי אוטובוסים בירושלים מה-6 דקות האחרונות. עם line_ref: רק הקו הזה."""
    key = line_ref or "*"
    with _live_lock:
        hit = _live_cache.get(key)
        if hit and time.time() - hit[0] < 25:
            return hit[1]
    n = dt.datetime.now(dt.timezone.utc)
    f = "%Y-%m-%dT%H:%M:%SZ"
    url = (f"{STRIDE}/siri_vehicle_locations/list?limit=5000"
           f"&recorded_at_time_from={(n - dt.timedelta(minutes=6)).strftime(f)}"
           f"&recorded_at_time_to={(n + dt.timedelta(minutes=1)).strftime(f)}"
           "&lat__greater_or_equal=31.70&lat__lower_or_equal=31.88"
           "&lon__greater_or_equal=35.09&lon__lower_or_equal=35.30")
    if line_ref:
        url += f"&siri_routes__line_ref={int(line_ref)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    rows = json.load(urllib.request.urlopen(req, timeout=20))
    latest = {}
    for r in rows:
        v = r["siri_ride__vehicle_ref"]
        if v not in latest or r["recorded_at_time"] > latest[v]["recorded_at_time"]:
            latest[v] = r
    data = list(latest.values())
    with _live_lock:
        _live_cache[key] = (time.time(), data)
        if len(_live_cache) > 200:
            _live_cache.clear()
    return data


def delay_for(row):
    """מעריך איחור: משווים את המרחק שהאוטובוס עבר מתחילת הנסיעה לזמני הלוח."""
    jr = row.get("siri_ride__journey_ref") or ""
    parts = jr.split("-")
    if len(parts) < 4:
        return None, None
    date = dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
    tids = D.trip_by_num.get(parts[3], [])
    day = D.day(date)
    for tid in tids:
        lst = D.trip_stops(day, tid)
        if not lst:
            continue
        dist = row.get("distance_from_journey_start")
        rec = dt.datetime.fromisoformat(row["recorded_at_time"]).astimezone(TZ)
        rec_s = (rec.date() - date).days * 86400 + rec.hour * 3600 + rec.minute * 60 + rec.second
        pts = [(d, a) for _, a, _, d in lst if d is not None]
        if dist is None or len(pts) < 2:
            return tid, None
        for (d1, a1), (d2, a2) in zip(pts, pts[1:]):
            if d1 <= dist <= d2:
                sched = a1 + (a2 - a1) * ((dist - d1) / (d2 - d1) if d2 > d1 else 0)
                return tid, round((rec_s - sched) / 60)
        return tid, None
    return None, None


def live(qs):
    want = [w for w in qs.get("routes", [""])[0].split(",") if w.strip().isdigit()][:6]
    try:
        rows = [r for w in want for r in fetch_live(w)] if want else fetch_live()
    except Exception as e:  # noqa
        return {"error": str(e), "vehicles": []}
    out = []
    for r in rows:
        gid = str(r["siri_route__line_ref"])
        rid = D.route_by_gtfs.get(gid)
        route = D.routes[rid] if rid is not None else None
        tid, delay = delay_for(r) if (want or len(rows) < 400) else (None, None)
        if delay is not None and abs(delay) > 30:
            delay = None  # כנראה אוטובוס שחונה במסוף; לא מציגים הערכה לא סבירה
        out.append({"lat": r["lat"], "lon": r["lon"], "bearing": r["bearing"],
                    "recorded": r["recorded_at_time"], "vehicle": r["siri_ride__vehicle_ref"],
                    "route_gtfs_id": gid, "short": route["short"] if route else "?",
                    "trip": tid, "delay_min": delay,
                    "headsign": D.trips[tid][4] if tid is not None else ""})
    return {"vehicles": out}


def geocode(qs):
    q = qs.get("q", [""])[0]
    url = ("https://nominatim.openstreetmap.org/search?format=json&limit=6&accept-language=he"
           "&viewbox=35.09,31.88,35.30,31.70&bounded=1&q=" + urllib.parse.quote(q))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    res = json.load(urllib.request.urlopen(req, timeout=15))
    return [{"name": r["display_name"].split(", ישראל")[0], "lat": float(r["lat"]),
             "lon": float(r["lon"])} for r in res]


def meta(qs):
    end = max((c[2] for c in D.calendar.values()), default="")
    return {"feed_date": D.meta.get("feed_date"), "built_at": D.meta.get("built_at"),
            "service_end": f"{end[:4]}-{end[4:6]}-{end[6:]}" if end else "",
            "stops": len(D.stops), "routes": len(D.routes)}


API = {"/api/plan": plan, "/api/departures": departures, "/api/trip": trip_detail,
       "/api/stops": search_stops, "/api/lines": search_lines, "/api/line": line_pattern,
       "/api/live": live, "/api/geocode": geocode, "/api/meta": meta}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            log(fmt % args)

    def send_config(self):
        """הגדרות אישיות (כתובת הבית) מ-config.local.json, שלא נשמר ב-git."""
        cfg = {}
        path = os.path.join(HERE, "config.local.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8-sig") as f:
                    cfg = json.load(f)
            except (OSError, ValueError) as e:
                log("שגיאה בקריאת config.local.json:", e)
        body = ("window.APP_CONFIG = " + json.dumps(cfg, ensure_ascii=False) + ";").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/config.js":
            return self.send_config()
        fn = API.get(u.path)
        if not fn:
            return super().do_GET()
        try:
            body = json.dumps(fn(urllib.parse.parse_qs(u.query)), ensure_ascii=False).encode()
            code = 200
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode()
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    global D
    D = Data()
    url = f"http://localhost:{PORT}"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # הפורט תפוס: כנראה השרת כבר רץ בחלון אחר. פשוט פותחים את הדף.
        log("השרת כבר פועל. פותח את הדפדפן...")
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        return
    # טוענים מראש את הימים הקרובים כדי שהבקשה הראשונה תהיה מהירה
    threading.Thread(target=lambda: [D.day(dt.datetime.now(TZ).date() + dt.timedelta(days=k))
                                     for k in DAY_OFFSETS], daemon=True).start()
    log(f"השרת פועל: {url}   (כדי לסגור: לסגור את החלון הזה)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
