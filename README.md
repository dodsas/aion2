# 아이온2 제작(장비) 레시피 + 시세 로컬 DB

아이온2 제작 레시피(필요 **재료·개수·이미지**, 완성품, 콤보 산출물)와
**거래소 시세**를 크롤링해 로컬 **SQLite** 파일로 만든 데이터셋.

## 데이터 출처

**① 레시피/재료/이미지 — 인벤 아이온2 DB (비공식 JSON API)**
- `https://aion2.inven.co.kr/db/api/craft/getList?class1=<분야>`
- 인벤 웹 프론트엔드(Svelte)가 쓰는 내부 엔드포인트를 그대로 호출.
- `class1`: `1`=대장(무기), `2`=갑옷, `3`=세공, `4`=연금, `5`=요리. 한 번 호출로 **천족·마족** 모두 반환.
- 아이콘: `https://static.inven.co.kr/image_2011/site_image/aion2/itemicon/<icon>.png`

**② 시세 — aion2craft.com 백엔드 (Supabase / PostgREST 공개 테이블)**
- `https://<project>.supabase.co/rest/v1/market_prices` (anon 키는 프론트 번들에 노출, 익명 read 허용).
- `item_id`가 `api_<인벤코드>` 형태라 **①의 아이템 코드와 그대로 조인**된다.
- ⚠️ **커뮤니티 수기 입력값**이다. 서버별/월드/종족/시점 편차가 크고, 없는 아이템도 많다. **최종 수동 보정 전제.**

> ⚠️ 아이온2 **공식 OpenAPI는 없다**(2026-07-08 재확인). NC가 **PLAYNC 개발자센터**
> (`https://developers.plaync.com`)에 게임 데이터 오픈 API를 열긴 했으나, 현재
> **리니지2M만** 지원(아이템 정보·시세·검색)하고 **아이온2는 미포함**이다. "대상 게임을
> 점차 확대"한다고 밝혔으니 추후 아이온2가 추가되면 시세 소스를 여기로 교체할 수 있다.
> questlog.gg는 Cloudflare + tRPC라 접근이 까다롭고, 레시피는 어차피 게임 데이터라
> 인벤과 동일하므로 사용하지 않았다.

## 구성 파일

| 경로 | 설명 |
|------|------|
| `crawl_craft.py` | 크롤러 + DB 빌더 (레시피 재생성 / 이미지 증분 / 시세 갱신) |
| `query.py` | DB 조회 CLI |
| `server.py` | 로컬 웹 뷰어 서버 (검색 UI + JSON API) |
| `index.html` | 검색 화면(단일 파일, 서버가 서빙) |
| `update.sh` | cron용 갱신 래퍼 (`prices`/`full`, flock+로그) |
| `aion2_craft.db` | SQLite DB (약 3.9MB) |
| `images/` | 아이템 아이콘 PNG (80×80, 497종) |
| `raw/` | 원본 API 응답 스냅샷 (레시피 분야별 + market_prices) |
| `logs/` | cron 갱신 로그 |

## 사용

```bash
python3 crawl_craft.py               # 전체: 레시피+이미지+시세
python3 crawl_craft.py --no-images   # 이미지 제외
python3 crawl_craft.py --prices-only # 시세만 갱신(레시피/이미지 유지)

python3 query.py fields                    # 분야/종류별 레시피 수
python3 query.py search 창룡왕             # 이름 검색
python3 query.py recipe "기룡왕의 대검"      # 상세: 재료+개수+이미지+시세
python3 query.py cost "기룡왕의 대검"        # 재료비 추정 요약
```

### 웹 화면(검색 UI)

```bash
python3 server.py            # http://127.0.0.1:8770 접속
python3 server.py --port 9000
```

- **아이템 이름 검색**(실시간) + 분야(대장/갑옷/세공/연금/요리)·종족 필터.
- "재료도 검색" 체크 시 완성품뿐 아니라 **재료 아이템**까지 검색.
- 레시피 클릭 → 완성품·필요 재료(아이콘+개수)·**단가/합계/재료비 추정·커버리지**·콤보 산출물.
- **보유량 입력**: 재료별 보유 수량을 입력하면 부족분(필요−보유)만으로 재료비를 실시간 재계산.
  보유량은 브라우저(localStorage)에 아이템 단위로 저장되어 레시피가 바뀌어도 유지된다.
- **시장가 인라인 수정(전역 적용)**: 단가 셀을 클릭 → 값 입력 → 저장 시 백엔드 `price_overrides`에
  기록되어 **그 아이템을 쓰는 모든 레시피에 즉시 반영**된다. 빈 값으로 저장하면 원복(크롤 시세로 복귀).
