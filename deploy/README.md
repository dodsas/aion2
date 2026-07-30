# 배포 (Jenkins → Podman, git push 자동배포)

yclaude 와 **동일한 방식**이다. `main` 에 push 하면 Jenkins 파이프라인이:

```
git archive → scp 로 dodsas 서버 전송 → 압축 해제 → deploy/deploy.sh 실행
  → podman-compose build & up → 헬스체크 → 오래된 이미지 정리
```

- **대상 서버**: `dodsas@dodsas.iptime.org:22311`
- **작업 디렉토리**: `/home/dodsas/work/aion2`
- **접속 URL**: `http://dodsas.iptime.org:9092` (yclaude=9091 과 겹치지 않게 9092)
- 앱은 **파이썬 표준 라이브러리 전용** → 의존성 설치 없음. 이미지만 이미지에 포함되며,
  제작 데이터는 Turso 백업에서 조회 스냅샷으로 복원된다.

## 파일 구성

| 파일 | 역할 |
|------|------|
| `Dockerfile` | `python:3.12-slim` + curl(헬스체크). `server.py` 실행 |
| `compose.yml` | 단일 컨테이너, `9092:8770`, rootless podman(keep-id), restart always |
| `Jenkinsfile` | 파이프라인 (githubPush + pollSCM 트리거) |
| `deploy/deploy.sh` | 호스트에서 build+up+헬스체크+이미지정리 |
| `.dockerignore` | 배포메타·런타임 파일 이미지 제외 |

## Jenkins 잡 등록 (최초 1회)

yclaude 잡이 이미 있으니 **SSH 크리덴셜(`ysadmin-deploy-ssh`)·GitHub 웹훅·podman 은
그대로 재사용**된다. aion2 잡만 새로 만들면 된다.

1. Jenkins → **New Item** → 이름 `aion2` → **Pipeline** → OK
2. **Pipeline** 섹션:
   - Definition: **Pipeline script from SCM**
   - SCM: **Git**, Repository URL: `https://github.com/dodsas/aion2.git`
   - Branch: `*/main`
   - Script Path: `Jenkinsfile`
3. **Build Triggers**: `GitHub hook trigger for GITScm polling` 체크
   (Jenkinsfile 안에도 `githubPush()`+`pollSCM` 이 있어 SCM 폴링 백업됨)
4. Save → **Build Now** 로 최초 배포 확인

> GitHub 웹훅: 리포 → Settings → Webhooks 에 `<JENKINS>/github-webhook/` 가
> 없으면 추가(yclaude 리포에 이미 있으면 그 형식을 그대로 사용).

## 이후 배포

```bash
git push origin main   # 끝. Jenkins 가 자동으로 빌드·배포한다.
```

Jenkins 잡 콘솔에서 로그 확인. 수동 배포가 필요하면 서버에서 직접:

```bash
cd /home/dodsas/work/aion2 && HOST_PORT=9092 bash deploy/deploy.sh
```

## DB 갱신

크래프트 데이터는 `crawl_craft.py`(또는 `update.sh`)로 갱신하면 조회 스냅샷과 Turso 백업이
함께 갱신된다. 데이터 SQLite 파일은 Git에 커밋하지 않는다.

## 참고 · 보안

- `/api/set_price` 는 무인증 쓰기 엔드포인트다. 시세 오버라이드는 조회 스냅샷과 Turso에
  함께 기록하므로 컨테이너 재배포 뒤에도 유지된다.
- 포트를 직접 노출한다(yclaude 와 동일). 도메인/HTTPS/인증이 필요하면 호스트의
  리버스 프록시 뒤에 두면 된다.
