/* Mock ERP UI helpers: toasts, the add-account dialog, tree collapse memory, rail toggle.
   Small and dependency free; HTMX does the requests. */
(function () {
  "use strict";

  var store = {
    get: function (key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } },
    set: function (key, value) { try { window.localStorage.setItem(key, value); } catch (e) { /* private mode */ } }
  };

  // Toasts. The server sends HX-Trigger: {"toast": {"message", "kind"}}.
  function toast(message, kind) {
    var region = document.getElementById("toasts");
    if (!region || !message) return;
    var el = document.createElement("div");
    el.className = "toast";
    var dot = document.createElement("span");
    dot.className = "dot " + (kind || "info");
    el.appendChild(dot);
    el.appendChild(document.createTextNode(message));
    region.appendChild(el);
    while (region.children.length > 3) region.removeChild(region.firstChild);
    setTimeout(function () {
      el.classList.add("leaving");
      setTimeout(function () { el.remove(); }, 220);
    }, 4000);
  }

  document.addEventListener("toast", function (evt) {
    var d = evt.detail || {};
    toast(d.message, d.kind);
  });
  document.addEventListener("htmx:responseError", function (evt) {
    var xhr = evt.detail.xhr;
    toast("The ERP answered " + xhr.status + ". Nothing was changed.", "bad");
  });
  document.addEventListener("htmx:sendError", function (evt) {
    if (evt.detail.requestConfig && evt.detail.requestConfig.verb === "get") return;
    toast("Could not reach the ERP. Is it running?", "bad");
  });

  // Dialogs.
  document.addEventListener("click", function (evt) {
    var opener = evt.target.closest("[data-open-dialog]");
    if (opener) {
      var dlg = document.getElementById(opener.getAttribute("data-open-dialog"));
      if (dlg && dlg.showModal) {
        dlg.showModal();
        var first = dlg.querySelector("input, select");
        if (first) first.focus();
      }
      return;
    }
    if (evt.target.closest("[data-close-dialog]")) {
      var open = evt.target.closest("dialog");
      if (open) open.close();
    }
  });
  // beforeSwap still runs on the attached form; after the swap it is gone from the page.
  document.addEventListener("htmx:beforeSwap", function (evt) {
    var elt = evt.detail.elt;
    if (elt && elt.id === "account-form" && evt.detail.xhr.status < 300) {
      var dlg = document.getElementById("add-account");
      if (dlg && dlg.open) dlg.close();
    }
  });
  // Keep the parent in step with the type: pick the first header of that type.
  document.addEventListener("change", function (evt) {
    if (evt.target.id !== "acc-type") return;
    var parent = document.getElementById("acc-parent");
    if (!parent) return;
    var current = parent.options[parent.selectedIndex];
    if (current && current.dataset.type === evt.target.value) return;
    for (var i = 0; i < parent.options.length; i++) {
      if (parent.options[i].dataset.type === evt.target.value) { parent.selectedIndex = i; return; }
    }
    parent.selectedIndex = 0;
  });
  // Suggest GL-<code> as the reference while the reference is untouched.
  document.addEventListener("input", function (evt) {
    if (evt.target.id !== "acc-code") return;
    var ref = document.getElementById("acc-ref");
    if (ref && (!ref.value || ref.dataset.auto === "1")) {
      ref.value = evt.target.value ? "GL-" + evt.target.value : "";
      ref.dataset.auto = "1";
    }
  });
  document.addEventListener("input", function (evt) {
    if (evt.target.id === "acc-ref") evt.target.dataset.auto = "0";
  });

  // Account tree: remember collapsed groups across the 5-second refresh.
  var COLLAPSED = "erp.collapsed";
  function collapsed() {
    var raw = store.get(COLLAPSED);
    return raw ? raw.split(",").filter(Boolean) : [];
  }
  function applyTree(root) {
    var closed = collapsed();
    (root || document).querySelectorAll("details.group[data-code]").forEach(function (d) {
      d.open = closed.indexOf(d.dataset.code) === -1;
    });
  }
  function saveTree() {
    var closed = [];
    document.querySelectorAll("details.group[data-code]").forEach(function (d) {
      if (!d.open) closed.push(d.dataset.code);
    });
    store.set(COLLAPSED, closed.join(","));
  }
  // Save only on a user's click (Enter and Space on a summary also fire click). Swapped-in
  // <details open> elements fire their own toggle events, so those are not a signal.
  document.addEventListener("click", function (evt) {
    if (evt.target.closest("details.group[data-code] > summary")) setTimeout(saveTree, 0);
    var btn = evt.target.closest("[data-tree]");
    if (!btn) return;
    var open = btn.getAttribute("data-tree") === "expand";
    document.querySelectorAll("details.group[data-code]").forEach(function (d) { d.open = open; });
    saveTree();
  });
  document.addEventListener("htmx:afterSwap", function () { applyTree(document); });
  document.addEventListener("htmx:oobAfterSwap", function () { applyTree(document); });

  // Activity rail: show or hide, remembered per browser.
  var RAIL = "erp.rail";
  function setRail(open) {
    var shell = document.getElementById("shell");
    var btn = document.getElementById("rail-toggle");
    if (!shell || !btn) return;
    shell.classList.toggle("rail-closed", !open);
    btn.setAttribute("aria-pressed", open ? "true" : "false");
  }
  document.addEventListener("click", function (evt) {
    if (!evt.target.closest("#rail-toggle")) return;
    var open = document.getElementById("shell").classList.contains("rail-closed");
    setRail(open);
    store.set(RAIL, open ? "open" : "closed");
  });

  document.addEventListener("DOMContentLoaded", function () {
    setRail(store.get(RAIL) !== "closed");
    applyTree(document);
  });
})();
