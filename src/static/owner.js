"use strict";
const el = (id) => document.getElementById(id);
async function call(action, body) {
  try {
    const options = {headers: {Authorization: "Bearer " + el("token").value}};
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch("/owner/" + action, options);
    const result = await response.json();
    el("result").textContent = JSON.stringify(result, null, 2);
    if (typeof result.require_trade_approval === "boolean") {
      el("approval-controls").hidden = !result.require_trade_approval;
    }
  } catch (_) { el("result").textContent = "Connection failed. Check the service in Render."; }
}
el("status").onclick = () => call("status");
el("halt").onclick = () => call("halt", {halted: true});
el("resume").onclick = () => call("halt", {halted: false});
el("revoke").onclick = () => call("revoke-sessions", {});
el("allocation-status").onclick = () => call("allocation");
el("allocation-set").onclick = () => call("allocation", {target: "default_40_combos_20_sports_40"});
el("approval").onsubmit = (event) => {
  event.preventDefault();
  call("approvals", {request_id: el("request_id").value, ticker: el("ticker").value,
    side: el("side").value, action: el("action").value, contracts: Number(el("contracts").value),
    limit_price_cents: Number(el("price").value), allow_taker: el("taker").checked});
};
