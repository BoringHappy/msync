"use strict";

const state = {
  locations: [],
  conversations: [],
  activeConversation: null,
  detailsExpanded: false,
  searchTimer: null,
};

const elements = {
  content: document.querySelector("#content"),
  conversation: document.querySelector("#conversation"),
  empty: document.querySelector("#empty-state"),
  location: document.querySelector("#location-select"),
  search: document.querySelector("#search-input"),
  sessionList: document.querySelector("#session-list"),
  sessionCount: document.querySelector("#session-count"),
  sidebar: document.querySelector("#sidebar"),
  title: document.querySelector("#conversation-title"),
  subtitle: document.querySelector("#conversation-subtitle"),
  provider: document.querySelector("#conversation-provider"),
  metadata: document.querySelector("#metadata-strip"),
  transcript: document.querySelector("#transcript"),
  toggleDetails: document.querySelector("#toggle-details"),
  detailLabel: document.querySelector("#detail-button-label"),
  footerStatus: document.querySelector("#footer-status"),
  toast: document.querySelector("#toast"),
};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = text;
  return element;
}

async function request(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch (_) {
      // The status text is enough when a proxy returns a non-JSON error page.
    }
    throw new Error(message);
  }
  return response.json();
}

function formatDate(value, long = false) {
  if (!value) return "unknown time";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(undefined, long
    ? { dateStyle: "medium", timeStyle: "medium" }
    : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
  ).format(date);
}

function oneLine(value, fallback = "Untitled session") {
  const normalized = (value || "").replace(/\s+/g, " ").trim();
  return normalized || fallback;
}

function selectedConversationId() {
  const value = new URLSearchParams(window.location.search).get("conversation");
  return value && /^\d+$/.test(value) ? Number(value) : null;
}

function updateUrl(conversationId = state.activeConversation?.summary.id || null) {
  const params = new URLSearchParams();
  if (elements.location.value) params.set("location", elements.location.value);
  if (elements.search.value.trim()) params.set("q", elements.search.value.trim());
  if (conversationId) params.set("conversation", String(conversationId));
  const query = params.toString();
  history.replaceState(null, "", query ? `?${query}` : window.location.pathname);
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.remove("hidden");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => elements.toast.classList.add("hidden"), 5000);
}

async function loadLocations() {
  state.locations = await request("/api/locations");
  const params = new URLSearchParams(window.location.search);
  const requested = params.get("location") || "";
  for (const location of state.locations) {
    const option = node("option", "", `${location.display_name} · ${location.hostname} · ${location.provider} (${location.conversation_count})`);
    option.value = String(location.id);
    option.title = location.root_path;
    elements.location.append(option);
  }
  if ([...elements.location.options].some((option) => option.value === requested)) {
    elements.location.value = requested;
  }
  elements.search.value = params.get("q") || "";
}

async function loadConversations({ keepSelection = false } = {}) {
  elements.sessionList.replaceChildren(node("div", "loading", "Loading sessions…"));
  const params = new URLSearchParams({ limit: "200" });
  if (elements.location.value) params.set("location", elements.location.value);
  if (elements.search.value.trim()) params.set("search", elements.search.value.trim());
  state.conversations = await request(`/api/conversations?${params}`);
  elements.sessionCount.textContent = String(state.conversations.length);
  renderConversationList();

  const requestedId = keepSelection ? state.activeConversation?.summary.id : selectedConversationId();
  const available = state.conversations.some((item) => item.id === requestedId);
  const nextId = available ? requestedId : state.conversations[0]?.id;
  if (nextId) {
    await openConversation(nextId);
  } else {
    state.activeConversation = null;
    elements.conversation.classList.add("hidden");
    elements.empty.classList.remove("hidden");
    elements.empty.querySelector("h1").textContent = "No sessions found";
    elements.empty.querySelector("p").textContent = elements.search.value
      ? "Try a different search or history location."
      : "Upload Claude or Codex history, then refresh this page.";
    elements.footerStatus.textContent = "0 sessions";
    updateUrl(null);
  }
}

