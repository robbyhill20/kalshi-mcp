"use strict";
const el = (id) => document.getElementById(id);

function reveal(note) {
  const box = note.getBoundingClientRect();
  if (box.top < 0 || box.top > window.innerHeight) note.scrollIntoView(true);
}

function report(message) {
  let banner = el("page-error");
  if (!banner) {
    banner = document.createElement("pre");
    banner.id = "page-error";
    banner.className = "feedback";
    banner.setAttribute("role", "alert");
    const main = document.querySelector("main");
    main.insertBefore(banner, main.firstChild);
  }
  banner.textContent = message;
  const box = el("result");
  if (box) box.textContent = message;
  reveal(banner);
}

window.onerror = (message, source, line) => {
  report("Page error: " + message + (line ? " (line " + line + ")" : ""));
};
window.onunhandledrejection = (event) => {
  const reason = event.reason;
  report("Page error: " + (reason && reason.message ? reason.message : reason));
};

function show(control, text) {
  let note = control.nextElementSibling;
  if (!note || !note.classList.contains("feedback")) {
    note = document.createElement("pre");
    note.className = "feedback";
    note.setAttribute("role", "status");
    control.insertAdjacentElement("afterend", note);
  }
  note.textContent = text;
  el("result").textContent = text;
  reveal(note);
}

// randomUUID is missing on Safari before 15.4 and throws outside a secure context.
function uuid() {
  const cryptoObj = window.crypto;
  try {
    if (cryptoObj && typeof cryptoObj.randomUUID === "function") return cryptoObj.randomUUID();
  } catch (_) { /* missing, or a non-secure context where reading it throws */ }
  if (!cryptoObj || typeof cryptoObj.getRandomValues !== "function") {
    throw new Error("This browser cannot create a request id");
  }
  const bytes = new Uint8Array(16);
  cryptoObj.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  let hex = "";
  for (let i = 0; i < bytes.length; i++) hex += bytes[i].toString(16).padStart(2, "0");
  return hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16) + "-"
    + hex.slice(16, 20) + "-" + hex.slice(20);
}

async function call(action, body, control) {
  show(control, "Working…");
  try {
    const options = {headers: {Authorization: "Bearer " + el("token").value}};
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch("/owner/" + action, options);
    if (response.status === 401) {
      show(control, "Wrong or missing ADMIN_TOKEN");
      return;
    }
    let result;
    try { result = await response.json(); }
    catch (_) { show(control, "Connection failed. Check the service in Render."); return; }
    show(control, JSON.stringify(result, null, 2));
    if (typeof result.require_trade_approval === "boolean") {
      el("approval-controls").hidden = !result.require_trade_approval;
    }
  } catch (_) { show(control, "Connection failed. Check the service in Render."); }
}

function bind(id, action, body) {
  const control = el(id);
  control.onclick = () => call(action, body, control);
}

bind("status", "status");
bind("halt", "halt", {halted: true});
bind("resume", "halt", {halted: false});
bind("revoke", "revoke-sessions", {});
bind("allocation-status", "allocation");
bind("allocation-set", "allocation", {target: "default_40_combos_20_sports_40"});
bind("pools", "exchange-balances");

function newTransferId(announce) {
  const button = el("transfer-new-id");
  try {
    el("transfer-request-id").value = uuid();
    if (announce) show(button, "New request id ready");
  } catch (_) {
    const message = "Could not create a request id in this browser.";
    show(button, message);
    report("Page error: " + message);
  }
}

el("transfer-new-id").onclick = () => newTransferId(true);
el("transfer").onsubmit = (event) => {
  event.preventDefault();
  const button = event.submitter || el("transfer").querySelector("button");
  call("transfer", {request_id: el("transfer-request-id").value,
    from_index: Number(el("from-index").value), to_index: Number(el("to-index").value),
    amount_cents: Number(el("transfer-amount").value)}, button);
};
el("approval").onsubmit = (event) => {
  event.preventDefault();
  const button = event.submitter || el("approval").querySelector("button");
  call("approvals", {request_id: el("request_id").value, ticker: el("ticker").value,
    side: el("side").value, action: el("action").value, contracts: Number(el("contracts").value),
    limit_price_cents: Number(el("price").value), allow_taker: el("taker").checked}, button);
};
// After the handlers above. A throw while minting the id must not leave them unset.
newTransferId(false);
