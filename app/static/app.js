/* ================= 标签页 ================= */
const panes = { recon: document.getElementById('pane-recon'), plan: document.getElementById('pane-plan') };
const tabBtns = { recon: document.getElementById('tab-recon'), plan: document.getElementById('tab-plan') };
for (const name of ['recon', 'plan']) {
  tabBtns[name].onclick = () => {
    for (const k of ['recon', 'plan']) {
      panes[k].hidden = k !== name;
      tabBtns[k].classList.toggle('active', k === name);
    }
  };
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
function fmtMs(v) { return v == null ? '—' : Number(v).toFixed(1) + ' ms'; }
function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleString() : '—'; }

/* ================= 每日流水核对 ================= */
const RECON_STATUS = {
  running: '核对中', published: '已发布', stale: '未覆盖（迟到）', failed: '失败',
};
const FIELD_TEXT = { merchant_id: '商户', amount: '金额', fee: '手续费' };

let currentReportDate = null;   // 报告区当前选中的结算日
let lastReportKey = null;       // 已渲染报告的「日期:序号」，避免每次轮询重绘
let reportListKey = null;       // 报告列表 key，避免每次轮询重建下拉框
let reportMeta = {};            // settle_date -> 列表接口返回的摘要（含最新任务）

async function refreshRecon() {
  try {
    const [tRes, rRes] = await Promise.all([
      fetch('/api/recon/tasks'), fetch('/api/recon/reports'),
    ]);
    const tData = await tRes.json();
    const rData = await rRes.json();
    renderReconTasks(tData.tasks || []);
    renderReportSelect(rData.reports || []);
  } catch (e) { /* 忽略瞬时网络错误，下一轮再试 */ }
}

function renderReconTasks(tasks) {
  const box = document.getElementById('recon-tasks');
  if (!tasks.length) { box.innerHTML = '<p class="hint">暂无核对任务</p>'; return; }
  const rows = tasks.map(t => {
    const s = t.summary;
    const stat = s
      ? `一致 ${s.matched} · 渠道独有 ${s.channel_only} · 库内独有 ${s.db_only} · 不符 ${s.mismatch} · 冲突 ${s.conflict}`
      : '';
    return `<tr>
      <td>#${t.seq}</td>
      <td>${escapeHtml(t.settle_date)}</td>
      <td>${escapeHtml(t.filename)}</td>
      <td>${fmtTime(t.created_at)}</td>
      <td><span class="ex-status ${t.status}">${RECON_STATUS[t.status] || t.status}</span></td>
      <td class="hint">${t.error ? escapeHtml(t.error) : escapeHtml(stat)}</td>
    </tr>`;
  }).join('');
  box.innerHTML = `<table class="ex-table">
    <thead><tr><th>序号</th><th>结算日</th><th>文件</th><th>提交时间</th><th>状态</th><th>说明 / 统计</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function renderReportSelect(reports) {
  reportMeta = {};
  for (const r of reports) reportMeta[r.settle_date] = r;
  const sel = document.getElementById('report-select');
  if (!reports.length) {
    sel.innerHTML = '';
    currentReportDate = null;
    lastReportKey = null;
    reportListKey = null;
    document.getElementById('report-notice').innerHTML = '';
    document.getElementById('report-view').innerHTML = '<p class="hint">暂无已发布报告，请先上传渠道 CSV。</p>';
    return;
  }
  if (!currentReportDate || !reportMeta[currentReportDate]) {
    currentReportDate = reports[0].settle_date;
    lastReportKey = null;
  }
  const listKey = reports.map(r => `${r.settle_date}:${r.seq}`).join('|');
  if (listKey !== reportListKey) {
    reportListKey = listKey;
    sel.innerHTML = reports.map(r =>
      `<option value="${escapeHtml(r.settle_date)}"${r.settle_date === currentReportDate ? ' selected' : ''}>
        ${escapeHtml(r.settle_date)}（第 ${r.seq} 版）
      </option>`).join('');
  }
  loadReport();
}

document.getElementById('report-select').onchange = (e) => {
  currentReportDate = e.target.value;
  lastReportKey = null;
  loadReport();
};

async function loadReport() {
  if (!currentReportDate) return;
  const meta = reportMeta[currentReportDate];
  const key = meta ? `${currentReportDate}:${meta.seq}:${meta.latest_task ? meta.latest_task.id + meta.latest_task.status : ''}` : null;
  if (key && key === lastReportKey) return;  // 报告与最新任务状态都没变，不重绘
  lastReportKey = key;
  try {
    const res = await fetch(`/api/recon/reports/${currentReportDate}`);
    if (!res.ok) {
      document.getElementById('report-view').innerHTML = '<p class="hint">报告不存在或已被替换。</p>';
      return;
    }
    const rep = await res.json();
    renderReportNotice(meta);
    renderReport(rep);
  } catch (e) { /* 下一轮再试 */ }
}

function renderReportNotice(meta) {
  const box = document.getElementById('report-notice');
  if (!meta || !meta.latest_task) { box.innerHTML = ''; return; }
  const t = meta.latest_task;
  if (t.status === 'failed') {
    box.innerHTML = `<div class="notice warn">该结算日最新任务 #${t.seq} 失败（${escapeHtml(t.error || '未知原因')}），以下仍为第 ${meta.seq} 版有效报告。</div>`;
  } else if (t.status === 'running') {
    box.innerHTML = `<div class="notice info">有更新的任务 #${t.seq} 正在核对，完成并发布前以下仍为第 ${meta.seq} 版报告。</div>`;
  } else {
    box.innerHTML = '';
  }
}

