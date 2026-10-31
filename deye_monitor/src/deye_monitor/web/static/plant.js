// "Set up plant": connections and inverters, edited here (no YAML, no app options).

import { t } from './i18n.js';
import { h, modal, toast, busy } from './ui.js';
import { state, canEdit, loadPlant } from './state.js';
import {
  endpointForm, inverterForm, testPanel, connectionTest, connectionUsers, routeChecks, savedInverterChecks, loadSerialDevices, transportLabel, endpointTarget,
  saveEndpoint, saveInverter, saveTotals, deleteEndpoint, deleteInverter, existingEndpointIds, existingInverterIds,
} from './plantforms.js';

export function noticeBanners(notices) {
  return (notices || []).map((n) => {
    const key = `plant.notice.${n.code}`;
    return h('p', { class: 'banner warn plant-notice', role: 'status' }, t(key));
  });
}

// Dialog: add or edit a connection (with the read-only try-out).
export async function openEndpointDialog(endpoint = null) {
  const editing = endpoint !== null;
  const serialDevices = await loadSerialDevices();
  const form = endpointForm({ initial: endpoint, editing, existingIds: existingEndpointIds(), serialDevices });
  const message = h('div', { class: 'messages' });
  const test = connectionTest(form, connectionUsers(editing ? endpoint.id : null));
  const panel = testPanel({
    intro: h('div', {}, h('h3', {}, t('ep.test_title')), h('p', { class: 'help' }, t('ep.test_help'))),
    extra: test.el,
    build: test.build,
  });
  return modal({
    title: editing ? t('ep.title_edit') : t('ep.title_new'),
    wide: true,
    build(close) {
      const save = h('button', { class: 'btn primary', type: 'button', disabled: !canEdit() }, t('ep.save'));
      save.addEventListener('click', async () => {
        const done = (id) => { toast(t('ep.saved'), 'ok'); close(id); };
        busy(save, true);
        const id = await saveEndpoint(form, { editing, message, onLoaded: done });
        busy(save, false);
        if (id) done(id);
      });
      return h('div', { class: 'modal-body' }, form.el, panel.el, message,
        h('div', { class: 'actions' }, h('button', { class: 'btn', type: 'button', onClick: () => close(null) }, t('common.cancel')), save));
    },
  });
}

// Dialog: add or edit an inverter.
export function openInverterDialog(inverter = null, defaultEndpoint = null) {
  const editing = inverter !== null;
  const form = inverterForm({ initial: inverter, editing, endpoints: state.plant.endpoints, existingIds: existingInverterIds(), defaultEndpoint });
  const message = h('div', { class: 'messages' });
  const panel = testPanel({
    intro: h('h3', {}, t('plant.check')),
    build() {
      const endpoint = form.endpointValue();
      const unit = form.unitValue();
      const fallback = form.fallbackValue();
      if (!endpoint || typeof unit !== 'number' || (fallback && typeof fallback.unit !== 'number')) { message.replaceChildren(h('p', { class: 'banner warn', role: 'alert' }, t('test.need_endpoint'))); return null; }
      message.replaceChildren();
      const values = form.values();
      const extra = {};
      if (editing) extra.inverter = values.id;
      if (values.expected_serial) extra.expected_serial = values.expected_serial;
      return routeChecks([{ endpoint, unit, ...extra }, ...(fallback ? [{ ...fallback, ...extra }] : [])], state.plant.endpoints);
    },
  });
  return modal({
    title: editing ? t('inv.title_edit') : t('inv.title_new'),
    wide: true,
    build(close) {
      const save = h('button', { class: 'btn primary', type: 'button', disabled: !canEdit() }, t('inv.save'));
      save.addEventListener('click', async () => {
        const done = (id) => { toast(t('inv.saved'), 'ok'); close(id); };
        busy(save, true);
        const id = await saveInverter(form, { editing, message, onLoaded: done });
        busy(save, false);
        if (id) done(id);
      });
      return h('div', { class: 'modal-body' }, form.el, h('p', { class: 'note' }, t('inv.apply_note')), panel.el, message,
        h('div', { class: 'actions' }, h('button', { class: 'btn', type: 'button', onClick: () => close(null) }, t('common.cancel')), save));
    },
  });
}

// Dialog: check a saved inverter.
function openCheckDialog(inv) {
  const panel = testPanel({
    intro: h('p', { class: 'help' }, t('ep.test_help')),
    build: () => {
      return savedInverterChecks(inv, state.plant.endpoints);
    },
  });
  return modal({
    title: `${t('plant.check')} · ${inv.name}`,
    build: (close) => h('div', { class: 'modal-body' }, panel.el,
      h('div', { class: 'actions' }, h('button', { class: 'btn', type: 'button', onClick: () => close(null) }, t('common.close')))),
  });
}

