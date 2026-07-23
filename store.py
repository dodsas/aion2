#!/usr/bin/env python3
"""상태 데이터 저장소 — Turso(libSQL) 또는 로컬 SQLite 자동 선택.

왜 필요한가:
  Render 무료 플랜은 파일시스템이 **비영속**(재배포·재시작 시 초기화)이라 SQLite
  파일에 쓴 값(캐릭터 캐시·내 캐릭터·파티 모집·숙제 등)이 날아간다. 그래서 상태
  데이터는 외부 관리형 libSQL(Turso)에 저장한다.

동작:
  - 환경변수 TURSO_DATABASE_URL + TURSO_AUTH_TOKEN 가 있으면 → Turso(HTTP libSQL).
  - 없으면 → 로컬 SQLite 파일(app_store.db) 로 폴백(오프라인 개발용).
  두 경우 모두 동일한 인터페이스(Store) 를 제공하므로 server.py 는 백엔드를 몰라도 된다.

Turso 접근은 별도 pip 의존성 없이 표준 라이브러리(urllib)로 libSQL 의 HTTP(Hrana v2)
`/v2/pipeline` 엔드포인트를 직접 호출한다 → Docker/Render 배포가 가벼워진다.

스키마(두 백엔드 공통):
  app_kv(k PK, v TEXT[JSON], updated_at)          범용 키-값(JSON) 저장
  char_cache(k PK, data TEXT[JSON], hash, updated_at)  캐릭터 상세 캐시
"""
from __future__ import annotations
import base64
import json
import os
import sqlite3
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# 로컬 폴백 DB 경로. 환경변수 APP_STORE_DB 로 덮어쓸 수 있다(테스트 격리·배치 분리용).
LOCAL_DB = os.environ.get("APP_STORE_DB") or str(BASE_DIR / "app_store.db")

