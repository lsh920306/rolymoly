# Rolymoly 화면 기준

최신 경매 화면은 [참고 이미지와 구현 매핑](AUCTION_REFERENCE_MAPPING.md)을 우선합니다. 상단 선수 순서·오른쪽 음량/경매 초기화, 왼쪽 팀 슬라이드/전체 팀 팝업, 중앙 포인트·입찰 조작을 해당 문서에 정리했습니다. 아래 내용은 기본 테마와 이전 요청의 이력입니다.

2026-09-07 최신 사용자 요청은 **화면 구성은 유지하고, 제공한 위밍글 라운지 이미지의 디자인을 참고하는 것**입니다. GoodChoice 디자인을 현재 화면에 적용하라는 앞선 요청은 취소되었습니다. 아래 기준을 우선하며, 뒤의 원본 문서는 참고 이력으로 보존합니다.

- `app.py`로 실행하는 Streamlit 운영 화면을 유지합니다.
- 페이지 이동은 왼쪽 사이드바 하나로 구성합니다. 공간 선택과 계정은 메뉴 아래에 둡니다.
- 라운지는 클랜 소개 아래 왼쪽에 경매 생성·일정, 일반내전, 최근 경기를 배치하고 오른쪽에 전력·기록 링크, 클랜원, 관리자 전용 가입 신청을 배치합니다. 최신 요청에 따라 모임 일정은 제거했습니다.
- 큰 소개 문구, 기능 홍보 카드, 같은 크기의 KPI 카드, 장식용 파란 배지를 사용하지 않습니다.
- 실제 기록과 가능한 행동을 우선합니다. 저장되지 않은 일정·모집 단계·점수를 예시로 만들어 표시하지 않습니다.
- 기본 배경은 연분홍 `#FFF7F7`, 주요 행동은 코랄 `#F94239`, 본문은 네이비 `#1B223D`, 카드·입력 배경은 흰색, 테두리는 옅은 분홍 `#F9E6E6`을 사용합니다. 성공·오류는 문구와 상태색을 함께 유지합니다.
- Pretendard 본문 15px, 페이지 제목 24px를 유지합니다. 버튼 모서리는 8px, 입력·회원 아바타는 12px, 회원 카드는 16px, 라운지 패널은 20px입니다.
- 색상·글꼴·위젯 모서리는 `.streamlit/config.toml`로 설정합니다. `static/app.css`는 명시적인 `lounge_*` 컨테이너 키에 흰 패널·안쪽 여백·옅은 그림자를 적용합니다. 회원 카드는 스타일이 분리된 `roly/member_cards.py` 컴포넌트가 담당합니다. 사이드바는 중립색을 유지하며 상단 탐색 메뉴를 추가하지 않습니다.
- 클랜원 카드는 이름, 포지션·전력, 판수·업적을 왼쪽 정렬한 별도 줄로 표시하고 첫 글자 아바타를 사용합니다. 검색 결과 전체를 최대 480px 패널 안에서 스크롤하며 페이지 선택을 두지 않습니다. MBTI·개인 소개글·가상 프로필 사진·확인하지 않은 티어·다른 클랜 정보를 추가하지 않습니다.
- 일정 건수는 제목 오른쪽에 정렬합니다. 코랄 생성 안내 영역은 흰 본문과 코랄 글자의 흰 버튼으로 구성하며, 버튼 내부 글자까지 본문색으로 덮어쓰지 않습니다.
- 소개 이미지는 사용자 제공 `rolymoly.png` 원본을 보관한 `static/rolymoly-poster.png` 한 장으로 통일하며, 이전 포스터와 이미지 선택 항목은 제거했습니다. 포스터를 새로 생성·합성하지 않으며 그림의 날짜·문구를 운영 데이터나 정책으로 자동 입력하지 않습니다.
- 회원 목록에는 실제 업적에 따른 🏆·🏅·⭐·🐱 우승 기호를 표시합니다. 우승 업적과 전력점수는 분리합니다.
- 기록은 사이드바의 경기 기록 아래 일반내전 / 경매 탭으로 나눕니다. 현재 경매 기능을 별도 대회라고 부르지 않습니다.
- 경매 생성은 별도 창으로, 일반내전은 정보 입력과 참가자 등록 두 구역으로 정리합니다. 참가자는 하나의 검색 선택창에서 모으고 배정 포지션을 표에서 수정합니다. 일반내전은 포지션별 부족·초과 인원을 보여주며 경매는 낙찰 후 최종 팀 포지션을 확인합니다.
- 경매 진행 중에는 상단 선택과 접힌 상세 정보만 두고, 팀장 상단·팀원 하단의 팀 카드, 경매 대상 선수, 최고 입찰 팀장·금액·시계, 입찰 입력을 구분합니다. 전체 팀 비교는 별도 창이며 입찰 완료 후에는 전체 팀을2열 카드로 표시합니다.
- 경매 소리는 사용자에게 켜기·끄기와0~100% 음량 조절을 제공합니다. 타이머는 서버 상태를 보간해 표시하며 입찰·낙찰 판정은 서버가 담당합니다. 자동 갱신하므로 수동 새로고침 버튼을 두지 않습니다.
- 출석은Discord에서 관리하고 앱에는 출석 요청·확인·미출석 관리 화면을 두지 않습니다. 사이드바의 기존 외부 롤리몰리 웹사이트 링크도 제거합니다.

