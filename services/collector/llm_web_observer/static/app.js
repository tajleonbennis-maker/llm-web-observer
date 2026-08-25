const state = {
  metrics: {}, conversations: [], policies: [],
  key: sessionStorage.getItem('lwo-key') || '',
  page: 0, pageSize: 50, total: 0, searching: false,
  live: [], maxLive: 100,
  titles: { overview: '仪表盘', live: '实时监控', search: '事件检索', policies: '策略管理' },
  subs: { overview: '实时观测每一次 LLM 调用', live: '毫秒级事件流', search: '检索历史调用', policies: '双向网关策略' },
};
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const dur = v => v == null ? '—' : v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(2)} s`;
const fmtTime = v => v ? new Date(v).toLocaleString('zh-CN', { hour12: false }) : '—';

function badge(action) {
  const label = { allow: '放行', redact: '脱敏', block: '阻断', log: '记录' }[action] || action || '放行';
  return `<span class="badge ${esc(action || 'allow')}">${esc(label)}</span>`;
}
function dirLabel(d) { return { request: '用户 → 模型', response: '模型 → 用户', both: '双向' }[d] || d; }
function toast(msg, bad = false) { const x = $('#toast'); x.textContent = msg; x.className = bad ? 'show bad' : 'show'; setTimeout(() => x.className = '', 2600); }

async function api(url, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (state.key) headers.Authorization = `Bearer ${state.key}`;
  const r = await fetch(url, Object.assign({}, options, { headers }));
  if (r.status === 401) { state.key = ''; sessionStorage.removeItem('lwo-key'); throw new Error('未授权，请先输入 API Key'); }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
  return r.json();
}

/* ---------- overview ---------- */
function renderMetrics() {
  const m = state.metrics, models = m.models || [];
  const calls = models.reduce((n, x) => n + x.calls, 0);
  const tokens = models.reduce((n, x) => n + (x.input_tokens || 0) + (x.output_tokens || 0), 0);
  const items = [
    ['LLM 调用', calls.toLocaleString(), '观测到的模型请求'],
    ['Trace', (m.traces || 0).toLocaleString(), '记录的完整链路'],
    ['错误', (m.errors || 0).toLocaleString(), '失败的调用'],
    ['Token 用量', tokens.toLocaleString(), '输入 + 输出'],
  ];
  $('#metrics').innerHTML = items.map(x => `<div class="metric"><div class="label">${x[0]}</div><div class="value">${x[1]}</div><div class="sub">${x[2]}</div></div>`).join('');
}
function renderActivity() {
  const recent = state.conversations.slice(0, 8);
  $('#activity').innerHTML = recent.map((c, i) => `
    <div class="row" data-idx="${i}">
      <div class="avatar">${esc((c.username || 'A').slice(0, 1).toUpperCase())}</div>
      <div class="body"><b>${esc(c.username || c.user_id || '匿名')}</b><small>${esc(c.message || '未捕获到消息')}</small></div>
      <div class="meta">${badge(c.policy_action)}<time>${dur(c.duration_ms)}</time></div>
    </div>`).join('') || '<p class="empty">暂无用户对话</p>';
  $$('#activity .row').forEach(x => x.onclick = () => openDetail(recent[+x.dataset.idx]));
}
function renderModels() {
  $('#models').innerHTML = (state.metrics.models || []).map(m => `
    <div class="row">
      <div class="avatar">AI</div>
      <div class="body"><b>${esc(m.model || '未知')}</b><small>${m.calls} 次 · ${((m.input_tokens || 0) + (m.output_tokens || 0)).toLocaleString()} tokens</small></div>
      <div class="meta"><span class="badge ok">${m.calls}</span></div>
    </div>`).join('') || '<p class="empty">暂无模型调用</p>';
}

/* ---------- live stream (SSE) ---------- */
function setConn(on) {
  const el = $('#conn-status');
  el.className = 'live-status ' + (on ? 'on' : 'off');
  $('#conn-label').textContent = on ? '实时流：已连接' : '实时流：已断开';
}
function renderLive() {
  $('#live-count').textContent = `${state.live.length} 条`;
  $('#live-stream').innerHTML = state.live.map((c, i) => `
    <div class="live-item" data-idx="${i}">
      <div class="head"><b>${esc(c.username || c.user_id || '匿名')}</b><span class="muted">${esc(c.model || '')}</span>${badge(c.policy_action)}<time class="muted">${fmtTime(c.timestamp)}</time></div>
      <div class="msg">${esc(c.message || '')}</div>
      ${c.response ? `<div class="resp">${esc(c.response)}</div>` : ''}
    </div>`).join('') || '<p class="empty">等待事件…</p>';
  $$('#live-stream .live-item').forEach(x => x.onclick = () => openDetail(state.live[+x.dataset.idx]));
}
async function connectStream() {
  try {
    const res = await fetch('/v1/events/stream', { headers: state.key ? { Authorization: `Bearer ${state.key}` } : {} });
    if (res.status === 401) { setConn(false); $('#conn-label').textContent = '需要 API Key'; if (!keyResolve) openKeyDialog(); return; }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    setConn(true);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const raw = buffer.slice(0, idx); buffer = buffer.slice(idx + 2);
        const line = raw.split('\n').find(l => l.startsWith('data: '));
        if (!line) continue;
        const data = line.slice(6).trim();
        if (data.startsWith('{')) { try { onEvent(JSON.parse(data)); } catch (e) {} }
      }
    }
  } catch (e) { /* network hiccup */ }
  setConn(false);
  setTimeout(connectStream, 3000);
}
function onEvent(item) {
  if (item.call_kind && item.call_kind !== 'user.chat') return;
  state.live.unshift(item);
  if (state.live.length > state.maxLive) state.live.pop();
  renderLive();
  if (state.page === 0 && state.searching) doSearch(); // live-refresh the first search page
  refreshMetricsOnly();
}

/* ---------- search ---------- */
function toUTC(v) { return v ? new Date(v).toISOString() : null; }
async function doSearch() {
  state.searching = true;
  const params = new URLSearchParams({ limit: state.pageSize, offset: state.page * state.pageSize });
  const f = id => $(id).value.trim();
  if (f('#f-q')) params.set('q', f('#f-q'));
  if (f('#f-user')) params.set('user', f('#f-user'));
  if (f('#f-model')) params.set('model', f('#f-model'));
  if (f('#f-action')) params.set('action', f('#f-action'));
  if (f('#f-direction')) params.set('call_kind', f('#f-direction'));
  if (toUTC(f('#f-since'))) params.set('since', toUTC(f('#f-since')));
  if (toUTC(f('#f-until'))) params.set('until', toUTC(f('#f-until')));
  try {
    const res = await api(`/v1/conversations?${params}`);
    state.conversations = res.items; state.total = res.total;
    renderSearchRows();
    $('#page-info').textContent = `共 ${res.total} 条 · 第 ${state.page + 1} 页（每页 ${state.pageSize} 条）`;
    $('#prev-page').disabled = state.page === 0;
    $('#next-page').disabled = (state.page + 1) * state.pageSize >= res.total;
  } catch (e) { toast(e.message, true); }
}
function renderSearchRows() {
  $('#search-rows').innerHTML = state.conversations.map((c, i) => `
    <tr data-idx="${i}">
      <td><time>${fmtTime(c.timestamp)}</time></td>
      <td><div class="cell-main">${esc(c.username || c.user_id || '匿名')}</div><div class="cell-sub">${esc(c.source_ip || '')}</div></td>
      <td class="cell-main">${esc(c.message || '—')}</td>
      <td class="cell-sub">${esc(c.response || '—')}</td>
      <td class="cell-main mono">${esc(c.model || '—')}</td>
      <td>${badge(c.policy_action)}</td>
      <td>${dur(c.duration_ms)}</td>
    </tr>`).join('') || '<tr><td colspan="7" class="empty">无匹配结果</td></tr>';
  $$('#search-rows tr[data-idx]').forEach(x => x.onclick = () => openDetail(state.conversations[+x.dataset.idx]));
}

/* ---------- detail ---------- */
function openDetail(c) {
  const context = c.context_messages || [];
  const history = context.filter(m => m.role !== 'system');
  const turns = history.length ? history : [{ role: 'user', content: c.message || '' }];
  const messages = [...turns, ...(c.response ? [{ role: 'assistant', content: c.response }] : [])];
  const systems = context.filter(m => m.role === 'system');
  const respAction = c.response_action || c.policy_action;
  $('#detail-title').textContent = '对话详情';
  $('#detail-body').innerHTML = `
    <div class="detail-id">
      <span>时间 <code>${esc(fmtTime(c.timestamp))}</code></span>
      <span>用户 <code>${esc(c.username || c.user_id || '匿名')}</code></span>
      <span>模型 <code>${esc(c.model || '—')}</code></span>
      <span>IP <code>${esc(c.source_ip || '—')}</code></span>
      <span>会话 <code>${esc(c.conversation_id || '—')}</code></span>
      <span>Trace <code>${esc(c.trace_id || '—')}</code></span>
      <span>Token <code>${esc(c.input_tokens ?? '—')} in / ${esc(c.output_tokens ?? '—')} out</code></span>
      <span>置信度 <code>${esc(c.identity_confidence || '—')}</code></span>
    </div>
    <div style="margin-bottom:14px">请求决策 ${badge(c.policy_action)} ${c.policy_rule ? `<span class="muted">· ${esc(c.policy_rule)}</span>` : ''}　响应决策 ${badge(respAction)}</div>
    ${messages.map(m => `<div class="msg-block ${esc(m.role)}"><div class="role">${m.role === 'user' ? '用户消息' : '模型回复'}</div><pre>${esc(m.content || '')}</pre></div>`).join('')}
    ${systems.length ? `<details><summary class="muted" style="cursor:pointer;margin-bottom:8px">系统提示词（${systems.length} 条）</summary>${systems.map(m => `<div class="msg-block system"><div class="role">system</div><pre>${esc(m.content)}</pre></div>`).join('')}</details>` : ''}`;
  $('#detail-dialog').showModal();
}

/* ---------- policies ---------- */
function renderPolicies() {
  $('#policy-list').innerHTML = state.policies.map(p => `
    <div class="policy-item" data-id="${p.id}">
      <div class="p-body"><b>${esc(p.name)}</b><small>${esc(p.description || `${p.match_type}: ${p.pattern}`)}</small></div>
      <span class="dir-tag">${esc(dirLabel(p.direction || 'request'))}</span>
      ${badge(p.action)}
      <span class="state ${p.enabled ? 'on' : 'off'}">${p.enabled ? '已启用' : '已停用'}</span>
    </div>`).join('') || '<p class="empty">暂无策略，流量默认全部放行。</p>';
  $$('.policy-item').forEach(x => x.onclick = () => editPolicy(state.policies.find(p => p.id === +x.dataset.id)));
}
function editPolicy(p = {}) {
  $('#policy-title').textContent = p.id ? '编辑策略' : '新建策略';
  $('#policy-id').value = p.id || '';
  $('#policy-name').value = p.name || '';
  $('#policy-description').value = p.description || '';
  $('#policy-pattern').value = p.pattern || '';
  $('#policy-match').value = p.match_type || 'contains';
  $('#policy-action').value = p.action || 'log';
  $('#policy-direction').value = p.direction || 'request';
  $('#policy-enabled').checked = p.enabled !== false;
  $('#policy-dialog').showModal();
}
let keyResolve = null;
function openKeyDialog() {
  $('#admin-key').value = state.key || '';
  $('#key-status').textContent = state.key ? '已配置 Key（可重新输入覆盖）' : '尚未配置 Key';
  $('#key-dialog').showModal();
}
function saveKey() {
  const v = $('#admin-key').value.trim();
  if (!v) { toast('请输入 API Key', true); return; }
  state.key = v; sessionStorage.setItem('lwo-key', v);
  const resolve = keyResolve; keyResolve = null;
  $('#key-dialog').close();
  if (resolve) resolve(true);
  toast('API Key 已保存');
  connectStream();
  loadPolicies();
}
function cancelKey() {
  const resolve = keyResolve; keyResolve = null;
  $('#key-dialog').close();
  if (resolve) resolve(false);
}
async function auth() {
  if (state.key) return true;
  openKeyDialog();
  return new Promise(ok => { keyResolve = ok; });
}
async function savePolicy(e) {
  e.preventDefault();
  if (!await auth()) return;
  const id = $('#policy-id').value;
  const body = {
    name: $('#policy-name').value, description: $('#policy-description').value, pattern: $('#policy-pattern').value,
    match_type: $('#policy-match').value, action: $('#policy-action').value, direction: $('#policy-direction').value, enabled: $('#policy-enabled').checked,
  };
  try {
    await api(id ? `/v1/policies/${id}` : '/v1/policies', { method: id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    $('#policy-dialog').close();
    await loadPolicies(); toast('策略已保存');
  } catch (e) { toast(e.message, true); }
}

/* ---------- nav & refresh ---------- */
function show(id) {
  $$('.view').forEach(x => x.classList.toggle('active', x.id === id));
  $$('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.view === id));
  $('#page-title').textContent = state.titles[id];
  $('#page-sub').textContent = state.subs[id];
}
async function refreshMetricsOnly() { try { state.metrics = await api('/v1/metrics'); if ($('#overview').classList.contains('active')) { renderMetrics(); renderModels(); } } catch (e) {} }
async function loadPolicies() { try { state.policies = await api('/v1/policies'); renderPolicies(); } catch (e) {} }
async function refreshAll() {
  try {
    const [metrics, conv] = await Promise.all([api('/v1/metrics'), api('/v1/conversations?limit=100')]);
    state.metrics = metrics; state.conversations = conv.items; state.total = conv.total;
    renderMetrics(); renderActivity(); renderModels(); renderSearchRows();
    $('#page-info').textContent = `共 ${conv.total} 条 · 第 1 页`;
  } catch (e) { toast(e.message, true); }
}

/* ---------- clock ---------- */
function tick() { $('#clock').textContent = new Date().toLocaleString('zh-CN', { hour12: false }); }

/* ---------- init ---------- */
$$('.nav-item').forEach(x => x.onclick = () => show(x.dataset.view));
$('#refresh').onclick = () => { refreshAll(); loadPolicies(); };
$('#btn-search').onclick = () => { state.page = 0; doSearch(); };
$('#btn-reset').onclick = () => { ['f-q', 'f-user', 'f-model', 'f-action', 'f-direction', 'f-since', 'f-until'].forEach(id => $(`#${id}`).value = ''); state.page = 0; doSearch(); };
$('#prev-page').onclick = () => { if (state.page > 0) { state.page--; doSearch(); } };
$('#next-page').onclick = () => { state.page++; doSearch(); };
$('#new-policy').onclick = () => editPolicy();
$('#policy-form').onsubmit = savePolicy;
$$('[data-close]').forEach(x => x.onclick = () => x.closest('dialog').close());
$('#config-key').onclick = openKeyDialog;
$('#save-key').onclick = saveKey;
$('#cancel-key').onclick = cancelKey;
$('#key-dialog').addEventListener('close', () => { if (keyResolve) { const r = keyResolve; keyResolve = null; r(false); } });

tick(); setInterval(tick, 1000);
show('overview');
refreshAll(); loadPolicies(); doSearch();
connectStream();
