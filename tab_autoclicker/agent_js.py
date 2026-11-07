"""JavaScript snippets injected into the page (in an isolated sandbox, so the
page cannot see or tamper with them)."""

# Shared helpers: an overlay pill that never intercepts the mouse.
_PILL = r"""
  const makePill = (text, css, bg) => {
    const ui = {host: null, pill: null, gone: false};
    const build = () => {
      if (ui.gone || ui.host || !document.documentElement) return;
      const host = document.createElement('tab-autoclicker-ui');
      host.style.cssText = 'all:initial;position:fixed;z-index:2147483647;pointer-events:none;' + css;
      const root = host.attachShadow({mode: 'closed'});
      const pill = document.createElement('div');
      pill.style.cssText = 'font:600 13px/1.3 system-ui,-apple-system,sans-serif;color:#fff;' +
        'background:' + (bg || 'rgba(20,20,28,.88)') + ';padding:7px 12px;border-radius:999px;' +
        'box-shadow:0 2px 12px rgba(0,0,0,.35);white-space:nowrap;pointer-events:none;';
      pill.textContent = text;
      root.appendChild(pill);
      document.documentElement.appendChild(host);
      ui.host = host; ui.pill = pill;
    };
    ui.setText = (t) => { text = t; if (ui.pill) ui.pill.textContent = t; };
    ui.remove = () => { ui.gone = true; if (ui.host) ui.host.remove(); ui.host = ui.pill = null; };
    try {
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', build, {once: true});
      else build();
    } catch (e) {}
    return ui;
  };
"""

# Pick mode: installed in every tab (and frame). The first real click in a tab
# selects it; that click is swallowed so it does not trigger anything on the page.
PICK = r"""(send) => {
  const G = globalThis;
  if (G.__tacPick) return 'already';
""" + _PILL + r"""
  const isTop = window.top === window;
  const types = ['pointerdown','pointerup','mousedown','mouseup','click','auxclick','dblclick','contextmenu'];
  let picked = false, ui = null;
  const remove = () => {
    for (const t of types) window.removeEventListener(t, swallow, true);
    if (ui) ui.remove();
    ui = null;
    delete G.__tacPick;
  };
  const swallow = (e) => {
    if (!e.isTrusted) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    if (!picked && (e.type === 'pointerdown' || e.type === 'mousedown')) {
      picked = true;
      if (ui) ui.setText('✔ This tab is now the Auto Clicker target');
      send({kind: 'pick', href: String(location.href)});
      setTimeout(remove, 1200);           // safety net
    } else if (picked && (e.type === 'click' || e.type === 'contextmenu' || e.type === 'auxclick')) {
      setTimeout(remove, 400);            // the gesture is over
    }
  };
  for (const t of types) window.addEventListener(t, swallow, true);
  if (isTop) {
    ui = makePill('🎯 Auto Clicker: click anywhere on this page to choose this tab',
                  'top:10px;left:50%;transform:translateX(-50%);', 'rgba(26,95,180,.93)');
  }
  G.__tacPick = { cancel: () => { if (!picked) remove(); } };
  return 'ok';
}"""

CANCEL_PICK = r"""() => { if (globalThis.__tacPick) globalThis.__tacPick.cancel(); return true; }"""