DDL = (
    "CREATE TABLE IF NOT EXISTS app_kv ("
    " k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS char_cache ("
    " k TEXT PRIMARY KEY, data TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL)",
    # 직업 통계(aion2tool 크롤). 배치로 스냅샷을 쌓는다: (captured_at,job,category) 단위 JSON.
    "CREATE TABLE IF NOT EXISTS job_stats ("
    " id INTEGER PRIMARY KEY,"
    " captured_at TEXT NOT NULL,"      # 크롤 시각(KST ISO)
    " job TEXT NOT NULL,"              # 직업명(검성…) 또는 'ALL'
    " category TEXT NOT NULL,"         # population_share|cp_distribution|class_trend|population_trend|overview|arcana
    " source_updated TEXT,"            # 원본(API) last_update
    " data TEXT NOT NULL)",            # 정리된 JSON 페이로드
    "CREATE INDEX IF NOT EXISTS ix_job_stats_cap ON job_stats(captured_at)",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- 로컬 SQLite 백엔드 ----------
class LocalBackend:
    kind = "local"

    def __init__(self, path: str = str(LOCAL_DB)):
        self.path = path
        for ddl in DDL:
            self.exec(ddl)

    def _con(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def query(self, sql, args=()):
        con = self._con()
        try:
            rows = [dict(r) for r in con.execute(sql, args).fetchall()]
            con.commit()
            return rows
        finally:
            con.close()

    def exec(self, sql, args=()):
        con = self._con()
        try:
            con.execute(sql, args)
            con.commit()
        finally:
            con.close()


# ---------- Turso(libSQL) HTTP 백엔드 ----------
class TursoBackend:
    kind = "turso"

    def __init__(self, url: str, token: str):
        u = url.strip()
        # libsql:// · wss:// · ws:// → http(s):// 로 정규화 (HTTP 파이프라인 엔드포인트)
        for pre, rep in (("libsql://", "https://"), ("wss://", "https://"),
                         ("ws://", "http://"), ("http://", "http://"),
                         ("https://", "https://")):
            if u.startswith(pre):
                u = rep + u[len(pre):]
                break
        self.endpoint = u.rstrip("/") + "/v2/pipeline"
        self.token = token
        self._ctx = ssl.create_default_context()
        self._ctx_unverified = ssl._create_unverified_context()
        # DDL 은 생성자에서 실행하지 않는다(웹 기동 시 포트 바인딩 전 네트워크 블로킹 방지).
        # 첫 쿼리 시 1회·단일 파이프라인으로 지연 실행한다.
        self._ready = False

    @staticmethod
    def _enc(v):
        if v is None:
            return {"type": "null", "value": None}
        if isinstance(v, bool):
            return {"type": "integer", "value": str(int(v))}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": v}
        if isinstance(v, (bytes, bytearray)):
            return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode()}
        return {"type": "text", "value": str(v)}

    @staticmethod
    def _dec(cell):
        t = cell.get("type")
        if t == "null":
            return None
        if t == "integer":
            return int(cell.get("value"))
        if t == "float":
            return float(cell.get("value"))
        if t == "blob":
            return base64.b64decode(cell.get("base64") or "")
        return cell.get("value")

    def _pipeline(self, stmts):
        reqs = [{"type": "execute",
                 "stmt": {"sql": sql, "args": [self._enc(a) for a in (args or [])]}}
                for (sql, args) in stmts]
        reqs.append({"type": "close"})
        body = json.dumps({"requests": reqs}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20, context=self._ctx) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                with urllib.request.urlopen(req, timeout=20, context=self._ctx_unverified) as r:
                    data = json.loads(r.read().decode("utf-8"))
            else:
                raise
        out = []
        for res in data.get("results", []):
            if res.get("type") == "error":
                raise RuntimeError("Turso error: " + json.dumps(res.get("error"), ensure_ascii=False))
            resp = res.get("response") or {}
            if resp.get("type") == "execute":
                out.append(resp.get("result") or {})
        return out

    def _ensure(self):
        if self._ready:
            return
        # 모든 DDL 을 한 번의 파이프라인(단일 HTTP 왕복)으로 실행 → 지연·최소 비용.
        self._pipeline([(ddl, ()) for ddl in DDL])
        self._ready = True

    def query(self, sql, args=()):
        self._ensure()
        result = self._pipeline([(sql, args)])[0]
        cols = [c.get("name") for c in result.get("cols", [])]
        return [{cols[i]: self._dec(cell) for i, cell in enumerate(row)}
                for row in result.get("rows", [])]

    def exec(self, sql, args=()):
        self._ensure()
        self._pipeline([(sql, args)])


# ---------- 공통 저장소 ----------
class Store:
    def __init__(self):
        url = os.environ.get("TURSO_DATABASE_URL", "").strip()
        tok = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
        if url and tok:
            self.backend = TursoBackend(url, tok)
        else:
            self.backend = LocalBackend()

    @property
    def kind(self):
        return self.backend.kind

    # 범용 KV(JSON)
    def kv_get(self, k):
        r = self.backend.query("SELECT v FROM app_kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else None

    def kv_set(self, k, v):
        self.backend.exec(
            "INSERT INTO app_kv(k, v, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (k, json.dumps(v, ensure_ascii=False), _now()))

    # 캐릭터 상세 캐시
    def char_get(self, k):
        r = self.backend.query("SELECT data, hash FROM char_cache WHERE k=?", (k,))
        if not r:
            return None
        return {"data": json.loads(r[0]["data"]), "hash": r[0]["hash"]}

    def char_set(self, k, data_json_str, h):
        self.backend.exec(
            "INSERT INTO char_cache(k, data, hash, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET data=excluded.data, hash=excluded.hash, "
            "updated_at=excluded.updated_at",
            (k, data_json_str, h, _now()))

    # 직업 통계(job_stats) — 배치 스냅샷 저장/조회
    def job_stats_write(self, captured_at, rows):
        """한 배치의 여러 (job, category) 행을 동일 captured_at 으로 적재.
        rows: [{job, category, source_updated, data(dict)}...]"""
        for r in rows:
            self.backend.exec(
                "INSERT INTO job_stats(captured_at, job, category, source_updated, data) "
                "VALUES(?,?,?,?,?)",
                (captured_at, r["job"], r["category"], r.get("source_updated"),
                 json.dumps(r["data"], ensure_ascii=False)))

    def job_stats_captures(self, limit=30):
        """최근 크롤 시각 목록(최신순)."""
        return [x["captured_at"] for x in self.backend.query(
            "SELECT DISTINCT captured_at FROM job_stats ORDER BY captured_at DESC LIMIT ?",
            (limit,))]

    def job_stats_latest(self, category=None, job=None):
        """가장 최근 스냅샷의 행들(파싱된 data 포함). category/job 로 필터 가능."""
        caps = self.job_stats_captures(1)
        if not caps:
            return []
        sql = "SELECT captured_at, job, category, source_updated, data FROM job_stats WHERE captured_at=?"
        args = [caps[0]]
        if category:
            sql += " AND category=?"; args.append(category)
        if job:
            sql += " AND job=?"; args.append(job)
        rows = self.backend.query(sql, tuple(args))
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    def job_stats_prune(self, keep=30):
        """최근 keep 개 스냅샷만 남기고 오래된 것 삭제(배치 누적 방지)."""
        caps = self.job_stats_captures(keep + 50)
        if len(caps) > keep:
            cutoff = caps[keep - 1]
            self.backend.exec("DELETE FROM job_stats WHERE captured_at < ?", (cutoff,))


def read_env(path: Path) -> dict:
    """의존성 없이 env 파일을 dict 로 파싱(주석·따옴표 제거). os.environ 은 건드리지 않음."""
    d: dict = {}
    if not path.exists():
        return d
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def load_dotenv(path: Path = BASE_DIR / ".env"):
    """.env 를 os.environ 에 로드(로컬 개발용). 이미 있는 값은 덮지 않음."""
    for k, v in read_env(path).items():
        os.environ.setdefault(k, v)
