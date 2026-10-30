/*
 * The UI's own script (rulings P10-S13 and P10-S20), the only one besides htmx: the keys, the
 * tabs and the site tree's selection.
 *
 * Keys: the API key and the optional LLM and ASR keys live in this tab's sessionStorage
 * (te.apiKey, te.llmKey, te.asrKey). They are sent only as request headers to /ui/run/, never
 * as a cookie, a form field or part of a URL.
 */
(function () {
  "use strict";

  var KEYS = { api: "te.apiKey", llm: "te.llmKey", asr: "te.asrKey" };
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
      counter.textContent = form.querySelectorAll("input[name=url]:checked").length + " selected";
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

  // On a reload the stored key logs in again; the login is only a convenience, every request
  // to /ui/run/ is checked with its key.
  document.addEventListener("DOMContentLoaded", function () {
    if (document.getElementById("login-form") && readKey(KEYS.api)) {
      login();
    }
  });
})();