- 재료/아이템 클릭 → **거래소 시세 이력**(서버·월드·종족·수집일) + **이 아이템을 쓰는 레시피** 역추적.
- 등급별 색상, 다크 테마, 헤더 중앙정렬. 외부 의존성 0.

의존성: **Python 3 표준 라이브러리만** 사용(`sqlite3`, `urllib`, `http.server`). 별도 설치 불필요.
(SQLite = 서버 불필요·파일 하나로 완결되는 가장 가벼운 DB.)

## 스키마

### `recipes` — 레시피 1건 = 완성품 1개
`code`(PK), `name`, `race`/`race_text`(1 천족·2 마족), `type`/`type_text`(1 세트·2 제작),
`class1`/`class1_text`(대장·갑옷·세공·연금·요리), `class2`/`class2_text`(대검·투구·반지…),
`full_text`(예 "대장-대검"), `grade`/`grade_text`, `mastery_grade`, `mastery_level`,
`cost_gold`, `combo_probability`, `product_code`(완성품), `combo_product_code`(대성공 산출),
`sort_order`, `raw_json`(원본 레코드 전체).

### `materials` — 레시피별 필요 재료
`recipe_code`(FK), `slot`, `code`(재료 아이템), `name`, `icon`, `grade`, `enchant`,
**`count`(필요 개수)**. PK = (`recipe_code`, `slot`).

### `items` — 아이템 카탈로그(완성품·재료 공통)
`code`(PK), `name`, `icon`, `grade`, `image_file`(`images/<icon>.png` 상대경로).

### `prices` — 거래소 시세 (aion2craft/Supabase)
`id`(PK, supabase 행), `item_id_raw`, `item_code`(items 조인 키), `item_name`(코드 없는 행용),
`server_id`, `market_type`(server/world), `race`(elyos/asmodian/NULL), `price`, `updated_at`, `source`.
한 아이템에 서버·월드·시점별 **여러 행**이 존재.

### `price_overrides` — 수동 지정 시장가(전역)
`item_code`(PK), `price`, `updated_at`. 화면에서 단가를 수정하면 여기에 기록된다.
**크롤(`--prices-only`/`full`)을 다시 돌려도 보존**되며, 시세 뷰에서 최우선 적용된다.

### 뷰
- `v_price_best` — 아이템 코드별 **대표가 1건**. 정책: **오버라이드 최우선** → `world` 시장 → 최신 `updated_at`.
  `is_override`=1이면 사용자가 지정한 값. `best_market`이 `override`로 표시됨.
- `v_recipe_cost` — 레시피별 `material_cost`(재료비 추정), `kina_cost`(키나 재료),
  `priced_materials`/`total_materials`(가격 확보 커버리지). 커버리지<100%면 **과소추정**.
  (화면의 재료비는 보유량까지 반영해 별도로 다시 계산한다.)

> 자동 대표가 정책이 마음에 안 들면 `crawl_craft.py`의 `V_PRICE_BEST_SQL`(`ORDER BY` 절)만
> 고치면 된다. 특정 서버 고정 등으로 바꿀 수 있고, 개별 아이템은 화면에서 오버라이드로 덮어쓰면 된다.

## 자동 갱신 (cron)

`update.sh`가 flock(중복 방지)+로그를 처리한다. 아래를 crontab에 등록하면
**시세 6시간마다 / 레시피·이미지 전체 주 1회** 갱신된다:

```cron
5 */6 * * * /home/ysnam/projects/aion2/update.sh prices
10 4 * * 1 /home/ysnam/projects/aion2/update.sh full
```

설치(직접 실행): 셸에서 아래 한 줄 —
```bash
( crontab -l 2>/dev/null | grep -v 'aion2/update.sh'; \
  echo '5 */6 * * * /home/ysnam/projects/aion2/update.sh prices'; \
  echo '10 4 * * 1 /home/ysnam/projects/aion2/update.sh full' ) | crontab -
```
로그는 `logs/update.log`. 수동 갱신은 `./update.sh prices` 또는 `./update.sh full`.

## 알아둘 점

- **제작에 드는 키나(게임 재화)는 `recipes.cost_gold`가 아니라
  `materials`의 "키나(통합)" 행 `count`로 표현**된다(예: `100000000`). 실제 재료비 합산 시
  이 행을 비용으로 처리할 것.
- 현재 규모: 레시피 **1,354**건 / 재료행 **6,952**건 / 고유 아이템 **1,649**종 / 이미지 **497**종.
- 게임 업데이트로 레시피가 늘면 `crawl_craft.py`를 다시 실행하면 된다.
- 이 DB에는 **아이템 시세(가격)는 없다.** 재료 개수 × 시세로 제작 비용을 내려면
  별도의 시세 소스가 필요하나, 아이온2는 시세 공식 API가 없어 현재는 수동/커뮤니티
  데이터에 의존해야 한다.
# aion2