function renderConversationList() {
  elements.sessionList.replaceChildren();
  if (!state.conversations.length) {
    elements.sessionList.append(node("div", "no-results", "No matching sessions"));
    return;
  }
  for (const conversation of state.conversations) {
    const card = node("button", "session-card");
    card.type = "button";
    card.dataset.id = String(conversation.id);
    card.addEventListener("click", () => openConversation(conversation.id));

    const top = node("div", "session-card-top");
    top.append(
      node("span", `provider-badge ${conversation.provider}`, conversation.provider),
      node("span", "session-time", formatDate(conversation.ended_at || conversation.started_at)),
    );
    const title = oneLine(conversation.title || conversation.preview || conversation.external_id);
    card.append(
      top,
      node("div", "session-title", title),
      node("div", "session-preview", oneLine(conversation.preview, "No visible user message")),
      node("div", "session-meta", `${conversation.hostname} · ${conversation.message_count} messages · ${conversation.event_count} events`),
    );
    elements.sessionList.append(card);
  }
}

async function openConversation(id) {
  if (!id) return;
  elements.footerStatus.textContent = "Loading transcript…";
  try {
    state.activeConversation = await request(`/api/conversations/${id}`);
    state.detailsExpanded = false;
    renderConversation();
    updateUrl(id);
    document.querySelectorAll(".session-card").forEach((card) => {
      card.classList.toggle("active", Number(card.dataset.id) === id);
    });
    elements.sidebar.classList.remove("open");
  } catch (error) {
    showToast(`Could not load session: ${error.message}`);
    elements.footerStatus.textContent = "Load failed";
  }
}

function addMetadata(label, value, title = value) {
  if (!value) return;
  const wrapper = node("div");
  wrapper.append(node("dt", "", label));
  const description = node("dd", "", value);
  description.title = title || "";
  wrapper.append(description);
  elements.metadata.append(wrapper);
}

function renderConversation() {
  const detail = state.activeConversation;
  const summary = detail.summary;
  elements.empty.classList.add("hidden");
  elements.conversation.classList.remove("hidden");
  elements.provider.textContent = summary.provider;
  elements.title.textContent = oneLine(summary.title || summary.preview || summary.external_id);
  elements.subtitle.textContent = detail.relative_path;
  elements.subtitle.title = detail.relative_path;
  elements.metadata.replaceChildren();
  addMetadata("provider", summary.provider);
  addMetadata("hostname", summary.hostname);
  addMetadata("time", formatDate(summary.started_at || summary.ended_at, true));
  addMetadata("model", summary.model);
  addMetadata("branch", summary.git_branch);
  addMetadata("cwd", summary.cwd);
  addMetadata("events", `${summary.message_count} messages / ${summary.event_count} total`);
  updateDetailsButton();
  renderEvents();
  elements.footerStatus.textContent = `${summary.message_count} messages · ${summary.provider}`;
  elements.content.scrollTo({ top: 0 });
}

function updateDetailsButton() {
  elements.toggleDetails.setAttribute("aria-pressed", String(state.detailsExpanded));
  elements.detailLabel.textContent = state.detailsExpanded ? "Collapse details" : "Expand details";
  document.querySelector("#footer-details").lastChild.textContent = state.detailsExpanded
    ? " Collapse detail"
    : " Expand detail";
}

function visibleEvents() {
  if (state.detailsExpanded) return state.activeConversation.events;
  return state.activeConversation.events.filter((event) => event.visibility === "display" && event.text);
}

function eventRole(event) {
  if (event.role) return event.role;
  if (event.event_type.includes("tool")) return "tool";
  return "metadata";
}

function eventMarker(role) {
  return { user: "›", assistant: "◆", tool: "⚙", system: "•", metadata: "·" }[role] || "·";
}

function renderEvents() {
  elements.transcript.replaceChildren();
  const events = visibleEvents();
  if (!events.length) {
    elements.transcript.append(node("div", "no-results", "This session has no visible messages. Expand details to inspect its raw events."));
    return;
  }
  for (const event of events) elements.transcript.append(renderEvent(event));
}

