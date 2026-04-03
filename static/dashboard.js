// 대시보드 상태
let records = [];
let stats = {};
let pending = {};
let pricing = {}; // 가격 정보
let expandedItems = new Set(); // 펼쳐진 항목 추적
let pendingExpandedItems = new Set(); // 대기 요청 펼쳐진 항목 추적
let lastRecordsLength = 0; // 이전 기록 개수
let lastStatsJson = ""; // 이전 통계 JSON
let quotaIntervalId = null; // 승수 조회 인터벌 ID
let tavilyQuotaIntervalId = null; // Tavily 전용 승수 조회 인터벌 ID

// 모델 이름 매핑 (stats 모델명 -> price 모델명)
const MODEL_NAME_MAP = {
    'vertex/gemini-3-flash-preview': 'Gemini 3.0 Flash',
    'vertex/gemini-3.1-pro-preview': 'Gemini 3.1 Pro',
    'vertex/gemini-3-flash': 'Gemini 3.0 Flash',
    'vertex/gemini-3.1-pro': 'Gemini 3.1 Pro',
    'vertex/gemini-2.5-pro': 'Gemini 3.1 Pro',
    'vertex/gemini-2.5-flash': 'Gemini 3.0 Flash',
    'copilot/claude-opus-4.5': 'Claude 4.5 Opus',
    'copilot/claude-opus-4-6': 'Claude 4.6 Opus',
    'zai/glm-5.1': 'GLM 5.1'
};

// 차트 색상 팔레트 (모델별)
const CHART_COLORS = ['#00d4ff', '#4caf50', '#ff9800', '#e91e63', '#9c27b0', '#ffeb3b', '#795548', '#607d8b'];

// 가격 정보 가져오기
async function fetchPricing() {
    try {
        const response = await fetch('/api/prices');
        pricing = await response.json();
    } catch (e) {
        console.error('가격 정보 로드 실패:', e);
        pricing = { models: [] };
    }
}

// 비용 계산
function calculateCost(modelName, inputTokens, outputTokens) {
    const priceModelName = MODEL_NAME_MAP[modelName];
    if (!priceModelName) {
        return null; // 매핑된 가격 정보가 없음
    }

    const modelPricing = pricing.models?.find(m => m.model === priceModelName);
    if (!modelPricing || !modelPricing.pricing || modelPricing.pricing.length === 0) {
        return null;
    }

    // 첫 번째 가격 정보 사용 (기본 가격)
    const price = modelPricing.pricing[0];
    const inputCost = (inputTokens / 1000000) * price.input_price_usd_per_1M_tokens;
    const outputCost = (outputTokens / 1000000) * price.output_price_usd_per_1M_tokens;
    const totalCost = inputCost + outputCost;

    return {
        inputCost,
        outputCost,
        totalCost
    };
}

// 원형 그래프 그리기 (모델별 분포)
function drawPieChart(canvas, segments, centerLabel) {
    const ctx = canvas.getContext('2d');
    const centerX = canvas.width / 2;
    const centerY = canvas.height / 2;
    const radius = Math.min(centerX, centerY) - 5;

    // 캔버스 초기화
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const total = segments.reduce((sum, s) => sum + s.value, 0);
    if (total === 0) {
        ctx.beginPath();
        ctx.arc(centerX, centerY, radius, 0, 2 * Math.PI);
        ctx.fillStyle = '#333';
        ctx.fill();
        ctx.fillStyle = '#888';
        ctx.font = '10px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText('데이터 없음', centerX, centerY);
        return;
    }

    let startAngle = -Math.PI / 2;
    const filteredSegments = segments.filter(s => s.value > 0);

    filteredSegments.forEach(segment => {
        const sliceAngle = (segment.value / total) * 2 * Math.PI;

        ctx.beginPath();
        ctx.moveTo(centerX, centerY);
        ctx.arc(centerX, centerY, radius, startAngle, startAngle + sliceAngle);
        ctx.closePath();
        ctx.fillStyle = segment.color;
        ctx.fill();

        // 슬라이스가 5% 이상이면 비율 표시
        const ratio = segment.value / total;
        if (ratio >= 0.05) {
            const midAngle = startAngle + sliceAngle / 2;
            const labelRadius = radius * 0.65;
            const labelX = centerX + Math.cos(midAngle) * labelRadius;
            const labelY = centerY + Math.sin(midAngle) * labelRadius;

            ctx.fillStyle = '#fff';
            ctx.font = 'bold 10px sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(`${Math.round(ratio * 100)}%`, labelX, labelY);
        }

        startAngle += sliceAngle;
    });

    // 중앙 라벨
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 12px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(centerLabel, centerX, centerY);
}

// 데이터 가져오기
async function fetchData() {
    try {
        // 최초 실행 시 가격 정보 로드
        if (!pricing.models || pricing.models.length === 0) {
            await fetchPricing();
        }

        const [recordsRes, statsRes, pendingRes] = await Promise.all([
            fetch('/api/records'),
            fetch('/api/stats'),
            fetch('/api/pending')
        ]);

        const newRecords = await recordsRes.json();
        const newStats = await statsRes.json();
        pending = await pendingRes.json();

        const newStatsJson = JSON.stringify(newStats);
        const statsChanged = newStatsJson !== lastStatsJson;
        lastStatsJson = newStatsJson;
        stats = newStats;

        // 기록이 변경된 경우에만 렌더링
        if (newRecords.length !== lastRecordsLength || JSON.stringify(newRecords) !== JSON.stringify(records)) {
            records = newRecords;
            lastRecordsLength = records.length;
            renderAll();
            restoreExpandedState(); // 펼쳐진 상태 복원
        } else if (statsChanged) {
            // 통계만 변경된 경우
            renderStats();
        } else {
            // 기록이 같으면 pending만 업데이트
            renderPending();
        }
    } catch (e) {
        console.error('데이터 로드 실패:', e);
    }
}

// 전체 렌더링
function renderAll() {
    renderPending();
    renderStats();
    renderRecords();
}

