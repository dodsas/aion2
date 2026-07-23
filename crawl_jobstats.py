#!/usr/bin/env python3
"""아이온2 직업 통계 크롤러 (aion2tool.com) — 별도 기능·배치 대응.

수집 대상 (https://aion2tool.com/statistics/jobstats · 세부: /jobstats/<직업>):
  1) 직업 점유율        /api/statistics?min_power=..&max_power=..&min_cp2=..  (job_stats)
  2) 직업별 전투력 분포  /api/statistics/combat-power-by-class                  (buckets+분위수)
  3) 직업별 인구 추이    /api/statistics/job/timeseries?min_cp2=..
  4) 전체 인구 추이      /api/statistics/population
  5) 직업별 개요         /api/stats/class-overview?job=<직업>   (세트옵션·날개·상위랭커)
  6) 직업별 아르카나     /api/stats/arcana-skills?job=<직업>    (아르카나 스킬 채용률)

특징:
  - Cloudflare 앞단이 있으나 브라우저 헤더(User-Agent+Referer+Accept)만으로 통과된다
    → 순수 표준 라이브러리(urllib)로 크롤(크롤러 전용 의존성 없음).
  - 결과를 정리(normalize)해 store.py 의 job_stats 테이블에 (captured_at, job, category)
    스냅샷으로 적재한다. 배치/cron 으로 주기 실행하면 최신 데이터가 쌓인다.
  - 저장 대상(로컬 SQLite/Turso)은 store.Store() 가 환경변수로 자동 선택한다.

실행:
  python3 crawl_jobstats.py                 # .env 기준 저장소에 최신 스냅샷 적재
  python3 crawl_jobstats.py --dry-run       # 저장 없이 수집 요약만 출력
  python3 crawl_jobstats.py --prune 30      # 적재 후 최근 30개 스냅샷만 유지
  APP_STORE_DB=/tmp/x.db python3 crawl_jobstats.py   # 임시 DB 로 격리 실행
"""
from __future__ import annotations
import argparse
import json
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

from store import Store, load_dotenv

BASE = "https://aion2tool.com"
JOBSTATS_REF = f"{BASE}/statistics/jobstats"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
KST = timezone(timedelta(hours=9))
_CTX_V = ssl.create_default_context()
_CTX_U = ssl._create_unverified_context()


def fetch(path: str, referer: str = JOBSTATS_REF) -> dict:
    """aion2tool JSON 엔드포인트 호출. 일부 환경의 CA 문제 시 unverified 폴백."""
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json",
        "Accept-Language": "ko-KR,ko;q=0.9", "Referer": referer})
    try:
        with urllib.request.urlopen(req, timeout=25, context=_CTX_V) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            with urllib.request.urlopen(req, timeout=25, context=_CTX_U) as r:
                return json.loads(r.read().decode("utf-8"))
        raise


