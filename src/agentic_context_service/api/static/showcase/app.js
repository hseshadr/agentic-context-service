const MAX_EVENTS_PER_LANE = 8;
const MAX_AUDIT_EVENTS = 6;
const SAFE_METADATA = new Set([
  "source", "record_id", "source_version", "action", "adapter", "attempt",
  "citation_count", "component", "decision", "document_id", "entity_type", "error_code",
  "event_id", "field_count", "index_alias", "operation", "outcome", "reason_code",
  "replay", "retryable", "state_from", "state_to", "step", "tool_name", "transition", "workflow", "expires_in_seconds",
]);
const LANE_TITLES = {
  source: "Source change received",
  cdc: "Governed context projection updated",
  agent: "Agent proposal evaluated",
  human: "Human approval checkpoint recorded",
  transaction: "Deterministic transaction transition recorded",
};

const ui = {
  form: document.querySelector("#run-form"),
  runId: document.querySelector("#run-id"),
  signal: document.querySelector(".signal"),
  signalTitle: document.querySelector("[data-message-title]"),
  signalBody: document.querySelector("[data-message-body]"),
  connection: document.querySelector(".connection"),
  connectionLabel: document.querySelector("[data-connection-label]"),
  runState: document.querySelector("[data-run-state]"),
  commandButtons: [...document.querySelectorAll("[data-command]")],
  facts: document.querySelector("[data-facts]"),
  audit: document.querySelector("[data-audit-log]"),
  template: document.querySelector("#event-template"),
};

let stream;
let currentRunId = "";
let snapshotSequence = 0;
let awaitingApproval = false;

function text(value, fallback = "—") {
  if (typeof value !== "string" && typeof value !== "number") return fallback;
  return String(value).slice(0, 160);
}

function normalizedRunId(value) {
  return value.trim().replace(/[^A-Za-z0-9._-]/g, "").slice(0, 120);
}

function endpoint(runId, suffix = "") {
  return `/v1/showcase/runs/${encodeURIComponent(runId)}${suffix}`;
}

function setConnection(state, label) {
  ui.connection.dataset.connection = state;
  ui.connectionLabel.textContent = label;
}

function setSignal(state, title, body) {
  ui.signal.dataset.message = state;
  ui.signalTitle.textContent = title;
  ui.signalBody.textContent = body;
}

function setControls(enabled) {
  ui.commandButtons.forEach((button) => {
    button.disabled = !enabled || (button.dataset.command !== "start" && !awaitingApproval);
  });
}

function resetFlow() {
  document.querySelectorAll("[data-events]").forEach((list) => {
    list.replaceChildren(Object.assign(document.createElement("li"), {
      className: "empty-event", textContent: "Waiting for redacted events.",
    }));
  });
  ui.audit.replaceChildren(Object.assign(document.createElement("li"), {
    className: "empty-event", textContent: "No redacted audit events yet.",
  }));
  ui.facts.querySelectorAll("dd").forEach((element) => { element.textContent = "—"; });
  ui.runState.textContent = "—";
  awaitingApproval = false;
}

function metadata(event) {
  const source = event.public_metadata && typeof event.public_metadata === "object"
    ? event.public_metadata : {};
  return Object.fromEntries(Object.entries(source)
    .filter(([key, value]) => SAFE_METADATA.has(key) && (typeof value === "string" || typeof value === "number"))
    .map(([key, value]) => [key, text(value)]));
}

function normalizeEvent(raw) {
  if (!raw || typeof raw !== "object") return null;
  const lane = ["source", "cdc", "agent", "human", "transaction"].includes(raw.lane) ? raw.lane : null;
  if (!lane) return null;
  const safeMetadata = metadata(raw);
  return {
    lane,
    type: text(raw.type, "event"),
    title: text(raw.public_title, LANE_TITLES[lane]),
    status: ["pending", "success", "failed", "compensated", "proposed"].includes(raw.status)
      ? raw.status : "pending",
    occurredAt: text(raw.occurred_at, "now"),
    metadata: safeMetadata,
  };
}

function createCard(event) {
  const node = ui.template.content.firstElementChild.cloneNode(true);
  node.dataset.status = event.status;
  node.querySelector(".event-type").textContent = event.type;
  const time = node.querySelector("time");
  time.textContent = event.occurredAt;
  time.dateTime = event.occurredAt === "now" ? "" : event.occurredAt;
  node.querySelector(".event-title").textContent = event.title;
  const details = node.querySelector(".event-meta");
  Object.entries(event.metadata).slice(0, 5).forEach(([key, value]) => {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = key.replaceAll("_", " ");
    description.textContent = value;
    row.append(term, description);
    details.append(row);
  });
  return node;
}