function flowCells(r) {
  return `<td>${escapeHtml(r.order_no)}</td><td>${r.merchant_id}</td>
    <td>${escapeHtml(r.amount)}</td><td>${escapeHtml(r.fee)}</td>
    <td class="hint">${r.line ? 'CSV 第 ' + r.line + ' 行' : ''}</td>`;
}

function simpleFlowTable(rows, side) {
  const body = rows.map(r => `<tr>${flowCells(r)}</tr>`).join('');
  return `<table class="ex-table"><thead><tr>
    <th>订单号</th><th>商户</th><th>金额</th><th>手续费</th><th>${side === 'channel' ? '来源' : ''}</th>
  </tr></thead><tbody>${body}</tbody></table>`;
}

function mismatchTable(rows) {
  const body = rows.map(m => {
    const fields = m.fields.map(f => FIELD_TEXT[f] || f).join('、');
    return `<tr>
      <td>${escapeHtml(m.order_no)}</td><td>${escapeHtml(fields)}</td>
      <td>${m.channel.merchant_id}</td><td>${escapeHtml(m.channel.amount)}</td><td>${escapeHtml(m.channel.fee)}</td>
      <td>${m.db.merchant_id}</td><td>${escapeHtml(m.db.amount)}</td><td>${escapeHtml(m.db.fee)}</td>
    </tr>`;
  }).join('');
  return `<table class="ex-table"><thead><tr>
    <th>订单号</th><th>不符字段</th>
    <th>渠道商户</th><th>渠道金额</th><th>渠道手续费</th>
    <th>库内商户</th><th>库内金额</th><th>库内手续费</th>
  </tr></thead><tbody>${body}</tbody></table>`;
}

function conflictTable(rows) {
  const body = rows.map(c => {
    const ch = c.channel_rows.map(r =>
      `<div>CSV 第 ${r.line} 行：商户 ${r.merchant_id}，金额 ${escapeHtml(r.amount)}，手续费 ${escapeHtml(r.fee)}</div>`).join('')
      || '<div class="hint">渠道无此单</div>';
    const db = c.db_rows.map(r =>
      `<div>库内：商户 ${r.merchant_id}，金额 ${escapeHtml(r.amount)}，手续费 ${escapeHtml(r.fee)}</div>`).join('')
      || '<div class="hint">库内无此单</div>';
    return `<tr><td>${escapeHtml(c.order_no)}</td><td>${ch}</td><td>${db}</td></tr>`;
  }).join('');
  return `<table class="ex-table"><thead><tr>
    <th>订单号</th><th>渠道侧记录</th><th>库内记录</th>
  </tr></thead><tbody>${body}</tbody></table>`;
}

function diffSection(title, count, truncated, tableHtml) {
  if (!count) return '';
  const cap = truncated ? `<span class="hint">（仅展示前 ${REPORT_LIST_CAP} 条）</span>` : '';
  return `<h3>${title}（${count}）${cap}</h3>${tableHtml}`;
}
const REPORT_LIST_CAP = 5000;  // 与服务端 recon.REPORT_LIST_CAP 一致，仅用于提示文案