// 토큰 분포 원형 그래프 렌더링 (입력/출력 각각 1개씩)
function renderTokenCharts() {
    const container = document.getElementById('token-charts-container');
    const statItems = Object.entries(stats);

    if (statItems.length === 0) {
        container.innerHTML = '<p class="no-records">아직 기록된 사용량이 없습니다</p>';
        return;
    }

    // 모델별 색상 할당
    const modelsWithColors = statItems.map(([model, data], i) => ({
        model,
        color: CHART_COLORS[i % CHART_COLORS.length],
        inputTokens: data.total_input_tokens,
        outputTokens: data.total_output_tokens
    }));

    // 입력/출력 세그먼트 생성
    const inputSegments = modelsWithColors.map(m => ({
        value: m.inputTokens,
        color: m.color,
        label: m.model
    }));
    const outputSegments = modelsWithColors.map(m => ({
        value: m.outputTokens,
        color: m.color,
        label: m.model
    }));

    const totalInput = inputSegments.reduce((sum, s) => sum + s.value, 0);
    const totalOutput = outputSegments.reduce((sum, s) => sum + s.value, 0);

    container.innerHTML = `
        <div class="token-chart-item input-chart">
            <div class="chart-title">📥 입력 토큰 분포</div>
            <div class="chart-total">${formatNumber(totalInput)} 토큰</div>
            <div class="chart-canvas-container">
                <canvas id="chart-input" width="140" height="140"></canvas>
            </div>
            <div class="chart-legend">
                ${modelsWithColors.filter(m => m.inputTokens > 0).map(m => `
                    <div class="chart-legend-item">
                        <div class="chart-legend-color" style="background: ${m.color};"></div>
                        <span>${m.model}: ${formatNumber(m.inputTokens)}</span>
                    </div>
                `).join('')}
            </div>
        </div>
        <div class="token-chart-item output-chart">
            <div class="chart-title">📤 출력 토큰 분포</div>
            <div class="chart-total">${formatNumber(totalOutput)} 토큰</div>
            <div class="chart-canvas-container">
                <canvas id="chart-output" width="140" height="140"></canvas>
            </div>
            <div class="chart-legend">
                ${modelsWithColors.filter(m => m.outputTokens > 0).map(m => `
                    <div class="chart-legend-item">
                        <div class="chart-legend-color" style="background: ${m.color};"></div>
                        <span>${m.model}: ${formatNumber(m.outputTokens)}</span>
                    </div>
                `).join('')}
            </div>
        </div>
    `;

    // 차트 그리기
    const inputCanvas = document.getElementById('chart-input');
    const outputCanvas = document.getElementById('chart-output');
    if (inputCanvas) drawPieChart(inputCanvas, inputSegments, '입력');
    if (outputCanvas) drawPieChart(outputCanvas, outputSegments, '출력');
}

// 대기 중인 요청 렌더링
function renderPending() {
    const container = document.getElementById('pending-list');
    const pendingItems = Object.entries(pending);

    // 사라진 pending 항목의 확장 상태 정리
    const currentPendingIds = new Set(pendingItems.map(([id]) => 'pending-json-' + id.replace(/[^a-zA-Z0-9_-]/g, '_')));
    expandedItems.forEach(id => {
        if (id.startsWith('pending-json-') && !currentPendingIds.has(id)) {
            expandedItems.delete(id);
        }
    });

    if (pendingItems.length === 0) {
        container.innerHTML = '<p class="no-records">대기 중인 요청이 없습니다</p>';
        return;
    }
    
    container.innerHTML = pendingItems.map(([id, data]) => {
        const elapsed = Math.floor((Date.now() / 1000) - data.start_time);
        const showRetryButton = elapsed >= 50; // 50초 초과 시 재시도 버튼 표시

        const retryButton = showRetryButton
            ? `<button class="btn-retry" onclick="retryRequest('${id}')">🔄 재시도</button>`
            : '';

        const cancelButton = `<button class="btn-cancel-request" onclick="cancelRequest('${id}')">✖ 취소</button>`;

        const inputTokens = data.input_tokens != null ? data.input_tokens.toLocaleString() : '-';
        const requestId = id.replace(/[^a-zA-Z0-9_-]/g, '_');

        return `
            <div class="pending-item">
                <div class="pending-info">
                    <div class="pending-header">
                        <strong>${data.model}</strong> (${data.type})
                        <div style="display: flex; gap: 6px; margin-top: 6px;">
                            <button class="collapsible pending-btn-json" data-toggle="pending-json-${requestId}">📝 JSON</button>
                            <button class="collapsible btn-viewer pending-btn-viewer" data-viewer-pending="${requestId}">🔍 뷰어</button>
                        </div>
                    </div>
                    <div id="pending-json-${requestId}" class="content-box">${formatJsonString(data.request_preview || '요청 중...')}</div>
                </div>
                <div style="display: flex; align-items: center; gap: 10px;">
                    <span>📝 ${inputTokens} 토큰</span>
                    <span>⏱️ ${elapsed}초</span>
                    ${data.retry_count ? `<span style="color: #ff6b6b; font-size: 0.9em; margin-left: 5px;">[재시도 ${data.retry_count}회]</span>` : ''}
                    ${retryButton}
                    ${cancelButton}
                    <div class="spinner"></div>
                </div>
            </div>
        `;
    }).join('');

    // JSON 토글 버튼 이벤트
    container.querySelectorAll('.pending-btn-json').forEach(btn => {
        btn.addEventListener('click', function() {
            const targetId = this.getAttribute('data-toggle');
            toggleContent(targetId);
        });
    });

    // 뷰어 버튼 이벤트
    container.querySelectorAll('.pending-btn-viewer').forEach(btn => {
        btn.addEventListener('click', function() {
            const requestId = this.getAttribute('data-viewer-pending');
            const data = pending[Object.keys(pending).find(k => k.replace(/[^a-zA-Z0-9_-]/g, '_') === requestId)];
            if (data && data.request_preview) {
                openPromptViewerWithData(data.request_preview);
            }
        });
    });

    // 펼쳐진 상태 복원
    expandedItems.forEach(id => {
        if (id.startsWith('pending-json-')) {
            const element = document.getElementById(id);
            if (element && !element.classList.contains('show')) {
                element.classList.add('show');
            }
        }
    });
}

// 재시도 요청
async function retryRequest(requestId) {
    try {
        const response = await fetch(`/api/retry/${requestId}`, {
            method: 'POST'
        });
        
        if (response.ok) {
            const result = await response.json();
            console.log(`재시도 시작: ${result.model} (${result.type})`);
            // 데이터 새로고침
            fetchData();
        } else {
            const error = await response.json();
            alert(`재시도 실패: ${error.detail || '알 수 없는 오류'}`);
        }
    } catch (e) {
        console.error('재시도 요청 실패:', e);
        alert('재시도 요청에 실패했습니다.');
    }
}

// 요청 취소
async function cancelRequest(requestId) {
    if (!confirm('이 요청을 취소하시겠습니까?')) {
        return;
    }
    
    try {
        const response = await fetch(`/api/cancel/${requestId}`, {
            method: 'POST'
        });
        
        if (response.ok) {
            const result = await response.json();
            console.log(`요청 취소됨: ${result.model} (${result.type})`);
            // 데이터 새로고침
            fetchData();
        } else {
            const error = await response.json();
            alert(`취소 실패: ${error.detail || '알 수 없는 오류'}`);
        }
    } catch (e) {
        console.error('취소 요청 실패:', e);
        alert('취소 요청에 실패했습니다.');
    }
}