function updateFacts(event) {
  const values = {
    Tenant: event.metadata.tenant,
    Policy: event.metadata.policy_result,
    "Citation version": event.metadata.source_version,
    Trace: event.metadata.trace_id,
  };
  ui.facts.querySelectorAll("div").forEach((fact) => {
    const label = fact.querySelector("dt").textContent;
    if (values[label]) fact.querySelector("dd").textContent = values[label];
  });
}

function appendEvent(raw, { audit = true } = {}) {
  const event = normalizeEvent(raw);
  if (!event) return;
  const list = document.querySelector(`[data-events="${event.lane}"]`);
  list.querySelector(".empty-event")?.remove();
  list.prepend(createCard(event));
  while (list.children.length > MAX_EVENTS_PER_LANE) list.lastElementChild.remove();
  if (audit) {
    ui.audit.querySelector(".empty-event")?.remove();
    ui.audit.prepend(createCard(event));
    while (ui.audit.children.length > MAX_AUDIT_EVENTS) ui.audit.lastElementChild.remove();
  }
  updateFacts(event);
  if (event.lane === "human") {
    awaitingApproval = event.status === "pending";
    setControls(Boolean(currentRunId));
  }
  setSignal("active", "Live trace connected", "Showing only allowlisted, redacted workflow metadata.");
}

function applySnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== "object") throw new Error("Invalid run snapshot.");
  snapshotSequence = Number.isSafeInteger(snapshot.sequence) ? snapshot.sequence : 0;
  ui.runState.textContent = text(snapshot.status, "connected");
  awaitingApproval = snapshot.status === "awaiting_approval";
  const events = Array.isArray(snapshot.events) ? snapshot.events : [];
  events.forEach((event) => appendEvent(event));
}

function disconnect() {
  stream?.close();
  stream = undefined;
  setControls(false);
}

async function connect(runId) {
  disconnect();
  resetFlow();
  currentRunId = runId;
  setConnection("connecting", "Connecting to redacted event stream…");
  setSignal("connecting", "Loading the latest durable state", "The server snapshot establishes the trace position before the live stream begins.");
  try {
    const response = await fetch(endpoint(runId), { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`Snapshot unavailable (${response.status}).`);
    applySnapshot(await response.json());
    openStream(runId, snapshotSequence);
    setControls(true);
  } catch (error) {
    setConnection("error", "Connection unavailable");
    setSignal("error", "This run is not available", text(error instanceof Error ? error.message : "Unable to connect."));
  }
}

function openStream(runId, after) {
  const url = new URL(endpoint(runId, "/events"), window.location.origin);
  if (after > 0) url.searchParams.set("after", String(after));
  stream = new EventSource(url);
  stream.onopen = () => setConnection("connected", "Live redacted event stream");
  stream.onmessage = (message) => {
    try { appendEvent(JSON.parse(message.data)); } catch { /* malformed events are intentionally ignored */ }
  };
  stream.onerror = () => {
    setConnection("error", "Reconnecting to event stream…");
    setSignal("error", "Live updates interrupted", "The browser will retry. The durable snapshot remains visible; no workflow authority exists in this screen.");
  };
}

async function command(action) {
  if (!currentRunId) return;
  try {
    const response = await fetch(endpoint(currentRunId, "/commands"), {
      method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ action }),
    });
    if (!response.ok) throw new Error(`Command was not accepted (${response.status}).`);
    setSignal("active", "Command accepted", "The authoritative service will emit the resulting redacted state transitions.");
  } catch (error) {
    setSignal("error", "Command unavailable", text(error instanceof Error ? error.message : "The command endpoint is unavailable."));
  }
}

ui.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const runId = normalizedRunId(ui.runId.value);
  if (!runId) {
    setSignal("error", "A run ID is required", "Use letters, numbers, periods, hyphens, or underscores.");
    return;
  }
  ui.runId.value = runId;
  void connect(runId);
});
ui.commandButtons.forEach((button) => button.addEventListener("click", () => void command(button.dataset.command)));
window.addEventListener("beforeunload", disconnect);
setControls(false);
