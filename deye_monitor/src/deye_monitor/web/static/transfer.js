// "Transfer to other inverters": preview per target, then execute with the previewed revision.

import { ApiError } from './api.js';
import { t, errorText } from './i18n.js';
import { h, uid, modal, toast, busy } from './ui.js';
import { state, inverterById, loadInverters, mutate } from './state.js';
import { levelChip, busChip, budgetLevel, sensorList } from './common.js';

export async function openTransfer(sourceId) {
  await loadInverters();
  const source = inverterById(sourceId);
  if (!source) return;
  const targets = state.inverters.filter((i) => i.id !== sourceId);
  await modal({
    title: t('transfer.title', { name: source.name }),
    wide: true,
    build(close) {
      const chosen = new Set(targets.map((i) => i.id));
      let includeFeatures = true;
      let previewData = null;
      let revision = null;
      // Every change of targets or features bumps the generation: an answer of an older preview
      // is dropped, and "execute" sends exactly the payload that was previewed.
      let generation = 0;
      let previewed = null; // { source, targets, include_features } as sent with the preview
      let running = false;
      let body = null;
      const result = h('div', { class: 'transfer-result', 'aria-live': 'polite' });
      const message = h('div', { class: 'messages' });
      const run = h('button', { class: 'btn primary', type: 'button', disabled: true }, t('transfer.execute'));
      const cancelBtn = h('button', { class: 'btn', type: 'button', onClick: () => close(false) }, t('common.cancel'));

      // A change of targets or options invalidates the preview and loads a new one at once.
      const stale = () => { generation += 1; previewData = null; previewed = null; revision = null; run.disabled = true; result.replaceChildren(); };
      const restale = () => { stale(); doPreview(); };
      const lock = (locked) => { running = locked; for (const input of body.querySelectorAll('input')) input.disabled = locked; cancelBtn.disabled = locked; };

      function targetCard(id, p) {
        const inv = inverterById(id);
        if (!p.ok) {
          return h('section', { class: 'target skipped' }, h('h3', {}, inv.name),
            h('p', { class: 'banner warn' }, `${t('transfer.skipped')}: ${errorText({ key: p.error.key, params: p.error.params })}`));
        }
        const level = budgetLevel(p.selection.budget);
        return h('section', { class: 'target' }, h('h3', {}, inv.name, h('span', { class: 'muted id' }, ` ${id}`)),
          h('p', {}, h('span', { class: 'plus' }, `+ ${t('setup.adds', { n: p.adds.length })}`), '  ',
            h('span', { class: 'minus' }, `− ${t('setup.drops', { n: p.drops.length })}`), '  ', levelChip(level, `${t('setup.query_load')}: ${t(`load.${level}`)}`), ' ', busChip(p.selection.budget)),
          p.adds.length ? h('details', {}, h('summary', {}, t('transfer.adds_list')), h('p', {}, sensorList(inv, p.adds))) : null,
          p.drops.length ? h('details', {}, h('summary', {}, t('transfer.drops_list')), h('p', {}, sensorList(inv, p.drops))) : null,
          p.inactive && p.inactive.length ? h('p', { class: 'banner info', 'data-inactive': 'true' }, t('transfer.inactive', { names: sensorList(inv, p.inactive) })) : null,
          p.capability_warnings.length ? h('p', { class: 'banner warn' }, t('setup.cap_warning', { names: sensorList(inv, p.capability_warnings) })) : null,
          p.selection.budget.overloaded ? h('p', { class: 'banner warn' }, t('setup.overloaded')) : null);
      }

      async function doPreview() {
        message.replaceChildren();
        stale();
        if (!chosen.size) { message.append(h('p', { class: 'banner error', role: 'alert' }, t('transfer.none_chosen'))); return; }
        const mine = generation;
        const payload = { source: sourceId, targets: [...chosen], include_features: includeFeatures };
        try {
          const data = await mutate('POST', 'api/transfer/preview', payload);
          if (mine !== generation) return; // the inputs changed meanwhile: this answer is outdated
          previewData = data;
          previewed = payload;
          revision = data.revision;
          result.replaceChildren(...Object.entries(data.targets).map(([id, p]) => targetCard(id, p)));
          run.disabled = !Object.values(data.targets).some((p) => p.ok);
        } catch (error) {
          if (mine !== generation) return;
          message.append(h('p', { class: 'banner error', role: 'alert' }, errorText(error)));
        }
      }

      async function doRun() {
        if (!previewed || running) return;
        const payload = previewed; // exactly what the user saw, never the current inputs
        busy(run, true);
        lock(true);
        message.replaceChildren();
        try {
          const data = await mutate('POST', 'api/transfer', payload, revision);
          await loadInverters();
          const skipped = Object.keys(data.skipped || {});
          toast(t('transfer.done', { n: data.applied.length, r: data.revision }) + (skipped.length ? ` ${t('transfer.some_skipped', { n: skipped.length })}` : ''), 'ok');
          close(true);
        } catch (error) {
          if (error instanceof ApiError && error.key === 'stale_revision') {
            message.append(h('div', { class: 'banner warn', role: 'alert' }, h('p', {}, t('transfer.stale')),
              h('button', { class: 'btn small', type: 'button', onClick: async () => { await loadInverters(); doPreview(); } }, t('transfer.refresh'))));
          } else message.append(h('p', { class: 'banner error', role: 'alert' }, errorText(error)));
          lock(false);
          busy(run, false);
        }
      }
      run.addEventListener('click', doRun);

      const featureId = uid('inc');
      body = h('div', { class: 'modal-body' },
        h('p', {}, t('transfer.intro')),
        h('p', { class: 'muted' }, t('transfer.not_copied')),
        h('fieldset', { class: 'feature' }, h('legend', {}, t('transfer.targets')),
          h('div', { class: 'options' }, targets.map((i) => h('label', { class: 'option' },
            h('input', { type: 'checkbox', checked: true, onChange: (e) => { if (e.target.checked) chosen.add(i.id); else chosen.delete(i.id); restale(); } }),
            h('span', { class: 'opt-text' }, h('span', { class: 'opt-title' }, i.name), h('span', { class: 'opt-help' }, i.id)))))),
        h('label', { class: 'option', for: featureId },
          h('input', { type: 'checkbox', id: featureId, checked: true, onChange: (e) => { includeFeatures = e.target.checked; restale(); } }),
          h('span', { class: 'opt-text' }, h('span', { class: 'opt-title' }, t('transfer.include_features')), h('span', { class: 'opt-help' }, t('transfer.include_features_help')))),
        message, result,
        h('div', { class: 'actions' }, cancelBtn, run));
      doPreview();
      return body;
    },
  });
}
