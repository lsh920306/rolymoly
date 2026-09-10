# 한국 가까운 서버 배포 준비

이 구성은 현재 Python 앱 전체를 사용자가 확보한 서울 또는 가까운 지역의 Linux 서버 한 대에 올리기 위한 배포 초안입니다. 서버 구입, 계정 생성, 외부 배포, DNS 변경은 실행하지 않았습니다. 월 예산 0원 조건에서는 현재 Community Cloud를 유지합니다. 사용할 수 있는 서버와 실제 왕복 지연을 확인한 뒤 이전 여부를 결정하며, 특정 유료 상품이나 무료 서비스의 지속 제공을 전제하지 않습니다.

사용자의 최종 접속 조건은 평소 60명 이하, 최대 80명입니다. 별도 웹 서버 없이 입찰 경로를 DB 쪽으로 옮기는 후보는 [첨부 호스팅 검토안 점검](AUCTION_HOSTING_REVIEW.md)에 정리했습니다. 현재 자체 WebSocket 구성과 Supabase Realtime·브라우저 직접 RPC 후보를 구분하며, 후자는 이번 수정본에 구현하거나 배포하지 않았습니다.

브라우저 → HTTPS 프록시 → 동일한 Rolymoly 앱 → 기존 Supabase 경로를 사용합니다. 화면, `/api/auction/bid`, `/api/auction/live`, `/api/auction/ws`를 같은 호스트에 둡니다. 별도 프런트엔드, 교차 출처 API, DB RPC 재작성은 필요하지 않습니다. 지역 배치는 입찰 접수 왕복을 줄이기 위한 후보이며, p95 200ms나 80명 화면 반영 시간을 보장하는 설정은 아닙니다.

## 포함한 실행 구성

| 파일 | 역할 |
| --- | --- |
| `Dockerfile` | Python 3.11, 기존 고정 의존성, UID/GID 10001, 명시적 소스 복사, HTTP 상태 검사 |
| `.dockerignore` | 런타임 소스 허용 목록, 비밀 설정·DB·로컬 산출물 제외 |
| `compose.yaml` | 앱 한 개, 재시작 정책, 읽기 전용 루트와 Secrets 연결, 필요한 쓰기 공간, 로컬 포트 |

실행 명령은 `python -m roly.server`입니다. 기존 실행기가 상시 마감 워커를 시작하고 `streamlit_app.py`를 실행합니다. 워커와 앱은 같은 저장소를 사용합니다. 앱 프로세스와 입찰 접수 기록은 한 개로 유지합니다. replica를 늘리거나 여러 웹 워커를 추가하는 구성은 이 검증 범위에 없습니다. 외부 프로세스가 같은 DB를 변경할 때 상태 전달은 커밋 알림과 버전 재조회로 복구합니다.

