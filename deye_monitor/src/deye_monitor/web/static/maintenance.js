// Maintenance (collapsed by default): rebind identity, reset stores, empty the cleanup ledger.
// Every action explains its effect and needs a typed confirmation.

import { t, errorText } from './i18n.js';
import { h, uid, modal, toast, busy } from './ui.js';
import { ApiError } from './api.js';
import { state, canEdit, loadAll, loadInverters, actionToken, actWith } from './state.js';

const RESET_ALL = 'reset-all-identity-bindings';
const PURGE = 'purge-cleanup-ledger';

// Dialog with a warning text and a text field: the button stays disabled until the expected
// text is typed exactly. ``run`` performs the call and throws on failure.
function confirmDialog({ title, warning, effects, expected, global = false, actionLabel, token, run }) {
  return modal({
    title,
    build(close) {
      const inputId = uid('confirm');
      const input = h('input', { type: 'text', id: inputId, class: 'confirm-input', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', 'aria-describedby': `${inputId}-help` });
      let outdated = false;
      const message = h('div', { class: 'messages' });
      const go = h('button', { class: 'btn danger', type: 'button', disabled: true }, actionLabel);
      input.addEventListener('input', () => { go.disabled = outdated || input.value.trim() !== expected; });
      go.addEventListener('click', async () => {
        busy(go, true);
        message.replaceChildren();
        try {
          await run(token);
          close(true);
        } catch (error) {
          if (error instanceof ApiError && error.key === 'stale_revision') {
            // The state changed since this dialog was opened: show the new state, no silent retry.
            outdated = true;
            message.append(h('p', { class: 'banner warn', role: 'alert' }, t('maint.outdated')));
            loadAll().catch(() => {});
          } else message.append(h('p', { class: 'banner error', role: 'alert' }, errorText(error)));
          busy(go, false);
          go.disabled = outdated || input.value.trim() !== expected;
        }
      });
      return h('div', { class: 'modal-body' },
        global ? h('p', { class: 'banner error global', role: 'alert' }, h('strong', {}, t('maint.global_badge')), ` ${t('maint.global_text')}`) : null,
        h('p', { class: 'banner warn' }, warning),
        effects && effects.length ? h('ul', { class: 'effects' }, effects.map((e) => h('li', {}, e))) : null,
        h('label', { for: inputId }, t('maint.type_to_confirm', { text: expected })),
        h('p', { class: 'help', id: `${inputId}-help` }, h('code', {}, expected)),
        input, message,
        h('div', { class: 'actions' }, h('button', { class: 'btn', type: 'button', onClick: () => close(false) }, t('common.cancel')), go));
    },
  });
}

function inverterSelect(id, label) {
  const select = h('select', { id, class: 'select', disabled: !canEdit() },
    state.inverters.map((i) => h('option', { value: i.id }, `${i.name} (${i.id})`)));
  return { select, field: h('div', { class: 'field' }, h('label', { for: id }, label), select) };
}

export function maintenanceSection() {
  const details = h('details', { class: 'maintenance card' }, h('summary', {}, h('h2', {}, t('maint.title'))));
  if (!state.inverters.length) {
    details.append(h('p', { class: 'muted' }, t('maint.nothing')));
    return details;
  }
  details.append(h('p', { class: 'banner warn' }, t('maint.intro')));

  // 1. rebind
  const rebind = inverterSelect(uid('rb'), t('maint.pick_inverter'));
  const rebindBtn = h('button', { class: 'btn danger', type: 'button', disabled: !canEdit(), onClick: async () => {
    const id = rebind.select.value;
    const inv = state.inverters.find((i) => i.id === id);
    if (inv.connection.serial_pinned) { toast(t('maint.rebind_pinned'), 'error'); return; }
    const token = await actionToken();
    const done = await confirmDialog({
      token, title: t('maint.rebind_title'), warning: t('maint.rebind_warning'), effects: [t('maint.rebind_effect1'), t('maint.rebind_effect2')],
      expected: id, actionLabel: t('maint.rebind_do'),
      run: (tk) => actWith(tk, `api/inverters/${id}/rebind`, { confirm: true }),
    });
    if (done) { toast(t('maint.rebind_done', { name: inv.name }), 'ok'); await loadInverters(); }
  } }, t('maint.rebind_btn'));
  details.append(h('section', { class: 'maint-block' }, h('h3', {}, t('maint.rebind_title')), h('p', {}, t('maint.rebind_text')), rebind.field, rebindBtn));

  // 2. store reset
  const reset = inverterSelect(uid('rs'), t('maint.pick_inverter'));
  const kindId = uid('kind');
  const kind = h('select', { id: kindId, class: 'select', disabled: !canEdit() },
    h('option', { value: 'energy' }, t('maint.kind.energy')), h('option', { value: 'identity' }, t('maint.kind.identity')));
  const resetBtn = h('button', { class: 'btn danger', type: 'button', disabled: !canEdit(), onClick: async () => {
    const id = reset.select.value;
    const k = kind.value;
    const token = await actionToken();
    const done = await confirmDialog({
      token, title: t(`maint.reset_${k}_title`), warning: t(`maint.reset_${k}_warning`), effects: [t(`maint.reset_${k}_effect`)],
      expected: id, actionLabel: t('maint.reset_do'),
      run: (tk) => actWith(tk, 'api/stores/reset', { kind: k, scope: 'inverter', inverter: id, confirm: true }),
    });
    if (done) { toast(t('maint.reset_done'), 'ok'); await loadAll(); }
  } }, t('maint.reset_btn'));
  const globalBtn = h('button', { class: 'btn danger', type: 'button', disabled: !canEdit(), onClick: async () => {
    const token = await actionToken();
    const done = await confirmDialog({
      token, title: t('maint.global_title'), warning: t('maint.global_warning'), effects: [t('maint.reset_identity_effect')], global: true,
      expected: RESET_ALL, actionLabel: t('maint.global_do'),
      run: (tk) => actWith(tk, 'api/stores/reset', { kind: 'identity', scope: 'global', confirm: RESET_ALL }),
    });
    if (done) { toast(t('maint.reset_done'), 'ok'); await loadAll(); }
  } }, t('maint.global_btn'));
  details.append(h('section', { class: 'maint-block' }, h('h3', {}, t('maint.reset_title')), h('p', {}, t('maint.reset_text')),
    reset.field, h('div', { class: 'field' }, h('label', { for: kindId }, t('maint.kind')), kind), resetBtn,
    h('div', { class: 'global-zone' }, h('h4', {}, t('maint.global_badge')), h('p', {}, t('maint.global_text')), globalBtn)));

  // 3. cleanup ledger
  const purge = inverterSelect(uid('pg'), t('maint.pick_inverter'));
  const scopeId = uid('scope');
  const scope = h('select', { id: scopeId, class: 'select', disabled: !canEdit() },
    h('option', { value: 'inverter' }, t('maint.purge_scope_inverter')), h('option', { value: 'all' }, t('maint.purge_scope_all')));
  scope.addEventListener('change', () => { purge.field.hidden = scope.value === 'all'; });
  const purgeBtn = h('button', { class: 'btn danger', type: 'button', disabled: !canEdit(), onClick: async () => {
    const all = scope.value === 'all';
    const id = purge.select.value;
    const token = await actionToken();
    const done = await confirmDialog({
      token, title: t('maint.purge_title'), warning: t('maint.purge_warning'), effects: [t('maint.purge_effect')], global: all,
      expected: PURGE, actionLabel: t('maint.purge_do'),
      run: (tk) => actWith(tk, 'api/cleanup/purge', { scope: all ? 'all' : 'inverter', ...(all ? {} : { inverter: id }), confirm: PURGE }),
    });
    if (done) toast(t('maint.purge_done'), 'ok');
  } }, t('maint.purge_btn'));
  details.append(h('section', { class: 'maint-block' }, h('h3', {}, t('maint.purge_title')), h('p', {}, t('maint.purge_text')),
    h('div', { class: 'field' }, h('label', { for: scopeId }, t('maint.purge_scope')), scope), purge.field, purgeBtn));
  return details;
}
