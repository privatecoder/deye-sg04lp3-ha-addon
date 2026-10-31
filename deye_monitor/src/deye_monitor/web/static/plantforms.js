// Forms of the plant setup: connection, inverter and the read-only connection test.
// The server validates everything again; the checks here only save a round trip.

import { t, tOr, errorText } from './i18n.js';
import { h, uid, busy } from './ui.js';
import { ApiError, api } from './api.js';
import { state, mutate, reloadAfterPlantChange, loadPlant } from './state.js';
import { conflictText, hintBody } from './common.js';

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
  const helpEl = help ? h('p', { class: 'help', id: helpId }, help) : null;
  const el = h('div', { class: 'field wide', 'data-field': name },
    h('label', { for: controlId }, label),
    control,
    helpEl,
    error);
  return {
    name,
    el,
    control,
    helpEl,
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

// An identifier that is fixed after creating: read-only (still submitted), visibly locked.
function lockId(input) {
  input.readOnly = true;
  input.classList.add('locked');
  input.setAttribute('aria-readonly', 'true');
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
  if (editing) lockId(idInput);
  let idTouched = editing;
  idInput.addEventListener('input', () => { idTouched = true; });
  const idField = register(field('id', t('ep.id'), idInput, editing ? t('ep.id_locked') : t('ep.id_help')));

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
  // the transport whose default port was last applied: a saved port is never replaced by a default
  let portBase = transport;
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
      // default only for a new connection or a real transport change, and only while the port still
      // is the default of the previous transport (a deliberately set port stays, also 8899 on Modbus)
      const port = portField.control;
      const text = port.value.trim();
      const wasDefault = portBase !== null && portBase !== transport && text === String(DEFAULT_PORT[portBase]);
      if (text === '' || wasDefault) port.value = String(DEFAULT_PORT[transport]);
    }
    portBase = transport;
    if (chosen && !idTouched) idInput.value = nextId(SUGGESTED_ID[transport], existingIds);
  }

  // V5: the override or 1; otherwise the field (a half-typed value stays visible to validate)
  function effectiveUnit(field_, override, v5) {
    if (v5) {
      const o = numberOf(override.control);
      return o === undefined ? 1 : o;
    }
    const n = numberOf(field_.control);
    return n === undefined ? '' : n;
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
  if (editing) lockId(idInput);
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
  const isV5 = (endpointId) => (endpoints.find((ep) => ep.id === endpointId) || {}).transport === 'solarman_v5_tcp';
  // One place for the Modbus address. A Solarman logger sits on exactly one inverter and only
  // answers on address 1: no field, a fixed note, an override under "Advanced" only. A gateway
  // or serial bus can serve several inverters, so there the address belongs to the inverter.
  const unitField = register(field('unit_id', t('inv.unit'), numberInput(f.unit_id ?? 1), t('inv.unit_help')));
  const unitFixed = h('p', { class: 'help fixed-address', 'data-fixed': 'unit_id' }, t('inv.unit_fixed'));
  unitField.el.after(unitFixed);
  let unitTouched = f.unit_id !== undefined;
  unitField.control.addEventListener('input', () => { unitTouched = true; });

  const fallbackSelect = h('select', { class: 'select' },
    h('option', { value: '' }, t('inv.fallback_none')),
    endpoints.map((ep) => h('option', { value: ep.id, selected: ep.id === f.fallback_endpoint }, label(ep))));
  const fallbackField = register(field('fallback_endpoint', t('inv.fallback'), fallbackSelect, t('inv.fallback_help')));
  const fallbackUnitField = register(field('fallback_unit_id', t('inv.fallback_unit'), numberInput(f.fallback_unit_id ?? f.unit_id ?? 1), t('inv.unit_help')));
  const fallbackUnitFixed = h('p', { class: 'help fixed-address', 'data-fixed': 'fallback_unit_id' }, t('inv.unit_fixed'));
  fallbackUnitField.el.after(fallbackUnitFixed);
  let fallbackUnitTouched = f.fallback_unit_id !== undefined && f.fallback_unit_id !== f.unit_id;
  fallbackUnitField.control.addEventListener('input', () => { fallbackUnitTouched = true; });
  // Overrides for a logger address other than 1 (rare): Advanced only, empty = 1.
  const unitOverride = register(field('unit_override', t('inv.unit_override'), numberInput(isV5(f.endpoint) && f.unit_id !== undefined && f.unit_id !== 1 ? f.unit_id : ''), t('inv.unit_override_help')));
  const fallbackOverride = register(field('fallback_unit_override', t('inv.fallback_unit_override'), numberInput(isV5(f.fallback_endpoint) && f.fallback_unit_id !== undefined && f.fallback_unit_id !== 1 ? f.fallback_unit_id : ''), t('inv.unit_override_help')));

  // Per route: the field, the fixed note and the override follow the connection type.
  function syncUnits({ endpointChanged = false } = {}) {
    if (endpointChanged && !unitTouched) unitField.control.value = 1;
    const primaryV5 = isV5(endpointSelect.value);
    const fallbackV5 = isV5(fallbackSelect.value);
    if (!fallbackUnitTouched) fallbackUnitField.control.value = fallbackV5 ? 1 : unitField.control.value;
    unitField.el.hidden = primaryV5;
    unitFixed.hidden = !primaryV5;
    unitOverride.el.hidden = !primaryV5;
    fallbackUnitField.el.hidden = !fallbackSelect.value || fallbackV5;
    fallbackUnitFixed.hidden = !fallbackSelect.value || !fallbackV5;
    fallbackOverride.el.hidden = !fallbackV5;
  }
  endpointSelect.addEventListener('change', () => syncUnits({ endpointChanged: true }));
  fallbackSelect.addEventListener('change', () => syncUnits());
  unitField.control.addEventListener('input', () => syncUnits());

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
    h('div', { class: 'grid-fields' }, unitOverride.el, fallbackOverride.el, Object.values(poll).map((x) => x.el)));

  // V5: the override or 1; otherwise the field (a half-typed value stays visible to validate)
  function effectiveUnit(field_, override, v5) {
    if (v5) {
      const o = numberOf(override.control);
      return o === undefined ? 1 : o;
    }
    const n = numberOf(field_.control);
    return n === undefined ? '' : n;
  }

  function values() {
    const v = { id: idInput.value.trim(), name: nameInput.value.trim(), endpoint: endpointSelect.value };
    v.unit_id = effectiveUnit(unitField, unitOverride, isV5(endpointSelect.value));
    if (fallbackSelect.value) {
      v.fallback_endpoint = fallbackSelect.value;
      const fu = effectiveUnit(fallbackUnitField, fallbackOverride, isV5(fallbackSelect.value));
      if (fu !== '') v.fallback_unit_id = fu;
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
    if (typeof v.unit_id !== 'number') bad(isV5(v.endpoint) ? unitOverride : unitField, v.unit_id === '' ? t('form.required') : t('form.number'));
    if (v.fallback_endpoint && typeof v.fallback_unit_id === 'string') bad(isV5(v.fallback_endpoint) ? fallbackOverride : fallbackUnitField, t('form.number'));
    for (const [name, x] of Object.entries(poll)) {
      if (typeof v[name] === 'string') bad(x, t('form.number'));
    }
    return ok;
  }

  const el = h('div', { class: 'plant-form inverter-form' },
    nameField.el, idField.el, endpointField.el, unitField.el, unitFixed, fallbackField.el, fallbackUnitField.el, fallbackUnitFixed,
    serialField.el, energyField.el, details,
    h('p', { class: 'note writes-note' }, t('inv.writes_note')));
  syncUnits();
  return {
    el,
    fields,
    values,
    validate,
    endpointValue: () => endpointSelect.value,
    unitValue: () => { const u = effectiveUnit(unitField, unitOverride, isV5(endpointSelect.value)); return u === '' ? undefined : u; },
    fallbackValue: () => {
      if (!fallbackSelect.value) return null;
      const u = effectiveUnit(fallbackUnitField, fallbackOverride, isV5(fallbackSelect.value));
      return { endpoint: fallbackSelect.value, unit: u === '' ? undefined : u };
    },
    focusFirst() { nameInput.focus(); },
  };
}

