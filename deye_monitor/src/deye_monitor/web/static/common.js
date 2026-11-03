// Pieces shared by the views: translated texts of states, errors, hints and the load light.

import { t, tOr, localName, localText, errorText } from './i18n.js';
import { h, toast, helpPopover } from './ui.js';
import { state, loadLevel, profileOf, serverNowEstimate, canEdit } from './state.js';

export { errorText };

export function stateChip(status) {
  return h('span', { class: `chip state-${status.state}` }, t(`state.${status.state}`));
}

const REASON_PATTERNS = [
  [/^identified$/, 'identified'],
  [/^starting$/, 'starting'],
  [/^no usable identity answer/, 'no_identity'],
  [/^serial number differs/, 'serial_differs'],
  [/^rebind requested/, 'rebind'],
  [/^store reset/, 'store_reset'],
  [/^internal_error$/, 'internal_error'],
  [/^identity_store_corrupt$/, 'identity_store_corrupt'],
  [/^identity_store_unwritable$/, 'identity_store_unwritable'],
  [/^energy_store_corrupt$/, 'energy_store_corrupt'],
  [/^energy_history_inconsistent$/, 'energy_history_inconsistent'],
  [/^energy_history_missing$/, 'energy_history_missing'],
  [/^energy_store_unwritable$/, 'energy_store_unwritable'],
  [/^selection_recovery_required$/, 'selection_recovery_required'],
  [/^plant_recovery_failed$/, 'plant_recovery_failed'],
  [/failed cycles on the/, 'failed_cycles'],
];

export function reasonText(status) {
  for (const [pattern, key] of REASON_PATTERNS) {
    if (pattern.test(status.reason || '')) return t(`reason.${key}`, { path: t(`path.${status.active_path}`) });
  }
  return t(`reason.generic.${status.state}`);
}

// Master or slave only: on a three-phase model the phase letter of R336 says nothing useful
// (every unit shows "A"); it stays in the diagnostics.
export function roleText(status) {
  const role = status.identity && status.identity.role;
  if (!role || !role.parallel_enable) return role ? t('role.single') : t('value.unknown');
  const master = role.master === true || role.master === 1 || role.master === 'master';
  return master ? t('role.master') : t('role.slave');
}

export function ageText(wall) {
  const now = serverNowEstimate();
  if (wall === null || wall === undefined || now === null) return t('time.never');
  const seconds = Math.max(0, Math.round(now - wall));
  if (seconds < 5) return t('time.now');
  if (seconds < 90) return t('time.seconds', { n: seconds });
  if (seconds < 5400) return t('time.minutes', { n: Math.round(seconds / 60) });
  if (seconds < 172800) return t('time.hours', { n: Math.round(seconds / 3600) });
  return t('time.days', { n: Math.round(seconds / 86400) });
}

export function absoluteTime(wall) {
  if (wall === null || wall === undefined) return '';
  return new Date(wall * 1000).toLocaleString();
}

export function levelChip(level, text) {
  return h('span', { class: `light ${level}` }, h('span', { class: 'dot', 'aria-hidden': 'true' }), text || t(`load.${level}`));
}

export function loadProgress(utilisation, level) {
  const value = utilisation === null || utilisation === undefined ? 0 : Math.min(utilisation, 1);
  return h('progress', { class: `load-bar ${level}`, max: 1, value, 'aria-label': t('load.label') });
}

// Total load of the shared bus (all inverters on it, with the proposed plan): a second light.
export function busLevel(budget) {
  if (budget.bus_utilisation === null || budget.bus_utilisation === undefined) return null;
  return loadLevel(budget.bus_utilisation);
}

export function busChip(budget) {
  const level = busLevel(budget);
  return level ? levelChip(level, `${t('setup.bus_load')}: ${t(`load.${level}`)} (${Math.round(budget.bus_utilisation * 100)} %)`) : null;
}

export function budgetLevel(budget) {
  return loadLevel(budget.utilisation, budget.overloaded);
}