// 통계 렌더링
function renderStats() {
    const container = document.getElementById('stats-grid');
    const statItems = Object.entries(stats);

    if (statItems.length === 0) {
        container.innerHTML = '<p class="no-records">아직 기록된 사용량이 없습니다</p>';
        // 원형 그래프 컨테이너도 초기화
        renderTokenCharts();
        return;
    }

    // 원형 그래프 렌더링
    renderTokenCharts();

    // 통계 카드 렌더링 (비용 포함)
    container.innerHTML = statItems.map(([model, data]) => {
        const cost = calculateCost(model, data.total_input_tokens, data.total_output_tokens);
        const costHtml = cost
            ? `<div class="stat-cost">
                   <span class="stat-cost-label">예상 비용:</span> $${cost.totalCost.toFixed(4)}
               </div>`
            : '';

        return `
        <div class="stat-card">
            <div class="model-name">${model}</div>
            <div class="stat-value">${data.total_calls}회</div>
            <div class="stat-label">
                입력: ${formatNumber(data.total_input_tokens)} | 출력: ${formatNumber(data.total_output_tokens)} 토큰
                <br>평균: ${data.total_calls > 0 ? (data.total_latency / data.total_calls).toFixed(2) : 0}초
            </div>
            ${costHtml}
        </div>
    `}).join('');
}

// 숫자 포맷팅 (천 단위 콤마)
function formatNumber(num) {
    return num.toLocaleString();
}

// 기록 렌더링
function renderRecords() {
    const container = document.getElementById('records-list');
    
    if (records.length === 0) {
        container.innerHTML = '<p class="no-records">아직 기록된 요청이 없습니다</p>';
        return;
    }
    
    container.innerHTML = records.map((record, index) => {
        // 캐싱 상태 표시
        let cacheBadge = '';
        if (record.cached === true) {
            cacheBadge = '<span class="cache-badge cached" title="암시적 캐싱 적용됨">📦 캐싱됨</span>';
        } else if (record.type === 'vertex') {
            cacheBadge = '<span class="cache-badge not-cached" title="캐싱 미적용">📦 캐싱 없음</span>';
        }

        // 폴백 상태 표시 (항상 표시)
        let fallbackBadge = '';
        if (record.fallback === true) {
            fallbackBadge = '<span class="cache-badge" style="background: rgba(255, 152, 0, 0.2); color: #ff9800; border: 1px solid rgba(255, 152, 0, 0.4);" title="검열 차단으로 폴백 모델 사용">🛡️ 폴백</span>';
        } else {
            fallbackBadge = '<span class="cache-badge" style="background: rgba(158, 158, 158, 0.15); color: #777; border: 1px solid rgba(158, 158, 158, 0.25);" title="폴백 없음">🛡️ 폴백 없음</span>';
        }

        // 재시도 횟수 표시 (항상 표시)
        const retryCount = record.retry_count || 0;
        let retryBadge = '';
        if (retryCount > 0) {
            retryBadge = `<span class="cache-badge" style="background: rgba(233, 30, 99, 0.2); color: #e91e63; border: 1px solid rgba(233, 30, 99, 0.4);" title="자동 재시도 ${retryCount}회 발생">🔄 재시도 ${retryCount}회</span>`;
        } else {
            retryBadge = '<span class="cache-badge" style="background: rgba(158, 158, 158, 0.15); color: #777; border: 1px solid rgba(158, 158, 158, 0.25);" title="재시도 없음">🔄 재시도 0회</span>';
        }
        
        return `
        <div class="record-card ${record.type}">
            <div class="record-header">
                <span class="record-model">${record.model}</span>
                <span class="record-time">${record.timestamp}</span>
                ${cacheBadge}
                ${retryBadge}
                ${fallbackBadge}
            </div>
            <div class="record-stats">
                <div class="token-info">
                    <div class="token-box">
                        <div class="label">입력 토큰</div>
                        <div class="value">${record.input_tokens}</div>
                    </div>
                    <div class="token-box">
                        <div class="label">출력 토큰</div>
                        <div class="value">${record.output_tokens}</div>
                    </div>
                    <div class="token-box">
                        <div class="label">응답 시간</div>
                        <div class="value latency">${record.latency}초</div>
                    </div>
                    <div class="token-box">
                        <div class="label">상태</div>
                        <div class="value">${record.status}</div>
                    </div>
                </div>
            </div>
            <div>
                <button class="collapsible" data-toggle="request-${index}">📝 요청 보기</button>
                <button class="collapsible btn-viewer" data-viewer="request" data-index="${index}">🔍 뷰어로 요청 보기</button>
                <button class="collapsible" data-toggle="response-${index}">💬 응답 보기 (Full JSON)</button>
                <button class="collapsible btn-viewer" data-viewer="response" data-index="${index}">🔍 뷰어로 응답 보기</button>
            </div>
            <div id="request-${index}" class="content-box">${formatJsonString(record.request)}</div>
            <div id="response-${index}" class="content-box">${formatJsonString(record.response)}</div>
        </div>
        `;
    }).join('');

    // 동적으로 생성된 버튼에 이벤트 리스너 추가
    container.querySelectorAll('.collapsible:not(.btn-viewer)').forEach(btn => {
        btn.addEventListener('click', function() {
            const targetId = this.getAttribute('data-toggle');
            toggleContent(targetId);
        });
    });

    // 뷰어 버튼 이벤트
    container.querySelectorAll('.btn-viewer').forEach(btn => {
        btn.addEventListener('click', function() {
            const type = this.getAttribute('data-viewer');
            const idx = parseInt(this.getAttribute('data-index'));
            const record = records[idx];
            const data = type === 'request' ? record.request : record.response;
            openPromptViewerWithData(data);
        });
    });
}

// 콘텐츠 토글
function toggleContent(id) {
    const element = document.getElementById(id);
    if (!element) return;
    
    element.classList.toggle('show');
    
    // 펼쳐진 상태 추적
    if (element.classList.contains('show')) {
        expandedItems.add(id);
    } else {
        expandedItems.delete(id);
    }
}

// 펼쳐진 상태 복원
function restoreExpandedState() {
    expandedItems.forEach(id => {
        const element = document.getElementById(id);
        if (element && !element.classList.contains('show')) {
            element.classList.add('show');
        }
    });
}

// HTML 이스케이프
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// JSON 문자열을 예쁘게 포맷팅
function formatJsonString(text) {
    if (!text) return '없음';
    
    // 이미 JSON 문자열인 경우 파싱하여 다시 포맷팅
    try {
        const parsed = JSON.parse(text);
        return escapeHtml(JSON.stringify(parsed, null, 2));
    } catch (e) {
        // JSON 파싱 실패 시 일반 텍스트로 처리
        return escapeHtml(text);
    }
}

// 통계 초기화
async function resetStats() {
    if (confirm('모델별 통계를 초기화하시겠습니까? (기록은 유지됩니다)')) {
        try {
            await fetch('/api/stats/reset', { method: 'POST' });
            fetchData();
        } catch (e) {
            console.error('초기화 실패:', e);
        }
    }
}

