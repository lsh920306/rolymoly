# PostgreSQL 연결과 배포 상태 — 2026-09-09

> 과거 원자료·일회성 스크립트는 사용자의 정리 요청으로 삭제되었습니다. 과거 검증 절의 인라인 경로는 이력으로 보존합니다. 이번 백업·워커 복구 근거는 아래 해당 절에 따로 명시합니다.

**운영 저장소는 Supabase PostgreSQL 하나이며 현재 앱 코드의 스키마 버전은 6입니다.** 계정·회원·두 티어·내전·경매·점수·업적과 Riot 조회 캐시·작업·요청 한도를 같은 DB에서 관리합니다.

## 현재 배포 상태

2026-09-09에 확인한 상태입니다. **사용자 승인 후 최신 커밋 `56f6c13`을 `main`과 `rolymoly-test`에 반영했고, Cloud의 06:17:44 UTC `Updated app` 및 정식 재시작을 확인했습니다.** 첫 배포 `a3a091d`, 중간 개선본 `cf92f09`에 이어 입찰 금액·팝업 경합 수정과 경매 3단 배치·원본 로고가 배포됐습니다. 금액·팝업 수정본의 **고유 테스트 606개가 모두 통과**했고, 이후 화면 변경의 영향 검증 35개도 통과했습니다. 브라우저 연결 복구 후 **최신 Cloud 초기화 이후의 새 10 P 직접 입찰·오른쪽 기록 표시·낙찰·팀원 추가·1,000→990 P 차감**을 확인했고, 읽기 전용 PostgreSQL 조회로 대조했습니다.

최신 화면 변경은 **왼쪽 팀 현황 / 가운데 경매·입찰 / 오른쪽 입찰 기록**의 3단 배치와 사용자 원본 로고 [static/rolymoly-logo.png](static/rolymoly-logo.png)의 200px 너비 표시입니다. 이 변경은 아래 606개 전체 검증 이후에 추가했으므로 관련 검증을 분리합니다. 로컬 실제 브라우저 확인과 자동 영향 검증 35개를 완료했고, Cloud에서도 3단 배치와 200px 로고를 확인했습니다.

1920×855 로컬 브라우저에서 세 패널의 상단 위치가 같고 너비 약 346/773/310px, 가로 넘침 없음, 흰 배경·진한 글씨의 입찰 기록을 확인했습니다. 로고는 200×67.53px로 잘리지 않았고, 전체 팀 팝업은 720×798.70px·내부 스크롤 없음·닫기 1회 뒤 닫힘 유지였습니다. 이 배치 확인에서는 새 입찰이나 Riot 조회를 하지 않았습니다. [로컬 배치·로고 확인](test-artifacts/auction-layout-logo-browser.json)

새로 전달받은 1차 코드 검토 Markdown과 추가 4개 확인 항목은 읽기 전용으로 검토했습니다. 비밀번호 방식·결과 승인·DB 분리 정책을 새로 구현하거나 변경하지 않았습니다. 특히 ‘종료 후 운영진 승인 시 점수·업적 공식 반영’은 제안이며, 현재 구현은 주최자 또는 관리자의 경기 결과 저장/경매 종료 시 반영합니다. 이번 화면 변경으로 Riot API를 추가 호출하지 않았고 회원 22명·계정 23개·저장된 Riot 프로필 20개를 유지합니다.

| 항목 | 현재 확인된 상태 |
| --- | --- |
| GitHub 저장소 | `lsh920306/rolymoly` |
| 검수·배포 브랜치 | `main`과 `rolymoly-test` 모두 `56f6c13` 반영. Cloud 업데이트와 정식 재시작 확인 |
| 실제 Cloud 앱 | `rolymoly-test.streamlit.app` → 위 저장소의 `main` / `streamlit_app.py` |
| 확인한 Cloud 빌드 | Python **3.11.16**, Streamlit **1.63**로 실제 실행 확인 |
| Cloud Secrets | Supabase 연결 및 `[app] environment = "test"`, `[riot] allow_demo = false` 설정 완료. 비밀값은 문서에 포함하지 않음 |
| Cloud 확인 완료 | 비로그인·개인회원·관리자 전체 페이지와 권한 동선, 720px 전체 팀 팝업·26px 제목·4팀 스크롤 없는 표시, 3단 배치·200px 로고. 재시작 직후 기존 일시정지·990 P 보존과 이후 초기화 뒤 새 Cloud 직접 10 P 입찰·낙찰·990 P 차감을 각각 확인 |
| 보존한 공유 자료 | 회원 22명·계정 23개·저장된 Riot 프로필 20개와 기존 준비 경매 1번 보존 |
| Cloud 경매 추가 검수 | 별도 임시 이벤트 2번의 초기화 이후 새 Cloud 입찰 1건·낙찰·배정·차감 확인. 사용자 승인 후 임시 이벤트와 하위 기록만 정리 완료. 여러 실제 기기의 마지막 1초 경쟁·지연/부하 검수는 별도 |
| 최신 수정 소스 | 입찰 금액·팝업 경합 수정, 경매 3단 배치와 사용자 원본 로고 200px 표시를 `56f6c13`으로 배포 |
| 추가 수정본 검증 | 3단 배치·로고 변경 전 고유 606개 전체 통과·542.563초·소스 140개 해시 일치. 이후 영향 검증 35개 통과·93.640초·대상 소스 140개 변경 없음. 실제 PG 연결풀 13조건·핵심 서비스 17단계와 임시 QA 정리 확인 |

3단 배치·로고 추가 전 마지막 전체 검증은 **고유 606개 통과·542.563초·누락/중복/예상외/미완료/테스트 오류/스킵 0**입니다. 소스 140개는 실행 전후와 최종 재확인에서 SHA-256이 같았습니다. 4번 실행기의 진행 JSON 교체가 Windows `WinError 5`로 8개 통과 뒤 중단되어 원래 종료 코드 1과 로그를 보존했습니다. 제품 코드를 바꾸지 않고 미실행 138개만 재개해 통과했으며, 전체 테스트 ID 대조로 606개를 확정했습니다. [606개 전체 검증](test-artifacts/deployment-interaction-final-summary.json)

3단 배치·로고 추가 후 **관련 테스트 35개가 93.640초에 모두 통과**했고 종료 코드 0, 검사 대상 소스 140개의 전후 변경 0을 확인했습니다. 606개 전체 검증과 이후 35개 영향 검증을 구분하여 보존합니다. [최종 화면 영향 검증](test-artifacts/deployment-logo-three-column-focused-report.json)

추가 요청한 입찰 시간 상한은 기존 구현대로 **선택한 5/10/15/20/25/30초**를 사용합니다. 유효 입찰마다 남은 시간에 5초를 더하되 해당 경매의 선택 시간을 넘지 않습니다. 관련 기존 테스트 **9개를 다시 실행해 통과**했고, 실행 전체 34.063초·소스 140개 변경 없음·외부 DB/Riot 호출 0건을 확인했습니다. 이 확인을 위해 제품 코드를 추가 변경하지 않았습니다. [선택 시간 상한 확인](test-artifacts/deployment-selected-bid-time-focused-report.json)

