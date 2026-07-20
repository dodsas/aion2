#!/usr/bin/env python3
"""
아이온2 제작 DB 로컬 뷰어 서버 (Python 표준 라이브러리만 사용).

실행:
  python3 server.py            # http://127.0.0.1:8770
  python3 server.py --port 9000

엔드포인트:
  GET /                         index.html
  GET /images/<file>           아이템 아이콘(PNG)
  GET /api/meta                필터용 분야/종족/등급 목록
  GET /api/search?q=&class1=&race=&only=recipe|any
  GET /api/recipe?code=        레시피 상세(재료+시세+비용)
  GET /api/item?code=          아이템 상세(시세 이력 + 이 아이템을 쓰는 레시피)
"""
from __future__ import annotations
import argparse
import concurrent.futures
import hashlib
import json
import sqlite3
import ssl
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote, urlencode

import crawl_craft as ccf  # 공유 스키마 상수(PRICE_OVERRIDES_DDL, V_*_SQL)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "aion2_craft.db"
# 캐릭터 상세 캐시 전용 DB(크롤러가 재생성하는 craft.db 와 분리 · git 미추적).
CHAR_CACHE_DB = BASE_DIR / "char_cache.db"
IMG_DIR = BASE_DIR / "images"
INDEX = BASE_DIR / "index.html"


