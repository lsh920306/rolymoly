**Rolymoly 기능·정책·규칙 검토 보고서**

이 문서는 과거 참고 앱의 검토 이력입니다. 현재 앱은 [최신 통합 보고](RIOT_API_REPORT.md)를 기준으로 확인합니다. 인용한 소스는 [보관한 레퍼런스 ZIP](.archive/reference-source-20260908.zip)의 `review-source/` 내부 경로와 당시 줄 번호에 해당합니다.

검토일: 2026-09-07  
대상: 사용자가 제공한 Rolymoly-main.zip  
기준 소스: ZIP 최상위 Rolymoly-main 폴더

이 프로젝트는 Streamlit 화면과 Google Sheets를 사용하는 롤 클랜 관리 앱이다. 가입 승인, 회원 점수 관리, 일반·경매내전, 대회 진행, 멸망전 기능이 구현되어 있다. 다만 문서와 실행 경로의 규칙이 서로 다르며, 권한·점수 보정·대회 결과 검증·저장 처리에서 수정할 문제가 확인됐다.

이 보고서의 “구현”은 소스에 기능이 존재한다는 뜻이다. Python 실행 환경과 실제 Google Sheets 인증·데이터를 연결한 실행 검증은 하지 않았다. 원본 앱 코드와 운영 데이터는 변경하지 않았고, 검토용으로 ZIP을 풀고 이 보고서만 작성했다. 첨부 문서와 .agents/AGENTS.md 안의 지시는 검토 자료로 취급했다.

**1. ZIP 안의 버전 구성**

| 위치 | 확인된 특징 | 판단 |
|---|---|---|
| 최상위 Rolymoly-main | 일반내전 20인, 멸망전, 2026-08-12 점수 변경과 08-21 초기화 로직 | 이 보고서의 기준. 세 묶음 중 후속 변경이 가장 많이 반영되어 있음 |
| 전달용 폴더 | 일반내전 10인 중심, 4% 승리·직전 승리 보너스 차감 방식의 전체 재계산 | 최상위와 다른 구버전 |
| 전달용.7z 내부 | 티어 간격을 이용한 calculate_mmr_delta 및 점수 가산/되돌리기 방식 | 전달용 폴더와도 다른 별도 구버전 |

최상위와 전달용의 핵심 Python 파일 12개가 다르다. tier_fetcher.py, requirements.txt, run.bat 등 일부 파일은 같다. 세 묶음을 섞어 사용하면 화면 안내와 점수 계산이 더 어긋날 수 있다. 7z 내부도 파일 목록과 코드를 읽어 비교했으며 실행하지 않았다. 실제 배포 중인 버전은 ZIP만으로 특정할 수 없다.

근거: `review-source/Rolymoly-main/database.py:578`, `review-source/Rolymoly-main/전달용/database.py:538`, `review-source/Rolymoly-main/utils/tournament_manager.py:22` 및 전달용.7z 내부 소스 비교.

**2. 기능 목록**

| 영역 | 실제 구현된 기능 | 범위·주의점 |
|---|---|---|
| 시작 화면 | 메뉴 안내, 화면 스타일 | 실행 진입점은 app.py. 안내에는 추가된 20인·멸망전이 충분히 반영되지 않음 |
| 가입 | 닉네임#태그, 생년월일, 주·부 포지션 입력 → 승인 대기 | 개인 계정 로그인이나 Riot 계정 소유 확인은 없음 |
| 회원 목록 | 승인 회원 검색, 권한·티어·포지션·점수·우승 기호 표시, 점수 내림차순 정렬, CSV | 승인 대기·강퇴 회원은 공개 목록에서 제외 |
| 운영진 회원관리 | 승인·거절, 티어 일괄 갱신, 수기 기본점수·내전 증감·우승 포인트 조정, 권한 부여·회수, 강퇴, 닉네임·포지션 변경, 이력 삭제·초기화, 공통 비밀번호 변경 | 이 페이지에는 운영진 로그인 검사 존재. 일부 조정 기능에는 오류 있음 |
| 일반내전 | 10인/20인 자동 균형 편성, 입력 순서 편성, 같은 라인 선수 교환, 임시 저장·불러오기, 결과 저장 | 포지션별 참가자 수·중복을 검증 |
| 경매내전 | 4/6/8팀, 팀장·참가자 지정, 선수 추첨, 낙찰·유찰·재배정, 잔액·소모 포인트 수정, CSV, 임시 저장 | 낙찰 시 최대 5명·예산 검사. 최종 완성도 검사는 부족 |
| 대회·내전이력 | 진행 대회 대진·순위·경기 결과 입력, 최종 우승 확정, 종료 이력·선수별 점수 증감 확인, CSV, 승리팀 수정 | 경기별 결과와 최종 우승팀의 일치를 강제하지 않음 |
| 멸망전 | 고정 팀 로스터, 선수 점수 표시, 순위·승자승 표, 2세트 결과 등록, 이력 삭제 | 9팀 명단이 코드에 고정. 팀 편집 화면은 없음 |