function renderReport(rep) {
  const s = rep.summary;
  const chips = [
    ['渠道行数', s.channel_rows], ['库内行数', s.db_rows], ['一致', s.matched, 'ok'],
    ['渠道独有', s.channel_only, s.channel_only ? 'bad' : ''],
    ['库内独有', s.db_only, s.db_only ? 'bad' : ''],
    ['字段不符', s.mismatch, s.mismatch ? 'bad' : ''],
    ['同号冲突', s.conflict, s.conflict ? 'bad' : ''],
  ].map(([label, v, cls]) => `<span class="chip ${cls || ''}">${label} <b>${v}</b></span>`).join('');
  const tr = rep.truncated || {};
  document.getElementById('report-view').innerHTML = `
    <p class="hint">第 ${rep.seq} 版 · 任务 #${rep.task_id} · 发布于 ${fmtTime(rep.published_at)}</p>
    <div class="chips">${chips}</div>
    ${diffSection('渠道独有（库内缺失）', s.channel_only, tr.channel_only, simpleFlowTable(rep.channel_only, 'channel'))}
    ${diffSection('库内独有（渠道缺失）', s.db_only, tr.db_only, simpleFlowTable(rep.db_only, 'db'))}
    ${diffSection('字段不符', s.mismatch, tr.mismatch, mismatchTable(rep.mismatches))}
    ${diffSection('同号冲突（任一侧重复，不做配对）', s.conflict, tr.conflict, conflictTable(rep.conflicts))}
    ${s.channel_only + s.db_only + s.mismatch + s.conflict === 0
      ? '<div class="notice ok">两侧完全一致，无差异。</div>' : ''}`;
}

document.getElementById('upload-btn').onclick = async () => {
  const msg = document.getElementById('upload-msg');
  const dateVal = document.getElementById('recon-date').value;
  const fileInput = document.getElementById('recon-file');
  msg.className = 'msg';
  if (!dateVal) { msg.textContent = '请选择结算日'; msg.classList.add('err'); return; }
  if (!fileInput.files.length) { msg.textContent = '请选择 CSV 文件'; msg.classList.add('err'); return; }
  const fd = new FormData();
  fd.append('settle_date', dateVal);
  fd.append('file', fileInput.files[0]);
  msg.textContent = '上传中…';
  try {
    const res = await fetch('/api/recon/tasks', { method: 'POST', body: fd });
    const data = await res.json();
    if (res.ok) {
      msg.textContent = `任务 #${data.seq} 已提交，正在核对`;
      msg.classList.add('ok');
      fileInput.value = '';
    } else {
      msg.textContent = '提交失败：' + (data.detail || res.status);
      msg.classList.add('err');
    }
  } catch (e) {
    msg.textContent = '网络错误：' + e;
    msg.classList.add('err');
  }
  refreshRecon();
};

document.getElementById('order-btn').onclick = async () => {
  const msg = document.getElementById('order-msg');
  msg.className = 'msg';
  const body = {
    order_no: document.getElementById('ord-no').value.trim(),
    merchant_id: Number(document.getElementById('ord-merchant').value),
    amount: document.getElementById('ord-amount').value,
    fee: document.getElementById('ord-fee').value,
    settle_date: document.getElementById('ord-date').value,
  };
  if (!body.order_no || !body.merchant_id || body.amount === '' || body.fee === '' || !body.settle_date) {
    msg.textContent = '请完整填写五个字段'; msg.classList.add('err'); return;
  }
  try {
    const res = await fetch('/api/orders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (res.ok) {
      msg.textContent = `已补记 ${data.row.order_no}（商户 ${data.row.merchant_id}，金额 ${data.row.amount}，结算日 ${data.row.settle_date}）`;
      msg.classList.add('ok');
    } else {
      msg.textContent = '补记失败：' + (data.detail || res.status);
      msg.classList.add('err');
    }
  } catch (e) {
    msg.textContent = '网络错误：' + e;
    msg.classList.add('err');
  }
};

