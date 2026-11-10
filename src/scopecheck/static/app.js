"use strict";
(() => {
  let token = "", authEpoch = 0, nextRuns = null, nextCases = null, selectedRun = null;
  const el = id => document.getElementById(id);
  const message = text => { el("message").textContent = text; };
  async function api(path, options = {}) {
    if (!token) throw new Error("Connect with an operator token first.");
    const epoch = authEpoch;
    const response = await fetch(path, {...options, cache: "no-store", credentials: "omit",
      headers: {"Authorization": `Bearer ${token}`, ...(options.headers || {})}});
    const body = await response.json();
    if (epoch !== authEpoch) throw new Error("Connection changed; reload results.");
    if (!response.ok) throw new Error(`${body.code}: ${body.message}`);
    return body;
  }
  const json = value => JSON.stringify(value, null, 2);
  const post = (path, body, headers = {}) => api(path, {method: "POST", body: JSON.stringify(body), headers: {"Content-Type": "application/json", ...headers}});
  const task = fn => async event => {
    if (event) event.preventDefault();
    try { await fn(); } catch (error) { message(error.message); }
  };
  async function runs(cursor = null) {
    const data = await api("/api/v1/runs" + (cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""));
    el("runs").replaceChildren();
    for (const run of data.items) {
      const row = document.createElement("tr");
      const cell = document.createElement("td"), link = document.createElement("a");
      link.href = "#run=" + encodeURIComponent(run.id); link.textContent = run.id;
      cell.append(link); row.append(cell);
      for (const text of [run.snapshot.mode, run.status, `${run.completed_cases}/${run.total_cases}`, run.created_at]) {
        const td = document.createElement("td"); td.textContent = text; row.append(td);
      }
      el("runs").append(row);
    }
    nextRuns = data.next_cursor; el("next-runs").disabled = !nextRuns;
    message("Synthetic runs loaded.");
  }
  async function cases(cursor = null) {
    const runId = selectedRun;
    const data = await api(`/api/v1/runs/${encodeURIComponent(runId)}/cases` + (cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""));
    if (runId !== selectedRun) return;
    el("cases").replaceChildren();
    for (const item of data.items) {
      const detail = document.createElement("details"), summary = document.createElement("summary"), pre = document.createElement("pre");
      summary.textContent = `${item.case_id} · repetition ${item.repetition} · ${item.status}`;
      pre.textContent = json(item); detail.append(summary, pre); el("cases").append(detail);
    }
    nextCases = data.next_cursor; el("next-cases").disabled = !nextCases;
  }
  async function detail() {
    const runId = selectedRun;
    el("run-title").textContent = runId;
    const result = await api(`/api/v1/runs/${encodeURIComponent(runId)}/report`);
    if (runId !== selectedRun) return;
    el("report").textContent = json(result);
    await cases();
    message("Synthetic report loaded. Check coverage before interpreting counts.");
  }
  async function route() {
    const hash = location.hash.slice(1);
    selectedRun = hash.startsWith("run=") ? decodeURIComponent(hash.slice(4)) : null;
    el("runs-view").hidden = !!selectedRun || hash === "compare";
    el("detail-view").hidden = !selectedRun;
    el("compare-view").hidden = hash !== "compare";
    if (token) { if (selectedRun) await detail(); else if (hash !== "compare") await runs(); }
  }
  function clearViews() {
    for (const id of ["runs", "cases", "report", "comparison"]) el(id).replaceChildren();
  }
  el("connect").addEventListener("submit", task(async () => {
    authEpoch++; token = el("token").value.trim(); el("token").value = ""; clearViews(); await route();
  }));
  el("disconnect").addEventListener("click", () => {
    authEpoch++; token = ""; el("token").value = "";
    clearViews();
    message("Token and displayed results cleared.");
  });
  el("refresh").addEventListener("click", task(() => runs()));
  el("next-runs").addEventListener("click", task(() => runs(nextRuns)));
  el("next-cases").addEventListener("click", task(() => cases(nextCases)));
  el("refresh-detail").addEventListener("click", task(detail));
  el("import").addEventListener("submit", task(async () => {
    const file = el("file").files[0];
    if (!file || file.size > 1048576) throw new Error("Choose a dataset no larger than 1 MiB.");
    const data = await api("/api/v1/datasets", {method: "POST", headers: {"Content-Type": "application/json"}, body: file});
    el("dataset").value = data.id; message("Immutable dataset imported.");
  }));
  el("launch").addEventListener("submit", task(async () => {
    const config = await post("/api/v1/configs", {mode: "fake", fixture: el("fixture").value});
    const run = await post("/api/v1/runs", {dataset_id: el("dataset").value.trim(), config_id: config.id, repetitions: Number(el("repetitions").value)}, {"Idempotency-Key": crypto.randomUUID()});
    location.hash = "run=" + encodeURIComponent(run.id);
  }));
  el("compare").addEventListener("submit", task(async () => {
    const params = new URLSearchParams({baseline: el("baseline").value.trim(), candidate: el("candidate").value.trim()});
    el("comparison").textContent = "";
    el("comparison").textContent = json(await api("/api/v1/comparisons?" + params));
    message("Compatible synthetic runs compared; inspect coverage on both sides.");
  }));
  window.addEventListener("hashchange", task(route));
  task(route)();
})();