function renderEvent(event) {
  const role = eventRole(event);
  const wrapper = node("section", `event ${role}`);
  wrapper.append(node("span", "event-marker", eventMarker(role)));

  const heading = node("div", "event-heading");
  const roleLabel = role === "user" ? "You" : role;
  heading.append(
    node("span", "event-role", roleLabel),
    node("span", "event-time", event.occurred_at ? formatDate(event.occurred_at, true) : ""),
  );
  const type = [event.event_type, event.event_subtype].filter(Boolean).join(" / ");
  const eventType = node("span", "event-type", type);
  eventType.title = type;
  heading.append(eventType);
  const toggle = node("button", "detail-toggle", state.detailsExpanded ? "hide" : "details");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", String(state.detailsExpanded));
  heading.append(toggle);
  wrapper.append(heading);

  if (event.text) {
    wrapper.append(node("div", "message-text", event.text));
  } else {
    wrapper.append(node("div", "metadata-event", `${event.visibility} event · no normalized text`));
  }

  const details = renderEventDetails(event);
  details.classList.toggle("hidden", !state.detailsExpanded);
  wrapper.append(details);
  toggle.addEventListener("click", () => {
    const expanded = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.textContent = expanded ? "hide" : "details";
    details.classList.toggle("hidden", !expanded);
  });
  return wrapper;
}

function renderEventDetails(event) {
  const details = node("div", "event-details");
  const title = node("div", "details-title");
  title.append(node("span", "", "EVENT DETAIL"), node("span", "", `#${event.sequence}`));
  details.append(title);

  const grid = node("div", "detail-grid");
  const values = [
    ["visibility", event.visibility],
    ["type", event.event_type],
    ["subtype", event.event_subtype || "—"],
    ["external id", event.external_id || "—"],
  ];
  for (const [label, value] of values) {
    const cell = node("div", "detail-cell");
    cell.append(node("b", "", label), node("span", "", value));
    cell.lastChild.title = value;
    grid.append(cell);
  }
  details.append(grid);
  if (event.parse_error) details.append(node("div", "parse-error", event.parse_error));

  for (const part of event.parts) {
    const partBlock = node("div", "part-block");
    partBlock.append(node("div", "part-label", `part ${part.sequence} · ${part.content_type}`));
    if (part.text && part.text !== event.text) partBlock.append(node("div", "message-text", part.text));
    partBlock.append(node("pre", "raw-json", prettyJson(part.raw_json)));
    details.append(partBlock);
  }
  details.append(node("div", "raw-label", "LOSSLESS SOURCE JSON"));
  details.append(node("pre", "raw-json", prettyJson(event.raw_json)));
  return details;
}

function prettyJson(raw) {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch (_) {
    return raw;
  }
}

function toggleAllDetails() {
  if (!state.activeConversation) return;
  state.detailsExpanded = !state.detailsExpanded;
  updateDetailsButton();
  renderEvents();
}

function toggleSidebar() {
  elements.sidebar.classList.toggle("open");
}

elements.location.addEventListener("change", () => loadConversations().catch(handleError));
elements.search.addEventListener("input", () => {
  window.clearTimeout(state.searchTimer);
  state.searchTimer = window.setTimeout(() => loadConversations().catch(handleError), 250);
});
elements.toggleDetails.addEventListener("click", toggleAllDetails);
document.querySelector("#footer-details").addEventListener("click", toggleAllDetails);
document.querySelector("#sessions-button").addEventListener("click", toggleSidebar);
document.querySelector("#footer-sessions").addEventListener("click", toggleSidebar);
document.querySelector("#scroll-top").addEventListener("click", () => elements.content.scrollTo({ top: 0 }));

document.addEventListener("keydown", (event) => {
  if (event.ctrlKey && event.key.toLowerCase() === "o") {
    event.preventDefault();
    toggleAllDetails();
  } else if (event.key === "/" && document.activeElement !== elements.search) {
    event.preventDefault();
    elements.search.focus();
  } else if (event.key === "Escape") {
    elements.sidebar.classList.remove("open");
    elements.search.blur();
  }
});

function handleError(error) {
  showToast(error.message);
  elements.footerStatus.textContent = "Request failed";
}

async function start() {
  try {
    await loadLocations();
    await loadConversations();
  } catch (error) {
    handleError(error);
    elements.sessionList.replaceChildren(node("div", "no-results", "Could not load archive"));
  }
}

start();
