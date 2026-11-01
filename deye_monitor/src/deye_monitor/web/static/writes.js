// Write layer v1 in the web interface: the release per inverter (plant page) and the dialog
// "Einstellungen ändern" (inverter page). Writes only ever go through this dialog: fresh value,
// preview (old -> new, raw word, peers), explicit confirmation, result with read-back and peers.

import { ApiError, api } from './api.js';
import { t, errorText, localText } from './i18n.js';
import { h, modal, toast, busy } from './ui.js';
import { state, canEdit, loadPlant, mutate, reloadAfterPlantChange } from './state.js';

const CAPS = '5 kW 120 A · 6 kW 150 A · 8 kW 190 A · 10 kW 210 A · 12 kW 240 A';

export const writeConfig = (id) => {
  const writes = state.plant && state.plant.writes;
  return (writes && writes.inverters && writes.inverters[id]) || { allow_writes: false, operations: [] };
};

// Role of an inverter as the status view knows it: 'master', 'slave', 'single' or 'unknown'. In a
// parallel plant the master distributes these settings to every slave, so a slave is not written.
export function parallelRole(id) {
  const inv = state.inverters.find((i) => i.id === id);
  const role = inv && inv.status && inv.status.identity && inv.status.identity.role;
  if (!role) return 'unknown';
  if (!role.parallel_enable) return 'single';
  return role.master === true || role.master === 1 || role.master === 'master' ? 'master' : 'slave';
}

function masterLink() {
  const master = state.inverters.find((i) => parallelRole(i.id) === 'master');
  return master ? h('a', { href: `#/inv/${master.id}`, class: 'master-link' }, t('writes.slave_master_link', { name: master.name })) : null;
}

function slaveNotice() {
  return h('p', { class: 'banner info slave-locked', role: 'status' }, t('writes.slave_locked'), ' ', masterLink());
}

const catalogue = () => (state.plant && state.plant.writes && state.plant.writes.operations) || [];

function rangeText(op) {
  if (op.kind === 'bit') return t('writes.range.bit');
  if (op.current_cap) return t('writes.range.cap', { caps: CAPS });
  return t('writes.range.unit', { min: fmtNumber(op, op.min), max: fmtNumber(op, op.max), unit: op.unit });
}

function fmtNumber(op, value) {
  if (value === null || value === undefined) return '–';
  return op.unit === 'V' ? Number(value).toFixed(2) : String(value);
}

function fmtValue(op, value) {
  if (value === null || value === undefined) return '–';
  if (op.kind === 'bit') return t(`writes.bit.${value ? 1 : 0}`);
  return `${fmtNumber(op, value)} ${op.unit}`;
}

const hex = (word) => (word === null || word === undefined ? '–' : `0x${Number(word).toString(16).toUpperCase().padStart(4, '0')}`);
const when = (seconds) => (seconds ? new Date(seconds * 1000).toLocaleString(state.lang) : '');

// -- plant page: the release ------------------------------------------------------------------