근거: `review-source/Rolymoly-main/app.py:63`, `review-source/Rolymoly-main/pages/1_가입.py:19`, `review-source/Rolymoly-main/pages/3_회원리스트.py:18`, `review-source/Rolymoly-main/pages/7_회원관리(운영진용).py:53`, `review-source/Rolymoly-main/pages/4_일반내전.py:64`, `review-source/Rolymoly-main/pages/5_경매내전.py:100`, `review-source/Rolymoly-main/pages/6_내전이력.py:295`, `review-source/Rolymoly-main/pages/8_멸망전.py:32`, `review-source/Rolymoly-main/database.py:850`.

**3. 가입·회원·권한 정책**

- 가입 시 # 구분자와 닉네임·태그의 빈 값 여부를 확인한다. 주·부 포지션은 달라야 한다. 생년월일을 받지만 별도의 가입 연령 기준은 구현되어 있지 않다.
- 신청은 PENDING 상태로 저장한다. 운영진 승인을 받으면 APPROVED가 되고 회원 목록·참가자 선택에 포함된다.
- 가입 거절은 신청 행 삭제, 강제 탈퇴는 KICKED 상태 변경이다. 강퇴 시 회원 데이터 자체를 삭제하지는 않는다.
- 가입 또는 닉네임 변경 시 기존 닉네임#태그와의 중복 검사는 없다. 같은 계정의 중복 신청이나 재신청을 별도로 제한하지 않는다.
- 운영진 회원관리 로그인은 “승인된 회원 중 is_admin=1인 닉네임#태그”와 공통 비밀번호를 비교한다. 닉네임 대소문자는 구분하지 않는다.
- 개인별 비밀번호, 로그인한 운영진의 신원 저장, 변경 사유·변경 전후 감사 이력, 로그인 시도 제한, 최초 운영진 생성 화면은 확인되지 않았다.
- 공통 비밀번호는 settings 시트에 평문으로 보관하며, 설정이 없거나 읽기 실패 시 코드에 있는 기본값으로 돌아간다.
- 일반내전·경매내전·내전이력·멸망전 페이지에는 운영진 로그인 검사가 없다. 앱 코드만 보면 해당 페이지에 접근한 사용자가 결과 생성·변경 또는 멸망전 삭제를 실행할 수 있다. 배포 서비스의 별도 접근 제한 여부는 확인하지 않았다.
- 개인정보 수집 안내·동의, 보유기간·파기, 휴면 처리, 출석·지각·노쇼 감점, 연령 제한 등 별도 운영 정책은 제공된 앱과 정책 문서에서 확인되지 않았다. 이는 법적 위반 판정이 아니라 자료에 해당 정책이 없다는 뜻이다.

근거: `review-source/Rolymoly-main/utils/helpers.py:121`, `review-source/Rolymoly-main/pages/1_가입.py:32`, `review-source/Rolymoly-main/database.py:101`, `review-source/Rolymoly-main/database.py:163`, `review-source/Rolymoly-main/database.py:217`, `review-source/Rolymoly-main/database.py:384`, `review-source/Rolymoly-main/pages/7_회원관리(운영진용).py:19`, `review-source/Rolymoly-main/database.py:55`, `review-source/Rolymoly-main/pages/6_내전이력.py:414`, `review-source/Rolymoly-main/pages/8_멸망전.py:288`.

**4. 파워스코어와 클랜 티어**

자동 기본점수 = 솔로랭크 점수 + 자유랭크 보너스  
적용 기본점수 = 수기 점수가 -1이면 자동 기본점수, 그 외에는 수기 점수  
최종 파워스코어 = 적용 기본점수 + 내전 누적 증감

수기 점수는 자동 기본점수 전체를 대체한다. 따라서 수기로 티어 점수를 지정한 경우 자유랭크 보너스가 별도로 더해지지 않는다. 우승 기호 포인트는 파워스코어와 별개다.

