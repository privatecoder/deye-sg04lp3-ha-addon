// Overview: one card per inverter with state, path, identity, hints and bus load.

import { t, problemText } from './i18n.js';
import { h } from './ui.js';
import { state, canEdit, loadLevel } from './state.js';
import { stateChip, reasonText, roleText, ageText, absoluteTime, levelChip, loadProgress, hintsOf } from './common.js';
import { openTransfer } from './transfer.js';
import { maintenanceSection } from './maintenance.js';

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
      hints.length
        ? h('ul', { class: 'hints', 'aria-label': t('overview.hints') },
            hints.slice(0, 4).map((hint) => h('li', { class: `hint ${hint.kind}` }, hint.text)),
            hints.length > 4 ? h('li', { class: 'muted' }, t('overview.more_hints', { n: hints.length - 4 })) : null)
        : null,
      h('footer', { class: 'card-actions' },
        h('a', { class: 'btn primary', href: `#/inv/${inv.id}` }, t('overview.setup')),
        state.inverters.length > 1
          ? h('button', { class: 'btn', type: 'button', disabled: !canEdit(), onClick: () => openTransfer(inv.id) }, t('overview.transfer'))
          : null));
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
    if (!canEdit()) root.append(h('p', { class: 'banner info' }, t('overview.readonly')));
    if (!state.inverters.length) {
      root.append(h('section', { class: 'empty' }, h('h2', {}, t('overview.empty_title')), h('p', {}, t('overview.empty_text'))));
    } else {
      root.append(h('h1', {}, t('overview.title')),
        h('div', { class: 'cards' }, state.inverters.map(card)));
    }
    root.append(maintenanceSection());
  }

  let signature = '';
  function onData() {
    const next = JSON.stringify([state.inverters.map((i) => ({ ...i, status: { ...i.status, last_success_wall: null } })), state.session && state.session.problems, state.lang]);
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
