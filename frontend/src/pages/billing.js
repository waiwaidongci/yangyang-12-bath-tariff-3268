import { get, post } from '../api/client.js';
import { table } from '../components/common/data.js';
import { intro } from '../components/common/frame.js';
import { formError } from '../components/common/forms.js';
import { AdjustmentStatus, Role, bandName, statusName } from '../constants/enums.js';
import { bindForm, notify } from '../hooks/actions.js';
import { esc, timeLabel } from '../utils/html.js';

const yuan = value => `${Number(value).toFixed(2)} 元`;
const signedYuan = value => `${Number(value) >= 0 ? '+' : ''}${Number(value).toFixed(2)} 元`;
const signedVolume = value => `${Number(value) >= 0 ? '+' : ''}${Number(value).toFixed(3)}`;
const adjustmentStatus = row => `<span class="status status-${esc(row.status)}">${esc(statusName[row.status] || row.status)}</span>`;

function reconcileBadge(detail) {
  return detail.reconciled
    ? '<span class="status status-approved">勾稽一致</span>'
    : '<span class="status status-failed">勾稽有差额</span>';
}

async function listPage(user) {
  const bills = await get('/bills');
  const columns = [
    {label: '账单', html: row => `<a href="#billing/${row.id}">${esc(row.title)}</a>`},
    {label: '来源分析', html: row => `<a href="#result/${row.run_id}">#${row.run_id}</a>`},
    {label: '原账单金额', html: row => yuan(row.original_total_yuan)},
    {label: '最新重算额', html: row => yuan(row.latest_total_yuan)},
    {label: '待结补差', html: row => signedYuan(row.pending_adjustment_yuan)},
    {label: '已交结算员', html: row => signedYuan(row.submitted_adjustment_yuan)},
    {label: '已结补差', html: row => signedYuan(row.settled_adjustment_yuan)},
  ];
  const create = user.role === Role.ANALYST
    ? `<section class="panel"><div class="panel-heading"><h2>按已完成分析建立热水账单</h2></div>
      <form id="bill-form" class="panel-body"><label class="field"><span>分析编号</span><input name="run_id" type="number" required min="1" placeholder="在「结果与复核」中确认的分析编号"></label>
      <label class="field"><span>账单名称（可选）</span><input name="title" maxlength="120" placeholder="留空时自动命名"></label>${formError}
      <button class="primary">建立账单并冻结原费率</button></form></section>` : '';
  return `${intro('费用补差', '晚到表底只拆开原区间、沿用原费率重算；原账单保留，补差按池区和时段追溯到表底。')}
    ${create}
    <section class="panel"><div class="panel-heading"><h2>热水账单</h2></div>
    ${table(columns, bills, '还没有账单。分析员可基于一份成功的分析建立账单。')}</section>`;
}

function deltaTable(detail) {
  const bands = ['valley', 'flat', 'peak'];
  const rows = detail.adjustments.flatMap(adjustment =>
    adjustment.delta.rows.map(row => ({...row, seq: adjustment.seq})));
  const columns = [
    {label: '补差批次', html: row => `#${row.seq}`},
    {key: 'pool_zone', label: '池区'},
    ...bands.flatMap(band => [
      {label: `${bandName[band]}用量差（m³）`, html: row => signedVolume(row[`${band}_volume`])},
      {label: `${bandName[band]}费用差（元）`, html: row => signedYuan(row[`${band}_cost`])},
    ]),
    {label: '费用小计差', html: row => signedYuan(row.total_cost)},
  ];
  return table(columns, rows, '补差不涉及池区或时段费用变化。');
}

function readingTable(detail) {
  return table([
    {key: 'meter_code', label: '表计'}, {key: 'pool_zone', label: '池区'},
    {label: '表底时刻', html: row => `${esc(row.reading_date)} ${String(row.reading_minute).padStart(4, '0')}`},
    {label: '累计表底（m³）', html: row => Number(row.reading_m3).toFixed(3)},
    {label: '归入补差', html: row => row.adjustment_id ? `#${row.adjustment_id}` : '待结'},
    {key: 'note', label: '备注'},
  ], detail.late_readings, '还没有晚到表底。');
}

function adjustmentTable(detail, user) {
  const columns = [
    {label: '补差批次', html: row => `#${row.seq}`},
    {label: '状态', html: adjustmentStatus},
    {label: '金额', html: row => signedYuan(row.amount_yuan)},
    {label: '来源表底数', html: row => row.late_readings.length},
    {label: '提交人', key: 'submitted_by'},
    {label: '提交时间', html: row => timeLabel(row.submitted_at)},
    {label: '结清时间', html: row => timeLabel(row.settled_at)},
  ];
  if (user.role === Role.SUPERVISOR) {
    columns.push({
      label: '结账',
      html: row => row.status === AdjustmentStatus.SUBMITTED
        ? `<button data-settle="${row.id}" data-mode="fail">模拟结账失败</button>
           <button class="primary" data-settle="${row.id}" data-mode="ok">结账（幂等重试）</button>`
        : '—',
    });
  }
  return table(columns, detail.adjustments, '暂无补差批次。');
}

