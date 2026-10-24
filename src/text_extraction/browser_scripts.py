"""The JavaScript the renderers run in a page, and the consent rules.

Consent (B11) uses DuckDuckGo's autoconsent (``duckduckgo-autoconsent-py``, MPL-2.0, used
unmodified): its bundle is evaluated in the page's main world, so the rules' own snippets run
there and no ``eval`` messages need an answer (``isMainWorld``). The bundle talks to its host
through ``window.autoconsentSendMessage``; ``CONSENT_RELAY`` is that host inside the page: it
answers ``init`` with the configuration and the rules, and hands every message to the waiters
``CONSENT_WAIT`` registers. Nothing goes back to Python but the outcome.
"""

import json
from functools import cache
from typing import Any, Final

from duckduckgo_autoconsent import bundle, rules_json

# Banners autoconsent's rule set does not know, in its declarative rule format.
CUSTOM_CONSENT_RULES: Final[list[dict[str, Any]]] = [
    {
        # German federal sites built with the Government Site Builder use their own banner.
        # "Confirm selection" keeps the optional (statistics) toggle off: an opt-out; "Accept
        # all" is the opt-in.
        "name": "GovernmentSiteBuilder",
        "detectCmp": [{"exists": "#cookiebanner"}],
        "detectPopup": [{"visible": "#cookiebanner"}],
        "optOut": [{"waitForThenClick": ".js-banner-button-confirm-selection"}],
        # The accept button's markup is unverified against a live Government Site Builder
        # site, so the selector list is tolerant.
        "optIn": [
            {
                "waitForThenClick": ".js-banner-button-accept-all, .js-banner-button-accept, "
                '#cookiebanner button[name*="accept" i]'
            }
        ],
    },
]

# autoconsent's configuration (``normalizeConfig``); ``autoAction`` is set per run. One retry,
# 500 ms after the first look: the render runs it after the load and the network-idle wait. A
# second retry would wait for a DOM mutation as well, which a quiet page never makes, so every
# page without a banner would use up the whole 3-second budget.
_CONSENT_CONFIG: Final[dict[str, Any]] = {
    "enabled": True,
    "disabledCmps": [],
    "enablePrehide": False,
    "enableCosmeticRules": True,
    "enableGeneratedRules": True,
    "enableHeuristicDetection": False,
    "detectRetries": 1,
    "isMainWorld": True,
    "logs": {
        "lifecycle": False,
        "rulesteps": False,
        "detectionsteps": False,
        "evals": False,
        "errors": False,
        "messages": False,
        "waits": False,
    },
}


@cache
def consent_bundle() -> str:
    """autoconsent's engine (evaluated as is)."""
    return str(bundle())


@cache
def consent_rules_json() -> str:
    """autoconsent's rules plus ``CUSTOM_CONSENT_RULES``, as one JSON array."""
    return json.dumps(json.loads(rules_json()) + CUSTOM_CONSENT_RULES)


def consent_config(opt: str) -> dict[str, Any]:
    return {**_CONSENT_CONFIG, "autoAction": opt}


# Installs the host side of autoconsent once per document; False when it is there already.
CONSENT_RELAY: Final = """({ rulesJson, config }) => {
  if (window.__textExtractionConsent) return false;
  const state = { listeners: new Set(), lifecycle: null, results: {} };
  window.__textExtractionConsent = state;
  window.autoconsentSendMessage = (message) => {
    if (message.type === 'init') {
      // The engine sends `init` before it has installed its receiver.
      setTimeout(() => window.autoconsentReceiveMessage({
        type: 'initResp', config, rules: { autoconsent: JSON.parse(rulesJson) },
      }), 0);
    } else if (message.type === 'report' && message.state) {
      state.lifecycle = message.state.lifecycle;
    } else if (message.type === 'optOutResult' || message.type === 'optInResult') {
      state.results[message.type.slice(0, -'Result'.length)] = Boolean(message.result);
    }
    for (const listener of state.listeners) listener(message);
    return Promise.resolve();
  };
  return true;
}"""

# Waits for the outcome of an opt-out or opt-in and answers one of: 'done' (the banner was
# handled), 'failed' (it was found but could not be handled), 'nothingDetected', 'timeout' or
# 'none' (no engine in this document). The engine reports twice, as a result message and as
# lifecycle states, and in either order: both are read the same way, and ``done`` only counts
# when the result of this very action was a success. ``again``: the engine runs already (an
# earlier run on this document), so it is asked to act now.
CONSENT_WAIT: Final = """({ opt, timeoutMs, again }) => new Promise((resolve) => {
  const state = window.__textExtractionConsent;
  if (!state || typeof window.autoconsentReceiveMessage !== 'function') {
    resolve('none');
    return;
  }
  const verdict = (lifecycle) => {
    if (lifecycle === 'nothingDetected') return 'nothingDetected';
    if (lifecycle === opt + 'Succeeded') return 'done';
    if (lifecycle === opt + 'Failed') return 'failed';
    if (lifecycle === 'done' && state.results[opt] !== undefined)
      return state.results[opt] ? 'done' : 'failed';
    return null;
  };
  let timer = null;
  const listener = (message) => {
    if (message.type === opt + 'Result') finish(message.result ? 'done' : 'failed');
    else if (message.type === 'report' && message.state) {
      const outcome = verdict(message.state.lifecycle);
      if (outcome) finish(outcome);
    }
  };
  const finish = (outcome) => {
    clearTimeout(timer);
    state.listeners.delete(listener);
    resolve(outcome);
  };
  timer = setTimeout(() => finish('timeout'), timeoutMs);
  state.listeners.add(listener);
  if (again) {
    // The engine has handled the banner already, or found none: a second run has nothing to
    // do (it would answer with a failure, as it has no banner to work on).
    if (state.lifecycle === 'done' || state.lifecycle === 'nothingDetected') {
      finish('nothingDetected');
      return;
    }
    delete state.results[opt];
    window.autoconsentReceiveMessage({ type: opt });
  } else {
    const outcome = verdict(state.lifecycle);
    if (outcome) finish(outcome);
  }
})"""

