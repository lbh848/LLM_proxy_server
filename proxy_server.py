"""
LLM 중계 프록시 서버
- Vertex AI 및 GitHub Copilot API 요청을 중계
- 트래픽 및 토큰 사용량 모니터링
- 웹 대시보드 제공
"""

import json
import time
import asyncio
import hashlib
import logging
import jwt
from datetime import datetime
from collections import deque
from typing import Optional, Dict, Any, List
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from google.oauth2 import service_account
import vertexai
from vertexai.generative_models import GenerativeModel
import tiktoken

# 로깅 설정 - 특정 경로 로그 숨기기
class SkipPathsFilter(logging.Filter):
    """특정 경로의 로그를 건너뛰는 필터"""
    SKIP_PATHS = ["/api/pending", "/api/records", "/api/stats"]
    
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for path in self.SKIP_PATHS:
            if path in message:
                return False
        return True

# uvicorn액세스 로그에 필터 적용
uvicorn_logger = logging.getLogger("uvicorn.access")
uvicorn_logger.addFilter(SkipPathsFilter())

# 설정
PORT = 8190
MAX_RECORDS = 500
KEY_DIR = Path(__file__).parent / "key"
STATIC_DIR = Path(__file__).parent / "static"
RECORDS_FILE = Path(__file__).parent / "data" / "records.json"
STATS_FILE = Path(__file__).parent / "data" / "stats.json"
SETTINGS_FILE = Path(__file__).parent / "data" / "settings.json"
INPUT_LOGS_FILE = Path(__file__).parent / "data" / "input_logs.json"

# FastAPI 앱 초기화
app = FastAPI(title="LLM Proxy Server")