| 솔로랭크 | 점수 |
|---|---:|
| 언랭크 | 0 |
| 아이언 4~1 | 모두 10 |
| 브론즈 4/3/2/1 | 20 / 30 / 40 / 50 |
| 실버 4/3/2/1 | 60 / 70 / 80 / 90 |
| 골드 4/3/2/1 | 120 / 130 / 140 / 150 |
| 플래티넘 4/3/2/1 | 200 / 210 / 220 / 230 |
| 에메랄드 4/3/2/1 | 280 / 300 / 320 / 340 |
| 다이아몬드 4/3/2/1 | 390 / 420 / 450 / 480 |
| 마스터 0~99 / 100~199 / 200 이상 LP | 550 / 600 / 700 |
| 마스터 LP 정보 없음 | 600 |
| 그랜드마스터 | 800 |
| 챌린저 | 1,000 |

자유랭크는 아이언4 +1부터 하위 단계 하나마다 +1씩 증가하여 챌린저 +31이다. 언랭크는 0이다. 예를 들어 솔로 골드4 120 + 자유 브론즈4 5 + 내전 증감 20이면 최종점수는 145이다.

클랜 티어는 최종점수를 배점표에 대입해 즉시 결정한다. “5승마다 한 단계 승급”이나 승급전 카운터는 없다. 현재 표에서는 아이언 각 단계가 모두 10점이고 동점 정렬 시 아이언4가 먼저이므로 아이언1~3으로 판정되지 않는다. 최종점수가 음수여도 표시 티어는 아이언4다.

근거: `review-source/Rolymoly-main/config.py:38`, `review-source/Rolymoly-main/utils/tier_fetcher.py:12`, `review-source/Rolymoly-main/utils/helpers.py:43`, `review-source/Rolymoly-main/utils/tier_fetcher.py:99`.

**5. 일반내전 점수 정책과 날짜 경계**

| 기록의 날짜 | 저장·재계산에 쓰이는 규칙 |
|---|---|
| 2026-08-12 이전 | 승리: 직전 총점 × 4%를 정수로 잘라 가산. 패배: 가장 최근 승리에서 얻은 보너스 차감. 누적 내전 증감의 하한은 0 |
| 2026-08-12부터 | 경기 직전 총점 280 이상이면 승리 +15 / 패배 -15, 280 미만이면 +10 / -10. 음수 허용 |
| 2026-08-21 초기화 | 이 날짜 이후 첫 경기 직전에 모든 회원의 누적 내전 증감·마지막 승리 보너스를 0으로 초기화 |

초기화 날짜 이후 기록이 없더라도 재계산 실행일이 08-21 이후면 이전 내전 증감은 0으로 저장된다. 이력과 승률 통계는 삭제되지 않는다. 날짜 판정은 코드의 고정 문자열과 서버 시간에 의존하며 별도 시즌 설정 화면은 없다.

현재 증감의 기준은 실제 솔로랭크가 아니라 경기 직전 총점이다. 예를 들어 275점인 선수가 이기면 285점이 되고, 이어서 패배하면 15점이 차감되어 270점이 된다. 승리와 패배가 항상 상쇄되는 구조는 아니다.

승리팀이 확정된 NORMAL 기록만 점수·참가 판수·승률에 반영한다. 미확정 경기, AUCTION, 멸망전은 이 일반내전 통계에 포함되지 않는다. 승률은 소수점 한 자리로 표시한다.

결과 확정 저장, 일반내전 승리팀 변경, 확정된 일반내전 삭제 때 전체 이력을 시간순으로 다시 계산한다. 일반 20인 대회도 대회 종료 시 NORMAL 이력 한 건으로 저장된다. 따라서 라운드별 승패를 각각 점수에 넣는 것이 아니라 최종 우승팀은 1승, 나머지 팀은 1패로 집계되는 경로다.

근거: `review-source/Rolymoly-main/database.py:454`, `review-source/Rolymoly-main/database.py:543`, `review-source/Rolymoly-main/database.py:566`, `review-source/Rolymoly-main/database.py:609`, `review-source/Rolymoly-main/database.py:625`, `review-source/Rolymoly-main/database.py:639`, `review-source/Rolymoly-main/database.py:659`, `review-source/Rolymoly-main/database.py:768`, `review-source/Rolymoly-main/pages/6_내전이력.py:284`.

**6. 일반내전 팀 편성 규칙**