현재 기능과 실행 방법은 [README](README.md)를 기준으로 확인합니다. 아래 GoodChoice 원문은 현행 화면의 적용 규칙이 아닙니다.

---

# 여기어때 (GoodChoice) Reference Design System

<!-- design-md:section experience -->
## 1. Experience

### Visual Theme & Atmosphere

여기어때의 공개 디자인 체계는 숙소와 여행 상품을 빠르게 탐색·비교·예약하게 하는 제품 UI와 이를 지탱하는 YDS 6.0으로 나뉩니다. 실서비스는 목적지 사진이 주도하는 카드, 흰 배경, `#222222` 텍스트, Cyan 800 `#1D8BFF` 액션을 사용해 가격·평점·혜택·예약 가능성을 한 흐름에서 읽게 합니다. 공식 Design Library는 이 제품 관찰값보다 넓은 Lively Red, Cyan, Neutral, membership, multi-color 팔레트와 foundation 규칙을 제공합니다. 따라서 이 reference는 여행의 활기라는 브랜드 인상과 실제 예약 UI의 효율을 함께 설명하되, library token과 특정 live surface 측정값을 서로 대체하지 않습니다.

2026-07-11 수집은 홈·국내 숙소 결과·공식 Design Library 6개 route, 총 8 surfaces를 대상으로 했습니다. 37 colors, 21 font families, 30 component variants, 3 interactions, coverage 95/100을 확보했고, `Pretendard`는 560개 요소에서 loaded/high confidence로 확인됐습니다.

### Brand Narrative

공식 Design Library가 밝히는 범위에서 여기어때의 디자인 언어는 여행을 위한 시각 언어이며 app/web 일관성을 유지하는 데 목적이 있습니다. YDS는 색, typography, layout, component를 공통 언어로 제공하고, 실제 제품은 이를 사진 중심 탐색과 예약 결정에 적용합니다. 이 결합이 중요한 이유는 여행 서비스가 영감과 거래를 동시에 다루기 때문입니다: 이미지는 가고 싶은 마음을 만들고, 명확한 정보 위계와 CTA는 그 마음을 실행 가능한 예약으로 바꿉니다.

현재 공개 surface에서 확인되는 정체성은 활기만 강조하는 광고 브랜드가 아니라, 수많은 숙소 조건을 비교하는 사용자의 부담을 줄이는 제품입니다. 그래서 accent color는 행동과 선택 상태에 집중되고, typography는 장식보다 가격·평점·혜택의 스캔을 지원합니다. 창업·인수·시장순위 같은 제3자 기업 서사는 디자인 근거로 사용하지 않습니다.

YDS와 live product를 함께 읽으면 여기어때의 차별점은 특정 장식 하나보다 여행 결정의 리듬에 있습니다. 넓은 탐색에서는 사진과 카테고리가 관심을 만들고, 목록에서는 반복 가능한 metadata가 비교를 돕고, 상세와 예약에서는 행동과 조건이 선명해집니다. 이 reference는 그 리듬을 재현하되 공식 foundation에 없는 native pattern이나 campaign 표현을 전체 시스템 규칙으로 확대하지 않습니다.

### Personas

공식 Design Library와 공개 제품 surface에는 검증 가능한 persona 정의가 없습니다. 확인 가능한 것은 작업 맥락입니다: 목적지와 날짜를 정해 숙소를 검색하는 사람, 가격·평점·혜택을 비교하는 사람, 예약 조건을 확인하고 결제 단계로 이동하는 사람, 그리고 app/web 사이에서 같은 여행을 이어가는 사람입니다. 이는 인구통계학적 persona가 아니며, 별도 사용자 조사 없이 이름·나이·동기·인용문을 만들어서는 안 됩니다.

<!-- design-md:section foundations -->
## 2. Foundations

<!-- design-md:claim foundations kind=rules-or-constraints lang=en -->
### Color Palette & Roles

