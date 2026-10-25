const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
let intakeId = null;
let autoStarted = false;

async function api(url, body) {
  const response = await fetch(url, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await response.json(); } catch (_) { /* report the status below */ }
  if (!response.ok) {
    const error = new Error(data.message || data.detail || `Request failed (${response.status})`);
    error.data = data;
    error.status = response.status;
    throw error;
  }
  return data;
}

function setStatus(message, alert = false) {
  const status = $("status");
  status.textContent = message;
  status.setAttribute("role", alert ? "alert" : "status");
}

function setTab(name) {
  for (const tab of ["builtin", "external"]) {
    const selected = tab === name;
    $(`tab-${tab}`).setAttribute("aria-selected", String(selected));
    $(`panel-${tab}`).classList.toggle("hidden", !selected);
  }
}

function renderErrors(errors) {
  const items = (errors || []).map((error) => `<li>${esc(error)}</li>`).join("");
  $("result").innerHTML = `<div class="error-list"><strong>Fix these extraction errors:</strong><ul>${items}</ul></div>`;
}

function renderAssessment(data) {
  intakeId = data.intake_id;
  const warnings = (data.warnings || []).map((warning) => `<li>${esc(warning)}</li>`).join("");
  const assumptions = (data.assumptions || []).map((assumption) => `<li>${esc(assumption)}</li>`).join("");
  const summaryRows = Object.entries(data.summary || {}).map(([path, item]) =>
    `<tr><td>${esc(path)}</td><td>${esc(item.value)}</td><td>${esc(item.unit || "—")}</td><td>${esc(item.source || "—")}</td><td class="evidence">${esc(item.evidence || "—")}</td></tr>`,
  ).join("");
  const questions = data.questions || [];
  const questionForm = questions.length ? `
    <form id="answers-form">
      <div class="notice"><strong>I need ${questions.reduce((count, question) => count + question.fields.length, 0)} values before I can run:</strong>
      ${questions.map((question) => `<p>${esc(question.message)}</p>`).join("")}</div>
      ${questions.flatMap((question) => question.fields.map((field) => {
        const current = field.current && typeof field.current === "object" && "value" in field.current
          ? field.current.value
          : field.current;
        const display = Array.isArray(current) ? current.join(", ") : current ?? "";
        const type = field.kind === "quantity" || field.kind === "number_list" || field.kind === "heat_source" ? "text" : "text";
        const placeholder = field.kind === "number_list" ? "e.g. 2, 2.5, 3" : "Enter a value";
        return `<div class="question"><input type="hidden" data-path="${esc(field.path)}" />
          <div class="field-grid">
            <div><label>${esc(field.label)} <span class="muted">(${esc(field.path)})</span></label></div>
            <div><input type="${type}" data-kind="${esc(field.kind)}" data-answer="${esc(field.path)}" value="${esc(display)}" placeholder="${placeholder}" required /></div>
            <div><label>Unit</label><input type="text" data-unit="${esc(field.path)}" value="${esc(field.unit || field.current?.unit || "")}" placeholder="unit" /></div>
          </div>
        </div>`;
      })).join("")}
      <button class="next" type="submit">Submit answers</button>
    </form>` : "";
  $("result").innerHTML = `
    ${summaryRows ? `<h3>Extracted requirements</h3><div style="overflow-x:auto"><table class="summary"><thead><tr><th>Field</th><th>Value</th><th>Unit</th><th>Source</th><th>Evidence</th></tr></thead><tbody>${summaryRows}</tbody></table></div>` : ""}
    ${warnings ? `<div class="notice"><strong>Warnings</strong><ul>${warnings}</ul></div>` : ""}
    ${assumptions ? `<div class="notice"><strong>Assumptions</strong><ul>${assumptions}</ul></div>` : ""}
    ${questionForm}
    ${data.status === "ok" && !questions.length ? `<p class="notice">Requirements are complete. Starting the reduced-order design search…</p>` : ""}
  `;
  $("answers-form")?.addEventListener("submit", submitAnswers);
  if (data.status === "ok" && !questions.length) startModelling();
}

async function handleAssessmentRequest(promise) {
  $("result").innerHTML = "";
  setStatus("Checking requirements…");
  try {
    const data = await promise;
    if (data.errors?.length) renderErrors(data.errors);
    else renderAssessment(data);
    setStatus(data.errors?.length ? "The extraction needs correction." : "");
  } catch (error) {
    const errors = error.data?.errors || [error.message];
    renderErrors(errors);
    setStatus("The request could not be completed.", true);
  }
}

async function submitAnswers(event) {
  event.preventDefault();
  const answers = {};
  for (const input of $("answers-form").querySelectorAll("[data-answer]")) {
    const path = input.dataset.answer;
    const unit = $(`[data-unit="${CSS.escape(path)}"]`).value.trim();
    let value = input.value.trim();
    if (input.dataset.kind === "number_list" || path.endsWith("_sweep")) {
      value = value.split(",").map((part) => Number(part.trim())).filter(Number.isFinite);
    } else if (value !== "" && Number.isFinite(Number(value))) {
      value = Number(value);
    }
    answers[path] = { value, unit: unit || null };
  }
  await handleAssessmentRequest(api(`/api/intake/${encodeURIComponent(intakeId)}/answers`, { answers }));
}

async function startModelling() {
  if (autoStarted || !intakeId) return;
  autoStarted = true;
  setStatus("Starting the design search…");
  try {
    await api("/api/modelling/start", { intake_id: intakeId });
    setStatus("Design search started. Opening the dashboard…");
    window.location.assign("/dashboard");
  } catch (error) {
    autoStarted = false;
    renderErrors([error.message]);
    setStatus("Could not start the design search.", true);
  }
}

$("tab-builtin").addEventListener("click", () => setTab("builtin"));
$("tab-external").addEventListener("click", () => setTab("external"));
for (const [tab, target] of [["tab-builtin", "external"], ["tab-external", "builtin"]]) {
  $(tab).addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      setTab(target);
      $(`tab-${target}`).focus();
    }
  });
}

$("builtin-file").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (file) $("builtin-text").value = await file.text();
});
$("builtin-submit").addEventListener("click", () => {
  handleAssessmentRequest(api("/api/intake/builtin", { text: $("builtin-text").value }));
});
$("external-submit").addEventListener("click", () => {
  handleAssessmentRequest(api("/api/intake/external", {
    json_text: $("external-json").value,
    source_name: $("source-name").value.trim() || null,
  }));
});
$("copy-prompt").addEventListener("click", async () => {
  const prompt = $("prompt").value;
  try {
    await navigator.clipboard.writeText(prompt);
  } catch (_) {
    $("prompt").focus();
    $("prompt").select();
    document.execCommand("copy");
  }
  $("copy-status").textContent = "Prompt copied.";
});

Promise.all([
  api("/api/intake/capabilities"),
  api("/api/intake/prompt"),
]).then(([capabilities, prompt]) => {
  $("prompt").value = prompt.prompt;
  const available = capabilities.builtin_parser;
  $("builtin-submit").disabled = !available;
  $("builtin-text").disabled = !available;
  $("builtin-file").disabled = !available;
  if (!available) {
    $("builtin-reason").textContent = capabilities.reason || "Built-in parser is unavailable.";
    $("builtin-reason").classList.remove("hidden");
  }
}).catch((error) => {
  setStatus(`Could not load intake capabilities: ${error.message}`, true);
});
