#!/usr/bin/env python3
"""제작 데이터 로컬 스냅샷 캐시 ↔ Turso 동기화.

서버 조회 경로는 ``cache/craft-snapshot.sqlite3``다. Turso는 영속 백업 및
배포/재시작 후 스냅샷 복원 원본으로 사용한다.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from store import CRAFT_DDL, Store, load_dotenv


BASE_DIR = Path(__file__).resolve().parent
SNAPSHOT_PATH = Path(os.environ.get("CRAFT_SNAPSHOT_PATH", BASE_DIR / "cache" / "craft-snapshot.sqlite3"))
TABLES = {
    "recipes": ("code", "name", "race", "race_text", "type", "type_text", "class1", "class1_text",
                "class2", "class2_text", "full_text", "grade", "grade_text", "mastery_grade",
                "mastery_level", "cost_gold", "combo_probability", "product_code", "combo_product_code",
                "sort_order", "raw_json"),
    "items": ("code", "name", "icon", "grade", "image_file"),
    "materials": ("recipe_code", "slot", "code", "name", "icon", "grade", "enchant", "count"),
    "prices": ("id", "item_id_raw", "item_code", "item_name", "server_id", "market_type", "race",
               "price", "updated_at", "source"),
    "price_overrides": ("item_code", "price", "updated_at"),
}


def chunks(values, size=1000):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _store():
    load_dotenv()
    store = Store()
    if store.kind != "turso":
        raise RuntimeError("제작 스냅샷은 Turso 연결이 필요합니다.")
    store.ensure_craft_schema()
    return store


def _init_sqlite(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(";\n".join(CRAFT_DDL) + ";")
    con.commit()
    return con


def snapshot_exists(path: Path = SNAPSHOT_PATH) -> bool:
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        n = con.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
        con.close()
        return n > 0
    except sqlite3.Error:
        return False


def pull_snapshot(path: Path = SNAPSHOT_PATH):
    """Turso 전체 제작 데이터를 새 SQLite 스냅샷으로 받은 후 원자적으로 교체한다."""
    store = _store()
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    con = _init_sqlite(tmp)
    try:
        for table, cols in TABLES.items():
            rows = store.backend.query(f"SELECT {','.join(cols)} FROM {table}")
            if rows:
                marks = ",".join("?" for _ in cols)
                con.executemany(f"INSERT INTO {table}({','.join(cols)}) VALUES ({marks})",
                                [tuple(r[c] for c in cols) for r in rows])
            print(f"{table}: {len(rows)}행 복원")
        con.commit()
    finally:
        con.close()
    os.replace(tmp, path)
    return path


def ensure_snapshot(path: Path = SNAPSHOT_PATH):
    """실제 조회 캐시가 없을 때만 Turso 백업에서 복원한다."""
    if not snapshot_exists(path):
        pull_snapshot(path)
    return path


def push_snapshot(path: Path = SNAPSHOT_PATH):
    """크롤 결과 스냅샷을 Turso 백업으로 전체 교체 후 행 수를 검증한다."""
    if not snapshot_exists(path):
        raise RuntimeError(f"유효한 제작 스냅샷이 없습니다: {path}")
    store = _store()
    source = sqlite3.connect(path)
    source.row_factory = sqlite3.Row
    try:
        store.backend.exec_many([("DELETE FROM " + t, ()) for t in
                                 ("materials", "recipes", "items", "prices", "price_overrides")])
        expected = {}
        for table, cols in TABLES.items():
            rows = source.execute(f"SELECT {','.join(cols)} FROM {table}").fetchall()
            expected[table] = len(rows)
            marks = "(" + ",".join("?" for _ in cols) + ")"
            for batch in chunks(rows):
                sql = f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES " + ",".join([marks] * len(batch))
                args = tuple(value for row in batch for value in (row[c] for c in cols))
                store.backend.exec(sql, args)
    finally:
        source.close()
    actual = {t: store.backend.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in TABLES}
    mismatch = {t: (expected[t], actual[t]) for t in TABLES if expected[t] != actual[t]}
    if mismatch:
        raise RuntimeError("제작 스냅샷 검증 실패: " + repr(mismatch))
    print("Turso 백업 동기화 완료:", actual)


def sync_price_override(item_code: int, price: int | None):
    """가격 오버라이드만 Turso에 즉시 write-through 한다."""
    store = _store()
    store.backend.exec("DELETE FROM price_overrides WHERE item_code=?", (item_code,))
    store.backend.exec("DELETE FROM prices WHERE item_code=? AND source='user_override'", (item_code,))
    if price is None:
        return
    item = store.backend.query("SELECT name FROM items WHERE code=?", (item_code,))
    if not item:
        raise RuntimeError("item not found")
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    store.backend.exec(
        "INSERT INTO prices(item_id_raw,item_code,item_name,server_id,market_type,race,price,updated_at,source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (f"api_{item_code}", item_code, item[0]["name"], None, "world", None, price, now, "user_override"))
    store.backend.exec("INSERT INTO price_overrides(item_code,price,updated_at) VALUES(?,?,?)",
                       (item_code, price, now))


def main():
    ap = argparse.ArgumentParser(description="제작 스냅샷 캐시와 Turso 백업 동기화")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--pull", action="store_true", help="Turso 백업 → 로컬 스냅샷 복원")
    group.add_argument("--push", action="store_true", help="로컬 스냅샷 → Turso 백업 반영")
    ap.add_argument("--path", type=Path, default=SNAPSHOT_PATH)
    args = ap.parse_args()
    if args.pull:
        pull_snapshot(args.path)
    else:
        push_snapshot(args.path)


if __name__ == "__main__":
    main()