# An init script (it runs in every document before the page's own scripts): removes each
# speculation rules script as soon as it is in the document, before Chromium acts on it. A
# prefetched or prerendered document is fetched outside the page's requests, and a navigation
# to it would use it without passing the route handler and the navigation check (probed with
# Chromium 153; no launch flag turned this off). Rules from a ``Speculation-Rules`` header
# are refused in the route handler instead.
NO_SPECULATION: Final = """(() => {
  const drop = (node) => {
    if (node.nodeName === 'SCRIPT'
        && (node.getAttribute('type') || '').trim().toLowerCase() === 'speculationrules') {
      node.remove();
    }
  };
  new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        drop(node);
        if (node.querySelectorAll) node.querySelectorAll('script').forEach(drop);
      }
    }
  }).observe(document, { childList: true, subtree: true });
})();"""

# Removes what a reader cannot see, while the page can still compute styles: the static HTML
# keeps inactive tabs and panels that a converter cannot tell from visible text.
# - ``display: none`` hides a whole subtree (no descendant can show itself again);
# - ``visibility: hidden`` is inherited but a descendant can set it back to visible, so only
#   leaves go (a fully hidden subtree goes leaf by leaf, leaving empty wrappers);
# - elements that are never rendered (scripts, the styles that do the hiding, ...) stay.
STRIP_HIDDEN: Final = """() => {
  if (!document.body) return 0;
  const keep = new Set(['SCRIPT', 'STYLE', 'LINK', 'META', 'TEMPLATE', 'NOSCRIPT', 'TITLE',
    'HEAD']);
  let removed = 0;
  for (const element of Array.from(document.body.querySelectorAll('*'))) {
    if (!element.isConnected || keep.has(element.tagName)) continue;
    const style = getComputedStyle(element);
    if (style.display === 'none'
        || (style.visibility === 'hidden' && element.children.length === 0)) {
      element.remove();
      removed += 1;
    }
  }
  return removed;
}"""

# The height of the whole document in CSS pixels, as Playwright measures a full-page capture.
PAGE_HEIGHT: Final = """() => {
  const b = document.body, d = document.documentElement;
  if (!b || !d) return 0;
  return Math.max(b.scrollHeight, d.scrollHeight, b.offsetHeight, d.offsetHeight,
    b.clientHeight, d.clientHeight);
}"""

# Scrolls down by one viewport height.
SCROLL_VIEWPORT: Final = "() => { window.scrollBy(0, window.innerHeight); }"

# Scrolls to the bottom of the document.
SCROLL_TO_END: Final = """() => {
  const d = document.documentElement;
  window.scrollTo(0, Math.max(document.body ? document.body.scrollHeight : 0, d.scrollHeight));
}"""

# Opens closed ``<details>`` and clicks up to ``limit`` visible elements with
# ``aria-expanded="false"`` (those that are still closed when their turn comes: a click can
# close another section). It leaves out what would take the reader to another page: links
# (other than fragment-only and ``javascript:`` ones) and buttons that submit a form. Answers
# how many it opened or clicked.
EXPAND_ALL: Final = """(limit) => {
  let opened = 0;
  for (const details of document.querySelectorAll('details:not([open])')) {
    details.open = true;
    opened += 1;
  }
  const visible = (element) => {
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && box.width > 0 && box.height > 0;
  };
  const navigates = (element) => {
    if (element.matches('a[href], area[href]')) {
      const href = (element.getAttribute('href') || '').trim().toLowerCase();
      return !(href.startsWith('#') || href.startsWith('javascript:'));
    }
    if (element.tagName === 'INPUT') return ['submit', 'image'].includes(element.type);
    return element.tagName === 'BUTTON' && element.type === 'submit' && element.form !== null;
  };
  const toggles = Array.from(document.querySelectorAll('[aria-expanded="false"]'))
    .filter((element) => visible(element) && !navigates(element));
  for (const toggle of toggles.slice(0, limit)) {
    if (toggle.getAttribute('aria-expanded') !== 'false' || toggle.disabled) continue;
    toggle.click();
    opened += 1;
  }
  return opened;
}"""

# Marks the current document, and tells whether it is still the marked one (a navigation
# replaces the window).
MARK_DOCUMENT: Final = "() => { window.__textExtractionMarked = true; }"
DOCUMENT_MARKED: Final = "() => window.__textExtractionMarked === true"