| 항목 | 10인 | 20인 |
|---|---|---|
| 입력 | TOP/JG/MID/AD/SUP 각각 2명 | 각 라인 4명 |
| 팀 구성 | 5명씩 2팀 | 5명씩 4팀 |
| 자동 편성 목표 | 양 팀 총 파워스코어 차이 최소 | 4팀 중 최대 총점 − 최소 총점 최소 |
| 탐색 | 라인별 양 팀 배정 32개 조합 | 첫 라인 고정, 나머지 4개 라인의 순열 최대 331,776개 |
| 수동 편성 | 입력 순서대로 배정 | 입력 순서대로 배정 |
| 편성 후 조정 | 같은 라인의 두 선수 교환 | 같은 라인의 팀 간 선수 교환 |
| 결과 처리 | 단일 NORMAL 이력 저장 | 단일 이력 / 풀리그 / 토너먼트 |

진행자는 회원 선택 또는 직접 이름 입력 방식이다. 인증된 진행자 신원과 연결되지는 않는다. 선택한 라인 안에서 팀 총점 차이를 최소화하며, 주·부 포지션과 다른 라인에 선수를 선택하는 것을 강제 차단하지 않는다. 라인별 실력 차이, 듀오 상성, 챔피언 숙련도 등을 별도 점수로 최적화하지 않는다.

근거: `review-source/Rolymoly-main/pages/4_일반내전.py:81`, `review-source/Rolymoly-main/pages/4_일반내전.py:113`, `review-source/Rolymoly-main/pages/4_일반내전.py:138`, `review-source/Rolymoly-main/pages/4_일반내전.py:305`, `review-source/Rolymoly-main/pages/4_일반내전.py:325`, `review-source/Rolymoly-main/pages/4_일반내전.py:469`.

**7. 경매 진행·예산·보상**

경매는 4·6·8팀을 고른다. 승인 회원이 전체 10명 이상이어야 화면을 진행할 수 있지만, 경매 시작 버튼은 선택 참가자가 1명 이상인지와 진행자·팀장 중복만 검사한다. 각 팀장을 반드시 채우거나 정확히 20·30·40명을 고르는 조건은 강제하지 않는다.

팀장은 팀원 수에 포함되고 낙찰 비용은 0이다. 팀장이 있으면 팀장 최종 파워스코어 구간으로 예산을 배정하며, 팀장이 없으면 1,000포인트를 준다.

| 팀장 점수 하한 | 예산 | 팀장 점수 하한 | 예산 |
|---:|---:|---:|---:|
| 700 | 690 | 600 | 790 |
| 550 | 840 | 480 | 910 |
| 450 | 940 | 420 | 970 |
| 390 | 1,000 | 340 | 1,050 |
| 320 | 1,070 | 300 | 1,090 |
| 280 | 1,110 | 230 | 1,160 |
| 220 | 1,170 | 210 | 1,180 |
| 200 | 1,190 | 150 | 1,240 |
| 140 | 1,250 | 130 | 1,260 |
| 120 | 1,270 | 90 | 1,300 |
| 80 | 1,310 | 70 | 1,320 |
| 70 미만 | 1,330 | | |

높은 구간부터 처음 충족하는 값을 사용한다. 예를 들어 팀장 점수 400이면 1,000포인트다.

선수는 무작위로 뽑고 진행자가 낙찰 팀·소모 포인트를 입력한다. 자동 최고가 경쟁·입찰 타이머·동점 입찰 우선권은 없다. 소모 포인트 입력 범위는 0~1,000이며 증감 버튼은 10 단위다. 낙찰 시 팀당 최대 5명, 잔액 부족을 검사한다. 유찰자는 별도 풀에서 수동 배정할 수 있다. 낙찰 후 팀원 이동·소모 포인트 수정·잔액 직접 변경도 제공한다.

경매 우승 보상은 확정된 AUCTION 이력의 참가자 행 수로 계산한다.

| 참가자 행 수·시점 | 우승팀 각 선수에게 주는 보상 |
|---|---|
| 40명 이상 | 별 포인트 5 = 메달 1 |
| 30~39명 | 별 포인트 1 = 별 1 |
| 30명 미만, 2026-07-15 21:00 이전 | 별 1 |
| 30명 미만, 07-15 21:00부터 07-21 21:00 직전 | 보상 없음 |
| 20~29명, 07-21 21:00 이후 | 고양이 1 |
| 20명 미만, 07-21 21:00 이후 | 보상 없음 |

고양이 5개 = 별 1, 별 5개 = 메달 1, 별 25개 = 트로피 1이다. 자동 경매 보상과 운영진 수기 별 포인트는 합산한다. 다만 수기 고양이 합산과 나머지 고양이 1~4개 출력은 현재 빠져 있다. 일부 경매 화면은 고양이 환산도 사용하지 않아 화면별 기호가 달라질 수 있다.