/* ================= SQL 计划对照（保留原功能） ================= */
const PRESETS = {
  skew: `SELECT m.merchant_id,
       m.merchant_name,
       COUNT(*)      AS order_cnt,
       SUM(o.amount) AS total_amount,
       SUM(o.fee)    AS total_fee
FROM settlement_orders o
JOIN merchants m ON m.merchant_id = o.merchant_id
WHERE left(m.category, 1) = 'A'
GROUP BY m.merchant_id, m.merchant_name
ORDER BY total_amount DESC`,
  single: `SELECT o.order_no, o.amount, o.fee, o.settle_date, m.merchant_name
FROM settlement_orders o
JOIN merchants m ON m.merchant_id = o.merchant_id
WHERE m.merchant_id = 42
  AND o.settle_date >= DATE '2026-11-01'
  AND o.settle_date <  DATE '2026-11-14'
ORDER BY o.settle_date DESC, o.order_no`,
  daily: `SELECT settle_date, COUNT(*) AS order_cnt, SUM(amount) AS total_amount
FROM settlement_orders
WHERE settle_date >= DATE '2026-11-01'
  AND settle_date <  DATE '2026-11-14'
GROUP BY settle_date
ORDER BY settle_date`,
};

const sqlInput = document.getElementById('sql-input');
sqlInput.value = PRESETS.skew;
document.getElementById('preset').onchange = (e) => {
  sqlInput.value = PRESETS[e.target.value] || '';
};

const expanded = new Set();   // 展开了计划详情的批次 id

const STATUS_TEXT = {
  pending: '排队中', running: '运行中', completed: '已完成',
  failed: '失败', timeout: '超时', cancelled: '已取消',
};
const SETTING_TEXT = {
  default: '默认 (enable_nestloop=on)',
  nestloop_off: '抑制嵌套循环 (enable_nestloop=off)',
};
const EX_STATUS_TEXT = {
  pending: '待执行', running: '执行中', ok: '成功',
  timeout: '超时', cancelled: '已取消', error: '报错',
};
const VERDICT_TEXT = {
  improved: '有改善', not_improved: '无改善', inconclusive: '无法判断改善',
};

async function refresh() {
  try {
    const res = await fetch('/api/batches');
    const data = await res.json();
    renderSlot(data);
    await renderBatches(data.batches);
  } catch (e) { /* 忽略瞬时网络错误，下一轮再试 */ }
}

function renderSlot(data) {
  const pill = document.getElementById('slot-pill');
  if (data.busy) {
    pill.textContent = '对照运行中（名额已占用）';
    pill.className = 'pill busy';
  } else {
    pill.textContent = '对照空闲，可提交';
    pill.className = 'pill idle';
  }
}

function conclusionHtml(b) {
  if (b.conclusion) {
    const v = b.conclusion.verdict;
    return `<div class="conclusion ${v}">
      <b>${VERDICT_TEXT[v] || v}</b> · ${escapeHtml(b.conclusion.message)}
    </div>`;
  }
  if (['failed', 'timeout', 'cancelled'].includes(b.status)) {
    const reason = { failed: '报错', timeout: '超时', cancelled: '已取消' }[b.status];
    return `<div class="conclusion none">无比较结论（批次${reason}）</div>`;
  }
  return '';
}