function confirmEndpointDelete(ep) {
  const message = h('div', { class: 'messages' });
  return modal({
    title: t('ep.delete_title'),
    build(close) {
      const go = h('button', { class: 'btn danger', type: 'button' }, t('ep.delete_do'));
      go.addEventListener('click', async () => {
        const done = () => { toast(t('ep.deleted'), 'ok'); close(true); };
        busy(go, true);
        const result = await deleteEndpoint(ep.id, { message, onLoaded: done });
        busy(go, false);
        if (result) done();
      });
      return h('div', { class: 'modal-body' }, h('p', {}, t('ep.delete_text', { id: ep.id })), message,
        h('div', { class: 'actions' }, h('button', { class: 'btn', type: 'button', onClick: () => close(false) }, t('common.cancel')), go));
    },
  });
}

function confirmInverterDelete(inv) {
  const message = h('div', { class: 'messages' });
  return modal({
    title: t('inv.delete_title'),
    build(close) {
      const binding = h('input', { type: 'checkbox', id: 'delete-binding' });
      const go = h('button', { class: 'btn danger', type: 'button' }, t('inv.delete_do'));
      go.addEventListener('click', async () => {
        const done = (result) => {
          toast(t('inv.deleted'), 'ok');
          if ((result.notes || []).includes('binding_not_removed')) toast(t('inv.deleted_binding_failed'), 'error');
          if ((result.notes || []).includes('prune_pending')) toast(t('inv.deleted_prune_pending'), 'error');
          close(true);
        };
        busy(go, true);
        const result = await deleteInverter(inv.id, binding.checked, { message, onLoaded: done });
        busy(go, false);
        if (result) done(result);
      });
      return h('div', { class: 'modal-body' },
        h('p', { class: 'banner warn' }, t('inv.delete_text', { name: inv.name })),
        h('label', { class: 'option', for: 'delete-binding' }, binding,
          h('span', { class: 'opt-text' }, h('span', { class: 'opt-title' }, t('inv.delete_binding')), h('span', { class: 'opt-help' }, t('inv.delete_binding_help')))),
        message,
        h('div', { class: 'actions' }, h('button', { class: 'btn', type: 'button', onClick: () => close(false) }, t('common.cancel')), go));
    },
  });
}