중간 개선본의 **전체 599개 테스트가 462.953초에 통과**했고 테스트 ID 누락·추가·중복은 없었습니다. 검사 대상 소스 138파일 중 실행 도중 `static/app.css`만 바뀌었으므로 당시 전체 실행의 `source_unchanged=false` 근거를 보존했습니다. 그 후 CSS의 **컴포넌트 17개를 1.172초에 재검증해 통과**했습니다. 이 이력은 위 최신 606개 검증과 구분합니다. [당시 전체 결과](test-artifacts/deployment-cloud-final-verification.json), [당시 CSS 결과](test-artifacts/deployment-cloud-final-css-recheck-report.json)

Cloud 배포 전의 전체 585개·마지막 수정 영향 54개와 더 이전의 575개·565개 검증은 해당 시점 소스의 완료 이력으로 유지합니다. [페이지 검수 결과](BROWSER_PAGE_REVIEW.md), [배포 준비 검증 이력](DEPLOYMENT_VERIFICATION_REPORT.md)

연결풀은 새 임시 PostgreSQL 스키마에서 동시 연결 제한·재사용·스키마/읽기 전용 설정 격리·끊어진 연결 복구를 **13조건**으로 확인했습니다. 별도의 새 QA 스키마에서는 인증·동시 입찰·마감·포인트·일반내전·경매 보상을 **17단계, 48.703초**에 검증했고 실행 전후 소스 138파일 해시와 공용 회원·이벤트 1번의 해시가 일치했습니다. 두 검증의 임시 스키마는 정리했으며 실제 Riot API를 호출하지 않았습니다. [연결풀 결과](test-artifacts/deployment-postgres-pool.json), [핵심 서비스 결과](test-artifacts/cloud-pool-domain-postgres.json)

## 체험 검증에서 운영 배포로 전환

`[app] environment`는 앱의 배포 모드입니다. **검수 배포 `test`와 정식 운영 `production`은 Workspace·체험 계정·체험 데이터 생성을 서버에서 차단하고 공용 Supabase만 사용합니다.** `test`에는 ‘검수용’ 배지가 표시되고 `production`에는 표시되지 않습니다. 모드를 생략하면 `production`이며, 잘못된 값이나 DB 설정 오류는 화면 실행을 중단합니다.

| 모드 | 화면·계정 | 저장소 |
| --- | --- | --- |
| `test` | 검수용 배지, 실제 가입·승인·로그인, 체험 없음 | 배포자가 지정한 공용 Supabase |
| `production` | 실제 가입·승인·로그인, 체험 없음 | 배포자가 지정한 공용 Supabase |
| `local` | 개발용 Workspace와 체험 계정 사용 가능 | 설정한 DB와 세션별 체험 SQLite |

모드 이름이 DB 프로젝트를 자동으로 분리하지는 않습니다. 검수에 사용할 Supabase 프로젝트를 Secrets에서 선택해야 하며, 같은 연결 설정을 넣은 앱과 브라우저는 같은 회원·경매 자료를 봅니다. 체험 데이터는 운영 회원·점수로 이관하지 않습니다. 이전 체험 세션을 배포 모드로 전환하면 토큰·DB 경로·폼을 비우고, 같은 공용 DB의 정상 로그인은 유지합니다.

체험 데이터 버전은 **5**(`roly/demo.py`의 `DEMO_DATA_VERSION`)이며 운영 스키마 버전 **6**과는 다릅니다. 제공된 Riot ID 22명 중 성공한 21명의 공개 티어·숙련도 스냅샷을 사용하고, 체험용 점수·클랜 티어·경기는 별도 예시입니다. 이 파일을 읽는 것만으로 Riot API를 다시 호출하지 않습니다.

`[riot] allow_demo`는 로컬 체험의 Riot 조회를 허용하는 별도 옵션입니다. **검수·운영 Cloud Secrets에는 `allow_demo = false`를 유지하세요.** 실제 회원의 수동 갱신은 별도로 설정한 API 키로 사용합니다. 로컬 비밀 설정을 그대로 복사하지 말고 모드와 체험 허용 값을 확인합니다. [공식 Cloud Secrets 안내](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)

## 검수 앱의 Cloud 설정

