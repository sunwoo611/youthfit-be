# 기획 발표자료 요약

> 원본: `originals/presentation.pdf` (git 미포함, 16슬라이드).
> PDF에서 한글 텍스트가 대부분 추출되지 않아 **슬라이드 구성과 영문·숫자 키워드만 확인한 요약**이다. 세부 내용은 계획서 정리본([project-plan.md](project-plan.md))을 기준으로 한다. 정확한 슬라이드 문구가 필요하면 PPT 원본이나 텍스트를 받아 이 문서를 보강한다.

## 목차

1. 문제 정의·배경 — 수동 필터 UI/UX, 정책 vs 금융 파편화, LLM 환각
2. 해결 방안 — 자연어 1문장 → LLM 역질문 3~5개(칩 UI) → 정책+금융 통합 추천 + 공식 URL
3. 시스템 구성·역할·일정
4. 위험 요소·기대 효과

## 시스템 구성 (4계층)

1. Frontend & Backend — 자연어 입력, FastAPI
2. Data Layer — 공공 API → 로컬 DB, 후보군(정책 30~50, 적금 10~20), RDBMS/Vector DB
3. Active Querying Engine — LLM이 역질문 3~5개를 JSON으로 생성, 칩 UI
4. RAG Layer — 선정 근거 + 공식 URL

## 기술 스택 (슬라이드 기준)

| 영역 | 내용 |
|------|------|
| Frontend | Next.js, React, Tailwind CSS, 칩 UI 3~5단계 |
| Backend | FastAPI RESTful API, 공공 API → DB 캐싱 |
| AI/LLM | GPT-4o-mini 등, 자격 근거·URL 1:1 바인딩 RAG |
| Data | 온통청년 Open API, 금감원 API(예·적금), PostgreSQL |

## 설계 원칙

Frictionless UX / Efficiency(SQL 우선, LLM 최소화) / Trust & Actionable(DB URL만 노출)

## 일정 (4단계 묶음)

| 단계 | 주차 | 기간 | 핵심 |
|------|------|------|------|
| 1 | 1~3 | 9/22~10/12 | API 연동, 정규화·DB, 1차 필터, FE 기본 UI |
| 2 | 4~6 | 10/13~11/2 | 역질문 프롬프트, JSON↔칩 UI, 세션, RAG, 중간 E2E |
| 3 | 7~9 | 11/3~11/23 | 통합 카드, URL 바인딩, 캐싱, 비용 최적화, 통합 배포 |
| 4 | 10~12 | 11/24~12/14 | 알파 테스트 10~15명, 개선, 환각·링크 점검, 최종 배포 |

## 계획서와 다른 점

- 발표자료는 RAG를 "Vector DB" 중심으로 표현하지만, 구현은 PostgreSQL 메타데이터 필터 + 후보 레코드 컨텍스트 방식으로 한다 ([ADR 001](../decisions/001-stack-and-mvp-scope.md)). 발표·질의응답에서는 "필요 시 pgvector로 확장 가능"으로 설명한다.