// -- connection test ------------------------------------------------------------------------------

function testOutcomeText(result) {
  return t(`test.outcome.${result.outcome}`);
}

export function testResultView(result, { unit = null } = {}) {
  const ok = result.ok;
  const advice = ok ? null : tOr(`test.hint.${result.outcome}.${result.transport}`, `test.hint.${result.outcome}`, {});
  const model = result.model;
  const facts = [];
  if (result.serial) facts.push(['test.serial', result.serial]);
  if (model && (model.mppt !== null || model.phases !== null || model.unreported) && ok) {
    const parts = [model.unreported
      ? t('test.model_unreported', { mppt: model.profile_mppt, phases: model.profile_phases })
      : t('test.model_value', { mppt: model.mppt ?? '?', phases: model.phases ?? '?' })];
    if (model.rated_kw !== null && model.rated_kw !== undefined) parts.push(t('test.rated', { kw: model.rated_kw }));
    facts.push(['test.model', parts.join(' · ')]);
  }
  const notes = [];
  if (!ok && result.transport === 'solarman_v5_tcp' && typeof unit === 'number' && unit !== 1) notes.push(['warn', t('inv.unit_v5_warning')]);
  if (result.serial_vs_expected === true) notes.push(['ok', t('test.expected_ok')]);
  if (result.serial_vs_expected === false) notes.push(['bad', t('test.expected_bad')]);
  if (result.serial_vs_bound === true) notes.push(['ok', t('test.bound_ok')]);
  if (result.serial_vs_bound === false) notes.push(['bad', t('test.bound_bad')]);
  for (const conflict of (model && model.conflicts) || []) {
    notes.push(['warn', hintBody({ text: conflictText(conflict), detail: conflict.detail || '' })]);
  }
  return h('div', { class: `test-result ${ok ? 'ok' : 'fail'}`, tabindex: '-1' },
    h('h4', {}, ok ? t('test.ok_title') : t('test.fail_title')),
    ok ? null : h('p', {}, h('strong', {}, testOutcomeText(result))),
    advice ? h('p', {}, advice) : null,
    facts.length ? h('dl', { class: 'facts' }, facts.map(([key, value]) => h('div', { class: 'fact' }, h('dt', {}, t(key)), h('dd', {}, value)))) : null,
    notes.length ? h('ul', { class: 'hints' }, notes.map(([kind, text]) => h('li', { class: `hint ${kind}` }, text))) : null,
    h('p', { class: 'muted small' }, [t(`test.path.${result.path}`), t('test.duration', { s: result.duration_s }), result.attempts > 1 ? t('test.attempts', { n: result.attempts }) : null].filter(Boolean).join(' · ')));
}