# Record mode: installed in the target tab and its frames. Reports each press
# (pointerdown) + release (pointerup) as one step, in frame-local CSS pixels.
RECORD = r"""(send) => {
  const G = globalThis;
  if (G.__tacRec) G.__tacRec.stop();
""" + _PILL + r"""
  const isTop = window.top === window;
  const cssPath = (el) => {
    try {
      if (!el || el.nodeType !== 1) return null;
      const parts = [];
      let node = el;
      while (node && node.nodeType === 1 && parts.length < 14) {
        if (node.id) {
          const idSel = '#' + CSS.escape(node.id);
          if (document.querySelectorAll(idSel).length === 1) { parts.unshift(idSel); break; }
        }
        let part = node.localName;
        const parent = node.parentElement;
        if (parent) {
          const same = Array.from(parent.children).filter(c => c.localName === node.localName);
          if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
        }
        parts.unshift(part);
        node = parent;
      }
      const sel = parts.join(' > ');
      return document.querySelector(sel) === el ? sel : null;
    } catch (e) { return null; }
  };
  const label = (el) => {
    try {
      if (!el || el.nodeType !== 1) return '';
      let s = el.localName + (el.id ? '#' + el.id : '');
      let txt = el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') ||
                el.getAttribute('placeholder') || '';
      if (!txt && el.childElementCount < 25) txt = el.innerText || el.value || '';
      txt = String(txt).replace(/\s+/g, ' ').trim();
      if (txt.length > 40) txt = txt.slice(0, 39) + '…';
      return txt ? s + ' "' + txt + '"' : s;
    } catch (e) { return ''; }
  };
  let down = null;
  const finish = (ux, uy, now) => {
    const d = down;
    down = null;
    send({kind: 'click', x: d.x, y: d.y, ux, uy, button: d.button, t: d.t,
          hold: Math.max(0, now - d.t), sx: d.sx, sy: d.sy, vw: d.vw, vh: d.vh,
          selector: d.selector, fx: d.fx, fy: d.fy, label: d.label, href: d.href, top: isTop});
  };
  const onDown = (e) => {
    if (!e.isTrusted || e.isPrimary === false) return;
    const now = Date.now();
    if (down) finish(down.x, down.y, down.t + 60);   // lost the release: keep the press
    const el = e.target;
    let fx = 0.5, fy = 0.5;
    try {
      const r = el.getBoundingClientRect();
      if (r.width > 0) fx = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
      if (r.height > 0) fy = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
    } catch (err) {}
    const path = e.composedPath ? e.composedPath() : [];
    down = {x: e.clientX, y: e.clientY, button: e.button, t: now,
            sx: window.scrollX, sy: window.scrollY, vw: window.innerWidth, vh: window.innerHeight,
            selector: cssPath(el), fx, fy, label: label(path[0] && path[0].nodeType === 1 ? path[0] : el),
            href: String(location.href)};
  };
  const onUp = (e) => {
    if (!e.isTrusted || !down || e.button !== down.button) return;
    finish(e.clientX, e.clientY, Date.now());
  };
  const onGone = () => { if (down) finish(down.x, down.y, Date.now()); };
  window.addEventListener('pointerdown', onDown, true);
  window.addEventListener('pointerup', onUp, true);
  window.addEventListener('pointercancel', onGone, true);
  window.addEventListener('pagehide', onGone, true);
  let ui = null;
  if (isTop) {
    ui = makePill('● REC  Auto Clicker is recording clicks in this tab',
                  'bottom:12px;right:12px;opacity:.9;', 'rgba(200,30,45,.9)');
    // Shrink to a small badge so it covers less of the page (games use every corner).
    setTimeout(() => { if (ui) ui.setText('● REC'); }, 4000);
  }
  G.__tacRec = { stop: () => {
    window.removeEventListener('pointerdown', onDown, true);
    window.removeEventListener('pointerup', onUp, true);
    window.removeEventListener('pointercancel', onGone, true);
    window.removeEventListener('pagehide', onGone, true);
    if (ui) ui.remove();
    delete G.__tacRec;
  }};
  return 'ok';
}"""

STOP_RECORD = r"""() => { if (globalThis.__tacRec) globalThis.__tacRec.stop(); return true; }"""

# In a parent document: the content-box origin of every child frame, so a
# frame-local click position can be converted to top-level page coordinates.
FRAME_OFFSETS = r"""() => Array.from(document.querySelectorAll('iframe, frame')).map((f) => {
  const r = f.getBoundingClientRect(), cs = getComputedStyle(f);
  return [f.contentWindow,
          r.left + parseFloat(cs.borderLeftWidth) + parseFloat(cs.paddingLeft),
          r.top + parseFloat(cs.borderTopWidth) + parseFloat(cs.paddingTop)];
})"""

# Hit-test one level: if (x, y) lands inside a child frame, return that frame's
# window plus the position inside it. Clicks for cross-origin frames must be
# dispatched in the frame's own browsing context.
FRAME_AT_POINT = r"""(x, y) => {
  const el = document.elementFromPoint(x, y);
  if (!el || (el.localName !== 'iframe' && el.localName !== 'frame') || !el.contentWindow) return null;
  const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
  const pl = parseFloat(cs.paddingLeft), pt = parseFloat(cs.paddingTop);
  const ox = r.left + parseFloat(cs.borderLeftWidth) + pl, oy = r.top + parseFloat(cs.borderTopWidth) + pt;
  const w = el.clientWidth - pl - parseFloat(cs.paddingRight), h = el.clientHeight - pt - parseFloat(cs.paddingBottom);
  const lx = x - ox, ly = y - oy;
  if (lx < 0 || ly < 0 || lx >= w || ly >= h) return null;
  return [el.contentWindow, lx, ly];
}"""

