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
        "vertex_retry_delay": 10  # Vertex 재시도 간격 (초, 0~600초 =0~10분)
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

def estimate_tokens(text: str) -> int:
    """간단한 토큰 추정 (실제 토크나이저 없이 근사치)"""
    if not text:
        return 0
    # 영어 기준 약 4자 = 1토큰, 한글 기준 약 2자 = 1토큰
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
    
    # 요청 텍스트 추출
    request_text = ""
    if "contents" in request_body:
        request_text = extract_text_from_content(request_body.get("contents", ""))
    elif "prompt" in request_body:
        request_text = request_body.get("prompt", "")
    elif "text" in request_body:
        request_text = request_body.get("text", "")
    elif "messages" in request_body:
        # OpenAI 형식의 messages에서 텍스트 추출
        request_text = extract_text_from_content(request_body.get("messages", ""))
    
    input_tokens = estimate_tokens(request_text)
    
    # 대기 요청 등록 - 재시도를 위해 전체 요청 데이터 저장
    store.add_pending(request_id, {
        "model": clean_model_name,
        "type": "vertex",
        "start_time": start_time,
        "request_preview": request_text[:200],
        "request_body": request_body  # 재시도를 위해 전체 요청 저장
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
                lambda: model.generate_content(request_text)
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
                        "status": 499
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
                "cached": cached_content
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
            "status": 500
        })
        
        return JSONResponse(
            content={"error": {"message": error_msg, "type": "vertex_error"}},
            status_code=500
        )

# Copilot 프록시
@app.api_route("/copilot/{model_name:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_copilot(request: Request, model_name: str):
    """GitHub Copilot API 프록시 - gpt-4.1, gpt-41 지원, 400 오류 시 재시도"""
    start_time = time.time()
    request_id = hashlib.md5(f"{time.time()}{model_name}".encode()).hexdigest()[:8]
    
    if not COPILOT_KEY:
        raise HTTPException(status_code=401, detail="Copilot API 키가 없습니다")
    
    # 경로에서 접미사 제거
    clean_model_name = model_name.split("/")[0] if "/" in model_name else model_name
    
    # 지원하는 모델 확인 (gpt-4.1, gpt-41 지원)
    if clean_model_name not in ["gpt-4.1", "gpt-41"]:
        raise HTTPException(status_code=400, detail=f"등록되지 않은 모델입니다: {clean_model_name}. 지원 모델: gpt-4.1, gpt-41")
    
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
    
    # 모델 설정 - gpt-41로 요청 시 gpt41로 변환 (에러 메시지 관측용), gpt-4.1은 그대로
    if clean_model_name == "gpt-41":
        request_body["model"] = "gpt41"
    else:
        request_body["model"] = clean_model_name
    actual_model = clean_model_name
    
    # 대기 요청 등록 - 전체 요청 본문을 JSON 문자열로 저장
    request_text = json.dumps(request_body, ensure_ascii=False, indent=2)
    store.add_pending(request_id, {
        "model": actual_model,
        "type": "copilot",
        "start_time": start_time,
        "request_preview": request_text[:200],
        "request_body": request_body  # 재시도를 위해 전체 요청 저장
    })
    
    # 요청 헤더 구성
    headers = {
        "Authorization": f"Bearer {COPILOT_KEY}",
        "Content-Type": "application/json",
        "Editor-Version": "vscode/1.85.0",
        "Editor-Plugin-Version": "copilot/1.150.0",
    }
    
    input_tokens = estimate_tokens(request_text)
    
    # 재시도 설정 가져오기
    retry_count = settings_manager.settings["retry_count"]
    retry_delay = settings_manager.settings["retry_delay"]
    
    try:
        last_response = None
        last_status_code = None
        last_response_body = None
        
        async with httpx.AsyncClient(timeout=300.0) as client:
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
                            "status": 499
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
            "status": last_status_code
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
            "status": 500
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
        "copilot_configured": COPILOT_KEY is not None
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
    uvicorn.run(app, host="127.0.0.1", port=PORT)