// One check per configured route of an inverter (primary and fallback, each with its own address).
// ``routes``: [{ endpoint: id, unit, ...extra body fields }].
export function routeChecks(routes, endpoints) {
  return routes.map(({ endpoint, unit, ...rest }, index) => {
    const ep = endpoints.find((e) => e.id === endpoint);
    const kind = t(index === 0 ? 'test.route_primary' : 'test.route_fallback');
    const type = ep ? transportLabel(ep.transport) : '?';
    return {
      label: t('test.route', { kind, id: endpoint, type, unit }),
      body: { endpoint_id: endpoint, unit_id: unit, ...rest },
    };
  });
}

export function savedInverterChecks(inv, endpoints) {
  const extra = { inverter: inv.id };
  if (inv.expected_serial) extra.expected_serial = inv.expected_serial;
  const routes = [{ endpoint: inv.endpoint, unit: inv.unit_id, ...extra }];
  if (inv.fallback_endpoint) routes.push({ endpoint: inv.fallback_endpoint, unit: inv.fallback_unit_id ?? inv.unit_id, ...extra });
  return routeChecks(routes, endpoints);
}

// The address part of a connection's test. A Solarman logger answers on address 1 only (no field).
// On a gateway or bus the address belongs to the inverters: the inverters that already use this
// connection are each tested with their own address; without any, the field is an address to
// test, which is not saved anywhere.
export function connectionTest(form, usedBy = []) {
  const input = h('input', { type: 'text', inputmode: 'numeric', class: 'select unit-input', value: '1', 'aria-label': t('ep.test_unit_unsaved') });
  const field_ = h('div', { class: 'field', 'data-test-address': 'free' }, h('label', {}, t('ep.test_unit_unsaved')), input);
  const users = h('p', { class: 'help', 'data-test-address': 'users' }, t('ep.test_users', { names: usedBy.map((u) => `${u.name} (${u.unit})`).join(', ') }));
  const el = h('div', {}, field_, users);
  const sync = () => {
    const v5 = form.transport === 'solarman_v5_tcp';
    field_.hidden = v5 || usedBy.length > 0;
    users.hidden = v5 || usedBy.length === 0;
  };
  form.el.addEventListener('change', sync);
  form.el.addEventListener('input', sync);
  sync();
  return {
    el,
    // null: not ready; else a body, or a list of { label, body } (one test per inverter)
    build() {
      if (!form.validate()) return null;
      const endpoint = form.values();
      if (form.transport === 'solarman_v5_tcp') return { endpoint, unit_id: 1 };
      if (usedBy.length) return usedBy.map((u) => ({ label: t('ep.test_for', { name: u.name, unit: u.unit }), body: { endpoint, unit_id: u.unit } }));
      const id = Number(input.value);
      if (!Number.isInteger(id) || id < 1 || id > 247) return null;
      return { endpoint, unit_id: id };
    },
  };
}

// Inverters of the saved plant that use a connection, with the address each one has on it.
export function connectionUsers(endpointId) {
  if (!endpointId || !state.plant) return [];
  const out = [];
  for (const inv of state.plant.inverters) {
    if (inv.endpoint === endpointId) out.push({ name: inv.name, unit: inv.unit_id });
    if (inv.fallback_endpoint === endpointId) out.push({ name: inv.name, unit: inv.fallback_unit_id ?? inv.unit_id });
  }
  return out;
}

