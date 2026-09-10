# 60명 운영·최대 80명 경매의 무료 운영 검토

검토일: 2026-09-10. 사용자 첨부 검토안의 주장, 현재 루트 제품 소스, 공식 공개 문서를 대조했습니다. 이 검토에서는 운영 Supabase, 요금 설정, 계정, 배포에 접속하거나 변경하지 않았습니다. 현재 코드의 측정 결과는 [경매 지연 검증 보고서](AUCTION_LATENCY_VERIFICATION.md)에 있습니다.

첨부안의 핵심 판단은 타당합니다. 브라우저가 직접 DB 함수에 입찰을 보내면 입찰 경로의 미국 Python 서버 경유를 없앨 수 있습니다. 다만 **60명 이하를 주 운영 조건으로 잡더라도, 무료 Realtime으로 연속 입찰을 목표 시간 안에 전달할 수 있다고 확정할 근거는 아직 없습니다.** 80명은 최대 부하 조건으로 유지하고 60명 결과를 별도로 판단하는 것이 맞습니다. 80명 측정값에 60/80을 곱한 숫자는 실측값이 아닙니다.

현재 완성한 수정과 첨부안은 구현 경로가 다릅니다. 현재 제품은 `브라우저 → Python 입찰 POST → PostgreSQL`과 `PostgreSQL 커밋 알림 → Python 공유 상태 → 자체 WebSocket → 브라우저`를 사용합니다. **Supabase Realtime의 초당 100개 제한은 현재 자체 WebSocket에 적용되지 않습니다.** 이 제한은 첨부의 `직접 RPC + Supabase Realtime` 후보를 평가할 때 적용합니다. 현재 구현에도 앱 호스트의 CPU·메모리·네트워크, DB 연결·대기·전송량 한도는 남습니다. 구현 근거: [입찰 HTTP](../roly/auction_http.py), [공유 상태와 WebSocket](../roly/auction_push.py), [커밋 알림 수신](../roly/auction_notifications.py).

공식 한도를 다시 확인한 결과는 다음과 같습니다. 이는 해당 서비스의 공개 조건이며 현재 계정의 실제 사용량을 조회한 결과가 아닙니다.

| 항목 | 공개 Free 조건 | 판단 |
| --- | --- | --- |
| Realtime 동시 연결 | 프로젝트당 200 | 사람 수와 다름. 여러 탭·기기·다른 화면의 연결도 계산 |
| Realtime 메시지 처리량 | 프로젝트당 초당 100 | 월 사용량이 적어도 마감 직전 몰림을 별도 검증해야 함 |
| Realtime 월 메시지 | 200만 | Broadcast·Postgres Changes·Presence 등 사용량을 함께 봐야 함 |
| Edge Functions | 월 50만 호출 | 응답 성공 여부와 무관하게 호출을 계산. OPTIONS 사전 요청은 제외 |
| DB | 저장공간 500MB, Shared CPU·RAM 500MB | 저장공간과 메모리는 서로 다른 자원 |
| API 요청 | Unlimited | 처리 속도나 동시 처리 성능 보장이 아님 |
| 전송량·운영 조건 | Egress 5GB, cached egress 별도 5GB, 1주 비활성 후 일시정지, 자동 백업 미포함 | 실시간 전송량과 보관·복구 요구도 함께 판단 |
| Pro | 월 $25부터 | 추가 프로젝트·컴퓨트·초과 사용 등에 따라 총액이 달라짐 |