function attemptsTable(detail) {
  if (!detail.settlement_attempts.length) return '';
  return `<section class="panel"><div class="panel-heading"><h2>结账尝试（失败重试不重复入账）</h2></div>
    ${table([
      {label: '尝试号', html: row => `#${row.id}`},
      {label: '补差', html: row => `#${row.adjustment_id}`},
      {label: '结果', html: row => `<span class="status status-${esc(row.outcome)}">${esc(statusName[row.outcome])}</span>`},
      {key: 'idempotency_key', label: '幂等键'}, {key: 'detail', label: '说明'},
      {key: 'actor', label: '操作人'}, {label: '时间', html: row => timeLabel(row.at)},
    ], detail.settlement_attempts)}</section>`;
}

async function detailPage(id, user) {
  const detail = await get(`/bills/${id}`);
  const canEdit = user.role === Role.ANALYST;
  const hasPending = detail.adjustments.some(a => a.status === AdjustmentStatus.PENDING);
  const readingForm = canEdit ? `<details class="panel-body" style="display:block"><summary>补录晚到表底</summary>
    <form id="reading-form"><div class="form-grid">
      <label class="field"><span>表计编号</span><input name="meter_code" required maxlength="60" placeholder="XQ-R01"></label>
      <label class="field"><span>表底日期</span><input name="reading_date" type="date" required></label>
      <label class="field"><span>日内分钟（0–1439）</span><input name="reading_minute" type="number" required min="0" max="1439"></label>
      <label class="field"><span>累计表底（m³）</span><input name="reading_m3" type="number" step="any" required min="0"></label>
      <label class="field"><span>备注</span><input name="note" maxlength="200"></label>
    </div>${formError}<button class="primary">登记并重算待结补差</button></form></details>` : '';
  const submitBar = canEdit && hasPending
    ? `<div class="panel-body"><form id="submit-form"><button class="primary">把当前待结补差交给结算员（金额冻结）</button></form></div>` : '';
  return `${intro(detail.title, `原账单 ${yuan(detail.original_total_yuan)} · 最新重算 ${yuan(detail.latest_total_yuan)} · 待结余额 ${signedYuan(detail.balance_due_yuan)}`, reconcileBadge(detail))}
    <section class="split result-summary"><section class="conclusion">
      <h2>勾稽口径</h2>
      <p>原账单 ${yuan(detail.original_total_yuan)} ＋ 已结 ${signedYuan(detail.settled_adjustment_yuan)}
         ＋ 已交结算员 ${signedYuan(detail.submitted_adjustment_yuan)}
         ＋ 待结 ${signedYuan(detail.pending_adjustment_yuan)} ＝ 最新重算 ${yuan(detail.latest_total_yuan)}</p>
      <dl class="metrics">
        <div><dt>原账单金额</dt><dd>${yuan(detail.original_total_yuan)}</dd></div>
        <div><dt>最新重算额</dt><dd>${yuan(detail.latest_total_yuan)}</dd></div>
        <div><dt>待结余额</dt><dd>${signedYuan(detail.balance_due_yuan)}</dd></div>
        <div><dt>已结补差</dt><dd>${signedYuan(detail.settled_adjustment_yuan)}</dd></div>
      </dl></section>
      <aside class="method"><h2>口径说明</h2><p>晚到表底落在原区间内部时按均匀流速拆成两段，费率沿用账单冻结值；已经交给结算员的批次金额不再随后续表底变化。</p></aside></section>
    <section class="panel"><div class="panel-heading"><h2>晚到表底</h2>${canEdit ? '<a class="text-link" href="#billing">返回账单列表 →</a>' : ''}</div>
      ${readingForm}${readingTable(detail)}</section>
    <section class="panel"><div class="panel-heading"><h2>补差批次（按池区×时段）</h2></div>
      ${adjustmentTable(detail, user)}${submitBar}
      <div class="panel-body">${deltaTable(detail)}</div></section>
    ${attemptsTable(detail)}`;
}

export async function billingPage(spec, user, route) {
  if (!route.id) return listPage(user);
  const id = Number(route.id);
  if (!Number.isSafeInteger(id) || id <= 0) throw new Error('账单编号无效');
  return detailPage(id, user);
}

export function wireBilling(root, refresh, route) {
  bindForm(root.querySelector('#bill-form'), async form => {
    const values = Object.fromEntries(new FormData(form));
    const bill = await post('/bills', {run_id: Number(values.run_id), title: values.title || ''});
    notify('账单已建立，原费率已冻结');
    location.hash = `#billing/${bill.id}`;
  });
  bindForm(root.querySelector('#reading-form'), async form => {
    const values = Object.fromEntries(new FormData(form));
    const id = route.id;
    await post(`/bills/${id}/late-readings`, {
      meter_code: values.meter_code, reading_date: values.reading_date,
      reading_minute: Number(values.reading_minute), reading_m3: Number(values.reading_m3),
      note: values.note || '',
    });
    notify('晚到表底已补录，待结补差已重算');
    refresh();
  });
  bindForm(root.querySelector('#submit-form'), async () => {
    await post(`/bills/${route.id}/submit`, {});
    notify('补差金额已冻结并交给结算员');
    refresh();
  });
  root.querySelectorAll('[data-settle]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    const adjustmentId = button.dataset.settle;
    const fail = button.dataset.mode === 'fail';
    // Deterministic per batch and action: repeating a click replays instead of posting twice.
    const key = `settle-${adjustmentId}-${fail ? 'fail' : 'ok'}`;
    try {
      const result = await post(`/bills/adjustments/${adjustmentId}/settle`,
        {idempotency_key: key, force_failure: fail});
      const replay = result.idempotent_replay ? '（重复请求，未重复入账）' : '';
      notify(`${fail ? '结账失败已记录' : '结账成功'}：${result.amount_yuan} 元${replay}`);
      refresh();
    } catch (err) {
      notify(err.message, true);
      button.disabled = false;
    }
  }));
}
