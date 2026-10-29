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
