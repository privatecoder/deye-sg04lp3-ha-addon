// Forms of the plant setup: connection, inverter and the read-only connection test.
// The server validates everything again; the checks here only save a round trip.

import { t, tOr, errorText } from './i18n.js';
import { h, uid, busy } from './ui.js';
import { ApiError, api } from './api.js';
import { state, mutate, reloadAfterPlantChange } from './state.js';

export const TRANSPORTS = ['modbus_tcp', 'rtu_over_tcp', 'solarman_v5_tcp', 'serial_rtu'];
const DEFAULT_PORT = { modbus_tcp: 502, rtu_over_tcp: 502, solarman_v5_tcp: 8899 };
const ID_RE = /^[a-z0-9_]{1,32}$/;
const SUGGESTED_ID = { modbus_tcp: 'gw', rtu_over_tcp: 'gw', solarman_v5_tcp: 'logger', serial_rtu: 'usb' };

// -- small helpers -------------------------------------------------------------------------------

function numberOf(input) {
  const text = input.value.trim();
  if (text === '') return undefined;
  const value = Number(text);
  return Number.isNaN(value) ? text : value;
}

// A labelled field with help text and an error slot.
function field(name, label, control, help) {
  const controlId = control.id || uid(name);
  control.id = controlId;
  const helpId = `${controlId}-help`;
  const errorId = `${controlId}-error`;
  const error = h('p', { class: 'field-error', id: errorId, role: 'alert', hidden: true });
  const describedBy = [help ? helpId : null, errorId].filter(Boolean).join(' ');
  control.setAttribute('aria-describedby', describedBy);
  const el = h('div', { class: 'field wide', 'data-field': name },
    h('label', { for: controlId }, label),
    control,
    help ? h('p', { class: 'help', id: helpId }, help) : null,
    error);
  return {
    name,
    el,
    control,
    setError(message) {
      error.textContent = message || '';
      error.hidden = !message;
      if (message) control.setAttribute('aria-invalid', 'true');
      else control.removeAttribute('aria-invalid');
    },
  };
}