export function sensorName(inv, key) {
  const profile = profileOf(inv);
  const entry = profile && profile.byKey.get(key);
  return entry ? localName(entry) : key;
}

// The plant totals an entry feeds on this inverter: only a member of the plant totals delivers
// inputs, and the master-only ones (battery state, BMS) only while it is (or may be) the master.
export function plantInputsOf(inv, entry) {
  const totals = state.totals;
  if (!entry || !entry.plant_total || !totals || !totals.active || !(totals.members || []).some((m) => m.id === inv.id)) return [];
  return entry.plant_total.filter((item) => !item.master_only || !totals.master || totals.master === inv.id);
}

export function plantTotalName(item) {
  const key = `plantname.${item.metric}`;
  return tOr(key, 'plantname.generic', { name: item.name });
}

export function sensorList(inv, keys) {
  return keys.map((k) => sensorName(inv, k)).join(', ');
}

// The explanation of one capability conflict; the raw values (`detail`) are shown beneath it.
export function conflictText(conflict) {
  return tOr(`hint.conflict.${conflict.code}`, 'hint.conflict.generic', { code: conflict.code, detail: conflict.detail || '' });
}

// Text of a hint with its raw-value detail (if any) as a small line beneath.
export function hintBody(hint) {
  const actions = (hint.actions || []).map((a) => h('button', { class: 'btn small', type: 'button', onClick: a.run }, a.label));
  if (!hint.detail && !actions.length) return hint.text;
  return h('span', { class: 'hint-text' }, hint.text,
    hint.detail ? h('small', { class: 'hint-detail' }, hint.detail) : null,
    actions.length ? h('span', { class: 'hint-actions' }, actions) : null);
}

// A plant change of the energy resolution, made only when the user clicks a button.
function energyChoice(inv) {
  const choose = (value) => async () => {
    const { chooseEnergyResolution } = await import('./plantforms.js');
    const error = await chooseEnergyResolution(inv.id, value);
    toast(error || t('hint.energy.chosen'), error ? 'error' : 'ok');
  };
  return canEdit()
    ? [{ label: t('hint.energy.choose_tenth'), run: choose('0.1') }, { label: t('hint.energy.choose_one'), run: choose('1') }]
    : [];
}

// GRID telemetry whose "only 0" is expected while the source described for the GRID terminal is
// disconnected or switched off (an intermittent utility, an outage, a generator that is not
// running). An exact list, never a prefix: energy counters, CT reports and settings stay out.
const GRID_TELEMETRY = new Set([
  'grid_voltage_a', 'grid_voltage_b', 'grid_voltage_c', 'grid_line_voltage_ca', 'grid_frequency',
  'grid_internal_current_a', 'grid_internal_current_b', 'grid_internal_current_c',
  'grid_external_current_a', 'grid_external_current_b', 'grid_external_current_c',
  'grid_internal_power_a', 'grid_internal_power_b', 'grid_internal_power_c', 'grid_internal_power_total',
  'grid_external_power_a', 'grid_external_power_b', 'grid_external_power_c', 'grid_external_power_total',
  'grid_selected_power_a', 'grid_selected_power_b', 'grid_selected_power_c', 'grid_selected_power_total',
  'relay_grid',
]);

// The saved description names a source for the GRID terminal. The signal answer is not asked.
function gridTerminalUsed(inv) {
  const use = inv.selection?.features?.grid_port_use;
  return use === 'utility' || use === 'generator';
}

