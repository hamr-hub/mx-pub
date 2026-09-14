"""Native-dialog guard.

Three Playwright CDP clients can share one Chrome; if a tab fires a native
alert/confirm (e.g. beforeunload "leave this page?" while navigating off a
filled form), every client tries to handle the same dialog and the loser dies
with "Protocol error (Page.handleJavaScriptDialog): No dialog is showing",
killing its driver process mid-publish.

We prevent dialogs at the source (verified against Douyin creator 2026-09: its
security/tea SDKs call addEventListener('beforeunload', ...) ~50 times per load
AND transiently assign window.onbeforeunload around network moments):
  1. block addEventListener for beforeunload/unload (every frame, document_start)
  2. trap Object.defineProperty(window, 'onbeforeunload', ...)
  3. install a null accessor so plain assignment cannot create an own property
  4. periodic sweeper deletes any own prop that slipped through (defence in
     depth — a property installed by an unwrapped native from another realm)
add_init_script covers future navigations and every frame incl. cross-origin;
the evaluate covers the already-loaded top document.
"""

# 2026-09 /goal: a beforeunload panel still appeared with v1 of the guard (the
# platform SDK assigns window.onbeforeunload directly, bypassing the
# addEventListener patch). Two clients raced its accept and the loser's node
# driver died on an uncatchable promise rejection; the python thread then
# burned 420s/upload wait loops on the dead connection. Layers 2-4 below close
# the assignment path; fatal-connection handling lives in core/watch.py.
DIALOG_SUPPRESS_JS = r"""
(() => {
  if (window.__mxDialogGuard) return;
  window.__mxDialogGuard = true;
  try {
    window.alert = () => undefined;
    window.confirm = () => true;
    window.prompt = () => null;
  } catch (e) {}
  const BLOCK_EVENTS = new Set(['beforeunload', 'unload']);
  try {
    const origAdd = EventTarget.prototype.addEventListener;
    const blockedAdd = function (type, listener, opts) {
      if (BLOCK_EVENTS.has(type)) return;
      return origAdd.call(this, type, listener, opts);
    };
    EventTarget.prototype.addEventListener = blockedAdd;
    // Some SDKs cache window.addEventListener before patching the prototype
    // chain; shadow the own slot as well.
    try { window.addEventListener = blockedAdd; } catch (e) {}

    const origDef = Object.defineProperty;
    Object.defineProperty = function (target, prop, desc) {
      if (target === window && (prop === 'onbeforeunload' || prop === 'onunload')) {
        return target;
      }
      return origDef.call(this, target, prop, desc);
    };

    const noopAccessor = {
      configurable: true,
      get: () => null,
      set: () => {},
    };
    const proto = Object.getPrototypeOf(window);
    origDef.call(Object, proto, 'onbeforeunload', noopAccessor);
    origDef.call(Object, proto, 'onunload', noopAccessor);
    // Same for the current window itself (covers already-created documents
    // where the SDK may hold an own data property from an earlier assignment).
    origDef.call(Object, window, 'onbeforeunload', noopAccessor);
    origDef.call(Object, window, 'onunload', noopAccessor);

    // Sweeper: delete anything exotic that re-installs an own data property.
    const sweep = () => {
      try {
        if (Object.getOwnPropertyDescriptor(window, 'onbeforeunload')) {
          delete window.onbeforeunload;
        }
        if (Object.getOwnPropertyDescriptor(window, 'onunload')) {
          delete window.onunload;
        }
      } catch (e) {}
    };
    setInterval(sweep, 1000);
  } catch (e) {}
})();
"""


def attach_dialog_guard(page) -> None:
    """Idempotently suppress native JS dialogs on a page (incl. future loads)."""
    try:
        if not getattr(page, "_mx_dialog_guard", False):
            page.add_init_script(DIALOG_SUPPRESS_JS)

            def _safe_dismiss(dialog):
                # Last-resort handler: suppression failed and a panel opened.
                # Accept immediately so navigation is not blocked; the
                # multi-client accept race is exactly why JS suppression is
                # the primary mechanism.
                try:
                    print(f"[dialog-guard] auto-accept: "
                          f"{dialog.type}: {dialog.message[:80]}", flush=True)
                except Exception:
                    pass
                try:
                    dialog.accept()
                except Exception:
                    pass

            page.on("dialog", _safe_dismiss)
            page._mx_dialog_guard = True
        page.evaluate(DIALOG_SUPPRESS_JS)
    except Exception:
        pass


def clear_contenteditable(page, locator) -> None:
    """Select-all + delete inside a contenteditable (React-safe clear)."""
    locator.click(timeout=8000)
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.press("Delete")
    page.keyboard.press("Backspace")