def db():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def char_db():
    con = sqlite3.connect(CHAR_CACHE_DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def ensure_char_cache_schema():
    """캐릭터 상세를 '화면 노출 기준 JSON' 한 컬럼(data)에 저장하고,
    변경 감지용 sha256(hash)을 함께 둔다. k = 'serverId:characterId'."""
    con = char_db()
    try:
        con.executescript(
            "CREATE TABLE IF NOT EXISTS char_cache("
            " k TEXT PRIMARY KEY,"
            " data TEXT NOT NULL,"
            " hash TEXT NOT NULL,"
            " updated_at TEXT NOT NULL);")
        con.commit()
    finally:
        con.close()


def db_rw():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def ensure_schema():
    """price_overrides 테이블 보장 + (기존 DB 대상) override-aware 뷰로 업그레이드."""
    con = db_rw()
    try:
        con.executescript(ccf.PRICE_OVERRIDES_DDL)
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='prices'").fetchone():
            con.executescript("DROP VIEW IF EXISTS v_recipe_cost;"
                              "DROP VIEW IF EXISTS v_price_best;")
            con.executescript(ccf.V_PRICE_BEST_SQL)
            con.executescript(ccf.V_RECIPE_COST_SQL)
        con.commit()
    finally:
        con.close()


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def has_prices(con) -> bool:
    r = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prices'").fetchone()
    return r is not None


# ---------- API 핸들러 ----------

def api_meta(con, q):
    class1 = rows_to_dicts(con.execute(
        "SELECT DISTINCT class1, class1_text FROM recipes "
        "WHERE class1_text IS NOT NULL ORDER BY class1"))
    class2 = rows_to_dicts(con.execute(
        "SELECT DISTINCT class1_text, class2_text FROM recipes "
        "WHERE class2_text IS NOT NULL ORDER BY class1, class2"))
    n = con.execute("SELECT COUNT(*) c FROM recipes").fetchone()["c"]
    price_updated = None
    if has_prices(con):
        row = con.execute("SELECT MAX(updated_at) m FROM prices").fetchone()
        price_updated = row["m"] if row else None
    return {
        "class1": class1,
        "class2": class2,
        "races": ["천족", "마족"],
        "recipe_count": n,
        "has_prices": has_prices(con),
        "price_latest": price_updated,
    }


def api_search(con, q):
    term = (q.get("q", [""])[0] or "").strip()
    class1 = q.get("class1", [""])[0]
    race = q.get("race", [""])[0]
    only = q.get("only", ["recipe"])[0]

    where, args = [], []
    if term:
        where.append("r.name LIKE ?")
        args.append(f"%{term}%")
    if class1:
        where.append("r.class1_text = ?")
        args.append(class1)
    if race:
        where.append("r.race_text = ?")
        args.append(race)
    wsql = ("WHERE " + " AND ".join(where)) if where else ""

    price_join = ""
    price_sel = ""
    if has_prices(con):
        price_join = "LEFT JOIN v_price_best b ON b.item_code = r.product_code"
        price_sel = ", b.best_price AS product_price"

    sql = f"""
        SELECT r.code, r.name, r.full_text, r.class1_text, r.class2_text,
               r.race_text, r.grade_text, r.grade, r.type_text,
               i.icon AS icon, i.image_file AS image_file {price_sel}
        FROM recipes r
        LEFT JOIN items i ON i.code = r.product_code
        {price_join}
        {wsql}
        ORDER BY r.class1, r.class2, r.name
        LIMIT 300
    """
    recipes = rows_to_dicts(con.execute(sql, args))

    materials = []
    if only == "any" and term:
        # 레시피(완성품)가 아닌, 재료로만 등장하는 아이템도 검색
        recipe_codes = {r["code"] for r in recipes}
        msql = """
            SELECT i.code, i.name, i.icon, i.image_file, i.grade,
                   (SELECT COUNT(*) FROM materials m WHERE m.code=i.code) AS used_in
            FROM items i
            WHERE i.name LIKE ?
            ORDER BY used_in DESC, i.name LIMIT 100
        """
        for row in rows_to_dicts(con.execute(msql, (f"%{term}%",))):
            if row["code"] not in recipe_codes and row["used_in"] > 0:
                materials.append(row)

    return {"recipes": recipes, "materials": materials,
            "count": len(recipes)}


def api_recipe(con, q):
    code = q.get("code", [None])[0]
    if not code:
        return {"error": "code required"}
    r = con.execute("SELECT * FROM recipes WHERE code=?", (code,)).fetchone()
    if not r:
        return {"error": "not found"}
    r = dict(r)

    price = has_prices(con)
    msql = f"""
        SELECT m.slot, m.code, m.name, m.icon, m.grade, m.enchant, m.count,
               i.image_file
               {", b.best_price, b.best_market, b.best_updated, b.is_override" if price else ""}
        FROM materials m
        LEFT JOIN items i ON i.code = m.code
        {"LEFT JOIN v_price_best b ON b.item_code = m.code" if price else ""}
        WHERE m.recipe_code=? ORDER BY m.slot
    """
    mats = rows_to_dicts(con.execute(msql, (r["code"],)))

    cost = None
    if price:
        c = con.execute("SELECT * FROM v_recipe_cost WHERE code=?", (r["code"],)).fetchone()
        cost = dict(c) if c else None

    # 완성품 / 콤보 산출물 이미지
    def item_img(c):
        if not c:
            return None
        row = con.execute("SELECT name, image_file FROM items WHERE code=?", (c,)).fetchone()
        return dict(row) if row else None

    return {
        "recipe": r,
        "materials": mats,
        "cost": cost,
        "product": item_img(r.get("product_code")),
        "combo_product": item_img(r.get("combo_product_code")),
    }


def api_item(con, q):
    code = q.get("code", [None])[0]
    if not code:
        return {"error": "code required"}
    item = con.execute("SELECT * FROM items WHERE code=?", (code,)).fetchone()
    if not item:
        return {"error": "not found"}
    item = dict(item)

    prices = []
    if has_prices(con):
        prices = rows_to_dicts(con.execute(
            "SELECT market_type, server_id, race, price, updated_at "
            "FROM prices WHERE item_code=? ORDER BY updated_at DESC LIMIT 40", (code,)))

    used_in = rows_to_dicts(con.execute(
        """SELECT r.code, r.name, r.full_text, r.race_text, m.count
           FROM materials m JOIN recipes r ON r.code=m.recipe_code
           WHERE m.code=? ORDER BY r.class1, r.name LIMIT 200""", (code,)))
    made_by = rows_to_dicts(con.execute(
        "SELECT code, name, full_text, race_text FROM recipes WHERE product_code=?", (code,)))

    return {"item": item, "prices": prices, "used_in": used_in, "made_by": made_by}


def api_set_price(con, data):
    """가격 오버라이드 저장/삭제. 전역(그 아이템을 쓰는 모든 레시피)에 즉시 반영."""
    code = data.get("item_code")
    if code is None:
        return {"error": "item_code required"}
    code = int(code)
    price = data.get("price")
    if price in (None, "", "null"):
        con.execute("DELETE FROM price_overrides WHERE item_code=?", (code,))
    else:
        price = int(float(price))
        if price < 0:
            return {"error": "price must be >= 0"}
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        con.execute(
            "INSERT INTO price_overrides(item_code, price, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(item_code) DO UPDATE SET price=excluded.price, "
            "updated_at=excluded.updated_at",
            (code, price, now))
    con.commit()
    row = con.execute(
        "SELECT best_price, best_market, best_updated, is_override "
        "FROM v_price_best WHERE item_code=?", (code,)).fetchone()
    used = con.execute(
        "SELECT COUNT(*) c FROM materials WHERE code=?", (code,)).fetchone()["c"]
    return {"ok": True, "item_code": code,
            "best": dict(row) if row else None, "applied_to_recipes": used}


# ---------- 캐릭터 조회 (공식 aion2.plaync.com 내부 JSON 엔드포인트 프록시) ----------
# 아이온2는 공식 OpenAPI가 없지만, 공식 사이트가 자기 랭킹/캐릭터 페이지에서 쓰는
# 무인증 JSON 엔드포인트가 공개돼 있다. 서버측에서 대신 호출(CORS 회피)해 그대로 넘긴다.
OFFICIAL_ORIGIN = "https://aion2.plaync.com"
PROFILE_IMG_ORIGIN = "https://profileimg.plaync.com"
OFFICIAL_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://aion2.plaync.com/ko-kr/ranking/combat",
    "Accept": "application/json",
}
# 일부 환경(WSL 등)의 CA 번들이 깨져 있어 검증 실패 시 unverified 로 폴백.
_SSL_VERIFIED = ssl.create_default_context()
_SSL_UNVERIFIED = ssl._create_unverified_context()


