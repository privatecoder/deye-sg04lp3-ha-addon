// Pieces shared by the views: translated texts of states, errors, hints and the load light.

import { t, tOr, localName, localText, errorText } from './i18n.js';
import { h, toast } from './ui.js';
import { loadLevel, profileOf, serverNowEstimate, canEdit } from './state.js';

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

// The device reports no usable R65: the user picks the energy resolution (a plant change).
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

// Hints for one inverter: capability conflicts, observations and energy migration.
export function hintsOf(inv) {
  const s = inv.status;
  const out = [];
  for (const conflict of s.capability_conflicts || []) {
    out.push({ kind: 'conflict', text: conflictText(conflict), detail: conflict.detail || '' });
  }
  const hardware = hardwareHint(s.identity || {});
  if (hardware) out.push({ kind: 'info', text: hardware });
  if (s.energy && s.energy.needs_choice) {
    out.push({ kind: 'energy', text: t('hint.energy.r65_invalid'), actions: energyChoice(inv) });
  }
  for (const hint of s.hints || []) {
    const name = sensorName(inv, hint.key);
    out.push({
      kind: 'observation',
      key: hint.key,
      text: t(`hint.obs.${hint.state}`, { name, n: hint.responses, d: hint.daylight_samples }),
    });
  }
  const energy = s.energy || {};
  // `pending_change` is the candidate resolution (a number) while a change is being confirmed.
  if (typeof energy.pending_change === 'number') {
    out.push({ kind: 'energy', text: t('hint.energy.pending', { value: energy.pending_change }) });
  }
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
