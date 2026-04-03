# LLM 중계 프록시 서버

Vertex AI와 GitHub Copilot API 요청을 중계하고 모든 트래픽과 토큰 사용량을 모니터링하는 프록시 서버입니다.

## 기능

- **API 프록시**: Vertex AI 및 GitHub Copilot API 요청 중계
- **실시간 모니터링**: 웹 대시보드에서 트래픽 실시간 확인
- **토큰 사용량 추적**: 입력/출력 토큰 및 응답 시간 기록
- **모델별 통계**: 모델별 호출 횟수, 토큰 사용량, 평균 응답 시간
- **요청 기록**: 최대 500개까지 요청/응답 기록 저장

## 설치

### 1. 가상환경 생성 및 활성화

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Linux/Mac
python3 -m venv venv
source venv/bin/activate
```

### 2. 의존성 설치

```bash
pip install -r requirements.txt
```

### 3. API 키 설정

`key/` 폴더에 다음 파일들을 배치합니다:

- **Vertex AI**: 서비스 계정 JSON 키 파일 (예: `rp5project-xxx.json`)
- **Copilot**: `copilot.json` 파일에 키 저장
  ```json
  key: gho_xxxxx
  ```
  또는
  ```json
  {"key": "gho_xxxxx"}
  ```
- **ZAI**: `zai.key` 파일에 API 키 저장 (텍스트)
  ```
  bxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxx
  ```

## 실행

### 서버 실행

```bash
# Windows
run_server.bat

# 또는 직접 실행
venv\Scripts\activate
python proxy_server.py
```

서버가 시작되면:
- **대시보드**: http://localhost:8190

## 사용 가능한 서비스

### Vertex AI (Google)
```
http://localhost:8190/vertex/gemini-3.1-pro-preview
http://localhost:8190/vertex/gemini-3-flash-preview
http://localhost:8190/vertex/gemini-3.1-pro
http://localhost:8190/vertex/gemini-3-flash
http://localhost:8190/vertex/gemini-2.5-pro
http://localhost:8190/vertex/gemini-2.5-flash
```

### GitHub Copilot
```
http://localhost:8190/copilot/gpt-4.1
http://localhost:8190/copilot/gpt-41
http://localhost:8190/copilot/gemini-3.1-pro-preview
http://localhost:8190/copilot/gemini-3-flash-preview
http://localhost:8190/copilot/claude-opus-4.5
http://localhost:8190/copilot/claude-opus-4-6
```

### ZAI (GLM)
```
http://localhost:8190/zai/glm-5.1
```

### 연결 테스트

```bash
# Windows
run_test.bat

# 또는 직접 실행
venv\Scripts\activate
python test_connection.py
```

## API 엔드포인트

### Vertex AI

```
POST http://localhost:8190/vertex/gemini-3.1-pro-preview
POST http://localhost:8190/vertex/gemini-3-flash-preview
```

### GitHub Copilot

```
POST http://localhost:8190/copilot/gpt-4.1
POST http://localhost:8190/copilot/gemini-3.1-pro-preview
POST http://localhost:8190/copilot/gemini-3-flash-preview
POST http://localhost:8190/copilot/claude-opus-4.5
POST http://localhost:8190/copilot/claude-opus-4-6
```

### ZAI (GLM)

```
POST http://localhost:8190/zai/glm-5.1
```

### 모니터링 API

| 엔드포인트 | 설명 |
|-----------|------|
| `GET /` | 웹 대시보드 |
| `GET /api/records` | 요청 기록 조회 |
| `GET /api/stats` | 모델별 통계 조회 |
| `POST /api/stats/reset` | 통계 초기화 |
| `GET /api/pending` | 대기 중인 요청 조회 |
| `GET /health` | 서버 상태 확인 |

## 웹 대시보드 기능

1. **대기 중인 요청**: 실시간으로 처리 중인 요청 표시
2. **모델별 사용량 통계**: 호출 횟수, 토큰 사용량, 평균 응답 시간
3. **요청 기록**: 
   - 요청/응답 내용 (펼치기/접기)
   - 입력/출력 토큰 수
   - 응답 시간
   - 상태 코드
4. **통계 초기화**: 버튼 클릭으로 통계 초기화 (기록은 유지)

## 프로젝트 구조

```
LLM중계서버/
├── proxy_server.py      # 메인 프록시 서버
├── test_connection.py   # 연결 테스트 스크립트
├── requirements.txt     # Python 의존성
├── run_server.bat       # 서버 실행 스크립트 (Windows)
├── run_test.bat         # 테스트 실행 스크립트 (Windows)
├── .gitignore          # Git 제외 파일
├── README.md           # 이 파일
├── key/                # API 키 폴더
│   ├── rp5project-xxx.json  # Vertex AI 서비스 계정 키
│   ├── copilot.json         # Copilot API 키
│   └── zai.key              # ZAI API 키
└── venv/               # 가상환경 (설치 후 생성)
```

## 설정

`proxy_server.py` 상단에서 다음 설정을 변경할 수 있습니다:

```python
PORT = 8190            # 서버 포트
MAX_RECORDS = 500      # 최대 기록 수
```