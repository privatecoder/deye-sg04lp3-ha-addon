// Guided setup of one inverter: step 1 plant features, step 2 sensors. The user edits a draft
// (overrides on top of the stored selection); the server previews and saves the whole draft in
// one atomic call, so the plant features and the deviations always change together.

import { ApiError, api } from './api.js';
import { t, errorText, localName, localText } from './i18n.js';
import { h, uid, modal, toast, debounce } from './ui.js';
import { state, canEdit, inverterById, profileOf, loadInverters, loadSession, mutate } from './state.js';
import { stateChip, reasonText, levelChip, busChip, loadProgress, budgetLevel, hintsOf, hintBody, sensorList } from './common.js';
import { openTransfer } from './transfer.js';

const BASIC_KEYS = ['serial_number', 'operating_state', 'fault_word_1', 'fault_word_2', 'fault_word_3', 'fault_word_4'];
const POINT_ORDER = ['grid_terminal', 'ups_load', 'inverter_output', 'battery', 'bms', 'pv', 'gen_port', 'parallel', 'tou', 'diagnostic'];
const DEFAULT_FEATURES = {
  utility_grid: true, grid_port_use: 'utility', battery: true, bms: false,
  gen_port_mode: 'unused', mppt1_used: true, mppt2_used: true, parallel: false,
};
const GRID_CHOICES = {
  utility: { utility_grid: true, grid_port_use: 'utility' },
  generator: { utility_grid: false, grid_port_use: 'generator' },
  island: { utility_grid: false, grid_port_use: 'unused' },
};

function newDraft() {
  return { fOver: {}, kOver: new Map(), confirm: new Set(), restoreAll: false, restoreSeq: 0, saving: false, baseStale: false, prefilled: new Set(), touched: false, prefillDone: false, search: '', filter: 'all', openGroups: new Map() };
}

