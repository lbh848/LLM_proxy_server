# CLAUDE.md - LLM 중계 프록시 서버

## 프로젝트 개요
FastAPI 기반 LLM 프록시 서버. 여러 LLM 서비스(Vertex AI, GitHub Copilot, ZAI 등)의 API 요청을 중계하고, 트래픽/토큰 사용량을 모니터링하며 웹 대시보드를 제공.

## 프로젝트 구조
- `proxy_server.py` - 메인 서버 (모든 라우트, 비즈니스 로직)
- `static/index.html` - 대시보드 HTML
- `static/dashboard.js` - 대시보드 JS
- `static/style.css` - 대시보드 스타일
- `key/` - API 키 파일 (copilot.json, zai.key, vertex 서비스 계정 JSON)
- `data/` - 기록/통계/설정 JSON 파일
- `API_PRICE/` - 모델별 가격 정보
- `venv/` - Python 가상환경

## 새 서비스 추가 시 규칙

### 대시보드 표시 규칙 (필수)
새로운 LLM 서비스를 추가할 때, 대시보드에서 **JSON 요청 본문과 JSON 응답 본문**이 모두 보여야 한다.

1. **대기 요청(request_preview)**: `request_text`는 반드시 `json.dumps(request_body, ensure_ascii=False, indent=2)` 형식의 전체 JSON 문자열이어야 함. `extract_text_from_content()`로 텍스트만 추출하지 말 것.
2. **완료 기록(response)**: 응답도 JSON 형태로 기록에 저장되어야 함.
3. **input_tokens**: `add_pending()` 시 `input_tokens` 필드 포함 필수 (tiktoken 기반 측정).
4. **대시보드 JS**: `MODEL_NAME_MAP`에 `서비스명/모델명` 매핑 추가 필수.

### 재시도 설정 (필수)
서비스를 추가할 때 **반드시** 해당 서비스 전용 재시도 설정을 만들어야 한다. 서비스를 제거할 때는 관련 재시도 설정도 함께 제거해야 한다.

**추가 시 수정 파일:**
1. `proxy_server.py` - `DEFAULT_SETTINGS`에 `{서비스명}_retry_count`, `{서비스명}_retry_delay` 추가
2. `proxy_server.py` - `update_settings()`에 유효성 검사 로직 추가
3. `proxy_server.py` - 프록시 라우트에서 `settings_manager.settings["{서비스명}_retry_count/delay"]` 사용
4. `static/index.html` - 설정 모달에 해당 서비스 섹션 추가 (재시도 횟수/간격 입력)
5. `static/dashboard.js` - 설정 로드/저장 함수에 해당 필드 추가

**제거 시:** 위 5개 파일에서 관련 설정을 모두 삭제.

### 프록시 라우트 구조
각 서비스는 `/서비스명/{model_name}` 경로로 라우트를 만든다. 기존 서비스(vertex, copilot, zai)의 패턴을 따를 것:
- `add_pending()` → API 호출 → `remove_pending()` → `add_record()`
- 인터럽트(취소/재시도) 처리 포함
- 폴백 지원 (fallback)

### README.md 업데이트 (필수)
서비스를 추가하거나 제거할 때 **반드시** `README.md`의 "사용 가능한 서비스" 섹션과 "API 엔드포인트" 섹션을 업데이트해야 한다.

형식: `http://localhost:8190/{서비스명}/{모델명}` (복붙해서 바로 사용할 수 있게)

**추가 시:** 해당 서비스 블록과 모델 경로를 추가.
**제거 시:** 해당 서비스 블록 전체를 삭제.

### 기타
- httpx 타임아웃: `timeout=None` (무제한)
- 토큰 측정: `tiktoken` cl100k_base 인코딩 사용
- 서버 포트: 8190
