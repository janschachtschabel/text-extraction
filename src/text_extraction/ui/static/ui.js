/*
 * The UI's own script (rulings P10-S13, P10-S20, R-P10-F3, P12-S6, P12-S8, P12-S9 and P15-R1),
 * the only one besides htmx: the keys, the language, the tabs, the site tree's selection,
 * folding and type filter, the result's "Copy" button, the page's status line and the status
 * line of an answer htmx does not swap.
 *
 * Keys: the API key and the optional LLM and ASR keys live in this tab's sessionStorage
 * (te.apiKey, te.llmKey, te.asrKey). They are sent only as request headers to /ui/run/, never
 * as a cookie, a form field or part of a URL.
 *
 * Language: the page's language is <html lang>; it goes as X-UI-Language with each request to
 * /ui/run/. The language switch's choice is kept in localStorage (te.lang), never in a cookie;
 * a page opened without a lang query moves to the stored language. The script's own texts come
 * from the page's data-text-* attributes, the texts of the API's error codes from its
 * <template id="ui-error-texts">.
 *
 * Screenshot form: its "custom capture" fields are enabled only with the preset "custom" (without
 * this script they stay usable and the service ignores them for any other preset).
 *
 * Extract form: the "Also take a screenshot" box needs the browser, so it is disabled and
 * unticked while the mode is "fast" (the "never" choice), and the hint that says why is shown.
 *
 * "Site rules": the "Unblock browser" box (package 22) is shown only while the step "ignore" is
 * chosen.
 *
 * Copy (P15-R1, P15-S4): a result's "Copy" button (data-action=copy) is rendered hidden and
 * shown only where the clipboard API exists (a secure context); without this script it stays
 * hidden. It writes the text of the result's first pre.content (the source shown) to the
 * clipboard and says in #ui-status whether the browser allowed it.
 *
 * Tabs: the WAI-ARIA tabs pattern, with a roving tabindex; the arrow keys, Home and End move the
 * focus and the selection.
 *
 * Status: #ui-status (role=status) says "Sending request …" while a request of the user's runs
 * (its target is aria-busy), then the answer's data-announce (a result's summary, the states of
 * a progress view's jobs). A progress view's own refresh (data-refresh) writes its states only
 * when they changed, so a refresh is never read out. A form error with data-error-for is tied to
 * its field (aria-invalid, aria-describedby), unless the field is disabled; the field is scrolled
 * to the middle of the screen and gets the focus.
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

  // Selects the tab "button" (aria-selected, the roving tabindex) and shows its panel.
  function selectTab(button) {
    var shell = button.closest(".shell");
    if (!shell) {
      return;
    }
    shell.querySelectorAll("[data-tab]").forEach(function (tab) {
      tab.setAttribute("aria-selected", tab === button ? "true" : "false");
      tab.tabIndex = tab === button ? 0 : -1;
    });
    shell.querySelectorAll("[data-panel]").forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-panel") !== button.getAttribute("data-tab");
    });
  }

  // The tab a key moves to from "tab": ArrowRight and ArrowLeft (wrapping around), Home, End.
  function tabFor(key, tab) {
    var list = tab.closest("[role=tablist]");
    var tabs = list ? Array.prototype.slice.call(list.querySelectorAll("[role=tab]")) : [];
    var position = tabs.indexOf(tab);
    if (position < 0) {
      return null;
    }
    if (key === "ArrowRight") {
      return tabs[(position + 1) % tabs.length];
    }
    if (key === "ArrowLeft") {
      return tabs[(position - 1 + tabs.length) % tabs.length];
    }
    if (key === "Home") {
      return tabs[0];
    }
    if (key === "End") {
      return tabs[tabs.length - 1];
    }
    return null;
  }

  document.addEventListener("keydown", function (event) {
    var tab = event.target instanceof Element ? event.target.closest("[role=tab]") : null;
    var plain = !event.altKey && !event.ctrlKey && !event.metaKey;
    var next = tab && plain ? tabFor(event.key, tab) : null;
    if (!next) {
      return;
    }
    event.preventDefault();
    selectTab(next);
    next.focus();
  });

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
    var fold = target ? target.closest(".tree button.fold") : null;
    if (fold) {
      setFold(fold, fold.getAttribute("aria-expanded") !== "true");
      return;
    }
    var chip = target ? target.closest(".tree-filter button.chip") : null;
    if (chip) {
      toggleFilter(chip);
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
    } else if (action === "tree-content") {
      selectContent(button.closest("form"), button.getAttribute("data-exclude") || "");
    } else if (action === "tree-unfold-all" || action === "tree-fold-all") {
      button.closest("form").querySelectorAll(".tree button.fold").forEach(function (each) {
        setFold(each, action === "tree-unfold-all");
      });
    } else if (action === "copy") {
      copyResult(button);
    }
  });

  // The result's "Copy" button: shown only where the clipboard API exists.
  function canCopy() {
    return Boolean(window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText);
  }

  function showCopy() {
    if (!canCopy()) {
      return;
    }
    document.querySelectorAll("button[data-action=copy][hidden]").forEach(function (button) {
      button.hidden = false;
    });
  }

  document.addEventListener("DOMContentLoaded", showCopy);
  document.addEventListener("htmx:load", showCopy);

  // Writes the text shown in the button's result (its first pre.content) to the clipboard.
  function copyResult(button) {
    var view = button.closest(".result-view");
    var source = view ? view.querySelector("pre.content") : null;
    if (!source || !canCopy()) {
      announceAgain(text("copy-failed"));
      return;
    }
    navigator.clipboard.writeText(source.textContent).then(
      function () {
        announceAgain(text("copied"));
      },
      function () {
        announceAgain(text("copy-failed"));
      }
    );
  }

  // The site tree (P10-S20): checking a node checks its subtree, a node shows indeterminate
  // while part of its subtree is checked, and the counter shows how many URLs are selected.
  function nodeBox(item) {
    var label = item.querySelector(":scope > .cell-name > label");
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

  // A section's box from its children's: checked when all enabled ones are, indeterminate when
  // some are; a section with no enabled box below it is unchecked (package 16: a URL the site's
  // rules block is disabled under "Respect the site's rules").
  function settle(item) {
    var boxes = Array.prototype.map.call(
      item.querySelectorAll(":scope > ul > li"),
      nodeBox
    ).filter(function (box) { return box && !box.disabled; });
    var own = nodeBox(item);
    if (!own || item.querySelector(":scope > ul") === null) {
      return;
    }
    if (own.name === "url") {
      if (boxes.length === 0) {
        return;  // a page with children keeps its own box
      }
    } else {
      // A section with no enabled box below it cannot be ticked.
      own.disabled = boxes.length === 0;
      if (boxes.length === 0) {
        own.checked = false;
        own.indeterminate = false;
        return;
      }
    }
    var checked = boxes.filter(function (box) { return box.checked; }).length;
    var partly = boxes.some(function (box) { return box.indeterminate; });
    own.checked = checked === boxes.length;
    own.indeterminate = partly || (checked > 0 && checked < boxes.length);
  }

  function settleParents(item) {
    var parent = item.parentElement ? item.parentElement.closest("li") : null;
    while (parent && parent.closest(".tree")) {
      settle(parent);
      parent = parent.parentElement ? parent.parentElement.closest("li") : null;
    }
  }

  // "Select content pages" (package 14): ticks every URL whose page type is not one of
  // ``excluded`` (a space-separated list), a URL without a type too, and unticks the others
  // and every crawled page that failed (``data-failed``, ruling P14-R9). While a type filter
  // (package 18) hides URLs, it acts on the visible ones only.
  function selectContent(form, excluded) {
    var skip = excluded.split(/\s+/).filter(Boolean);
    form.querySelectorAll(".tree input[name=url]").forEach(function (box) {
      // A box the filter hides keeps its state: only what the user sees is ticked.
      if (box.disabled || box.closest("li[hidden]")) {
        return;
      }
      box.checked =
        !box.hasAttribute("data-failed") &&
        skip.indexOf(box.getAttribute("data-page-type") || "") < 0;
      box.indeterminate = false;
    });
    settleAll(form);
    countSelected(form);
  }

  // "Select all" ticks what the user sees: while a type filter (package 18) hides URLs it skips
  // them, like a section tick and "Select content pages". "Select none" clears everything,
  // hidden boxes included, so no invisible tick stays behind.
  function selectAll(form, value) {
    form.querySelectorAll(".tree input[type=checkbox]").forEach(function (box) {
      if (box.disabled || (value && box.closest("li[hidden]"))) {
        return;
      }
      box.checked = value;
      box.indeterminate = false;
    });
    settleAll(form);
    countSelected(form);
  }

  // Every section from its children, children before their parents.
  function settleAll(form) {
    var items = form.querySelectorAll(".tree li");
    for (var index = items.length - 1; index >= 0; index -= 1) {
      settle(items[index]);
    }
  }

  // The mode the tree is judged under: the checked "Site rules" step, else the crawl's.
  function treeMode(form) {
    var radio = form.querySelector("input[name=access]:checked");
    return radio ? radio.getAttribute("data-mode") : form.getAttribute("data-access-mode");
  }

  // Under "enforce" a URL the site's rules block cannot be selected; under any other mode it can.
  function applyTreeMode(form) {
    var enforce = treeMode(form) === "enforce";
    form.querySelectorAll(".tree input[data-blocked]").forEach(function (box) {
      box.disabled = enforce;
      if (enforce) {
        box.checked = false;
      }
    });
    settleAll(form);
    countSelected(form);
  }

  // Folding (package 17): a node with children has a fold button before its label, rendered
  // hidden, so without this script every node stays unfolded. A folded node's list (the
  // button's aria-controls, its sibling) is hidden; its boxes still take part in the selection.
  function setFold(button, open) {
    button.setAttribute("aria-expanded", open ? "true" : "false");
    var list = button.closest("li").querySelector(":scope > ul");
    if (list) {
      list.hidden = !open;
    }
  }

  // Shows a new tree's fold buttons and folds every node below the hosts. A button already
  // shown belongs to a tree set up before, whose folds the user may have changed.
  function initFolding(form) {
    var fresh = form.querySelectorAll(".tree button.fold[hidden]");
    if (fresh.length === 0) {
      return;
    }
    fresh.forEach(function (button) {
      button.hidden = false;
      setFold(button, button.closest("li").parentElement.classList.contains("tree"));
    });
    form.querySelectorAll("[data-action=tree-unfold-all], [data-action=tree-fold-all]").forEach(
      function (button) {
        button.hidden = false;
      }
    );
  }

  // The type filter (package 18): a chip per type, rendered hidden, so without this script the
  // tree shows every node and the text line of the counts stays. A type chip toggles; several
  // make a union; "All" clears them. A leaf stays when its type is chosen, a section when a
  // leaf below it does, and the sections of the leaves left are unfolded. A hidden node is the
  // `hidden` attribute, so its box keeps its state.
  function press(chip, on) {
    chip.setAttribute("aria-pressed", on ? "true" : "false");
  }

  function chosenTypes(form) {
    return Array.prototype.map.call(
      form.querySelectorAll(".tree-filter button.chip[aria-pressed=true]"),
      function (chip) { return chip.getAttribute("data-filter-type"); }
    ).filter(function (type) { return type !== "all"; });
  }

  function applyFilter(form) {
    var types = chosenTypes(form);
    var items = form.querySelectorAll(".tree li");
    // Children come after their parent: walking backwards judges them first.
    for (var index = items.length - 1; index >= 0; index -= 1) {
      var item = items[index];
      var below = item.querySelector(":scope > ul > li:not([hidden])") !== null;
      var own = types.indexOf(item.getAttribute("data-type")) >= 0;
      item.hidden = types.length > 0 && !own && !below;
      var fold = item.querySelector(":scope > .cell-name > button.fold");
      if (types.length > 0 && below && fold) {
        setFold(fold, true);
      }
    }
  }

  function toggleFilter(chip) {
    var form = chip.closest("form");
    var chips = form.querySelectorAll(".tree-filter button.chip");
    var all = form.querySelector(".tree-filter button.chip[data-filter-type=all]");
    if (chip === all) {
      chips.forEach(function (each) { press(each, each === all); });
    } else {
      press(chip, chip.getAttribute("aria-pressed") !== "true");
      press(all, chosenTypes(form).length === 0);
    }
    applyFilter(form);
  }

  function initFilter(form) {
    var chips = form.querySelectorAll(".tree-filter button.chip[hidden]");
    if (chips.length === 0) {
      return;
    }
    chips.forEach(function (chip) { chip.hidden = false; });
    var group = form.querySelector(".tree-filter");
    if (group) {
      group.hidden = false;
    }
    var view = form.closest(".tree-view");
    var line = view ? view.querySelector(".tree-types") : null;
    if (line) {
      line.hidden = true;
    }
  }

  document.addEventListener("htmx:load", function (event) {
    var root = event.target;
    if (!(root instanceof Element)) {
      return;
    }
    var forms = root.matches("form.tree-form") ? [root] : root.querySelectorAll("form.tree-form");
    Array.prototype.forEach.call(forms, function (form) {
      initFolding(form);
      initFilter(form);
      applyTreeMode(form);
    });
  });

  // The live tree (package 18): while its job runs, the tree is wrapped in an element
  // ([data-tree-live], its value the refresh interval in seconds) that htmx replaces every few
  // seconds. What the user did in the old tree is kept: the ticked URLs, the folds (keyed by the
  // section's path, not by the list's generated id), the type filter, the batch options, the
  // scroll position and the focus. A refresh says nothing and moves the focus nowhere; while a
  // text field, select or textarea of the form has the focus it waits.
  var treeSaved = null;
  var autoLoaded = {};

  // A node's key across refreshes: a page by its URL, a section by the labels of its path.
  function nodeKey(item) {
    var box = nodeBox(item);
    if (box && box.name === "url") {
      return "u:" + box.value;
    }
    var labels = [];
    for (var up = item; up; up = up.parentElement ? up.parentElement.closest("li") : null) {
      var label = up.querySelector(":scope > .cell-name .label");
      labels.unshift(label ? label.textContent : "");
    }
    return "s:" + labels.join("\u0000");
  }

  function takesText(element) {
    if (element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement) {
      return true;
    }
    return element instanceof HTMLInputElement &&
      ["checkbox", "radio", "button", "submit", "reset", "image", "hidden"].indexOf(element.type) < 0;
  }

  // The controls of the form outside the tree: the batch options.
  function optionFields(form) {
    return Array.prototype.filter.call(form.elements, function (field) {
      return field.name && !field.closest(".tree") && field.type !== "hidden" &&
        field.type !== "button" && field.type !== "submit";
    });
  }

  function fieldKey(field) {
    return field.name + (field.type === "radio" || field.type === "checkbox" ? "=" + field.value : "");
  }

  // Where the focus is in the tree, as something to find again in the new one.
  function focusPlace(wrapper) {
    var active = document.activeElement;
    if (!(active instanceof Element) || !wrapper.contains(active)) {
      return null;
    }
    var item = active.closest(".tree li");
    if (active.matches(".tree button.fold") && item) {
      return { kind: "fold", key: nodeKey(item) };
    }
    if (active.matches(".tree input[type=checkbox]") && item) {
      return { kind: "box", key: nodeKey(item) };
    }
    if (active.matches(".tree-filter button.chip")) {
      return { kind: "chip", key: active.getAttribute("data-filter-type") };
    }
    // Anything else that can be named again: a control of the batch options by its name and
    // value, a link by its address, the options' summary, a button of the tools, the skip link,
    // the submit button, any element with an id.
    var form = wrapper.querySelector("form.tree-form");
    if (form && optionFields(form).indexOf(active) >= 0) {
      return { kind: "field", key: fieldKey(active) };
    }
    if (active.matches("a[href]:not(.skip-to-actions)")) {
      return { kind: "href", key: active.getAttribute("href") };
    }
    var selector = null;
    if (active.matches("[data-action]")) {
      selector = "[data-action=\"" + active.getAttribute("data-action") + "\"]";
    } else if (active.matches("a.skip-to-actions")) {
      selector = "a.skip-to-actions";
    } else if (active.matches("details.advanced > summary")) {
      selector = "details.advanced > summary";
    } else if (active.matches("button[type=submit]")) {
      selector = "button[type=submit]";
    } else if (active.id) {
      selector = "#" + CSS.escape(active.id);
    }
    return selector ? { kind: "sel", key: selector } : null;
  }

  function findPlace(form, place) {
    if (place.kind === "sel") {
      return (form.closest(".tree-view") || form).querySelector(place.key);
    }
    if (place.kind === "field") {
      return optionFields(form).filter(function (field) { return fieldKey(field) === place.key; })[0];
    }
    if (place.kind === "href") {
      return Array.prototype.filter.call(
        (form.closest(".tree-view") || form).querySelectorAll("a[href]"),
        function (link) { return link.getAttribute("href") === place.key; }
      )[0];
    }
    var found = null;
    var candidates = place.kind === "chip"
      ? form.querySelectorAll(".tree-filter button.chip")
      : place.kind === "fold"
        ? form.querySelectorAll(".tree button.fold")
        : form.querySelectorAll(".tree input[type=checkbox]");
    Array.prototype.forEach.call(candidates, function (candidate) {
      var item = candidate.closest(".tree li");
      var key = place.kind === "chip" ? candidate.getAttribute("data-filter-type")
        : item ? nodeKey(item) : null;
      if (!found && key === place.key) {
        found = candidate;
      }
    });
    return found;
  }

  function saveTree(wrapper) {
    var form = wrapper.querySelector("form.tree-form");
    var saved = { wrapper: wrapper, form: null };
    if (!form) {
      return saved;
    }
    saved.form = {
      ticked: Array.prototype.map.call(
        form.querySelectorAll(".tree input[name=url]:checked"),
        function (box) { return box.value; }
      ),
      folds: {},
      types: chosenTypes(form),
      fields: optionFields(form).map(function (field) {
        return { key: fieldKey(field), value: field.value, checked: field.checked };
      }),
      options_open: Boolean(form.querySelector("details.advanced[open]")),
      scroll: form.querySelector(".tree-box") ? form.querySelector(".tree-box").scrollTop : 0,
      focus: focusPlace(wrapper)
    };
    form.querySelectorAll(".tree button.fold").forEach(function (button) {
      saved.form.folds[nodeKey(button.closest("li"))] = button.getAttribute("aria-expanded") === "true";
    });
    return saved;
  }

  function restoreTree(form, saved) {
    // htmx sets a new tree up (htmx:load) only after this swap: do it now, so the controls to
    // restore are shown. What is shown already is left as it is, so the later setup is a no-op.
    initFolding(form);
    initFilter(form);
    var fields = {};
    saved.fields.forEach(function (field) { fields[field.key] = field; });
    optionFields(form).forEach(function (field) {
      var before = fields[fieldKey(field)];
      if (!before) {
        return;
      }
      if (field.type === "radio" || field.type === "checkbox") {
        field.checked = before.checked;
      } else {
        field.value = before.value;
      }
    });
    var advanced = form.querySelector("details.advanced");
    if (advanced) {
      advanced.open = saved.options_open;
    }
    applyTreeMode(form);  // the restored "Site rules" decide which boxes are disabled
    var ticked = {};
    saved.ticked.forEach(function (value) { ticked[value] = true; });
    form.querySelectorAll(".tree input[name=url]").forEach(function (box) {
      if (ticked[box.value] && !box.disabled) {
        box.checked = true;
      }
    });
    settleAll(form);
    countSelected(form);
    form.querySelectorAll(".tree-filter button.chip").forEach(function (chip) {
      var type = chip.getAttribute("data-filter-type");
      press(chip, saved.types.length === 0 ? type === "all" : saved.types.indexOf(type) >= 0);
    });
    applyFilter(form);  // before the folds: the user's fold of a section beats the filter's unfold
    form.querySelectorAll(".tree button.fold").forEach(function (button) {
      var open = saved.folds[nodeKey(button.closest("li"))];
      if (open !== undefined) {
        setFold(button, open);
      }
    });
    var box = form.querySelector(".tree-box");
    if (box) {
      box.scrollTop = saved.scroll;
    }
    var target = saved.focus ? findPlace(form, saved.focus) : null;
    if (target) {
      target.focus({ preventScroll: true });
    }
  }

  // A refresh that did not happen (the focus was in a text field, the answer was a 204 or an
  // error, the network failed) is asked for again after the interval, or the Retry-After when
  // that is longer: the one-shot "load" trigger of the wrapper is spent.
  function refreshLater(wrapper, seconds) {
    window.clearTimeout(wrapper.treeTimer);
    var wait = Math.max(Number(wrapper.getAttribute("data-tree-live")) || 4, seconds || 0);
    wrapper.treeTimer = window.setTimeout(function () {
      if (wrapper.isConnected) {
        window.htmx.ajax("GET", wrapper.getAttribute("hx-get"), {
          source: wrapper, target: wrapper, swap: "outerHTML"
        });
      }
    }, wait * 1000);
  }

  function typing(wrapper) {
    var active = document.activeElement;
    return active instanceof Element && wrapper.contains(active) && takesText(active);
  }

  function isLive(element) {
    return element instanceof Element && element.hasAttribute("data-tree-live");
  }

  document.addEventListener("htmx:beforeRequest", function (event) {
    var wrapper = event.detail.elt;
    if (isLive(wrapper) && typing(wrapper)) {
      event.preventDefault();
      refreshLater(wrapper, 0);
    }
  });

  // The answer arrived while the user started typing: it is dropped (afterRequest asks again).
  document.addEventListener("htmx:beforeSwap", function (event) {
    var wrapper = event.detail.elt;
    if (!isLive(wrapper) || !event.detail.shouldSwap) {
      return;
    }
    if (typing(wrapper)) {
      event.detail.shouldSwap = false;
    } else {
      treeSaved = saveTree(wrapper);
    }
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    var wrapper = event.detail.elt;
    if (isLive(wrapper) && wrapper.isConnected) {  // not swapped out: nothing new arrived
      var after = event.detail.xhr ? Number(event.detail.xhr.getResponseHeader("Retry-After")) : 0;
      refreshLater(wrapper, after || 0);
    }
  });

  // The note that the tree updates is shown only here: without this script nothing does, and
  // it would contradict the snapshot note, which this hides in turn.
  function showLiveNote(root) {
    var notes = root.matches(".tree-live[hidden]") ? [root] : root.querySelectorAll(".tree-live[hidden]");
    Array.prototype.forEach.call(notes, function (note) {
      note.hidden = false;
      var view = note.closest(".tree-view");
      var snapshot = view ? view.querySelector(".tree-snapshot") : null;
      if (snapshot) {
        snapshot.hidden = true;
      }
    });
  }

  document.addEventListener("htmx:load", function (event) {
    if (event.target instanceof Element) {
      showLiveNote(event.target);
    }
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    var saved = treeSaved;
    treeSaved = null;
    var root = event.target instanceof Element ? event.target : null;
    // The swap that follows the beforeSwap above: its new content is no more the old wrapper.
    if (!saved || !saved.form || !root || root === saved.wrapper) {
      return;
    }
    var form = root.matches("form.tree-form") ? root : root.querySelector("form.tree-form");
    if (form) {
      restoreTree(form, saved.form);
    }
    showLiveNote(root);
  });

  // A site job's tree shows itself once the job has a result: the progress view marks the
  // button (data-tree-auto), and the script presses it once per job unless the tree was asked
  // for already (a click, a request in flight) or something is shown below the jobs. Marked
  // data-refresh, the request is no request of the user's: nothing is announced. A click while
  // the script's own request is in flight is dropped (the tree is on its way), any other click
  // counts as the user's request, so the script never loads the tree a second time. Without
  // this script the "Show tree now" button stays as it is. The progress view replaces the
  // button every few seconds, so the state is kept by the job's button id.
  var autoPending = {};
  var pressing = false;

  document.addEventListener("click", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    var button = target ? target.closest("button.tree-button") : null;
    if (!button || pressing) {
      return;
    }
    if (autoPending[button.id]) {
      event.stopImmediatePropagation();  // htmx listens on the button itself, later
      return;
    }
    autoLoaded[button.id] = true;
  }, true);

  document.addEventListener("htmx:load", function (event) {
    var root = event.target instanceof Element ? event.target : null;
    var buttons = root ? root.querySelectorAll("button.tree-button[data-tree-auto]") : [];
    Array.prototype.forEach.call(buttons, function (button) {
      window.setTimeout(function () {
        var view = button.closest(".jobs-view");
        var detail = view ? view.querySelector(".job-detail") : null;
        if (!button.isConnected || autoLoaded[button.id] || !detail || detail.firstChild ||
            button.classList.contains("htmx-request")) {
          return;
        }
        autoLoaded[button.id] = true;
        autoPending[button.id] = true;
        button.setAttribute("data-refresh", "");
        // Once the answer is in, the button is the user's again.
        var done = function (settled) {
          // A swap is over at afterSettle; a request that swapped nothing ends at afterRequest.
          var over = settled.type === "htmx:afterSettle" || !settled.detail.successful;
          if (settled.detail.elt === button && over) {
            autoPending[button.id] = false;
            button.removeAttribute("data-refresh");
            document.removeEventListener("htmx:afterSettle", done);
            document.removeEventListener("htmx:afterRequest", done);
          }
        };
        document.addEventListener("htmx:afterSettle", done);
        document.addEventListener("htmx:afterRequest", done);
        // A request that never ends (its button gone) must not drop the user's clicks for good.
        window.setTimeout(function () { autoPending[button.id] = false; }, 10000);
        pressing = true;
        button.click();
        pressing = false;
      }, 0);
    });
  });

  // The "Unblock browser" box (package 22) shows only while the chosen "Site rules" step is
  // "ignore" (the radio's data-mode). Without this script it keeps the state it was rendered
  // with; the service reads the box under "ignore" only.
  function syncUnblockBox(form) {
    var group = form.querySelector("[data-unblock]");
    var radio = form.querySelector("input[name=access]:checked");
    if (group && radio) {
      group.hidden = radio.getAttribute("data-mode") !== "ignore";
    }
  }

  function initUnblockBoxes() {
    document.querySelectorAll("form [data-unblock]").forEach(function (group) {
      var form = group.closest("form");
      if (form) {
        syncUnblockBox(form);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", initUnblockBoxes);
  document.addEventListener("htmx:load", initUnblockBoxes);

  document.addEventListener("change", function (event) {
    var radio = event.target;
    if (radio instanceof HTMLInputElement && radio.name === "access" && radio.form) {
      if (radio.form.classList.contains("tree-form")) {
        applyTreeMode(radio.form);
      }
      syncUnblockBox(radio.form);
    }
  });

  // "Fast: sitemap first" needs the sitemaps: without them it is disabled and "Thorough" is chosen.
  // Reading them again enables it, but leaves the choice as it is.
  document.addEventListener("change", function (event) {
    var box = event.target;
    if (!(box instanceof HTMLInputElement) || box.name !== "sitemaps" || !box.form) {
      return;
    }
    var fast = box.form.querySelector("input[name=discovery][value=sitemap_first]");
    if (!fast) {
      return;
    }
    fast.disabled = !box.checked;
    if (!box.checked && fast.checked) {
      var thorough = box.form.querySelector("input[name=discovery][value=crawl]");
      if (thorough) {
        thorough.checked = true;
      }
    }
  });

  document.addEventListener("change", function (event) {
    var box = event.target;
    if (!(box instanceof HTMLInputElement) || !box.closest(".tree")) {
      return;
    }
    var item = box.closest("li");
    item.querySelectorAll("ul input[type=checkbox]").forEach(function (inner) {
      // A type filter's hidden boxes keep their state (package 18).
      if (inner.disabled || inner.closest("li[hidden]")) {
        return;
      }
      inner.checked = box.checked;
      inner.indeterminate = false;
    });
    box.indeterminate = false;
    settle(item);
    settleParents(item);
    countSelected(box.closest("form"));
  });

  // The screenshot form's custom fields apply only with the preset "custom".
  function syncCapture(form) {
    var preset = form.querySelector("#screenshot-preset");
    var group = form.querySelector("#screenshot-custom");
    if (preset && group) {
      group.disabled = preset.value !== "custom";
    }
  }

  document.addEventListener("change", function (event) {
    var select = event.target;
    if (select instanceof HTMLSelectElement && select.id === "screenshot-preset") {
      syncCapture(select.form);
    }
  });

  // The form is on the page at load, or swapped in after the login (htmx:load).
  function initCapture() {
    var form = document.getElementById("screenshot-form");
    if (form) {
      syncCapture(form);
    }
  }

  document.addEventListener("DOMContentLoaded", initCapture);
  document.addEventListener("htmx:load", initCapture);

  // The extract form's screenshot box is off while the mode is "fast" (the API refuses the pair).
  function syncScreenshotBox(form) {
    var mode = form.querySelector("#extract-mode");
    var box = form.querySelector("input[name=screenshot]");
    var hint = form.querySelector("#extract-screenshot-fast-hint");
    if (!(mode && box && hint)) {
      return;
    }
    if (box.hasAttribute("data-no-browser")) {
      return;
    }
    var fast = mode.value === "fast";
    box.disabled = fast;
    if (fast) {
      box.checked = false;
    }
    hint.hidden = !fast;
    // A hidden hint is not read out with the box.
    var ids = describedBy(box).filter(function (id) {
      return id !== hint.id;
    });
    setDescribedBy(box, fast ? ids.concat(hint.id) : ids);
  }

  document.addEventListener("change", function (event) {
    var select = event.target;
    if (select instanceof HTMLSelectElement && select.id === "extract-mode") {
      syncScreenshotBox(select.form);
    }
  });

  function initScreenshotBox() {
    var form = document.getElementById("extract-form");
    if (form) {
      syncScreenshotBox(form);
    }
  }

  document.addEventListener("DOMContentLoaded", initScreenshotBox);
  document.addEventListener("htmx:load", initScreenshotBox);

  // The page's status line (ruling P12-S9).
  function announce(message) {
    var status = document.getElementById("ui-status");
    if (status && status.textContent !== message) {
      status.textContent = message;
    }
  }

  // Says "message" even when #ui-status already holds it (a second "Copy"): the status is
  // cleared first and written again a moment later, so a screen reader reads it once more.
  function announceAgain(message) {
    var status = document.getElementById("ui-status");
    if (!status) {
      return;
    }
    status.textContent = "";
    window.setTimeout(function () {
      status.textContent = message;
    }, 50);
  }

  // Whether "element" is a view that refreshes itself: its requests are no requests of the user's.
  function isRefresh(element) {
    return element instanceof Element && element.hasAttribute("data-refresh");
  }

  // The data-announce texts of "root" and the elements in it, joined.
  function announcement(root) {
    var found = root.matches("[data-announce]") ? [root] : root.querySelectorAll("[data-announce]");
    return Array.prototype.map
      .call(found, function (element) {
        return element.getAttribute("data-announce").trim();
      })
      .filter(Boolean)
      .join(" ");
  }

  // A form error ties itself to its field (ruling P12-S8); a new request of the user's unties it.
  function describedBy(control) {
    return (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
  }

  function setDescribedBy(control, ids) {
    if (ids.length) {
      control.setAttribute("aria-describedby", ids.join(" "));
    } else {
      control.removeAttribute("aria-describedby");
    }
  }

  // Open every closed <details> around a control, so a focus or a browser's validation bubble
  // does not point at a field nobody can see.
  function revealField(control) {
    var section = control.closest("details");
    while (section) {
      section.open = true;
      section = section.parentElement ? section.parentElement.closest("details") : null;
    }
  }

  document.addEventListener("invalid", function (event) {
    if (event.target instanceof Element) {
      revealField(event.target);
    }
  }, true);

  function tieError(root) {
    var error = root.matches("[data-error-for]") ? root : root.querySelector("[data-error-for]");
    var field = error && error.id ? error.getAttribute("data-error-for") : "";
    var control = field ? document.getElementById(field) : null;
    // A disabled control (a custom-capture field under another preset) is not at fault: the
    // status line shows the error (audit U2).
    if (!control || control.matches(":disabled")) {
      return;
    }
    var ids = describedBy(control).filter(function (id) {
      return id !== error.id;
    });
    setDescribedBy(control, ids.concat([error.id]));
    control.setAttribute("aria-invalid", "true");
    control.setAttribute("data-error-id", error.id);
    revealField(control);
    // On a phone the field can be far above the answer: bring it to the middle of the screen,
    // then focus it there (audit U6).
    control.scrollIntoView({block: "center"});
    control.focus({preventScroll: true});
  }

  function untieErrors() {
    document.querySelectorAll("[data-error-id]").forEach(function (control) {
      var error = control.getAttribute("data-error-id");
      setDescribedBy(control, describedBy(control).filter(function (id) {
        return id !== error;
      }));
      control.removeAttribute("aria-invalid");
      control.removeAttribute("data-error-id");
    });
  }

  document.addEventListener("htmx:beforeRequest", function (event) {
    if (isRefresh(event.detail.elt)) {
      return;
    }
    untieErrors();
    announce(text("sending"));
    if (event.detail.target instanceof Element) {
      event.detail.target.setAttribute("aria-busy", "true");
    }
  });

  function notBusy(event) {
    if (event.detail.target instanceof Element) {
      event.detail.target.removeAttribute("aria-busy");
    }
  }

  document.addEventListener("htmx:afterRequest", notBusy);

  // Focus across a swap (ruling R-P12-9). htmx gives the focus back to the element with the
  // focused element's id; when the new content has none, the focus would fall to the page and a
  // keyboard user's next key would start from nowhere. So: after a login the selected tab (the
  // tab list is where the application starts) or, after a refused key, the key field; after a
  // swap that removed the focused element of a progress view, the view itself.
  var focusedBeforeSwap = null;

  document.addEventListener("htmx:beforeSwap", function () {
    focusedBeforeSwap = document.activeElement;
  });

  function keepFocus(root) {
    var before = focusedBeforeSwap;
    focusedBeforeSwap = null;
    if (root.id === "app") {
      var start = root.querySelector("[role=tab][aria-selected=true]") ||
        root.querySelector("#login-api-key");
      if (start) {
        start.focus();
      }
      return;
    }
    var lost = before instanceof Element && before !== document.body && !before.isConnected;
    var active = document.activeElement;
    if (!lost || (active && active !== document.body)) {
      return;
    }
    var view = root.closest(".jobs-view") || root.querySelector(".jobs-view");
    if (view) {
      view.focus({ preventScroll: true });
    }
  }

  // htmx fires afterSwap on the swapped target (innerHTML) or on each new element (outerHTML).
  document.addEventListener("htmx:afterSwap", function (event) {
    var root = event.target instanceof Element ? event.target : null;
    if (!root) {
      return;
    }
    keepFocus(root);
    var message = announcement(root);
    if (isRefresh(event.detail.elt)) {
      // The view before the refresh said its states already.
      if (message && message !== announcement(event.detail.elt)) {
        announce(message);
      }
      return;
    }
    if (message) {
      announce(message);
    } else {
      // An error that replaced its target (outerHTML) is the root itself.
      var alert = root.matches("[role=alert]") || root.querySelector("[role=alert]");
      announce(alert ? "" : text("done"));
    }
    tieError(root);
  });

  // An answer htmx does not swap (a 4xx or 5xx from outside a view, such as the 413 of a body
  // over its route's limit) or no answer at all: a status line at the top of the request's
  // target, the text of the error's code and below it the API's own English message, set with
  // textContent only (the answer may hold a caller's text).
  function answerError(xhr) {
    var type = xhr.getResponseHeader("Content-Type") || "";
    if (type.indexOf("application/json") === 0) {
      try {
        var body = JSON.parse(xhr.responseText);
        if (body && body.error && typeof body.error.message === "string") {
          return { code: String(body.error.code || ""), message: body.error.message };
        }
      } catch (error) {
        /* not an envelope: the status text below */
      }
    }
    return { code: "", message: xhr.statusText || "" };
  }

  // The text of the API's error "code", or "" for a code the page does not know.
  function errorText(code) {
    var holder = document.getElementById("ui-error-texts");
    var texts = holder && code ? holder.content.querySelectorAll("[data-code]") : [];
    for (var index = 0; index < texts.length; index += 1) {
      if (texts[index].getAttribute("data-code") === code) {
        return texts[index].textContent;
      }
    }
    return "";
  }

  function paragraph(className, content) {
    var line = document.createElement("p");
    if (className) {
      line.className = className;
    }
    line.textContent = content;
    return line;
  }

  function showStatus(target, line, detail) {
    if (!(target instanceof Element)) {
      return;
    }
    var box = target.querySelector(":scope > .request-status");
    if (box && isLive(target)) {
      return;  // a live tree's retried refresh keeps its first alert: a refresh announces nothing
    }
    if (!box) {
      box = document.createElement("div");
      box.className = "request-status";
      box.setAttribute("role", "alert");
      target.insertBefore(box, target.firstChild);
    }
    box.textContent = "";
    box.appendChild(paragraph("", line));
    if (detail) {
      var api = document.createElement("div");
      api.className = "api-detail";
      api.appendChild(paragraph("api-detail-label", text("api-details")));
      var message = paragraph("error-message", detail);
      message.setAttribute("lang", "en");
      api.appendChild(message);
      box.appendChild(api);
    }
    announce("");  // the alert says it
  }

  document.addEventListener("htmx:responseError", function (event) {
    var xhr = event.detail.xhr;
    var error = answerError(xhr);
    var reason = errorText(error.code);
    var line = text("request-failed", { status: xhr.status, reason: reason || error.message });
    notBusy(event);
    showStatus(event.detail.target, line.trim(), reason ? error.message : "");
  });

  document.addEventListener("htmx:sendError", function (event) {
    notBusy(event);
    showStatus(event.detail.target, text("network-error"), "");
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
