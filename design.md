# UI Design Notes

## Item Grade Colors

아이템 등급 색은 제작 데이터의 숫자 등급과 공식 캐릭터 API의 영문 등급명을 같은 의미로 맞춰 사용한다.

| 의미 | 제작 등급 변수 | 공식 API 등급명 | 색상 |
|---|---:|---|---|
| 일반 | `--g1` | `Common`, `Normal` | `#a7b4b7` |
| 희귀 | `--g2` | `Rare` | `#79df9e` |
| 전승 | `--g3` | `Superior`, `Legend`, `Legendary` | `#9fdcff` |
| 유일 | `--g4` | `Unique` | `#ffd84a` |
| 영웅 | `--g5` | `Epic`, `Heroic` | `#ff5f68` |
| 신화 | `--g6` | `Mythic` | `#c6a7ff` |
| 스페셜 | `--g7` | `Special` | `#74f0df` |
| 미지정/폴백 | - | unknown | `var(--dim)` |

### Rules

- 전승 아이템은 연한 파란색 `#9fdcff`를 사용한다.
- 영웅 아이템은 붉은색 `#ff5f68`를 사용한다.
- 공식 캐릭터 API에서 보리뚜의 `순례자의 브로치`는 `grade: "Legend"`이지만 `gradeName: "전승"`으로 내려온다. 따라서 `Legend`, `Legendary`는 전승색에 매핑한다.
- 공식 캐릭터 API의 실제 장비 응답은 영웅을 주로 `Epic`으로 내려준다. `Heroic`은 호환용 별칭으로 같은 붉은색에 매핑한다.
- 장비 카드, 리스트 카드, 아이콘 프레임, 등급 강조선은 같은 등급 색을 공유한다.
- `index.html`의 `:root` 등급 변수와 `GRADEC` 매핑을 함께 수정해야 제작 탭과 캐릭터 장비 탭의 색이 어긋나지 않는다.
- 장비의 수치형 옵션(`subStats`)과 스킬형 옵션(`subSkills`)은 화면에서 모두 `조율`로 묶어 표시한다.
- 아르카나는 마석/조율 개념을 쓰지 않는다. `mainStats`를 주 능력치로 보여주고, `subSkills`가 있으면 `아르카나 스킬` 패널로 별도 표시한다.