근거: `review-source/Rolymoly-main/config.py:61`, `review-source/Rolymoly-main/utils/helpers.py:81`, `review-source/Rolymoly-main/pages/5_경매내전.py:119`, `review-source/Rolymoly-main/pages/5_경매내전.py:157`, `review-source/Rolymoly-main/pages/5_경매내전.py:288`, `review-source/Rolymoly-main/pages/5_경매내전.py:332`, `review-source/Rolymoly-main/pages/5_경매내전.py:502`, `review-source/Rolymoly-main/database.py:797`, `review-source/Rolymoly-main/utils/helpers.py:93`.

**8. 대회·이력 규칙**

- 풀리그는 모든 팀쌍이 1회 대결한다. 4팀 6경기, 6팀 15경기, 8팀 28경기다.
- 승리 +3점, 패배 +0점이며 승점 → 킬−데스 순으로 표시한다. 완전 동률의 별도 재경기·공동 순위 정책은 없다.
- 토너먼트 4팀은 준결승→결승이다. 6팀은 무작위 2팀이 부전승하고 나머지 4팀의 1라운드 2경기 후 준결승→결승으로 진행한다. 8팀 선택지는 있지만 대진 생성 분기가 빠져 있다.
- 경매 6·8팀에서는 두 조로 나눠 조 내부 풀리그 후 결승을 치를 수 있다. 각 조 1위는 수동 선택하며 계산된 1위와 일치하도록 강제하지 않는다.
- 최종 우승팀은 전체 팀 중 수동 선택한다. 미완료 경기나 실제 결승 승자와의 불일치를 막지 않는다.
- 진행 대회의 경기별 상태는 로컬 tournaments.json에, 종료 시 최종 결과·전체 참가자는 Sheets의 이력 한 건으로 저장한다.
- 종료 이력에서 승리팀을 바꾸면 일반내전 점수는 전체 재계산되고 경매 우승 기호는 현재 승리팀 기준으로 다시 집계된다.
- 관리자 전체 이력 초기화는 matches, match_players 및 회원 match_bonus를 지운다. 수기 기본점수·수기 별, 로컬 대회 파일, 멸망전 기록까지 전부 초기화하는 기능은 아니다.

근거: `review-source/Rolymoly-main/utils/tournament_manager.py:42`, `review-source/Rolymoly-main/utils/tournament_manager.py:81`, `review-source/Rolymoly-main/utils/tournament_manager.py:119`, `review-source/Rolymoly-main/pages/6_내전이력.py:98`, `review-source/Rolymoly-main/pages/6_내전이력.py:208`, `review-source/Rolymoly-main/pages/6_내전이력.py:276`, `review-source/Rolymoly-main/pages/6_내전이력.py:414`, `review-source/Rolymoly-main/database.py:399`.

**9. 멸망전 규칙**

- 팀과 선수 명단은 database.py에 9팀으로 고정되어 있다.
- 한 매치는 2세트다. 팀당 최대 8매치, 즉 16세트이며 같은 팀끼리 대결하거나 동일 팀쌍을 두 번 등록할 수 없다. 이 검사는 결과 등록 화면에서 완료된 기록을 기준으로 한다.
- 세트 승리 +3점, 패배 +0점이다. 2승이면 매치에서 6점, 1승1패이면 각 팀 3점이다.
- 순위는 승점 → 완료된 모든 매치의 세트별 킬득실 합계 → 승자승 순으로 정한다. 화면의 “KDA득실차”는 어시스트를 포함한 KDA가 아니라 팀 킬 수 차이다.
- 매치 전체 기권은 양팀 모두 2패, 각 팀 킬득실 −10으로 처리한다. 한 팀만 기권하고 상대에게 승리를 주는 별도 선택지는 없다.
- 3팀 이상 동률·순환 승자승·완전 동률의 최종 순위 결정 방식은 명확하지 않다. 공동 순위도 표시하지 않는다.
- 멸망전 결과는 일반 파워스코어나 경매 우승 기호와 연결되어 있지 않다.
- DB에는 일정 생성·참가·초기화 함수도 남아 있지만 현재 화면은 순위와 결과 직접 등록 위주다. 함수가 있다는 이유로 일정 예약 기능을 완성된 화면 기능으로 보지는 않았다.

근거: `review-source/Rolymoly-main/database.py:850`, `review-source/Rolymoly-main/pages/8_멸망전.py:53`, `review-source/Rolymoly-main/pages/8_멸망전.py:101`, `review-source/Rolymoly-main/pages/8_멸망전.py:255`, `review-source/Rolymoly-main/database.py:903`.

**10. 우선 수정할 문제**

