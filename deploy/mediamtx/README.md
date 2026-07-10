# 화면공유 스트리밍 (MediaMTX)

맥북 OBS(아이폰 미러링 캡처) → MediaMTX(dodsas) → aion2 **📱 화면공유** 탭.
aion2 앱과 무관한 별도 인프라라 저장소엔 문서/설정만 두고, 실제 컨테이너는 dodsas 에서 돌린다.

## 서버 컨테이너 (dodsas)

설정 파일을 `/home/dodsas/work/mediamtx/mediamtx.yml` 에 두고(이 폴더의 `mediamtx.yml`
참고, `pass` 는 실제 값으로 교체), 아래로 기동:

```bash
podman run -d --name mediamtx --restart=always \
  -p 1935:1935 -p 8888:8888 -p 8889:8889 -p 8189:8189/udp \
  -v /home/dodsas/work/mediamtx/mediamtx.yml:/mediamtx.yml:Z \
  docker.io/bluenviron/mediamtx:latest
```

> ⚠ 4개 포트를 모두 `-p` 로 매핑해야 한다. WebRTC(8889·8189)를 빼먹으면 저지연 재생이
> 컨테이너 밖으로 안 나온다.

## 라우터 포트포워딩 (dodsas 공유기)

| 포트 | 용도 | 필요 시점 |
|------|------|-----------|
| 1935/tcp | RTMP 수신 (OBS 송출) | 필수 |
| 8888/tcp | HLS 재생 | 필수 |
| 8889/tcp | WebRTC 시그널링(WHEP) | 저지연(WebRTC) 쓸 때 |
| 8189/udp | WebRTC ICE 미디어 | 저지연(WebRTC) 쓸 때 |

## OBS (맥북)

- 소스: **iPhone Mirroring 앱 창** 캡처 (macOS 화면 캡처 → 방식=애플리케이션)
- 방송(사용자 지정):
  - 서버: `rtmp://dodsas.iptime.org:1935`
  - 스트림 키: `iphone?user=publisher&pass=<PASS>`

## aion2 화면공유 탭 URL

- **WebRTC (저지연 ~0.3~1s, 권장)**: `http://dodsas.iptime.org:8889/iphone`
- **HLS (호환·폴백 2~6s)**: `http://dodsas.iptime.org:8888/iphone/index.m3u8`

탭은 URL 이 `.m3u8` 면 HLS, 아니면 WebRTC(WHEP)로 자동 판별한다.