// 설정 모달 열기
async function openSettingsModal() {
    const modal = document.getElementById('settings-modal');
    modal.style.display = 'block';
    
    // 현재 설정 로드
    try {
        const response = await fetch('/api/settings');
        const settings = await response.json();
        // Copilot 설정
        document.getElementById('retry-count').value = settings.retry_count;
        document.getElementById('retry-delay').value = settings.retry_delay;
        // Vertex 설정
        document.getElementById('vertex-retry-count').value = settings.vertex_retry_count;
        document.getElementById('vertex-retry-delay').value = settings.vertex_retry_delay;
        document.getElementById('zai-retry-count').value = settings.zai_retry_count;
        document.getElementById('zai-retry-delay').value = settings.zai_retry_delay;
        // ZAI thinking 설정
        document.getElementById('zai-thinking').value = settings.zai_thinking || 'disabled';
        document.getElementById('zai-thinking-budget').value = settings.zai_thinking_budget || 8000;
        document.getElementById('zai-thinking-budget-item').style.display =
            settings.zai_thinking === 'enabled' ? 'block' : 'none';
        // 폴백 모델 설정
        document.getElementById('fallback-model').value = settings.fallback_model || '';
        // 코파일럿 승수 조회 설정
        document.getElementById('copilot-quota-enabled').value = String(settings.copilot_quota_enabled || false);
        // ZAI 승수 조회 설정
        document.getElementById('zai-quota-enabled').value = String(settings.zai_quota_enabled || false);
        // Tavily 승수 조회 설정
        document.getElementById('tavily-quota-enabled').value = String(settings.tavily_quota_enabled || false);
    } catch (e) {
        console.error('설정 로드 실패:', e);
    }
}

// 설정 모달 닫기
function closeSettingsModal() {
    const modal = document.getElementById('settings-modal');
    modal.style.display = 'none';
}

// 설정 저장
async function saveSettings() {
    // Copilot 설정
    const retryCount = parseInt(document.getElementById('retry-count').value);
    const retryDelay = parseInt(document.getElementById('retry-delay').value);
    
    // Vertex 설정
    const vertexRetryCount = parseInt(document.getElementById('vertex-retry-count').value);
    const vertexRetryDelay = parseInt(document.getElementById('vertex-retry-delay').value);

    // ZAI 설정
    const zaiRetryCount = parseInt(document.getElementById('zai-retry-count').value);
    const zaiRetryDelay = parseInt(document.getElementById('zai-retry-delay').value);
    const zaiThinking = document.getElementById('zai-thinking').value;
    const zaiThinkingBudget = parseInt(document.getElementById('zai-thinking-budget').value);

    // 폴백 모델 설정
    const fallbackModel = document.getElementById('fallback-model').value.trim();

    // 코파일럿 승수 조회 설정
    const copilotQuotaEnabled = document.getElementById('copilot-quota-enabled').value === 'true';

    // ZAI 승수 조회 설정
    const zaiQuotaEnabled = document.getElementById('zai-quota-enabled').value === 'true';

    // Tavily 승수 조회 설정
    const tavilyQuotaEnabled = document.getElementById('tavily-quota-enabled').value === 'true';

    // Copilot 유효성 검사
    if (isNaN(retryCount) || retryCount < 0 || retryCount > 10) {
        alert('Copilot 재시도 횟수는 0-10 사이의 숫자여야 합니다.');
        return;
    }
    
    if (isNaN(retryDelay) || retryDelay < 0 || retryDelay > 60) {
        alert('Copilot 재시도 간격은 0-60 사이의 숫자여야 합니다.');
        return;
    }
    
    // Vertex 유효성 검사
    if (isNaN(vertexRetryCount) || vertexRetryCount < 0 || vertexRetryCount > 10) {
        alert('Vertex 재시도 횟수는 0-10 사이의 숫자여야 합니다.');
        return;
    }
    
    if (isNaN(vertexRetryDelay) || vertexRetryDelay < 0 || vertexRetryDelay > 600) {
        alert('Vertex 재시도 간격은 0-600 사이의 숫자여야 합니다.');
        return;
    }

    // ZAI 유효성 검사
    if (isNaN(zaiRetryCount) || zaiRetryCount < 0 || zaiRetryCount > 10) {
        alert('ZAI 재시도 횟수는 0-10 사이의 숫자여야 합니다.');
        return;
    }

    if (isNaN(zaiRetryDelay) || zaiRetryDelay < 0 || zaiRetryDelay > 600) {
        alert('ZAI 재시도 간격은 0-600 사이의 숫자여야 합니다.');
        return;
    }
    
    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                retry_count: retryCount,
                retry_delay: retryDelay,
                vertex_retry_count: vertexRetryCount,
                vertex_retry_delay: vertexRetryDelay,
                zai_retry_count: zaiRetryCount,
                zai_retry_delay: zaiRetryDelay,
                zai_thinking: zaiThinking,
                zai_thinking_budget: zaiThinkingBudget,
                fallback_model: fallbackModel,
                copilot_quota_enabled: copilotQuotaEnabled,
                zai_quota_enabled: zaiQuotaEnabled,
                tavily_quota_enabled: tavilyQuotaEnabled
            })
        });
        
        if (response.ok) {
            alert('설정이 저장되었습니다.');
            closeSettingsModal();
            // 승수 조회 인터벌 갱신
            startQuotaPolling();
        } else {
            alert('설정 저장에 실패했습니다.');
        }
    } catch (e) {
        console.error('설정 저장 실패:', e);
        alert('설정 저장에 실패했습니다.');
    }
}

// DOM 로드 완료 시 이벤트 바인딩
document.addEventListener('DOMContentLoaded', function() {
    // 재시도 설정 버튼
    const settingsBtn = document.querySelector('.btn-settings');
    if (settingsBtn) {
        settingsBtn.addEventListener('click', openSettingsModal);
        settingsBtn.removeAttribute('onclick');
    }

    // 통계 초기화 버튼
    const resetBtn = document.querySelector('.btn-reset');
    if (resetBtn) {
        resetBtn.addEventListener('click', resetStats);
        resetBtn.removeAttribute('onclick');
    }

    // 프롬프트 뷰어 버튼
    const promptViewerBtn = document.querySelector('.btn-prompt-viewer');
    if (promptViewerBtn) {
        promptViewerBtn.addEventListener('click', openPromptViewerModal);
        promptViewerBtn.removeAttribute('onclick');
    }

    // 모달 닫기 버튼 (X) - 각 모달의 close 버튼에 해당 모달 닫기 연결
    document.querySelectorAll('.close').forEach(btn => {
        const modal = btn.closest('.modal');
        if (modal) {
            btn.addEventListener('click', () => { modal.style.display = 'none'; });
            btn.removeAttribute('onclick');
        }
    });

    // 취소 버튼
    const cancelBtn = document.querySelector('.btn-cancel');
    if (cancelBtn) {
        cancelBtn.addEventListener('click', closeSettingsModal);
        cancelBtn.removeAttribute('onclick');
    }

    // 저장 버튼
    const saveBtn = document.querySelector('.btn-save');
    if (saveBtn) {
        saveBtn.addEventListener('click', saveSettings);
        saveBtn.removeAttribute('onclick');
    }

    // 모달 외부 클릭 시 닫기
    window.addEventListener('click', function(event) {
        const settingsModal = document.getElementById('settings-modal');
        const promptModal = document.getElementById('prompt-viewer-modal');
        const quotaModal = document.getElementById('quota-modal');
        if (event.target == settingsModal) {
            settingsModal.style.display = 'none';
        }
        if (event.target == promptModal) {
            promptModal.style.display = 'none';
        }
        if (event.target == quotaModal) {
            quotaModal.style.display = 'none';
        }
    });

    // 프롬프트 뷰어 단축키 (Ctrl+Enter로 변환)
    document.getElementById('prompt-input').addEventListener('keydown', function(e) {
        if (e.ctrlKey && e.key === 'Enter') {
            e.preventDefault();
            convertPrompt();
        }
    });

    // ZAI thinking 드롭다운 변경 시 예산 필드 토글
    document.getElementById('zai-thinking').addEventListener('change', function() {
        document.getElementById('zai-thinking-budget-item').style.display =
            this.value === 'enabled_with_budget' ? 'block' : 'none';
    });

    // 초기 로드 및 주기적 갱신
    fetchData();
    setInterval(fetchData, 1000);

    // 승수 조회 버튼
    const quotaBtn = document.querySelector('.btn-quota');
    if (quotaBtn) {
        quotaBtn.addEventListener('click', openQuotaModal);
        quotaBtn.removeAttribute('onclick');
    }

    // 승수 조회 폴링 시작
    startQuotaPolling();
});

