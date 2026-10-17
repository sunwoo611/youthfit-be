# 2026-10-17 Alembic이 한글 주석이 든 alembic.ini를 읽지 못함

## 상황 / 증상

`alembic.ini`에 한글 주석을 넣은 뒤 `uv run alembic upgrade head --sql` 실행 시 실패.

```
UnicodeDecodeError: 'cp949' codec can't decode byte 0xec in position 3409: illegal multibyte sequence
```

## 원인 분석

- Alembic 1.20은 ini 파일을 `encoding="locale"`로 읽는다 (`alembic/util/compat.py`의 `read_config_parser`).
- 한국어 Windows 로캘은 cp949라서, UTF-8로 저장된 한글 주석을 디코딩하지 못한다.
- `.py` 파일(`env.py`, 마이그레이션)은 Python이 UTF-8로 읽으므로 한글 주석이 있어도 문제없다.

## 해결

- `alembic.ini` 주석을 영어(ASCII)로 바꾸고, "ASCII만 사용" 주의 문구를 같은 파일에 남겼다.
- DB URL은 ini에 두지 않고 `env.py`가 `Settings.database_url`(`.env`)에서 읽도록 했다.

## 결과 (수치)

- 수정 후 `alembic upgrade head --sql` 정상 렌더링 (테이블 5개, 인덱스 6개).

## 배운 점 / 재발 방지

- Windows 팀원이 있는 저장소에서는 ini/cfg 같은 설정 파일을 ASCII로 유지한다. 설명이 필요하면 `.py`나 docs에 적는다.
- 대안으로 `PYTHONUTF8=1` 환경 변수도 있지만, 팀원마다 설정을 강제해야 해서 채택하지 않았다.

## 이력서 반영 여부

- [ ] `resume-bullets.md` 에 추가함 (사소한 환경 이슈라 미반영)
