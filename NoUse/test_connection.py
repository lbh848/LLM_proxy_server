"""
LLM 프록시 서버 연결 테스트 스크립트
- Vertex AI Gemini 모델 연결 테스트
- GitHub Copilot 연결 테스트
"""

import httpx
import json
import asyncio
from pathlib import Path

PROXY_URL = "http://localhost:30004"

# 테스트할 모델 목록
VERTEX_MODELS = [
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
]

COPILOT_MODELS = [
    "gpt-4.1",
]

def print_header(title: str):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)

def print_result(model: str, success: bool, message: str = "", latency: float = 0):
    status = "✅ 성공" if success else "❌ 실패"
    print(f"\n{status} - {model}")
    if latency > 0:
        print(f"   응답 시간: {latency:.2f}초")
    if message:
        print(f"   {message}")

async def test_vertex_model(model: str) -> tuple[bool, str, float]:
    """Vertex AI 모델 테스트"""
    url = f"{PROXY_URL}/vertex/{model}"
    
    # 간단한 테스트 요청
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": "안녕하세요. 간단히 인사해 주세요."}
                ]
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 100,
            "temperature": 0.7
        }
    }
    
    try:
        start_time = asyncio.get_event_loop().time()
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(url, json=payload)
        end_time = asyncio.get_event_loop().time()
        latency = end_time - start_time
        
        if response.status_code == 200:
            try:
                data = response.json()
                # 응답에서 텍스트 추출
                text = ""
                if "candidates" in data:
                    for candidate in data["candidates"]:
                        if "content" in candidate and "parts" in candidate["content"]:
                            for part in candidate["content"]["parts"]:
                                if "text" in part:
                                    text += part["text"]
                return True, f"응답: {text[:100]}..." if len(text) > 100 else f"응답: {text}", latency
            except:
                return True, f"상태 코드: {response.status_code}", latency
        else:
            try:
                error = response.json()
                return False, f"에러: {error.get('detail', response.text[:200])}", latency
            except:
                return False, f"상태 코드: {response.status_code}, {response.text[:200]}", latency
                
    except httpx.TimeoutException:
        return False, "타임아웃 (120초 초과)", 0
    except Exception as e:
        return False, f"연결 오류: {str(e)}", 0

async def test_copilot_model(model: str) -> tuple[bool, str, float]:
    """Copilot 모델 테스트"""
    url = f"{PROXY_URL}/copilot/{model}"
    
    # 간단한 테스트 요청
    payload = {
        "messages": [
            {"role": "user", "content": "안녕하세요. 간단히 인사해 주세요."}
        ],
        "max_tokens": 100,
        "temperature": 0.7
    }
    
    try:
        start_time = asyncio.get_event_loop().time()
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(url, json=payload)
        end_time = asyncio.get_event_loop().time()
        latency = end_time - start_time
        
        if response.status_code == 200:
            try:
                data = response.json()
                # 응답에서 텍스트 추출
                text = ""
                if "choices" in data:
                    for choice in data["choices"]:
                        if "message" in choice and "content" in choice["message"]:
                            text += choice["message"]["content"]
                return True, f"응답: {text[:100]}..." if len(text) > 100 else f"응답: {text}", latency
            except:
                return True, f"상태 코드: {response.status_code}", latency
        else:
            try:
                error = response.json()
                return False, f"에러: {error.get('detail', response.text[:200])}", latency
            except:
                return False, f"상태 코드: {response.status_code}, {response.text[:200]}", latency
                
    except httpx.TimeoutException:
        return False, "타임아웃 (120초 초과)", 0
    except Exception as e:
        return False, f"연결 오류: {str(e)}", 0

async def check_server_health() -> bool:
    """서버 상태 확인"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{PROXY_URL}/health")
            if response.status_code == 200:
                data = response.json()
                print(f"서버 상태: {data.get('status', 'unknown')}")
                print(f"Vertex AI 프로젝트: {data.get('vertex_projects', [])}")
                print(f"Copilot 설정됨: {data.get('copilot_configured', False)}")
                return True
    except Exception as e:
        print(f"서버 연결 실패: {e}")
    return False

async def run_tests():
    """모든 테스트 실행"""
    print_header("LLM 프록시 서버 연결 테스트")
    print(f"프록시 URL: {PROXY_URL}")
    
    # 서버 상태 확인
    print_header("1. 서버 상태 확인")
    if not await check_server_health():
        print("\n❌ 서버가 실행 중이지 않습니다.")
        print("   먼저 proxy_server.py를 실행해주세요:")
        print("   python proxy_server.py")
        return
    
    # Vertex AI 테스트
    print_header("2. Vertex AI 모델 테스트")
    vertex_results = []
    for model in VERTEX_MODELS:
        print(f"\n테스트 중: {model}...")
        success, message, latency = await test_vertex_model(model)
        vertex_results.append((model, success))
        print_result(model, success, message, latency)
    
    # Copilot 테스트
    print_header("3. GitHub Copilot 모델 테스트")
    copilot_results = []
    for model in COPILOT_MODELS:
        print(f"\n테스트 중: {model}...")
        success, message, latency = await test_copilot_model(model)
        copilot_results.append((model, success))
        print_result(model, success, message, latency)
    
    # 결과 요약
    print_header("4. 테스트 결과 요약")
    
    print("\n[Vertex AI]")
    for model, success in vertex_results:
        status = "✅" if success else "❌"
        print(f"  {status} {model}")
    
    print("\n[GitHub Copilot]")
    for model, success in copilot_results:
        status = "✅" if success else "❌"
        print(f"  {status} {model}")
    
    total_success = sum(1 for _, s in vertex_results + copilot_results if s)
    total_tests = len(vertex_results) + len(copilot_results)
    
    print(f"\n총 {total_tests}개 중 {total_success}개 성공")
    
    if total_success == total_tests:
        print("\n🎉 모든 테스트가 성공했습니다!")
    else:
        print("\n⚠️ 일부 테스트가 실패했습니다. 로그를 확인해주세요.")

def main():
    import sys
    import io
    # 윈도우 콘솔 인코딩 설정
    if sys.platform == 'win32':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    
    print("=" * 60)
    print("  LLM 프록시 서버 자동 연결 테스트")
    print("=" * 60)
    print("\n이 스크립트는 다음 모델들의 연결을 테스트합니다:")
    print("  - Vertex AI: gemini-3.1-pro-preview, gemini-3-flash-preview")
    print("  - Copilot: gpt-4.1")
    print("\n테스트 전 proxy_server.py가 실행 중인지 확인하세요.")
    print("-" * 60)
    
    print("\n테스트를 시작합니다...")
    
    asyncio.run(run_tests())

if __name__ == "__main__":
    main()