// ============ 프롬프트 뷰어 ============

// 프롬프트 뷰어 모달 열기
function openPromptViewerModal() {
    const modal = document.getElementById('prompt-viewer-modal');
    modal.style.display = 'block';
}

// 프롬프트 뷰어 모달 닫기
function closePromptViewerModal() {
    const modal = document.getElementById('prompt-viewer-modal');
    modal.style.display = 'none';
}

// 데이터를 받아 프롬프트 뷰어 열기 (카드에서 호출)
function openPromptViewerWithData(data) {
    const text = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
    document.getElementById('prompt-input').value = text;
    convertPrompt();
    openPromptViewerModal();
}

// 프롬프트 변환 (\n -> 줄바꿈)
function convertPrompt() {
    const input = document.getElementById('prompt-input').value.trim();
    const outputBox = document.getElementById('prompt-output');

    if (!input) {
        outputBox.innerHTML = '<span class="prompt-error">JSON 데이터를 입력해주세요.</span>';
        return;
    }

    try {
        const parsed = JSON.parse(input);
        let html = '';

        // 메타데이터 표시
        const metaParts = [];
        if (parsed.model) metaParts.push('Model: ' + parsed.model);
        if (parsed.temperature !== undefined) metaParts.push('Temperature: ' + parsed.temperature);
        if (parsed.stream !== undefined) metaParts.push('Stream: ' + parsed.stream);

        if (metaParts.length > 0) {
            html += '<div class="prompt-meta">' + escapeHtml(metaParts.join(' | ')) + '</div>';
        }

        // messages 배열 처리
        if (parsed.messages && Array.isArray(parsed.messages)) {
            parsed.messages.forEach((msg, idx) => {
                const role = (msg.role || 'unknown').toUpperCase();
                const roleClass = msg.role || 'unknown';
                let content = msg.content || '';
                // content가 배열인 경우 (멀티모달 형식) 텍스트 추출
                if (Array.isArray(content)) {
                    content = content.map(block => {
                        if (typeof block === 'string') return block;
                        if (block.type === 'text') return block.text || '';
                        return JSON.stringify(block);
                    }).join('\n');
                }

                // \n을 실제 줄바꿈으로 변환
                const formatted = content.replace(/\\n/g, '\n');

                html += '<div class="prompt-message">';
                html += '<div class="prompt-role ' + roleClass + '">[' + role + '] (메시지 ' + (idx + 1) + '/' + parsed.messages.length + ')</div>';
                html += '<div class="prompt-content">' + escapeHtml(formatted) + '</div>';
                html += '</div>';
            });

            html += '<div class="prompt-summary">총 ' + parsed.messages.length + '개 메시지</div>';
        } else {
            // messages가 없는 경우 전체 JSON을 줄바꿈 변환해서 표시
            const formatted = JSON.stringify(parsed, null, 2).replace(/\\n/g, '\n');
            html += '<div class="prompt-content">' + escapeHtml(formatted) + '</div>';
        }

        outputBox.innerHTML = html;
    } catch (e) {
        // JSON 파싱 실패 시 일반 텍스트로 \n 변환만 처리
        const formatted = input.replace(/\\n/g, '\n');
        outputBox.innerHTML = '<div class="prompt-error">JSON 파싱 실패 (' + escapeHtml(e.message) + ')</div>'
            + '<div class="prompt-content">' + escapeHtml(formatted) + '</div>';
    }
}

// 프롬프트 입력/출력 지우기
function clearPrompt() {
    document.getElementById('prompt-input').value = '';
    document.getElementById('prompt-output').innerHTML = '<span class="prompt-output-placeholder">변환된 결과가 여기에 표시됩니다</span>';
}

// ============ 승수 조회 ============

// 승수 조회 모달 열기
function openQuotaModal() {
    document.getElementById('quota-modal').style.display = 'block';
    fetchAndRenderCopilotQuota();
}

// 승수 조회 모달 닫기
function closeQuotaModal() {
    document.getElementById('quota-modal').style.display = 'none';
}