function executionsHtml(b) {
  if (!b.executions.length) {
    return b.status === 'running' ? '<p class="hint">正在建立事务快照…</p>' : '';
  }
  const rows = b.executions.map(ex => `<tr>
    <td>第 ${ex.round} 轮</td>
    <td>#${ex.seq}</td>
    <td>${SETTING_TEXT[ex.setting] || ex.setting}</td>
    <td><span class="ex-status ${ex.status}">${EX_STATUS_TEXT[ex.status] || ex.status}</span></td>
    <td>${fmtMs(ex.execution_time_ms)}</td>
    <td>${fmtMs(ex.planning_time_ms)}</td>
    <td>${ex.error ? escapeHtml(ex.error) : ''}</td>
  </tr>`).join('');
  return `<table class="ex-table">
    <thead><tr><th>轮次</th><th>顺序</th><th>设置</th><th>状态</th>
    <th>执行耗时</th><th>规划耗时</th><th>备注</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function batchCardHtml(b) {
  const cancelBtn = (b.status === 'running' || b.status === 'pending')
    ? (b.cancel_requested
        ? '<span class="msg warn">取消中…</span>'
        : `<button class="btn-cancel" data-cancel="${b.id}">取消本批</button>`)
    : '';
  const toggleBtn = b.executions.length
    ? `<button class="btn-toggle" data-toggle="${b.id}">${expanded.has(b.id) ? '收起计划' : '查看计划与结构'}</button>`
    : '';
  return `<div class="card batch">
    <div class="batch-head">
      <span class="badge ${b.status}">${STATUS_TEXT[b.status] || b.status}</span>
      <code>#${b.id}</code>
      <span class="hint">提交于 ${fmtTime(b.created_at)}</span>
      <span class="spacer"></span>
      ${cancelBtn}${toggleBtn}
    </div>
    <pre class="sql">${escapeHtml(b.sql)}</pre>
    ${b.error ? `<div class="conclusion none">错误：${escapeHtml(b.error)}</div>` : ''}
    ${conclusionHtml(b)}
    ${executionsHtml(b)}
    <div class="detail" id="detail-${b.id}"></div>
  </div>`;
}

async function renderBatches(batches) {
  const container = document.getElementById('batches');
  container.innerHTML = batches.length
    ? batches.map(batchCardHtml).join('')
    : '<p class="hint">暂无批次</p>';
  container.querySelectorAll('[data-cancel]').forEach(btn => {
    btn.onclick = () => cancelBatch(btn.dataset.cancel);
  });
  container.querySelectorAll('[data-toggle]').forEach(btn => {
    btn.onclick = () => toggleDetail(btn.dataset.toggle);
  });
  for (const id of expanded) {
    if (batches.some(b => b.id === id)) await loadDetail(id);
  }
}

async function cancelBatch(id) {
  await fetch(`/api/batches/${id}/cancel`, { method: 'POST' });
  refresh();
}

async function toggleDetail(id) {
  if (expanded.has(id)) {
    expanded.delete(id);
    const box = document.getElementById(`detail-${id}`);
    if (box) box.innerHTML = '';
  } else {
    expanded.add(id);
    await loadDetail(id);
  }
  refresh();
}

async function loadDetail(id) {
  const box = document.getElementById(`detail-${id}`);
  if (!box) return;
  try {
    const res = await fetch(`/api/batches/${id}`);
    if (!res.ok) { box.innerHTML = ''; return; }
    const d = await res.json();
    box.innerHTML = d.executions.map(ex => {
      const struct = (ex.structure_lines || []).join('\n');
      const plan = ex.plan ? JSON.stringify(ex.plan, null, 2) : '（无计划）';
      return `<div class="ex-detail">
        <h4>第 ${ex.round} 轮 · #${ex.seq} · ${SETTING_TEXT[ex.setting] || ex.setting} ·
            ${EX_STATUS_TEXT[ex.status] || ex.status}${ex.execution_time_ms != null ? ' · ' + fmtMs(ex.execution_time_ms) : ''}</h4>
        ${struct ? `<pre class="struct">${escapeHtml(struct)}</pre>` : ''}
        <details><summary>完整计划 JSON</summary><pre class="plan">${escapeHtml(plan)}</pre></details>
      </div>`;
    }).join('');
  } catch (e) { /* 下一轮再试 */ }
}

document.getElementById('run-btn').onclick = async () => {
  const msg = document.getElementById('submit-msg');
  msg.className = 'msg';
  msg.textContent = '提交中…';
  try {
    const res = await fetch('/api/batches', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql: sqlInput.value }),
    });
    if (res.status === 409) {
      msg.textContent = '繁忙：已有对照批次在运行，请稍后再试';
      msg.classList.add('warn');
    } else if (res.status === 400) {
      const d = await res.json();
      msg.textContent = '校验失败：' + (d.detail || '');
      msg.classList.add('err');
    } else if (res.ok) {
      msg.textContent = '批次已创建，开始运行';
      msg.classList.add('ok');
    } else {
      msg.textContent = '提交失败（HTTP ' + res.status + '）';
      msg.classList.add('err');
    }
  } catch (e) {
    msg.textContent = '网络错误：' + e;
    msg.classList.add('err');
  }
  refresh();
};

document.getElementById('insert-btn').onclick = async () => {
  const msg = document.getElementById('insert-msg');
  msg.className = 'msg';
  try {
    const res = await fetch('/api/sample-row', { method: 'POST' });
    const d = await res.json();
    if (d.ok) {
      msg.textContent = `已补记 ${d.row.order_no}（商户 ${d.row.merchant_id}，金额 ${d.row.amount}，结算日 ${d.row.settle_date}）`;
      msg.classList.add('ok');
    } else {
      msg.textContent = '写入失败';
      msg.classList.add('err');
    }
  } catch (e) {
    msg.textContent = '网络错误：' + e;
    msg.classList.add('err');
  }
};

refresh();
refreshRecon();
setInterval(refresh, 1500);
setInterval(refreshRecon, 1500);