def fetch_official(path: str, params: dict):
    url = f"{OFFICIAL_ORIGIN}{path}?{urlencode(params)}"
    req = urllib.request.Request(url, headers=OFFICIAL_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12, context=_SSL_VERIFIED) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            with urllib.request.urlopen(req, timeout=12, context=_SSL_UNVERIFIED) as r:
                return json.loads(r.read().decode("utf-8"))
        raise


# ---------- 스킬 상세 (인벤 스킬시뮬레이터 — 공식 스킬 id 와 동일 키) ----------
# 공식 캐릭터 API 는 스킬 이름·레벨만 준다(설명 없음). 인벤 스킬시뮬레이터의
# getSkillData/<직업코드> 가 스킬 id(=공식 id) 별 설명·레벨별 추가효과(speciality)·
# 레벨별 시전정보(level_data)를 준다. 정적 게임데이터라 프로세스 메모리에 캐시한다.
INVEN_SKILL_API = "https://aion2.inven.co.kr/db/api/skillsimulator/getSkillData"
INVEN_HEADERS = {
    "User-Agent": OFFICIAL_HEADERS["User-Agent"],
    "Referer": "https://aion2.inven.co.kr/db/skillsimulator/",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}
# 공식 className → 인벤 직업코드 (호법성은 인벤 미제공 → 매핑 없음, 자동 폴백)
CLASS_JOB = {"검성": 2, "수호성": 3, "궁성": 4, "살성": 5,
             "정령성": 6, "마도성": 7, "치유성": 8}
_skill_cache = {}
_skill_lock = threading.Lock()