// 코파일럿 승수 조회 및 렌더링
async function fetchAndRenderCopilotQuota() {
    const display = document.getElementById('copilot-quota-display');
    const statusBadge = document.getElementById('copilot-quota-status');

    try {
        // 설정 확인
        const settingsRes = await fetch('/api/settings');
        const settings = await settingsRes.json();

        if (!settings.copilot_quota_enabled) {
            statusBadge.textContent = '비활성화';
            statusBadge.className = 'quota-status';
            display.innerHTML = '<p class="no-records">설정에서 코파일럿 승수 조회를 활성화하세요.</p>';
            return;
        }

        statusBadge.textContent = '조회 중...';
        statusBadge.className = 'quota-status';

        const res = await fetch('/api/copilot/quota');
        const result = await res.json();

        if (!result.configured) {
            statusBadge.textContent = '오류';
            statusBadge.className = 'quota-status error';
            display.innerHTML = `<p class="no-records">${escapeHtml(result.error || 'API 키가 없습니다.')}</p>`;
            return;
        }

        if (result.message && !result.data) {
            statusBadge.textContent = '활성';
            statusBadge.className = 'quota-status active';
            display.innerHTML = `<p class="no-records">${escapeHtml(result.message)}</p>`;
            return;
        }

        statusBadge.textContent = '활성';
        statusBadge.className = 'quota-status active';

        // 위젯 데이터 저장
        lastCopilotQuotaData = result.data;
        updateWidgetDisplay('copilot');

        // 위젯 데이터 저장
        lastCopilotQuotaData = result.data;
        updateWidgetDisplay('copilot');

        // copilot_internal/user 응답 파싱
        const quotaData = result.data;
        let html = '';

        // 플랜 정보
        if (quotaData.copilot_plan) {
            html += `<div class="quota-item">
                <span class="quota-item-label">플랜</span>
                <span class="quota-item-value">${escapeHtml(quotaData.copilot_plan)}</span>
            </div>`;
        }
        if (quotaData.access_type_sku) {
            html += `<div class="quota-item">
                <span class="quota-item-label">유형</span>
                <span class="quota-item-value">${escapeHtml(quotaData.access_type_sku)}</span>
            </div>`;
        }

        // Premium Interactions
        if (quotaData.quota_snapshots) {
            const qs = quotaData.quota_snapshots;

            if (qs.premium_interactions) {
                const pi = qs.premium_interactions;
                const pct = pi.percent_remaining || (pi.entitlement > 0 ? (pi.remaining / pi.entitlement * 100) : 0);
                const usedPct = (100 - pct).toFixed(1);
                let valueClass = 'quota-item-value';
                if (pct <= 20) valueClass += ' low';
                else if (pct >= 80) valueClass += ' full';

                html += `<div class="quota-item" style="margin-top:10px; padding-top:8px; border-top:1px solid rgba(255,255,255,0.05);">
                    <span class="quota-item-label" style="font-weight:bold; color:#ddd;">Premium Requests</span>
                    <span class="${valueClass}">${pi.remaining} / ${pi.entitlement}</span>
                </div>`;
                html += `<div class="quota-item">
                    <span class="quota-item-label">사용량</span>
                    <span class="quota-item-value">${usedPct}% 사용 (${pi.entitlement - pi.remaining}건)</span>
                </div>`;
            }

            if (qs.chat && !qs.chat.unlimited) {
                html += `<div class="quota-item" style="margin-top:10px; padding-top:8px; border-top:1px solid rgba(255,255,255,0.05);">
                    <span class="quota-item-label" style="font-weight:bold; color:#ddd;">Chat</span>
                    <span class="quota-item-value">${qs.chat.remaining} / ${qs.chat.entitlement}</span>
                </div>`;
            }

            if (qs.completions && !qs.completions.unlimited) {
                html += `<div class="quota-item" style="margin-top:10px; padding-top:8px; border-top:1px solid rgba(255,255,255,0.05);">
                    <span class="quota-item-label" style="font-weight:bold; color:#ddd;">Completions</span>
                    <span class="quota-item-value">${qs.completions.remaining} / ${qs.completions.entitlement}</span>
                </div>`;
            }
        }

        // 리셋 날짜
        if (quotaData.quota_reset_date) {
            html += `<div class="quota-item">
                <span class="quota-item-label">리셋 날짜</span>
                <span class="quota-item-value">${quotaData.quota_reset_date}</span>
            </div>`;
        }

        if (!html) {
            html = '<p class="no-records">사용량 정보를 찾을 수 없습니다.</p>';
        }

        const now = new Date();
        html += `<div class="quota-updated">마지막 조회: ${now.toLocaleTimeString()}</div>`;

        display.innerHTML = html;

    } catch (e) {
        console.error('승수 조회 실패:', e);
        statusBadge.textContent = '오류';
        statusBadge.className = 'quota-status error';
        display.innerHTML = `<p class="no-records">조회 실패: ${escapeHtml(e.message)}</p>`;
    }
}

// 승수 조회 폴링 시작/중지
async function startQuotaPolling() {
    // 기존 인터벌 중지
    if (quotaIntervalId) {
        clearInterval(quotaIntervalId);
        quotaIntervalId = null;
    }
    if (tavilyQuotaIntervalId) {
        clearInterval(tavilyQuotaIntervalId);
        tavilyQuotaIntervalId = null;
    }

    try {
        const settingsRes = await fetch('/api/settings');
        const settings = await settingsRes.json();

        // Copilot이 활성화된 경우 모달 열 때 조회 + 위젯 30초 갱신
        if (settings.copilot_quota_enabled) {
            fetchAndRenderCopilotQuota();
        }

        // ZAI가 활성화된 경우 30초마다 조회
        if (settings.zai_quota_enabled) {
            fetchAndRenderZaiQuota(); // 즉시 첫 조회
        }

        // Tavily가 활성화된 경우 30초마다 조회
        if (settings.tavily_quota_enabled) {
            fetchAndRenderTavilyQuota(); // 즉시 첫 조회
        }

        // ZAI, Copilot은 30초마다 조회
        if (settings.zai_quota_enabled || settings.copilot_quota_enabled) {
            quotaIntervalId = setInterval(() => {
                if (settings.zai_quota_enabled) fetchAndRenderZaiQuota();
                if (settings.copilot_quota_enabled) fetchAndRenderCopilotQuota();
            }, 30000);
        }

        // Tavily는 Usage API 제한(10분당 10회)으로 70초마다 조회
        if (settings.tavily_quota_enabled) {
            tavilyQuotaIntervalId = setInterval(() => {
                if (settings.tavily_quota_enabled) fetchAndRenderTavilyQuota();
            }, 70000);
        }
    } catch (e) {
        console.error('승수 조회 폴링 시작 실패:', e);
    }
}