- **Cyan 800 `#1D8BFF`**: 현재 제품의 주요 탐색·행동 강조. 공식 palette token입니다.
- **Lively Red 800 `#F94239`**: 경고·할인 계열 의미에 쓰이는 공식 red scale의 핵심값입니다.
- **Lively Red 100 `#FFEDEA`**: red-tint soft surface.
- **White `#FFFFFF` / Neutral 900 `#222222`**: 기본 surface와 foreground.
- **Neutral 100 `#E6E6E6`**: 제품의 outline과 divider에서 관찰되는 light border.
- **Cyan 100 `#E3F0FF`**: blue soft surface.
- **Yellow 800 `#FFC83B`**: rating·multi-color emphasis.
- **Navy 500 `#49627A`**: member-price 계열 보조 강조.

채도가 있는 palette에는 임의 opacity를 적용하지 않는 것이 YDS 원칙입니다. 예외 opacity token은 공식 문서에 별도로 열거된 값만 사용합니다.
<!-- design-md:claim-end -->

### Layout & Spacing

기본 화면 좌우 margin은 20px, JTBD module을 강조하는 화면은 10px입니다. module width는 screen width minus 20px, radius는 20px, module section bottom spacing은 12px입니다. 공식 spacing scale은 2, 4, 8, 10, 12, 14, 16, 20, 24, 28, 32, 36, 40, 48, 56, 64, 96px입니다.

### Shape, Border & Elevation

공식 radius tokens는 2, 3, 4, 8, 10, 12, 16, 20px와 50%입니다. Core sizes는 8, 12, 20px입니다. Shadow는 Flat, Header, Dock, Raised, Float, Sheet의 용도별 단계이며, 반복 card에는 Flat 또는 Raised, dialog에는 Raised, bottom sheet에는 Sheet를 사용합니다.

<!-- design-md:section typography-assets -->
## 3. Typography & Assets

### Typography Rules

### Font evidence boundary

| Evidence class | Resolution |
|---|---|
| Official product-use | YDS 6.0 typography foundation이 `Pretendard` 사용을 명시합니다. |
| Live surface-use | 홈·숙소 결과·Design Library에서 loaded Pretendard가 560개 요소에 관찰됐습니다. |
| Official distributed asset | 별도의 여기어때 전용 제품 서체 배포 근거는 확인되지 않았습니다. |
| Declared-only | 다른 family 선언은 visible usage 없이는 승격하지 않습니다. |
| Evidence boundary | 공개 foundation 밖의 native-app 전용 family는 검증 전까지 미확정입니다. |

Specimen availability is separate from family truth and requires a loadable, licensed source.

국문·영문·숫자 모두 **Pretendard**를 사용합니다. 공식 scale은 Display Large 32/38부터 Badge 9/11까지 이어지고, UI Typo는 16·15·14·13·12·11·10px의 Semibold 계층을 제공합니다. Letter spacing은 별도값을 지정하지 않으며, 두 줄 이상은 multiline role을 사용합니다. Underline은 링크, strikethrough는 가격 원가에 한정합니다.

### Imagery & Iconography

실서비스의 핵심 시각 자산은 숙소와 여행지 사진입니다. 아이콘은 장식보다 검색·필터·탐색 affordance를 보조해야 합니다. 공식 iconography를 확인할 수 없는 새 glyph나 임의 stroke 규칙은 추가하지 않습니다.

### Do

- 숙소와 여행지 사진을 탐색 정보의 첫 단서로 사용합니다.
- 검색·필터 아이콘에는 텍스트 label이나 접근 가능한 이름을 제공합니다.

### Don't

- 검증되지 않은 icon stroke나 brand illustration 규칙을 만들지 않습니다.
- 저대비 사진 위에 핵심 가격이나 행동 label을 직접 올리지 않습니다.

<!-- design-md:section components-states -->
## 4. Components & States

### Component Stylings

공개 YDS component catalog는 현재 Button, Price marker, Search bar를 명시합니다. 아래 숙소 filter와 card는 동일 capture에서 확인한 제품 패턴이며, 공개 YDS 명세와 혼동하지 않습니다.

### Buttons

**Primary Action**
- Type: button
- Background: `#1D8BFF`
- Text: `#FFFFFF`
- Radius: 8px
- States: enabled, pressed, disabled
- Use: 검색과 다음 단계로 이어지는 핵심 행동

### Inputs

**Search Bar**
- Type: input
- Background: `#FFFFFF`
- Text: `#222222`
- Radius: 12px
- States: idle, focused, typing, populated
- Use: 여행지와 숙소 검색

### Cards

**Accommodation Listing**
- Type: card
- Background: `#FFFFFF`
- Radius: 12px
- Shadow: none or Flat only
- Use: 사진, 숙소명, 위치, 평점, 가격의 반복 결과 구조

### Badges

**Price Marker**
- Type: badge
- Background: `#FFFFFF`
- Text: `#222222`
- Radius: 20px
- States: default, selected
- Use: 지도 위 가격 탐색

**Filter Chip — observed product pattern**
- Type: badge
- Background: `#FFFFFF`
- Text: `#222222`
- Border: 1.5px solid `#E6E6E6`
- Radius: 50%
- Use: 가격·등급·편의시설 필터

