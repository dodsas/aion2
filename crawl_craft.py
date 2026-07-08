#!/usr/bin/env python3
"""
아이온2 제작(장비) 레시피 크롤러 + 로컬 SQLite DB 빌더.

데이터 출처: 인벤 아이온2 DB 비공식 JSON API
  https://aion2.inven.co.kr/db/api/craft/getList?class1=<분야코드>
  - class1: 1=대장(무기), 2=갑옷, 3=세공, 4=연금, 5=요리
  - 한 번의 호출로 양 종족(천족/마족) 전체 레시피를 반환
아이콘 이미지:
  https://static.inven.co.kr/image_2011/site_image/aion2/itemicon/<icon>.png

공식 OpenAPI는 없으며(2026-07 기준), 위 API는 인벤 프론트엔드가 사용하는
비공식 엔드포인트를 그대로 호출하는 방식이다.

사용법:
  python3 crawl_craft.py            # 크롤 + DB 구축 + 이미지 다운로드
  python3 crawl_craft.py --no-images  # 이미지 제외
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import ssl
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# WSL 등 일부 환경의 시스템 CA 인증서 결함(key usage 확장 누락)으로
# 기본 검증이 실패하므로 검증을 끈 컨텍스트를 사용한다. (공개 데이터 조회 목적)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "aion2_craft.db"
IMG_DIR = BASE_DIR / "images"
RAW_DIR = BASE_DIR / "raw"

API = "https://aion2.inven.co.kr/db/api/craft/getList"
IMG_BASE = "https://static.inven.co.kr/image_2011/site_image/aion2/itemicon/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
REFERER = "https://aion2.inven.co.kr/db/craft/"

# 시세 소스: aion2craft.com 이 사용하는 Supabase(PostgREST) 공개 테이블.
# anon 키는 프론트엔드 번들에 그대로 노출되어 있으며 익명 read 가 허용돼 있다.
# ⚠️ 시세는 커뮤니티가 입력하는 값이라 서버/월드/종족/시점별로 편차가 크다(수동 보정 전제).
SB_URL = "https://tdtvqtqpnfdilbbslkgv.supabase.co"
SB_ANON = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6"
           "InRkdHZxdHFwbmZkaWxiYnNsa2d2Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjcwMDQ1"
           "MDUsImV4cCI6MjA4MjU4MDUwNX0.4wKq2BUhd4_Czu-GaAX6FghQLOnN5eXWiacJ8EA__jU")
PRICE_SOURCE = "aion2craft"

# 인벤 craft.js 내부 매핑 (그대로 반영)
CLASS1_FIELDS = {1: "대장", 2: "갑옷", 3: "세공", 4: "연금", 5: "요리"}
TYPE_TEXT = {1: "세트", 2: "제작"}
GRADE_TEXT = {1: "일반", 2: "희귀", 3: "전승", 4: "유일", 5: "영웅", 6: "신화", 7: "스페셜"}
RACE_TEXT = {1: "천족", 2: "마족"}

# ── 시세 스키마 (server.py 와 공유) ──────────────────────────────
# 수동 보정용 '가격 오버라이드'. crawl 재실행 시에도 보존(load_prices 가 지우지 않음).
PRICE_OVERRIDES_DDL = """
CREATE TABLE IF NOT EXISTS price_overrides (
    item_code  INTEGER PRIMARY KEY,   -- items.code
    price      INTEGER NOT NULL,      -- 사용자가 지정한 전역 시장가
    updated_at TEXT
);
"""

# 대표가(v_price_best): 아이템 코드별 대표 시세 1건.
# 정책 = 오버라이드가 있으면 최우선, 없으면 world 시장 우선 → 최신 updated_at.
V_PRICE_BEST_SQL = """
CREATE VIEW v_price_best AS
WITH ranked AS (
    SELECT p.*,
           ROW_NUMBER() OVER (
               PARTITION BY item_code
               ORDER BY (market_type='world') DESC, updated_at DESC
           ) AS rn
    FROM prices p
    WHERE item_code IS NOT NULL
),
mbest AS (
    SELECT item_code, price AS best_price, market_type AS best_market,
           server_id AS best_server, race AS best_race, updated_at AS best_updated
    FROM ranked WHERE rn = 1
)
SELECT
    COALESCE(o.item_code, m.item_code)              AS item_code,
    COALESCE(o.price, m.best_price)                 AS best_price,
    CASE WHEN o.item_code IS NOT NULL THEN 'override'
         ELSE m.best_market END                     AS best_market,
    m.best_server                                   AS best_server,
    m.best_race                                     AS best_race,
    COALESCE(o.updated_at, m.best_updated)          AS best_updated,
    CASE WHEN o.item_code IS NOT NULL THEN 1 ELSE 0 END AS is_override