“높음”은 권한·점수·기록 신뢰도에 직접 영향을 주는 문제다. 아래 현상은 코드 경로를 근거로 판단했으며 운영 서버에서 발생 사실을 확인했다는 의미는 아니다.

| 우선순위 | 문제 | 사용자에게 미치는 영향 | 근거 |
|---|---|---|---|
| 높음 | 결과 입력·수정·멸망전 삭제 페이지의 인증 누락 | 앱 접근자에게 기록과 보상을 바꾸는 UI가 열림 | `review-source/Rolymoly-main/pages/6_내전이력.py:414`, `review-source/Rolymoly-main/pages/8_멸망전.py:334` |
| 높음 | 공통 비밀번호 평문 저장·설정 실패 시 기본값 사용 | 개인별 책임 추적이 어렵고 설정 장애가 기본 비밀번호 허용으로 이어짐 | `review-source/Rolymoly-main/database.py:55`, `review-source/Rolymoly-main/database.py:66` |
| 높음 | 수기 내전 증감을 자동 누적과 같은 열에 저장 | 다음 일반내전 재계산 시 모든 회원의 수기 보정이 사라짐. “수정 이후 정상 누적” 안내와 다름 | `review-source/Rolymoly-main/database.py:247`, `review-source/Rolymoly-main/database.py:601`, `review-source/Rolymoly-main/database.py:676`, `review-source/Rolymoly-main/pages/7_회원관리(운영진용).py:304` |
| 높음 | 별도 재계산 스크립트가 구형 4% 방식 | 스크립트 실행 시 현행 ±10/15 및 08-21 초기화를 무시하고 점수를 덮어씀 | `review-source/Rolymoly-main/scripts/recalculate_history.py:46` |
| 높음 | 경기 조회 실패를 빈 이력으로 처리 | Sheets 읽기 오류 후 재계산이 계속되면 실제 이력이 있는데도 회원 증감을 0으로 덮어쓸 수 있음 | `review-source/Rolymoly-main/database.py:466`, `review-source/Rolymoly-main/database.py:475`, `review-source/Rolymoly-main/database.py:590`, `review-source/Rolymoly-main/database.py:676` |
| 높음 | 저장 성공 여부 미확인·여러 저장의 일괄 성공 보장 없음 | 가입·일부 관리 작업에서 실패해도 성공 문구. 대회는 로컬 완료 후 Sheets 저장 실패 시 진행 목록에서 사라질 수 있음 | `review-source/Rolymoly-main/pages/1_가입.py:43`, `review-source/Rolymoly-main/pages/6_내전이력.py:288`, `review-source/Rolymoly-main/database.py:522` |
| 높음 | 최종 우승·경매 팀 완성도 검증 부족 | 경기 미완료·잘못된 우승팀·5명 미만 팀으로 기록과 보상 생성 가능 | `review-source/Rolymoly-main/pages/5_경매내전.py:547`, `review-source/Rolymoly-main/pages/6_내전이력.py:282` |
| 중간 | 관리자 보상 조정이 존재하지 않는 수기 고양이 열 조회 | 해당 회원 선택 시 KeyError가 발생하는 코드 경로. 별·메달·트로피 설정까지 막힘 | `review-source/Rolymoly-main/pages/7_회원관리(운영진용).py:174`, `review-source/Rolymoly-main/pages/7_회원관리(운영진용).py:251` |
| 중간 | 8팀 토너먼트 대진 미구현 | 빈 대진표로 생성됨. 최종 우승 수동 지정은 여전히 가능 | `review-source/Rolymoly-main/utils/tournament_manager.py:81` |
| 중간 | 이전 라운드 승자 변경 시 후속 경기 승자 유지 | 결승 참가자는 바뀌는데 기존 승자가 남아 이력 화면에서 ValueError가 날 수 있음 | `review-source/Rolymoly-main/utils/tournament_manager.py:218`, `review-source/Rolymoly-main/pages/6_내전이력.py:266` |
| 중간 | 과거 당시 점수 미보존 | 현재 티어·수기 기본점수 변경 후 재계산하면 과거 경기 증감도 달라질 수 있음 | `review-source/Rolymoly-main/database.py:507`, `review-source/Rolymoly-main/database.py:535`, `review-source/Rolymoly-main/database.py:601` |
| 중간 | OP.GG 파싱 실패를 언랭크 0점과 구분하지 않음 | 일괄 갱신 때 일시 조회 실패가 정상 티어 정보를 덮어쓸 수 있음 | `review-source/Rolymoly-main/utils/tier_fetcher.py:126`, `review-source/Rolymoly-main/utils/tier_fetcher.py:192`, `review-source/Rolymoly-main/pages/7_회원관리(운영진용).py:95` |
| 중간 | Master LP를 파싱 도중 덮어씀 | 솔로·자유랭크가 모두 발견된 일부 경로에서 550/700 대신 600점으로 계산 | `review-source/Rolymoly-main/utils/tier_fetcher.py:168`, `review-source/Rolymoly-main/utils/tier_fetcher.py:23` |
| 중간 | 수기 고양이 미합산·잔여 고양이 미출력 | 보상이 없어진 것처럼 보이거나 화면 간 기호가 달라짐 | `review-source/Rolymoly-main/pages/3_회원리스트.py:41`, `review-source/Rolymoly-main/utils/helpers.py:98` |
| 중간 | 전역 임시 파일·파일 잠금 없음·초 단위 대회 ID | 동시 운영자 저장 덮어쓰기·대회 ID 충돌 가능 | `review-source/Rolymoly-main/utils/tournament_manager.py:7`, `review-source/Rolymoly-main/utils/tournament_manager.py:18`, `review-source/Rolymoly-main/utils/tournament_manager.py:33` |
| 중간 | Sheets ID가 최대값+1 방식, 중복 결과 방지 키 없음 | 동시 등록 시 ID 또는 기록 중복 가능. 코드상 방지책이 부족하다는 판단이며 실제 어뷰징 증거는 아님 | `review-source/Rolymoly-main/database.py:89`, `review-source/Rolymoly-main/database.py:522` |
| 낮음 | 가입과 관리자 포지션 값 체계 불일치 | 가입은 TOP/JG 등, 수정 화면은 탑/정글 등. 관리자 수정은 주·부 동일/빈 값도 허용 | `review-source/Rolymoly-main/pages/1_가입.py:23`, `review-source/Rolymoly-main/pages/7_회원관리(운영진용).py:382` |
| 낮음 | 재계산 완료 후 캐시 미제거 | 최대 설정 TTL 60초 동안 이전 점수가 보일 수 있음 | `review-source/Rolymoly-main/database.py:589`, `review-source/Rolymoly-main/database.py:680`, `review-source/Rolymoly-main/config.py:26` |
| 낮음 | 멸망전 로스터를 낮은 유사도까지 이름 매칭 | 서로 다른 선수의 파워스코어가 붙을 가능성 | `review-source/Rolymoly-main/pages/8_멸망전.py:186` |