function textInput(value, props = {}) {
  return h('input', { type: 'text', class: 'select', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', value: value ?? '', ...props });
}

function numberInput(value, props = {}) {
  return h('input', { type: 'text', inputmode: 'decimal', class: 'select', autocomplete: 'off', value: value ?? '', ...props });
}

function slug(text) {
  return text.toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 32);
}

function nextId(prefix, taken) {
  for (let n = 1; n < 100; n += 1) {
    if (!taken.includes(`${prefix}${n}`)) return `${prefix}${n}`;
  }
  return prefix;
}

function uniqueId(base, taken) {
  const root = base || 'x';
  if (!taken.includes(root)) return root;
  for (let n = 2; n < 100; n += 1) {
    const candidate = `${root.slice(0, 29)}_${n}`;
    if (!taken.includes(candidate)) return candidate;
  }
  return root;
}

// Server findings -> the fields of a form. Returns the text for anything that has no field.
export function applyServerError(error, fields) {
  for (const f of fields.values()) f.setError('');
  if (!(error instanceof ApiError)) return errorText(error);
  if (error.key === 'invalid_fields') {
    const rest = [];
    for (const item of error.params.fields || []) {
      const name = String(item.field || '').split('.')[0];
      const f = fields.get(name);
      const text = tOr(`fielderr.${item.type}`, 'fielderr.generic', { msg: item.msg });
      if (f) f.setError(text);
      else rest.push(item.msg);
    }
    return rest.length ? `${errorText(error)} ${rest.join('; ')}` : errorText(error);
  }
  if (error.key === 'invalid_plant') {
    return (error.params.problems || []).map((p) => tOr(`plantproblem.${p.code}`, 'form.general', p.params)).join(' ');
  }
  return errorText(error);
}

// -- connection form ------------------------------------------------------------------------------

export function transportLabel(transport) {
  return t(`ep.t.${transport}`);
}

export function endpointTarget(ep) {
  if (ep.transport === 'serial_rtu') return t('plant.target.serial', { device: ep.serial_device, baud: ep.baudrate || 9600 });
  if (ep.transport === 'solarman_v5_tcp') return t('plant.target.logger', { host: ep.host, port: ep.port, serial: ep.logger_serial });
  return t('plant.target.tcp', { host: ep.host, port: ep.port });
}

export function endpointForm({ initial = null, editing = false, existingIds = [], serialDevices = [] }) {
  const f = initial || {};
  let transport = f.transport || null;
  const fields = new Map();
  const register = (x) => { fields.set(x.name, x); return x; };

  const idInput = textInput(f.id || '', { maxlength: 32 });
  if (editing) idInput.readOnly = true;
  let idTouched = editing;
  idInput.addEventListener('input', () => { idTouched = true; });
  const idField = register(field('id', t('ep.id'), idInput, t('ep.id_help')));

  const hostField = register(field('host', t('ep.host'), textInput(f.host || '', { inputmode: 'url' }), t('ep.host_help')));
  const portField = register(field('port', t('ep.port'), numberInput(f.port ?? '')));
  const loggerField = register(field('logger_serial', t('ep.logger_serial'), numberInput(f.logger_serial ?? ''), t('ep.logger_serial_help')));
  const deviceSelect = h('select', { class: 'select' },
    h('option', { value: '' }, t('ep.serial_device_pick')),
    serialDevices.map((d) => h('option', { value: d, selected: d === f.serial_device }, d)));
  // keep a device that is configured but not (any longer) listed, so that an edit does not drop it
  if (f.serial_device && !serialDevices.includes(f.serial_device)) {
    deviceSelect.append(h('option', { value: f.serial_device, selected: true }, f.serial_device));
  }
  const deviceField = register(field('serial_device', t('ep.serial_device'), deviceSelect, t('ep.serial_device_help')));
  const noDevices = h('p', { class: 'banner warn', role: 'status' }, t('ep.serial_device_none'));
  const baudField = register(field('baudrate', t('ep.baudrate'), numberInput(f.baudrate ?? 9600), t('ep.baudrate_help')));

  const advanced = {
    timeout_s: register(field('timeout_s', t('ep.timeout_s'), numberInput(f.timeout_s ?? 5))),
    rtu_late_window_s: register(field('rtu_late_window_s', t('ep.rtu_late_window_s'), numberInput(f.rtu_late_window_s ?? ''), t('ep.rtu_late_window_s_help'))),
    max_read_registers: register(field('max_read_registers', t('ep.max_read_registers'), numberInput(f.max_read_registers ?? 60), t('ep.max_read_registers_help'))),
    request_gap_ms: register(field('request_gap_ms', t('ep.request_gap_ms'), numberInput(f.request_gap_ms ?? 50))),
    queue_size: register(field('queue_size', t('ep.queue_size'), numberInput(f.queue_size ?? 32))),
    bus_id: register(field('bus_id', t('ep.bus_id'), textInput(f.bus_id || '', { maxlength: 32 }), t('ep.bus_id_help'))),
  };

  // transport choice: one radio card per transport, with the explanation of when to choose it
  const groupName = uid('transport');
  const radios = TRANSPORTS.map((value) => {
    const input = h('input', { type: 'radio', name: groupName, value, checked: value === transport });
    input.addEventListener('change', () => { transport = value; refresh(); });
    return h('label', { class: 'option transport-option', 'data-transport': value },
      input,
      h('span', { class: 'opt-text' },
        h('span', { class: 'opt-title' }, t(`ep.t.${value}`)),
        h('span', { class: 'opt-help' }, t(`ep.t.${value}_when`))));
  });
  const transportSet = h('fieldset', { class: 'feature' },
    h('legend', {}, t('ep.transport')),
    h('p', { class: 'help' }, t('ep.transport_help')),
    h('div', { class: 'options one-col' }, radios));
  const transportError = h('p', { class: 'field-error', role: 'alert', hidden: true }, t('ep.choose_first'));

  const details = h('details', { class: 'advanced' },
    h('summary', {}, t('ep.advanced')),
    h('p', { class: 'help' }, t('ep.advanced_help')),
    h('div', { class: 'grid-fields' }, Object.values(advanced).map((x) => x.el)));

  const detail = h('div', { class: 'transport-fields' },
    hostField.el, portField.el, loggerField.el, deviceField.el, noDevices, baudField.el);

  function refresh() {
    const chosen = transport !== null;
    detail.hidden = !chosen;
    details.hidden = !chosen;
    idField.el.hidden = !chosen;
    transportError.hidden = true;
    const tcp = chosen && transport !== 'serial_rtu';
    hostField.el.hidden = !tcp;
    portField.el.hidden = !tcp;
    loggerField.el.hidden = transport !== 'solarman_v5_tcp';
    deviceField.el.hidden = transport !== 'serial_rtu';
    noDevices.hidden = !(transport === 'serial_rtu' && serialDevices.length === 0);
    baudField.el.hidden = transport !== 'serial_rtu';
    advanced.rtu_late_window_s.el.hidden = !(transport === 'rtu_over_tcp' || transport === 'serial_rtu');
    if (tcp && DEFAULT_PORT[transport] !== undefined) {
      const known = Object.values(DEFAULT_PORT).map(String);
      const port = portField.control;
      if (port.value.trim() === '' || known.includes(port.value.trim())) port.value = String(DEFAULT_PORT[transport]);
    }
    if (chosen && !idTouched) idInput.value = nextId(SUGGESTED_ID[transport], existingIds);
  }

  function values() {
    const v = { id: idInput.value.trim(), transport };
    if (transport === 'serial_rtu') {
      v.serial_device = deviceSelect.value;
      const baud = numberOf(baudField.control);
      if (baud !== undefined) v.baudrate = baud;
    } else {
      v.host = hostField.control.value.trim();
      const port = numberOf(portField.control);
      if (port !== undefined) v.port = port;
      if (transport === 'solarman_v5_tcp') {
        const serial = numberOf(loggerField.control);
        if (serial !== undefined) v.logger_serial = serial;
      }
    }
    for (const [name, x] of Object.entries(advanced)) {
      if (name === 'rtu_late_window_s' && !(transport === 'rtu_over_tcp' || transport === 'serial_rtu')) continue;
      if (name === 'bus_id') {
        const text = x.control.value.trim();
        if (text) v.bus_id = text;
        continue;
      }
      const value = numberOf(x.control);
      if (value !== undefined) v[name] = value;
    }
    return v;
  }

  // Returns true when nothing obvious is wrong; marks the fields otherwise.
  function validate() {
    for (const x of fields.values()) x.setError('');
    transportError.hidden = true;
    if (transport === null) {
      transportError.hidden = false;
      return false;
    }
    let ok = true;
    const bad = (x, message) => { x.setError(message); ok = false; };
    const v = values();
    if (!ID_RE.test(v.id)) bad(idField, t('form.id_pattern'));
    else if (!editing && existingIds.includes(v.id)) bad(idField, t('form.id_taken'));
    if (transport === 'serial_rtu') {
      if (!v.serial_device) bad(deviceField, t('form.required'));
    } else {
      if (!v.host) bad(hostField, t('form.required'));
      if (typeof v.port === 'string') bad(portField, t('form.number'));
      if (transport === 'solarman_v5_tcp') {
        if (v.logger_serial === undefined) bad(loggerField, t('form.required'));
        else if (typeof v.logger_serial === 'string') bad(loggerField, t('form.number'));
      }
    }
    for (const [name, x] of Object.entries(advanced)) {
      if (name !== 'bus_id' && typeof v[name] === 'string') bad(x, t('form.number'));
    }
    if (Object.values(advanced).some((x) => x.el.querySelector('[aria-invalid]'))) details.open = true;
    return ok;
  }

  const el = h('div', { class: 'plant-form endpoint-form' }, transportSet, transportError, idField.el, detail, details);
  refresh();
  return {
    el,
    fields,
    values,
    validate,
    get transport() { return transport; },
    focusFirst() {
      const first = el.querySelector('input[type="radio"]:checked') || el.querySelector('input[type="radio"]');
      if (first) first.focus();
    },
  };
}

// -- inverter form --------------------------------------------------------------------------------

export function inverterForm({ initial = null, editing = false, endpoints, existingIds = [], defaultEndpoint = null }) {
  const f = initial || {};
  const fields = new Map();
  const register = (x) => { fields.set(x.name, x); return x; };

  const nameInput = textInput(f.name || '', { maxlength: 64 });
  const nameField = register(field('name', t('inv.name'), nameInput, t('inv.name_help')));
  const idInput = textInput(f.id || '', { maxlength: 32 });
  if (editing) idInput.readOnly = true;
  let idTouched = editing;
  idInput.addEventListener('input', () => { idTouched = true; });
  const idField = register(field('id', t('inv.id'), idInput, editing ? t('inv.id_locked') : t('inv.id_help')));
  nameInput.addEventListener('input', () => {
    if (!idTouched) idInput.value = uniqueId(slug(nameInput.value), existingIds);
  });

  const label = (ep) => `${ep.id} · ${transportLabel(ep.transport)} · ${endpointTarget(ep)}`;
  const endpointSelect = h('select', { class: 'select' },
    h('option', { value: '' }, '—'),
    endpoints.map((ep) => h('option', { value: ep.id, selected: ep.id === (f.endpoint || defaultEndpoint) }, label(ep))));
  const endpointField = register(field('endpoint', t('inv.endpoint'), endpointSelect, t('inv.endpoint_help')));
  const unitField = register(field('unit_id', t('inv.unit'), numberInput(f.unit_id ?? 1), t('inv.unit_help')));

  const fallbackSelect = h('select', { class: 'select' },
    h('option', { value: '' }, t('inv.fallback_none')),
    endpoints.map((ep) => h('option', { value: ep.id, selected: ep.id === f.fallback_endpoint }, label(ep))));
  const fallbackField = register(field('fallback_endpoint', t('inv.fallback'), fallbackSelect, t('inv.fallback_help')));
  const fallbackUnitField = register(field('fallback_unit_id', t('inv.fallback_unit'), numberInput(f.fallback_unit_id ?? f.unit_id ?? 1)));
  let fallbackUnitTouched = f.fallback_unit_id !== undefined && f.fallback_unit_id !== f.unit_id;
  fallbackUnitField.control.addEventListener('input', () => { fallbackUnitTouched = true; });
  unitField.control.addEventListener('input', () => { if (!fallbackUnitTouched) fallbackUnitField.control.value = unitField.control.value; });

  const serialField = register(field('expected_serial', t('inv.expected_serial'), textInput(f.expected_serial || '', { maxlength: 10 }), t('inv.expected_serial_help')));
  const energySelect = h('select', { class: 'select' },
    [['auto', 'inv.energy_auto'], ['0.1', 'inv.energy_tenth'], ['1', 'inv.energy_one']].map(([value, key]) =>
      h('option', { value, selected: value === (f.energy_resolution || 'auto') }, t(key))));
  const energyField = register(field('energy_resolution', t('inv.energy'), energySelect, t('inv.energy_help')));
  const poll = {};
  for (const [name, dflt] of [['poll_fast_s', 10], ['poll_energy_s', 60], ['poll_settings_s', 300], ['poll_identity_s', 3600]]) {
    poll[name] = register(field(name, t(`inv.${name}`), numberInput(f[name] ?? dflt)));
  }
  const details = h('details', { class: 'advanced' },
    h('summary', {}, t('inv.advanced')),
    h('div', { class: 'grid-fields' }, Object.values(poll).map((x) => x.el)));

  function refresh() {
    const hasFallback = fallbackSelect.value !== '';
    fallbackUnitField.el.hidden = !hasFallback;
  }
  fallbackSelect.addEventListener('change', refresh);

  function values() {
    const v = { id: idInput.value.trim(), name: nameInput.value.trim(), endpoint: endpointSelect.value };
    const unit = numberOf(unitField.control);
    v.unit_id = unit === undefined ? '' : unit;
    if (fallbackSelect.value) {
      v.fallback_endpoint = fallbackSelect.value;
      const fu = numberOf(fallbackUnitField.control);
      if (fu !== undefined) v.fallback_unit_id = fu;
    }
    const serial = serialField.control.value;
    if (serial !== '') v.expected_serial = serial;
    v.energy_resolution = energySelect.value;
    for (const [name, x] of Object.entries(poll)) {
      const value = numberOf(x.control);
      if (value !== undefined) v[name] = value;
    }
    return v;
  }

  function validate() {
    for (const x of fields.values()) x.setError('');
    let ok = true;
    const bad = (x, message) => { x.setError(message); ok = false; };
    const v = values();
    if (!v.name) bad(nameField, t('form.required'));
    if (!ID_RE.test(v.id)) bad(idField, t('form.id_pattern'));
    else if (!editing && existingIds.includes(v.id)) bad(idField, t('form.id_taken'));
    if (!v.endpoint) bad(endpointField, t('form.required'));
    if (typeof v.unit_id !== 'number') bad(unitField, v.unit_id === '' ? t('form.required') : t('form.number'));
    if (v.fallback_endpoint && typeof v.fallback_unit_id === 'string') bad(fallbackUnitField, t('form.number'));
    for (const [name, x] of Object.entries(poll)) {
      if (typeof v[name] === 'string') bad(x, t('form.number'));
    }
    return ok;
  }

  const el = h('div', { class: 'plant-form inverter-form' },
    nameField.el, idField.el, endpointField.el, unitField.el, fallbackField.el, fallbackUnitField.el,
    serialField.el, energyField.el, details,
    h('p', { class: 'note writes-note' }, t('inv.writes_note')));
  refresh();
  return {
    el,
    fields,
    values,
    validate,
    endpointValue: () => endpointSelect.value,
    unitValue: () => numberOf(unitField.control),
    focusFirst() { nameInput.focus(); },
  };
}

// -- connection test ------------------------------------------------------------------------------

function testOutcomeText(result) {
  return t(`test.outcome.${result.outcome}`);
}

export function testResultView(result) {
  const ok = result.ok;
  const advice = ok ? null : tOr(`test.hint.${result.outcome}.${result.transport}`, `test.hint.${result.outcome}`, {});
  const model = result.model;
  const facts = [];
  if (result.serial) facts.push(['test.serial', result.serial]);
  if (model && (model.mppt !== null || model.phases !== null) && ok) {
    const parts = [t('test.model_value', { mppt: model.mppt ?? '?', phases: model.phases ?? '?' })];
    if (model.rated_kw !== null && model.rated_kw !== undefined) parts.push(t('test.rated', { kw: model.rated_kw }));
    facts.push(['test.model', parts.join(' · ')]);
  }
  const notes = [];
  if (result.serial_vs_expected === true) notes.push(['ok', t('test.expected_ok')]);
  if (result.serial_vs_expected === false) notes.push(['bad', t('test.expected_bad')]);
  if (result.serial_vs_bound === true) notes.push(['ok', t('test.bound_ok')]);
  if (result.serial_vs_bound === false) notes.push(['bad', t('test.bound_bad')]);
  for (const conflict of (model && model.conflicts) || []) {
    notes.push(['warn', tOr(`hint.conflict.${conflict.code}`, 'hint.conflict.generic', { code: conflict.code, detail: conflict.detail || '' })]);
  }
  return h('div', { class: `test-result ${ok ? 'ok' : 'fail'}`, tabindex: '-1' },
    h('h4', {}, ok ? t('test.ok_title') : t('test.fail_title')),
    ok ? null : h('p', {}, h('strong', {}, testOutcomeText(result))),
    advice ? h('p', {}, advice) : null,
    facts.length ? h('dl', { class: 'facts' }, facts.map(([key, value]) => h('div', { class: 'fact' }, h('dt', {}, t(key)), h('dd', {}, value)))) : null,
    notes.length ? h('ul', { class: 'hints' }, notes.map(([kind, text]) => h('li', { class: `hint ${kind}` }, text))) : null,
    h('p', { class: 'muted small' }, `${t(`test.path.${result.path}`)} · ${t('test.duration', { s: result.duration_s })}`));
}

// ``build()`` returns the request body, or ``null`` when the form is not ready (it marks the fields).
export function testPanel({ build, intro = null, extra = null }) {
  const out = h('div', { class: 'test-out', 'aria-live': 'polite' });
  const button = h('button', { class: 'btn', type: 'button' }, t('test.btn'));
  button.addEventListener('click', async () => {
    const body = build();
    if (!body) return;
    busy(button, true);
    button.textContent = t('test.running');
    out.replaceChildren(h('p', { class: 'muted', role: 'status' }, t('test.running')));
    try {
      const result = await mutate('POST', 'api/plant/test', body);
      const view = testResultView(result);
      out.replaceChildren(view);
      view.focus({ preventScroll: false });
    } catch (error) {
      out.replaceChildren(h('p', { class: 'banner error', role: 'alert' }, errorText(error)));
    } finally {
      busy(button, false);
      button.textContent = t('test.btn');
    }
  });
  return {
    el: h('section', { class: 'test-panel' }, intro, extra, h('div', { class: 'actions left' }, button), out),
    run: () => button.click(),
    button,
  };
}

export async function loadSerialDevices() {
  try {
    return (await api('GET', 'api/plant/serial-devices')).devices;
  } catch {
    return [];
  }
}

export const existingEndpointIds = () => (state.plant ? state.plant.endpoints.map((e) => e.id) : []);
export const existingInverterIds = () => (state.plant ? state.plant.inverters.map((i) => i.id) : []);

// -- changing calls (shared by the plant page and the setup assistant) -------------------------

// Run a plant change with the plant revision the user saw. Returns the response, or ``null`` after
// the error was shown in ``message`` (and in the fields of ``form``).
async function plantCall(method, path, body, { form = null, message }) {
  message.replaceChildren();
  try {
    const result = await mutate(method, path, body, state.plant ? state.plant.revision : 0);
    await reloadAfterPlantChange();
    return result;
  } catch (error) {
    const text = applyServerError(error, form ? form.fields : new Map());
    if (error instanceof ApiError && error.key === 'stale_revision') {
      await reloadAfterPlantChange().catch(() => {});
      message.append(h('p', { class: 'banner warn', role: 'alert' }, `${text} ${t('plant.reloaded')}`));
    } else {
      message.append(h('p', { class: 'banner error', role: 'alert' }, text));
    }
    return null;
  }
}

export async function saveEndpoint(form, { editing, message }) {
  if (!form.validate()) {
    message.replaceChildren(h('p', { class: 'banner warn', role: 'alert' }, t('test.fix_form')));
    return null;
  }
  const values = form.values();
  const result = await plantCall(editing ? 'PUT' : 'POST', editing ? `api/plant/endpoints/${values.id}` : 'api/plant/endpoints', values, { form, message });
  return result ? values.id : null;
}

export async function saveInverter(form, { editing, message }) {
  if (!form.validate()) {
    message.replaceChildren(h('p', { class: 'banner warn', role: 'alert' }, t('test.fix_form')));
    return null;
  }
  const values = form.values();
  const result = await plantCall(editing ? 'PUT' : 'POST', editing ? `api/plant/inverters/${values.id}` : 'api/plant/inverters', values, { form, message });
  return result ? values.id : null;
}

export const deleteEndpoint = (id, { message }) =>
  plantCall('POST', `api/plant/endpoints/${id}/delete`, { confirm: true }, { message });

export const deleteInverter = (id, removeBinding, { message }) =>
  plantCall('POST', `api/plant/inverters/${id}/delete`, { confirm: true, remove_binding: removeBinding }, { message });