export function plantView() {
  const root = h('div', { class: 'plant' });
  let signature = '';

  function endpointCard(ep) {
    const users = ep.used_by;
    return h('article', { class: 'card plant-item endpoint', 'data-endpoint': ep.id },
      h('header', { class: 'card-head' },
        h('div', {}, h('h3', {}, ep.id), h('p', { class: 'muted' }, transportLabel(ep.transport)))),
      h('p', { class: 'target-line' }, h('code', {}, endpointTarget(ep))),
      users.length
        ? h('p', { class: 'note' }, `${t('plant.used_by')}: `, users.map((u) => `${u.inverter}${u.role === 'fallback' ? ` (${t('plant.role.fallback')})` : ''}`).join(', '))
        : h('p', { class: 'note' }, t('plant.unused')),
      h('footer', { class: 'card-actions' },
        h('button', { class: 'btn small', type: 'button', disabled: !canEdit(), onClick: () => edit(ep) }, t('plant.edit')),
        h('button', { class: 'btn small', type: 'button', disabled: !canEdit() || users.length > 0, title: users.length ? t('plant.delete_blocked') : null, onClick: () => remove(ep) }, t('plant.delete'))));
  }

  function inverterCard(inv) {
    const fallback = inv.fallback_endpoint ? `${inv.fallback_endpoint} · ${inv.fallback_unit_id ?? inv.unit_id}` : t('plant.none');
    const fact = (label, value) => h('div', { class: 'fact' }, h('dt', {}, label), h('dd', {}, value));
    return h('article', { class: 'card plant-item inverter', 'data-inverter': inv.id },
      h('header', { class: 'card-head' }, h('div', {}, h('h3', {}, inv.name), h('p', { class: 'muted' }, inv.id))),
      h('dl', { class: 'facts' },
        fact(t('plant.inv.connection'), inv.endpoint),
        fact(t('plant.inv.unit'), String(inv.unit_id)),
        fact(t('plant.inv.fallback'), fallback),
        inv.expected_serial ? fact(t('plant.inv.serial'), inv.expected_serial) : null,
        fact(t('plant.inv.writes'), t('plant.writes_unavailable'))),
      h('footer', { class: 'card-actions' },
        h('button', { class: 'btn small primary', type: 'button', disabled: !canEdit(), onClick: () => checkInverter(inv) }, t('plant.check')),
        h('a', { class: 'btn small', href: `#/inv/${inv.id}` }, t('plant.pick_sensors')),
        h('button', { class: 'btn small', type: 'button', disabled: !canEdit(), onClick: () => editInverter(inv) }, t('plant.edit')),
        h('button', { class: 'btn small', type: 'button', disabled: !canEdit(), onClick: () => removeInverter(inv) }, t('plant.delete'))));
  }

  // The page is rebuilt after a dialog; focus returns to the control that opened it.
  function describeOpener(el) {
    const art = el && el.closest ? el.closest('article[data-endpoint], article[data-inverter]') : null;
    let article = null;
    if (art) article = art.dataset.endpoint ? `article[data-endpoint="${art.dataset.endpoint}"]` : `article[data-inverter="${art.dataset.inverter}"]`;
    return { id: el && el.id ? el.id : null, article, text: el ? el.textContent : '' };
  }

  function restoreFocus(opener) {
    if (opener.id) {
      const el = root.querySelector(`#${opener.id}`);
      if (el) el.focus();
      return;
    }
    const article = opener.article ? root.querySelector(opener.article) : null;
    if (!article) return;
    const buttons = [...article.querySelectorAll('button, a')];
    (buttons.find((b) => b.textContent === opener.text && !b.disabled) || buttons.find((b) => !b.disabled) || article).focus?.();
  }

  // "Plant (totals)": which inverters the totals add up (default: all).
  function totalsSection(plant) {
    const totals = plant.totals || { members: [], allow_single: false };
    const members = new Set(totals.members);
    const boxes = plant.inverters.map((inv) => h('label', { class: 'check', 'data-member': inv.id },
      h('input', { type: 'checkbox', value: inv.id, checked: members.has(inv.id), disabled: !canEdit() }), ` ${inv.name} (${inv.id})`));
    const single = h('input', { type: 'checkbox', id: 'totals-single', checked: totals.allow_single, disabled: !canEdit() });
    const message = h('div');
    const button = h('button', { class: 'btn primary', id: 'save-totals', type: 'button', disabled: !canEdit() }, t('plant.totals_save'));
    button.addEventListener('click', async () => {
      const chosen = boxes.map((b) => b.querySelector('input')).filter((i) => i.checked).map((i) => i.value);
      busy(button, true);
      const result = await saveTotals(chosen, single.checked, { message });
      busy(button, false);
      if (result) {
        render();
        toast(t('plant.totals_saved'));
      }
    });
    return h('section', { class: 'plant-section', id: 'plant-totals', 'aria-labelledby': 'plant-totals-title' },
      h('h2', { id: 'plant-totals-title' }, t('plant.totals')),
      h('p', { class: 'help' }, t('plant.totals_intro')),
      h('div', { class: 'checks' }, boxes),
      h('label', { class: 'check' }, single, ` ${t('plant.totals_single')}`),
      h('p', { class: 'note' }, totals.active ? t('plant.totals_active', { n: totals.members.length }) : t('plant.totals_inactive')),
      message,
      h('div', { class: 'actions left' }, button));
  }

  async function around(run) {
    const opener = describeOpener(document.activeElement);
    await run();
    render();
    restoreFocus(opener);
  }

  const edit = (ep) => around(() => openEndpointDialog(ep));
  const remove = (ep) => around(() => confirmEndpointDelete(ep));
  const editInverter = (inv) => around(() => openInverterDialog(inv));
  const removeInverter = (inv) => around(() => confirmInverterDelete(inv));
  const addEndpoint = () => around(() => openEndpointDialog());
  const addInverter = () => around(() => openInverterDialog());
  const checkInverter = (inv) => around(() => openCheckDialog(inv));

  function render() {
    const plant = state.plant;
    root.replaceChildren();
    if (!plant) return;
    signature = JSON.stringify([plant, state.lang]);
    root.append(h('h1', {}, t('plant.title')), h('p', {}, t('plant.intro')), ...noticeBanners(plant.notices));
    if (!canEdit()) root.append(h('p', { class: 'banner info' }, t('overview.readonly')));
    root.append(
      h('section', { class: 'plant-section', 'aria-labelledby': 'plant-eps' },
        h('h2', { id: 'plant-eps' }, t('plant.endpoints')),
        h('p', { class: 'help' }, t('plant.endpoints_intro')),
        plant.endpoints.length ? h('div', { class: 'cards' }, plant.endpoints.map(endpointCard)) : h('p', { class: 'muted' }, t('plant.endpoints_empty')),
        h('div', { class: 'actions left' },
          h('button', { class: 'btn primary', id: 'add-endpoint', type: 'button', disabled: !canEdit(), onClick: addEndpoint }, t('plant.add_endpoint')))),
      h('section', { class: 'plant-section', 'aria-labelledby': 'plant-invs' },
        h('h2', { id: 'plant-invs' }, t('plant.inverters')),
        h('p', { class: 'help' }, t('plant.inverters_intro')),
        plant.inverters.length ? h('div', { class: 'cards' }, plant.inverters.map(inverterCard)) : h('p', { class: 'muted' }, plant.endpoints.length ? t('plant.inverters_empty') : t('plant.inverters_need_endpoint')),
        h('div', { class: 'actions left' },
          h('button', { class: 'btn primary', id: 'add-inverter', type: 'button', disabled: !canEdit() || plant.endpoints.length === 0, onClick: addInverter }, t('plant.add_inverter')))),
      plant.inverters.length ? totalsSection(plant) : null);
  }

  async function onData() {
    // somebody else changed the plant (or this page did, from another tab)
    if (state.plantRevision !== undefined && state.plant && state.plantRevision !== state.plant.revision && !document.querySelector('dialog[open]')) {
      await loadPlant();
      render();
    }
  }

  loadPlant().then(render).catch(() => render());
  render();
  return { el: root, onData, tick: onData };
}
