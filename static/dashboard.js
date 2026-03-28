// 대시보드 상태
let records = [];
let stats = {};
let pending = {};
let pricing = {}; // 가격 정보
let expandedItems = new Set(); // 펼쳐진 항목 추적
let lastRecordsLength = 0; // 이전 기록 개수
let lastStatsJson = ""; // 이전 통계 JSON

// 모델 이름 매핑 (stats 모델명 -> price 모델명)
const MODEL_NAME_MAP = {
    'vertex/gemini-3-flash-preview': 'Gemini 3.0 Flash',
    'vertex/gemini-3.1-pro-preview': 'Gemini 3.1 Pro',
    'vertex/gemini-3-flash': 'Gemini 3.0 Flash',
    'vertex/gemini-3.1-pro': 'Gemini 3.1 Pro',
    'vertex/gemini-2.5-pro': 'Gemini 3.1 Pro',
    'vertex/gemini-2.5-flash': 'Gemini 3.0 Flash'
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
        
        return `
            <div class="pending-item">
                <div>
                    <strong>${data.model}</strong> (${data.type})
                    <br><small>${data.request_preview || '요청 중...'}</small>
                </div>
                <div style="display: flex; align-items: center; gap: 10px;">
                    <span>⏱️ ${elapsed}초</span>
                    ${data.retry_count ? `<span style="color: #ff6b6b; font-size: 0.9em; margin-left: 5px;">[재시도 ${data.retry_count}회]</span>` : ''}
                    ${retryButton}
                    ${cancelButton}
                    <div class="spinner"></div>
                </div>
            </div>
        `;
    }).join('');
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
        
        return `
        <div class="record-card ${record.type}">
            <div class="record-header">
                <span class="record-model">${record.model}</span>
                <span class="record-time">${record.timestamp}</span>
                ${cacheBadge}
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
                <button class="collapsible" data-toggle="response-${index}">💬 응답 보기 (Full JSON)</button>
            </div>
            <div id="request-${index}" class="content-box">${formatJsonString(record.request)}</div>
            <div id="response-${index}" class="content-box">${formatJsonString(record.response)}</div>
        </div>
        `;
    }).join('');
    
    // 동적으로 생성된 버튼에 이벤트 리스너 추가
    container.querySelectorAll('.collapsible').forEach(btn => {
        btn.addEventListener('click', function() {
            const targetId = this.getAttribute('data-toggle');
            toggleContent(targetId);
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
                vertex_retry_delay: vertexRetryDelay
            })
        });
        
        if (response.ok) {
            alert('설정이 저장되었습니다.');
            closeSettingsModal();
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
    
    // 모달 닫기 버튼 (X)
    const closeBtn = document.querySelector('.close');
    if (closeBtn) {
        closeBtn.addEventListener('click', closeSettingsModal);
        closeBtn.removeAttribute('onclick');
    }
    
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
        const modal = document.getElementById('settings-modal');
        if (event.target == modal) {
            modal.style.display = 'none';
        }
    });
    
    // 초기 로드 및 주기적 갱신
    fetchData();
    setInterval(fetchData, 1000);
});