def fetch_inven_json(url):
    req = urllib.request.Request(url, headers=INVEN_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL_VERIFIED) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            with urllib.request.urlopen(req, timeout=15, context=_SSL_UNVERIFIED) as r:
                return json.loads(r.read().decode("utf-8"))
        raise


def skilltree_for_job(job):
    """직업(2~8)별 스킬 데이터(스킬id→상세). 정적이라 메모리 캐시."""
    with _skill_lock:
        if job in _skill_cache:
            return _skill_cache[job]
    data = {}
    try:
        r = fetch_inven_json(f"{INVEN_SKILL_API}/{job}")
        d = r.get("data")
        if isinstance(d, dict):
            data = d
    except Exception:
        data = {}
    with _skill_lock:
        _skill_cache[job] = data
    return data


def skill_detail(s, smap):
    """캐릭터 스킬 1건 + 인벤 스킬데이터 → 툴팁용 상세(현재 레벨 기준)."""
    g = smap.get(str(s.get("id")))
    if not g:
        return None
    lvl = s.get("skillLevel") or 0
    ld = g.get("level_data") or []
    cur = next((e for e in ld if e.get("level") == lvl), (ld[-1] if ld else None))
    return {
        "properties": g.get("properties"),
        "tags": [t.get("name") for t in (g.get("tag") or []) if t.get("name")],
        "max_level": g.get("max_level"),
        "link_type": g.get("link_type"),
        "desc": (cur or {}).get("desc"),
        "attributes": (cur or {}).get("attributes") or [],
        "speciality": g.get("speciality") or [],
    }


def _strip_tags(s):
    return (s or "").replace("<strong>", "").replace("</strong>", "")


def api_char_search(q):
    kw = (q.get("q", [""])[0] or "").strip()
    if not kw:
        return {"list": []}
    data = fetch_official(
        "/ko-kr/api/search/aion2/search/v2/character", {"keyword": kw})
    for it in data.get("list", []):
        it["name"] = _strip_tags(it.get("name"))
        # characterId 는 이미 URL 인코딩(%3D)돼 온다 → 원문(=)으로 정규화해서
        # 프런트→프록시→공식 API 로 넘어갈 때 이중 인코딩이 나지 않게 한다.
        if it.get("characterId"):
            it["characterId"] = unquote(it["characterId"])
        rel = it.get("profileImageUrl") or ""
        if rel.startswith("/"):
            it["profileImageUrl"] = PROFILE_IMG_ORIGIN + rel
    return data


def api_char_detail(q):
    cid = q.get("characterId", [None])[0]
    sid = q.get("serverId", [None])[0]
    if not cid or not sid:
        return {"error": "characterId, serverId required"}
    params = {"lang": "ko", "characterId": cid, "serverId": sid}
    # parse_qs 가 %3D→= 로 디코드 → urlencode 가 다시 %3D 로 인코드(라운드트립 OK)
    info = fetch_official("/api/character/info", params)
    equipment = fetch_official("/api/character/equipment", params)

    # 클릭 없이 한눈에 보이도록 장비별 상세(기본/부가 능력치·마석·신석)를 병렬로 임베드
    items = ((equipment.get("equipment") or {}).get("equipmentList")) or []

    def _detail(it):
        try:
            return fetch_official("/api/character/equipment/item", {
                "id": it.get("id"), "enchantLevel": it.get("enchantLevel", 0),
                "characterId": cid, "serverId": sid, "slotPos": it.get("slotPos")})
        except Exception:
            return None

    if items:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for it, det in zip(items, ex.map(_detail, items)):
                it["detail"] = det

    # 스킬 설명(레벨별 추가효과 등) 임베드 — 인벤 스킬시뮬레이터(공식 id 일치)
    cls = (info.get("profile") or {}).get("className")
    job = CLASS_JOB.get(cls)
    skills = ((equipment.get("skill") or {}).get("skillList")) or []
    if job and skills:
        smap = skilltree_for_job(job)
        for s in skills:
            det = skill_detail(s, smap)
            if det:
                s["detail"] = det

    detail = {"info": info, "equipment": equipment}
    # 화면 노출 기준 JSON 을 한 컬럼에 저장하고 sha256 해시를 함께 반환한다.
    # (sort_keys 로 정규화 → 키 순서 변동만으로 해시가 바뀌지 않게)
    payload = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    try:
        con = char_db()
        try:
            con.execute(
                "INSERT INTO char_cache(k, data, hash, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET data=excluded.data, "
                "hash=excluded.hash, updated_at=excluded.updated_at",
                (f"{sid}:{cid}", payload, h,
                 datetime.now(timezone.utc).isoformat()))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass  # 캐시 저장 실패는 조회 자체를 막지 않는다
    return {"data": detail, "hash": h}