// ZAI 승수 조회 및 렌더링
async function fetchAndRenderZaiQuota() {
    const display = document.getElementById('zai-quota-display');
    const statusBadge = document.getElementById('zai-quota-status');

    try {
        // 설정 확인
        const settingsRes = await fetch('/api/settings');
        const settings = await settingsRes.json();

        if (!settings.zai_quota_enabled) {
            statusBadge.textContent = '비활성화';
            statusBadge.className = 'quota-status';
            display.innerHTML = '<p class="no-records">설정에서 ZAI 승수 조회를 활성화하세요.</p>';
            return;
        }

        statusBadge.textContent = '조회 중...';
        statusBadge.className = 'quota-status';

        const res = await fetch('/api/zai/quota');
        const result = await res.json();

        if (!result.configured) {
            statusBadge.textContent = '오류';
            statusBadge.className = 'quota-status error';
            display.innerHTML = `<p class="no-records">${escapeHtml(result.error || 'API 키가 없습니다.')}</p>`;
            return;
        }

        if (result.error) {
            statusBadge.textContent = '오류';
            statusBadge.className = 'quota-status error';
            display.innerHTML = `<p class="no-records">${escapeHtml(result.error)}</p>`;
            return;
        }

        statusBadge.textContent = '활성';
        statusBadge.className = 'quota-status active';

        // 위젯 데이터 저장
        lastZaiQuotaData = result.data;
        updateWidgetDisplay('zai');

        // ZAI 응답 파싱
        const quotaData = result.data;
        let html = '';

        if (quotaData && quotaData.data && quotaData.data.limits) {
            const qData = quotaData.data;
            const level = (qData.level || 'Unknown').toUpperCase();
            html += `<div class="quota-item">
                <span class="quota-item-label">플랜</span>
                <span class="quota-item-value">${escapeHtml(level)}</span>
            </div>`;

            for (const limit of qData.limits) {
                if (limit.type === 'TIME_LIMIT') {
                    const pct = limit.percentage || 0;
                    let valueClass = 'quota-item-value';
                    if (pct >= 80) valueClass += ' low';
                    else if (pct <= 20) valueClass += ' full';

                    html += `<div class="quota-item" style="margin-top:10px; padding-top:8px; border-top:1px solid rgba(255,255,255,0.05);">
                        <span class="quota-item-label" style="font-weight:bold; color:#ddd;">Web/Reader/Zread</span>
                        <span class="${valueClass}">${pct}% 사용</span>
                    </div>`;
                    html += `<div class="quota-item">
                        <span class="quota-item-label">사용량</span>
                        <span class="quota-item-value">${limit.currentValue || 0} / ${limit.usage || 0} (남은: ${limit.remaining || 0})</span>
                    </div>`;
                } else if (limit.type === 'TOKENS_LIMIT') {
                    const pct = limit.percentage || 0;
                    let valueClass = 'quota-item-value';
                    if (pct >= 80) valueClass += ' low';
                    else if (pct <= 20) valueClass += ' full';

                    html += `<div class="quota-item" style="margin-top:10px; padding-top:8px; border-top:1px solid rgba(255,255,255,0.05);">
                        <span class="quota-item-label" style="font-weight:bold; color:#ddd;">5시간 토큰 할당량</span>
                        <span class="${valueClass}">${pct}% 사용</span>
                    </div>`;

                    if (limit.nextResetTime) {
                        const resetTime = new Date(limit.nextResetTime);
                        const now = new Date();
                        const diff = resetTime - now;
                        let remainStr;
                        if (diff <= 0) {
                            remainStr = '초기화됨';
                        } else {
                            const rh = Math.floor(diff / (1000 * 60 * 60));
                            const rm = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
                            remainStr = rh > 0 ? `${rh}시간 ${rm}분 남음` : `${rm}분 남음`;
                        }
                        html += `<div class="quota-item">
                            <span class="quota-item-label">초기화 시간</span>
                            <span class="quota-item-value">${resetTime.toLocaleString()}</span>
                        </div>`;
                        html += `<div class="quota-item">
                            <span class="quota-item-label">남은 시간</span>
                            <span class="quota-item-value" style="color:${diff < 10 * 60 * 1000 ? '#ff6b6b' : '#4caf50'}">${remainStr}</span>
                        </div>`;
                    }
                }
            }
        }

        if (!html) {
            html = '<p class="no-records">사용량 정보를 찾을 수 없습니다.</p>';
        }

        const now = new Date();
        html += `<div class="quota-updated">마지막 조회: ${now.toLocaleTimeString()}</div>`;
        display.innerHTML = html;

    } catch (e) {
        console.error('ZAI 승수 조회 실패:', e);
        statusBadge.textContent = '오류';
        statusBadge.className = 'quota-status error';
        display.innerHTML = `<p class="no-records">조회 실패: ${escapeHtml(e.message)}</p>`;
    }
}

// Tavily 승수 조회 및 렌더링
async function fetchAndRenderTavilyQuota() {
    const display = document.getElementById('tavily-quota-display');
    const statusBadge = document.getElementById('tavily-quota-status');

    try {
        // 설정 확인
        const settingsRes = await fetch('/api/settings');
        const settings = await settingsRes.json();

        if (!settings.tavily_quota_enabled) {
            statusBadge.textContent = '비활성화';
            statusBadge.className = 'quota-status';
            display.innerHTML = '<p class="no-records">설정에서 Tavily 승수 조회를 활성화하세요.</p>';
            return;
        }

        statusBadge.textContent = '조회 중...';
        statusBadge.className = 'quota-status';

        const res = await fetch('/api/tavily/quota');
        const result = await res.json();

        if (!result.configured) {
            statusBadge.textContent = '오류';
            statusBadge.className = 'quota-status error';
            display.innerHTML = `<p class="no-records">${escapeHtml(result.error || 'API 키가 없습니다.')}</p>`;
            return;
        }

        statusBadge.textContent = '활성';
        statusBadge.className = 'quota-status active';

        // Tavily 응답 파싱
        let html = '';
        const keys = result.keys || [];

        html += `<div class="quota-item" style="margin-bottom:8px;">
            <span class="quota-item-label" style="font-weight:bold; color:#ddd;">등록된 키</span>
            <span class="quota-item-value">${keys.length}개</span>
        </div>`;

        for (const keyInfo of keys) {
            const label = keyInfo.label || '?';
            if (keyInfo.error) {
                html += `<div class="quota-item" style="margin-top:8px; padding-top:8px; border-top:1px solid rgba(255,255,255,0.05);">
                    <span class="quota-item-label" style="font-weight:bold; color:#ddd;">키 ${escapeHtml(label)}</span>
                    <span class="quota-item-value" style="color:#ff6b6b;">조회 실패</span>
                </div>`;
            } else {
                const used = keyInfo.used || 0;
                const limit = keyInfo.limit || 1000;
                const remaining = keyInfo.remaining || 0;
                const pct = limit > 0 ? ((used / limit) * 100).toFixed(1) : 0;
                const remainColor = remaining < 100 ? '#ff6b6b' : (remaining < 300 ? '#ff9800' : '#4caf50');

                html += `<div class="quota-item" style="margin-top:8px; padding-top:8px; border-top:1px solid rgba(255,255,255,0.05);">
                    <span class="quota-item-label" style="font-weight:bold; color:#ddd;">키 ${escapeHtml(label)}</span>
                    <span class="quota-item-value" style="color:${remainColor};">${remaining} / ${limit} 크레딧 (${pct}% 사용)</span>
                </div>`;
            }
        }

        const now = new Date();
        html += `<div class="quota-updated">마지막 조회: ${now.toLocaleTimeString()}</div>`;
        display.innerHTML = html;

    } catch (e) {
        console.error('Tavily 승수 조회 실패:', e);
        statusBadge.textContent = '오류';
        statusBadge.className = 'quota-status error';
        display.innerHTML = `<p class="no-records">조회 실패: ${escapeHtml(e.message)}</p>`;
    }
}

// ============ 플로팅 승수 위젯 ============

const activeWidgets = {};
let lastCopilotQuotaData = null;
let lastZaiQuotaData = null;
let zaiNextResetTime = null;
let zaiTimerInterval = null;

// 위젯 토글
function toggleQuotaWidget(type) {
    if (activeWidgets[type]) {
        removeQuotaWidget(type);
    } else {
        createQuotaWidget(type);
    }
}

