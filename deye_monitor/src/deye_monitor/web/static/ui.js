// Small DOM helpers: no HTML strings anywhere, text always goes through text nodes.

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (name === 'class') el.className = value;
    else if (name.startsWith('on') && typeof value === 'function') {
      el.addEventListener(name.slice(2).toLowerCase(), value);
    } else if (value === true) el.setAttribute(name, '');
    else el.setAttribute(name, String(value));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

let idCounter = 0;
export function uid(prefix) {
  idCounter += 1;
  return `${prefix}-${idCounter}`;
}

export function toast(message, kind = 'info') {
  const host = document.getElementById('toasts');
  if (!host) return;
  const el = h('div', { class: `toast ${kind}` }, message);
  host.append(el);
  setTimeout(() => el.remove(), kind === 'error' ? 9000 : 4500);
}

// Modal dialog on the native <dialog> element: the browser traps focus, closes on Escape and
// returns focus to the opener. ``build(close)`` returns the body; resolves with the close value.
export function modal({ title, build, wide = false, onClose }) {
  const titleId = uid('dlg');
  const dialog = h('dialog', { class: wide ? 'modal wide' : 'modal', 'aria-labelledby': titleId });
  let result;
  const close = (value) => {
    result = value;
    dialog.close();
  };
  const body = build(close, dialog);
  // Keep Tab inside the dialog (native modal dialogs would hand focus to the browser chrome).
  dialog.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab') return;
    const items = [...dialog.querySelectorAll('button, a[href], input, select, textarea, summary, [tabindex]:not([tabindex="-1"])')]
      .filter((el) => !el.disabled && !el.hidden && el.closest('[hidden]') === null && el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  dialog.append(
    h('div', { class: 'modal-head' },
      h('h2', { id: titleId }, title),
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': document.documentElement.dataset.closeLabel || 'Close', onClick: () => close(undefined) }, '×')),
    body,
  );
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => {
      dialog.remove();
      if (onClose) onClose(result);
      resolve(result);
    });
    document.body.append(dialog);
    dialog.showModal();
  });
}

export function busy(button, isBusy) {
  if (!button) return;
  button.disabled = isBusy;
  button.setAttribute('aria-busy', isBusy ? 'true' : 'false');
}

export function debounce(fn, ms) {
  let timer = null;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

export function safeStorage() {
  return {
    get(key) {
      try {
        return window.localStorage.getItem(key);
      } catch {
        return null;
      }
    },
    set(key, value) {
      try {
        window.localStorage.setItem(key, value);
      } catch {
        /* storage may be blocked: the page works without it */
      }
    },
  };
}

// -- (i) help popover ---------------------------------------------------------------------------
// A button toggles a small floating note. One note is open at a time; it closes on Escape (focus
// returns to the button), on a click or tap outside, when focus moves away, and when another
// opens. The note lives next to the button's dialog (or in the body), never inside a <label>.

let openHelp = null;
let helpObserver = null;

function closeHelp(restoreFocus = false) {
  if (!openHelp) return;
  const { button, pop } = openHelp;
  openHelp = null;
  if (helpObserver) helpObserver.disconnect();
  button.setAttribute('aria-expanded', 'false');
  button.removeAttribute('aria-describedby');
  pop.hidden = true;
  if (!button.isConnected) pop.remove();
  if (restoreFocus && button.isConnected) button.focus();
}

function placeHelp({ button, pop }) {
  const margin = 8;
  const vv = window.visualViewport;
  const viewTop = vv ? vv.offsetTop : 0;
  const viewHeight = vv ? vv.height : window.innerHeight;
  const viewWidth = vv ? vv.width : window.innerWidth;
  const box = button.getBoundingClientRect();
  pop.style.maxHeight = `${Math.max(48, viewHeight - 2 * margin)}px`;
  const width = pop.offsetWidth;
  const height = pop.offsetHeight;
  const left = Math.min(Math.max(margin, box.left), Math.max(margin, viewWidth - width - margin));
  const below = box.bottom + 6;
  const above = box.top - 6 - height;
  let top = below + height > viewTop + viewHeight - margin && above > viewTop + margin ? above : below;
  // never leave the visible area: the note scrolls inside itself when it is taller than the space
  top = Math.min(top, viewTop + viewHeight - margin - height);
  top = Math.max(viewTop + margin, top);
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
  if (pop.scrollHeight > pop.clientHeight) pop.setAttribute('tabindex', '0'); else pop.removeAttribute('tabindex');
}

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || !openHelp) return;
  event.preventDefault();
  event.stopPropagation();
  closeHelp(true);
}, true);
document.addEventListener('pointerdown', (event) => {
  if (openHelp && !openHelp.button.contains(event.target) && !openHelp.pop.contains(event.target)) closeHelp();
}, true);
document.addEventListener('focusin', (event) => {
  if (openHelp && !openHelp.button.contains(event.target) && !openHelp.pop.contains(event.target)) closeHelp();
});
window.addEventListener('resize', () => { if (openHelp) placeHelp(openHelp); });
window.addEventListener('scroll', (event) => { if (openHelp && !openHelp.pop.contains(event.target)) placeHelp(openHelp); }, true);
if (window.visualViewport) window.visualViewport.addEventListener('resize', () => { if (openHelp) placeHelp(openHelp); });

export function helpPopover({ label, text, meta }) {
  const id = uid('help');
  const button = h('button', { class: 'help-btn', type: 'button', 'aria-label': label, 'aria-expanded': 'false', 'aria-controls': id });
  const pop = h('div', { class: 'help-pop', id, role: 'note', hidden: true }, h('span', { class: 'help-text' }, text), meta ? h('span', { class: 'help-meta' }, meta) : null);
  button.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (openHelp && openHelp.button === button) {
      closeHelp(true);
      return;
    }
    closeHelp();
    (button.closest('dialog') || document.body).append(pop);
    pop.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    button.setAttribute('aria-describedby', id);
    openHelp = { button, pop };
    placeHelp(openHelp);
    // a repaint of the surrounding view removes the button: take the note away with it
    helpObserver = helpObserver || new MutationObserver(() => { if (openHelp && !openHelp.button.isConnected) closeHelp(); });
    helpObserver.observe(document.body, { childList: true, subtree: true });
  });
  return button;
}