# 정적 파일 서빙
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 전역 상태
class TrafficStore:
    def __init__(self, max_records: int = 500):
        self.records: deque = deque(maxlen=max_records)
        self.model_stats: Dict[str, Dict[str, Any]] = {}
        self.pending_requests: Dict[str, Dict[str, Any]] = {}
        self.interrupt_events: Dict[str, asyncio.Event] = {}
        self.interrupt_actions: Dict[str, str] = {}
        self.cancelled_requests: set = set()  # 취소된 요청 ID 추적
        self._load_from_files()
    
    def _load_from_files(self):
        """파일에서 기록과 통계 로드"""
        # 기록 로드
        if RECORDS_FILE.exists():
            try:
                with open(RECORDS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 파일에는 [최신, ..., 오래된] 순서로 저장되어 있음
                    # 그대로 추가하면 최신이앞에 옴
                    for record in reversed(data):  # 오래된 것부터 추가
                        self.records.appendleft(record)
                print(f"[LOAD] {len(self.records)}개의 기록을 로드했습니다.")
            except Exception as e:
                print(f"[ERROR] 기록 로드 실패: {e}")
        
        # 통계 로드
        if STATS_FILE.exists():
            try:
                with open(STATS_FILE, 'r', encoding='utf-8') as f:
                    self.model_stats = json.load(f)
                print(f"[LOAD] 통계를 로드했습니다.")
            except Exception as e:
                print(f"[ERROR] 통계 로드 실패: {e}")
    
    def _save_records(self):
        """기록을 파일에 저장"""
        try:
            RECORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(RECORDS_FILE, 'w', encoding='utf-8') as f:
                json.dump(list(self.records), f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[ERROR] 기록 저장 실패: {e}")
    
    def _save_stats(self):
        """통계를 파일에 저장"""
        try:
            STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(STATS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.model_stats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[ERROR] 통계 저장 실패: {e}")
    
    def add_record(self, record: Dict[str, Any]):
        record["id"] = hashlib.md5(f"{time.time()}{json.dumps(record)}".encode()).hexdigest()[:8]
        record["timestamp"] = datetime.now().isoformat()
        self.records.appendleft(record)
        self._save_records()
    
    def update_stats(self, model: str, input_tokens: int, output_tokens: int, latency: float):
        if model not in self.model_stats:
            self.model_stats[model] = {
                "total_calls": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_latency": 0.0
            }
        self.model_stats[model]["total_calls"] += 1
        self.model_stats[model]["total_input_tokens"] += input_tokens
        self.model_stats[model]["total_output_tokens"] += output_tokens
        self.model_stats[model]["total_latency"] += latency
        self._save_stats()
    
    def reset_stats(self):
        self.model_stats = {}
        self._save_stats()
    
    def get_records(self) -> List[Dict[str, Any]]:
        return list(self.records)
    
    def get_stats(self) -> Dict[str, Dict[str, Any]]:
        return self.model_stats
    
    def add_pending(self, request_id: str, data: Dict[str, Any]):
        self.pending_requests[request_id] = data
        self.interrupt_events[request_id] = asyncio.Event()
        self.interrupt_actions[request_id] = ""
    
    def remove_pending(self, request_id: str):
        if request_id in self.pending_requests:
            del self.pending_requests[request_id]
        if request_id in self.interrupt_events:
            del self.interrupt_events[request_id]
        if request_id in self.interrupt_actions:
            del self.interrupt_actions[request_id]
            
    def trigger_interrupt(self, request_id: str, action: str):
        if request_id in self.interrupt_events:
            self.interrupt_actions[request_id] = action
            self.interrupt_events[request_id].set()
    
    def get_pending(self) -> Dict[str, Dict[str, Any]]:
        return self.pending_requests
    
    def get_pending_by_id(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self.pending_requests.get(request_id)
    
    def mark_cancelled(self, request_id: str):
        """요청을 취소됨으로 표시"""
        self.cancelled_requests.add(request_id)
    
    def is_cancelled(self, request_id: str) -> bool:
        """요청이 취소되었는지 확인"""
        return request_id in self.cancelled_requests
    
    def clear_cancelled(self, request_id: str):
        """취소 표시 제거"""
        self.cancelled_requests.discard(request_id)

store = TrafficStore(MAX_RECORDS)

# 사용자 입력 로그 관리 (최근 20개만 유지)
class InputLogger:
    """사용자 입력 활동 로그 - 버그 분석용"""
    MAX_LOGS = 20
    
    def __init__(self, log_file: Path):
        self.log_file = log_file
        self.logs: deque = deque(maxlen=self.MAX_LOGS)
        self._load_logs()
    
    def _load_logs(self):
        """로그 파일에서 로드"""
        if self.log_file.exists():
            try:
                with open(self.log_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for log in reversed(data):  # 오래된 것부터 추가
                        self.logs.appendleft(log)
                print(f"[LOAD] {len(self.logs)}개의 입력 로그를 로드했습니다.")
            except Exception as e:
                print(f"[ERROR] 입력 로그 로드 실패: {e}")
    
    def _save_logs(self):
        """로그 파일에 저장"""
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, 'w', encoding='utf-8') as f:
                json.dump(list(self.logs), f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[ERROR] 입력 로그 저장 실패: {e}")
    
    def log_input(self, log_type: str, endpoint: str, data: Dict[str, Any]):
        """사용자 입력 로그 기록"""
        log_entry = {
            "id": hashlib.md5(f"{time.time()}{log_type}{endpoint}".encode()).hexdigest()[:8],
            "timestamp": datetime.now().isoformat(),
            "type": log_type,
            "endpoint": endpoint,
            "data": data
        }
        self.logs.appendleft(log_entry)
        self._save_logs()
        print(f"[INPUT LOG] {log_type} - {endpoint}")
    
    def get_logs(self) -> List[Dict[str, Any]]:
        """모든 로그 반환"""
        return list(self.logs)
    
    def clear_logs(self):
        """로그 초기화"""
        self.logs.clear()
        self._save_logs()

input_logger = InputLogger(INPUT_LOGS_FILE)

# 설정 관리
class SettingsManager:
    """재시도 설정 관리 - Copilot과 Vertex 각각 별도 설정"""
    DEFAULT_SETTINGS = {
        # Copilot 재시도 설정
        "retry_count": 3,  # 기본 재시도 횟수
        "retry_delay": 1,  # 기본 재시도 간격 (초)
        # Vertex 재시도 설정 (429 Resource exhausted 오류 시)
        "vertex_retry_count": 3,  # Vertex 재시도 횟수
        "vertex_retry_delay": 10,  # Vertex 재시도 간격 (초, 0~600초 =0~10분)
        # ZAI 재시도 설정
        "zai_retry_count": 3,  # ZAI 재시도 횟수
        "zai_retry_delay": 10,  # ZAI 재시도 간격 (초, 0~600초 =0~10분)
        # ZAI thinking 설정
        "zai_thinking": "disabled",  # disabled, enabled
        "zai_thinking_budget": 8000,  # thinking 예산 (enabled일 때 사용, 최소 1024)
        # 검열 차단 시 폴백 모델 설정
        "fallback_model": "",  # 예: "copilot/gpt-4.1", "vertex/gemini-2.5-pro" (빈 값=비활성화)
        # 코파일럿 승수 조회 설정
        "copilot_quota_enabled": False,  # 코파일럿 남은 사용량 자동 조회
        # ZAI 승수 조회 설정
        "zai_quota_enabled": False,  # ZAI 남은 사용량 자동 조회
        # Tavily 승수 조회 설정
        "tavily_quota_enabled": False,  # Tavily 남은 사용량 자동 조회
    }
    
    def __init__(self, settings_file: Path):
        self.settings_file = settings_file
        self.settings: Dict[str, Any] = self.DEFAULT_SETTINGS.copy()
        self._load_settings()
    
    def _load_settings(self):
        """설정 파일에서 로드"""
        if self.settings_file.exists():
            try:
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    self.settings.update(loaded)
                print(f"[LOAD] 설정을 로드했습니다: {self.settings}")
            except Exception as e:
                print(f"[ERROR] 설정 로드 실패: {e}")
    
    def _save_settings(self):
        """설정 파일에 저장"""
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[ERROR] 설정 저장 실패: {e}")
    
    def get_settings(self) -> Dict[str, Any]:
        """현재 설정 반환"""
        return self.settings.copy()
    
    def update_settings(self, new_settings: Dict[str, Any]) -> Dict[str, Any]:
        """설정 업데이트"""
        # Copilot 재시도 설정 유효성 검사
        retry_count = new_settings.get("retry_count", self.settings["retry_count"])
        retry_delay = new_settings.get("retry_delay", self.settings["retry_delay"])
        
        # Copilot 재시도 횟수는 0-10 사이
        self.settings["retry_count"] = max(0, min(10, int(retry_count)))
        # Copilot 재시도 간격은 0-60 사이
        self.settings["retry_delay"] = max(0, min(60, int(retry_delay)))
        
        # Vertex 재시도 설정 유효성 검사
        vertex_retry_count = new_settings.get("vertex_retry_count", self.settings["vertex_retry_count"])
        vertex_retry_delay = new_settings.get("vertex_retry_delay", self.settings["vertex_retry_delay"])
        
        # Vertex 재시도 횟수는 0-10 사이
        self.settings["vertex_retry_count"] = max(0, min(10, int(vertex_retry_count)))
        # Vertex 재시도 간격은 0-600 사이 (0~10분)
        self.settings["vertex_retry_delay"] = max(0, min(600, int(vertex_retry_delay)))

        # ZAI 재시도 설정 유효성 검사
        zai_retry_count = new_settings.get("zai_retry_count", self.settings["zai_retry_count"])
        zai_retry_delay = new_settings.get("zai_retry_delay", self.settings["zai_retry_delay"])

        # ZAI 재시도 횟수는 0-10 사이
        self.settings["zai_retry_count"] = max(0, min(10, int(zai_retry_count)))
        # ZAI 재시도 간격은 0-600 사이 (0~10분)
        self.settings["zai_retry_delay"] = max(0, min(600, int(zai_retry_delay)))

        # ZAI thinking 설정
        zai_thinking = new_settings.get("zai_thinking", self.settings.get("zai_thinking", "disabled"))
        if zai_thinking not in ["disabled", "enabled"]:
            zai_thinking = "disabled"
        self.settings["zai_thinking"] = zai_thinking

        zai_thinking_budget = new_settings.get("zai_thinking_budget", self.settings.get("zai_thinking_budget", 8000))
        self.settings["zai_thinking_budget"] = max(1024, min(100000, int(zai_thinking_budget)))

        # 폴백 모델 설정 (문자열)
        fallback_model = new_settings.get("fallback_model", self.settings.get("fallback_model", ""))
        self.settings["fallback_model"] = str(fallback_model).strip()

        # 코파일럿 승수 조회 설정
        copilot_quota_enabled = new_settings.get("copilot_quota_enabled", self.settings.get("copilot_quota_enabled", False))
        self.settings["copilot_quota_enabled"] = copilot_quota_enabled in [True, "true", "True", 1]

        # ZAI 승수 조회 설정
        zai_quota_enabled = new_settings.get("zai_quota_enabled", self.settings.get("zai_quota_enabled", False))
        self.settings["zai_quota_enabled"] = zai_quota_enabled in [True, "true", "True", 1]

        # Tavily 승수 조회 설정
        tavily_quota_enabled = new_settings.get("tavily_quota_enabled", self.settings.get("tavily_quota_enabled", False))
        self.settings["tavily_quota_enabled"] = tavily_quota_enabled in [True, "true", "True", 1]

        self._save_settings()
        return self.settings.copy()

settings_manager = SettingsManager(SETTINGS_FILE)

# Vertex AI 인증 관리
class VertexAuthManager:
    def __init__(self, key_dir: Path):
        self.key_dir = key_dir
        self.credentials: Dict[str, Any] = {}
        self._load_credentials()
    
    def _load_credentials(self):
        """키 폴더에서 서비스 계정 키 파일들을 로드"""
        for key_file in self.key_dir.glob("*.json"):
            if "copilot" not in key_file.name:
                try:
                    with open(key_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        project_id = data.get("project_id", "unknown")
                        self.credentials[project_id] = {
                            "file_path": key_file,
                            "data": data,
                        }
                        # Vertex AI 초기화
                        credentials = service_account.Credentials.from_service_account_file(
                            str(key_file)
                        )
                        vertexai.init(project=project_id, location="global", credentials=credentials)
                        print(f"Vertex AI 초기화 완료: {project_id}")
                except Exception as e:
                    print(f"키 파일 로드 실패 {key_file}: {e}")
    
    def get_project_ids(self) -> List[str]:
        """사용 가능한 프로젝트 ID 목록 반환"""
        return list(self.credentials.keys())

vertex_auth = VertexAuthManager(KEY_DIR)

# Copilot 키 로드
def load_copilot_key() -> Optional[str]:
    """Copilot 키 로드"""
    copilot_file = KEY_DIR / "copilot.json"
    if copilot_file.exists():
        try:
            with open(copilot_file, 'r') as f:
                content = f.read().strip()
                if content.startswith("{"):
                    data = json.loads(content)
                    return data.get("key", "")
                else:
                    # "key: xxx" 형식
                    if ":" in content:
                        return content.split(":", 1)[1].strip()
                    return content
        except Exception as e:
            print(f"Copilot 키 로드 실패: {e}")
    return None

COPILOT_KEY = load_copilot_key()

# ZAI 키 로드
def load_zai_key() -> Optional[str]:
    """ZAI API 키 로드"""
    zai_file = KEY_DIR / "zai.key"
    if zai_file.exists():
        try:
            with open(zai_file, 'r') as f:
                content = f.read().strip()
                if content.startswith("{"):
                    data = json.loads(content)
                    return data.get("key", "")
                else:
                    if ":" in content:
                        return content.split(":", 1)[1].strip()
                    return content
        except Exception as e:
            print(f"ZAI 키 로드 실패: {e}")
    return None

ZAI_KEY = load_zai_key()

# Tavily 키 로드 (여러 키 지원, 파일 변경 시 자동 갱신)
_tavily_keys: List[str] = []
_tavily_mtime: float = 0.0

def load_tavily_keys() -> List[str]:
    """Tavily API 키 목록 로드 (줄별로 하나씩)"""
    tavily_file = KEY_DIR / "tavily.key"
    keys = []
    if tavily_file.exists():
        try:
            with open(tavily_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        keys.append(line)
            print(f"[LOAD] {len(keys)}개의 Tavily 키를 로드했습니다.")
        except Exception as e:
            print(f"[ERROR] Tavily 키 로드 실패: {e}")
    return keys

def get_tavily_keys() -> List[str]:
    """Tavily 키 반환 (파일이 변경되면 자동 리로드)"""
    global _tavily_keys, _tavily_mtime
    tavily_file = KEY_DIR / "tavily.key"
    try:
        mtime = tavily_file.stat().st_mtime if tavily_file.exists() else 0.0
        if mtime != _tavily_mtime:
            _tavily_mtime = mtime
            _tavily_keys = load_tavily_keys()
    except Exception:
        pass
    return _tavily_keys

TAVILY_KEYS_INITIAL = load_tavily_keys()
_tavily_mtime = (KEY_DIR / "tavily.key").stat().st_mtime if (KEY_DIR / "tavily.key").exists() else 0.0
_tavily_keys = TAVILY_KEYS_INITIAL

def estimate_tokens(text: str) -> int:
    """tiktoken을 사용한 정확한 토큰 수 측정"""
    if not text:
        return 0
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # 폴백: 간단한 추정
        korean_chars = sum(1 for c in text if '가' <= c <= '힣')
        other_chars = len(text) - korean_chars
        return (other_chars // 4) + (korean_chars // 2) + 1

def extract_text_from_content(content: Any) -> str:
    """요청/응답 내용에서 텍스트 추출"""
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict):
                if "text" in item:
                    texts.append(item["text"])
                elif "content" in item:
                    texts.append(extract_text_from_content(item["content"]))
                elif "parts" in item:
                    texts.append(extract_text_from_content(item["parts"]))
            elif isinstance(item, str):
                texts.append(item)
        return " ".join(texts)
    elif isinstance(content, dict):
        if "text" in content:
            return content["text"]
        elif "content" in content:
            return extract_text_from_content(content["content"])
        elif "parts" in content:
            return extract_text_from_content(content["parts"])
        elif "choices" in content:
            return extract_text_from_content(content["choices"])
    return str(content)

def is_vertex_429_error(error_msg: str) -> bool:
    """Vertex AI 429 Resource exhausted 오류인지 확인"""
    error_lower = error_msg.lower()
    return "429" in error_msg and "resource" in error_lower and "exhausted" in error_lower

def is_content_blocked_error(error_msg: str) -> bool:
    """에러 메시지에서 검열 차단 여부 확인"""
    keywords = [
        "prohibited_content",
        "blocked by the safety filters",
        "blocked due to prohibited",
        "cannot get the response text",
        "cannot get the candidate text",
        "content has no parts",
        "block_reason_safety"
    ]
    error_lower = error_msg.lower()
    return any(kw in error_lower for kw in keywords)

def is_vertex_response_blocked(response) -> bool:
    """Vertex AI 응답 객체에서 검열 차단 여부 확인 (candidates의 finish_reason 체크)"""
    try:
        if hasattr(response, 'candidates') and response.candidates:
            for candidate in response.candidates:
                finish_reason = str(getattr(candidate, 'finish_reason', '')).upper()
                if 'PROHIBITED' in finish_reason or 'SAFETY' in finish_reason or 'BLOCK' in finish_reason:
                    return True
    except:
        pass
    return False

def is_copilot_response_blocked(response_json: dict) -> bool:
    """Copilot 응답 JSON에서 검열 차단 여부 확인"""
    try:
        choices = response_json.get("choices", [])
        for choice in choices:
            finish_reason = str(choice.get("finish_reason", "")).upper()
            if 'PROHIBITED' in finish_reason or 'SAFETY' in finish_reason:
                return True
    except:
        pass
    return False

async def execute_fallback(request_body: dict, request_text: str, original_type: str):
    """검열 차단 시 폴백 모델로 요청 전송

    Args:
        request_body: 원본 요청 본문 (JSON)
        request_text: 추출된 요청 텍스트 (Vertex용)
        original_type: 원본 요청 타입 ("vertex" or "copilot")

    Returns:
        폴백 응답 JSON 또는 None (폴백 불가 시)
    """
    fallback_model = settings_manager.settings.get("fallback_model", "")
    if not fallback_model:
        return None

    parts = fallback_model.strip().split("/", 1)
    if len(parts) != 2:
        print(f"[FALLBACK] 잘못된 폴백 모델 형식: {fallback_model} (예: copilot/gpt-4.1, vertex/gemini-2.5-pro)")
        return None

    fallback_type, fallback_model_name = parts
    print(f"[FALLBACK] 검열 차단 감지 - 폴백 모델로 전송: {fallback_model}")

    try:
        if fallback_type == "vertex":
            # Vertex AI로 폴백 요청
            text = request_text
            if not text and request_body:
                if "messages" in request_body:
                    text = extract_text_from_content(request_body["messages"])
                elif "contents" in request_body:
                    text = extract_text_from_content(request_body["contents"])
                elif "prompt" in request_body:
                    text = request_body["prompt"]

            if not text:
                print("[FALLBACK] 요청 텍스트가 없어 폴백 불가")
                return None

            model = GenerativeModel(fallback_model_name)
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: model.generate_content(text)
            )

            if is_vertex_response_blocked(response):
                print(f"[FALLBACK] 폴백 모델에서도 검열 차단: {fallback_model}")
                return None

            response_text = response.text if hasattr(response, 'text') else str(response)
            output_tokens = estimate_tokens(response_text)
            input_tokens_fb = estimate_tokens(text)

            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                input_tokens_fb = getattr(response.usage_metadata, 'prompt_token_count', input_tokens_fb)
                output_tokens = getattr(response.usage_metadata, 'candidates_token_count', output_tokens)

            return {
                "choices": [{
                    "message": {"role": "assistant", "content": response_text},
                    "finish_reason": "stop",
                    "index": 0
                }],
                "usage": {
                    "prompt_tokens": input_tokens_fb,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens_fb + output_tokens
                },
                "model": fallback_model_name,
                "fallback": True,
                "fallback_from": original_type
            }

        elif fallback_type == "copilot":
            # Copilot으로 폴백 요청
            if not COPILOT_KEY:
                print("[FALLBACK] Copilot 키가 없어 폴백 불가")
                return None

            supported = ["gpt-4.1", "gpt-41", "gemini-3.1-pro-preview", "gemini-3-flash-preview"]
            if fallback_model_name not in supported:
                print(f"[FALLBACK] 지원하지 않는 Copilot 모델: {fallback_model_name} (지원: {', '.join(supported)})")
                return None

            fallback_body = request_body.copy() if request_body else {}
            fallback_body["model"] = fallback_model_name

            # messages가 없으면 텍스트에서 생성
            if "messages" not in fallback_body and request_text:
                fallback_body["messages"] = [{"role": "user", "content": request_text}]

            headers = {
                "Authorization": f"Bearer {COPILOT_KEY}",
                "Content-Type": "application/json",
                "Editor-Version": "vscode/1.92.0",
                "Editor-Plugin-Version": "copilot/1.220.0",
                "User-Agent": "GithubCopilot/1.220.0",
            }

            async with httpx.AsyncClient(timeout=None) as client:
                resp = await client.post(
                    "https://api.githubcopilot.com/chat/completions",
                    content=json.dumps(fallback_body).encode(),
                    headers=headers
                )

            if resp.status_code != 200:
                print(f"[FALLBACK] Copilot 폴백 실패: HTTP {resp.status_code}")
                return None

            resp_json = json.loads(resp.content)

            if is_copilot_response_blocked(resp_json):
                print(f"[FALLBACK] 폴백 모델에서도 검열 차단: {fallback_model}")
                return None

            resp_json["fallback"] = True
            resp_json["fallback_from"] = original_type
            return resp_json

        elif fallback_type == "zai":
            # ZAI로 폴백 요청
            if not ZAI_KEY:
                print("[FALLBACK] ZAI 키가 없어 폴백 불가")
                return None

            supported = ["glm-5.1"]
            if fallback_model_name not in supported:
                print(f"[FALLBACK] 지원하지 않는 ZAI 모델: {fallback_model_name} (지원: {', '.join(supported)})")
                return None

            # Anthropic Messages API 형식으로 변환
            fallback_body = {
                "model": fallback_model_name,
                "max_tokens": 8192,
                "messages": []
            }

            # 원본에서 messages 추출
            if "messages" in request_body:
                fallback_body["messages"] = request_body["messages"]
            elif "contents" in request_body:
                # Vertex 형식을 Anthropic messages로 변환
                for content in request_body["contents"]:
                    role = "user" if content.get("role", "user") == "user" else "assistant"
                    text = extract_text_from_content(content.get("parts", ""))
                    if text:
                        fallback_body["messages"].append({"role": role, "content": text})
            elif request_text:
                fallback_body["messages"] = [{"role": "user", "content": request_text}]

            if not fallback_body["messages"]:
                print("[FALLBACK] 변환된 메시지가 없어 ZAI 폴백 불가")
                return None

            headers = {
                "x-api-key": ZAI_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }

            async with httpx.AsyncClient(timeout=None) as client:
                resp = await client.post(
                    "https://api.z.ai/api/anthropic/v1/messages",
                    content=json.dumps(fallback_body).encode(),
                    headers=headers
                )

            if resp.status_code != 200:
                print(f"[FALLBACK] ZAI 폴백 실패: HTTP {resp.status_code}")
                return None

            resp_json = json.loads(resp.content)

            # Anthropic 응답을 OpenAI 형식으로 변환
            response_text = ""
            if "content" in resp_json:
                for block in resp_json["content"]:
                    if isinstance(block, dict) and block.get("type") == "text":
                        response_text += block.get("text", "")

            fb_input_tokens = resp_json.get("usage", {}).get("input_tokens", estimate_tokens(request_text))
            fb_output_tokens = resp_json.get("usage", {}).get("output_tokens", estimate_tokens(response_text))

            return {
                "choices": [{
                    "message": {"role": "assistant", "content": response_text},
                    "finish_reason": "end_turn" if resp_json.get("stop_reason") == "end_turn" else "stop",
                    "index": 0
                }],
                "usage": {
                    "prompt_tokens": fb_input_tokens,
                    "completion_tokens": fb_output_tokens,
                    "total_tokens": fb_input_tokens + fb_output_tokens
                },
                "model": fallback_model_name,
                "fallback": True,
                "fallback_from": original_type
            }

        else:
            print(f"[FALLBACK] 알 수 없는 폴백 타입: {fallback_type} (사용 가능: copilot, vertex, zai)")
            return None

    except Exception as e:
        print(f"[FALLBACK ERROR] {fallback_model}: {e}")
        return None


# Vertex AI 프록시 (vertexai SDK 사용)
@app.api_route("/vertex/{model_name:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_vertex(request: Request, model_name: str):
    """Vertex AI API 프록시 - vertexai SDK 사용, 429 오류 시 재시도"""
    start_time = time.time()
    request_id = hashlib.md5(f"{time.time()}{model_name}".encode()).hexdigest()[:8]
    
    # 경로에서 /v1/chat/completions 등의 접미사 제거
    clean_model_name = model_name.split("/")[0] if "/" in model_name else model_name
    
    # 요청 본문 읽기
    body = await request.body()
    try:
        request_body = json.loads(body) if body else {}
    except:
        request_body = {}
    
    # 사용자 입력 로그 기록
    input_logger.log_input(
        log_type="vertex_request",
        endpoint=f"/vertex/{model_name}",
        data={
            "model": clean_model_name,
            "request_body": request_body,
            "headers": dict(request.headers)
        }
    )
    
    # 실제 모델 이름 (매핑 없이 그대로 사용)
    actual_model = clean_model_name
    
    # 요청 텍스트 추출 (토큰 측정용)
    request_text_raw = ""
    if "contents" in request_body:
        request_text_raw = extract_text_from_content(request_body.get("contents", ""))
    elif "prompt" in request_body:
        request_text_raw = request_body.get("prompt", "")
    elif "text" in request_body:
        request_text_raw = request_body.get("text", "")
    elif "messages" in request_body:
        request_text_raw = extract_text_from_content(request_body.get("messages", ""))

    input_tokens = estimate_tokens(request_text_raw)

    # 대기 요청 등록 - request_preview는 전체 JSON
    request_text = json.dumps(request_body, ensure_ascii=False, indent=2)

    store.add_pending(request_id, {
        "model": clean_model_name,
        "type": "vertex",
        "start_time": start_time,
        "request_preview": request_text,
        "request_body": request_body,
        "input_tokens": input_tokens
    })
    
    # Vertex 재시도 설정 가져오기
    vertex_retry_count = settings_manager.settings["vertex_retry_count"]
    vertex_retry_delay = settings_manager.settings["vertex_retry_delay"]
    
    try:
        # Vertex AI SDK를 사용하여 요청 처리
        model = GenerativeModel(actual_model)
        
        last_error = None
        response = None
        
        # 재시도 루프
        attempt = 0
        while attempt <= vertex_retry_count:
            loop = asyncio.get_event_loop()
            
            # 실행기 태스크 생성 - run_in_executor는 Future를 반환하므로 task로 감싸기 위해 코루틴으로 래핑하거나 wrap_future 사용
            future = loop.run_in_executor(
                None,
                lambda: model.generate_content(request_text_raw)
            )
            llm_task = asyncio.wrap_future(future)
            
            # 인터럽트 대기 이벤트 연결
            interrupt_event = store.interrupt_events.get(request_id)
            if not interrupt_event:
                try:
                    response = await llm_task
                    break
                except Exception as e:
                    last_error = e
                    error_msg = str(e)
                    if is_vertex_429_error(error_msg) and attempt < vertex_retry_count:
                        print(f"[VERTEX RETRY] {clean_model_name} - 429 Resource exhausted, {attempt + 1}번째 재시도 (총 {vertex_retry_count}회, {vertex_retry_delay}초 대기)")
                        await asyncio.sleep(vertex_retry_delay)
                        attempt += 1
                        continue
                    else:
                        raise

            interrupt_task = asyncio.create_task(interrupt_event.wait())
            
            # 레이스 조건
            done, pending = await asyncio.wait(
                [llm_task, interrupt_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            if interrupt_task in done:
                # 수동 조작으로 인터럽트 발생
                action = store.interrupt_actions.get(request_id)
                interrupt_event.clear()
                
                if action == "cancel":
                    print(f"[VERTEX CANCELLED] {clean_model_name} - 사용자 수동 취소")
                    store.mark_cancelled(request_id)
                    store.remove_pending(request_id)
                    
                    cancel_response = {
                        "error": {
                            "message": "user canceled",
                            "code": "user_canceled",
                            "param": "model",
                            "type": "invalid_request_error"
                        }
                    }
                    
                    # 취소 기록 저장
                    store.add_record({
                        "model": clean_model_name,
                        "type": "vertex",
                        "request": json.dumps(request_body, ensure_ascii=False, indent=2),
                        "response": json.dumps(cancel_response, ensure_ascii=False, indent=2),
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "latency": round(time.time() - start_time, 2),
                        "status": 499,
                        "retry_count": attempt
                    })
                    
                    return JSONResponse(
                        content=cancel_response,
                        status_code=499
                    )
                elif action == "retry":
                    print(f"[VERTEX RETRY MANUAL] {clean_model_name} - 사용자 수동 재시도: 기존 연결 재사용, 새 요청 시작")
                    attempt = 0
                    continue
            else:
                # 정상 응답 도착 (또는 에러 발생 시 처리)
                interrupt_task.cancel()
                try:
                    response = llm_task.result()
                    break
                except Exception as e:
                    last_error = e
                    error_msg = str(e)
                    if is_vertex_429_error(error_msg) and attempt < vertex_retry_count:
                        print(f"[VERTEX RETRY] {clean_model_name} - 429 Resource exhausted, {attempt + 1}번째 재시도 (총 {vertex_retry_count}회, {vertex_retry_delay}초 대기)")
                        await asyncio.sleep(vertex_retry_delay)
                        attempt += 1
                        continue
                    else:
                        raise
        
        end_time = time.time()
        latency = end_time - start_time
        
        # 대기 요청 제거
        store.remove_pending(request_id)
        
        # 취소된 요청인지 확인 - 취소된 경우 기록 저장하지 않고 응답만 반환
        if store.is_cancelled(request_id):
            print(f"[VERTEX CANCELLED] {clean_model_name} - 요청이 취소됨, 기록 저장 생략")
            store.clear_cancelled(request_id)
            return JSONResponse(
                content={
                    "error": {
                        "message": "user canceled",
                        "code": "user_canceled",
                        "param": "model",
                        "type": "invalid_request_error"
                    }
                },
                status_code=499
            )
        else:
            # 검열 차단 여부 확인 (candidates finish_reason 체크)
            if is_vertex_response_blocked(response):
                print(f"[VERTEX BLOCKED] {clean_model_name} - 검열로 인해 응답 차단됨")
                store.remove_pending(request_id)

                # 폴백 시도
                fallback_result = await execute_fallback(request_body, request_text_raw, "vertex")
                if fallback_result:
                    fb_input = fallback_result.get("usage", {}).get("prompt_tokens", input_tokens)
                    fb_output = fallback_result.get("usage", {}).get("completion_tokens", 0)
                    store.add_record({
                        "model": f"{clean_model_name} -> {fallback_result.get('model', '?')}",
                        "type": "fallback",
                        "request": json.dumps(request_body, ensure_ascii=False, indent=2),
                        "response": json.dumps(fallback_result, ensure_ascii=False, indent=2),
                        "input_tokens": fb_input,
                        "output_tokens": fb_output,
                        "latency": round(time.time() - start_time, 2),
                        "status": 200,
                        "fallback": True,
                        "retry_count": attempt
                    })
                    store.update_stats(f"fallback/{fallback_result.get('model', '?')}", fb_input, fb_output, time.time() - start_time)
                    return JSONResponse(content=fallback_result)

                # 폴백 불가 - 에러 반환
                store.add_record({
                    "model": clean_model_name,
                    "type": "vertex",
                    "request": json.dumps(request_body, ensure_ascii=False, indent=2),
                    "response": "ERROR: Content blocked by safety filters (no fallback)",
                    "input_tokens": input_tokens,
                    "output_tokens": 0,
                    "latency": round(time.time() - start_time, 2),
                    "status": 451,
                    "retry_count": attempt
                })
                return JSONResponse(
                    content={"error": {"message": "Content blocked by safety filters and no fallback model configured", "type": "content_blocked"}},
                    status_code=451
                )

            # 응답 처리 - 전체 응답 객체를 JSON으로 변환
            response_text = response.text if hasattr(response, 'text') else str(response)
            output_tokens = estimate_tokens(response_text)
            
            # 실제 토큰 사용량이 있으면 사용
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                input_tokens = getattr(response.usage_metadata, 'prompt_token_count', input_tokens)
                output_tokens = getattr(response.usage_metadata, 'candidates_token_count', output_tokens)
            
            # 전체 응답 JSON 생성
            full_response_json = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": response_text
                        },
                        "finish_reason": "stop",
                        "index": 0
                    }
                ],
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens
                },
                "model": actual_model
            }
            
            # 암시적 캐싱 정보 추출
            cached_content = False
            cache_details = {}
            
            # usage_metadata에서 캐싱 정보 확인
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                usage_meta = response.usage_metadata
                # cached_content_token_count가 있으면 캐싱이 적용됨
                if hasattr(usage_meta, 'cached_content_token_count'):
                    cached_tokens = usage_meta.cached_content_token_count
                    if cached_tokens and cached_tokens > 0:
                        cached_content = True
                        cache_details["cached_token_count"] = cached_tokens
                        cache_details["cache_used"] = True
            
            # candidates에서 캐싱 메타데이터 확인
            if hasattr(response, 'candidates') and response.candidates:
                for candidate in response.candidates:
                    if hasattr(candidate, 'finish_reason'):
                        cache_details["finish_reason"] = str(candidate.finish_reason)
                    if hasattr(candidate, 'safety_ratings'):
                        cache_details["safety_ratings"] = str(candidate.safety_ratings)
            
            # 캐싱 정보를 응답 JSON에 추가
            if cache_details:
                full_response_json["cache_info"] = cache_details
            
            response_json_str = json.dumps(full_response_json, ensure_ascii=False, indent=2)
            
            # 기록 저장 - 전체 JSON으로 저장
            store.add_record({
                "model": clean_model_name,
                "type": "vertex",
                "request": json.dumps(request_body, ensure_ascii=False, indent=2),
                "response": response_json_str,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency": round(latency, 2),
                "status": 200,
                "cached": cached_content,
                "retry_count": attempt
            })
            
            # 통계 업데이트
            store.update_stats(f"vertex/{clean_model_name}", input_tokens, output_tokens, latency)
        
        # OpenAI 호환 응답 형식으로 변환 (항상 반환)
        response_text = response.text if hasattr(response, 'text') else str(response)
        response_json = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": response_text
                    },
                    "finish_reason": "stop",
                    "index": 0
                }
            ],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens
            },
            "model": actual_model
        }
        
        return JSONResponse(content=response_json)
        
    except Exception as e:
        store.remove_pending(request_id)
        error_msg = str(e)
        # 에러 로그 출력
        print(f"[VERTEX ERROR] {clean_model_name}: {error_msg}")

        # 검열 차단 여부 확인 (response.text 접근 시 예외 발생 케이스)
        if is_content_blocked_error(error_msg):
            print(f"[VERTEX BLOCKED] {clean_model_name} - 검열로 인해 응답 차단됨")
            fallback_result = await execute_fallback(request_body, request_text_raw, "vertex")
            if fallback_result:
                fb_input = fallback_result.get("usage", {}).get("prompt_tokens", input_tokens)
                fb_output = fallback_result.get("usage", {}).get("completion_tokens", 0)
                store.add_record({
                    "model": f"{clean_model_name} -> {fallback_result.get('model', '?')}",
                    "type": "fallback",
                    "request": json.dumps(request_body, ensure_ascii=False, indent=2),
                    "response": json.dumps(fallback_result, ensure_ascii=False, indent=2),
                    "input_tokens": fb_input,
                    "output_tokens": fb_output,
                    "latency": round(time.time() - start_time, 2),
                    "status": 200,
                    "fallback": True,
                    "retry_count": attempt
                })
                store.update_stats(f"fallback/{fallback_result.get('model', '?')}", fb_input, fb_output, time.time() - start_time)
                return JSONResponse(content=fallback_result)

        # 취소된 요청인 경우 "user canceled" 메시지 반환
        if store.is_cancelled(request_id):
            store.clear_cancelled(request_id)
            return JSONResponse(
                content={
                    "error": {
                        "message": "user canceled",
                        "code": "user_canceled",
                        "param": "model",
                        "type": "invalid_request_error"
                    }
                },
                status_code=499  # Client Closed Request
            )

        # 일반 에러 기록 저장
        store.add_record({
            "model": clean_model_name,
            "type": "vertex",
            "request": json.dumps(request_body, ensure_ascii=False, indent=2),
            "response": f"ERROR: {error_msg}",
            "input_tokens": input_tokens,
            "output_tokens": 0,
            "latency": round(time.time() - start_time, 2),
            "status": 500,
            "retry_count": attempt
        })
        
        return JSONResponse(
            content={"error": {"message": error_msg, "type": "vertex_error"}},
            status_code=500
        )