컨테이너는 `init: true`, `SIGINT`, 종료 유예 30초를 사용합니다. `unless-stopped`는 종료된 컨테이너의 재시작 정책이고, 상태가 `unhealthy`가 되었다는 이유만으로 자동 재시작하지는 않습니다. Secrets는 기존 호스트 파일을 읽기 전용으로 연결하며 파일이 없으면 실행을 중단합니다. 이 항목들의 동작은 [Docker Compose 서비스 문서](https://docs.docker.com/reference/compose-file/services/)를 기준으로 작성했습니다.

## Secrets와 저장소 준비

실제 설정은 소스 폴더 밖의 절대 경로(예: `/etc/rolymoly/secrets.toml`)에 둡니다. `.streamlit/secrets.toml.example` 형식을 참고하되 비밀값을 Dockerfile, Compose 환경값, 명령행 인수, Git, 소스 ZIP에 넣지 않습니다. 이미지에는 `.streamlit/config.toml`만 복사합니다. `.dockerignore`는 빌드 컨텍스트 자체에서 제외할 파일을 정합니다. [Docker 빌드 컨텍스트 문서](https://docs.docker.com/build/concepts/context/#dockerignore-files)

설정 파일은 컨테이너의 UID/GID 10001이 읽을 수 있어야 합니다. Linux에서 소유자 `10001:10001`, 권한 `0400`으로 관리하는 방식 등을 사용할 수 있습니다. 읽기 권한을 확인할 때 파일 내용을 터미널에 출력하지 않습니다. 준비 단계에서는 별도 검수 Supabase 프로젝트의 연결 정보를 사용하고 `[riot] allow_demo = false`를 유지합니다. 검수와 운영 계정·DB를 구분합니다.

Compose가 `ROLYMOLY_DATABASE_TARGET=supabase://rolymoly`를 명시하므로 쓰기 폴더를 지정해도 SQLite로 전환되지 않습니다. `/var/lib/rolymoly`의 named volume은 앱 로컬 파일용이며 Supabase DB 백업을 대신하지 않습니다. DB 백업과 복구는 기존 [배포 안내](../DEPLOYMENT.md)의 별도 절차를 따릅니다.

## 배포 전에 실행할 검증

현재 작업 환경에는 Docker CLI가 없어 이미지 빌드·Compose 실행은 확인하지 못했습니다. 아래는 서버 확보 후 별도 검수 환경에서 실행할 순서입니다. 현재 Cloud가 자동 배포하는 `main`과 독립된 체크아웃 또는 검토용 소스 ZIP에서 진행합니다.

1. Docker Engine과 Compose를 설치한 Linux 검수 서버를 준비합니다. 소스와 Secrets 파일 경로를 구분하고 이미지 태그를 검수할 버전으로 지정합니다. 기반 `python:3.11-slim-bookworm` 태그는 갱신될 수 있으므로 최종 검수 시 실제 이미지 digest도 기록합니다.
2. 소스 루트에서 아래 설정 검사와 빌드를 실행합니다. `config --quiet`는 Compose 형식 검사이며 DB 연결이나 앱 실행을 하지 않습니다. 이미지 빌드는 네트워크에서 기반 이미지와 의존성을 내려받습니다.

```bash
export ROLYMOLY_SECRETS_FILE=/etc/rolymoly/secrets.toml
export ROLYMOLY_APP_ENVIRONMENT=test
export ROLYMOLY_IMAGE_TAG=review-20260910
docker compose config --quiet
docker compose build app
docker compose run --rm --no-deps --entrypoint python app scripts/check_supabase.py --check-config
docker compose run --rm --no-deps --entrypoint python app scripts/check_supabase.py
```

3. 별도 검수 DB임을 확인하고 앱을 시작합니다. 앱 시작은 DB 초기화·마이그레이션과 마감 워커 실행을 포함할 수 있습니다. 운영 DB로 향한 설정으로 먼저 실행하지 않습니다.

```bash
docker compose up -d app
docker compose ps
curl --fail http://127.0.0.1:8501/_stcore/health
```

상태 검사는 Streamlit HTTP 응답 여부를 확인합니다. DB 쓰기 성공, 상시 워커 정상 동작, 로그인, WebSocket 연결까지 보증하지 않습니다. Streamlit도 Docker 예시에서 `/_stcore/health`를 사용합니다. [Streamlit Docker 배포 안내](https://docs.streamlit.io/deploy/tutorials/docker)

4. 다음 HTTPS 프록시 설정 후 한국의 실제 브라우저로 로그인, 팀장 입찰 ACK, 관전자 반영, pause·resume·reset·정정, 마지막 1초 경쟁, 재접속과 서버 재시작을 검수합니다. 같은 UUID의 확인 요청은 새 입찰로 바뀌면 안 됩니다. 브라우저를 모두 닫은 동안 마감이 진행되는지도 확인합니다.
5. 팀장을 포함한 평소 60명 이하와 최대 80명 조건을 나눠 클릭→접수 확정 표시와 클릭→다른 화면의 가격·마감 반영을 각각 측정합니다. 정상 입찰과 마감 직전 동시 입찰을 나누고 평균·p95·p99·최대·1초 초과·시간 초과·누락 수를 기록합니다. 목표는 버튼 반응 100ms, 확정 p95 200ms, 상대 반영 p95 300ms, 두 서버 연동 구간 p99 500ms입니다. 재접속·느린 통신망, DB 대기, 프록시 오류, CPU·메모리, 전달 모드도 별도로 확인합니다. 로컬 합성 테스트와 실제 한국→검수 서버→DB 결과를 구분합니다.
6. 이 결과와 이용 가능한 서버의 유지 조건을 검토한 뒤 운영 전환을 결정합니다. 전환이 결정되면 진행 중 경매를 일시정지하고 기존 앱 이용을 정리한 다음 새 앱 한 곳으로 접속을 모읍니다. 운영 앱 두 곳에 같은 경매의 새 입찰을 동시에 보내는 방식은 사용하지 않습니다. 새 도메인에서는 다시 로그인해야 하며, 서버 재시작 시 기존 페이지도 재접속 안내를 따릅니다.

## HTTPS와 WebSocket 프록시

Compose의 8501 포트는 서버의 `127.0.0.1`에만 연결합니다. 서버 호스트에서 실행하는 HTTPS 프록시가 이를 전달하도록 구성합니다. 프록시가 별도 컨테이너라면 루프백 주소를 그대로 쓰지 말고 전용 Docker 네트워크와 `app:8501` 주소로 조정해야 합니다.

아래는 인증서와 도메인을 준비한 후 적용할 Nginx 설정 예시입니다. `/api/auction/ws`와 Streamlit의 `/_stcore/stream` 모두 업그레이드해야 하므로 앱 전체 경로를 전달합니다. `Host`는 공개 호스트를 유지하고 `Origin`과 앱 세션 헤더를 제거하지 않습니다. 캐시와 요청 자동 재전송은 비활성화합니다.

```nginx
# http 블록 안
map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}

server {
    listen 443 ssl;
    server_name auction.example.com;
    ssl_certificate /etc/letsencrypt/live/auction.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/auction.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 75s;
        proxy_send_timeout 75s;
        proxy_buffering off;
        proxy_cache off;
        proxy_next_upstream off;
    }
}
```

인증서 발급·갱신과 80→443 전환은 서버의 기존 운영 방식에 맞춰 별도로 설정합니다. 프록시의 `Upgrade`·`Connection` 전달과 유휴 연결 시간 설정은 [Nginx WebSocket 문서](https://nginx.org/en/docs/http/websocket.html)를 참고했습니다. 앱의 2초 heartbeat는 상태 확인용이고, 프록시 설정이나 DB 장애를 대체하지 않습니다.

경로 앞에 별도 접두사를 붙이는 배포는 이 예시에서 검수하지 않았습니다. 새 서버는 루트 경로로 배포하고, Community Cloud 전용 `/~/+/` 접두사를 Nginx에 수동 추가하지 않습니다. CORS/XSRF 보호 설정도 끄지 않습니다.

## 중단과 복구

검수 컨테이너만 중단할 때는 `docker compose stop app`을 사용합니다. 배포 전환 후 문제가 발견되면 진행 중 경매와 미확정 입찰부터 확인하고 이전에 검증한 이미지 또는 기존 Cloud로 접속을 정리합니다. 코드를 되돌리는 것과 DB 스키마를 되돌리는 것은 별개이므로 DB 파일·named volume·공유 스키마를 삭제하는 복구 명령은 포함하지 않습니다.

이 파일과 Docker 구성을 소스 검토·ZIP에 추가해도 현재 Cloud의 실행 명령과 자동 배포 연결은 바뀌지 않습니다. 현재 Cloud 유지와 지역 서버 검수 결과를 각각 기록합니다.
