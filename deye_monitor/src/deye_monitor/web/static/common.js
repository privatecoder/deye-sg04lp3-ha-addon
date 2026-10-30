// Pieces shared by the views: translated texts of states, errors, hints and the load light.

import { t, tOr, localName, localText, errorText } from './i18n.js';
import { h } from './ui.js';
import { loadLevel, profileOf, serverNowEstimate } from './state.js';

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

export function roleText(status) {
  const role = status.identity && status.identity.role;
  if (!role || !role.parallel_enable) return role ? t('role.single') : t('value.unknown');
  const master = role.master === true || role.master === 1 || role.master === 'master';
  const phase = role.phase !== null && role.phase !== undefined ? ` · ${t('role.phase', { phase: role.phase })}` : '';
  return (master ? t('role.master') : t('role.slave')) + phase;
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

// Hints for one inverter: capability conflicts, observations and energy migration.
export function hintsOf(inv) {
  const s = inv.status;
  const out = [];
  for (const conflict of s.capability_conflicts || []) {
    out.push({ kind: 'conflict', text: tOr(`hint.conflict.${conflict.code}`, 'hint.conflict.generic', { code: conflict.code, detail: conflict.detail || '' }) });
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
  return out;
}

export function describe(entry) {
  return localText(entry.description) || '';
}
