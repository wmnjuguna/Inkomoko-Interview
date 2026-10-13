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


  // ---------- Journal list: remember the chosen entry for the 5-second refresh ----------
  document.addEventListener("htmx:beforeRequest", function (evt) {
    var card = evt.detail.elt.closest && evt.detail.elt.closest(".entry-card");
    if (!card) return;
    var hidden = document.getElementById("j-selected");
    if (hidden) hidden.value = card.dataset.ref;
    document.querySelectorAll(".entry-card[aria-current]").forEach(function (c) { c.removeAttribute("aria-current"); });
    card.setAttribute("aria-current", "true");
  });

  // After a swap that the user asked for, move focus to the new content.
  document.addEventListener("htmx:afterSwap", function (evt) {
    var target = evt.detail.target;
    if (!target || !target.id) return;
    if (target.id === "entry-detail" || (evt.detail.elt && evt.detail.elt.closest && evt.detail.elt.closest(".entry-detail"))) {
      var detail = document.getElementById("entry-detail");
      var heading = detail && detail.querySelector("h2");
      if (heading) heading.focus({ preventScroll: true });
      if (detail && window.matchMedia("(max-width: 860px)").matches) {
        detail.scrollIntoView({ block: "start", behavior: reducedMotion() ? "auto" : "smooth" });
      }
    }
    if (target.id === "entry-form") {
      var notice = document.getElementById("entry-notice");
      if (notice) notice.focus({ preventScroll: false });
      updateBalance();
    }
  });
  function reducedMotion() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  // ---------- New entry: lines, balance check, reversal field ----------
  function amount(input) {
    var raw = (input && input.value || "").replace(/[\s,]/g, "");
    return /^\d+$/.test(raw) ? parseInt(raw, 10) : 0;
  }
  function fmt(n) { return n.toLocaleString("en-US"); }

  function renumber(form) {
    var rows = form.querySelectorAll("[data-lines] [data-line]");
    rows.forEach(function (row, i) {
      var n = i + 1;
      row.querySelectorAll("[data-label]").forEach(function (label) {
        var kind = label.getAttribute("data-label");
        var id = "line-" + n + "-" + kind;
        var field = row.querySelector("[name=" + (kind === "account" ? "account_code" : kind) + "]");
        label.setAttribute("for", id);
        label.textContent = "Line " + n + " " + kind;
        if (field) field.id = id;
      });
      var remove = row.querySelector("[data-remove-line]");
      if (remove) {
        remove.setAttribute("aria-label", "Remove line " + n);
        remove.disabled = rows.length <= 2;
      }
    });
  }

  function updateBalance() {
    var form = document.getElementById("entry-form");
    if (!form) return;
    var dr = 0, cr = 0, used = 0;
    form.querySelectorAll("[data-lines] [data-line]").forEach(function (row) {
      var d = amount(row.querySelector("[name=debit]"));
      var c = amount(row.querySelector("[name=credit]"));
      var acct = row.querySelector("[name=account_code]").value;
      dr += d; cr += c;
      if (acct && (d > 0 || c > 0)) used += 1;
    });
    var top = Math.max(dr, cr, 1);
    var balanced = dr === cr && dr > 0;
    var ready = balanced && used >= 2;
    var state = balanced ? "good" : dr === cr ? "idle" : "bad";
    var q = function (sel) { return form.querySelector(sel); };
    q("[data-total=debit]").textContent = fmt(dr);
    q("[data-total=credit]").textContent = fmt(cr);
    q("[data-bar=debit]").style.width = Math.round(dr / top * 100) + "%";
    q("[data-bar=credit]").style.width = Math.round(cr / top * 100) + "%";
    var badge = q("[data-badge]");
    var badgeText = state === "good" ? "Balanced" : state === "idle" ? "Empty" : "Off balance";
    if (badge.textContent !== badgeText) badge.textContent = badgeText;
    badge.className = "balance-badge " + state;
    var diff = q("[data-diff]");
    diff.textContent = dr === cr ? "0" : "Off by " + fmt(Math.abs(dr - cr));
    diff.className = "balance-diff num " + state;
    q("[data-post]").disabled = !ready;
    q("[data-post-hint]").textContent = ready
      ? "Ready. The ERP checks it again when you post."
      : !balanced ? "Debits and credits must be equal."
      : "Needs at least two lines with an account and an amount.";
  }

  function syncReverses(form) {
    var checked = form.querySelector("[data-source]:checked");
    var box = form.querySelector("[data-reverses]");
    if (!box) return;
    var on = checked && checked.value === "REVERSAL";
    box.hidden = !on;
    box.querySelector("input").disabled = !on;
  }

  document.addEventListener("click", function (evt) {
    var form = document.getElementById("entry-form");
    if (!form) return;
    if (evt.target.closest("[data-add-line]")) {
      var tpl = document.getElementById("line-template");
      var row = tpl.content.firstElementChild.cloneNode(true);
      form.querySelector("[data-lines]").appendChild(row);
      renumber(form);
      row.querySelector("select").focus();
      updateBalance();
      return;
    }
    var remove = evt.target.closest("[data-remove-line]");
    if (remove && !remove.disabled) {
      var line = remove.closest("[data-line]");
      var next = line.nextElementSibling || line.previousElementSibling;
      line.remove();
      renumber(form);
      updateBalance();
      var focusTo = next && next.querySelector("[data-remove-line]");
      if (focusTo && !focusTo.disabled) focusTo.focus();
      else form.querySelector("[data-add-line]").focus();
    }
  });
  document.addEventListener("input", function (evt) {
    if (evt.target.closest && evt.target.closest("#entry-form [data-lines]")) updateBalance();
  });
  document.addEventListener("change", function (evt) {
    var form = evt.target.closest && evt.target.closest("#entry-form");
    if (!form) return;
    if (evt.target.matches("[data-source]")) syncReverses(form);
    updateBalance();
  });
  document.addEventListener("htmx:afterSettle", function (evt) {
    var form = document.getElementById("entry-form");
    if (form && evt.detail.target && evt.detail.target.id === "entry-form") { renumber(form); syncReverses(form); }
  });

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
    var form = document.getElementById("entry-form");
    if (form) { renumber(form); syncReverses(form); updateBalance(); }
  });
})();