FROM mbest m
FULL OUTER JOIN price_overrides o ON o.item_code = m.item_code;
"""

# 레시피별 재료비 추정. '키나(통합)' 재료는 그 자체가 비용이므로 별도 합산.
V_RECIPE_COST_SQL = """
CREATE VIEW v_recipe_cost AS
SELECT
    r.code, r.name, r.full_text, r.race_text, r.grade_text,
    SUM(CASE WHEN m.name='키나(통합)' THEN m.count ELSE 0 END) AS kina_cost,
    SUM(CASE WHEN m.name<>'키나(통합)' THEN m.count*COALESCE(b.best_price,0) ELSE 0 END) AS material_cost,
    SUM(CASE WHEN m.name<>'키나(통합)' AND b.best_price IS NOT NULL THEN 1 ELSE 0 END) AS priced_materials,
    SUM(CASE WHEN m.name<>'키나(통합)' THEN 1 ELSE 0 END) AS total_materials
FROM recipes r
JOIN materials m ON m.recipe_code = r.code
LEFT JOIN v_price_best b ON b.item_code = m.code
GROUP BY r.code;
"""


def http_get(url: str, retries: int = 3, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return r.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last = e
            time.sleep(1.0 + i)
    raise RuntimeError(f"GET failed {url}: {last}")


def crawl() -> list[dict]:
    """class1=1..5 를 모두 받아 code 기준으로 dedup."""
    RAW_DIR.mkdir(exist_ok=True)
    by_code: dict[int, dict] = {}
    for c1, field in CLASS1_FIELDS.items():
        url = f"{API}?class1={c1}"
        payload = json.loads(http_get(url))
        if not payload.get("success"):
            print(f"  [warn] class1={c1} success=false", file=sys.stderr)
            continue
        data = payload.get("data", [])
        (RAW_DIR / f"craft_class1_{c1}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        for rec in data:
            by_code[rec["code"]] = rec
        print(f"  class1={c1} ({field}): {len(data)}건")
        time.sleep(0.4)
    recipes = list(by_code.values())
    print(f"총 레시피(dedup): {len(recipes)}건")
    return recipes


def collect_items(recipes: list[dict]) -> dict[int, dict]:
    """product / combo_product / material 에 등장하는 모든 아이템을 code 기준 카탈로그화."""
    items: dict[int, dict] = {}

    def add(obj: dict | None):
        if not obj or not obj.get("code"):
            return
        c = obj["code"]
        cur = items.get(c, {})
        # 이름/아이콘이 채워진 쪽을 우선
        items[c] = {
            "code": c,
            "name": obj.get("name") or cur.get("name"),
            "icon": obj.get("icon") or cur.get("icon"),
            "grade": obj.get("grade") if obj.get("grade") is not None else cur.get("grade"),
        }

    for r in recipes:
        add(r.get("product"))
        add(r.get("combo_product"))
        for m in r.get("material", []) or []:
            add(m)
    return items


def build_db(recipes: list[dict], items: dict[int, dict]):
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript(
        """
        PRAGMA journal_mode=WAL;

        CREATE TABLE recipes (
            code            INTEGER PRIMARY KEY,   -- 레시피(=완성품) 코드
            name            TEXT NOT NULL,
            race            INTEGER,               -- 1 천족 / 2 마족
            race_text       TEXT,
            type            INTEGER,               -- 1 세트 / 2 제작
            type_text       TEXT,
            class1          INTEGER,               -- 제작 분야
            class1_text     TEXT,                  -- 대장/갑옷/세공/연금/요리
            class2          INTEGER,               -- 세부 종류 코드
            class2_text     TEXT,                  -- 대검/투구/반지 ...
            full_text       TEXT,                  -- "대장-대검"
            grade           INTEGER,
            grade_text      TEXT,                  -- 제작 등급(입문/전문 ...)
            mastery_grade   INTEGER,
            mastery_level   INTEGER,
            cost_gold       INTEGER,               -- 제작 비용(키나)
            combo_probability REAL,
            product_code    INTEGER,               -- 완성품 아이템 코드
            combo_product_code INTEGER,            -- 콤보(대성공) 산출물 코드
            sort_order      INTEGER,
            raw_json        TEXT                   -- 원본 레코드 전체
        );

        CREATE TABLE materials (
            recipe_code INTEGER NOT NULL REFERENCES recipes(code),
            slot        INTEGER NOT NULL,
            code        INTEGER NOT NULL,          -- 재료 아이템 코드
            name        TEXT,
            icon        TEXT,
            grade       INTEGER,
            enchant     INTEGER,
            count       INTEGER,                   -- 필요 개수
            PRIMARY KEY (recipe_code, slot)
        );

        CREATE TABLE items (
            code       INTEGER PRIMARY KEY,        -- 아이템(완성품/재료 공통) 코드
            name       TEXT,
            icon       TEXT,
            grade      INTEGER,
            image_file TEXT                        -- 로컬 이미지 상대경로 (images/<icon>.png)
        );

        CREATE INDEX idx_mat_recipe ON materials(recipe_code);
        CREATE INDEX idx_mat_code   ON materials(code);
        CREATE INDEX idx_rec_class  ON recipes(class1, class2);
        CREATE INDEX idx_rec_race   ON recipes(race);
        """
    )

    for r in recipes:
        prod = r.get("product") or {}
        combo = r.get("combo_product") or {}
        cur.execute(
            """INSERT INTO recipes VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                r["code"], r.get("name"),
                r.get("race"), RACE_TEXT.get(r.get("race")),
                r.get("type"), TYPE_TEXT.get(r.get("type")),
                r.get("class1"), r.get("class1_text") or CLASS1_FIELDS.get(r.get("class1")),
                r.get("class2"), r.get("class2_text"),
                r.get("full_text"),
                r.get("grade"), r.get("grade_text"),
                r.get("mastery_grade"), r.get("mastery_level"),
                r.get("cost_gold"),
                r.get("combo_probability"),
                prod.get("code"), combo.get("code"),
                r.get("order"),
                json.dumps(r, ensure_ascii=False),
            ),
        )
        for m in r.get("material", []) or []:
            cur.execute(
                "INSERT OR REPLACE INTO materials VALUES (?,?,?,?,?,?,?,?)",
                (r["code"], m.get("slot"), m.get("code"), m.get("name"),
                 m.get("icon"), m.get("grade"), m.get("enchant"), m.get("count")),
            )

    for it in items.values():
        icon = it.get("icon")
        img_file = f"images/{icon}.png" if icon else None
        cur.execute(
            "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?)",
            (it["code"], it.get("name"), icon, it.get("grade"), img_file),
        )

    con.commit()
    # 요약
    n_rec = cur.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    n_mat = cur.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
    n_item = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    con.close()
    print(f"DB 저장 완료: recipes={n_rec}, materials={n_mat}, items={n_item} -> {DB_PATH}")


