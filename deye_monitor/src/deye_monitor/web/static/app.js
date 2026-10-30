// Entry point: boot, router (hash based, works below the Ingress base path), header, polling.

import { ApiError } from './api.js';
import { t, errorText, localiseDocument } from './i18n.js';
import { h, modal } from './ui.js';
import { state, loadAll, loadInverters, setLanguage, setTheme, applyTheme, hasPlant } from './state.js';
import { overviewView } from './overview.js';
import { setupView } from './setup.js';
import { plantView } from './plant.js';
import { wizardView } from './wizard.js';

const POLL_MS = 10000;
const root = document.getElementById('app');
let current = null; // { el, onData, canLeave, destroy, route }
let route = '';
let connectionLost = false;

function parseRoute(hash) {
  const m = /^#\/inv\/([a-z0-9_]{1,32})$/.exec(hash);
  if (m) return { name: 'setup', id: m[1] };
  if (hash === '#/plant' && hasPlant()) return { name: 'plant' };
  if (hash === '#/start' && hasPlant()) return { name: 'wizard' };
  return { name: 'overview' };
}

function header() {
  const langButton = (lang) => h('button', { type: 'button', class: 'seg', lang, 'aria-pressed': String(state.lang === lang), 'aria-label': t(`lang.${lang}`), onClick: () => { setLanguage(lang); render(); } }, lang.toUpperCase());
  const themes = ['auto', 'light', 'dark'];
  const nextTheme = themes[(themes.indexOf(state.theme) + 1) % themes.length];
  return h('header', { class: 'app-header' },
    h('a', { class: 'brand', href: '#/' }, h('span', { class: 'brand-mark', 'aria-hidden': 'true' }), h('span', {}, 'Deye Monitor')),
    hasPlant()
      ? h('nav', { class: 'app-nav', 'aria-label': t('nav.main') },
          h('a', { href: '#/', 'aria-current': location.hash === '' || location.hash === '#/' ? 'page' : null }, t('nav.overview')),
          h('a', { href: '#/plant', id: 'nav-plant', 'aria-current': location.hash === '#/plant' ? 'page' : null }, t('nav.plant')))
      : null,
    h('div', { class: 'header-tools' },
      h('div', { class: 'segmented', role: 'group', 'aria-label': t('lang.label') }, langButton('de'), langButton('en')),
      h('button', { type: 'button', class: 'btn small', id: 'theme-toggle', 'aria-label': t('theme.switch', { next: t(`theme.${nextTheme}`) }), onClick: () => { setTheme(nextTheme); render(); } }, t(`theme.${state.theme}`))));
}

function mount(view) {
  if (current && current.destroy) current.destroy();
  current = view;
  const main = document.getElementById('view');
  main.replaceChildren(view.el);
}

function showRoute() {
  const next = parseRoute(location.hash);
  route = location.hash;
  if (next.name === 'setup' && !state.inverters.some((i) => i.id === next.id)) {
    mount({ el: h('section', { class: 'empty' }, h('h2', {}, t('route.unknown_title')), h('p', {}, t('route.unknown_text')), h('a', { class: 'btn', href: '#/' }, t('nav.overview'))) });
    return;
  }
  const views = { setup: () => setupView(next.id), plant: plantView, wizard: wizardView, overview: overviewView };
  const view = views[next.name]();
  mount(view);
  document.title = next.name === 'setup' ? `${state.inverters.find((i) => i.id === next.id).name} · Deye Monitor` : next.name === 'plant' ? `${t('nav.plant')} · Deye Monitor` : next.name === 'wizard' ? `${t('wiz.title')} · Deye Monitor` : 'Deye Monitor';
  window.scrollTo(0, 0);
  const heading = document.querySelector('#view h1');
  if (heading) { heading.setAttribute('tabindex', '-1'); heading.focus({ preventScroll: true }); }
}

function render() {
  localiseDocument();
  root.replaceChildren(
    h('a', { class: 'skip', href: '#view-anchor', onClick: (e) => { e.preventDefault(); document.getElementById('view').focus(); } }, t('nav.skip')),
    header(),
    h('div', { class: 'conn banner warn', id: 'conn', role: 'alert', hidden: !connectionLost }, t('conn.lost')),
    h('main', { id: 'view', tabindex: '-1' }));
  showRoute();
}

async function poll() {
  if (document.visibilityState !== 'visible' || !current) return;
  try {
    await loadInverters();
  } catch (error) {
    connectionLost = true;
    const conn = document.getElementById('conn');
    if (conn) conn.hidden = false;
    return;
  }
  if (connectionLost) { connectionLost = false; document.getElementById('conn').hidden = true; }
  // A rendering failure is not a lost connection: report it as what it is.
  try {
    if (current.onData) await current.onData();
  } catch (error) {
    showFailure(error);
  }
}

async function onHashChange() {
  if (location.hash === route) return;
  const target = location.hash;
  if (current && current.canLeave && !current.canLeave()) {
    history.replaceState(null, '', route || '#/');
    const leave = await modal({ title: t('leave.title'), build: (close) => h('div', { class: 'modal-body' },
      h('p', {}, t('leave.text')),
      h('div', { class: 'actions' },
        h('button', { class: 'btn', type: 'button', onClick: () => close(false) }, t('leave.stay')),
        h('button', { class: 'btn danger', type: 'button', onClick: () => close(true) }, t('leave.discard')))) });
    if (!leave) return;
    if (current.discardDraft) current.discardDraft();
    history.replaceState(null, '', target || '#/');
  }
  refreshNav();
  showRoute();
}

function refreshNav() {
  const old = document.querySelector('header.app-header');
  if (old) old.replaceWith(header());
}

function showFailure(error) {
  const forbidden = error instanceof ApiError && error.key === 'forbidden_peer';
  const text = forbidden ? t('boot.forbidden') : error instanceof ApiError ? errorText(error) : t('boot.render_failed');
  root.replaceChildren(h('section', { class: 'empty', role: 'alert' },
    h('h1', {}, t('boot.failed_title')),
    h('p', {}, text),
    h('button', { class: 'btn primary', type: 'button', onClick: () => location.reload() }, t('boot.retry'))));
}

async function boot() {
  applyTheme();
  setLanguage(state.lang);
  localiseDocument();
  root.replaceChildren(h('p', { class: 'loading', role: 'status' }, t('boot.loading')));
  try {
    await loadAll();
  } catch (error) {
    showFailure(error);
    return;
  }
  // a plant without connections and inverters starts with the setup assistant
  if (hasPlant() && state.session.setup_needed && state.plant && state.plant.endpoints.length === 0 && (location.hash === '' || location.hash === '#/')) {
    history.replaceState(null, '', '#/start');
  }
  try {
    render();
  } catch (error) {
    showFailure(error);
    return;
  }
  window.addEventListener('hashchange', onHashChange);
  window.addEventListener('beforeunload', (e) => { if (current && current.canLeave && !current.canLeave()) { e.preventDefault(); } });
  setInterval(poll, POLL_MS);
  document.addEventListener('visibilitychange', poll);
}

boot();