### Component Patterns

제품 결과 화면의 반복 문법은 `photo → property metadata → name → location → rating → price`입니다. 검색은 하나의 명확한 query surface로 유지하고, 필터는 결과를 가리지 않는 compact chip으로 제공합니다. 공개 YDS component coverage가 세 family에 한정되므로 비공개 component anatomy를 추정해 채우지 않습니다.

<!-- design-md:section layout-platforms -->
## 5. Layout & Platforms

### Responsive Behavior

YDS layout은 모바일 screen margin을 20px로 두고, 강조 module에서 10px로 줄입니다. desktop web의 최대폭이나 breakpoint는 공개 foundation에서 확인되지 않았으므로 구현 환경의 content constraint를 따르되 토큰처럼 주장하지 않습니다.

제품 카피는 여행자가 지금 결정해야 하는 정보와 다음 행동을 짧게 연결합니다. 목적지·날짜·인원·가격·혜택처럼 비교에 필요한 명사는 먼저 보여주고, 검색·예약·확인 CTA는 동사형으로 유지합니다. 감성적인 여행 문구는 hero나 campaign에 둘 수 있지만 가격 조건, 취소 규정, 재고, 오류 상태를 덮어서는 안 됩니다. 확인되지 않은 push·오류 문구 규칙은 만들지 않으며, live surface에서 관찰된 용례만 예시로 사용합니다.

<!-- design-md:section content-locales -->
## 6. Content & Locales

<!-- design-md:section governance -->
## 7. Governance

### Preserved source material — Interaction & Motion

This material is retained for review because it has not yet been assigned to a typed Core field. Do not promote it to a token or product fact without an explicit decision.

확인된 상태는 버튼·검색바·price marker의 기본/선택/입력 흐름과 product filter interaction입니다. duration이나 easing은 공개 source에서 확인되지 않았으므로 고정 token을 만들지 않습니다. 상태 변화는 색상만이 아니라 label, border, focus affordance로도 구분합니다.

### Preserved source material — Design Principles

This material is retained for review because it has not yet been assigned to a typed Core field. Do not promote it to a token or product fact without an explicit decision.

- 여행 선택에 필요한 정보 위계를 먼저 보여줍니다.
- app/web 간 foundation과 component 사용을 일관되게 유지합니다.
- 강조는 JTBD가 필요한 최상단 module에 제한합니다.
- 공식 token과 실서비스 관찰 패턴의 증거 수준을 구분합니다.
- 가격·혜택·취소 조건 같은 결정 정보를 이미지나 감성 문구 뒤에 숨기지 않습니다.
- 색만으로 선택·오류·할인을 구분하지 않고 텍스트와 구조를 함께 제공합니다.
- **사진과 정보가 역할을 나눕니다.** 사진은 장소의 성격을 전달하고, 텍스트는 비교와 거래 조건을 책임집니다.
- **surface 근거를 보존합니다.** YDS 규칙, 웹 관찰값, 확인되지 않은 앱 동작을 한 수준의 사실로 합치지 않습니다.

### Accessibility

작은 badge와 caption에서도 의미를 색상 하나에만 의존하지 않습니다. 사진 위 텍스트는 별도 contrast surface를 확보하고, search·filter·button은 visible focus와 명시적 label을 유지합니다. 정확한 contrast ratio나 target size는 별도 검증 전에는 주장하지 않습니다.

### Implementation Checklist

- Pretendard를 국문·영문·숫자 공통 family로 사용합니다.
- Cyan 800을 product primary action에 사용하되 전체 브랜드를 단일 blue palette로 축약하지 않습니다.
- 20px 기본 screen margin과 공식 spacing/radius scale을 우선합니다.
- 공개 YDS components와 product-observed patterns를 코드·문서에서 구분합니다.
- 새로운 motion, persona, breakpoint, component state를 근거 없이 만들지 않습니다.

<!-- design-md:claim authority kind=evidence-backed-reconstruction lang=en -->
### Authority

This document is an evidence-backed reconstruction, not authority for an unrelated target project.
<!-- design-md:claim-end -->

<!-- design-md:claim application-priority order=prompt-fact,repository-fact,system-contract,reference-inspiration lang=en -->
### Application priority

1. Direct user instructions for the requested scope.
2. Repository facts.
3. This system contract.
4. Reference inspiration.
<!-- design-md:claim-end -->

<!-- design-md:claim unknowns policy=absent-at-smallest-unresolved-boundary lang=en -->
### Unknowns

Omit only the smallest unresolved value or group. Do not replace it with a plausible default.
<!-- design-md:claim-end -->

<!-- design-md:claim changes policy=review-record-validate-before-adoption lang=en -->
### Changes

Record, review, and validate changes before adoption.
<!-- design-md:claim-end -->