export function setupView(invId) {
  const draft = state.drafts.get(invId) || newDraft();
  state.drafts.set(invId, draft);
  // A draft with edits keeps the stored state it was made on (kept across a language switch).
  const hasEdits = Object.keys(draft.fOver).length > 0 || draft.kOver.size > 0 || draft.restoreAll || draft.confirm.size > 0;
  let base = hasEdits && draft.base ? draft.base : inverterById(invId);
  let baseRevision = hasEdits && draft.baseRevision !== undefined ? draft.baseRevision : state.revision;
  draft.base = base;
  draft.baseRevision = baseRevision;
  const profile = profileOf(base);
  const entries = profile.entries;
  const root = h('div', { class: 'setup' });
  const rows = new Map();
  const groups = [];
  let preview = null; // { sel, adds, drops, warnings, conflicts, budget }
  let previewError = null;
  let seq = 0;
  let conflict = null; // { current }
  let failure = null;
  const refs = {};

  // -- draft model --------------------------------------------------------------------------

  const baseFeatures = () => base.selection.features;
  const draftFeatures = () => ({ ...baseFeatures(), ...draft.fOver });
  const baseIntent = (key) => {
    if (draft.restoreAll) return null;
    if (base.selection.added.includes(key)) return 'add';
    if (base.selection.removed.includes(key)) return 'remove';
    return null;
  };
  // While a save is in flight, or after a save whose stored state could not be loaded yet (the base
  // is then OLDER than the server), edits are kept as explicit operations: comparing them with the
  // old base could make a counter-edit look like "already saved".
  const explicit = () => draft.saving || draft.baseStale;
  const baseConfirmed = () => new Set(base.selection.confirmed || []);
  const dirty = () => Object.keys(draft.fOver).length > 0 || draft.kOver.size > 0 || draft.restoreAll || draft.confirm.size > 0;

  function editBody() {
    const body = {};
    if (Object.keys(draft.fOver).length) body.features = { ...draft.fOver };
    if (draft.restoreAll) body.restore_template = true;
    if (draft.confirm.size) body.confirm = [...draft.confirm];
    for (const op of ['add', 'remove', 'reset']) {
      const keys = [...draft.kOver].filter(([, v]) => v === op).map(([k]) => k);
      if (keys.length) body[op] = keys;
    }
    return body;
  }

  // The selection the rows show, computed locally so that a click answers at once: the stored
  // intents with the draft's operations applied on the template (the server's preview of the
  // draft once the features changed, otherwise the stored one). The server's preview confirms it.
  function currentTemplate() {
    if (!Object.keys(draft.fOver).length) return base.selection.template;
    return preview ? preview.sel.template : base.selection.template;
  }

  function shown() {
    if (!dirty()) return base.selection;
    const template = new Set(currentTemplate());
    const added = new Set(draft.restoreAll ? [] : base.selection.added);
    const removed = new Set(draft.restoreAll ? [] : base.selection.removed);
    for (const [key, op] of draft.kOver) {
      if (op === 'reset') { added.delete(key); removed.delete(key); }
      else if (op === 'add') { added.add(key); removed.delete(key); }
      else { removed.add(key); added.delete(key); }
    }
    const effective = new Set([...template, ...added]);
    for (const key of removed) effective.delete(key);
    return {
      template: [...template],
      effective: [...effective],
      deviations: { added: [...added].filter((k) => !template.has(k)), removed: [...removed].filter((k) => template.has(k)) },
      budget: preview ? preview.sel.budget : base.selection.budget,
    };
  }

  // While a save is in flight, later edits are kept as explicit operations (never dropped because
  // they equal the old base): they are settled against the new stored state afterwards.
  function setFeature(name, value) {
    draft.touched = true;
    draft.prefilled.delete(name);
    if (!explicit() && value === baseFeatures()[name]) delete draft.fOver[name];
    else draft.fOver[name] = value;
    // An answer is a decision, also when it equals the default: it is stored as confirmed.
    if (baseConfirmed().has(name) && !explicit()) draft.confirm.delete(name);
    else draft.confirm.add(name);
  }

  function applyKey(key, wanted) {
    draft.touched = true;
    const sel = shown();
    const inTemplate = sel.template.includes(key);
    const op = wanted === inTemplate ? 'reset' : wanted ? 'add' : 'remove';
    const stored = baseIntent(key);
    if (!explicit() && ((op === 'reset' && stored === null) || op === stored)) draft.kOver.delete(key);
    else draft.kOver.set(key, op);
  }

  function setKey(key, wanted) {
    applyKey(key, wanted);
    changed();
  }

  // Quick actions of step 2: ordinary deviations from the template, previewed and saved as usual.
  function quick(keep) {
    for (const key of rows.keys()) applyKey(key, keep.includes(key));
    changed();
  }

  function applySuggestions() {
    if (draft.touched || draft.prefillDone) return;
    const suggested = base.status.suggested_features || {};
    if (!Object.keys(suggested).length) return;
    draft.prefillDone = true;
    // Only features the user has not answered yet are pre-filled; an answered one (even equal to
    // the default) stays, the device setting is then just shown as a hint.
    const f = baseFeatures();
    const confirmed = baseConfirmed();
    for (const [name, value] of Object.entries(suggested)) {
      if (!(name in DEFAULT_FEATURES) || confirmed.has(name) || value === f[name]) continue;
      draft.fOver[name] = value;
      draft.prefilled.add(name);
      if (name === 'grid_port_use' && value === 'generator' && !confirmed.has('utility_grid')) draft.fOver.utility_grid = false;
    }
  }

  // -- preview ------------------------------------------------------------------------------

  const requestPreview = debounce(async () => {
    if (!dirty()) return;
    const mine = ++seq;
    try {
      const data = await api('POST', `api/inverters/${invId}/sensors/preview`, editBody());
      if (mine !== seq) return;
      const p = data.preview;
      preview = { sel: p.selection, adds: p.adds, drops: p.drops, warnings: p.capability_warnings, conflicts: p.capability_conflicts };
      previewError = null;
    } catch (error) {
      if (mine !== seq) return;
      previewError = error;
    }
    paint();
  }, 180);

  function changed() {
    seq += 1;
    if (!dirty()) {
      preview = null;
      previewError = null;
      requestPreview.cancel();
    } else requestPreview();
    paint();
  }

  // -- step 1: plant features ---------------------------------------------------------------

  function suggestion(names, current) {
    const suggested = base.status.suggested_features || {};
    const name = names.find((n) => n in suggested);
    if (!name) return null;
    const mark = h('div', { class: 'suggest' });
    const prefilled = names.some((n) => draft.prefilled.has(n));
    const matches = current(suggested[name]);
    if (prefilled && matches) {
      mark.append(h('span', { class: 'chip prefilled' }, t('setup.prefilled')));
    } else if (matches) {
      mark.append(h('span', { class: 'chip match' }, t('setup.matches_device')));
    } else {
      mark.append(h('span', { class: 'muted' }, t('setup.device_says', { value: t(`feat.value.${name}.${String(suggested[name])}`) })),
        h('button', { class: 'link', type: 'button', disabled: !canEdit(), onClick: () => {
          if (name === 'grid_port_use') {
            setFeature('grid_port_use', suggested[name]);
            if (suggested[name] === 'generator') setFeature('utility_grid', false);
          } else setFeature(name, suggested[name]);
          changed();
        } }, t('setup.take_over')));
    }
    return mark;
  }

  function radios({ name, legend, help, options, current, choose, visible, suggest }) {
    const group = uid('feat');
    const inputs = new Map();
    const field = h('fieldset', { class: 'feature', 'data-feature': name },
      h('legend', {}, legend),
      help ? h('p', { class: 'help', id: `${group}-help` }, help) : null,
      h('div', { class: 'options', role: 'radiogroup', 'aria-describedby': help ? `${group}-help` : null },
        options.map((o) => {
          const input = h('input', { type: 'radio', name: group, value: String(o.value), disabled: !canEdit(), onChange: () => { choose(o.value); changed(); } });
          inputs.set(String(o.value), input);
          return h('label', { class: 'option' }, input, h('span', { class: 'opt-text' }, h('span', { class: 'opt-title' }, o.label), o.help ? h('span', { class: 'opt-help' }, o.help) : null));
        })),
      h('div', { class: 'suggest-slot' }));
    const custom = h('p', { class: 'note custom-note', hidden: true }, t('feat.custom_note'));
    field.append(custom);
    return {
      el: field,
      sync() {
        const value = current();
        for (const [v, input] of inputs) input.checked = v === String(value);
        custom.hidden = value !== '__custom__';
        field.hidden = visible ? !visible() : false;
        const slot = field.querySelector('.suggest-slot');
        slot.replaceChildren(...(suggest ? [suggest()].filter(Boolean) : []));
      },
    };
  }

  function boolOptions(name) {
    return [
      { value: true, label: t(`feat.${name}.yes`), help: t(`feat.${name}.yes_help`) },
      { value: false, label: t(`feat.${name}.no`), help: t(`feat.${name}.no_help`) },
    ];
  }

  function buildFeatures() {
    const f = draftFeatures;
    const controls = [];
    const gridCurrent = () => {
      const g = f();
      const hit = Object.entries(GRID_CHOICES).find(([, v]) => v.utility_grid === g.utility_grid && v.grid_port_use === g.grid_port_use);
      return hit ? hit[0] : '__custom__';
    };
    const grid = radios({
      name: 'grid', legend: t('feat.grid.q'), help: t('feat.grid.help'),
      options: [
        { value: 'utility', label: t('feat.grid.utility'), help: t('feat.grid.utility_help') },
        { value: 'island', label: t('feat.grid.island'), help: t('feat.grid.island_help') },
        { value: 'generator', label: t('feat.grid.generator'), help: t('feat.grid.generator_help') },
      ],
      current: gridCurrent,
      choose: (v) => { setFeature('utility_grid', GRID_CHOICES[v].utility_grid); setFeature('grid_port_use', GRID_CHOICES[v].grid_port_use); },
      // "Public grid" is only the default of the device (R189 says "no generator"): it is not
      // worth a hint to someone who has deliberately chosen island operation.
      suggest: () => ((base.status.suggested_features || {}).grid_port_use === 'utility' && gridCurrent() === 'island'
        ? null
        : suggestion(['grid_port_use'], (v) => f().grid_port_use === v)),
    });
    const battery = radios({
      name: 'battery', legend: t('feat.battery.q'), help: t('feat.battery.help'), options: boolOptions('battery'),
      current: () => f().battery, choose: (v) => setFeature('battery', v),
      suggest: () => suggestion(['battery'], (v) => f().battery === v),
    });
    const bms = radios({
      name: 'bms', legend: t('feat.bms.q'), help: t('feat.bms.help'), options: boolOptions('bms'),
      current: () => f().bms, choose: (v) => setFeature('bms', v), visible: () => f().battery,
    });
    const gen = radios({
      name: 'gen_port_mode', legend: t('feat.gen.q'), help: t('feat.gen.help'),
      options: ['unused', 'generator', 'smart_load', 'ac_coupled'].map((v) => ({ value: v, label: t(`feat.gen.${v}`), help: t(`feat.gen.${v}_help`) })),
      current: () => f().gen_port_mode, choose: (v) => setFeature('gen_port_mode', v),
      suggest: () => suggestion(['gen_port_mode'], (v) => f().gen_port_mode === v),
    });
    const parallel = radios({
      name: 'parallel', legend: t('feat.parallel.q'), help: t('feat.parallel.help'), options: boolOptions('parallel'),
      current: () => f().parallel, choose: (v) => setFeature('parallel', v),
      suggest: () => suggestion(['parallel'], (v) => f().parallel === v),
    });
    const mppt = h('fieldset', { class: 'feature', 'data-feature': 'mppt' },
      h('legend', {}, t('feat.mppt.q')), h('p', { class: 'help' }, t('feat.mppt.help')),
      h('div', { class: 'options' }, [1, 2].map((n) => {
        const input = h('input', { type: 'checkbox', disabled: !canEdit(), 'data-mppt': n, onChange: (e) => { setFeature(`mppt${n}_used`, e.target.checked); changed(); } });
        return h('label', { class: 'option' }, input, h('span', { class: 'opt-text' }, h('span', { class: 'opt-title' }, t('feat.mppt.n', { n })), h('span', { class: 'opt-help' }, t('feat.mppt.n_help', { n }))));
      })));
    controls.push(grid, battery, bms, gen, { el: mppt, sync() { for (const n of [1, 2]) mppt.querySelector(`[data-mppt="${n}"]`).checked = f()[`mppt${n}_used`]; } }, parallel);
    refs.controls = controls;
    return h('section', { class: 'card step', 'aria-labelledby': 'step1-title' },
      h('h2', { id: 'step1-title' }, h('span', { class: 'step-no' }, '1'), t('setup.step1')),
      h('p', { class: 'muted' }, t('setup.step1_intro')),
      refs.prefillNote = h('p', { class: 'banner info', role: 'status', hidden: true }, t('setup.prefill_note')),
      controls.map((c) => c.el));
  }

  // -- step 2: sensors ------------------------------------------------------------------------

  function sensorRow(entry, expert) {
    const id = uid('sensor');
    const input = h('input', { type: 'checkbox', id, 'aria-describedby': `${id}-desc ${id}-state`, disabled: !canEdit(), onChange: (e) => setKey(entry.key, e.target.checked) });
    const state_ = h('span', { class: 'sensor-state', id: `${id}-state` });
    const reset = h('button', { class: 'link', type: 'button', hidden: true, 'aria-label': t('sensor.reset_for', { name: localName(entry) }), onClick: () => { setKey(entry.key, shown().template.includes(entry.key)); } }, t('sensor.reset'));
    const warn = h('span', { class: 'chip warn', hidden: true }, t('sensor.warn'));
    const note = h('span', { class: 'chip', hidden: true });
    const description = localText(entry.description);
    const row = h('div', { class: 'sensor', 'data-key': entry.key },
      h('label', { class: 'sensor-main', for: id },
        input,
        h('span', { class: 'sensor-text' },
          h('span', { class: 'sensor-name' }, localName(entry), entry.unit ? h('span', { class: 'unit' }, ` (${entry.unit})`) : null),
          h('span', { class: 'sensor-desc', id: `${id}-desc` }, description || ''),
          h('span', { class: 'sensor-meta' },
            entry.hint ? h('span', { class: `chip hint-${entry.hint}` }, t(`sensor.hint.${entry.hint}`)) : null,
            expert ? h('span', { class: 'chip' }, t(`point.${entry.point}`)) : null,
            warn, note))),
      h('span', { class: 'sensor-actions' }, state_, reset));
    const search = [entry.key, entry.name.de, entry.name.en, description, entry.unit || '', t(`point.${entry.point}`)].join(' ').toLowerCase();
    rows.set(entry.key, { entry, row, input, state: state_, reset, warn, note, search });
    return row;
  }

  function buildGroup(title, list, { expert = false, id }) {
    const body = h('div', { class: 'sensor-list' }, list.map((e) => sensorRow(e, expert)));
    const count = h('span', { class: 'count' });
    const details = h('details', { class: expert ? 'group expert' : 'group', 'data-group': id },
      h('summary', {}, h('span', { class: 'group-title' }, title), count),
      expert ? h('p', { class: 'banner warn' }, t('sensor.expert_warning')) : null,
      body);
    // A click on the summary is the user's decision; programmatic changes are not.
    details.querySelector('summary').addEventListener('click', () => { draft.openGroups.set(id, !details.open); });
    groups.push({ id, el: details, keys: list.map((e) => e.key), count, expert });
    return details;
  }

  function buildSensors() {
    const regular = entries.filter((e) => !e.raw_diagnostic);
    const raw = entries.filter((e) => e.raw_diagnostic);
    const byPoint = new Map();
    for (const e of regular) {
      if (!byPoint.has(e.point)) byPoint.set(e.point, []);
      byPoint.get(e.point).push(e);
    }
    const points = [...byPoint.keys()].sort((a, b) => POINT_ORDER.indexOf(a) - POINT_ORDER.indexOf(b));
    const search = h('input', { type: 'search', class: 'search', id: 'sensor-search', value: draft.search, placeholder: t('sensor.search_ph'), 'aria-label': t('sensor.search'), onInput: (e) => { draft.search = e.target.value; filterRows(); } });
    const filter = h('div', { class: 'segmented', role: 'group', 'aria-label': t('sensor.filter') },
      ['all', 'selected', 'deviations'].map((f) => h('button', { type: 'button', class: 'seg', 'data-filter': f, 'aria-pressed': String(draft.filter === f), onClick: () => { draft.filter = f; filterRows(); } }, t(`sensor.filter.${f}`))));
    refs.filter = filter;
    refs.empty = h('p', { class: 'muted empty-result', hidden: true, role: 'status' }, t('sensor.no_match'));
    refs.quickNone = h('button', { class: 'btn small', type: 'button', 'data-quick': 'none', disabled: !canEdit(), onClick: () => quick([]) }, t('sensor.quick.none'));
    refs.quickBasic = h('button', { class: 'btn small', type: 'button', 'data-quick': 'basic', disabled: !canEdit(), onClick: () => quick(BASIC_KEYS) }, t('sensor.quick.basic'));
    refs.restoreAll = h('button', { class: 'btn small', type: 'button', disabled: !canEdit(), onClick: () => { draft.touched = true; draft.restoreAll = true; draft.restoreSeq += 1; draft.kOver.clear(); changed(); } }, t('sensor.restore_all'));
    refs.changes = h('section', { class: 'changes', 'aria-live': 'polite' });
    refs.warnings = h('div', { class: 'warnings', 'aria-live': 'polite' });
    refs.hints = h('div', { class: 'hint-block' });
    return h('section', { class: 'card step', 'aria-labelledby': 'step2-title' },
      h('h2', { id: 'step2-title' }, h('span', { class: 'step-no' }, '2'), t('setup.step2')),
      h('p', { class: 'muted' }, t('setup.step2_intro')),
      refs.hints,
      refs.changes, refs.warnings,
      h('div', { class: 'toolbar' }, search, filter, refs.quickBasic, refs.quickNone, refs.restoreAll),
      refs.empty,
      points.map((p) => buildGroup(t(`point.${p}`), byPoint.get(p), { id: p })),
      raw.length ? buildGroup(t('sensor.expert_title'), raw, { expert: true, id: '__raw' }) : null);
  }

  // -- painting ------------------------------------------------------------------------------

  function filterRows() {
    const q = draft.search.trim().toLowerCase();
    const sel = shown();
    const eff = new Set(sel.effective);
    const dev = new Set([...sel.deviations.added, ...sel.deviations.removed]);
    let visible = 0;
    for (const [key, r] of rows) {
      const ok = (!q || r.search.includes(q))
        && (draft.filter === 'all' || (draft.filter === 'selected' && eff.has(key)) || (draft.filter === 'deviations' && dev.has(key)));
      r.row.hidden = !ok;
      visible += ok;
    }
    for (const g of groups) {
      const any = g.keys.some((k) => !rows.get(k).row.hidden);
      g.el.hidden = !any;
      if (q && any && g.expert) g.el.open = true;
    }
    for (const b of refs.filter.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.filter === draft.filter));
    refs.empty.hidden = visible > 0;
  }

  function paintRows() {
    const sel = shown();
    const eff = new Set(sel.effective);
    const tpl = new Set(sel.template);
    const added = new Set(sel.deviations.added);
    const removed = new Set(sel.deviations.removed);
    const warned = new Set(preview && dirty() ? preview.warnings : []);
    const unreported = ((inverterById(invId) || base).status || {}).unreported || {};
    for (const [key, r] of rows) {
      r.input.checked = eff.has(key);
      let mark = '';
      let kind = '';
      if (removed.has(key)) { mark = t('sensor.state.removed'); kind = 'removed'; }
      else if (added.has(key)) { mark = t('sensor.state.added'); kind = 'added'; }
      else if (tpl.has(key)) { mark = t('sensor.state.template'); kind = 'template'; }
      r.state.textContent = mark;
      r.state.className = `sensor-state ${kind}`;
      r.row.dataset.state = kind;
      r.reset.hidden = !(removed.has(key) || added.has(key));
      r.warn.hidden = !warned.has(key);
      const gone = unreported[key];
      r.row.classList.toggle('unreported', !!gone && gone.profile_value == null);
      r.note.hidden = !gone;
      r.note.textContent = gone ? t(gone.profile_value == null ? 'sensor.unreported' : 'sensor.unreported_profile', { reg: `R${gone.register}`, raw: `0x${Number(gone.raw).toString(16).toUpperCase().padStart(4, '0')}` }) : '';
    }
    for (const g of groups) {
      const on = g.keys.filter((k) => eff.has(k)).length;
      g.count.textContent = t('sensor.group_count', { n: on, m: g.keys.length });
      if (draft.openGroups.has(g.id)) g.el.open = draft.openGroups.get(g.id);
      else if (!g.expert) g.el.open = on > 0 || g.keys.some((k) => added.has(k) || removed.has(k));
    }
    filterRows();
  }

  function paintBar() {
    const sel = shown();
    const budget = sel.budget;
    const level = budgetLevel(budget);
    refs.budget.replaceChildren(
      levelChip(level, `${t('setup.query_load')}: ${t(`load.${level}`)}`),
      loadProgress(budget.utilisation, level),
      busChip(budget),
      h('span', { class: 'muted' }, t('setup.budget_detail', { s: sel.effective.length, b: budget.blocks, r: budget.registers, p: Math.round(budget.utilisation * 100) })));
    const pending = dirty();
    refs.save.disabled = draft.saving || !pending || !canEdit() || (previewError !== null && previewError.key !== 'network_error');
    refs.save.setAttribute('aria-busy', draft.saving ? 'true' : 'false');
    refs.discard.disabled = !pending || draft.saving;
    refs.dirtyNote.textContent = pending ? (preview ? t('setup.dirty_counts', { a: preview.adds.length, d: preview.drops.length }) : t('setup.dirty')) : t('setup.clean');
    refs.dirtyNote.className = pending ? 'dirty-note dirty' : 'dirty-note';
    if (budget.overloaded) refs.overload.hidden = false; else refs.overload.hidden = true;
  }

  function paintMessages() {
    refs.banner.replaceChildren();
    if (conflict) {
      refs.banner.append(h('div', { class: 'banner warn', role: 'alert' },
        h('strong', {}, t('setup.conflict_title')),
        h('p', {}, t('setup.conflict_text', { n: Object.keys(draft.fOver).length + draft.kOver.size })),
        h('div', { class: 'actions' },
          h('button', { class: 'btn primary', type: 'button', onClick: reloadKeepingDraft }, t('setup.conflict_reload')),
          h('button', { class: 'btn', type: 'button', onClick: discard }, t('setup.conflict_discard')))));
    } else if (state.revision !== baseRevision && !conflict) {
      refs.banner.append(h('div', { class: 'banner info', role: 'status' },
        h('span', {}, t('setup.stale_notice')),
        h('button', { class: 'btn small', type: 'button', onClick: reloadKeepingDraft }, t('setup.conflict_reload'))));
    }
    if (failure) refs.banner.append(h('p', { class: 'banner error', role: 'alert' }, errorText(failure)));
    if (previewError) refs.banner.append(h('p', { class: 'banner error', role: 'alert' }, errorText(previewError)));
    if (!canEdit()) refs.banner.append(h('p', { class: 'banner info' }, t('overview.readonly')));
    // changes + warnings
    refs.changes.replaceChildren();
    if (dirty() && preview) {
      refs.changes.append(h('div', { class: 'change-box' },
        h('strong', {}, t('setup.changes_title')),
        preview.adds.length ? h('p', {}, h('span', { class: 'plus' }, `+ ${t('setup.adds', { n: preview.adds.length })}: `), sensorList(base, preview.adds)) : null,
        preview.drops.length ? h('p', {}, h('span', { class: 'minus' }, `− ${t('setup.drops', { n: preview.drops.length })}: `), sensorList(base, preview.drops)) : null,
        !preview.adds.length && !preview.drops.length ? h('p', { class: 'muted' }, t('setup.no_sensor_change')) : null));
    }
    refs.warnings.replaceChildren();
    if (dirty() && preview && preview.warnings.length) {
      refs.warnings.append(h('p', { class: 'banner warn' }, t('setup.cap_warning', { names: sensorList(base, preview.warnings) })));
    }
    // observation hints with a one-click "deselect"
    refs.hints.replaceChildren();
    const observation = hintsOf(base).filter((x) => x.kind === 'observation' && x.key);
    const live = new Set(shown().effective);
    if (observation.length) {
      refs.hints.append(h('ul', { class: 'hints' }, observation.map((hint) => h('li', { class: 'hint observation' },
        h('span', {}, hint.text),
        live.has(hint.key) && canEdit() ? h('button', { class: 'btn small', type: 'button', onClick: () => setKey(hint.key, false) }, t('setup.deselect')) : null))));
    }
    const device = hintsOf(base).filter((x) => x.kind === 'energy' || x.kind === 'info');
    if (device.length) {
      refs.hints.append(h('ul', { class: 'hints' }, device.map((hint) => h('li', { class: `hint ${hint.kind}` }, hintBody(hint)))));
    }
    refs.prefillNote.hidden = draft.prefilled.size === 0;
    refs.awaiting.hidden = base.selection.chosen !== false;
    refs.takeTemplate.disabled = draft.saving || !canEdit() || sensorDeviations();
    refs.takeTemplateNote.hidden = !sensorDeviations();
  }

  function paintHeader() {
    const latest = inverterById(invId) || base;
    refs.status.replaceChildren(stateChip(latest.status), h('span', { class: 'muted' }, reasonText(latest.status)));
  }

  function paint() {
    for (const c of refs.controls) c.sync();
    paintRows();
    paintBar();
    paintMessages();
  }

  // -- actions ------------------------------------------------------------------------------

  // Settle the draft against the stored state after the server accepted `sent`: only exactly what
  // was sent is acknowledged; everything entered while the call was in flight stays pending.
  function settle(sent) {
    for (const [name, value] of Object.entries(sent.fOver)) if (draft.fOver[name] === value) delete draft.fOver[name];
    for (const [key, op] of sent.kOver) if (draft.kOver.get(key) === op) draft.kOver.delete(key);
    if (sent.restoreAll && draft.restoreSeq === sent.restoreSeq) draft.restoreAll = false;
    for (const name of sent.confirm) draft.confirm.delete(name);
    for (const name of sent.confirm) draft.prefilled.delete(name);
    for (const name of Object.keys(sent.fOver)) draft.prefilled.delete(name);
    normalise();
  }

  // Operations that equal the stored state drop out.
  function normalise() {
    for (const name of Object.keys(draft.fOver)) if (draft.fOver[name] === baseFeatures()[name]) delete draft.fOver[name];
    for (const [key, op] of [...draft.kOver]) {
      const stored = baseIntent(key);
      if ((op === 'reset' && stored === null) || op === stored) draft.kOver.delete(key);
    }
    for (const name of [...draft.confirm]) if (baseConfirmed().has(name)) draft.confirm.delete(name);
  }

  async function save(choose = false) {
    if (draft.saving) return;
    // Accepting the template also accepts the answers shown pre-filled from the device.
    if (choose) for (const name of draft.prefilled) if (name in draft.fOver) draft.confirm.add(name);
    const sent = { fOver: { ...draft.fOver }, kOver: new Map(draft.kOver), restoreAll: draft.restoreAll, restoreSeq: draft.restoreSeq, confirm: new Set(draft.confirm) };
    draft.saving = true;
    failure = null;
    paintBar();
    let result;
    try {
      result = await mutate('POST', `api/inverters/${invId}/sensors`, choose ? { ...editBody(), choose: true } : editBody(), baseRevision);
    } catch (error) {
      if (error instanceof ApiError && error.key === 'stale_revision') conflict = { current: error.params.current };
      else failure = error;
      draft.saving = false;
      paint();
      return;
    }
    // `saving` stays on until the confirmed stored state is loaded and the draft is settled
    // against it: edits made during the follow-up GET are kept as explicit operations, too.
    preview = null;
    conflict = null;
    baseRevision = result.revision;
    draft.baseRevision = baseRevision;
    draft.prefillDone = true;
    try {
      await loadInverters();
      base = inverterById(invId);
      draft.base = base;
      draft.baseStale = false;
      settle(sent);
      toast(t('setup.saved', { r: result.revision }), 'ok');
    } catch (error) {
      // saved, but the new stored state could not be loaded: "base not loaded yet". Nothing is
      // dropped from the draft and later edits stay explicit until a load succeeds.
      draft.baseStale = true;
      failure = error;
    }
    draft.saving = false;
    if (dirty()) { changed(); return; }
    paint();
  }

  function discard() {
    Object.assign(draft, newDraft(), { prefillDone: true, openGroups: draft.openGroups });
    preview = null;
    failure = null;
    conflict = null;
    reloadKeepingDraft();
  }

  async function reloadKeepingDraft() {
    try {
      await Promise.all([loadInverters(), loadSession()]);
      base = inverterById(invId) || base;
      baseRevision = state.revision;
      draft.base = base;
      draft.baseRevision = baseRevision;
      conflict = null;
      failure = null;
      draft.baseStale = false;
      // Operations equal to the new stored state drop out; the rest is re-previewed.
      normalise();
      changed();
    } catch (error) {
      failure = error;
      paint();
    }
  }

  async function transfer() {
    if (dirty()) {
      const ok = await modal({ title: t('setup.save_first_title'), build: (close) => h('div', { class: 'modal-body' },
        h('p', {}, t('setup.save_first_text')),
        h('div', { class: 'actions' }, h('button', { class: 'btn primary', type: 'button', onClick: () => close(true) }, t('common.ok')))) });
      return ok;
    }
    openTransfer(invId);
  }

  // The template as it stands is a choice, too: it ends the "awaiting selection" state. The answers
  // shown (also those pre-filled from the device) go along; only sensor deviations must be saved
  // with the regular button.
  const sensorDeviations = () => draft.kOver.size > 0 || draft.restoreAll;
  const takeTemplate = () => save(true);

  // -- assemble -------------------------------------------------------------------------------

  applySuggestions();
  refs.banner = h('div', { class: 'messages' });
  refs.status = h('div', { class: 'status-line' });
  refs.budget = h('div', { class: 'budget', 'aria-live': 'polite' });
  refs.dirtyNote = h('span', { class: 'dirty-note' });
  refs.overload = h('p', { class: 'banner warn', hidden: true, role: 'alert' }, t('setup.overloaded'));
  refs.takeTemplateNote = h('p', { class: 'muted small', hidden: true }, t('setup.take_template_dirty'));
  refs.takeTemplate = h('button', { class: 'btn', type: 'button', onClick: takeTemplate }, t('setup.take_template'));
  refs.awaiting = h('div', { class: 'banner warn awaiting', role: 'status', hidden: true },
    h('strong', {}, t('setup.awaiting_title')), h('p', {}, t('setup.awaiting_text')), refs.takeTemplate, refs.takeTemplateNote);
  refs.save = h('button', { class: 'btn primary', type: 'button', onClick: () => save() }, t('setup.save'));
  refs.discard = h('button', { class: 'btn', type: 'button', onClick: discard }, t('setup.discard'));
  const step1 = buildFeatures();
  const step2 = buildSensors();
  root.append(
    h('nav', { class: 'crumbs', 'aria-label': t('nav.breadcrumb') }, h('a', { href: '#/' }, `← ${t('nav.overview')}`)),
    h('header', { class: 'page-head' },
      h('div', {}, h('h1', {}, base.name, h('span', { class: 'muted id' }, ` ${base.id}`)), refs.status),
      state.inverters.length > 1 ? h('button', { class: 'btn', type: 'button', disabled: !canEdit(), onClick: transfer }, t('overview.transfer')) : null),
    refs.banner, refs.awaiting, step1, step2,
    h('div', { class: 'action-bar', role: 'region', 'aria-label': t('setup.actions') },
      h('div', { class: 'bar-info' }, refs.budget, refs.dirtyNote),
      h('div', { class: 'bar-actions' },
        refs.discard, refs.save)),
    refs.overload);
  paintHeader();
  paint();
  if (dirty()) requestPreview();

  return {
    el: root,
    onData() {
      // a view built while a save was in flight (language switch) catches up with the stored state
      if (draft.baseStale && !draft.saving) {
        // a later poll delivered the stored state: the draft is compared with it from now on
        base = inverterById(invId) || base;
        baseRevision = state.revision;
        draft.base = base;
        draft.baseRevision = baseRevision;
        draft.baseStale = false;
        normalise();
        changed();
      }
      if (!draft.saving && draft.base && draft.base !== base) { base = draft.base; baseRevision = draft.baseRevision; normalise(); }
      if (!dirty() && !draft.saving) { base = inverterById(invId) || base; baseRevision = state.revision; draft.base = base; draft.baseRevision = baseRevision; }
      paintHeader();
      if (!draft.touched) { applySuggestions(); if (dirty()) requestPreview(); }
      paint();
    },
    canLeave: () => !dirty(),
    discardDraft() { state.drafts.delete(invId); },
    destroy() { requestPreview.cancel(); },
  };
}
