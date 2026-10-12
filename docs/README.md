# docs 인덱스

이력서·자소서·면접에서 바로 꺼내 쓸 수 있도록 **기술 결정, 트러블슈팅, 성과**를 기록하는 폴더입니다.
Cursor 규칙(`.cursor/rules/youthfit-docs.mdc`)에 따라 Agent가 작업 후 자동으로 갱신하거나 제안합니다.

| 위치 | 용도 | 언제 쓰나 |
|------|------|-----------|
| [project-overview.md](project-overview.md) | 서비스 한 페이지 요약 | 목표/범위가 바뀔 때 |
| [architecture.md](architecture.md) | 시스템 구조·데이터 흐름 | 구조가 바뀔 때 |
| [decisions/](decisions/) | ADR: 왜 이 기술/방식을 골랐나 | 기술 선택, 설계 갈림길 |
| [journal/](journal/) | 트러블슈팅·실험·회고 (날짜별) | 버그 해결, 예상 밖 이슈, 측정 결과 |
| [milestones/](milestones/) | 주차별 산출물 체크리스트 | 주차 시작/종료 |
| [resume-bullets.md](resume-bullets.md) | 이력서·자소서용 STAR 문장 누적 | 의미 있는 성과/해결이 생길 때마다 |

## 작성 원칙

- 한국어로 작성한다.
- **수치와 근거**를 남긴다 (응답 시간, 호출 수, 데이터 건수, 토큰 비용 등). 없으면 "측정 전"이라고 적는다.
- 비밀값(API 키, 비밀번호)은 절대 기록하지 않는다.
- ADR 번호는 연속으로 증가시킨다 (`001`, `002`, ...). 번복할 때는 기존 ADR을 수정하지 않고 새 ADR로 `Superseded` 처리한다.
- journal 파일명은 `YYYY-MM-DD-주제.md`.