# Copilot 프록시
@app.api_route("/copilot/{model_name:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_copilot(request: Request, model_name: str):
    """GitHub Copilot API 프록시 - gpt-4.1, gpt-41, gemini-3.1-pro-preview, gemini-3-flash-preview, claude-opus-4.5, claude-opus-4-6 지원, 400 오류 시 재시도"""
    start_time = time.time()
    request_id = hashlib.md5(f"{time.time()}{model_name}".encode()).hexdigest()[:8]
    
    if not COPILOT_KEY:
        raise HTTPException(status_code=401, detail="Copilot API 키가 없습니다")
    
    # 경로에서 접미사 제거
    clean_model_name = model_name.split("/")[0] if "/" in model_name else model_name
    
    # 지원하는 모델 확인 (gpt-4.1, gpt-41, gemini-3.1-pro-preview, gemini-3-flash-preview 지원)
    if clean_model_name not in ["gpt-4.1", "gpt-41", "gemini-3.1-pro-preview", "gemini-3-flash-preview", "claude-opus-4.5", "claude-opus-4-6"]:
        raise HTTPException(status_code=400, detail=f"등록되지 않은 모델입니다: {clean_model_name}. 지원 모델: gpt-4.1, gpt-41, gemini-3.1-pro-preview, gemini-3-flash-preview, claude-opus-4.5, claude-opus-4-6")
    
    # 요청 본문 읽기
    body = await request.body()
    try:
        request_body = json.loads(body) if body else {}
    except:
        request_body = {}
    
    # 사용자 입력 로그 기록
    input_logger.log_input(
        log_type="copilot_request",
        endpoint=f"/copilot/{model_name}",
        data={
            "model": clean_model_name,
            "request_body": request_body,
            "headers": dict(request.headers)
        }
    )
    
    # Copilot API URL
    url = "https://api.githubcopilot.com/chat/completions"
    
    # 모델 설정 - 모델명 그대로 전달
    request_body["model"] = clean_model_name
    actual_model = clean_model_name
    
    # 대기 요청 등록 - 전체 요청 본문을 JSON 문자열로 저장
    request_text = json.dumps(request_body, ensure_ascii=False, indent=2)
    copilot_input_tokens = estimate_tokens(extract_text_from_content(request_body.get("messages", "")))
    store.add_pending(request_id, {
        "model": actual_model,
        "type": "copilot",
        "start_time": start_time,
        "request_preview": request_text,
        "request_body": request_body,  # 재시도를 위해 전체 요청 저장
        "input_tokens": copilot_input_tokens
    })
    
    # 요청 헤더 구성
    headers = {
        "Authorization": f"Bearer {COPILOT_KEY}",
        "Content-Type": "application/json",
        "Editor-Version": "vscode/1.92.0",
        "Editor-Plugin-Version": "copilot/1.220.0",
        "User-Agent": "GithubCopilot/1.220.0",
    }
    
    input_tokens = estimate_tokens(request_text)
    
    # 재시도 설정 가져오기
    retry_count = settings_manager.settings["retry_count"]
    retry_delay = settings_manager.settings["retry_delay"]
    
    try:
        last_response = None
        last_status_code = None
        last_response_body = None
        
        async with httpx.AsyncClient(timeout=None) as client:
            attempt = 0
            while attempt <= retry_count:
                # 1. 태스크 생성
                llm_task = asyncio.create_task(
                    client.post(
                        url,
                        content=json.dumps(request_body).encode(),
                        headers=headers
                    )
                )
                
                # 2. 인터럽트 대기 이벤트 연결
                interrupt_event = store.interrupt_events.get(request_id)
                if not interrupt_event:
                    # 취소 등의 이유로 이벤트가 사라진 경우
                    response = await llm_task
                    last_status_code = response.status_code
                    last_response_body = response.content
                    break
                    
                interrupt_task = asyncio.create_task(interrupt_event.wait())
                
                # 3. 레이스 조건
                done, pending = await asyncio.wait(
                    [llm_task, interrupt_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                if interrupt_task in done:
                    # 수동 조작으로 인터럽트 발생
                    llm_task.cancel()  # 기존 HTTP 요청 취소
                    action = store.interrupt_actions.get(request_id)
                    interrupt_event.clear()  # 다음 인터럽트를 위해 리셋
                    
                    if action == "cancel":
                        print(f"[COPILOT CANCELLED] {actual_model} - 사용자 수동 취소")
                        store.mark_cancelled(request_id)
                        store.remove_pending(request_id)
                        
                        cancel_response = {
                            "error": {
                                "message": "user canceled",
                                "code": "user_canceled",
                                "param": "model",
                                "type": "invalid_request_error"
                            }
                        }
                        
                        # 취소 기록 저장
                        store.add_record({
                            "model": actual_model,
                            "type": "copilot",
                            "request": json.dumps(request_body, ensure_ascii=False, indent=2),
                            "response": json.dumps(cancel_response, ensure_ascii=False, indent=2),
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "latency": round(time.time() - start_time, 2),
                            "status": 499,
                            "retry_count": attempt
                        })
                        
                        return JSONResponse(
                            content=cancel_response,
                            status_code=499
                        )
                    elif action == "retry":
                        print(f"[COPILOT RETRY MANUAL] {actual_model} - 사용자 수동 재시도: 기존 연결 재사용, 새 요청 시작")
                        attempt = 0  # 횟수 초기화
                        continue
                else:
                    # 정상적으로 LLM 응답 도착
                    interrupt_task.cancel()
                    response = llm_task.result()
                    last_response = response
                    last_status_code = response.status_code
                    last_response_body = response.content

                    # 400 오류이고 재시도 횟수가 남아있으면 재시도
                    if response.status_code == 400 and attempt < retry_count:
                        print(f"[COPILOT RETRY] {actual_model} - 400 오류, {attempt + 1}번째 재시도 (총 {retry_count}회)")
                        await asyncio.sleep(retry_delay)
                        attempt += 1
                        continue
                    else:
                        # 성공 또는 재시도 횟수 초과
                        break
            
        end_time = time.time()
        latency = end_time - start_time

        # 대기 요청 제거
        store.remove_pending(request_id)

        # 취소된 요청인지 확인 - 취소된 경우 기록 저장하지 않고 응답 반환
        if store.is_cancelled(request_id):
            print(f"[COPILOT CANCELLED] {actual_model} - 요청이 취소됨, 기록 저장 생략")
            store.clear_cancelled(request_id)
            return JSONResponse(
                content={
                    "error": {
                        "message": "user canceled",
                        "code": "user_canceled",
                        "param": "model",
                        "type": "invalid_request_error"
                    }
                },
                status_code=499
            )

        # 응답 처리
        try:
            response_json = json.loads(last_response_body)
        except:
            response_json = {}

        # 검열 차단 여부 확인
        if is_copilot_response_blocked(response_json):
            print(f"[COPILOT BLOCKED] {actual_model} - 검열로 인해 응답 차단됨")
            store.remove_pending(request_id)

            # 폴백 시도
            fallback_result = await execute_fallback(request_body, request_text, "copilot")
            if fallback_result:
                fb_input = fallback_result.get("usage", {}).get("prompt_tokens", input_tokens)
                fb_output = fallback_result.get("usage", {}).get("completion_tokens", 0)
                store.add_record({
                    "model": f"{actual_model} -> {fallback_result.get('model', '?')}",
                    "type": "fallback",
                    "request": request_text,
                    "response": json.dumps(fallback_result, ensure_ascii=False, indent=2),
                    "input_tokens": fb_input,
                    "output_tokens": fb_output,
                    "latency": round(time.time() - start_time, 2),
                    "status": 200,
                    "fallback": True,
                    "retry_count": attempt
                })
                store.update_stats(f"fallback/{fallback_result.get('model', '?')}", fb_input, fb_output, time.time() - start_time)
                return JSONResponse(content=fallback_result)

            # 폴백 불가 - 에러 기록 저장
            store.add_record({
                "model": actual_model,
                "type": "copilot",
                "request": request_text,
                "response": "ERROR: Content blocked by safety filters (no fallback)",
                "input_tokens": input_tokens,
                "output_tokens": 0,
                "latency": round(time.time() - start_time, 2),
                "status": 451,
                "retry_count": attempt
            })
            return JSONResponse(
                content={"error": {"message": "Content blocked by safety filters and no fallback model configured", "type": "content_blocked"}},
                status_code=451
            )

        # 응답 텍스트 및 토큰 추출
        response_text = json.dumps(response_json, ensure_ascii=False, indent=2)
        output_tokens = estimate_tokens(response_text)
        
        # 실제 토큰 사용량이 있으면 사용
        usage = response_json.get("usage", {})
        if usage:
            input_tokens = usage.get("prompt_tokens", input_tokens)
            output_tokens = usage.get("completion_tokens", output_tokens)
        
        # 기록 저장 - 전체 JSON 저장
        store.add_record({
            "model": actual_model,
            "type": "copilot",
            "request": request_text,
            "response": response_text,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency": round(latency, 2),
            "status": last_status_code,
            "retry_count": attempt
        })
        
        # 통계 업데이트
        store.update_stats(f"copilot/{actual_model}", input_tokens, output_tokens, latency)
        
        return Response(
            content=last_response_body,
            status_code=last_status_code,
            media_type="application/json"
        )
        
    except Exception as e:
        store.remove_pending(request_id)
        error_msg = str(e)
        # 에러 로그 출력
        print(f"[COPILOT ERROR] {actual_model}: {error_msg}")

        # 취소된 요청인 경우 "user canceled" 메시지 반환
        if store.is_cancelled(request_id):
            store.clear_cancelled(request_id)
            return JSONResponse(
                content={
                    "error": {
                        "message": "user canceled",
                        "code": "user_canceled",
                        "param": "model",
                        "type": "invalid_request_error"
                    }
                },
                status_code=499  # Client Closed Request
            )

        # 에러 기록 저장
        store.add_record({
            "model": actual_model,
            "type": "copilot",
            "request": request_text,
            "response": f"ERROR: {error_msg}",
            "input_tokens": input_tokens,
            "output_tokens": 0,
            "latency": round(time.time() - start_time, 2),
            "status": 500,
            "retry_count": attempt
        })
        
        raise HTTPException(status_code=500, detail=str(e))

# ZAI 프록시 (Anthropic Messages API 형식)
@app.api_route("/zai/{model_name:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_zai(request: Request, model_name: str):
    """ZAI API 프록시 - Anthropic Messages API 형식, glm-5.1 지원"""
    start_time = time.time()
    request_id = hashlib.md5(f"{time.time()}{model_name}".encode()).hexdigest()[:8]

    if not ZAI_KEY:
        raise HTTPException(status_code=401, detail="ZAI API 키가 없습니다")

    # 경로에서 접미사 제거
    clean_model_name = model_name.split("/")[0] if "/" in model_name else model_name

    # 지원하는 모델 확인
    if clean_model_name not in ["glm-5.1"]:
        raise HTTPException(status_code=400, detail=f"등록되지 않은 모델입니다: {clean_model_name}. 지원 모델: glm-5.1")

    # 요청 본문 읽기
    body = await request.body()
    try:
        request_body = json.loads(body) if body else {}
    except:
        request_body = {}

    # 사용자 입력 로그 기록
    input_logger.log_input(
        log_type="zai_request",
        endpoint=f"/zai/{model_name}",
        data={
            "model": clean_model_name,
            "request_body": request_body,
            "headers": dict(request.headers)
        }
    )

    # Anthropic Messages API 형식으로 모델 설정
    request_body["model"] = clean_model_name
    actual_model = clean_model_name

    # ZAI thinking 설정 주입 (extra_body)
    zai_thinking = settings_manager.settings.get("zai_thinking", "disabled")
    if zai_thinking == "disabled":
        request_body["thinking"] = {"type": "disabled"}
    elif zai_thinking == "enabled":
        budget = settings_manager.settings.get("zai_thinking_budget", 8000)
        request_body["thinking"] = {"type": "enabled", "budget_tokens": budget}

    # 요청 텍스트 (전체 JSON)
    request_text = json.dumps(request_body, ensure_ascii=False, indent=2)

    # 토큰 추정은 messages에서 추출
    messages_text = extract_text_from_content(request_body.get("messages", "")) if "messages" in request_body else ""

    # 대기 요청 등록
    input_tokens = estimate_tokens(messages_text)
    store.add_pending(request_id, {
        "model": actual_model,
        "type": "zai",
        "start_time": start_time,
        "request_preview": request_text,
        "request_body": request_body,
        "input_tokens": input_tokens
    })

    # ZAI API URL
    url = "https://api.z.ai/api/anthropic/v1/messages"

    # 요청 헤더 구성 (Anthropic API 형식)
    headers = {
        "x-api-key": ZAI_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    # ZAI 재시도 설정 가져오기
    retry_count = settings_manager.settings["zai_retry_count"]
    retry_delay = settings_manager.settings["zai_retry_delay"]

    try:
        last_response_body = None
        last_status_code = None

        async with httpx.AsyncClient(timeout=None) as client:
            attempt = 0
            while attempt <= retry_count:
                # 1. 태스크 생성
                llm_task = asyncio.create_task(
                    client.post(
                        url,
                        content=json.dumps(request_body).encode(),
                        headers=headers
                    )
                )

                # 2. 인터럽트 대기 이벤트 연결
                interrupt_event = store.interrupt_events.get(request_id)
                if not interrupt_event:
                    response = await llm_task
                    last_status_code = response.status_code
                    last_response_body = response.content
                    break

                interrupt_task = asyncio.create_task(interrupt_event.wait())

                # 3. 레이스 조건
                done, pending = await asyncio.wait(
                    [llm_task, interrupt_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                if interrupt_task in done:
                    llm_task.cancel()
                    action = store.interrupt_actions.get(request_id)
                    interrupt_event.clear()

                    if action == "cancel":
                        print(f"[ZAI CANCELLED] {actual_model} - 사용자 수동 취소")
                        store.mark_cancelled(request_id)
                        store.remove_pending(request_id)

                        cancel_response = {
                            "error": {
                                "message": "user canceled",
                                "code": "user_canceled",
                                "param": "model",
                                "type": "invalid_request_error"
                            }
                        }

                        store.add_record({
                            "model": actual_model,
                            "type": "zai",
                            "request": request_text,
                            "response": json.dumps(cancel_response, ensure_ascii=False, indent=2),
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "latency": round(time.time() - start_time, 2),
                            "status": 499,
                            "retry_count": attempt
                        })

                        return JSONResponse(
                            content=cancel_response,
                            status_code=499
                        )
                    elif action == "retry":
                        print(f"[ZAI RETRY MANUAL] {actual_model} - 사용자 수동 재시도")
                        attempt = 0
                        continue
                else:
                    # 정상적으로 LLM 응답 도착
                    interrupt_task.cancel()
                    response = llm_task.result()
                    last_status_code = response.status_code
                    last_response_body = response.content

                    # 400/429 오류이고 재시도 횟수가 남아있으면 재시도
                    if response.status_code in [400, 429] and attempt < retry_count:
                        print(f"[ZAI RETRY] {actual_model} - {response.status_code} 오류, {attempt + 1}번째 재시도 (총 {retry_count}회)")
                        await asyncio.sleep(retry_delay)
                        attempt += 1
                        continue
                    else:
                        break

        end_time = time.time()
        latency = end_time - start_time

        # 대기 요청 제거
        store.remove_pending(request_id)

        # 취소된 요청인지 확인
        if store.is_cancelled(request_id):
            print(f"[ZAI CANCELLED] {actual_model} - 요청이 취소됨, 기록 저장 생략")
            store.clear_cancelled(request_id)
            return JSONResponse(
                content={
                    "error": {
                        "message": "user canceled",
                        "code": "user_canceled",
                        "param": "model",
                        "type": "invalid_request_error"
                    }
                },
                status_code=499
            )

        # 응답 처리
        try:
            response_json = json.loads(last_response_body)
        except:
            response_json = {}

        # 에러 응답 처리
        if last_status_code != 200:
            error_msg = last_response_body.decode('utf-8', errors='replace')[:500] if last_response_body else "Unknown error"
            print(f"[ZAI ERROR] {actual_model}: HTTP {last_status_code} - {error_msg[:200]}")

            store.add_record({
                "model": actual_model,
                "type": "zai",
                "request": request_text,
                "response": f"ERROR: {error_msg}",
                "input_tokens": input_tokens,
                "output_tokens": 0,
                "latency": round(latency, 2),
                "status": last_status_code,
                "retry_count": attempt
            })

            return JSONResponse(
                content={"error": {"message": error_msg, "type": "zai_error"}},
                status_code=last_status_code
            )

        # Anthropic 응답에서 텍스트 추출
        response_text = ""
        if "content" in response_json:
            for block in response_json["content"]:
                if isinstance(block, dict) and block.get("type") == "text":
                    response_text += block.get("text", "")

        # 토큰 사용량 추출 (Anthropic usage 형식)
        output_tokens = estimate_tokens(response_text)
        if "usage" in response_json:
            usage = response_json["usage"]
            input_tokens = usage.get("input_tokens", input_tokens)
            output_tokens = usage.get("output_tokens", output_tokens)

        # 기록 저장
        store.add_record({
            "model": actual_model,
            "type": "zai",
            "request": request_text,
            "response": response_text[:500],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency": round(latency, 2),
            "status": 200,
            "retry_count": attempt
        })

        store.update_stats(f"zai/{actual_model}", input_tokens, output_tokens, latency)

        # 원본 Anthropic 응답 그대로 반환
        return Response(
            content=last_response_body,
            status_code=200,
            media_type="application/json"
        )

    except Exception as e:
        error_msg = str(e)
        print(f"[ZAI ERROR] {actual_model}: {error_msg}")

        store.remove_pending(request_id)
        store.add_record({
            "model": actual_model,
            "type": "zai",
            "request": request_text,
            "response": f"ERROR: {error_msg}",
            "input_tokens": input_tokens,
            "output_tokens": 0,
            "latency": round(time.time() - start_time, 2),
            "status": 500,
            "retry_count": attempt
        })

        raise HTTPException(status_code=500, detail=str(e))

# 웹 대시보드
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """웹 대시보드 - 정적 HTML 파일 반환"""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return HTMLResponse(content="<h1>Dashboard not found</h1>", status_code=404)

# API 엔드포인트
@app.get("/api/records")
async def get_records():
    """요청 기록 조회"""
    return store.get_records()

@app.get("/api/stats")
async def get_stats():
    """모델별 통계 조회"""
    return store.get_stats()

@app.post("/api/stats/reset")
async def reset_stats():
    """통계 초기화"""
    store.reset_stats()
    return {"status": "ok"}

@app.get("/api/pending")
async def get_pending():
    """대기 중인 요청 조회"""
    return store.get_pending()

@app.get("/api/settings")
async def get_settings():
    """재시도 설정 조회"""
    return settings_manager.get_settings()

@app.post("/api/settings")
async def update_settings(request: Request):
    """재시도 설정 업데이트"""
    try:
        body = await request.body()
        new_settings = json.loads(body) if body else {}
        
        # 사용자 입력 로그 기록
        input_logger.log_input(
            log_type="settings_update",
            endpoint="/api/settings",
            data={
                "new_settings": new_settings
            }
        )
        
        return settings_manager.update_settings(new_settings)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/input-logs")
async def get_input_logs():
    """사용자 입력 로그 조회 (버그 분석용)"""
    return input_logger.get_logs()

@app.post("/api/input-logs/clear")
async def clear_input_logs():
    """사용자 입력 로그 초기화"""
    input_logger.clear_logs()
    return {"status": "ok"}

@app.post("/api/retry/{request_id}")
async def retry_request(request_id: str):
    """대기 중인 요청 재시도 - 즉시 해당 요청 핸들러에 재시도 신호 전송"""
    pending_data = store.get_pending_by_id(request_id)
    
    if not pending_data:
        raise HTTPException(status_code=404, detail="요청을 찾을 수 없습니다")
    
    request_type = pending_data.get("type")
    model = pending_data.get("model")
    
    # 사용자 입력 로그 기록
    input_logger.log_input(
        log_type="retry_request",
        endpoint=f"/api/retry/{request_id}",
        data={
            "request_id": request_id,
            "request_type": request_type,
            "model": model
        }
    )
    
    # 해당 요청 핸들러로 재시도(interrupt) 신호 전송
    print(f"[{request_type.upper()} RETRY SIGNAL] {model} - 수동 재시도 신호 전송")
    pending_data["start_time"] = time.time() # 요구사항11: 타이머 초기화
    pending_data["retry_count"] = pending_data.get("retry_count", 0) + 1
    return {"status": "retry_started", "request_id": request_id, "type": request_type, "model": model}


@app.post("/api/cancel/{request_id}")
async def cancel_request(request_id: str):
    """대기 중인 요청 취소"""
    pending_data = store.get_pending_by_id(request_id)
    
    if not pending_data:
        raise HTTPException(status_code=404, detail="요청을 찾을 수 없습니다")
    
    request_type = pending_data.get("type")
    model = pending_data.get("model")
    
    print(f"[{request_type.upper()} CANCEL SIGNAL] {model} - 사용자 취소 신호 전송")
    # 취소 신호 전송
    store.trigger_interrupt(request_id, "cancel")
    
    return {"status": "cancelled", "request_id": request_id, "type": request_type, "model": model}





# 가격 정보 파일 경로
PRICE_FILE = Path(__file__).parent / "API_PRICE" / "vertex_price.json"

@app.get("/api/copilot/quota")
async def get_copilot_quota():
    """Copilot 남은 사용량 조회 (GitHub copilot_internal/user API)"""
    if not COPILOT_KEY:
        return {"configured": False, "error": "Copilot API 키가 없습니다"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                "https://api.github.com/copilot_internal/user",
                headers={
                    "Authorization": f"Bearer {COPILOT_KEY}",
                    "Accept": "application/json",
                    "Origin": "vscode-file://vscode-app",
                }
            )
            if response.status_code == 200:
                return {"configured": True, "data": response.json()}
            else:
                return {"configured": True, "error": f"API 오류: HTTP {response.status_code}"}
    except Exception as e:
        return {"configured": False, "error": str(e)}

# ZAI 사용량 조회
def generate_zai_token(apikey: str, exp_seconds: int = 300):
    """ZAI API 키로 JWT 토큰 생성"""
    try:
        id_part, secret = apikey.split('.')
        payload = {
            'api_key': id_part,
            'exp': int(round(time.time() * 1000)) + exp_seconds * 1000,
            'timestamp': int(round(time.time() * 1000)),
        }
        return jwt.encode(
            payload,
            secret,
            algorithm='HS256',
            headers={'alg': 'HS256', 'sign_type': 'SIGN'},
        )
    except Exception:
        return None

@app.get("/api/zai/quota")
async def get_zai_quota():
    """ZAI 남은 사용량 조회"""
    if not ZAI_KEY:
        return {"configured": False, "error": "ZAI API 키가 없습니다"}
    try:
        token = generate_zai_token(ZAI_KEY)
        if not token:
            return {"configured": False, "error": "API 키 형식이 잘못되었습니다"}

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                "https://api.z.ai/api/monitor/usage/quota/limit",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
            )
            if response.status_code == 200:
                return {"configured": True, "data": response.json()}
            else:
                return {"configured": True, "error": f"API 오류: HTTP {response.status_code}"}
    except Exception as e:
        return {"configured": False, "error": str(e)}

# Tavily 사용량 조회
async def get_tavily_key_credits(api_key: str) -> Optional[Dict[str, Any]]:
    """단일 Tavily 키의 잔여 크레딧 조회"""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                "https://api.tavily.com/usage",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }
            )
            if response.status_code == 200:
                data = response.json()
                used = data.get("usage", 0)
                limit = data.get("limit", 1000)
                remaining = limit - used
                return {"used": used, "limit": limit, "remaining": remaining}
            else:
                return {"used": None, "limit": None, "remaining": None, "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"used": None, "limit": None, "remaining": None, "error": str(e)}

@app.get("/api/tavily/quota")
async def get_tavily_quota():
    """Tavily 각 키별 남은 사용량 조회 (키는 마스킹하여 반환)"""
    tavily_keys = get_tavily_keys()
    if not tavily_keys:
        return {"configured": False, "error": "Tavily API 키가 없습니다"}
    try:
        keys_info = []
        for i, key in enumerate(tavily_keys):
            credits = await get_tavily_key_credits(key)
            keys_info.append({
                "label": f"#{i + 1}",
                **credits
            })
        return {"configured": True, "keys": keys_info}
    except Exception as e:
        return {"configured": False, "error": str(e)}

@app.get("/tavily_valid_key")
async def tavily_valid_key(request: Request):
    """사용량이 가장 많이 남은 Tavily API 키 반환"""
    tavily_keys = get_tavily_keys()
    if not tavily_keys:
        raise HTTPException(status_code=404, detail="Tavily API 키가 없습니다")

    best_key = None
    best_remaining = -1
    best_info = {}

    for i, key in enumerate(tavily_keys):
        credits = await get_tavily_key_credits(key)
        remaining = credits.get("remaining")
        if remaining is not None and remaining > best_remaining:
            best_remaining = remaining
            best_key = key
            best_info = credits

    if best_key is None:
        raise HTTPException(status_code=500, detail="사용 가능한 Tavily 키를 찾을 수 없습니다")

    return {
        "api_key": best_key,
        "remaining": best_remaining,
        "limit": best_info.get("limit"),
        "used": best_info.get("used")
    }

@app.get("/api/prices")
async def get_prices():
    """모델별 가격 정보 조회"""
    try:
        if PRICE_FILE.exists():
            with open(PRICE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {"pricing_unit": "USD per 1M tokens", "models": []}
    except Exception as e:
        return {"pricing_unit": "USD per 1M tokens", "models": [], "error": str(e)}

@app.get("/health")
async def health_check():
    """헬스 체크"""
    return {
        "status": "healthy",
        "vertex_projects": vertex_auth.get_project_ids(),
        "copilot_configured": COPILOT_KEY is not None,
        "zai_configured": ZAI_KEY is not None,
        "tavily_configured": len(get_tavily_keys()) > 0,
        "tavily_key_count": len(get_tavily_keys())
    }

if __name__ == "__main__":
    import uvicorn
    import sys
    import io
    # 윈도우 콘솔 인코딩 설정
    if sys.platform == 'win32':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    print(f"[START] LLM 프록시 서버 시작: http://localhost:{PORT}")
    print(f"[DASHBOARD] 대시보드: http://localhost:{PORT}")
    print(f"[VERTEX] Vertex AI: http://localhost:{PORT}/vertex/{{model_name}}")
    print(f"[COPILOT] Copilot: http://localhost:{PORT}/copilot/{{model_name}}")
    print(f"[ZAI] ZAI: http://localhost:{PORT}/zai/{{model_name}}")
    # 대시보드 자동 열기
    import webbrowser
    webbrowser.open(f"http://127.0.0.1:{PORT}/")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