근거: [Realtime 제한](https://supabase.com/docs/guides/realtime/limits), [메시지 산정과 월 한도](https://supabase.com/docs/guides/platform/manage-your-usage/realtime-messages), [Edge 호출 산정](https://supabase.com/docs/guides/platform/manage-your-usage/edge-function-invocations), [공식 요금표](https://supabase.com/pricing). 유료 전환은 Realtime 처리량, DB 대기, 전송량, 백업 요구 중 먼저 문제가 된 항목으로 판단해야 합니다. $25 결제만으로 경매 목표 지연을 보장하지는 않습니다.

60명과 80명의 차이는 아래처럼 계산할 수 있습니다. **한 경매 화면당 구독 연결 한 개, 상태 변경마다 하나의 Broadcast, 모든 연결이 그 메시지를 수신, 회당 상태 변경 1,000번, 월 9회**라는 가정입니다. 1,000번과 9회는 예상치이지 운영 로그에서 확인한 값이 아닙니다.

| 계산 항목 | 60개 연결 | 80개 연결 |
| --- | ---: | ---: |
| Broadcast 한 번의 월 사용량 산정: 발신 1 + 수신 N | 61개 | 81개 |
| 월 사용량: 1,000 × (N + 1) × 9 | 549,000개 | 729,000개 |
| 무료 월 200만 대비 | 27.45% | 36.45% |
| 초당 상태 변경 1번의 **수신분만** | 60개 | 80개 |
| 초당 상태 변경 2번의 **수신분만** | 120개 | 160개 |
| 초당 상태 변경 4번의 **수신분만** | 240개 | 320개 |

월 계산은 [공식 Broadcast 산정 방식](https://supabase.com/docs/guides/platform/manage-your-usage/realtime-messages)에 따른 산술입니다. 초당 행은 부하 추정이며 실제 제한 발생 시점을 측정한 결과가 아닙니다. 60개 연결에서도 수신분만 초당 두 번 전달하면 100개를 넘습니다. 공식 문서는 처리량 초과 시 연결 해제와 처리량 감소 후 자동 재접속을 설명합니다. [Realtime 제한과 오류](https://supabase.com/docs/guides/realtime/limits)

가격·최고 입찰 팀·마감 시각·revision을 확정 상태 메시지 하나로 묶는 제안에는 동의합니다. 단, 1초마다 전송하는 방식으로 제한에 맞추면 다음 전송까지 기다리는 시간이 생겨 클릭→상대 화면 p95 300ms 목표와 충돌할 수 있습니다. 짧은 시간에 모인 중간 상태를 합치는 것도 검증 대상입니다. 개별 UUID 접수 영수증은 보존하고, 상대가 마지막 확정 상태를 언제 보았는지와 생략된 중간 상태를 구분해야 합니다. 인원 수만으로 허용 입찰 빈도를 정하거나 재접속을 정상 운영으로 간주해서는 안 됩니다.

현재 코드와 직접 RPC 후보를 대조하면 다음 작업이 남습니다.

| 범위 | 현재 코드에서 확인한 동작 | 직접 RPC 후보에 필요한 구현·검증 |
| --- | --- | --- |
| 입찰 인증 | 자체 `accounts`·`sessions`, 무작위 토큰의 SHA-256 저장, 기본 12시간 만료, 활성 계정·회원 승인·팀장 소속 확인 | Supabase가 검증할 사용자 자격과 기존 회원·팀 매핑. 만료·강제 로그아웃·승인 취소도 연결 |
| DB 접근 | 앱 전용 스키마. 생성·마이그레이션 경로에서 `PUBLIC`, `anon`, `authenticated`의 스키마·테이블·시퀀스 권한 회수 | 제한된 RPC 공개 경계, 함수 실행 권한, 테이블 직접 변경 차단. 기존 초기화·마이그레이션과 권한 설계 충돌 점검 |
| 입찰 판정 | 같은 쓰기 트랜잭션 안에서 팀장·예산·선수·현재 경매·DB 마감 시각 검증, UUID와 payload fingerprint 확인 | 기존 규칙을 SQL/PLpgSQL 함수로 이식하고 경쟁 요청·응답 분실·재요청 동일성 검증 |
| 동시성 | PostgreSQL 쓰기 경로에 스키마 단위 `pg_advisory_xact_lock` 사용 | 관리자 정정·낙찰 워커·새 RPC가 같은 배타성 규칙을 사용. RPC만 별도 행 잠금을 잡는 식의 불일치 방지 |
| 연장·낙찰 | 정상 입찰 때 기존 마감에 5초 추가, 현재 DB 시각에서 설정된 입찰 시간 상한 적용. 워커가 마감·낙찰·다음 선수 전환 수행 | 연장 상한과 동일 UUID 재확인 시 추가 연장 금지. 입찰과 낙찰 경합 및 중복 정산 방지 |
| 전파·복구 | 자체 WS, 공유 revision·snapshot, DB 커밋 알림, 알림 누락 시 버전 재조회, 오래된 상태 적용 방지 | 권한 있는 Realtime 구독, 초기 구독과 snapshot 사이 누락 복구, 끊김 후 최신 상태·revision 재확인 |

로컬 코드 근거: [세션과 로그인](../roly/core.py), [회원·운영 권한](../roly/auth.py), [입찰과 낙찰 엔진](../roly/live_auction.py), [잠금·스키마 권한](../roly/postgres.py), [버전과 스냅샷](../roly/auction_state.py). 12시간 토큰을 Supabase JWT인 것처럼 전달하는 인증 연결은 현재 구현되어 있지 않습니다. 전체 로그인 서비스를 반드시 전면 교체해야 한다는 뜻은 아니며, 기존 신원을 Supabase가 신뢰할 수 있도록 연결하는 설계가 필요하다는 뜻입니다.

DB 함수는 API로 호출할 수 있습니다. PostgREST는 API 자원 요청마다 트랜잭션을 사용하지만 기본 격리는 READ COMMITTED입니다. 따라서 RPC 하나로 묶는 것과 경쟁 입찰을 올바르게 직렬화하는 것은 별도 구현 사항입니다. 입찰 함수는 적절한 쓰기 트랜잭션 안에서 검증·영수증·가격·연장·감사 기록을 함께 확정해야 합니다. [Supabase DB 함수](https://supabase.com/docs/guides/database/functions), [PostgREST 트랜잭션](https://postgrest.org/en/stable/references/transactions.html)

브라우저용 공개 API 키는 앱을 식별하며 사용자·팀장 자격을 대신하지 않습니다. secret 또는 기존 `service_role` 키는 브라우저로 보내지 않습니다. private 채널에는 `realtime.messages`의 권한 정책과 클라이언트 `private: true` 설정을 연결하고, 구독자에게 상태를 읽을 권한과 상태를 발행할 권한을 구분해야 합니다. `security definer`가 필요하면 함수 실행 역할, `search_path`, 참조 스키마를 명시합니다. [API 키와 사용자 인증](https://supabase.com/docs/guides/getting-started/api-keys), [Realtime 구독 권한](https://supabase.com/docs/guides/realtime/authorization), [함수 실행 권한](https://supabase.com/docs/guides/database/functions#function-privileges)

미국 경유 제거에 관한 첨부 설명도 맞습니다. Community Cloud는 앱을 미국에서 호스팅하며 지역 변경을 제공하지 않습니다. Python에서 `.rpc()`를 호출하면 브라우저→미국 앱 왕복은 남습니다. 브라우저의 패널에서 Supabase RPC를 직접 호출하고 수신 상태를 그 패널에서 적용해야 입찰·전파 경로에서 해당 경유를 제외할 수 있습니다. 로그인·초기 화면·남겨 둔 관리 기능까지 모두 국내로 옮겨지는 것은 아닙니다. [Streamlit Community Cloud 제한](https://docs.streamlit.io/deploy/streamlit-community-cloud/status)

Edge Function은 추가 인증 처리나 요청 제한, 외부 연동이 필요할 때 앞단 후보입니다. 필요한 검증을 안전하게 직접 RPC에서 수행할 수 있으면 필수 단계가 아닙니다. Edge에서 여러 REST 요청을 순서대로 보내도 그 전체가 자동으로 한 트랜잭션이 되지는 않으므로, DB 변경은 단일 RPC 또는 명시적 DB 트랜잭션에 묶어야 합니다. 이는 앞의 요청별 트랜잭션 구조에서 도출한 설계 판단입니다.

첨부의 `2,000회 × 월 9회 = 18,000회`, Free 50만의 3.6%는 산술상 맞습니다. 실패 응답도 호출량에 들어가며 OPTIONS는 제외됩니다. Edge에는 콜드 스타트 가능성이 있습니다. 서울 `ap-northeast-2` 지정은 가능한 비교 조건이고, DB 왕복이 많으면 DB와 가까운 지역이 유리할 수 있습니다. 지역을 강제로 고르면 해당 지역 장애 시 자동 우회하지 않는 조건도 함께 봅니다. 어느 경로가 빠른지는 콜드·웜 상태를 나눠 실측해야 합니다. [Edge 호출 산정](https://supabase.com/docs/guides/platform/manage-your-usage/edge-function-invocations), [Edge 실행 특성](https://supabase.com/docs/guides/functions), [지역 지정](https://supabase.com/docs/guides/functions/regional-invocation)

자동 낙찰은 별도로 완성해야 합니다. 현재 [실행기](../roly/server.py)는 상시 Python 워커를 시작하고 [워커](../roly/live_auction.py)는 처리 후 기본 250ms 대기합니다. 250ms는 DB 작업·대기까지 포함한 실제 낙찰 완료 상한이 아닙니다. 직접 RPC로 옮겨도 워커를 미국에 유지할 수는 있지만, 낙찰까지 그 경유를 없애려면 낙찰 로직과 실행 주체도 이전해야 합니다.

Supabase Cron은 초 단위 일정과 SQL·DB 함수 실행을 지원합니다. 1초 주기 Edge 호출을 30일 내내 켜 두면 `30 × 24 × 3,600 = 2,592,000회`로 무료 50만을 넘는 계산이 맞습니다. Cron이 SQL 함수를 직접 실행하면 해당 작업의 Edge 호출은 없지만 DB 작업·실행 기록 부하는 남습니다. 경매가 실제 열려 있는 시간만 실행하는 운영과 상시 감시를 구분하고, 1초 간격의 이상적인 스케줄 대기만으로도 거의 1초가 걸릴 수 있다는 점을 낙찰 지연 목표에 반영해야 합니다. 지연·실행 겹침·장애까지 포함하면 1초가 절대 상한인 것은 아닙니다. [Supabase Cron](https://supabase.com/docs/guides/cron)

첨부의 Lambda 무료량인 월 100만 요청·400,000 GB-seconds도 공식 요금표와 일치합니다. 이 수치가 별도 API·전송·상시 연결까지 포함한 전체 비용 0원을 뜻하지는 않습니다. 이 검토에서 AWS 추가 구성을 선택하거나 구매하지 않았습니다. [AWS Lambda 요금](https://aws.amazon.com/lambda/pricing/)

선택지는 다음과 같이 정리할 수 있습니다. 비용과 개발 범위를 비교하기 위한 판단이며 서비스별 성능 순위를 측정한 표는 아닙니다.

| 후보 | 지금 얻는 점 | 남는 조건 |
| --- | --- | --- |
| 현재 Python + 자체 WS, 기존 Community Cloud | 검증한 입찰 엔진 재사용, 별도 호스트 구매 없음 | 미국 앱 왕복, Cloud에서 실제 WS·부하·화면 지연 검증 |
| 현재 Python + 자체 WS, 한국 가까운 앱 호스트 | 엔진·인증·관리 기능 유지, 앱 왕복을 줄일 후보 | 사용할 호스트·비용·상시 워커·프록시 지원 및 한국 사용자 실측. 배포 초안만 준비됨 |
| 브라우저 직접 RPC + Supabase Realtime | 별도 Python 호스트를 구매하지 않고 입찰의 미국 경유를 제거할 후보 | 인증·입찰·낙찰 이전, Free 순간 전파 제한, 실제 화면 지연 검증 |
| Edge + RPC + Realtime | 앞단 인증·요청 처리 기능 추가 가능 | 추가 실행 단계·콜드 스타트·지역·호출량 검증. 단순 입찰에 자동으로 필요한 구성은 아님 |

현재 코드 보존과 이전 검증 범위 최소화에는 지역 앱 호스트 후보가 유리합니다. 월 호스팅비 0원이 최우선이면 직접 RPC 후보를 비교할 가치가 있습니다. 둘 중 하나를 모든 조건에서 최선이라고 단정하지 않습니다. 지역 이전 초안의 실제 준비 상태는 [배포 검토 문서](REGIONAL_DEPLOYMENT.md)에 기록되어 있습니다.

운영 규모 기준은 **60명 연결을 주 조건, 80명 연결을 최대 부하 조건**으로 나누되, 접수 p95 200ms·상대 화면 p95 300ms·각 p99 500ms 목표는 유지합니다. 별도 화면 반응은 100ms를 목표로 합니다. 두 인원 조건에서 같은 입찰 패턴을 사용하고 평균·p95·p99·최대·1초 초과·타임아웃·누락의 분모를 함께 기록해야 합니다. 실제 60명 성능은 60개 연결 조건의 결과가 확보되었을 때만 판정합니다.

직접 RPC 후보의 후속 검증에는 정상 입찰과 마감 직전 여러 팀의 연속 입찰, 한 화면당 여러 연결 발생 여부, 실제 초당 발신·수신량, 연결 해제·재가입, 최초 상태·재접속 복구를 포함합니다. UUID 재사용과 응답 분실, 5초 연장 상한, 관리자 변경과 낙찰 경합, 모든 브라우저가 닫힌 뒤 자동 마감도 검증해야 합니다. 서버 응답 수신과 화면 DOM 적용, 렌더 콜백·실제 표시의 측정 범위는 분리하고, 느린 통신망·재접속을 정상 상태 표에 섞지 않습니다.

이 문서에서 완료한 것은 **첨부 주장의 공식 문서 확인·60/80명 사용량 계산·현재 소스와 이전 범위 대조**입니다. 직접 RPC·Supabase Realtime·Cron·Edge 후보를 구현하거나 60/80명으로 그 서비스에 부하를 가한 결과는 아닙니다. 기존 자체 WS의 측정값을 직접 RPC 후보의 성능으로 가져오지 않으며, 무료 운영 지속 가능성과 실제 국내 사용자 목표 달성도 이 문서만으로 확정하지 않습니다.