현재 연결값과 저장 완료 여부는 위 [현재 배포 상태](#현재-배포-상태)를 기준으로 합니다. 아래 설정은 적용했으며, 이후 재배포에서도 유지합니다.

1. 현재 소스의 **`streamlit_app.py`는 `app.py`와 같은 `roly.ui.run()`을 실행하는 호환 진입점**입니다. 승인된 `main`의 첫 배포 `a3a091d`, 중간 개선본 `cf92f09`, 최신 `56f6c13`을 이 진입점으로 배포했습니다. 이후 수정도 Cloud가 연결한 `main`의 커밋과 실제 빌드를 함께 확인합니다.
2. Cloud에서 Python **3.11.16**과 Streamlit **1.63** 실행을 확인했습니다. Python 3.11 계열과 이 프로젝트의 `requirements.txt`를 유지하고, 과거 템플릿의 의존성 파일을 되살리지 않습니다.
3. **Advanced settings → Secrets** 또는 앱 **Settings → Secrets**에 저장한 검수용 Supabase 연결 항목과 아래 설정을 유지합니다. 다른 배포를 새로 만들 때는 해당 환경에 별도로 입력합니다. 실제 비밀번호와 API 키는 문서·소스·ZIP에 넣지 않습니다.

```toml
[app]
environment = "test"

[riot]
allow_demo = false
```

4. Cloud에서는 `ROLYMOLY_APP_ENVIRONMENT=local`이나 로컬 SQLite 대상·개발용 `ROLYMOLY_DATA_DIR`을 지정하지 않습니다. 공용 DB 설정을 누락하거나 잘못 입력하면 앱이 중단되는 것이 정상입니다.
5. 준비한 공용 검수 DB의 관리자·개인 계정 로그인과 비로그인 페이지/권한 동선을 Cloud에서 확인했습니다. 재배포 후에는 임시 경매로 팀장 입찰·관전자 반영·재개·초기화를 다시 확인하고, 별도 가입→승인→복구와 여러 기기 연결 검수를 이어갑니다. 로컬 체험 계정을 배포용으로 사용하지 않습니다.
6. 검수가 끝나면 사용할 DB 프로젝트를 다시 확인하고 `[app] environment = "production"`으로 전환합니다. 모드만 바꿔도 DB가 복사·초기화되지는 않습니다.

앱 스키마의 수동 백업·별도 복원 검증 도구와 워커 프로세스 복구 리허설은 추가했지만, 예약 백업·운영 DB 복원과 Cloud 휴면 중 독립 마감은 완료하지 않았습니다. 계정 비밀번호 복구는 운영진이 본인 확인 후 일회용 코드를 발급하는 방식입니다. 월 운영 예산은 0원입니다.

## 관리자용 DB 위치와 회원 식별값

Supabase 프로젝트의 **Table Editor → 스키마 선택 `public` → `rolymoly`**로 바꾸면 앱 테이블을 찾을 수 있습니다. 현재 `members`에는 회원 22명, `accounts`에는 관리자 포함 계정 23개가 있으며, 저장된 Riot 조회 결과는 `riot_profiles`에서 확인합니다. `public`에 테이블이 보이지 않아도 앱 데이터가 없는 것은 아닙니다.

`members.riot_id`는 표시할 `닉네임#태그`이고 `canonical_id`는 앞뒤 공백 제거와 대소문자 통일(`strip`·`casefold`)을 적용한 중복 방지 값입니다. 본인 닉네임 변경이나 관리자 수정 시 앱이 함께 갱신하므로 따로 입력하지 않습니다. 회원의 고유 번호인 `members.id`와 계정 연결은 닉네임이 바뀌어도 유지됩니다. 주·부 포지션과 클랜 티어는 앱의 관리자 회원 수정 기능에서 관리합니다.

## 과거 검증 — 최초 PostgreSQL 이식

다음 16단계·12단계·219개는 최초 PostgreSQL 이식 시점의 과거 검증입니다. 현재 개인 가입·회원 티어·일반내전 추가 검증이나 Cloud 배포 완료와 구분합니다.

실제 PostgreSQL 리허설은 매번 새로 생성하는 임시 QA 스키마에서 **16단계·51.625초로 통과**했고 해당 스키마도 정리했습니다. 4명 동시 같은 금액 입찰의 1건 승인, 중복 마감의 1회 정산, 선수 16명 낙찰·4팀 각 5명·총 170포인트 차감, 경매 결과의 전력점수 불변과 우승팀 5명 업적, 일반내전 ±10점·결과 정정을 검증했습니다. PostgreSQL 실행 결과 (`test-artifacts/postgres-rehearsal-report.json`)

추가로 실제 PostgreSQL 결과 정정 12단계가 55.734초에 통과했습니다. UNIQUE 오류 뒤 SAVEPOINT 복구, 정정 중 실패의 전체 롤백, 중복 적용, 결승·3위전 재경기, 동률 추가 경기 재생성과 ID 재사용 방지를 확인했으며 임시 QA 스키마도 정리했습니다. 상세 근거는 [운영 리허설 보고](REHEARSAL.md)에 있습니다.

당시 PostgreSQL 전환 후 전체 회귀 테스트는 **219개 통과·427.619초·종료 코드 0**이며 검사 전후 Python 소스 63개 해시가 일치했습니다. 2026-09-07~08의 Chrome 6계정 리허설과 당시 SQLite 기준 자동 테스트 166개 결과는 [REHEARSAL.md](REHEARSAL.md)에 별도 과거 검증 이력으로 보존합니다.

## Supabase 연결 정보 입력

실제 입력 파일은 `.streamlit/secrets.toml`입니다. React에서 `.env`에 넣었던 비밀 설정에 해당하며 Python 서버에서 읽습니다. 이 앱은 `.env`를 자동으로 읽지 않습니다. `.streamlit/secrets.toml.example`은 빈 배포용 예시로 유지합니다. 실제 설정 파일은 Git과 소스 ZIP에서 제외합니다.

1. Supabase의 **사용할 프로젝트 → Connect → Session pooler**를 엽니다.
2. 연결 매개변수의 `host`, `user`와 프로젝트 생성 시 설정한 **DB 비밀번호**를 실제 설정 파일에 입력합니다. `database = "postgres"`, `port = 5432`는 그대로 둡니다. `host`에 연결 URL 전체를 넣지 않습니다.
3. `user`는 일반적으로 `postgres.프로젝트참조ID`입니다. 서비스 이름인 `rolymoly-test`나 Supabase 로그인 이메일을 넣는 칸이 아닙니다. 이 연결에는 `anon`·`service_role` API 키가 필요하지 않습니다.
4. 아래 검사를 실행합니다. 입력 형식 검사는 네트워크에 접속하지 않으며 실제 연결 검사는 서버 정보와 `pg_cron` 사용 가능/설치 여부만 조회하고 rollback합니다. 테이블을 만들거나 기존 자료를 옮기지 않습니다.

```powershell
# 설정 형식 확인 (네트워크 접속 없음)
.\.venv\Scripts\python.exe -X utf8 scripts\check_supabase.py --check-config
# 실제 연결 확인 (읽기 전용)
.\.venv\Scripts\python.exe -X utf8 scripts\check_supabase.py
```

새 PC에서 입력 파일이 없다면 `--init-config`로 준비할 수 있습니다. 기존 파일은 덮어쓰지 않습니다. 비밀번호는 URL 인코딩하지 않고 TOML 문자열로 입력합니다. 기본 작은따옴표 문자열은 `@`, `#`, `$`, 역슬래시를 그대로 보존합니다. 비밀번호에 작은따옴표가 있으면 TOML 큰따옴표 문자열의 이스케이프 규칙을 사용합니다. 검사 도구는 비밀번호나 원본 연결 오류를 출력하지 않습니다.

앱과 진단 도구는 `sslmode=require`, 연결 제한 10초, 쿼리 제한 10초와 `prepare_threshold=None`을 사용합니다. 앱 연결은 읽기·쓰기를 허용하고 진단 도구는 읽기 전용으로 제한합니다. `require`는 통신 암호화를 요구하며 `verify-full`의 인증서·호스트 이름 검증과 동일하지 않습니다. IPv4 연결에 Session pooler를 사용할 수 있습니다. [Supabase 연결 안내](https://supabase.com/docs/guides/database/connecting-to-postgres), [PostgreSQL SSL 모드](https://www.postgresql.org/docs/current/libpq-ssl.html).

Community Cloud에 검수 앱을 만들 때는 **Advanced settings → Secrets** 또는 앱 설정의 **Secrets**에 같은 TOML 내용을 입력합니다. 로컬 파일이 서버로 자동 전송되지는 않습니다. GitHub에 비밀 파일을 올리지 않습니다. [Streamlit Secrets 안내](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management).

`scripts/prepare_storage.py`는 읽기 전용 진단과 달리 비공개 운영 스키마를 준비하는 명령입니다. 현재 코드는 버전 **6**까지 필요한 테이블·컬럼을 추가하며 아래 명령 또는 새 앱의 초기화가 선택한 스키마를 갱신합니다. v5의 신청 메시지·내전 생성 영수증에 v6의 현재 티어 출처와 Riot 캐시·작업·호출 한도 테이블을 더합니다. 기존 `notes`는 내부 메모로 보존하고 `application_notes`는 빈값으로 추가합니다. 기존 메모를 회원에게 공개하거나 외부 자료를 가져오지 않습니다. 별도 QA 스키마의 업그레이드 통과가 실제 운영 스키마의 적용 완료를 의미하지는 않습니다.

```powershell
$env:ROLYMOLY_DATABASE_TARGET = "supabase://rolymoly"
.\.venv\Scripts\python.exe -X utf8 scripts\prepare_storage.py
```

## 운영 저장소 선택

`roly/storage_config.py`가 앱·실행기·관리 명령의 연결 설정을 함께 검증합니다. 프로젝트 루트의 `.streamlit/secrets.toml`이 있으면 먼저 읽고, 없으면 `st.secrets`를 읽습니다. 기본 운영 실행은 Supabase 설정이 없거나 모두 비어 있어도 오류로 중단합니다. SQLite로 자동 전환하지 않습니다.

| 우선순위 | 조건 | 선택 결과 |
| --- | --- | --- |
| 1 | `ROLYMOLY_DATABASE_TARGET` 있음 | 정확한 `supabase://rolymoly` 또는 명시적 개발·검증용 SQLite 파일 경로 |
| 2 | 위 대상 없이 `ROLYMOLY_DATA_DIR` 있음 | 개발·검증 예외로 해당 폴더의 `rolymoly.sqlite3` |
| 3 | 두 환경변수 없음 | Supabase 설정을 필수 검증하고 비공개 `rolymoly` PostgreSQL 스키마 사용 |
| 중단 | 기본 운영 실행에서 연결 정보 없음·빈값·형식 오류 | 연결 오류를 안내하고 종료 |

명시적인 SQLite 파일 경로는 확장자 없이도 사용할 수 있습니다. 상대 경로는 실행한 작업 폴더 기준으로 절대 경로로 바꾸고 `~`를 확장합니다. URL·SQLite URI·`:memory:`·제어 문자·네트워크 공유 경로는 대상 환경변수에서 받지 않습니다. 연결 정보는 별도 Secrets에 두며 대상 값에 비밀번호를 넣지 않습니다.

배포 모드는 **명시한 `ROLYMOLY_APP_ENVIRONMENT` → Supabase 대상 없이 명시한 `ROLYMOLY_DATA_DIR`의 로컬 개발 예외 → `[app] environment`(기본 `production`)** 순서로 결정합니다. 모드 환경변수는 `local`, `test`, `production`만 허용하며 잘못된 값은 개발 예외가 있어도 중단합니다. 실행기가 Supabase 대상과 데이터 폴더를 함께 전달한 경우에는 로컬로 추정하지 않습니다.

위 표의 SQLite 선택은 개발·CLI 리허설 계약입니다. **`test`/`production` 앱은 서비스 생성 전에 SQLite 대상을 거절**합니다. Cloud에서는 개발 예외를 사용하지 않습니다. `roly.server`는 선택한 저장소와 절대 데이터 경로를 화면 자식 프로세스에 전달합니다. `local` 모드의 체험 공간은 세션별 SQLite를 사용하며 운영 자료와 합치지 않습니다.

## 로그인과 팀장 입찰

Supabase를 선택한 새 세션은 **운영 공간**으로 시작하고, 관리자가 있으면 사이드바의 **계정 로그인**을 펼쳐 보여줍니다. 관리자 계정이 없는 PostgreSQL 운영 공간에는 준비 안내만 표시하며 최초 관리자 생성 폼을 차단합니다. Supabase 대시보드 계정은 프로젝트 관리 계정이고 Rolymoly 앱 로그인은 별도로 관리합니다. Supabase Auth로 바꾼 것은 아닙니다.

현재 공용 검수 DB에는 관리자와 검수용 개인 계정을 준비해 두었습니다. 아래 명령은 **관리자가 없는 새 DB를 준비할 때만** 사용합니다. 배포자가 비공개 터미널에서 아이디·표시 이름·앱 비밀번호를 입력하며 비밀번호 입력은 표시되지 않습니다. 기존 관리자가 있거나 비밀번호 확인이 다르면 계정을 만들지 않습니다.

```powershell
$env:ROLYMOLY_DATABASE_TARGET = "supabase://rolymoly"
.\.venv\Scripts\python.exe -X utf8 scripts\create_admin.py
```

명시적으로 선택한 개발용 SQLite 공간은 기존 최초 관리자 설정 폼을 유지합니다. PostgreSQL 보호 동작과 로그인 화면은 임시 SQLite를 이용한 화면 테스트로, 비공개 생성·기존 계정 보존·비밀번호 불일치는 CLI 테스트로 확인했습니다.

현재 흐름은 **개인 계정과 가입 신청 동시 등록 → 운영진 승인 → 카카오톡 명단 확정 → 경매별 팀장 지정 → 본인 계정 입찰·공동 관전**입니다. 승인 회원 누구나 개설할 수 있고 본인이 만든 내전만 진행합니다. 관리자도 타 팀 대신 입찰하지 않습니다. 상세 권한과 복구 절차는 [현재 운영 흐름](CURRENT_FLOW.md)을 따릅니다.

로컬 실제 브라우저에서 팀장 선택 팝업의 **4명 저장과 외곽 너비 520px**을 확인했습니다. 준비 화면은 데스크톱 2열과 기존 카드·선수 안쪽 여백을 유지하며, 좌우 패널 비율만 1.35:1에서 1.25:1로 소폭 조정했습니다. 사진·이름·Riot ID를 상단에, 티어·주부 포지션·숙련도 상위 5개를 그 아래 사진의 왼쪽 끝에 맞춰 표시합니다. Riot ID는 줄바꿈 없이 전체 ID 툴팁과 함께 표시하며, 실제 예시 `화려한솔로#외로운청년`이 생략 없이 한 줄로 보이는 것을 확인했습니다. 숙련도 아이콘은 한 줄로 유지하고 빈자리 안내·하단의 별도 5포지션 행은 제거했습니다. 서버 재시작 후 4팀 모두 이 정렬과 제거 상태를 실제 DOM·스크린샷으로 확인했습니다. 이 카드 변경은 업로드 기준 575개 회귀에 포함됐으며, 새 앱의 Cloud 화면 확인은 아직 완료되지 않았습니다.

운영 역할은 승인된 개인계정에 부여합니다. 관리자 UI에서 일반회원 계정을 별도 발급하거나 다른 회원으로 재연결하지 않으며, 개인계정의 회원 ID 연결은 고정됩니다. 탈퇴 중에는 관리자·진행자 역할도 사용할 수 없습니다. 마지막 활성 관리자의 해제·탈퇴는 차단하고, 복귀는 이전 승인 여부에 따라 활동 또는 보완 대기 상태로 처리합니다. 신청 메시지와 관리자 내부 메모는 각각 별도 필드에 보관합니다.

본인 비밀번호 변경과 관리자 본인 확인 후 일회용 복구 코드 발급을 구현했습니다. 새로고침 시 같은 탭의 토큰을 재검증하며 비밀번호 변경·복구는 기존 세션을 폐기합니다. 탈퇴·역할 변경·호환용 기존 계정 재연결도 이전 세션과 복구 코드를 폐기합니다. Supabase Auth나 소셜 로그인으로 바꾸지 않았습니다.

승인된 개인계정은 **본인 프로필 또는 내 계정 → 닉네임 변경**에서 `닉네임#태그`를 저장합니다. 다른 회원에게는 이 버튼을 표시하지 않고, 팝업에서도 현재 로그인·본인 회원·DB·수정 버전을 다시 확인합니다. 이미 등록된 ID나 오래된 폼의 저장은 거절하며 같은 ID를 그대로 저장하면 변경하지 않습니다. 로그인 아이디·계정과 회원 ID·세션·포지션·클랜 티어·점수·업적·당시 내전 기록은 유지됩니다. 실제 Riot ID가 바뀌면 이전 현재 티어와 Riot 캐시·조회 작업을 비우며, 자동 API 조회 없이 회원이 **갱신하기**를 눌러 새 정보를 조회합니다. 대소문자만 바뀐 동일 ID의 API 캐시는 유지합니다.

## 백업과 복구 범위

`scripts/backup_storage.py`는 PostgreSQL 17의 `pg_dump`·`pg_restore`를 사용합니다. `create`는 읽기 전용 스냅샷으로 **비공개 앱 스키마 전체**를 백업하고, `verify`는 새 QA 스키마에만 복원하여 테이블·행·컬럼·제약조건·인덱스·sequence를 대조한 뒤 정리합니다. 계정·세션·회원·원장·입찰·대진·스냅샷이 포함되므로 원본은 민감한 자료입니다. `.data/backups/`에 보관하며 Git/소스 ZIP에 넣지 않습니다.

```powershell
# 설치한 PostgreSQL 17 도구 폴더를 지정합니다.
.\.venv\Scripts\python.exe -X utf8 scripts\backup_storage.py check-tools --pg-bin "C:\PostgreSQL\17\bin"
.\.venv\Scripts\python.exe -X utf8 scripts\backup_storage.py create --pg-bin "C:\PostgreSQL\17\bin"
# create가 만든 .data/backups 아래 디렉터리를 지정합니다.
.\.venv\Scripts\python.exe -X utf8 scripts\backup_storage.py verify --pg-bin "C:\PostgreSQL\17\bin" --backup-dir ".data\backups\생성된-백업-폴더"
```

실제 Supabase의 새 합성 QA 자료에서 선행 서비스 흐름 15개와 **35개 테이블·20개 sequence 복원 대조를 75.422초에 통과**했습니다. 원본 QA·복원 QA·합성 백업은 정리했습니다. 근거: `test-artifacts/deployment-backup-rehearsal.json`. **운영 스키마를 덮어쓰는 복원, Supabase 프로젝트 역할·설정·확장·외부 자산, 예약 백업·외부 보관·실패 알림은 이 도구의 완료 범위가 아닙니다.** 회원 CSV는 이 전체 백업을 대신하지 않습니다.

경매 워커 복구는 팀 카드·전체 페이지 검수 수정 전 동결한 소스의 실제 PostgreSQL QA에서 **9단계·66.032초 통과**했습니다. 워커가 없으면 만료된 경매도 정산되지 않는 것을 확인한 뒤, 새 프로세스가 브라우저 없이 기존 낙찰을 한 번만 정산하고 다음 선수를 최소 3초 뒤 여는 것을 확인했습니다. 일시정지는 재시작 후에도 유지됐으며 두 워커의 동시 실행에서 중복 차감은 없었습니다. 생성한 QA 스키마와 네 워커 프로세스는 모두 정리했습니다. 근거: `test-artifacts/deployment-recovery-remote.json`.

이는 앱·DB의 실제 복구 동작 검증이며 **Cloud 휴면 중 마감을 수행하는 독립 워커 구현이나 실제 브라우저의 인터넷 지연 검증은 아닙니다.** 운영 전에는 정기 백업의 보관·실패 대응·실제 운영 복원 절차와 Cloud에서의 재시작·로그인·입찰을 별도로 확인합니다.

## 구현한 PostgreSQL 처리

`Core('supabase://rolymoly')`가 PostgreSQL 백엔드를 선택합니다. SQL 어댑터와 현재 **v6**까지의 스키마 초기화가 기존 서비스의 데이터 형식·트랜잭션 계약을 유지합니다. 아래 기본 처리에는 과거 실제 PostgreSQL 검증 근거가 있으며, 최신 전체·원격 재검증 결과는 [QA 보고](QA_SCENARIOS.md)와 [Riot 연동 보고](RIOT_API_REPORT.md)에서 확인합니다. 과거 v5 결과를 v6 전체 검증으로 간주하지 않습니다.

- 비공개 `rolymoly` 스키마를 사용하고 `anon`·`authenticated`의 직접 접근 권한을 제거합니다. 브라우저가 DB 계정이나 DB 비밀번호를 받지 않습니다.
- 입찰·마감·회원·경기 등의 운영 쓰기는 같은 스키마의 트랜잭션 advisory lock을 사용합니다. 입찰·마감·회원 변경·취소와 중복 워커가 같은 잠금 규칙을 따릅니다.
- Riot 작업 예약과 호출 한도는 각각 별도 메타데이터 잠금으로 관리하고 HTTP 요청은 DB 쓰기 트랜잭션 밖에서 실행합니다. 최종 회원·캐시 반영 시에는 운영 쓰기 잠금과 회원 정보 버전을 다시 확인합니다.
- 여러 SELECT로 구성된 조회는 `REPEATABLE READ READ ONLY` 스냅샷을 사용합니다. 쓰기 중 전달받은 연결에는 새 읽기 트랜잭션을 열지 않습니다.
- 경매 epoch 초는 `DOUBLE PRECISION`으로 보존합니다. PostgreSQL 경매는 잠금을 얻은 뒤 DB의 `clock_timestamp()`를 읽어 입찰 마감·연장·일시정지·재개와 화면 시각에 사용합니다.
- ID 생성, FK·UNIQUE·부분 UNIQUE 인덱스, SAVEPOINT 오류 복구와 경기 아카이브 뒤 ID 할당을 보존합니다. 기존 JSON·UTC 날짜 문자열 형식도 유지합니다.
- 회원·가입·명단 확정·결과는 검토 당시 버전을 저장 직전에 검사하며, 내전 생성·입찰·수기 보정은 요청 영수증으로 재전송을 구분합니다. 명단·경기와 CSV는 저장된 당시 이름·티어를 사용합니다.
- 4명 동시 입찰·동일 요청 재전송·중복 정산, 재연결 후 저장 상태, 일반내전 점수·정정과 경매 업적을 임시 PostgreSQL QA 스키마에서 검증했습니다.

## 공개 운영 전에 필요한 작업

| 항목 | 현재 확인된 상태 | 남은 작업 | 배포 전 통과 기준 |
| --- | --- | --- | --- |
| 최초 관리자 | PostgreSQL 공개 생성 차단과 비공개 CLI 구현·테스트 완료, 준비된 관리자의 Cloud 로그인 확인 | 재배포 후 관리자 권한 유지 재확인 | 해당 계정으로 검수 앱 로그인 가능 |
| 영속 저장·백업 | 앱 스키마 백업·새 QA 복원 도구 및 실제 PG 대조 완료 | 예약 실행·외부 보관·실패 알림·운영 복원 절차 | 원본과 복원본의 계정·점수·입찰·대진 일치 및 운영 복구 절차 확인 |
| 독립 경매 마감 | 프로세스 종료·재시작·두 워커 정산 검증 완료, 휴면 중 독립 워커 없음 | DB 마감 함수와 Cron 또는 별도 상시 처리 서비스 구현, 같은 잠금·시계·중복 방어 적용 | 화면 접속과 앱 가동 여부에 관계없이 마감 배정·차감 1회 처리 |
| 운영·체험 분리 | test/production의 체험 진입·seed 차단, Cloud의 test 설정·공유 DB 연결 확인 | 재배포에서도 모드·DB·체험 차단 유지 | 검수 앱에 모든 사용자가 개인계정으로 가입·로그인하며 체험 자료가 생성되지 않음 |
| 로그인·계정 복구 | 개인 가입, 승인·재신청, 본인 변경·일회용 복구 구현. 비밀번호 계산을 쓰기 잠금 밖으로 분리 | Cloud 실제 브라우저에서 가입·재로그인·복구·탭 재접속 확인 | 계정과 기록이 유지되고 폐기된 세션은 재사용 불가 |
| Cloud 배포·업데이트 | `56f6c13` 배포·정식 재시작과 기존 일시정지·포인트 보존, 초기화 이후 새 Cloud 직접 입찰·낙찰·배정·차감 확인. 화면 변경 전 606개 전체·변경 후 35개 영향 검증 통과 | 여러 기기 경쟁·연결 단절/복귀·전체 조작 흐름과 롤백 확인 | Cloud에서 전체 흐름·재연결·운영 자료 보존 확인 |

공개 접근의 추가 제한 수준은 인터넷 전체 공개인지, 인증된 클랜원만 접속하는 배포인지에 맞춥니다. 경매 중에는 배포를 피하고, 필요한 경우 일시정지·상태 확인·배포·재개 순서를 따릅니다.

## 첫 출시 전에 함께 개선할 사용 흐름

- 모바일 입찰 화면에서는 현재 선수·최고 입찰·타이머·입찰 버튼의 접근성을 확인합니다. 과거 390×844 검수에서는 입찰 버튼이 약 1,488px 아래에 있었습니다. 이 수치는 현재 화면의 측정값이 아니며, PC에서 통과한 화면 배치만으로 모바일 입찰 사용성이 확보되지는 않습니다.
- 가입 신청에서 개인 계정 생성과 회원 연결을 한 번에 처리합니다. 실제 검수 앱에서 가입 대기·보완 요청·승인·복구까지 확인하고 운영진의 본인 확인 절차를 점검합니다.
- 재연결 중에는 상태가 갱신 중임을 표시하고 입찰 재확인을 안내합니다. 현재 서버가 오래된 선수·마감 후·중복 요청은 검증하지만, 실제 인터넷 연결이 끊어진 사용자가 접수 여부를 이해하는 흐름까지 확인해야 합니다.

## 무료 테스트 배포 구성

선택한 구성은 **Streamlit Community Cloud + Supabase Free의 PostgreSQL**입니다. Supabase 연결은 완료했으며 기존 Cloud 앱에 새 소스를 반영하는 단계는 [현재 배포 상태](#현재-배포-상태)에 정리했습니다. 회원·계정·내전·입찰·기록은 이 DB에서 관리하며 Google Sheets 연결·동기화·기존 자료 가져오기는 운영 범위에서 제외합니다. 월 예산 0원의 무료 범위에서 검수하며 유료 추가 기능·요금제를 적용하지 않습니다. 플랜의 저장 공간·전송량·프로젝트 한도는 배포 시 공식 자료에서 확인합니다. [Community Cloud 안내](https://docs.streamlit.io/deploy/streamlit-community-cloud), [Supabase 요금표](https://supabase.com/pricing)

| 역할 | 구성과 진행 상태 |
| --- | --- |
| 화면·주소 | 기존 Community Cloud 검수 앱 사용. 확인한 주소·저장소·브랜치·시작 파일은 [현재 배포 상태](#현재-배포-상태) 참고 |
| 운영 데이터 기준 저장소 | PostgreSQL 비공개 `rolymoly` 스키마 준비 완료. 원격 리허설은 별도 임시 QA 스키마를 생성·정리하여 운영 자료와 분리 |
| 경매 동시 처리 | 공통 트랜잭션 잠금 안에서 가격·팀 권한·예산·마감·요청 ID를 검사하고 입찰·연장을 함께 확정. 실제 PostgreSQL 경쟁 입찰 검증 완료 |
| 마감 처리 | 현재 Python 워커 사용. DB 마감 함수와 Supabase Cron을 연결하는 독립 처리 방식은 아직 미구현 |
| 회원·점수 관리 | 앱의 관리 화면에서 PostgreSQL 자료를 수정. Google Sheets 연동 및 기존 회원·점수·업적 가져오기는 사용하지 않음 |

실제 프로젝트는 PostgreSQL 17이며 `pg_cron`은 사용 가능하지만 아직 설치하지 않았습니다. Supabase Cron은 DB 함수 실행과 초 단위 일정을 지원하며 Python 마감 로직을 그대로 실행하지는 않습니다. 후속 DB 함수는 Python 쓰기와 같은 advisory lock·DB 시계·중복 정산 방어를 사용하고, 실행 주기 지연·다음 선수 3초 규칙을 다시 검증해야 합니다. [Cron](https://supabase.com/docs/guides/cron), [초 단위 일정의 버전 조건](https://supabase.com/docs/guides/cron/quickstart)

앱의 PostgreSQL SQL 어댑터·스키마·잠금·정산 연결은 구현했습니다. Session pooler로 연결하므로 유료 IPv4 추가 기능을 전제로 하지 않습니다. [연결 방식](https://supabase.com/docs/guides/database/connecting-to-postgres)

무료 플랜의 비활동 중지와 관리형 백업 제약은 배포 시 공식 자료에서 다시 확인합니다. 현재 수동 백업 도구는 앱 스키마를 보존하며, 프로젝트 전체 백업과 정기 보관·복구 운영을 대신하지 않습니다. DB가 중지되었을 때는 재개·무결성 확인 후 경매를 시작하도록 하고, 유료 상시 가동 수준을 보장하지 않습니다. [무료 플랜의 가용성·백업 제약](https://supabase.com/docs/guides/deployment/going-into-prod)

기존 검수 주소를 유지합니다. 추후 별도 앱이나 주소를 만들 때는 Cloud에서 이름의 중복 여부와 연결 브랜치를 확인합니다. [새 앱 배포](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy), [주소 변경·중복 확인](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app/app-settings)

## 과거 참고 코드의 Google Sheets 연동 검토

이 절은 현재 앱의 연결 안내가 아니라 과거 참고 코드의 검토 기록입니다. 아래 `review-source/` 경로는 원본 비교 시점의 경로입니다. 정리 후에는 로컬 `.archive/reference-source-20260908.zip` 안에 같은 폴더 구조로 보관합니다. 운영·정책 Markdown 문서는 작업 폴더의 원래 위치에 보존합니다.

`review-source/Rolymoly-main/database.py:14`는 `gspread`와 서비스 계정으로 Google Sheets에 연결합니다. 회원·설정·확정 경기·출전 기록은 `users`, `settings`, `matches`, `match_players`에 저장합니다. 그러나 경매 진행 중 팀·포인트·유찰은 `pages/5_경매내전.py`의 세션 상태와 로컬 `temp_save_auction.json`, 대진·진행 결과는 `utils/tournament_manager.py:7`의 로컬 `tournaments.json`을 사용합니다. 기존 코드도 모든 경매 정보를 Google Sheets에 영속 저장하지 않았습니다.

`database.py:522`의 경기 추가는 다음 ID 조회 → 경기 append → 선수 append를 별도 호출하므로 중간 실패·동시 요청 때 전체 작업을 함께 확정하는 구조가 아닙니다. Google Sheets 자체에는 원자적 batchUpdate가 있지만, 그것만으로 분리된 현재가 조회·권한 확인·입찰·예산 차감의 경쟁 처리가 자동 해결되지는 않습니다. 따라서 참고 코드의 저장 함수를 새 경매에 그대로 연결하지 않습니다. [Google batchUpdate의 보장 범위](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate)

현재 앱에는 시트 연결 코드나 관련 의존성이 없으며 Google 서비스 계정·시트 권한 설정도 필요하지 않습니다. 기존 회원·점수는 옮기지 않고 새 가입·승인 절차로 등록합니다. CSV 다운로드는 DB 자료를 파일로 확인하는 기능이며 자동 동기화나 별도 운영 저장소가 아닙니다.

Supabase 프로젝트와 DB 연결, GitHub/Community Cloud 앱 연결은 확인했습니다. 새 소스의 배포 반영·실행 검수는 [현재 배포 상태](#현재-배포-상태)를 따릅니다.

## 외부 검수의 완료 기준

Supabase 운영 DB를 연결했어도 Community Cloud의 앱 휴면 중에는 현재 Python 워커가 실행되지 않습니다. 원격 저장·재시작 복구·휴면 중 독립 마감은 별도 항목입니다. 정식 운영 전에 독립 워커, 백업 보관·운영 복원 절차와 Cloud 검수를 완료해야 합니다. 검수·운영 배포는 로컬 파일 보존에 의존하는 체험 SQLite를 사용하지 않습니다. [공식 데이터 보관 제약](https://docs.streamlit.io/develop/concepts/connections/connecting-to-data), [공식 휴면 안내](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)

비공개 검수 앱에서 여러 기기·네트워크로 관리자·팀장 4명·참가자의 전 과정을 반복해야 합니다. 마지막 1초 경쟁 입찰, 연결 단절·복귀, 모바일 음량·타이머, 프로세스 재시작, 배포 환경의 백업·복원을 확인한 뒤 공개합니다. 데이터 처리 코드가 바뀌면 전체 회귀 테스트와 실제 PostgreSQL 통합 검증도 다시 실행합니다. 과거 로컬에서 관측한 0.5~1.8초는 원격 접속의 보장값이 아닙니다.

초기 PostgreSQL 이식 당시 실제 운영 DB를 연결한 `app.py` AppTest에서는 기본 운영 화면→회원→경매→경기 기록→라운지의 5회 렌더가 예외·오류 없이 완료됐고 8개 확인 항목이 5.484초에 통과했습니다. 공개 최초 관리자 폼이 없으며 계정·회원·경기를 포함한 9개 대상 테이블의 행 수는 전후 모두 0개였습니다. 이 과거 기록을 현재 소스나 실제 Chrome·Community Cloud의 검증 결과로 간주하지 않습니다. 운영 PostgreSQL 화면 확인 (`test-artifacts/postgres-operating-ui-report.json`)

## 과거 검증 — 2026-09-07 원격 저장소 조회

아래는 검수 브랜치 업로드와 Cloud 연결 확인 **이전**에 Git의 `ls-remote`와 인증 없는 GitHub REST API로 읽은 기록입니다. 당시의 미확인 항목을 현재 상태로 해석하지 않습니다.

| 항목 | 2026-09-07 당시 확인 결과 |
| --- | --- |
| 저장소 | `lsh920306/rolymoly`, 공개 저장소 |
| 기본 브랜치 | `main` |
| 확인한 HEAD | `ba91d073a6947b8885e9f085508d21afe38a7a46` |
| 기존 시작 파일 | `streamlit_app.py`, 6줄짜리 `My new app` 템플릿 |
| 기존 환경 | `pyproject.toml`: Python `>=3.14`, Streamlit `>=1.61.1`; `.python-version`: `3.14`; `uv.lock` 존재 |
| 새 앱 파일 | 당시 조회한 원격 `main`에는 `app.py`, `roly/`, `app_pages/`, `requirements.txt`가 없었음 |
| 당시 작업 폴더 | Git 저장소로 초기화되어 있지 않아 원격 연결 정보가 없었음 |
| 당시 공개 앱 연결 조사 | `rolymoly.streamlit.app`과 저장소의 연결 여부는 당시 확인하지 못했음. 현재 확인한 검수 앱 주소는 상단에 별도 명시 |

당시 원격 README의 앱 링크는 기본 템플릿 주소였고 GitHub Deployments 조회 결과는 비어 있었습니다. 이 조회만으로는 Community Cloud 배포 유무를 판단할 수 없었습니다. 이후 실제 Cloud 연결은 관리 화면에서 확인하여 상단 현재 상태에 반영했습니다.

확인한 공개 소스: [저장소](https://github.com/lsh920306/rolymoly), [기존 시작 파일](https://github.com/lsh920306/rolymoly/blob/ba91d073a6947b8885e9f085508d21afe38a7a46/streamlit_app.py), [기존 환경 설정](https://github.com/lsh920306/rolymoly/blob/ba91d073a6947b8885e9f085508d21afe38a7a46/pyproject.toml).

## 소스를 올릴 때 맞출 사항

현재 소스는 `app.py`와 호환 파일 `streamlit_app.py` 모두에서 같은 앱을 실행합니다. **Cloud 시작 파일 `streamlit_app.py`를 유지하여 첫 배포 `a3a091d`, 중간 개선본 `cf92f09`, 최신 `56f6c13`을 반영했습니다.** 이후 배포도 같은 진입점, Python **3.11**과 이번 `requirements.txt`를 유지합니다. [공식 배포 안내](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)

기존 템플릿의 `uv.lock`·`pyproject.toml`·Python 3.14 설정이 새 `requirements.txt`와 Python 3.11을 대신 선택하지 않도록 관리합니다. 현재 Cloud에서는 Python 3.11.16으로 빌드했으며, `streamlit_app.py`는 새 호환 진입점입니다. 다음 배포에서도 검수 브랜치·`main`·실제 Cloud 커밋과 의존성을 대조합니다. [의존성 파일 우선순위](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies)

배포 소스에는 `app.py`, `streamlit_app.py`, `requirements.txt`, `roly/`, `app_pages/`, `scripts/`, `static/`, `.streamlit/config.toml`과 필요한 문서를 포함합니다. `.venv/`, `.data/`(원본 백업 포함), SQLite 파일, `.streamlit/secrets.toml`, `.env`, 검수 계정·측정 원자료가 들어 있는 `test-artifacts/`, 원본 비교 자료를 보관한 `.archive/`는 공개 업로드 대상에서 제외합니다. 비밀값은 소스 대신 배포 환경의 비밀 설정으로 관리합니다. [공식 비밀 관리 안내](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)

검수한 소스 묶음은 `dist/rolymoly-source.zip`으로 준비합니다. `.streamlit/config.toml`의 로컬 수신 주소는 ZIP 사본에서 제거하고 `.python-version`은 3.11로 포함합니다. 실행 코드·문서·테스트와 파일별 SHA-256 매니페스트가 들어 있으며 DB·비밀 설정·브라우저 검수 프로필은 제외합니다. ZIP을 풀어 저장소 루트에 반영하면서 기존 `streamlit_app.py`는 새 호환 파일로 교체하고 템플릿의 의존성 파일은 정리합니다. 압축을 올리는 것만으로 기존 파일이 삭제되지는 않습니다.

로컬 재생성: `.\.venv\Scripts\python.exe scripts/package_source.py`. 이 명령은 소스 파일 목록·경로·압축 무결성·해시를 검사하며 원격 저장소를 변경하지 않습니다.

## Community Cloud에서 남아 있는 운영 제약

PostgreSQL 운영 자료는 Community Cloud 로컬 파일과 분리했고, 실제 Cloud 빌드·공유 DB 연결·로그인·페이지 권한 확인을 마쳤습니다. 아래는 확인된 범위와 남은 운영 제약입니다.

| 항목 | 현재 상태와 필요한 조치 |
| --- | --- |
| 운영 저장소 | Cloud Secrets의 `[app] environment=test`와 Supabase 연결을 확인했습니다. 회원 22명·계정 23개·Riot 캐시 20개를 유지합니다. 두 배포 모드는 설정 누락·SQLite 대상을 거절하며 예약 백업·보관·운영 복원은 별도로 구성합니다. |
| 경매 마감 작업 | 로컬 권장 실행기인 `python -m roly.server`는 앱과 별도로 마감 worker를 먼저 시작합니다. Community Cloud는 지정한 `streamlit_app.py` 또는 `app.py`를 실행하므로 이 실행기를 자동 사용하지 않습니다. 앱 서비스 초기화 시 활성 경매가 있으면 worker를 시작합니다. 실제 재시작 복구는 검증했지만 해당 서비스 초기화 전이나 플랫폼 휴면 중 마감은 보장되지 않습니다. |
| 최초 관리자 설정 | PostgreSQL 공개 생성 폼은 차단했고 준비한 검수 DB 관리자의 Cloud 로그인을 확인했습니다. 새 DB만 비공개 CLI로 최초 관리자를 준비하며, 이후에는 승인된 개인계정에 운영 역할을 부여합니다. 개발용 SQLite의 초기 설정 폼은 유지됩니다. |
| 수신 주소 | 로컬 `.streamlit/config.toml`의 `server.address="127.0.0.1"`은 로컬 접속용입니다. 배포 사본에서는 해당 줄을 제거하거나 배포 환경에서 `0.0.0.0`으로 덮어써야 합니다. 소스 ZIP을 만들 때 Cloud용 설정 사본과 로컬 원본을 구분합니다. |
| 동시 접속 검증 범위 | 실제 PostgreSQL 경쟁 입찰·워커 복구·연결풀 격리, 로컬 입찰의 다른 Cloud 접속 동기화와 `56f6c13` 초기화 이후 새 Cloud 직접 10 P 입찰·낙찰·차감을 확인했습니다. 여러 실제 기기의 인터넷 지연·부하 보장값은 아직 없습니다. |

SQLite 파일 보존 제약은 [Streamlit의 데이터 연결 문서](https://docs.streamlit.io/develop/concepts/connections/connecting-to-data)에 명시되어 있습니다. Community Cloud는 저장소 루트에서 `streamlit run`을 실행하고, 트래픽이 없는 앱은 휴면할 수 있습니다. [파일 실행 구조](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/file-organization), [휴면 및 앱 관리](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)

Cloud 검수 앱은 최신 `56f6c13`까지 반영했고, 연결·로그인·페이지 권한과 전체 팀 팝업 표시를 확인했습니다. 추가 수정 후 로컬 브라우저에서 팝업 닫기 1회 뒤 닫힘 유지 → 재개 → 10 P 입찰 접수 → 낙찰 → 1,000→990 P 차감을 확인했고 다른 Cloud 접속에도 결과가 동기화됐습니다. PostgreSQL의 입찰·낙찰 각 1건과 선수 배정, 회원·계정·Riot·이벤트 1번 보존을 읽기 전용으로 대조했습니다. 브라우저 관찰 시각과 DB 시각은 실제 요청 전송에 맞춘 측정값이 아니므로 응답 지연 수치로 사용하지 않습니다. [입찰 DB 대조](test-artifacts/browser-live-bid-postgres-20260909.json)

`56f6c13` 업데이트 뒤 Cloud의 정식 재시작을 수행했습니다. **초기화 전 재시작 직후** 읽기 전용 확인에서 임시 경매의 일시정지·남은 약 8.919초, 기존 10 P 입찰·낙찰 각 1건과 남은 포인트 990 P가 보존됐습니다. 회원 22명·계정 23개·Riot 프로필 20개·기존 경매 1번의 보호 해시 8개도 일치했습니다. 이 결과를 이후 초기화 뒤의 새 Cloud 입찰 성공으로 집계하지 않습니다. [재시작 직후 DB 확인](test-artifacts/cloud-event2-reboot-dry-run.json)

입찰 금액·팝업 수정본의 고유 606개 검증과 이후 3단 배치·로고 영향 검증 35개를 완료했습니다. Cloud 검수 중 브라우저 도구가 시간초과로 중단됐지만, 브라우저 재시작 후 **초기화 이후 새 Cloud 10 P 직접 입찰과 오른쪽 입찰 기록 표시**를 확인했습니다. 07:01:16 UTC의 읽기 전용 보고서에서 새 입찰 ID 2·선수 차례 75의 낙찰, 팀원 1명 추가와 1,000→990 P 차감, 보호 해시 일치를 확인했습니다. 이전 로컬 입찰 ID 1은 초기화로 취소된 차례의 이력이며 새 입찰에 중복 집계하지 않습니다. [보존한 새 Cloud 입찰 DB 확인](test-artifacts/cloud-event2-cloud-bid-success-preserved.json)

최종 Cloud 1920×855 화면에서 전체 팀 팝업 720×798.70px·4팀·내부 스크롤 없음, 갱신된 990 P와 2/5명 표시, 닫기 1회 뒤 반복 갱신에도 닫힘 유지를 확인했습니다. 3개 흰 패널은 상단이 일치하고 너비 약 346/773/310px이며 가로 넘침이 없었습니다. 원본 로고는 200×67.53px로 잘리지 않았습니다. [최종 Cloud 브라우저 확인](test-artifacts/cloud-browser-final-56f6c13.json)

브라우저에서 남은 25초·입력 0 P → 남은 22초·입력 10 P → 제출을 관찰했지만, 이는 네트워크 요청의 시작과 응답을 측정한 지연 수치가 아닙니다. 이번 성공은 여러 기기의 마지막 1초 경쟁·부하 검증을 대신하지 않습니다.

사용자의 명시적 승인 후 Cloud에서 임시 이벤트 2번을 일시정지하고 정확한 제목·주최자·외래키·보호 해시를 확인해 **해당 이벤트와 하위 검수 기록만 정리했습니다.** 1.344초에 완료했으며 커밋 후 대상 10개 테이블의 해당 이벤트 행은 모두 0개입니다. 회원 22명·계정 23개·Riot 프로필 20개·기존 준비 경매 1번의 보호 해시 8개와 추가 보호 해시는 그대로입니다. 새 Cloud 입찰 성공 근거는 삭제 전에 별도 파일로 보존했습니다. 정리 후 실제 Cloud 라운지에서도 회원 22명·기존 경매 1개 유지, 임시 경매 부재와 오류 없는 렌더를 확인했습니다. [정리 결과](test-artifacts/cloud-event2-cleanup-executed.json)

## 예산 확정 전 검토했던 SQLite 유지 대안

아래는 Supabase 단일 운영 저장소를 정하기 전에 검토한 과거 대안이며 현재 운영 방식이 아닙니다. SQLite를 유지한다면 영속 디스크를 제공하는 단일 서버에서 `roly.server`를 프로세스 관리 도구로 실행하고 저장 위치를 지정하는 구성이 필요했습니다. 외부 접속은 HTTPS 프록시와 배포 환경에 맞는 수신 주소를 사용하며 관리자 초기화 접근과 백업·복구도 준비해야 합니다. Streamlit 공식 Docker 예시도 외부 컨테이너 수신 주소로 `0.0.0.0`을 사용합니다. [공식 Docker 배포 안내](https://docs.streamlit.io/deploy/tutorials/docker)

현재 Cloud 앱은 `main`에 연결되어 있으므로 해당 브랜치 변경은 자동 반영될 수 있습니다. 승인 후 반영할 때 확인한 시작 파일·Python 버전·DB 설정을 함께 적용합니다. [GitHub 변경 자동 반영 안내](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)
