// Application state: server data, language, theme and the per-inverter drafts.

import { ApiError, api, setCsrf } from './api.js';
import { safeStorage } from './ui.js';

const storage = safeStorage();

export const state = {
  session: null,
  catalog: null,
  plant: null,
  inverters: [],
  revision: 0,
  actionCounter: 0,
  serverNow: null,
  fetchedAt: 0,
  lang: initialLanguage(),
  theme: storage.get('deye.theme') || 'auto',
  drafts: new Map(),
};

function initialLanguage() {
  const saved = storage.get('deye.lang');
  if (saved === 'de' || saved === 'en') return saved;
  const browser = (navigator.language || 'en').toLowerCase();
  return browser.startsWith('de') ? 'de' : 'en';
}

export function setLanguage(lang) {
  state.lang = lang;
  storage.set('deye.lang', lang);
  document.documentElement.lang = lang;
}

export function setTheme(theme) {
  state.theme = theme;
  storage.set('deye.theme', theme);
  applyTheme();
}

export function applyTheme() {
  const root = document.documentElement;
  if (state.theme === 'light' || state.theme === 'dark') root.dataset.theme = state.theme;
  else delete root.dataset.theme;
}

// no change while the plant is in recovery (the app has to be restarted first)
export const canEdit = () => Boolean(state.session && state.session.csrf && !state.session.recovery_failed);

export async function loadSession() {
  const session = await api('GET', 'api/session');
  state.session = session;
  state.revision = session.revision;
  state.actionCounter = session.action_counter;
  setCsrf(session.csrf);
  return session;
}

export async function loadCatalog() {
  const data = await api('GET', 'api/catalog');
  const profiles = {};
  for (const [name, profile] of Object.entries(data.profiles)) {
    profiles[name] = { ...profile, byKey: new Map(profile.entries.map((e) => [e.key, e])) };
  }
  state.catalog = profiles;
}

export async function loadInverters() {
  const data = await api('GET', 'api/inverters');
  state.inverters = data.inverters;
  state.revision = data.revision;
  state.plantRevision = data.plant_revision;
  state.serverNow = data.now;
  state.fetchedAt = Date.now();
  return data;
}

export async function loadPlant() {
  state.plant = await api('GET', 'api/plant');
  return state.plant;
}

export async function loadAll() {
  await Promise.all([loadSession(), loadCatalog()]);
  await loadInverters();
  if (hasPlant()) await loadPlant();
}

// The plant (connections and inverters) can be edited in the web interface.
export const hasPlant = () => Boolean(state.session && state.session.plant_revision !== null && state.session.plant_revision !== undefined);

// After a plant change: everything the views show depends on it.
export async function reloadAfterPlantChange() {
  await loadAll();
  for (const id of [...state.drafts.keys()]) {
    if (!state.inverters.some((i) => i.id === id)) state.drafts.delete(id);
  }
}

export const inverterById = (id) => state.inverters.find((i) => i.id === id) || null;
export const profileOf = (inv) => (state.catalog ? state.catalog[inv.profile] : null);

// Server time "now", advanced by the local clock since the last fetch (no skew with the browser).
export function serverNowEstimate() {
  if (state.serverNow === null || state.serverNow === undefined) return null;
  return state.serverNow + (Date.now() - state.fetchedAt) / 1000;
}

// -- load level (traffic light) -----------------------------------------------------------------

export function loadLevel(utilisation, overloaded = false) {
  if (overloaded) return 'over';
  if (utilisation === null || utilisation === undefined) return 'unknown';
  if (utilisation > 1) return 'over';
  if (utilisation >= 0.7) return 'high';
  if (utilisation >= 0.4) return 'mid';
  return 'low';
}

// -- changing calls -------------------------------------------------------------------------------


// A changing call with an explicit If-Match revision. A rejected CSRF token (the app restarted) is
// renewed once; the revision of the caller is kept so that a stale draft is still detected.
export async function mutate(method, path, body, revision) {
  const run = () => api(method, path, body, { revision });
  try {
    return await run();
  } catch (error) {
    if (error instanceof ApiError && error.key === 'csrf_invalid') {
      const known = state.revision;
      await loadSession();
      state.revision = known;
      return run();
    }
    throw error;
  }
}

// Identity, energy and cleanup actions are guarded by their own monotone counter (not the selection
// revision of a draft). The token is read once BEFORE the confirmation dialog opens and is then fixed
// for that dialog: the call sends exactly this token (a renewed CSRF token never changes it), so a
// state that moved on meanwhile is answered with 409 and has to be confirmed again.
export async function actionToken() {
  await loadSession();
  return state.actionCounter;
}

export const actWith = (token, path, body) => mutate('POST', path, body, token);
