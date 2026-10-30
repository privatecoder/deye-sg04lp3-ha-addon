// Setup assistant for the first start: connection -> inverter -> check -> choose sensors.

import { t, errorText } from './i18n.js';
import { h, busy } from './ui.js';
import { state, canEdit, loadPlant } from './state.js';
import {
  endpointForm, inverterForm, testPanel, loadSerialDevices, saveEndpoint, saveInverter,
  existingEndpointIds, existingInverterIds, transportLabel, endpointTarget,
} from './plantforms.js';
import { noticeBanners } from './plant.js';

// The assistant keeps its place while the page is open (re-rendered by language changes).
const wiz = { step: 1, endpointId: null, inverterId: null };

export function wizardView() {
  if (wiz.step === 4) { wiz.step = 1; wiz.inverterId = null; } // a finished run starts over
  const root = h('div', { class: 'wizard' });
  const body = h('div', { class: 'wizard-body' });
  let serialDevices = [];
  let ready = false;

  function stepper() {
    const labels = ['wiz.step1', 'wiz.step2', 'wiz.step3', 'wiz.step4'];
    return h('ol', { class: 'stepper', 'aria-label': t('wiz.steps') },
      labels.map((key, i) => h('li', { class: i + 1 === wiz.step ? 'current' : i + 1 < wiz.step ? 'done' : '', 'aria-current': i + 1 === wiz.step ? 'step' : null },
        h('span', { class: 'step-no', 'aria-hidden': 'true' }, String(i + 1)), h('span', {}, t(key)))));
  }

  function go(step) {
    wiz.step = step;
    render();
    const heading = body.querySelector('h2');
    if (heading) { heading.setAttribute('tabindex', '-1'); heading.focus({ preventScroll: true }); }
  }

  function step1() {
    const plant = state.plant;
    const message = h('div', { class: 'messages' });
    const form = endpointForm({ editing: false, existingIds: existingEndpointIds(), serialDevices });
    let pick = null;
    const existing = plant.endpoints;
    if (existing.length) {
      pick = h('select', { class: 'select', id: 'wiz-existing' },
        h('option', { value: '' }, '—'),
        existing.map((ep) => h('option', { value: ep.id, selected: ep.id === wiz.endpointId }, `${ep.id} · ${transportLabel(ep.transport)} · ${endpointTarget(ep)}`)));
      pick.addEventListener('change', () => { form.el.hidden = pick.value !== ''; panel.el.hidden = pick.value !== ''; });
    }
    const unit = h('input', { type: 'text', inputmode: 'numeric', class: 'select unit-input', value: '1', 'aria-label': t('ep.test_unit') });
    const panel = testPanel({
      intro: h('div', {}, h('h3', {}, t('ep.test_title')), h('p', { class: 'help' }, t('ep.test_help'))),
      extra: h('div', { class: 'field' }, h('label', {}, t('ep.test_unit')), unit),
      build() {
        if (!form.validate()) return null;
        const id = Number(unit.value);
        if (!Number.isInteger(id) || id < 1 || id > 247) return null;
        return { endpoint: form.values(), unit_id: id };
      },
    });
    const next = h('button', { class: 'btn primary', type: 'button', id: 'wiz-next', disabled: !canEdit() }, t('wiz.save_continue'));
    next.addEventListener('click', async () => {
      if (pick && pick.value) { wiz.endpointId = pick.value; go(2); return; }
      const cont = (id) => { wiz.endpointId = id; go(2); };
      busy(next, true);
      const id = await saveEndpoint(form, { editing: false, message, onLoaded: cont });
      busy(next, false);
      if (id) cont(id);
    });
    const el = h('section', { class: 'card step', 'aria-labelledby': 'wiz-h' },
      h('h2', { id: 'wiz-h' }, t('wiz.step_of', { n: 1 }), ' · ', t('wiz.step1')),
      pick ? h('div', { class: 'field wide' }, h('label', { for: 'wiz-existing' }, t('wiz.use_existing')), pick, h('p', { class: 'help' }, t('wiz.use_existing_help'))) : null,
      form.el, panel.el, message,
      h('div', { class: 'actions' }, next));
    if (pick && wiz.endpointId) { form.el.hidden = true; panel.el.hidden = true; }
    return el;
  }

  function step2() {
    const plant = state.plant;
    const message = h('div', { class: 'messages' });
    const existing = wiz.inverterId ? plant.inverters.find((i) => i.id === wiz.inverterId) : null;
    const form = inverterForm({
      initial: existing, editing: Boolean(existing), endpoints: plant.endpoints,
      existingIds: existingInverterIds(), defaultEndpoint: wiz.endpointId,
    });
    const next = h('button', { class: 'btn primary', type: 'button', id: 'wiz-next', disabled: !canEdit() }, t('wiz.save_continue'));
    next.addEventListener('click', async () => {
      const cont = (id) => { wiz.inverterId = id; go(3); };
      busy(next, true);
      const id = await saveInverter(form, { editing: Boolean(existing), message, onLoaded: cont });
      busy(next, false);
      if (id) cont(id);
    });
    return h('section', { class: 'card step', 'aria-labelledby': 'wiz-h' },
      h('h2', { id: 'wiz-h' }, t('wiz.step_of', { n: 2 }), ' · ', t('wiz.step2')),
      form.el, message,
      h('div', { class: 'actions' }, h('button', { class: 'btn', type: 'button', onClick: () => go(1) }, t('wiz.back')), next));
  }

  function step3() {
    const inv = state.plant.inverters.find((i) => i.id === wiz.inverterId);
    if (!inv) { wiz.step = 2; return step2(); }
    const panel = testPanel({
      intro: h('p', {}, t('wiz.check_intro')),
      build: () => {
        const request = { endpoint_id: inv.endpoint, unit_id: inv.unit_id, inverter: inv.id };
        if (inv.expected_serial) request.expected_serial = inv.expected_serial;
        return request;
      },
    });
    queueMicrotask(() => panel.run());
    return h('section', { class: 'card step', 'aria-labelledby': 'wiz-h' },
      h('h2', { id: 'wiz-h' }, t('wiz.step_of', { n: 3 }), ' · ', t('wiz.step3')),
      panel.el,
      h('div', { class: 'actions' },
        h('button', { class: 'btn', type: 'button', onClick: () => go(2) }, t('wiz.back')),
        h('button', { class: 'btn primary', type: 'button', id: 'wiz-next', onClick: () => go(4) }, t('wiz.check_skip'))));
  }

  function step4() {
    return h('section', { class: 'card step', 'aria-labelledby': 'wiz-h' },
      h('h2', { id: 'wiz-h' }, t('wiz.done_title')),
      h('p', {}, t('wiz.done_text')),
      h('div', { class: 'actions' },
        h('a', { class: 'btn', href: '#/' }, t('wiz.later')),
        h('a', { class: 'btn primary', id: 'wiz-sensors', href: `#/inv/${wiz.inverterId}` }, t('wiz.done_btn'))));
  }

  function render() {
    if (!ready) return;
    const steps = [step1, step2, step3, step4];
    body.replaceChildren(stepper(), steps[wiz.step - 1]());
  }

  root.append(h('h1', {}, t('wiz.title')), h('p', {}, t('wiz.intro')), body);
  Promise.all([loadPlant(), loadSerialDevices()]).then(([plant, devices]) => {
    serialDevices = devices;
    ready = true;
    // a start with a finished chain continues where it ended; an unknown inverter restarts at step 1
    if (wiz.step >= 3 && !plant.inverters.some((i) => i.id === wiz.inverterId)) { wiz.step = 1; wiz.inverterId = null; }
    root.insertBefore(h('div', {}, ...noticeBanners(plant.notices)), body);
    render();
  }).catch((error) => body.replaceChildren(h('p', { class: 'banner error', role: 'alert' }, errorText(error))));
  return { el: root, canLeave: () => true };
}