SCROLL_POS = r"""() => [window.scrollX, window.scrollY, window.innerWidth, window.innerHeight]"""

RESTORE_SCROLL = r"""(sx, sy) => {
  if (Math.abs(window.scrollX - sx) <= 1 && Math.abs(window.scrollY - sy) <= 1) return false;
  window.scrollTo({left: sx, top: sy, behavior: 'instant'});
  return true;
}"""

# Element aiming: find the recorded element again (it may have moved) and
# return the point at the same relative position inside it.
ELEMENT_POINT = r"""(sel, fx, fy) => {
  let el = null;
  try { el = document.querySelector(sel); } catch (e) { return null; }
  if (!el) return null;
  let r = el.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return null;
  if (r.bottom <= 0 || r.right <= 0 || r.top >= window.innerHeight || r.left >= window.innerWidth) {
    el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
    r = el.getBoundingClientRect();
  }
  const x = Math.min(Math.max(r.left + r.width * fx, 0), window.innerWidth - 1);
  const y = Math.min(Math.max(r.top + r.height * fy, 0), window.innerHeight - 1);
  return [x, y];
}"""

# A short-lived ring where a replayed click landed (purely visual).
MARKER = r"""(x, y, color, ms) => {
  const d = document.createElement('tab-autoclicker-ui');
  d.setAttribute('data-marker', '');
  d.style.cssText = 'all:initial;position:fixed;z-index:2147483647;pointer-events:none;box-sizing:border-box;' +
    'left:' + (x - 13) + 'px;top:' + (y - 13) + 'px;width:26px;height:26px;border-radius:50%;' +
    'border:3px solid ' + color + ';background:rgba(255,255,255,.25);' +
    'transition:transform .45s ease-out,opacity .45s ease-out;';
  if (!document.documentElement) return false;
  document.documentElement.appendChild(d);
  const hold = Math.max(30, (ms || 600) - 570);
  setTimeout(() => { d.style.transform = 'scale(1.9)'; d.style.opacity = '0'; }, hold);
  setTimeout(() => d.remove(), hold + 570);
  return true;
}"""

TITLE = r"""() => [String(document.title || ''), String(location.href)]"""

# Before a screenshot: hide (or show again) everything Auto Clicker draws in the
# page, and drop click markers, so they do not end up in image checks.
OVERLAYS = r"""(visible) => {
  for (const el of document.querySelectorAll('tab-autoclicker-ui')) {
    if (el.hasAttribute('data-marker')) el.remove();
    else el.style.visibility = visible ? '' : 'hidden';
  }
  return [window.devicePixelRatio || 1, window.innerWidth, window.innerHeight];
}"""

VISIBILITY = r"""() => document.visibilityState"""

# Game mode: games such as Unity WebGL ones ignore input once their window
# loses focus (they listen for the window "blur" event). While this is on, that
# event is kept from the page and the game is told it has focus again.
# Game mode, when clicks stopped registering: let the game see it lose focus and
# get it back (as stopping and starting playback does), which resets its input.
# Call with "blur", wait a few frames, then call with "focus".
FOCUS_CYCLE = r"""(phase) => {
  const G = globalThis, st = G.__tacFocus;
  if (phase === 'blur') {
    const was = st ? st.on : false;
    if (st) st.on = false;
    window.dispatchEvent(new FocusEvent('blur'));
    if (st) st.on = was;
    return null;
  }
  window.dispatchEvent(new FocusEvent('focus'));
  return {game: !!document.querySelector('canvas'), focus: document.hasFocus(),
          visible: document.visibilityState, blocked: st ? (st.blocked || 0) : 0};
}"""

KEEP_FOCUS = r"""(on, force) => {
  const G = globalThis;
  if (!G.__tacFocus) {
    G.__tacFocus = {on: false};
    window.addEventListener('blur', (e) => {
      if (G.__tacFocus && G.__tacFocus.on && e.target === window) {
        e.stopImmediatePropagation();
        G.__tacFocus.blocked = (G.__tacFocus.blocked || 0) + 1;
      }
    }, true);
  }
  G.__tacFocus.on = !!on;
  if (on && (force || !document.hasFocus())) window.dispatchEvent(new FocusEvent('focus'));
  return true;
}"""