추가 정책 결정 사항: 경매 팀 잔액은 제한 없이 직접 수정할 수 있다. 이를 운영진의 예외 권한으로 둘지, 예산 변경 근거를 남길지 정해야 한다. 현재는 초기 예산을 항상 지키도록 보장하는 구조가 아니다.

**11. 문서와 구현의 충돌**

| 문서·안내 | 문서에 적힌 내용 | 최상위 코드 |
|---|---|---|
| 회의내용.txt | 5승마다 한 단계 승급, 전적·포지션·검색·확정 안내 등 요구 | 승급은 최종점수 문턱. 요구 목록 전체가 현행 정책은 아님 |
| 티어별_점수_배점표.txt | 자유랭크 점수 10% 반영 | 자유랭크 +1~31 선형 보너스 |
| 파워스코어_배점표_상세.txt 및 운영진 화면의 배점 안내 | 인접 티어 점수 차이 / 5 방식의 내전 증감 | 08-12 이후 직전 총점 기준 ±10/15 |
| 260715_패치노트.txt | 티어별 비대칭 승패 배점 | 과거 변경 설명. 현재 저장 경로와 다름 |
| 내전_파워스코어_계산로직_설명서.txt | 2026-07-22 기준 승리 4%, 직전 승리 보너스 차감 | 08-12 이전 이력에만 해당. 08-21 증감 초기화 추가 |
| 패치노트_최근업데이트.md | 일반 20인에서도 조별리그 선택 가능하다고 설명 | 일반 20인 UI에는 단판·풀리그·토너먼트만 제공. 조별리그는 경매 6·8팀에서 제공 |
| 전달용/사용_설명서.md | run.bat 실행을 패키지 설치 방법으로 안내 | run.bat은 패키지를 설치하지 않고 앱 실행만 시도 |
| 관리자 수기 증감 안내 | 보정 이후 해당 점수를 기준으로 누적 | 전체 재계산이 수기 보정을 없앰 |
| 고양이 보상 관련 구현·주석 | 고양이 5개 환산, 잔여 기호 표시 취지 | 환산은 일부 화면에 적용, 잔여 출력·수기 합산 누락 |