export function writesSection(inv, { onChanged }) {
  const cfg = writeConfig(inv.id);
  const ops = catalogue();
  const enabled = new Set(cfg.operations);
  const master = h('input', { type: 'checkbox', id: `writes-allow-${inv.id}`, checked: cfg.allow_writes, disabled: !canEdit() });
  const slave = parallelRole(inv.id) === 'slave';
  const boxes = ops.map((op) => {
    const box = h('input', { type: 'checkbox', value: op.key, checked: enabled.has(op.key), disabled: !canEdit() || slave, 'data-operation': op.key });
    return h('label', { class: 'option write-op' },
      box,
      h('span', { class: 'opt-text' },
        h('span', { class: 'opt-title' }, localText(op.name)),
        h('span', { class: 'opt-help' }, t('writes.op_line', { register: op.register, range: rangeText(op) })),
        h('span', { class: 'opt-help risk' }, localText(op.risk))));
  });
  const message = h('div', { class: 'messages' });
  const save = h('button', { class: 'btn small primary', type: 'button', disabled: !canEdit() }, t('writes.save'));
  save.addEventListener('click', async () => {
    const chosen = boxes.map((b) => b.querySelector('input')).filter((i) => i.checked).map((i) => i.value);
    busy(save, true);
    message.replaceChildren();
    try {
      await mutate('PUT', `api/plant/inverters/${inv.id}/writes`, { allow_writes: master.checked, operations: chosen }, state.plant.revision);
      await reloadAfterPlantChange();
      toast(t('writes.saved'), 'ok');
      onChanged();
    } catch (error) {
      message.replaceChildren(h('p', { class: 'banner error', role: 'alert' }, errorText(error)));
      if (error instanceof ApiError && error.key === 'stale_revision') await loadPlant().then(onChanged).catch(() => {});
    }
    busy(save, false);
  });
  const summary = cfg.allow_writes && cfg.operations.length
    ? t('writes.summary_on', { n: cfg.operations.length })
    : t('writes.summary_off');
  return h('details', { class: 'writes-section', 'data-writes': inv.id, open: cfg.allow_writes || undefined },
    h('summary', {}, h('span', { class: 'writes-title' }, t('plant.inv.writes')), ' ', h('span', { class: `chip ${cfg.allow_writes && cfg.operations.length ? 'warn' : ''}` }, summary)),
    h('p', { class: 'help' }, t('writes.intro')),
    slave ? slaveNotice() : null,
    h('label', { class: 'option writes-master', for: `writes-allow-${inv.id}` }, master,
      h('span', { class: 'opt-text' }, h('span', { class: 'opt-title' }, t('writes.allow')), h('span', { class: 'opt-help' }, t('writes.allow_help')))),
    h('div', { class: 'write-ops' }, boxes),
    h('p', { class: 'note' }, t('writes.reset_note')),
    message,
    h('div', { class: 'actions left' }, save));
}

// -- inverter page: the entrance to the dialog ----------------------------------------------------

export function writesPanel(invId) {
  const root = h('section', { class: 'card writes-panel', 'aria-labelledby': 'writes-panel-title' });
  function paint() {
    const cfg = writeConfig(invId);
    const active = cfg.allow_writes && cfg.operations.length > 0;
    const role = parallelRole(invId);
    root.replaceChildren(
      h('h2', { id: 'writes-panel-title' }, t('writes.panel_title')),
      active
        ? h('p', { class: 'help' }, t('writes.panel_intro', { n: cfg.operations.length }))
        : h('p', { class: 'help' }, t('writes.panel_off'), ' ', h('a', { href: '#/plant' }, t('nav.plant'))),
      active && role === 'slave' ? slaveNotice() : null,
      active && role === 'master' ? h('p', { class: 'muted small master-hint' }, t('writes.master_hint')) : null,
      h('div', { class: 'actions left' },
        h('button', { class: 'btn', id: 'open-writes', type: 'button', disabled: !active || !canEdit() || role === 'slave', onClick: () => openSettingsDialog(invId) }, t('writes.panel_open'))));
  }
  paint();
  // the release is changed on the plant page: look at the stored plant again with every poll
  const refresh = () => { loadPlant().then(paint).catch(() => {}); };
  return { el: root, refresh };
}

// -- the dialog -----------------------------------------------------------------------------------

