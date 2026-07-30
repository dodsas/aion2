# 아르카나 스킬 옵션 데이터 수집

## 목적

직업별 아르카나 카드에 붙을 수 있는 스킬 풀을 조회·표시하기 위한 데이터다. 카드 구분은 성배·양피지·나침반·종·거울·천칭이며, 성배와 천칭은 전체 풀, 나머지 카드는 직업별 고정 풀을 사용한다.

## 출처와 한계

공식 아이온2 페이지에서는 직업별 전체 옵션 목록을 기계가 읽을 수 있는 형태로 공개하지 않는 것을 확인했다. 따라서 공개 아르카나 계산기 페이지의 인라인 데이터 `window.AION2_ARCANA_DATA`를 수집한다.

- 출처: `https://aion2scam.com/page_arcana/`
- 데이터 성격: 비공식 공개 페이지의 내장 데이터
- 주의: 게임 패치 뒤 출처 페이지가 갱신되기 전까지 실제 게임과 차이가 날 수 있다. UI에는 공식 데이터로 표기하지 않는다.

## 수집 방법

`crawl_jobstats.py`가 공개 HTML을 요청한 뒤 `window.AION2_ARCANA_DATA = {...};` 블록을 정규식으로 찾아 JSON으로 파싱한다. 결과는 Turso의 `job_stats` 테이블에 다음 형태로 스냅샷 적재된다.

```text
job       = ALL
category  = arcana_options
data.jobs = { 직업명: { active: { pen, compass }, passive: { bell, mirror } } }
```

카드와 풀의 대응은 다음과 같다.

| 카드 | 옵션 풀 |
| --- | --- |
| 성배, 천칭 | 모든 액티브·패시브 옵션 |
| 양피지 | `active.pen` |
| 나침반 | `active.compass` |
| 종 | `passive.bell` |
| 거울 | `passive.mirror` |

수동 갱신은 아래 명령으로 한다. `.env`에 `TURSO_DATABASE_URL`과 `TURSO_AUTH_TOKEN`이 있어야 한다.

```bash
python3 crawl_jobstats.py --arcana-options-only
```

일반 직업 통계 크롤도 같은 옵션 스냅샷을 함께 수집한다.
