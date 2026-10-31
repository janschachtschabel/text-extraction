/*
 * The UI's own script (rulings P10-S13, P10-S20, R-P10-F3 and P12-S6), the only one besides
 * htmx: the keys, the language, the tabs, the site tree's selection and the status line of an
 * answer htmx does not swap.
 *
 * Keys: the API key and the optional LLM and ASR keys live in this tab's sessionStorage
 * (te.apiKey, te.llmKey, te.asrKey). They are sent only as request headers to /ui/run/, never
 * as a cookie, a form field or part of a URL.
 *
 * Language: the page's language is <html lang>; it goes as X-UI-Language with each request to
 * /ui/run/. The language switch's choice is kept in localStorage (te.lang), never in a cookie;
 * a page opened without a lang query moves to the stored language. The script's own texts come
 * from the page's data-text-* attributes.
 */
(function () {
  "use strict";

  var KEYS = { api: "te.apiKey", llm: "te.llmKey", asr: "te.asrKey" };
  var LANG_KEY = "te.lang";
  var RUN_PREFIX = "/ui/run/";

  function readKey(name) {
    try {
      return window.sessionStorage.getItem(name) || "";
    } catch (error) {
      return "";
    }
  }

  function writeKey(name, value) {
    try {
      if (value) {
        window.sessionStorage.setItem(name, value);
      } else {
        window.sessionStorage.removeItem(name);
      }
    } catch (error) {
      /* storage off: the key lasts until the page is left */
    }
  }

  function readLanguage() {
    try {
      return window.localStorage.getItem(LANG_KEY) || "";
    } catch (error) {
      return "";
    }
  }

  function writeLanguage(value) {
    try {
      window.localStorage.setItem(LANG_KEY, value);
    } catch (error) {
      /* storage off: the choice lasts as long as the page's lang query */
    }
  }

  // The link of the language switch for "language", if the page has one.
  function switchLink(language) {
    var links = document.querySelectorAll(".language-switch a[hreflang]");
    for (var index = 0; index < links.length; index += 1) {
      if (links[index].getAttribute("hreflang") === language) {
        return links[index];
      }
    }
    return null;
  }

  // A page opened without a lang query moves to the stored language (location.replace, so the
  // first page leaves no history entry). Returns whether it moves.
  function moveToStoredLanguage() {
    if (new URLSearchParams(window.location.search).has("lang")) {
      return false;
    }
    var stored = readLanguage();
    var link = stored ? switchLink(stored) : null;
    if (!link || stored === document.documentElement.lang) {
      return false;
    }
    window.location.replace(link.href);
    return true;
  }

  // One of the script's texts, from the page's data-text-<name> attribute, with each {key}
  // replaced by values[key].
  function text(name, values) {
    var template = document.body.getAttribute("data-text-" + name) || "";
    return template.replace(/\{([a-z_][a-z0-9_]*)\}/g, function (match, key) {
      return Object.prototype.hasOwnProperty.call(values || {}, key) ? String(values[key]) : match;
    });
  }

  function clearKeys() {
    writeKey(KEYS.api, "");
    writeKey(KEYS.llm, "");
    writeKey(KEYS.asr, "");
  }

  function valueOf(id) {
    var input = document.getElementById(id);
    return input ? input.value.trim() : "";
  }

  function login() {
    window.htmx.ajax("POST", "/ui/run/login", { target: "#app", swap: "innerHTML" });
  }

  function selectTab(button) {
    var shell = button.closest(".shell");
    if (!shell) {
      return;
    }
    shell.querySelectorAll("[data-tab]").forEach(function (tab) {
      tab.setAttribute("aria-selected", tab === button ? "true" : "false");
    });
    shell.querySelectorAll("[data-panel]").forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-panel") !== button.getAttribute("data-tab");
    });
  }

  // Every request to /ui/run/ of this page's own origin carries the stored keys as headers; no
  // other request does (htmx's selfRequestsOnly refuses a foreign one anyway).
  document.addEventListener("htmx:configRequest", function (event) {
    var url;
    try {
      url = new URL(event.detail.path, window.location.href);
    } catch (error) {
      return;
    }
    if (url.origin !== window.location.origin || url.pathname.indexOf(RUN_PREFIX) !== 0) {
      return;
    }
    var api = readKey(KEYS.api);
    var llm = readKey(KEYS.llm);
    var asr = readKey(KEYS.asr);
    event.detail.headers["X-UI-Language"] = document.documentElement.lang;
    if (api) {
      event.detail.headers["Authorization"] = "Bearer " + api;
    }
    if (llm) {
      event.detail.headers["X-LLM-Api-Key"] = llm;
    }
    if (asr) {
      event.detail.headers["X-ASR-Api-Key"] = asr;
    }
  });

  // A refused key is dropped, so a reload does not send it again.
  document.addEventListener("ui-login-failed", function () {
    clearKeys();
    var input = document.getElementById("login-api-key");
    if (input) {
      input.focus();
    }
  });

  document.addEventListener("submit", function (event) {
    if (!event.target || event.target.id !== "login-form") {
      return;
    }
    event.preventDefault();
    writeKey(KEYS.api, valueOf("login-api-key"));
    writeKey(KEYS.llm, valueOf("login-llm-key"));
    writeKey(KEYS.asr, valueOf("login-asr-key"));
    login();
  });

  document.addEventListener("click", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    var choice = target ? target.closest(".language-switch a[hreflang]") : null;
    if (choice) {
      // The link reloads the page in its language; the keys stay in sessionStorage.
      writeLanguage(choice.getAttribute("hreflang"));
      return;
    }
    var button = target ? target.closest("[data-action], [data-tab]") : null;
    if (!button) {
      return;
    }
    if (button.hasAttribute("data-tab")) {
      selectTab(button);
      return;
    }
    var action = button.getAttribute("data-action");
    if (action === "logout") {
      clearKeys();
      window.location.reload();
    } else if (action === "save-keys") {
      writeKey(KEYS.llm, valueOf("keys-llm"));
      writeKey(KEYS.asr, valueOf("keys-asr"));
    } else if (action === "tree-all" || action === "tree-none") {
      selectAll(button.closest("form"), action === "tree-all");
    }
  });

  // The site tree (P10-S20): checking a node checks its subtree, a node shows indeterminate
  // while part of its subtree is checked, and the counter shows how many URLs are selected.
  function nodeBox(item) {
    var label = item.querySelector(":scope > label");
    return label ? label.querySelector("input[type=checkbox]") : null;
  }

  function countSelected(form) {
    var counter = form.querySelector(".tree-counter");
    if (counter) {
      counter.textContent = text("selected", {
        count: form.querySelectorAll("input[name=url]:checked").length
      });
    }
  }

  function settleParents(item) {
    var parent = item.parentElement ? item.parentElement.closest("li") : null;
    while (parent && parent.closest(".tree")) {
      var boxes = Array.prototype.map.call(
        parent.querySelectorAll(":scope > ul > li"),
        nodeBox
      ).filter(Boolean);
      var checked = boxes.filter(function (box) { return box.checked; }).length;
      var partly = boxes.some(function (box) { return box.indeterminate; });
      var own = nodeBox(parent);
      if (own) {
        own.checked = boxes.length > 0 && checked === boxes.length;
        own.indeterminate = partly || (checked > 0 && checked < boxes.length);
      }
      parent = parent.parentElement ? parent.parentElement.closest("li") : null;
    }
  }

  function selectAll(form, value) {
    form.querySelectorAll(".tree input[type=checkbox]").forEach(function (box) {
      box.checked = value;
      box.indeterminate = false;
    });
    countSelected(form);
  }

  document.addEventListener("change", function (event) {
    var box = event.target;
    if (!(box instanceof HTMLInputElement) || !box.closest(".tree")) {
      return;
    }
    var item = box.closest("li");
    item.querySelectorAll("ul input[type=checkbox]").forEach(function (inner) {
      inner.checked = box.checked;
      inner.indeterminate = false;
    });
    box.indeterminate = false;
    settleParents(item);
    countSelected(box.closest("form"));
  });

  // An answer htmx does not swap (a 4xx or 5xx from outside a view, such as the 413 of a body
  // over its route's limit) or no answer at all: a status line at the top of the request's
  // target, set with textContent only (the answer may hold a caller's text).
  function answerReason(xhr) {
    var type = xhr.getResponseHeader("Content-Type") || "";
    if (type.indexOf("application/json") === 0) {
      try {
        var body = JSON.parse(xhr.responseText);
        if (body && body.error && typeof body.error.message === "string") {
          return body.error.message;
        }
      } catch (error) {
        /* not an envelope: the status text below */
      }
    }
    return xhr.statusText || "";
  }

  function showStatus(target, text) {
    if (!(target instanceof Element)) {
      return;
    }
    var line = target.querySelector(":scope > .request-status");
    if (!line) {
      line = document.createElement("p");
      line.className = "request-status";
      line.setAttribute("role", "alert");
      target.insertBefore(line, target.firstChild);
    }
    line.textContent = text;
  }

  document.addEventListener("htmx:responseError", function (event) {
    var xhr = event.detail.xhr;
    var line = text("request-failed", { status: xhr.status, reason: answerReason(xhr) });
    showStatus(event.detail.target, line.trim());
  });

  document.addEventListener("htmx:sendError", function (event) {
    showStatus(event.detail.target, text("network-error"));
  });

  // On a reload the stored key logs in again; the login is only a convenience, every request
  // to /ui/run/ is checked with its key. A page that moves to the stored language logs in there.
  document.addEventListener("DOMContentLoaded", function () {
    if (moveToStoredLanguage()) {
      return;
    }
    if (document.getElementById("login-form") && readKey(KEYS.api)) {
      login();
    }
  });
})();