// 위젯 생성
function createQuotaWidget(type) {
    if (activeWidgets[type]) return;

    const container = document.getElementById('floating-widgets-container');
    const widget = document.createElement('div');
    widget.className = 'floating-quota-widget';
    widget.id = `widget-${type}`;

    const color = type === 'copilot' ? '#00d4ff' : '#ce93d8';
    const title = type === 'copilot' ? '🔵 Copilot Premium' : '🟣 ZAI';

    widget.style.borderColor = color;
    widget.innerHTML = `
        <div class="widget-title">
            <span>${title}</span>
            <span class="widget-close" onclick="removeQuotaWidget('${type}')">&times;</span>
        </div>
        <canvas id="widget-canvas-${type}" width="100" height="100"></canvas>
        <div class="widget-info">
            <span class="widget-pct" id="widget-pct-${type}" style="color:${color}">--</span>
            <span id="widget-detail-${type}">조회 중...</span>
        </div>
        <div class="widget-timer" id="widget-timer-${type}"></div>
    `;

    // 기존 위젯 개수에 따라 위치 배치
    const existingCount = Object.keys(activeWidgets).length;
    widget.style.right = `${20 + existingCount * 180}px`;
    widget.style.top = '80px';

    container.appendChild(widget);
    makeDraggable(widget);
    activeWidgets[type] = widget;

    // 데이터 로드
    fetchQuotaDataForWidget(type);
}

// 위젯 제거
function removeQuotaWidget(type) {
    if (activeWidgets[type]) {
        activeWidgets[type].remove();
        delete activeWidgets[type];
    }
    // ZAI 타이머 정리
    if (type === 'zai' && zaiTimerInterval) {
        clearInterval(zaiTimerInterval);
        zaiTimerInterval = null;
        zaiNextResetTime = null;
    }
}

// 드래그 기능
function makeDraggable(el) {
    let isDragging = false;
    let startX, startY, origLeft, origTop;

    const onMouseDown = (e) => {
        if (e.target.classList.contains('widget-close')) return;
        isDragging = true;
        startX = e.clientX;
        startY = e.clientY;
        const rect = el.getBoundingClientRect();
        origLeft = rect.left;
        origTop = rect.top;
        el.style.transition = 'none';
        e.preventDefault();
    };

    const onMouseMove = (e) => {
        if (!isDragging) return;
        const dx = e.clientX - startX;
        const dy = e.clientY - startY;
        el.style.left = `${origLeft + dx}px`;
        el.style.top = `${origTop + dy}px`;
        el.style.right = 'auto';
    };

    const onMouseUp = () => {
        isDragging = false;
        el.style.transition = '';
    };

    el.addEventListener('mousedown', onMouseDown);
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
}

// 위젯용 데이터 로드
async function fetchQuotaDataForWidget(type) {
    try {
        const res = await fetch(`/api/${type === 'copilot' ? 'copilot' : 'zai'}/quota`);
        const result = await res.json();

        if (type === 'copilot') {
            if (result.configured && result.data) lastCopilotQuotaData = result.data;
        } else {
            if (result.configured && result.data) lastZaiQuotaData = result.data;
        }
    } catch (e) {
        console.error(`위젯 데이터 로드 실패 (${type}):`, e);
    }
    updateWidgetDisplay(type);
}

// 원형 그래프 그리기
function drawWidgetDonut(canvasId, percentage, color) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const cx = canvas.width / 2;
    const cy = canvas.height / 2;
    const radius = 38;
    const lineWidth = 10;

    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // 배경 원
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, 2 * Math.PI);
    ctx.strokeStyle = 'rgba(255,255,255,0.1)';
    ctx.lineWidth = lineWidth;
    ctx.stroke();

    // 사용량 원
    if (percentage > 0) {
        const usedAngle = (percentage / 100) * 2 * Math.PI;
        ctx.beginPath();
        ctx.arc(cx, cy, radius, -Math.PI / 2, -Math.PI / 2 + usedAngle);
        ctx.strokeStyle = color;
        ctx.lineWidth = lineWidth;
        ctx.lineCap = 'round';
        ctx.stroke();
    }

    // 중앙 텍스트
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 14px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(`${percentage.toFixed(1)}%`, cx, cy);
}

// 위젯 표시 업데이트
function updateWidgetDisplay(type) {
    const pctEl = document.getElementById(`widget-pct-${type}`);
    const detailEl = document.getElementById(`widget-detail-${type}`);

    if (type === 'copilot' && lastCopilotQuotaData) {
        const pi = lastCopilotQuotaData.quota_snapshots?.premium_interactions;
        if (pi) {
            const remaining = pi.remaining || 0;
            const entitlement = pi.entitlement || 1;
            const usedPct = ((entitlement - remaining) / entitlement * 100);
            drawWidgetDonut('widget-canvas-copilot', usedPct, '#ff6b6b');
            if (pctEl) pctEl.textContent = `${remaining} / ${entitlement}`;
            if (detailEl) detailEl.textContent = 'Premium Requests';
        }
    } else if (type === 'zai' && lastZaiQuotaData) {
        const qData = lastZaiQuotaData.data;
        if (qData && qData.limits) {
            const tokensLimit = qData.limits.find(l => l.type === 'TOKENS_LIMIT');

            // TOKENS_LIMIT가 없으면 100% 도달로 간주 (|| 대신 ?? 사용)
            const pct = tokensLimit?.percentage ?? 100;
            drawWidgetDonut('widget-canvas-zai', pct, '#ce93d8');
            if (pctEl) pctEl.textContent = `${pct.toFixed(1)}% 사용`;
            if (detailEl) detailEl.textContent = '5시간 토큰 할당량';

            // 5시간 쿼터 초기화까지 남은 시간 카운트다운
            if (tokensLimit?.nextResetTime) {
                zaiNextResetTime = new Date(tokensLimit.nextResetTime);
                updateZaiTimerDisplay();
                if (!zaiTimerInterval) {
                    zaiTimerInterval = setInterval(updateZaiTimerDisplay, 1000);
                }
            }
        }
    }
}

// ZAI 위젯 타이머 표시 업데이트
function updateZaiTimerDisplay() {
    const timerEl = document.getElementById('widget-timer-zai');
    if (!timerEl || !zaiNextResetTime) return;

    const now = new Date();
    const diff = zaiNextResetTime - now;

    if (diff <= 0) {
        timerEl.textContent = '초기화됨 - 새로고침 필요';
        timerEl.style.color = '#4caf50';
        if (zaiTimerInterval) {
            clearInterval(zaiTimerInterval);
            zaiTimerInterval = null;
        }
        return;
    }

    const hours = Math.floor(diff / (1000 * 60 * 60));
    const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
    const seconds = Math.floor((diff % (1000 * 60)) / 1000);

    let timeStr;
    if (hours > 0) {
        timeStr = `${hours}시간 ${minutes}분 ${seconds}초`;
    } else if (minutes > 0) {
        timeStr = `${minutes}분 ${seconds}초`;
    } else {
        timeStr = `${seconds}초`;
    }
    timerEl.textContent = `⏱ ${timeStr} 남음`;
    timerEl.style.color = diff < 10 * 60 * 1000 ? '#ff6b6b' : '#aaa';
}

// 모든 활성 위젯 새로고침
async function refreshAllWidgets() {
    for (const type of Object.keys(activeWidgets)) {
        await fetchQuotaDataForWidget(type);
    }
}