export function openSettingsDialog(invId) {
  const inv = state.inverters.find((i) => i.id === invId);
  const ops = new Map(catalogue().map((op) => [op.key, op]));
  const view = { rows: null, loadError: null, audit: [], preview: null, result: null, busy: false, error: null, expires: 0, lateDue: 0, rereading: null };
  let timer = null;
  let lateTimer = null;
  let body = null;

  const reasonText = (reason) => {
    const key = `writes.reason.${reason}`;
    return t(key) === key ? reason : t(key);
  };

  function errorBanner(error) {
    if (!error) return null;
    let text = errorText(error);
    if (error instanceof ApiError && (error.key === 'operation_locked' || error.key === 'inverter_not_ready')) text = t(`error.${error.key}`, { reason: reasonText(error.params.reason) });
    if (error instanceof ApiError && error.key === 'parallel_slave') text = t('writes.slave_locked');
    if (error instanceof ApiError && error.key === 'peer_unavailable') text = t('error.peer_unavailable', { peers: (error.params.peers || []).join(', ') });
    return h('p', { class: 'banner error', role: 'alert' }, text);
  }

  async function reload() {
    try {
      const [current, audit] = await Promise.all([
        mutate('POST', `api/inverters/${invId}/writes/current`, {}),
        api('GET', `api/inverters/${invId}/writes/operations`),
      ]);
      view.rows = current.operations;
      view.audit = audit.operations;
      view.loadError = null;
    } catch (error) {
      view.loadError = error;
    }
    paint();
  }

  async function runPreview(op, raw) {
    view.error = null;
    view.busy = true;
    paint();
    try {
      const plant = await loadPlant();
      const value = op.kind === 'bit' ? Number(raw) : String(raw).trim().replace(',', '.');
      const data = await mutate('POST', `api/inverters/${invId}/writes/preview`, { operation: op.key, value }, plant.revision);
      view.preview = data.preview;
      view.expires = Date.now() + data.preview.expires_in_s * 1000;
      clearInterval(timer);
      timer = setInterval(tick, 1000);
    } catch (error) {
      view.error = error;
    }
    view.busy = false;
    paint();
  }

  function tick() {
    if (!view.preview) { clearInterval(timer); return; }
    const left = document.querySelector('.write-expiry');
    const seconds = Math.max(0, Math.ceil((view.expires - Date.now()) / 1000));
    if (left) left.textContent = seconds > 0 ? t('writes.preview.expires', { s: seconds }) : t('writes.preview.expired');
    const go = document.querySelector('#write-confirm');
    if (go && seconds === 0) go.disabled = true;
  }

  async function runConfirm() {
    const preview = view.preview;
    view.error = null;
    view.busy = true;
    paint();
    try {
      const data = await mutate('POST', `api/writes/operations/${preview.operation_id}/confirm`, {}, preview.plant_revision);
      view.result = data.operation;
      view.preview = null;
      clearInterval(timer);
      watchLate();
    } catch (error) {
      view.error = error;
      if (error instanceof ApiError && ['permit_expired', 'unknown_operation', 'operation_already_used', 'stale_revision'].includes(error.key)) view.preview = null;
    }
    view.busy = false;
    paint();
    reload();
  }

  // The record of an operation changed (later observation, read again): result and log follow.
  function applyOperation(operation) {
    if (view.result && view.result.operation_id === operation.operation_id) view.result = operation;
    view.audit = view.audit.map((o) => (o.operation_id === operation.operation_id ? operation : o));
    paint();
    watchLate();
  }

  // The second peer observation arrives on its own: count down, then fetch the record until it is in.
  function watchLate() {
    clearInterval(lateTimer);
    const late = view.result && view.result.late_observation;
    if (!late || late.status !== 'pending') return;
    const operationId = view.result.operation_id;
    view.lateDue = Date.now() + late.in_s * 1000;
    let fetching = false;
    lateTimer = setInterval(async () => {
      const left = Math.max(0, Math.ceil((view.lateDue - Date.now()) / 1000));
      for (const el of document.querySelectorAll('.late-countdown')) el.textContent = t('writes.peer.late_wait', { s: left });
      if (left > 0 || fetching) return;
      fetching = true;
      try {
        const data = await api('GET', `api/writes/operations/${operationId}`);
        if (view.result && view.result.operation_id === operationId && data.operation.late_observation.status !== 'pending') applyOperation(data.operation);
      } catch (error) { /* the next tick asks again */ }
      fetching = false;
    }, 1000);
  }

  async function runReread(operationId) {
    view.error = null;
    view.rereading = operationId;
    paint();
    try {
      const data = await mutate('POST', `api/writes/operations/${operationId}/peers`, {});
      view.rereading = null;
      applyOperation(data.operation);
      return;
    } catch (error) {
      view.error = error;
    }
    view.rereading = null;
    paint();
  }

  async function runVerify(operationId, verdict) {
    try {
      const data = await mutate('POST', `api/writes/operations/${operationId}/verification`, { state: verdict, note: '' });
      view.result = data.operation;
    } catch (error) {
      view.error = error;
    }
    paint();
    reload();
  }

  // -- pieces -----------------------------------------------------------------------------------

  function lateCell(op, p, late) {
    if (!late) return h('td', {}, '–');
    if (late.status === 'pending') return h('td', { class: 'late pending' }, h('span', { class: 'late-countdown' }, t('writes.peer.late_wait', { s: Math.max(0, Math.ceil((view.lateDue - Date.now()) / 1000)) })));
    if (late.status !== 'done') return h('td', { class: 'late' }, t('writes.peer.late_missing'));
    const word = p.late_word;
    return h('td', { class: `late late-state ${p.late_state}` },
      word === null || word === undefined ? t('writes.peer.unavailable') : `${fmtValue(op, p.late_value)} (${hex(word)}) · ${t(`writes.peer.${p.late_state}`)}`);
  }

  function peerTable(op, peers, withLater, late) {
    if (!peers.length) return h('p', { class: 'muted' }, t('writes.peers_none'));
    return h('table', { class: 'peer-table' },
      h('thead', {}, h('tr', {}, h('th', {}, t('writes.peer.name')), h('th', {}, t('writes.peer.baseline')), withLater ? h('th', {}, t('writes.peer.later')) : null, withLater ? h('th', {}, t('writes.peer.state')) : null, withLater ? h('th', {}, t('writes.peer.late', { s: (late && late.after_s) || 30 })) : null)),
      h('tbody', {}, peers.map((p) => h('tr', { 'data-peer': p.id },
        h('th', { scope: 'row' }, p.name || p.id),
        h('td', {}, p.baseline_word === null || p.baseline_word === undefined ? t('writes.peer.unavailable') : `${fmtValue(op, p.baseline_value)} (${hex(p.baseline_word)})`),
        withLater ? h('td', {}, p.later_word === null || p.later_word === undefined ? '–' : `${fmtValue(op, p.later_value)} (${hex(p.later_word)})`) : null,
        withLater ? h('td', { class: `peer-state ${p.state}` }, t(`writes.peer.${p.state}`)) : null,
        withLater ? lateCell(op, p, late) : null))));
  }

  function rereadButton(o) {
    if (!(o.peers || []).length || ['rejected', 'pending'].includes(o.status)) return null;
    return h('button', { class: 'btn small', type: 'button', 'data-reread': o.operation_id, disabled: view.rereading === o.operation_id, onClick: () => runReread(o.operation_id) }, t('writes.reread'));
  }

  function rereadsView(op, r) {
    const reads = (r.peer_rereads || []).slice(-3).reverse();
    if (!reads.length) return null;
    const names = new Map((r.peers || []).map((p) => [p.id, p.name || p.id]));
    return h('div', { class: 'rereads' }, reads.map((read) => h('div', { class: 'reread', 'data-reread-at': String(read.at) },
      h('p', { class: 'muted small' }, t('writes.reread.at', { time: when(read.at) })),
      h('ul', {}, read.peers.map((p) => h('li', { 'data-peer': p.id }, `${names.get(p.id) || p.id}: `,
        p.word === null || p.word === undefined ? t('writes.peer.unavailable') : `${fmtValue(op, p.value)} (${hex(p.word)}) · ${t(`writes.peer.${p.state}`)}`))))));
  }

  function rowView(row) {
    const op = ops.get(row.key);
    if (!op) return null;
    const locked = row.state !== 'ready';
    const id = `write-input-${row.key}`;
    const input = op.kind === 'bit'
      ? h('select', { id, disabled: locked || view.busy }, h('option', { value: '1' }, t('writes.bit.1')), h('option', { value: '0' }, t('writes.bit.0')))
      : h('input', { id, type: 'text', inputmode: 'decimal', autocomplete: 'off', disabled: locked || view.busy, 'aria-describedby': `${id}-help` });
    const go = h('button', { class: 'btn small', type: 'button', disabled: locked || view.busy || !canEdit(), 'data-preview': row.key }, t('writes.preview_btn'));
    go.addEventListener('click', () => runPreview(op, input.value));
    return h('article', { class: `write-row ${locked ? 'locked' : ''}`, 'data-operation': row.key },
      h('h3', {}, localText(op.name)),
      locked
        ? h('p', { class: 'banner warn' }, t('writes.locked', { reason: reasonText(row.reason) }))
        : h('p', { class: 'current' }, t('writes.current'), ': ', h('strong', { class: 'current-value' }, fmtValue(op, row.value)), ` (${hex(row.word)})`),
      h('p', { class: 'muted small', id: `${id}-help` }, row.cap ? t('writes.range.limit', { max: row.cap, unit: op.unit }) : rangeText(op)),
      h('p', { class: 'muted small risk' }, localText(op.risk)),
      locked ? null : h('div', { class: 'write-edit' }, h('label', { for: id }, t('writes.input', { unit: op.unit || '' })), input, go));
  }

  function previewView(p) {
    const op = ops.get(p.operation.key);
    const role = p.inverter.role || {};
    const roleText = role.parallel === null || role.parallel === undefined ? t('value.unknown') : role.parallel ? (role.master ? t('role.master') : t('role.slave')) : t('role.single');
    const go = h('button', { class: 'btn danger', id: 'write-confirm', type: 'button', disabled: view.busy, onClick: runConfirm }, t('writes.confirm_btn'));
    return h('section', { class: 'write-preview', 'aria-labelledby': 'write-preview-title' },
      h('h3', { id: 'write-preview-title' }, t('writes.preview.title')),
      h('p', { class: 'banner warn' }, t('writes.scope')),
      h('dl', { class: 'facts' },
        h('div', { class: 'fact' }, h('dt', {}, t('writes.preview.target')), h('dd', {}, `${inv ? inv.name : p.inverter.name} · ${p.inverter.serial} · ${roleText}`)),
        h('div', { class: 'fact' }, h('dt', {}, localText(p.operation.name)), h('dd', { class: 'change' }, t('writes.preview.change', { old: fmtValue(op, p.old_value), new: fmtValue(op, p.new_value) }))),
        h('div', { class: 'fact' }, h('dt', {}, t('writes.preview.raw_title')), h('dd', {}, t('writes.preview.raw', { old: p.old_word_hex, new: p.new_word_hex, mask: p.mask_hex })))),
      h('p', { class: 'muted small risk' }, localText(p.operation.risk)),
      h('h4', {}, t('writes.preview.peers')),
      peerTable(op, p.peers, false),
      role.parallel && role.master && p.peers.length ? h('p', { class: 'muted small master-hint' }, t('writes.master_hint')) : null,
      h('p', { class: 'muted small write-expiry', 'aria-live': 'off' }, t('writes.preview.expires', { s: Math.max(0, Math.ceil((view.expires - Date.now()) / 1000)) })),
      h('div', { class: 'actions' },
        h('button', { class: 'btn', type: 'button', disabled: view.busy, onClick: () => { view.preview = null; clearInterval(timer); paint(); } }, t('common.cancel')),
        go));
  }

  function resultView(r) {
    const op = ops.get(r.operation);
    const good = r.status === 'readback_confirmed';
    const target = r.target;
    const verification = r.verification;
    const verify = (verdict) => h('button', { class: 'btn small', type: 'button', 'data-verify': verdict, onClick: () => runVerify(r.operation_id, verdict) }, t(`writes.verify.${verdict}`));
    return h('section', { class: 'write-result', 'aria-labelledby': 'write-result-title', 'data-status': r.status },
      h('h3', { id: 'write-result-title' }, t('writes.result.title')),
      h('p', { class: `banner ${good ? 'ok' : r.status === 'rejected' ? 'info' : 'warn'}`, role: 'status' },
        h('strong', { class: 'write-status' }, t(`writes.status.${r.status}`)), ' ', t(`writes.status_help.${r.status}`)),
      r.reason ? h('p', { class: 'muted small' }, t('writes.result.reason', { reason: reasonText(String(r.reason).split(':')[0]) })) : null,
      h('p', { class: 'change' }, h('strong', {}, op ? localText(op.name) : r.operation), ': ', t('writes.preview.change', { old: fmtValue(op, r.old_value), new: fmtValue(op, r.requested_value) })),
      target ? h('p', { class: 'readback' }, t('writes.result.readback', { value: target.word === null ? '–' : fmtValue(op, target.value), word: hex(target.word) })) : null,
      r.attribution === 'not_established' ? h('p', { class: 'banner warn' }, t('writes.result.unknown_note')) : null,
      h('h4', {}, t('writes.result.peers')),
      peerTable(op, r.peers || [], true, r.late_observation),
      (r.peers || []).length ? h('p', { class: 'muted small' }, t('writes.peer.late_note', { s: (r.late_observation && r.late_observation.after_s) || 30 })) : null,
      parallelRole(r.inverter) === 'master' && (r.peers || []).length ? h('p', { class: 'muted small master-hint' }, t('writes.master_hint')) : null,
      h('div', { class: 'actions left' }, rereadButton(r)),
      rereadsView(op, r),
      h('p', { class: 'note' }, t('writes.result.limits')),
      r.drift ? h('p', { class: 'banner warn' }, t('writes.result.drift', { word: hex(r.drift.word) })) : null,
      r.status === 'rejected'
        ? null
        : h('div', { class: 'verify' },
          verification ? h('p', { class: 'muted' }, t('writes.verify.recorded', { state: t(`writes.verify.${verification.state}`) })) : h('p', { class: 'muted small' }, t('writes.verify.ask')),
          h('div', { class: 'actions left' }, verify('confirmed'), verify('not_confirmed'))));
  }

  function auditView() {
    if (!view.audit.length) return h('p', { class: 'muted' }, t('writes.audit.empty'));
    return h('table', { class: 'audit-table' },
      h('thead', {}, h('tr', {}, h('th', {}, t('writes.audit.time')), h('th', {}, t('writes.audit.operation')), h('th', {}, t('writes.audit.change')), h('th', {}, t('writes.audit.status')), h('th', {}, ''))),
      h('tbody', {}, view.audit.slice(0, 10).map((o) => {
        const op = ops.get(o.operation);
        return h('tr', { 'data-operation-id': o.operation_id },
          h('td', {}, when(o.created_at)),
          h('td', {}, op ? localText(op.name) : o.operation),
          h('td', {}, `${hex(o.old_word)} → ${hex(o.new_word)}`),
          h('td', {}, t(`writes.status.${o.status}`)),
          h('td', {}, rereadButton(o)));
      })));
  }

  function paint() {
    if (!body) return;
    const parts = [];
    parts.push(h('p', { class: 'banner info write-scope' }, t('writes.scope')));
    if (view.loadError) parts.push(errorBanner(view.loadError));
    if (view.error) parts.push(errorBanner(view.error));
    if (view.preview) parts.push(previewView(view.preview));
    else if (view.rows === null && !view.loadError) parts.push(h('p', { class: 'loading', role: 'status' }, t('writes.loading')));
    else if (view.rows) {
      if (view.result) parts.push(resultView(view.result));
      parts.push(h('div', { class: 'write-rows' }, view.rows.map(rowView)));
    }
    parts.push(h('h3', {}, t('writes.audit.title')), auditView());
    body.replaceChildren(...parts.filter(Boolean));
  }

  return modal({
    title: t('writes.dialog_title', { name: inv ? inv.name : invId }),
    wide: true,
    onClose: () => { clearInterval(timer); clearInterval(lateTimer); },
    build() {
      body = h('div', { class: 'modal-body writes-dialog' });
      paint();
      reload();
      return body;
    },
  });
}
