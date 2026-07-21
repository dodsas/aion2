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
LOCAL_DB = BASE_DIR / "app_store.db"

DDL = (
    "CREATE TABLE IF NOT EXISTS app_kv ("
    " k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS char_cache ("
    " k TEXT PRIMARY KEY, data TEXT NOT NULL, hash TEXT NOT NULL, updated_at TEXT NOT NULL)",
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
        for ddl in DDL:
            self.exec(ddl)

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

    def query(self, sql, args=()):
        result = self._pipeline([(sql, args)])[0]
        cols = [c.get("name") for c in result.get("cols", [])]
        return [{cols[i]: self._dec(cell) for i, cell in enumerate(row)}
                for row in result.get("rows", [])]

    def exec(self, sql, args=()):
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


def load_dotenv(path: Path = BASE_DIR / ".env"):
    """의존성 없이 .env 를 os.environ 에 로드(로컬 개발용). 이미 있는 값은 덮지 않음."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