// Hints for one inverter: capability conflicts, observations and energy migration. The energy
// resolution line (factor, source, R65 raw) is a diagnostic: only the sensor page asks for it.
export function hintsOf(inv, { diagnostics = false } = {}) {
  const s = inv.status;
  const out = [];
  for (const conflict of s.capability_conflicts || []) {
    out.push({ kind: 'conflict', text: conflictText(conflict), detail: conflict.detail || '' });
  }
  const hardware = hardwareHint(s.identity || {});
  if (hardware) out.push({ kind: 'info', text: hardware });
  const energyNow = s.energy || {};
  if (diagnostics && energyNow.effective_resolution != null) {
    const r65 = s.r65 && s.r65.raw != null ? `0x${Number(s.r65.raw).toString(16).toUpperCase().padStart(4, '0')}` : t('hint.energy.r65_none');
    const source = t(`hint.energy.source.${{ profile: 'profile', override: 'override' }[energyNow.source] || 'profile'}`);
    out.push({ kind: 'info', text: t('hint.energy.diag', { factor: energyNow.effective_resolution, source, r65 }) });
  }
  const check = energyNow.plausibility || {};
  if (check.state === 'counter_too_high' || check.state === 'counter_too_low') {
    const high = check.state === 'counter_too_high';
    const actions = energyChoice(inv).filter((a, i) => (high ? energyNow.effective_resolution === 1 && i === 0 : energyNow.effective_resolution === 0.1 && i === 1));
    out.push({
      kind: 'energy',
      text: t(high ? 'hint.energy.too_high' : 'hint.energy.too_low', { ratio: check.ratio, counter: check.counter_kwh, integrated: check.integrated_kwh }),
      actions,
    });
  }
  for (const hint of s.hints || []) {
    const entry = profileOf(inv)?.byKey.get(hint.key);
    const zeroOnly = hint.state === 'zero_only_inconclusive';
    // only the zero-only prompt is dropped: read errors, placeholders and conflicts of the same key stay
    // PV zeros (shade, night, a quiet string) say nothing about the sensor
    if (zeroOnly && entry?.point === 'pv') continue;
    // a plant total needs this value: "only 0" is legitimate (the master may supply no load)
    if (zeroOnly && plantInputsOf(inv, entry).length) continue;
    if (zeroOnly && GRID_TELEMETRY.has(hint.key) && gridTerminalUsed(inv)) continue;
    const name = sensorName(inv, hint.key);
    out.push({
      kind: 'observation',
      key: hint.key,
      text: t(`hint.obs.${hint.state}`, { name, n: hint.responses, d: hint.daylight_samples }),
    });
  }
  const energy = s.energy || {};
  if (energy.blocked) out.push({ kind: 'energy', text: t('hint.energy.blocked') });
  // warnings (real problems) first, then information, then observations
  return out.map((hint, i) => ({ hint, i })).sort((a, b) => HINT_RANK(a.hint) - HINT_RANK(b.hint) || a.i - b.i).map((x) => x.hint);
}

const HINT_RANK = (hint) => ({ conflict: 0, energy: 0, info: 1, observation: 2 }[hint.kind] ?? 1);

// "Hardware data not reported" and an undefined battery input code are one finding: one hint.
function hardwareHint(identity) {
  const unreported = identity.capabilities_unreported || [];
  const unknown = (identity.capabilities_unknown || {}).battery_input_code;
  const words = identity.capability_words || {};
  const hex = (a) => `R${a}=0x${Number(words[a]).toString(16).toUpperCase().padStart(4, '0')}`;
  const parts = [];
  if (unreported.includes('device_type')) parts.push(hex(0));
  if (unreported.includes('mppt_phases')) parts.push(hex(22));
  if (unreported.includes('battery_input_code')) parts.push(hex(24));
  if (!parts.length) return unknown === undefined ? null : t('hint.unknown_battery_code', { value: unknown });
  if (unknown !== undefined) parts.push(`R24=${unknown}`);
  return t('hint.hardware_unreported', { words: parts.join(', ') });
}

export function describe(entry) {
  return localText(entry.description) || '';
}

// The (i) button of a setting or sensor: null when it has no help text. ``registers`` (sensors) adds
// the "Register R83" line and ``raw`` the note that the raw word is an attribute.
export function helpButton(help, name, registers, raw) {
  const text = localText(help);
  if (!text) return null;
  const lines = [];
  if (registers && registers.length) lines.push(t('help.register', { regs: registers.map((r) => `R${r}`).join(', ') }));
  if (raw) lines.push(t('help.raw_attribute'));
  return helpPopover({ label: t('help.about', { name }), text, meta: lines.length ? lines.join(' · ') : null });
}