// ``build()`` returns the request body (or, for several routes, a list of ``{label, body}``), or
// ``null`` when the form is not ready (it marks the fields).
export function testPanel({ build, intro = null, extra = null }) {
  const out = h('div', { class: 'test-out', 'aria-live': 'polite' });
  const button = h('button', { class: 'btn', type: 'button' }, t('test.btn'));
  button.addEventListener('click', async () => {
    const built = build();
    if (!built) return;
    const routes = Array.isArray(built) ? built : [{ label: null, body: built }];
    busy(button, true);
    button.textContent = t('test.running');
    out.replaceChildren(h('p', { class: 'muted', role: 'status' }, t('test.running')));
    const blocks = [];
    try {
      for (const { label, body } of routes) {
        let view;
        try {
          view = testResultView(await mutate('POST', 'api/plant/test', body), { unit: body.unit_id });
        } catch (error) {
          view = h('p', { class: 'banner error', role: 'alert' }, errorText(error));
        }
        blocks.push(label ? h('section', { class: 'test-route' }, h('h4', { class: 'route-title' }, label), view) : view);
        out.replaceChildren(...blocks, ...(blocks.length < routes.length ? [h('p', { class: 'muted', role: 'status' }, t('test.running'))] : []));
      }
      const first = out.querySelector('.test-result, .banner');
      if (first) first.focus({ preventScroll: false });
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

// Changes the server accepted whose follow-up reload failed, per message element: the call is NOT
// repeated (it would run into a stale revision or an existing id); only the reading is retried.
const unloaded = new WeakMap();

async function reloaded(result, message, id, onLoaded) {
  try {
    await reloadAfterPlantChange();
  } catch {
    const retry = () => reloaded(result, message, id, onLoaded);
    retry.id = id;
    unloaded.set(message, retry);
    const button = h('button', { class: 'btn', type: 'button', id: 'plant-reload' }, t('plant.reload_btn'));
    button.addEventListener('click', async () => {
      busy(button, true);
      const again = await retry();
      busy(button, false);
      if (again && onLoaded) onLoaded(again);
    });
    message.replaceChildren(h('div', { class: 'banner warn', role: 'alert' }, h('p', {}, t('plant.saved_not_loaded')), button));
    return null;
  }
  unloaded.delete(message);
  message.replaceChildren();
  return result;
}

// Run a plant change with the plant revision the user saw. Returns the response, or ``null`` after
// the error was shown in ``message`` (and in the fields of ``form``). ``onLoaded`` continues after a
// reload that had to be repeated.
async function plantCall(method, path, body, { form = null, message, id = null, onLoaded = null }) {
  if (unloaded.has(message)) return unloaded.get(message)();
  message.replaceChildren();
  let result;
  try {
    result = await mutate(method, path, body, state.plant ? state.plant.revision : 0);
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
  return reloaded(result, message, id, onLoaded);
}

async function save(form, { editing, message, onLoaded }, collection) {
  const waiting = unloaded.get(message);
  if (waiting) return (await waiting()) ? waiting.id : null; // accepted before: only read again
  if (!form.validate()) {
    message.replaceChildren(h('p', { class: 'banner warn', role: 'alert' }, t('test.fix_form')));
    return null;
  }
  const values = form.values();
  const path = editing ? `api/plant/${collection}/${values.id}` : `api/plant/${collection}`;
  const result = await plantCall(editing ? 'PUT' : 'POST', path, values, {
    form, message, id: values.id, onLoaded: onLoaded && (() => onLoaded(values.id)),
  });
  return result ? values.id : null;
}

// Set the energy resolution of one inverter ('0.1' or '1') from a hint; returns an error text or null.
export async function chooseEnergyResolution(id, value) {
  if (!state.plant) await loadPlant();
  const inverter = state.plant.inverters.find((i) => i.id === id);
  if (!inverter) return t('form.general');
  const message = h('div');
  const result = await plantCall('PUT', `api/plant/inverters/${id}`, { ...inverter, energy_resolution: value }, { message, id });
  return result ? null : (message.textContent || t('form.general'));
}

export const saveEndpoint = (form, options) => save(form, options, 'endpoints');
export const saveInverter = (form, options) => save(form, options, 'inverters');

export const deleteEndpoint = (id, { message, onLoaded = null }) =>
  plantCall('POST', `api/plant/endpoints/${id}/delete`, { confirm: true }, { message, onLoaded });

export const deleteInverter = (id, removeBinding, { message, onLoaded = null }) =>
  plantCall('POST', `api/plant/inverters/${id}/delete`, { confirm: true, remove_binding: removeBinding }, { message, onLoaded });

// The "Plant (totals)" setting: the inverters whose values the plant totals add up.
export const saveTotals = (members, allowSingle, { message }) =>
  plantCall('PUT', 'api/plant/totals', { members, allow_single: allowSingle }, { message });
