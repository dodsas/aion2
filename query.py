#!/usr/bin/env python3
"""
아이온2 제작 DB 간단 조회 CLI.

예:
  python3 query.py fields                     # 제작 분야/종류 목록
  python3 query.py search 창룡왕              # 이름으로 레시피 검색
  python3 query.py recipe 111022015           # 레시피 상세(재료+개수+이미지+시세)
  python3 query.py recipe "빛나는 창룡왕의 대검"  # 이름으로도 가능
  python3 query.py cost "기룡왕의 대검"          # 재료비 추정 요약
"""
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent / "aion2_craft.db"


def con():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def fields():
    c = con()
    print("제작 분야 / 세부 종류 / 레시피 수")
    for r in c.execute(
        "SELECT class1_text, class2_text, COUNT(*) n FROM recipes "
        "GROUP BY class1, class2 ORDER BY class1, class2"
    ):
        print(f"  {r['class1_text']:5} {r['class2_text']:8} {r['n']}")


def search(term):
    c = con()
    rows = c.execute(
        "SELECT code, name, full_text, race_text, grade_text FROM recipes "
        "WHERE name LIKE ? ORDER BY class1, name LIMIT 60",
        (f"%{term}%",),
    ).fetchall()
    if not rows:
        print("결과 없음")
        return
    for r in rows:
        g = f" [{r['grade_text']}]" if r["grade_text"] else ""
        print(f"  {r['code']:>12}  {r['name']}  ({r['full_text']}/{r['race_text']}){g}")
    print(f"\n{len(rows)}건")


def recipe(key):
    c = con()
    if key.isdigit():
        r = c.execute("SELECT * FROM recipes WHERE code=?", (int(key),)).fetchone()
    else:
        r = c.execute("SELECT * FROM recipes WHERE name=? LIMIT 1", (key,)).fetchone()
    if not r:
        print("레시피 없음")
        return
    print(f"■ {r['name']}  ({r['full_text']} / {r['race_text']})")
    print(f"  코드={r['code']}  타입={r['type_text']}  제작등급={r['grade_text']}  "
          f"숙련={r['mastery_level']}")
    if r["combo_product_code"]:
        cp = c.execute("SELECT name FROM items WHERE code=?", (r["combo_product_code"],)).fetchone()
        print(f"  콤보(대성공) 산출: {cp['name'] if cp else r['combo_product_code']}"
              f"  (확률 {r['combo_probability']})")
    print("  ── 필요 재료 ──")
    for m in c.execute(
        "SELECT m.*, i.image_file, b.best_price, b.best_market "
        "FROM materials m LEFT JOIN items i ON i.code=m.code "
        "LEFT JOIN v_price_best b ON b.item_code=m.code "
        "WHERE m.recipe_code=? ORDER BY m.slot",
        (r["code"],),
    ):
        price = m["best_price"]
        cost = f"  = {price*m['count']:>13,} ({m['best_market']})" if price else "  = 시세없음"
        print(f"    {m['name']:24} x{m['count']:<10}{cost}   {m['image_file'] or ''}")


def cost(key):
    """레시피 재료비 추정 요약."""
    c = con()
    where = "code=?" if key.isdigit() else "name=?"
    r = c.execute(
        f"SELECT * FROM v_recipe_cost WHERE code=(SELECT code FROM recipes WHERE {where} LIMIT 1)",
        (int(key) if key.isdigit() else key,),
    ).fetchone()
    if not r:
        print("레시피 없음")
        return
    print(f"■ {r['name']} ({r['full_text']}/{r['race_text']})")
    print(f"  재료비 추정 ≈ {r['material_cost']:,}  (+키나 {r['kina_cost']:,})")
    print(f"  가격 확보 재료: {r['priced_materials']}/{r['total_materials']}"
          + ("  ⚠️ 일부 재료 시세 없음 → 실제보다 과소추정" if r['priced_materials'] < r['total_materials'] else ""))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    arg = " ".join(sys.argv[2:])
    if cmd == "fields":
        fields()
    elif cmd == "search":
        search(arg)
    elif cmd == "recipe":
        recipe(arg)
    elif cmd == "cost":
        cost(arg)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