def api_char_cached(q):
    """저장된(이력 있는) 캐릭터 상세를 즉시 반환. 없으면 data=None."""
    cid = q.get("characterId", [None])[0]
    sid = q.get("serverId", [None])[0]
    if not cid or not sid:
        return {"data": None, "hash": None}
    try:
        con = char_db()
        try:
            r = con.execute("SELECT data, hash FROM char_cache WHERE k=?",
                            (f"{sid}:{cid}",)).fetchone()
        finally:
            con.close()
    except Exception:
        r = None
    if not r:
        return {"data": None, "hash": None}
    return {"data": json.loads(r["data"]), "hash": r["hash"]}


def api_char_item(q):
    """장비 아이템 상세(기본/부가 능력치, 마석, 신석, 강화 한계 등)."""
    need = ("id", "enchantLevel", "characterId", "serverId", "slotPos")
    params = {k: (q.get(k, [""])[0] or "") for k in need}
    if not params["id"] or not params["characterId"] or not params["serverId"]:
        return {"error": "id, characterId, serverId required"}
    return fetch_official("/api/character/equipment/item", params)


ROUTES = {
    "/api/meta": api_meta,
    "/api/search": api_search,
    "/api/recipe": api_recipe,
    "/api/item": api_item,
}

# 네트워크(공식 API) 프록시 라우트 — DB 불필요
NET_ROUTES = {
    "/api/char/search": api_char_search,
    "/api/char/detail": api_char_detail,
    "/api/char/cached": api_char_cached,
    "/api/char/item": api_char_item,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 조용히

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache is not None:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        # API 응답은 항상 최신이어야 하므로 캐시 금지
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   cache="no-store")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            if INDEX.exists():
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8",
                           cache="no-store, must-revalidate")
            else:
                self._send(500, b"index.html missing", "text/plain")
            return

        if path.startswith("/images/"):
            name = unquote(path[len("/images/"):])
            f = (IMG_DIR / name).resolve()
            # 디렉토리 이탈 방지
            if IMG_DIR.resolve() in f.parents and f.is_file():
                self._send(200, f.read_bytes(), "image/png")
            else:
                self._send(404, b"", "image/png")
            return

        if path in NET_ROUTES:
            try:
                self._json(NET_ROUTES[path](parse_qs(parsed.query)))
            except Exception as e:
                self._json({"error": str(e)}, 502)
            return

        if path in ROUTES:
            try:
                con = db()
                try:
                    result = ROUTES[path](con, parse_qs(parsed.query))
                finally:
                    con.close()
                self._json(result)
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/set_price":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
                con = db_rw()
                try:
                    result = api_set_price(con, data)
                finally:
                    con.close()
                self._json(result)
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        self._send(404, b"not found", "text/plain")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    if not DB_PATH.exists():
        raise SystemExit("aion2_craft.db 없음 — 먼저 crawl_craft.py 실행")
    ensure_schema()
    ensure_char_cache_schema()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"아이온2 제작 뷰어: http://{args.host}:{args.port}  (Ctrl+C 종료)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")


if __name__ == "__main__":
    main()
