// Overview: one card per inverter with state, path, identity, hints and bus load.

import { t, problemText } from './i18n.js';
import { h } from './ui.js';
import { state, canEdit, loadLevel, hasPlant } from './state.js';
import { stateChip, reasonText, roleText, ageText, absoluteTime, levelChip, loadProgress, hintsOf, hintBody } from './common.js';
import { openTransfer } from './transfer.js';
import { maintenanceSection } from './maintenance.js';
import { noticeBanners } from './plant.js';

const HINTS_SHOWN = 4;
const expandedHints = new Set(); // inverter ids whose full hint list is open

export function overviewView() {
  const root = h('div', { class: 'overview' });
  const timeRefs = [];

  function card(inv) {
    const s = inv.status;
    const level = loadLevel(s.bus_utilisation);
    const hints = hintsOf(inv);
    const lastQuery = h('span', { title: absoluteTime(s.last_success_wall) }, ageText(s.last_success_wall));
    timeRefs.push({ el: lastQuery, wall: s.last_success_wall });
    const sel = inv.selection;
    const deviations = sel.deviations.added.length + sel.deviations.removed.length;
    const unidentified = s.serial === null || s.serial === undefined;
    const headingId = `inv-${inv.id}-title`;
    return h('article', { class: `card inverter state-${s.state}`, 'aria-labelledby': headingId },
      h('header', { class: 'card-head' },
        h('div', {},
          h('h2', { id: headingId }, inv.name),
          h('p', { class: 'muted' }, inv.id)),
        stateChip(s)),
      h('p', { class: 'reason' }, reasonText(s)),
      s.awaiting_selection
        ? h('div', { class: 'banner warn awaiting', role: 'status' },
            h('strong', {}, t('overview.awaiting')),
            h('p', {}, t('overview.awaiting_text')),
            h('a', { class: 'btn primary', href: `#/inv/${inv.id}` }, t('overview.awaiting_btn')))
        : null,
      s.reason && !['identified', 'starting'].includes(s.reason)
        ? h('details', { class: 'tech' }, h('summary', {}, t('overview.technical')), h('code', {}, s.reason))
        : null,
      h('dl', { class: 'facts' },
        fact(t('overview.path'), `${t(`transport.${inv.connection.transport}`)} · ${inv.connection.endpoint} (${t(`path.${s.active_path}`)})`),
        fact(t('overview.serial'), unidentified ? t('overview.not_identified') : s.serial),
        fact(t('overview.role'), unidentified ? t('value.unknown') : roleText(s)),
        fact(t('overview.last_query'), lastQuery),
        fact(t('overview.selection'), s.awaiting_selection ? t('overview.selection_none') : t('overview.selection_value', { n: sel.effective.length, d: deviations })),
        fact(t('overview.bus'), h('span', { class: 'bus' }, levelChip(level, t(`load.${level}`)), loadProgress(s.bus_utilisation, level)))),
      unidentified && s.state !== 'blocked'
        ? h('p', { class: 'note' }, t('overview.unidentified_hint'))
        : null,
      hintList(inv.id, hints),
      h('footer', { class: 'card-actions' },
        h('a', { class: 'btn primary', href: `#/inv/${inv.id}` }, t('overview.setup')),
        state.inverters.length > 1
          ? h('button', { class: 'btn', type: 'button', disabled: !canEdit(), onClick: () => openTransfer(inv.id) }, t('overview.transfer'))
          : null));
  }

  // The first hints are always shown; the rest opens with a button (state kept while the page lives).
  function hintList(id, hints) {
    if (!hints.length) return null;
    const open = expandedHints.has(id);
    const rest = hints.slice(HINTS_SHOWN);
    const toggle = h('button', { class: 'btn small linklike', type: 'button', 'aria-expanded': String(open), 'aria-controls': `hints-more-${id}` },
      open ? t('overview.show_less') : t('overview.show_more', { n: rest.length }));
    toggle.addEventListener('click', () => {
      if (expandedHints.has(id)) expandedHints.delete(id); else expandedHints.add(id);
      signature = '';
      onData();
      const again = root.querySelector(`[aria-controls="hints-more-${id}"]`);
      if (again) again.focus();
    });
    return h('div', { class: 'hint-block' },
      h('ul', { class: 'hints', 'aria-label': t('overview.hints') },
        hints.slice(0, HINTS_SHOWN).map((hint) => h('li', { class: `hint ${hint.kind}` }, hintBody(hint))),
        open ? rest.map((hint) => h('li', { class: `hint ${hint.kind}` }, hintBody(hint))) : null),
      rest.length ? h('div', { id: `hints-more-${id}` }, toggle) : null);
  }

  const TOTAL_ROWS = [
    ['plant_pv_power', 'W'], ['plant_load_power', 'W'], ['plant_battery_power', 'W'], ['plant_inverter_power', 'W'],
    ['plant_battery_soc', '%'], ['plant_pv_energy_total', 'kWh'], ['plant_load_energy_total', 'kWh'],
    ['plant_battery_charge_energy_total', 'kWh'], ['plant_battery_discharge_energy_total', 'kWh'],
  ];

  function totalsValue(value, unit) {
    if (value === null || value === undefined) return t('value.unknown');
    return `${value.toLocaleString(state.lang, { maximumFractionDigits: unit === 'kWh' ? 1 : 0 })} ${unit}`;
  }

  // The plant card: totals over the member inverters plus the member status.
  function plantCard() {
    const totals = state.totals;
    if (!totals || !totals.active) return null;
    return h('article', { class: 'card plant-totals', id: 'plant-card', 'aria-labelledby': 'plant-card-title' },
      h('header', { class: 'card-head' }, h('div', {}, h('h2', { id: 'plant-card-title' }, t('overview.plant')), h('p', { class: 'muted' }, t('overview.plant_sub', { n: totals.members.length })))),
      h('dl', { class: 'facts' },
        TOTAL_ROWS.map(([key, unit]) => fact(t(`totals.${key}`), h('span', { 'data-total': key }, totalsValue(totals.values[key], unit))))),
      h('ul', { class: 'members', 'aria-label': t('overview.plant_members') },
        totals.members.map((m) => h('li', { class: m.stale ? 'hint warn' : 'hint info', 'data-member': m.id },
          `${m.name}${m.role === 'master' ? ` (${t('totals.master')})` : ''}: `,
          m.online ? (m.stale ? t('overview.member_stale') : t('overview.member_ok')) : t('overview.member_offline')))),
      totals.master ? null : h('p', { class: 'note' }, t('overview.plant_no_master')));
  }

  function fact(label, value) {
    return h('div', { class: 'fact' }, h('dt', {}, label), h('dd', {}, value));
  }

  function render() {
    timeRefs.length = 0;
    root.replaceChildren();
    const problems = state.session ? state.session.problems : [];
    if (problems.length) {
      root.append(h('section', { class: 'banner warn', role: 'alert' },
        h('strong', {}, t('overview.store_problems')),
        h('ul', {}, problems.map((p) => h('li', {}, problemText(p))))));
    }
    if (hasPlant() && state.session.plant_notices) root.append(...noticeBanners(state.session.plant_notices));
    if (!canEdit()) root.append(h('p', { class: 'banner info' }, t('overview.readonly')));
    if (!state.inverters.length) {
      root.append(h('section', { class: 'empty' }, h('h1', {}, t('overview.empty_title')), h('p', {}, t('overview.empty_text')),
        hasPlant() ? h('a', { class: 'btn primary', id: 'start-setup', href: '#/start' }, t('overview.empty_btn')) : null));
    } else {
      root.append(h('h1', {}, t('overview.title')),
        h('div', { class: 'cards' }, plantCard(), state.inverters.map(card)));
    }
    root.append(maintenanceSection());
  }

  let signature = '';
  function onData() {
    const next = JSON.stringify([state.session && state.session.plant_notices, state.inverters.map((i) => ({ ...i, status: { ...i.status, last_success_wall: null } })), state.session && state.session.problems, state.totals, state.lang, [...expandedHints]]);
    if (next !== signature) {
      signature = next;
      // Keep an open "Maintenance" section open across re-renders.
      const open = root.querySelector('details.maintenance[open]') !== null;
      render();
      if (open) root.querySelector('details.maintenance').open = true;
    } else {
      for (const ref of timeRefs) ref.el.textContent = ageText(ref.wall);
    }
  }

  onData();
  return { el: root, onData, tick: () => onData() };
}
