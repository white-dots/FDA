# 라이온켐텍 프로젝트: SharePoint/OneDrive 어댑터 검증 결과

**일자**: 2026-04-26
**테스터**: 정재혁 (Jae Heuk Jung)
**대상**: fda-system v1의 SharePoint/OneDrive 통합 (`outlook.py`)
**목적**: 라이온켐텍 데이터 수집 Agent의 기반으로 fda-system v1을 fork할지 결정하기 위한 검증

## 배경

라이온켐텍 Phase 1 제안서의 핵심 약속 중 하나:

> **데이터 수집 Agent**가 SharePoint 기반으로 "누가, 언제, 무엇을 수정했는지" 자동 이력 관리

이 약속이 fda-system v1의 기존 코드로 가능한지 실제 M365 테넌트 (개인 계정 + 친구 회사 sofsys 테넌트)에서 검증.

## 검증 시나리오 (SP-1 ~ SP-6)

테스트 스크립트: `scripts/sharepoint_smoke_test.py`

| 시나리오 | 검증 내용 | 결과 |
|---|---|---|
| **SP-1** | MSAL device code 인증 + 토큰 캐시 | ✅ 통과 |
| **SP-2** | OneDrive 메타데이터 조회 (`/me/drive`) | ✅ 통과 |
| **SP-3** | OneDrive 루트 파일 리스트 | ✅ 통과 (7개 항목) |
| **SP-4** | 파일 검색 (`/me/drive/root/search`) | ✅ 통과 |
| **SP-5** | 파일 다운로드 + 텍스트 추출 (.docx) | ✅ 통과 ("Sales special" 추출 확인) |
| **SP-6** | **버전 이력 조회** (`/items/{id}/versions`) | ✅ **통과 — 핵심 약속 검증 완료** |

### SP-6 상세 — "누가/언제/무엇 수정" 약속 증명

```
✓ Found 2 version(s):
  v2.0  2026-04-26T09:24:19Z  by contact@datacore.digital  (11774 bytes)
  v1.0  2026-04-26T09:24:10Z  by contact@datacore.digital  (11700 bytes)
```

Microsoft Graph `/drives/{id}/items/{id}/versions` 엔드포인트가 각 버전마다 다음 정보 반환:
- 수정 일시 (`lastModifiedDateTime`)
- 수정자 displayName (`lastModifiedBy.user.displayName`)
- 파일 크기 (변경 추적 가능)
- 버전 ID

→ 라이온켐텍 production에서 동일하게 작동 예상 (work/school 계정에선 더 풍부한 정보 가능)

## 발견된 v1 버그 (fork에서 fix 필요)

검증 과정에서 v1 outlook.py에서 4가지 버그/누락 발견:

### 버그 1: `_make_request`의 URL 구성 오류
**위치**: `outlook.py:462`
```python
url = urljoin(self.GRAPH_API_BASE, endpoint.lstrip("/"))
```
**문제**: `GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"` (끝에 `/` 없음). Python의 `urljoin`은 base URL 끝에 `/`가 없으면 마지막 path segment를 endpoint로 대체 → `/v1.0` strip됨 → 모든 호출이 404.
**증상**: `me/drive`, `search/query` 등 엔드포인트 전부 `Invalid version: me` 에러
**도입 시점**: SharePoint 통합 commit `3ea6112` (regression bug)
**Fix**: `GRAPH_API_BASE` 끝에 `/` 추가 또는 `urljoin` 대신 직접 문자열 결합

### 버그 2: `get_file_content`의 double slash
**위치**: `outlook.py:741`
```python
url = f"{self.GRAPH_API_BASE}{endpoint}"
```
**문제**: 위 버그를 fix하기 위해 `GRAPH_API_BASE`에 `/`를 추가하면, endpoint가 `/`로 시작하는 이 코드 경로가 `//` 생성 → 404.
**Fix**: 두 코드 경로를 일관된 방식으로 통합 (예: `_make_request`에서 모든 URL 구성 처리)

### 버그 3: `ORG_SCOPES` defined but never used
**위치**: `outlook.py:39-42`
```python
ORG_SCOPES = [
    "Sites.Read.All",
    "Files.Read.All",
]
```
**문제**: 정의만 되어 있고 어디서도 `SCOPES`에 합쳐지지 않음 → SharePoint 검색 호출 시 권한 부족.
**Fix**: 인증 시 organizational account면 `SCOPES + ORG_SCOPES`를 요청하도록 분기

### 누락 4: 버전 이력 메서드 부재
**문제**: `/drives/{id}/items/{id}/versions` 엔드포인트 호출 메서드가 outlook.py에 없음. 우리 핵심 약속의 기반인데 v1엔 미구현.
**Fix**: `get_file_versions(item_id, drive_id=None)` 메서드 약 30 LOC 추가

### Microsoft 제약사항 (v1 버그 아님 — 참고용)
**Microsoft Search API (`/search/query`) 가 MSA(개인) 계정 미지원**
- 응답: `"This API is not supported for MSA accounts"`
- 우회: `/me/drive/root/search(q='...')` 사용 (MSA + work/school 모두 지원)
- 라이온켐텍은 work/school 계정이므로 production 영향 없음
- 단, 개발 시 personal 계정으로 테스트하는 경우 우회 코드 필요

## fork 결정 — Go

검증 결과 종합:
- **핵심 약속(누가/언제/무엇 수정) 작동** → Go decision
- v1의 LibrarianAgent + file_indexer + journal + msal 인증 기반 그대로 활용 가능
- 발견된 4개 버그는 모두 small fix (각 5~30 LOC)
- 라이온켐텍 production에선 work/school 계정 + admin consent로 더 깔끔하게 작동 예상

## 다음 단계

이 브랜치 (`feat/for_lionchemtech`)에서 진행할 작업:

1. **버그 fix 4개** — 위 발견된 버그를 outlook.py에 직접 패치 (현재 스크립트는 monkey-patch로만 검증)
2. **`get_file_versions()` 메서드 추가** — 약 30 LOC, 우리 핵심 약속의 기반
3. **`fda/data/sharepoint_adapter.py`** 신규 작성 — outlook.py에서 SharePoint 관련 코드 분리, 데이터 수집 전용 어댑터로 정리
4. **`LibrarianAgent` → `DataCollectionAgent` rename**
5. **테스트 추가** — SharePoint 어댑터 단위 테스트 (mock 기반)
6. **upstream PR** — 버그 fix들은 fda-system upstream(`origin/v1`)에도 PR 제출 (라이온켐텍과 무관하게 가치 있음)

## 참고 — 테스트 환경

- **Python**: 3.11.9
- **테스트 계정**: `contact@datacore.digital` (개인 MS 계정, OneDrive Personal)
- **Microsoft Graph PowerShell client** (multi-tenant default, `14d82eec-204b-4c2f-b7e8-296a70dab67e`)
- **요청 스코프**: `User.Read`, `Files.Read`, `Calendars.Read` (모두 user-consentable)

## 재현 방법

```bash
cd ~/Documents/fda-system
python3.11 -m venv .venv-test
source .venv-test/bin/activate
pip install -e ".[all]"
pip install fastembed pypdf python-docx

# Optional: 본인 등록한 Azure AD 앱 사용 시
# export FDA_OUTLOOK_CLIENT_ID=<your-client-id>
# export FDA_OUTLOOK_TENANT_ID=<your-tenant-id>

python3 scripts/sharepoint_smoke_test.py
```

OneDrive 루트에 .docx 파일 1개 이상 있어야 SP-5/SP-6 검증 가능 (한 번 이상 수정해서 버전 2개 만들어두면 더 명확).
