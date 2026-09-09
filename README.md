# 롤리몰리

리그 오브 레전드 클랜의 회원, 일반 내전과 경매 내전을 관리하는 앱입니다.

회원 프로필과 Riot 정보 조회, 카카오톡 모집 명단 등록, 팀 편성, 경매 진행, 경기 결과와 우승 업적 관리를 지원합니다. 승인된 회원은 일반 내전과 경매 내전을 만들 수 있습니다.

## 실행

Python 3.11을 사용합니다. 처음 설치할 때 아래 명령을 실행하세요.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

설정 후 `run.bat`를 실행하거나 아래 명령으로 시작합니다.

```powershell
.\.venv\Scripts\python.exe -m roly.server
```

## 설정

DB 연결과 Riot API 키는 `.streamlit/secrets.toml`에 입력합니다. 항목은 [.streamlit/secrets.toml.example](.streamlit/secrets.toml.example)을 참고하세요. 실제 비밀번호와 API 키는 저장소에 올리지 않습니다.

Streamlit 배포 시작 파일은 `streamlit_app.py`입니다. 설치·관리자 설정·배포 절차는 [배포 안내](DEPLOYMENT.md)에 정리되어 있습니다.

## 문서

- [운영 흐름](CURRENT_FLOW.md)
- [구현·검증 현황과 남은 작업](docs/ROLYMOLY_IMPLEMENTATION_REPORT.md)
- [배포 안내](DEPLOYMENT.md)

검수 앱의 경매 응답성은 추가 확인 중입니다. 실제 운영 전에는 구현·검증 현황의 최신 결과를 확인하세요.