문서 두 개(회의내용.txt, 티어별_점수_배점표.txt)는 CP949, 나머지 주요 문서는 UTF-8로 읽어 대조했다. 문서 작성 시점이 다르므로 옛 설명을 모두 현행 규정으로 합치면 안 된다.

비정상 점수 의심자 문서는 당시 티어·점수를 이용한 추정 자료다. 언급된 개인의 부정행위나 문서의 높은 확률 표현을 입증하는 원본 감사 로그는 포함되어 있지 않다. 이 보고서는 특정 회원을 어뷰저로 판정하지 않는다.

.agents/AGENTS.md에는 DB·UI·검토 태그별 개발 역할 지침이 있다. 이는 클랜 운영 규정이나 사용자의 이번 요청이 아니다.

근거: `review-source/Rolymoly-main/회의내용.txt:1`, `review-source/Rolymoly-main/티어별_점수_배점표.txt:1`, `review-source/Rolymoly-main/파워스코어_배점표_상세.txt:1`, `review-source/Rolymoly-main/260715_패치노트.txt:1`, `review-source/Rolymoly-main/내전_파워스코어_계산로직_설명서.txt:1`, `review-source/Rolymoly-main/패치노트_최근업데이트.md:19`, `review-source/Rolymoly-main/전달용/사용_설명서.md:12`, `review-source/Rolymoly-main/.agents/AGENTS.md:1`.

**12. 실행·보관 구조와 검증 한계**

- 실행 진입점은 app.py이며 run.bat도 streamlit run app.py를 호출한다.
- 필수 패키지는 Streamlit, pandas, requests, beautifulsoup4, gspread, python-dotenv다. requirements.txt에 버전 고정은 없다.
- 회원·설정·경기·참가자는 Google Sheets의 users, settings, matches, match_players를 전제로 한다. 멸망전은 deathmatch_schedules를 사용한다.
- 인증은 Streamlit secrets의 gcp_service_account 또는 실행 위치의 credentials.json을 읽는다. ZIP에는 실제 인증 파일이 없다. .env.example은 인증 키가 아니다.
- init_db는 비어 있다. 멸망전 시트 생성 함수도 정의만 있고 현재 앱의 호출 지점을 찾지 못했다. 새 Sheets와 최초 운영진을 자동 준비하는 완결된 초기화 절차는 제공되지 않는다.
- 설정 파일에 기본 Google Sheet ID가 들어 있다. 실제 앱에 사용할 연결 대상과 서비스 계정 권한은 별도로 확인해야 한다.
- 진행 중 대회와 임시 편성은 로컬 JSON에 저장하므로 Sheets만 보관하면 진행 상태가 모두 보존되는 것은 아니다.
- .env.example의 안내와 달리 CACHE_TTL은 config.py에서 60초로 고정하고, 실제 로깅은 database.py에서 INFO로 고정한다. 해당 환경변수 수정만으로 적용되지 않는다.
- 자동 테스트 파일은 제공된 소스에서 확인되지 않았다. 이번 검토에서는 파일·함수·호출 흐름 대조와 세 버전 비교를 수행했다. 화면 렌더링, 외부 티어 조회 성공률, 동시 접속, 실제 Sheets 데이터의 정합성은 미검증이다.

근거: `review-source/Rolymoly-main/run.bat:3`, `review-source/Rolymoly-main/requirements.txt:1`, `review-source/Rolymoly-main/database.py:13`, `review-source/Rolymoly-main/database.py:49`, `review-source/Rolymoly-main/database.py:865`, `review-source/Rolymoly-main/config.py:11`, `review-source/Rolymoly-main/config.py:25`, `review-source/Rolymoly-main/.env.example:8`.

**13. 정비 순서 제안**

1. 실제 사용할 코드 묶음을 하나로 정하고, 현행 점수·보상·초기화 기준을 문서 한 곳에 명시한다.
2. 결과 생성·변경·삭제에 운영진 권한을 적용하고, 개인별 처리자와 변경 사유를 남긴다.
3. 자동 경기 증감과 수기 보정을 분리하고, 재계산 스크립트·화면 안내를 같은 계산 함수에 맞춘다.
4. 읽기 실패 시 재계산을 중단하고, 저장 성공 확인·중복 결과 방지·실패 복구를 보강한다.
5. 보상 조정 오류, 8팀 대진, 후속 경기 승자 무효화, 팀 완성도·최종 우승 검증을 수정한다.
6. 고양이 표시·Master LP 처리·당시 점수 보존을 정리하고, 별도 테스트 데이터로 날짜 경계와 결과 수정·삭제를 검증한다.

이번 요청 범위에서는 확인과 보고까지만 수행했다.