# ---------- 개별 데이터 정리(normalize) ----------
def collect():
    """모든 통계를 수집해 (job, category, source_updated, data) 행 리스트로 정리."""
    rows = []

    # 1) 직업 점유율 + 총계
    share = fetch("/api/statistics?min_power=0&max_power=10000&min_cp2=10000").get("data", {})
    jobs_list = [j for j in share.get("job_stats", []) if j.get("job") and j.get("job") != "-"]
    rows.append({"job": "ALL", "category": "population_share",
                 "source_updated": share.get("last_updated"),
                 "data": {"jobs": share.get("job_stats", []),
                          "total_count": share.get("total_count"),
                          "filtered_count": share.get("filtered_count"),
                          "bounds": share.get("bounds"), "bounds_cp2": share.get("bounds_cp2"),
                          "last_updated": share.get("last_updated")}})
    job_names = [j["job"] for j in jobs_list]

    # 2) 직업별 전투력 분포(분위수 포함)
    cp = fetch("/api/statistics/combat-power-by-class").get("data", {})
    cp_meta = {k: cp.get(k) for k in
               ("bucket_width", "top_percent", "sample_window_days",
                "min_bucket_samples", "sample_since_utc", "last_updated")}
    for job, cd in (cp.get("class_data") or {}).items():
        rows.append({"job": job, "category": "cp_distribution",
                     "source_updated": cp.get("last_updated"),
                     "data": {"buckets": cd.get("buckets", []),
                              "sample_total": cd.get("sample_total"),
                              "sample_total_raw": cd.get("sample_total_raw"),
                              "top_threshold": cd.get("top_threshold"), **cp_meta}})

    # 3) 직업별 인구 추이(직업 시계열)
    ts = fetch("/api/statistics/job/timeseries?min_cp2=10000")
    rows.append({"job": "ALL", "category": "class_trend",
                 "source_updated": (ts.get("latest") or {}).get("date"),
                 "data": {"dates": ts.get("dates"), "series_by_job": ts.get("chart_series"),
                          "jobs": ts.get("jobs"), "latest": ts.get("latest"),
                          "min_cp2": ts.get("min_cp2")}})

    # 4) 전체 인구 추이
    pop = fetch("/api/statistics/population")
    rows.append({"job": "ALL", "category": "population_trend",
                 "source_updated": (pop.get("latest") or {}).get("date"),
                 "data": {"dates": pop.get("dates"), "series": pop.get("series"),
                          "latest": pop.get("latest"),
                          "sample_window_days": pop.get("sample_window_days")}})

    # 5)/6)/7) 직업별 스킬·스티그마 채용률 + 개요 + 아르카나 (직업 수만큼)
    for job in job_names:
        ref = f"{JOBSTATS_REF}/{quote(job)}"
        # 7) 스킬/스티그마 통계 (active/passive/stigma 각 채용률·평균레벨·티어분포)
        try:
            sk = fetch(f"/api/stats/skills?job={quote(job)}", ref)
            skd = sk.get("data", {})
            rows.append({"job": job, "category": "skills",
                         "source_updated": sk.get("last_update"),
                         "data": {"active": skd.get("active", []),
                                  "passive": skd.get("passive", []),
                                  "stigma": skd.get("stigma", []),
                                  "total_rankers": sk.get("total_rankers"),
                                  "valid_rankers": sk.get("valid_rankers")}})
        except Exception as e:
            print(f"  ! {job} skills 실패: {e}")
        try:
            ov = fetch(f"/api/stats/class-overview?job={quote(job)}", ref).get("data", {})
            rows.append({"job": job, "category": "overview",
                         "source_updated": ov.get("last_update"),
                         "data": {"set_options": ov.get("set_options", []),
                                  "set_sample": ov.get("set_sample"),
                                  "wings": ov.get("wings", []),
                                  "wing_sample": ov.get("wing_sample"),
                                  "top_rankers": ov.get("top_rankers", [])}})
        except Exception as e:
            print(f"  ! {job} overview 실패: {e}")
        try:
            ar = fetch(f"/api/stats/arcana-skills?job={quote(job)}", ref)
            rows.append({"job": job, "category": "arcana",
                         "source_updated": ar.get("last_update"),
                         "data": {"arcana": ar.get("data", {}),
                                  "total_rankers": ar.get("total_rankers")}})
        except Exception as e:
            print(f"  ! {job} arcana 실패: {e}")
        time.sleep(0.4)  # 예의상 간격

    return job_names, rows


def main():
    ap = argparse.ArgumentParser(description="아이온2 직업 통계 크롤러")
    ap.add_argument("--dry-run", action="store_true", help="저장하지 않고 수집 요약만 출력")
    ap.add_argument("--prune", type=int, default=0, help="적재 후 최근 N개 스냅샷만 유지(0=미적용)")
    args = ap.parse_args()

    load_dotenv()
    captured_at = datetime.now(KST).isoformat(timespec="seconds")
    print(f"[{captured_at}] 직업 통계 수집 시작 …")
    job_names, rows = collect()
    print(f"직업 {len(job_names)}종, 정리된 행 {len(rows)}개 "
          f"({', '.join(sorted({r['category'] for r in rows}))})")

    if args.dry_run:
        for r in rows:
            n = len(r["data"]) if isinstance(r["data"], dict) else 0
            print(f"  - {r['category']:<17} job={r['job']:<6} src={r.get('source_updated')}")
        return

    store = Store()
    store.job_stats_write(captured_at, rows)
    if args.prune:
        store.job_stats_prune(args.prune)
    print(f"저장 완료 → 저장소: {store.kind} (captured_at={captured_at})")


if __name__ == "__main__":
    main()