def crawl_prices() -> list[dict]:
    """aion2craft Supabase market_prices 전량(페이지네이션) 수집."""
    RAW_DIR.mkdir(exist_ok=True)
    rows: list[dict] = []
    step = 1000
    for off in range(0, 100000, step):
        url = (f"{SB_URL}/rest/v1/market_prices?select=*&order=id"
               f"&offset={off}&limit={step}")
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "apikey": SB_ANON,
            "Authorization": f"Bearer {SB_ANON}"})
        for i in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
                    part = json.loads(r.read())
                break
            except Exception as e:
                if i == 2:
                    raise RuntimeError(f"price GET failed {url}: {e}")
                time.sleep(1.0 + i)
        rows += part
        if len(part) < step:
            break
        time.sleep(0.3)
    (RAW_DIR / "market_prices.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    print(f"시세 {len(rows)}건 수집 (출처: {PRICE_SOURCE}/supabase)")
    return rows


def load_prices(prices: list[dict]):
    """prices 테이블 및 비용계산 뷰를 (재)생성. recipes/items 는 건드리지 않음."""
    if not DB_PATH.exists():
        raise RuntimeError("먼저 레시피 DB를 생성하세요 (--prices-only 아닌 전체 실행).")
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript(
        """
        DROP VIEW IF EXISTS v_price_best;
        DROP VIEW IF EXISTS v_recipe_cost;
        DROP TABLE IF EXISTS prices;
        -- price_overrides 는 수동 보정값이므로 보존(지우지 않음).
        CREATE TABLE prices (
            id          INTEGER PRIMARY KEY,   -- supabase row id
            item_id_raw TEXT,                  -- 'api_<code>' 또는 'item_<한글명>'
            item_code   INTEGER,               -- api_ 접두면 코드, 아니면 NULL (items.code 조인)
            item_name   TEXT,                  -- item_ 접두면 한글명, 아니면 NULL (items.name 조인)
            server_id   TEXT,                  -- 'server_XXXX' 또는 NULL(월드)
            market_type TEXT,                  -- 'server' | 'world'
            race        TEXT,                  -- 'elyos'|'asmodian'|NULL
            price       INTEGER,
            updated_at  TEXT,
            source      TEXT
        );
        CREATE INDEX idx_price_code ON prices(item_code);
        CREATE INDEX idx_price_name ON prices(item_name);
        """
    )
    for p in prices:
        raw = p.get("item_id") or ""
        code = None
        name = None
        if raw.startswith("api_"):
            rest = raw[4:]
            code = int(rest) if rest.isdigit() else None
        elif raw.startswith("item_"):
            name = raw[5:].replace("_", " ")
        cur.execute(
            "INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?,?,?,?)",
            (p.get("id"), raw, code, name, p.get("server_id"),
             p.get("market_type"), p.get("race"), p.get("price"),
             p.get("updated_at"), PRICE_SOURCE),
        )

    cur.executescript(PRICE_OVERRIDES_DDL)
    cur.executescript(V_PRICE_BEST_SQL)
    cur.executescript(V_RECIPE_COST_SQL)
    con.commit()
    n = cur.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    matched = cur.execute(
        "SELECT COUNT(DISTINCT p.item_code) FROM prices p "
        "JOIN items i ON i.code=p.item_code").fetchone()[0]
    con.close()
    print(f"prices 저장: {n}건 / 아이템코드 매칭 {matched}종. 뷰: v_price_best, v_recipe_cost")


def download_images(items: dict[int, dict], workers: int = 8):
    IMG_DIR.mkdir(exist_ok=True)
    icons = sorted({it["icon"] for it in items.values() if it.get("icon")})
    todo = [ic for ic in icons if not (IMG_DIR / f"{ic}.png").exists()]
    print(f"이미지 대상 {len(icons)}종, 신규 다운로드 {len(todo)}종")
    ok = fail = 0

    def dl(icon: str):
        dst = IMG_DIR / f"{icon}.png"
        try:
            data = http_get(f"{IMG_BASE}{icon}.png")
            dst.write_bytes(data)
            return True, icon
        except Exception as e:
            return False, f"{icon}: {e}"

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(dl, ic) for ic in todo]
        for i, f in enumerate(as_completed(futs), 1):
            success, info = f.result()
            if success:
                ok += 1
            else:
                fail += 1
                print(f"  [img fail] {info}", file=sys.stderr)
            if i % 50 == 0:
                print(f"  ...{i}/{len(todo)}")
    print(f"이미지 완료: 성공 {ok}, 실패 {fail} (기존 보유 {len(icons) - len(todo)})")


def main():
    ap = argparse.ArgumentParser(description="아이온2 제작 레시피 + 시세 크롤러")
    ap.add_argument("--no-images", action="store_true", help="이미지 다운로드 생략")
    ap.add_argument("--no-prices", action="store_true", help="시세 수집 생략")
    ap.add_argument("--prices-only", action="store_true",
                    help="레시피/이미지는 건너뛰고 시세만 갱신(자주 실행용)")
    args = ap.parse_args()

    if args.prices_only:
        print("시세만 갱신...")
        load_prices(crawl_prices())
        print("완료.")
        return

    print("1) 레시피 크롤링...")
    recipes = crawl()
    print("2) 아이템 카탈로그 수집...")
    items = collect_items(recipes)
    print(f"   고유 아이템 {len(items)}종")
    print("3) SQLite DB 구축...")
    build_db(recipes, items)
    if not args.no_images:
        print("4) 이미지 다운로드...")
        download_images(items)
    if not args.no_prices:
        print("5) 시세 수집...")
        load_prices(crawl_prices())
    print("완료.")


if __name__ == "__main__":
    main()
