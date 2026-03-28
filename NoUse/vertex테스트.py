import json
from google.oauth2 import service_account
import vertexai
from vertexai.generative_models import GenerativeModel

# 첨부해주신 서비스 계정 키 파일 경로
key_path = "./key/rp5project-686cf88f7b72.json"

# 1. JSON 파일에서 프로젝트 ID 자동 추출
with open(key_path, 'r', encoding='utf-8') as f:
    key_info = json.load(f)
project_id = key_info.get("project_id")

# 2. 서비스 계정 인증 정보 로드
credentials = service_account.Credentials.from_service_account_file(key_path)

# 3. Vertex AI 초기화 (요청하신 global 리전 설정)
vertexai.init(project=project_id, location="global", credentials=credentials)

# 4. 사용할 모델 로드 (가장 빠른 최신 모델인 Gemini 1.5 Flash 사용)
model = GenerativeModel("gemini-3-flash-preview")

# 5. 원하는 내용(프롬프트) 전송
prompt = "여기에 구글 버텍스에게 보낼 내용을 입력하세요."
response = model.generate_content(prompt)

# 6. 응답 결과 출력
print("Vertex AI 응답 결과:")
print(response.text)
