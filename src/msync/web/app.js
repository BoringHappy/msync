"use strict";

const SESSION_PAGE_SIZE = 50;
const EVENT_PAGE_SIZE = 100;
const LAYOUT_STORAGE_KEY = "msync:fit-width";

const state = {
  locations: [],
  conversations: [],
  activeConversation: null,
  conversationEntries: [],
  conversationController: null,
  conversationLoading: false,
  detailsExpanded: false,
  eventController: null,
  eventLoading: false,
  eventRequest: 0,
  hasMoreConversations: false,
  hiddenContextCount: 0,
  humanCursorSequence: null,
  listController: null,
  listRequest: 0,
  loadingAllEvents: false,
  conversationRequest: 0,
  searchTimer: null,
  fitWidth: false,
  transcriptFilter: "all",
  transcriptQuery: "",
};

const elements = {
  content: document.querySelector("#content"),
  conversation: document.querySelector("#conversation"),
  conversationBottom: document.querySelector("#conversation-bottom"),
  conversationTop: document.querySelector("#conversation-top"),
  copyLink: document.querySelector("#copy-link"),
  clearSearch: document.querySelector("#clear-search"),
  clearTranscriptSearch: document.querySelector("#clear-transcript-search"),
  empty: document.querySelector("#empty-state"),
  archiveStatus: document.querySelector("#archive-status"),
  location: document.querySelector("#location-select"),
  loadMore: document.querySelector("#load-more"),
  nextHuman: document.querySelector("#next-human"),
  order: document.querySelector("#order-select"),
  reload: document.querySelector("#reload-button"),
  search: document.querySelector("#search-input"),
  sessionList: document.querySelector("#session-list"),
  sessionCount: document.querySelector("#session-count"),
  sessionsButton: document.querySelector("#sessions-button"),
  sidebar: document.querySelector("#sidebar"),
  sidebarScrim: document.querySelector("#sidebar-scrim"),
  title: document.querySelector("#conversation-title"),
  titleTooltip: document.querySelector("#conversation-title-tooltip"),
  titleWrap: document.querySelector("#conversation-title-wrap"),
  subtitle: document.querySelector("#conversation-subtitle"),
  provider: document.querySelector("#conversation-provider"),
  previousHuman: document.querySelector("#previous-human"),
  metadata: document.querySelector("#metadata-strip"),
  transcript: document.querySelector("#transcript"),
  toggleDetails: document.querySelector("#toggle-details"),
  toggleWidth: document.querySelector("#toggle-width"),
  detailLabel: document.querySelector("#detail-button-label"),
  footerStatus: document.querySelector("#footer-status"),
  filterButtons: [...document.querySelectorAll("[data-transcript-filter]")],
  transcriptSearch: document.querySelector("#transcript-search"),
  transcriptMatchCount: document.querySelector("#transcript-match-count"),
  toast: document.querySelector("#toast"),
  widthLabel: document.querySelector("#width-button-label"),
};

const sessionLoaderObserver = typeof IntersectionObserver === "undefined"
  ? null
  : new IntersectionObserver((entries) => {
    if (
      !entries.some((entry) => entry.isIntersecting)
      || !state.hasMoreConversations
      || elements.loadMore.disabled
    ) return;
    sessionLoaderObserver.unobserve(elements.loadMore);
    loadConversations({ append: true }).catch(handleError);
  }, { root: elements.sessionList, rootMargin: "0px 0px 160px" });

const transcriptLoaderObserver = typeof IntersectionObserver === "undefined"
  ? null
  : new IntersectionObserver((entries) => {
    const loader = entries.find((entry) => entry.isIntersecting)?.target;
    const activeLoader = elements.transcript.querySelector(".transcript-load-more");
    if (!loader || loader !== activeLoader || state.eventLoading) return;
    transcriptLoaderObserver.unobserve(loader);
    loadMoreEvents();
  }, { root: elements.content, rootMargin: "0px 0px 480px" });

function updateConversationBottom() {
  elements.conversationBottom.disabled = state.conversationLoading
    || state.eventLoading
    || state.loadingAllEvents;
}

function cancelEventPagination() {
  transcriptLoaderObserver?.takeRecords();
  transcriptLoaderObserver?.disconnect();
  state.eventRequest += 1;
  state.eventController?.abort();
  state.eventController = null;
  state.eventLoading = false;
  state.loadingAllEvents = false;
  updateConversationBottom();
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = text;
  return element;
}

async function request(path, { signal } = {}) {
  const response = await fetch(path, {
    headers: {
      Accept: "application/json",
      "X-Msync-Browser-Request": "1",
    },
    signal,
  });
  if (response.status === 401) {
    const next = `${window.location.pathname}${window.location.search}`;
    window.location.assign(`/login?${new URLSearchParams({ next })}`);
    throw new Error("Authentication required.");
  }
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

function updateTitleOverflow() {
  const truncated = elements.title.scrollWidth > elements.title.clientWidth + 1;
  elements.titleWrap.classList.toggle("is-truncated", truncated);
  elements.titleTooltip.setAttribute("aria-hidden", String(!truncated));
  if (truncated) {
    elements.title.tabIndex = 0;
    elements.title.setAttribute("aria-describedby", "conversation-title-tooltip");
  } else {
    elements.title.removeAttribute("tabindex");
    elements.title.removeAttribute("aria-describedby");
  }
}

function setFitWidth(enabled, { persist = false } = {}) {
  state.fitWidth = enabled;
  elements.conversation.classList.toggle("fit-width", enabled);
  elements.toggleWidth.setAttribute("aria-pressed", String(enabled));
  elements.toggleWidth.title = enabled ? "Use reading width" : "Use full window width";
  elements.widthLabel.textContent = enabled ? "Reading width" : "Fit width";
  if (persist) {
    try {
      window.localStorage.setItem(LAYOUT_STORAGE_KEY, enabled ? "true" : "false");
    } catch (_) {
      // The layout still changes when storage is disabled by the browser.
    }
  }
  window.requestAnimationFrame(updateTitleOverflow);
}

function loadWidthPreference() {
  let enabled = false;
  try {
    enabled = window.localStorage.getItem(LAYOUT_STORAGE_KEY) === "true";
  } catch (_) {
    // Use the reading width when storage is disabled by the browser.
  }
  setFitWidth(enabled);
}

function selectedConversationId() {
  const value = new URLSearchParams(window.location.search).get("conversation");
  return value && /^\d+$/.test(value) ? Number(value) : null;
}

function updateUrl(conversationId = state.activeConversation?.summary.id || null) {
  const params = new URLSearchParams();
  if (elements.location.value) params.set("location", elements.location.value);
  if (elements.search.value.trim()) params.set("q", elements.search.value.trim());
  if (elements.order.value !== "newest") params.set("order", elements.order.value);
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
  const requested = elements.location.value || params.get("location") || "";
  const allLocations = node("option", "", "All locations");
  allLocations.value = "";
  elements.location.replaceChildren(allLocations);
  for (const location of state.locations) {
    const option = node("option", "", `${location.display_name} · ${location.hostname} · ${location.provider} (${location.conversation_count})`);
    option.value = String(location.id);
    option.title = location.root_path;
    elements.location.append(option);
  }
  if ([...elements.location.options].some((option) => option.value === requested)) {
    elements.location.value = requested;
  }
  if (!elements.search.dataset.hydrated) {
    elements.search.value = params.get("q") || "";
    elements.search.dataset.hydrated = "true";
  }
  if (!elements.order.dataset.hydrated) {
    const requestedOrder = params.get("order") || "newest";
    if ([...elements.order.options].some((option) => option.value === requestedOrder)) {
      elements.order.value = requestedOrder;
    }
    elements.order.dataset.hydrated = "true";
  }
  elements.clearSearch.classList.toggle("hidden", !elements.search.value);
  const locationCount = state.locations.length;
  const conversationCount = state.locations.reduce(
    (total, location) => total + location.conversation_count,
    0,
  );
  elements.archiveStatus.replaceChildren(
    node("span", "status-light"),
    document.createTextNode(
      `${conversationCount.toLocaleString()} sessions · ${locationCount} ${locationCount === 1 ? "location" : "locations"}`,
    ),
  );
}

async function loadConversations({ append = false, keepSelection = false } = {}) {
  const requestId = ++state.listRequest;
  state.listController?.abort();
  const controller = new AbortController();
  state.listController = controller;
  if (!append) {
    state.conversationLoading = true;
    state.conversationRequest += 1;
    state.conversationController?.abort();
    cancelEventPagination();
    elements.sessionList.replaceChildren(node("div", "loading", "Loading sessions…"));
  }
  elements.sessionList.setAttribute("aria-busy", "true");
  elements.loadMore.disabled = true;
  elements.loadMore.textContent = append ? "Loading more sessions…" : "Load more sessions";
  const offset = append ? state.conversations.length : 0;
  const params = new URLSearchParams({
    limit: String(SESSION_PAGE_SIZE + 1),
    offset: String(offset),
  });
  if (elements.location.value) params.set("location", elements.location.value);
  if (elements.search.value.trim()) params.set("search", elements.search.value.trim());
  params.set("order", elements.order.value);
  let response;
  try {
    response = await request(`/api/conversations?${params}`, { signal: controller.signal });
  } catch (error) {
    if (error.name === "AbortError") return;
    if (requestId !== state.listRequest) return;
    elements.sessionList.setAttribute("aria-busy", "false");
    elements.loadMore.disabled = false;
    elements.loadMore.textContent = "Load more sessions";
    if (!append) {
      state.conversationLoading = false;
      updateConversationBottom();
    }
    throw error;
  }
  if (requestId !== state.listRequest) return;

  state.hasMoreConversations = response.length > SESSION_PAGE_SIZE;
  const page = response.slice(0, SESSION_PAGE_SIZE);
  state.conversations = append ? [...state.conversations, ...page] : page;
  elements.sessionList.setAttribute("aria-busy", "false");
  elements.sessionCount.textContent = `${state.conversations.length}${state.hasMoreConversations ? "+" : ""}`;
  elements.sessionCount.title = state.hasMoreConversations
    ? `${state.conversations.length} sessions loaded; more are available`
    : `${state.conversations.length} sessions`;
  elements.loadMore.classList.toggle("hidden", !state.hasMoreConversations);
  elements.loadMore.disabled = false;
  elements.loadMore.textContent = "Load more sessions";
  renderConversationList();

  if (append) {
    elements.footerStatus.textContent = `${state.conversations.length} sessions loaded`;
    return;
  }
  const requestedId = keepSelection ? state.activeConversation?.summary.id : selectedConversationId();
  const available = state.conversations.some((item) => item.id === requestedId);
  if (requestedId && !available && !keepSelection) {
    await openConversation(requestedId);
    if (state.activeConversation?.summary.id === requestedId) return;
  }
  if (keepSelection && available && state.activeConversation?.summary.id === requestedId) {
    state.conversationLoading = false;
    updateConversationBottom();
    elements.footerStatus.textContent = `${state.conversations.length} sessions loaded`;
    updateUrl(requestedId);
    return;
  }
  const nextId = available ? requestedId : state.conversations[0]?.id;
  if (nextId) {
    await openConversation(nextId);
  } else {
    state.conversationLoading = false;
    updateConversationBottom();
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
    const active = conversation.id === state.activeConversation?.summary.id;
    card.classList.toggle("active", active);
    if (active) card.setAttribute("aria-current", "true");
    card.addEventListener("click", () => openConversation(conversation.id));

    const top = node("div", "session-card-top");
    top.append(
      node("span", `provider-badge ${conversation.provider}`, conversation.provider),
      node("span", "session-time", formatDate(conversation.ended_at || conversation.started_at)),
    );
    const title = oneLine(conversation.title || conversation.preview || conversation.external_id);
    const titleNode = node("div", "session-title", title);
    titleNode.title = title;
    card.append(
      top,
      titleNode,
      node("div", "session-preview", oneLine(conversation.preview, "No visible user message")),
      node("div", "session-meta", `${conversation.hostname} · ${conversation.message_count} messages · ${conversation.event_count} events`),
    );
    elements.sessionList.append(card);
  }
  if (state.hasMoreConversations) {
    elements.sessionList.append(elements.loadMore);
    sessionLoaderObserver?.observe(elements.loadMore);
  }
}

async function openConversation(id) {
  if (!id) return;
  state.conversationLoading = true;
  const requestId = ++state.conversationRequest;
  state.conversationController?.abort();
  cancelEventPagination();
  const controller = new AbortController();
  state.conversationController = controller;
  elements.footerStatus.textContent = "Loading transcript…";
  elements.conversation.setAttribute("aria-busy", "true");
  try {
    const params = new URLSearchParams({ event_limit: String(EVENT_PAGE_SIZE) });
    const detail = await request(`/api/conversations/${id}?${params}`, {
      signal: controller.signal,
    });
    if (requestId !== state.conversationRequest) return;
    // A queued observer may have started pagination for the old session while this request ran.
    cancelEventPagination();
    state.activeConversation = detail;
    state.detailsExpanded = false;
    state.humanCursorSequence = null;
    state.transcriptQuery = "";
    elements.transcriptSearch.value = "";
    elements.clearTranscriptSearch.classList.add("hidden");
    renderConversation();
    updateUrl(id);
    document.querySelectorAll(".session-card").forEach((card) => {
      const active = Number(card.dataset.id) === id;
      card.classList.toggle("active", active);
      card.toggleAttribute("aria-current", active);
    });
    setSidebar(false);
    state.conversationLoading = false;
    updateConversationBottom();
    elements.conversation.setAttribute("aria-busy", "false");
  } catch (error) {
    if (error.name === "AbortError") return;
    if (requestId !== state.conversationRequest) return;
    state.conversationLoading = false;
    cancelEventPagination();
    showToast(`Could not load session: ${error.message}`);
    elements.footerStatus.textContent = "Load failed";
    elements.conversation.setAttribute("aria-busy", "false");
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
  const title = oneLine(summary.title || summary.preview || summary.external_id);
  elements.title.textContent = title;
  elements.titleTooltip.textContent = title;
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
  state.conversationEntries = conversationItems();
  updateDetailsButton();
  renderEvents();
  elements.content.scrollTo({ top: 0 });
  window.requestAnimationFrame(updateTitleOverflow);
}

function updateDetailsButton() {
  elements.toggleDetails.setAttribute("aria-pressed", String(state.detailsExpanded));
  elements.detailLabel.textContent = state.detailsExpanded ? "Conversation" : "Raw events";
  document.querySelector("#footer-details").lastChild.textContent = state.detailsExpanded
    ? " Conversation"
    : " Raw events";
}

function parseJson(raw) {
  try {
    return JSON.parse(raw);
  } catch (_) {
    return null;
  }
}

function isInjectedClaudeContext(event) {
  if (
    state.activeConversation?.summary.provider !== "claude"
    || event.event_type !== "user"
  ) return false;
  if (event.injectedClaudeContext !== undefined) return event.injectedClaudeContext;

  const record = parseJson(event.raw_json);
  const content = record?.message?.content;
  const blocks = Array.isArray(content) ? content : [content];
  const onlyText = blocks.length > 0 && blocks.every((block) => (
    typeof block === "string"
    || (block && block.type === "text" && typeof block.text === "string")
  ));
  const text = onlyText
    ? blocks.map((block) => typeof block === "string" ? block : block.text).join("\n").trimStart()
    : "";
  const markedContext = text.startsWith("Base directory for this skill:")
    || text.startsWith("[SYSTEM NOTIFICATION - NOT USER INPUT]");
  event.injectedClaudeContext = Boolean(
    onlyText
    && (record?.isMeta === true
      || (markedContext && (record?.isSidechain === true || record?.sourceToolUseID))),
  );
  return event.injectedClaudeContext;
}

function isToolType(type = "") {
  return type.includes("tool")
    || type.endsWith("_call")
    || type.endsWith("_call_output")
    || type === "mcp_approval_request"
    || type === "mcp_approval_response";
}

function isReasoningType(type = "") {
  return ["reasoning", "summary_text", "thinking", "redacted_thinking"].includes(type);
}

function isTextType(type = "") {
  return ["text", "input_text", "output_text"].includes(type);
}

function partRole(event, part) {
  if (isToolType(part.content_type)) return "tool";
  if (isReasoningType(part.content_type)) return "reasoning";
  return eventRole(event);
}

function toolPayload(event, part) {
  const source = parseJson(part?.raw_json || event.raw_json) || {};
  if (source.payload && typeof source.payload === "object") return source.payload;
  return source;
}

function prettyValue(value) {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value !== "string") return JSON.stringify(value, null, 2);
  const parsed = parseJson(value);
  return parsed === null ? value : JSON.stringify(parsed, null, 2);
}

function firstDefined(value, keys) {
  for (const key of keys) {
    if (value[key] !== undefined && value[key] !== null) return value[key];
  }
  return undefined;
}

function describeTool(event, part) {
  const value = toolPayload(event, part);
  const type = part?.content_type || value.type || event.event_subtype || "tool";
  const resultItem = type.includes("result")
    || type.includes("output")
    || type.endsWith("_response");
  const callId = value.tool_use_id || value.call_id || value.id || event.external_id || "";
  const name = value.name || value.tool_name || value.server_name || "";
  const status = typeof value.status === "string" ? value.status.toLowerCase() : "";
  const failure = ["failed", "cancelled", "canceled", "incomplete"].includes(status);
  const error = Boolean(value.is_error || value.error || failure);
  const terminal = resultItem || error || ["completed", "succeeded"].includes(status);
  const embeddedOutput = resultItem
    ? undefined
    : firstDefined(value, ["output", "result", "results", "outputs", "tools"]);
  const input = firstDefined(
    value,
    ["input", "arguments", "action", "query", "queries", "code", "revised_prompt"],
  );
  const body = resultItem
    ? firstDefined(value, ["content", "output", "result", "results", "outputs", "tools"])
      ?? part?.text
      ?? event.text
    : input ?? (embeddedOutput === undefined ? part?.text : undefined);
  return {
    body: prettyValue(body),
    callId,
    embeddedOutput: prettyValue(embeddedOutput),
    error,
    hasEmbeddedOutput: embeddedOutput !== undefined,
    kind: resultItem ? "result" : "call",
    name,
    pending: ["in_progress", "queued", "running"].includes(status),
    status,
    terminal,
    type,
  };
}

function eventRole(event) {
  if (event.role) return event.role;
  if (isToolType(event.event_subtype || "") || event.event_type.includes("tool")) return "tool";
  return "metadata";
}

function appendToolItem(items, pendingTools, event, part) {
  const tool = describeTool(event, part);
  if (tool.kind === "call") {
    const embeddedResult = tool.hasEmbeddedOutput
      ? { ...tool, body: tool.embeddedOutput, kind: "result", type: "embedded result" }
      : null;
    const entry = {
      event,
      events: [event],
      part,
      role: "tool",
      text: tool.body,
      tool: {
        call: tool,
        complete: tool.terminal || (tool.hasEmbeddedOutput && !tool.pending),
        error: tool.error,
        name: tool.name,
        result: embeddedResult,
        status: tool.status,
      },
    };
    items.push(entry);
    if (tool.callId) pendingTools.set(tool.callId, entry);
    return;
  }

  const callEntry = tool.callId ? pendingTools.get(tool.callId) : null;
  if (callEntry) {
    callEntry.tool.result = tool;
    callEntry.tool.complete = true;
    callEntry.tool.error ||= tool.error;
    callEntry.tool.name ||= tool.name;
    callEntry.tool.status = tool.status || callEntry.tool.status;
    if (!callEntry.events.includes(event)) callEntry.events.push(event);
    pendingTools.delete(tool.callId);
    return;
  }
  items.push({
    event,
    events: [event],
    part,
    role: "tool",
    text: tool.body,
    tool: {
      call: null,
      complete: true,
      error: tool.error,
      name: tool.name,
      result: tool,
      status: tool.status,
    },
  });
}

function conversationItems() {
  const items = [];
  const pendingTools = new Map();
  state.hiddenContextCount = 0;
  for (const event of state.activeConversation.events) {
    if (isInjectedClaudeContext(event)) {
      state.hiddenContextCount += 1;
      continue;
    }
    let added = false;
    for (const part of event.parts || []) {
      const role = partRole(event, part);
      if (role === "tool") {
        appendToolItem(items, pendingTools, event, part);
        added = true;
      } else if (role === "reasoning" && part.text) {
        items.push({ event, events: [event], part, role, text: part.text, tool: null });
        added = true;
      } else if (
        event.visibility === "display"
        && ["user", "assistant", "system"].includes(role)
        && isTextType(part.content_type)
        && part.text
      ) {
        items.push({ event, events: [event], part, role, text: part.text, tool: null });
        added = true;
      }
    }

    if (!added && isToolType(event.event_subtype || "")) {
      appendToolItem(items, pendingTools, event, null);
    } else if (!added && event.visibility === "display" && event.text) {
      const role = eventRole(event);
      items.push({ event, events: [event], part: null, role, text: event.text, tool: null });
    } else if (!added && eventRole(event) === "reasoning" && event.text) {
      items.push({
        event,
        events: [event],
        part: null,
        role: "reasoning",
        text: event.text,
        tool: null,
      });
    }
  }
  return items;
}

function itemsForFilter(items, filter) {
  if (filter === "chat") {
    return items.filter((item) => ["user", "assistant", "system"].includes(item.role));
  }
  if (filter === "tools") return items.filter((item) => item.role === "tool");
  if (filter === "reasoning") {
    return items.filter((item) => item.role === "reasoning");
  }
  return items.filter((item) => item.role !== "reasoning");
}

function itemSearchText(entry) {
  if (entry.searchText !== undefined) return entry.searchText;
  const event = entry.event;
  const values = [
    entry.text,
    entry.role,
    entry.tool?.name,
    entry.tool?.call?.body,
    entry.tool?.call?.type,
    entry.tool?.result?.body,
    entry.tool?.result?.type,
    event?.text,
    event?.event_type,
    event?.event_subtype,
  ];
  entry.searchText = values.filter(Boolean).join("\n").toLowerCase();
  return entry.searchText;
}

function matchesTranscriptQuery(entry) {
  const query = state.transcriptQuery.trim().toLowerCase();
  return !query || itemSearchText(entry).includes(query);
}

function eventMarker(role) {
  return { user: "›", assistant: "◆", tool: "⚙", reasoning: "∿", system: "•", metadata: "·" }[role] || "·";
}

function appendInlineMarkdown(parent, text) {
  const pattern = /(`[^`\n]+`|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*)/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > cursor) parent.append(document.createTextNode(text.slice(cursor, match.index)));
    if (match[0].startsWith("`")) {
      parent.append(node("code", "inline-code", match[0].slice(1, -1)));
    } else if (match[2] && match[3]) {
      const link = node("a", "", match[2]);
      link.href = match[3];
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      parent.append(link);
    } else if (match[4]) {
      parent.append(node("strong", "", match[4]));
    } else if (match[5]) {
      parent.append(node("em", "", match[5]));
    }
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
}

function copyButton(value) {
  const button = node("button", "copy-button", "copy");
  button.type = "button";
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(value);
      button.textContent = "copied";
      window.setTimeout(() => { button.textContent = "copy"; }, 1400);
    } catch (_) {
      showToast("Clipboard access is unavailable in this browser.");
    }
  });
  return button;
}

function startsMarkdownBlock(line) {
  return /^(```|#{1,6}\s|>\s?|[-*+]\s+|\d+\.\s+| {0,3}([-*_])(?:\s*\2){2,}\s*$)/.test(line);
}

function markdownTableCells(line) {
  let value = line.trim();
  if (value.startsWith("|")) value = value.slice(1);
  if (value.endsWith("|")) value = value.slice(0, -1);
  const cells = [];
  let cell = "";
  let code = false;
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index];
    if (character === "`" && value[index - 1] !== "\\") code = !code;
    if (character === "|" && value[index - 1] !== "\\" && !code) {
      cells.push(cell.trim());
      cell = "";
    } else if (character === "|" && value[index - 1] === "\\") {
      cell = `${cell.slice(0, -1)}|`;
    } else {
      cell += character;
    }
  }
  cells.push(cell.trim());
  return cells;
}

function markdownTableAlignment(value) {
  const cell = value.trim();
  if (!/^:?-{3,}:?$/.test(cell)) return null;
  if (cell.startsWith(":") && cell.endsWith(":")) return "center";
  if (cell.endsWith(":")) return "right";
  return "left";
}

function markdownTableStart(lines, index) {
  if (index + 1 >= lines.length || !lines[index].includes("|")) return false;
  const headings = markdownTableCells(lines[index]);
  const dividers = markdownTableCells(lines[index + 1]);
  return headings.length > 1
    && headings.length === dividers.length
    && dividers.every((cell) => markdownTableAlignment(cell));
}

function renderMarkdownTable(lines, start) {
  const headings = markdownTableCells(lines[start]);
  const alignments = markdownTableCells(lines[start + 1]).map(markdownTableAlignment);
  const wrapper = node("div", "markdown-table-wrap");
  const table = node("table", "markdown-table");
  const head = node("thead");
  const headingRow = node("tr");
  for (const [index, value] of headings.entries()) {
    const heading = node("th", `align-${alignments[index]}`);
    appendInlineMarkdown(heading, value);
    headingRow.append(heading);
  }
  head.append(headingRow);
  table.append(head);

  const body = node("tbody");
  let index = start + 2;
  while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
    const values = markdownTableCells(lines[index]);
    const row = node("tr");
    for (let cellIndex = 0; cellIndex < headings.length; cellIndex += 1) {
      const cell = node("td", `align-${alignments[cellIndex]}`);
      appendInlineMarkdown(cell, values[cellIndex] || "");
      row.append(cell);
    }
    body.append(row);
    index += 1;
  }
  table.append(body);
  wrapper.append(table);
  return { element: wrapper, nextIndex: index };
}

function renderMarkdown(value) {
  const root = node("div", "message-text markdown");
  const lines = value.replace(/\r\n?/g, "\n").split("\n");
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    if (markdownTableStart(lines, index)) {
      const table = renderMarkdownTable(lines, index);
      root.append(table.element);
      index = table.nextIndex;
      continue;
    }

    const fence = line.match(/^```\s*([\w.+-]*)\s*$/);
    if (fence) {
      const codeLines = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const code = codeLines.join("\n");
      const block = node("div", "code-block");
      const heading = node("div", "code-heading");
      heading.append(node("span", "", fence[1] || "code"), copyButton(code));
      block.append(heading, node("pre", "", code));
      root.append(block);
      continue;
    }

    const headingMatch = line.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      const heading = node(`h${headingMatch[1].length}`);
      appendInlineMarkdown(heading, headingMatch[2]);
      root.append(heading);
      index += 1;
      continue;
    }

    if (/^>\s?/.test(line)) {
      const quote = node("blockquote");
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        const quoteLine = node("div");
        appendInlineMarkdown(quoteLine, lines[index].replace(/^>\s?/, ""));
        quote.append(quoteLine);
        index += 1;
      }
      root.append(quote);
      continue;
    }

    const listMatch = line.match(/^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/);
    if (listMatch) {
      const ordered = Boolean(listMatch[2]);
      const list = node(ordered ? "ol" : "ul");
      while (index < lines.length) {
        const itemMatch = lines[index].match(/^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/);
        if (!itemMatch || Boolean(itemMatch[2]) !== ordered) break;
        const item = node("li");
        appendInlineMarkdown(item, itemMatch[3]);
        list.append(item);
        index += 1;
      }
      root.append(list);
      continue;
    }

    if (/^ {0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      root.append(node("hr"));
      index += 1;
      continue;
    }

    const paragraphLines = [line];
    index += 1;
    while (
      index < lines.length
      && lines[index].trim()
      && !startsMarkdownBlock(lines[index])
      && !markdownTableStart(lines, index)
    ) {
      paragraphLines.push(lines[index]);
      index += 1;
    }
    const paragraph = node("p");
    for (const [lineIndex, paragraphLine] of paragraphLines.entries()) {
      appendInlineMarkdown(paragraph, paragraphLine);
      if (lineIndex < paragraphLines.length - 1) paragraph.append(node("br"));
    }
    root.append(paragraph);
  }
  return root;
}

function toolDuration(entry) {
  if (!entry.tool.call || !entry.tool.result || entry.events.length < 2) return "";
  if (!entry.events[0].occurred_at || !entry.events.at(-1).occurred_at) return "";
  const started = new Date(entry.events[0].occurred_at).valueOf();
  const ended = new Date(entry.events.at(-1).occurred_at).valueOf();
  if (!Number.isFinite(started) || !Number.isFinite(ended) || ended < started) return "";
  const milliseconds = ended - started;
  return milliseconds < 1000 ? `${milliseconds} ms` : `${(milliseconds / 1000).toFixed(1)} s`;
}

function toolSection(label, value) {
  const section = node("div", "tool-section");
  const heading = node("div", "tool-section-heading", label);
  if (value) heading.append(copyButton(value));
  section.append(heading);
  if (value) section.append(node("pre", "tool-payload", value));
  else section.append(node("div", "tool-empty", "No textual payload"));
  return section;
}

function renderToolActivity(entry) {
  const card = node("div", "tool-card");
  if (entry.tool.call) card.append(toolSection("INPUT", entry.tool.call.body));
  if (entry.tool.result) {
    if ((entry.tool.result.body || "").length > 900) {
      const disclosure = node("details", "tool-output-disclosure");
      disclosure.append(node(
        "summary",
        "",
        `Output · ${entry.tool.result.body.length.toLocaleString()} characters`,
      ));
      disclosure.addEventListener(
        "toggle",
        () => {
          if (disclosure.open && disclosure.children.length === 1) {
            disclosure.append(toolSection("OUTPUT", entry.tool.result.body));
          }
        },
        { once: true },
      );
      card.append(disclosure);
    } else {
      card.append(toolSection("OUTPUT", entry.tool.result.body));
    }
  } else if (!entry.tool.complete) {
    card.append(node("div", "tool-pending", "Awaiting result"));
  } else {
    card.append(node("div", "tool-finished", "Completed without textual output"));
  }
  return card;
}

function appendTranscriptLoader() {
  const summary = state.activeConversation.summary;
  const loaded = state.activeConversation.events.length;
  const remaining = Math.max(0, summary.event_count - loaded);
  if (!remaining) return;
  const count = Math.min(EVENT_PAGE_SIZE, remaining);
  const button = node(
    "button",
    "transcript-load-more",
    state.eventLoading
      ? "Loading more events…"
      : `Load ${count} more events · ${remaining.toLocaleString()} remaining`,
  );
  button.type = "button";
  button.disabled = state.eventLoading;
  button.addEventListener("click", loadMoreEvents);
  elements.transcript.append(button);
  transcriptLoaderObserver?.disconnect();
  transcriptLoaderObserver?.observe(button);
}

function appendHiddenContextNotice() {
  if (state.detailsExpanded || !state.hiddenContextCount) return;
  const count = state.hiddenContextCount;
  const notice = node("div", "context-notice");
  notice.append(node(
    "span",
    "",
    `${count} Claude skill/context ${count === 1 ? "record" : "records"} hidden from conversation`,
  ));
  const button = node("button", "", "View raw events");
  button.type = "button";
  button.addEventListener("click", toggleAllDetails);
  notice.append(button);
  elements.transcript.append(notice);
}

function renderEvents() {
  elements.transcript.replaceChildren();
  elements.transcript.classList.toggle("raw-mode", state.detailsExpanded);
  const conversationEntries = state.conversationEntries;
  for (const button of elements.filterButtons) {
    const filter = button.dataset.transcriptFilter;
    button.setAttribute("aria-pressed", String(filter === state.transcriptFilter));
    button.disabled = state.detailsExpanded;
    const count = itemsForFilter(conversationEntries, filter).length;
    button.querySelector(".filter-count").textContent = String(count);
    button.setAttribute(
      "aria-label",
      `${button.firstElementChild.textContent}: ${count} loaded events`,
    );
  }
  const visibleEntries = state.detailsExpanded
    ? state.activeConversation.events.map((event) => ({ event, events: [event], raw: true }))
    : itemsForFilter(conversationEntries, state.transcriptFilter);
  const entries = visibleEntries.filter(matchesTranscriptQuery);
  const hasQuery = Boolean(state.transcriptQuery.trim());
  elements.clearTranscriptSearch.classList.toggle("hidden", !hasQuery);
  elements.transcriptMatchCount.textContent = hasQuery ? String(entries.length) : "";
  elements.transcriptSearch.setAttribute(
    "aria-label",
    hasQuery ? `Find in loaded events; ${entries.length} matches` : "Find in loaded events",
  );
  appendHiddenContextNotice();
  if (!entries.length) {
    elements.transcript.append(node(
      "div",
      "no-results transcript-empty",
      hasQuery ? `No events contain “${state.transcriptQuery.trim()}”.` : "No events match this view.",
    ));
    appendTranscriptLoader();
    const loaded = state.activeConversation.events.length;
    const total = state.activeConversation.summary.event_count;
    const hidden = state.hiddenContextCount ? ` · ${state.hiddenContextCount} context hidden` : "";
    elements.footerStatus.textContent = `0 visible · ${loaded}/${total} events loaded${hidden}`;
    return;
  }
  for (const entry of entries) elements.transcript.append(renderEvent(entry));
  appendTranscriptLoader();
  const matchStatus = hasQuery ? ` · ${entries.length}/${visibleEntries.length} matches` : "";
  const loaded = state.activeConversation.events.length;
  const total = state.activeConversation.summary.event_count;
  const hidden = state.hiddenContextCount ? ` · ${state.hiddenContextCount} context hidden` : "";
  elements.footerStatus.textContent = `${entries.length} visible · ${loaded}/${total} events loaded${matchStatus}${hidden}`;
}

function renderEvent(entry) {
  const event = entry.event;
  const role = entry.raw ? eventRole(event) : entry.role;
  const toolState = entry.tool
    ? (entry.tool.error ? "failed" : (entry.tool.complete ? "complete" : "pending"))
    : "";
  const toolClass = entry.tool ? ` tool-${toolState}` : "";
  const searchClass = state.transcriptQuery.trim() ? " search-match" : "";
  const wrapper = node("section", `event ${role}${toolClass}${searchClass}`);
  wrapper.tabIndex = -1;
  wrapper.dataset.sequence = String(event.sequence);
  wrapper.append(node("span", "event-marker", eventMarker(role)));

  const heading = node("div", "event-heading");
  let roleLabel = role === "user" ? "You" : role;
  if (entry.tool) {
    roleLabel = "Tool";
    if (entry.tool.name) roleLabel += ` · ${entry.tool.name}`;
  }
  heading.append(
    node("span", "event-role", roleLabel),
    node("span", "event-time", event.occurred_at ? formatDate(event.occurred_at, true) : ""),
  );
  const toolTypes = entry.tool
    ? [entry.tool.call?.type, entry.tool.result?.type].filter(Boolean).join(" → ")
    : "";
  const type = toolTypes || [event.event_type, event.event_subtype].filter(Boolean).join(" / ");
  const eventType = node("span", "event-type", type);
  eventType.title = type;
  heading.append(eventType);
  if (entry.tool) {
    const duration = toolDuration(entry);
    const nativeStatus = entry.tool.status;
    const statusText = toolState === "failed"
      ? `× ${nativeStatus || "failed"}`
      : (toolState === "complete" ? `✓ ${duration || nativeStatus || "done"}` : "… running");
    heading.append(node("span", `tool-status ${toolState}`, statusText));
  }
  const toggle = node("button", "detail-toggle", entry.raw ? "hide" : "raw");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", String(Boolean(entry.raw)));
  toggle.setAttribute("aria-label", `Toggle raw detail for event ${event.sequence}`);
  heading.append(toggle);
  wrapper.append(heading);

  const text = entry.raw ? event.text : entry.text;
  if (entry.tool) {
    wrapper.append(renderToolActivity(entry));
  } else if (text) {
    wrapper.append(entry.raw ? node("div", "message-text", text) : renderMarkdown(text));
  } else {
    wrapper.append(node("div", "metadata-event", `${event.visibility} event · no normalized text`));
  }

  const details = node("div", "entry-details");
  let detailsRendered = false;
  const ensureDetails = () => {
    if (detailsRendered) return;
    const seenSequences = new Set();
    for (const detailEvent of entry.events || [event]) {
      if (!seenSequences.has(detailEvent.sequence)) {
        details.append(renderEventDetails(detailEvent));
      }
      seenSequences.add(detailEvent.sequence);
    }
    detailsRendered = true;
  };
  if (entry.raw) ensureDetails();
  details.classList.toggle("hidden", !entry.raw);
  wrapper.append(details);
  toggle.addEventListener("click", () => {
    const expanded = toggle.getAttribute("aria-expanded") !== "true";
    if (expanded) ensureDetails();
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.textContent = expanded ? "hide" : "raw";
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

function setSidebar(open) {
  elements.sidebar.classList.toggle("open", open);
  elements.sidebarScrim.classList.toggle("hidden", !open);
  elements.sessionsButton.setAttribute("aria-expanded", String(open));
  elements.sessionsButton.setAttribute("aria-label", open ? "Close sessions" : "Open sessions");
}

function toggleSidebar() {
  setSidebar(!elements.sidebar.classList.contains("open"));
}

function setTranscriptFilter(filter) {
  if (!state.activeConversation) return;
  state.transcriptFilter = filter;
  state.detailsExpanded = false;
  updateDetailsButton();
  renderEvents();
  elements.content.scrollTo({ top: 0, behavior: "smooth" });
}

function moveEventFocus(delta) {
  const events = [...elements.transcript.querySelectorAll(".event")];
  if (!events.length) return;
  const active = document.activeElement?.closest?.(".event");
  const current = events.indexOf(active);
  const next = current < 0
    ? (delta > 0 ? 0 : events.length - 1)
    : Math.min(events.length - 1, Math.max(0, current + delta));
  events[next].focus({ preventScroll: true });
  events[next].scrollIntoView({ behavior: "smooth", block: "center" });
}

function moveSession(delta) {
  const activeId = state.activeConversation?.summary.id;
  const current = state.conversations.findIndex((conversation) => conversation.id === activeId);
  if (current < 0 || !state.conversations.length) return;
  const next = Math.min(state.conversations.length - 1, Math.max(0, current + delta));
  if (next !== current) openConversation(state.conversations[next].id);
}

function humanMessageElements() {
  const seen = new Set();
  return [...elements.transcript.querySelectorAll(".event.user")].filter((event) => {
    if (seen.has(event.dataset.sequence)) return false;
    seen.add(event.dataset.sequence);
    return true;
  });
}

function humanTargetIndex(messages, delta) {
  const cursorIndex = messages.findIndex(
    (message) => Number(message.dataset.sequence) === state.humanCursorSequence,
  );
  const navigationTop = document.querySelector(".conversation-header").getBoundingClientRect().bottom;
  const navigationBottom = elements.content.getBoundingClientRect().bottom;
  if (cursorIndex >= 0) {
    const cursorBounds = messages[cursorIndex].getBoundingClientRect();
    if (cursorBounds.bottom > navigationTop && cursorBounds.top < navigationBottom) {
      return cursorIndex + delta;
    }
  }
  state.humanCursorSequence = null;
  if (delta > 0) {
    const next = messages.findIndex(
      (message) => message.getBoundingClientRect().top >= navigationTop - 1,
    );
    return next >= 0 ? next : messages.length;
  }
  return messages.findLastIndex(
    (message) => message.getBoundingClientRect().top < navigationTop - 1,
  );
}

async function moveHumanMessage(delta) {
  if (!state.activeConversation) return;
  let viewChanged = false;
  if (!["all", "chat"].includes(state.transcriptFilter) || state.detailsExpanded) {
    state.transcriptFilter = "chat";
    state.detailsExpanded = false;
    updateDetailsButton();
    viewChanged = true;
  }
  if (state.transcriptQuery) {
    state.transcriptQuery = "";
    elements.transcriptSearch.value = "";
    viewChanged = true;
  }
  if (viewChanged) renderEvents();

  let messages = humanMessageElements();
  let targetIndex = humanTargetIndex(messages, delta);
  while (
    delta > 0
    && targetIndex >= messages.length
    && state.activeConversation.events.length < state.activeConversation.summary.event_count
  ) {
    const loaded = state.activeConversation.events.length;
    await loadMoreEvents();
    if (state.activeConversation.events.length === loaded) break;
    messages = humanMessageElements();
    targetIndex = humanTargetIndex(messages, delta);
  }

  const target = messages[targetIndex];
  if (!target) {
    elements.footerStatus.textContent = delta > 0
      ? "Last loaded human message"
      : "First human message";
    return;
  }
  state.humanCursorSequence = Number(target.dataset.sequence);
  target.focus({ preventScroll: true });
  target.scrollIntoView({ behavior: "smooth", block: "center" });
}

function isTypingTarget(target) {
  return target instanceof HTMLInputElement
    || target instanceof HTMLSelectElement
    || target instanceof HTMLTextAreaElement
    || target?.isContentEditable;
}

async function loadMoreEvents() {
  const detail = state.activeConversation;
  if (!detail || state.eventLoading) return;
  const loaded = detail.events.length;
  if (loaded >= detail.summary.event_count) return;

  const conversationId = detail.summary.id;
  const requestId = ++state.eventRequest;
  state.eventController?.abort();
  const controller = new AbortController();
  state.eventController = controller;
  state.eventLoading = true;
  updateConversationBottom();
  const loadButton = elements.transcript.querySelector(".transcript-load-more");
  if (loadButton) {
    transcriptLoaderObserver?.unobserve(loadButton);
    loadButton.disabled = true;
    loadButton.textContent = "Loading more events…";
  }

  let pageLoaded = false;
  try {
    const params = new URLSearchParams({
      event_limit: String(EVENT_PAGE_SIZE),
      event_offset: String(loaded),
    });
    const page = await request(`/api/conversations/${conversationId}?${params}`, {
      signal: controller.signal,
    });
    if (
      requestId !== state.eventRequest
      || state.activeConversation?.summary.id !== conversationId
    ) return;
    const seen = new Set(detail.events.map((event) => event.sequence));
    const newEvents = page.events.filter((event) => !seen.has(event.sequence));
    detail.events.push(...newEvents);
    if (!newEvents.length) detail.summary.event_count = detail.events.length;
    state.conversationEntries = conversationItems();
    pageLoaded = true;
  } catch (error) {
    if (error.name === "AbortError") return;
    showToast(`Could not load more events: ${error.message}`);
  } finally {
    if (
      requestId === state.eventRequest
      && state.activeConversation?.summary.id === conversationId
    ) {
      state.eventLoading = false;
      updateConversationBottom();
      if (pageLoaded) {
        renderEvents();
      } else if (loadButton?.isConnected) {
        const remaining = Math.max(0, detail.summary.event_count - detail.events.length);
        const count = Math.min(EVENT_PAGE_SIZE, remaining);
        loadButton.disabled = false;
        loadButton.textContent = `Load ${count} more events · ${remaining.toLocaleString()} remaining`;
      }
    }
  }
}

function scrollConversationTop() {
  state.humanCursorSequence = null;
  elements.content.scrollTo({ top: 0 });
}

async function scrollConversationBottom() {
  const detail = state.activeConversation;
  if (!detail || state.conversationLoading || state.eventLoading) return;

  const conversationId = detail.summary.id;
  state.humanCursorSequence = null;
  state.loadingAllEvents = true;
  updateConversationBottom();
  try {
    while (
      state.loadingAllEvents
      && state.activeConversation?.summary.id === conversationId
      && detail.events.length < detail.summary.event_count
    ) {
      const loaded = detail.events.length;
      await loadMoreEvents();
      if (detail.events.length === loaded) break;
    }
    if (
      state.activeConversation?.summary.id === conversationId
      && detail.events.length >= detail.summary.event_count
    ) {
      elements.content.scrollTo({ top: elements.content.scrollHeight });
    }
  } finally {
    state.loadingAllEvents = false;
    updateConversationBottom();
  }
}

async function copyConversationLink() {
  if (!state.activeConversation) return;
  updateUrl();
  try {
    await navigator.clipboard.writeText(window.location.href);
    const label = elements.copyLink.lastElementChild;
    label.textContent = "Copied";
    window.setTimeout(() => { label.textContent = "Copy link"; }, 1400);
  } catch (_) {
    showToast("Clipboard access is unavailable in this browser.");
  }
}

async function reloadArchive() {
  elements.reload.disabled = true;
  elements.reload.classList.add("spinning");
  elements.footerStatus.textContent = "Refreshing archive…";
  try {
    await loadLocations();
    await loadConversations({ keepSelection: true });
  } finally {
    elements.reload.disabled = false;
    elements.reload.classList.remove("spinning");
  }
}

elements.location.addEventListener("change", () => loadConversations({ keepSelection: true }).catch(handleError));
elements.order.addEventListener("change", () => loadConversations({ keepSelection: true }).catch(handleError));
elements.search.addEventListener("input", () => {
  window.clearTimeout(state.searchTimer);
  elements.clearSearch.classList.toggle("hidden", !elements.search.value);
  state.searchTimer = window.setTimeout(
    () => loadConversations({ keepSelection: true }).catch(handleError),
    250,
  );
});
elements.clearSearch.addEventListener("click", () => {
  elements.search.value = "";
  elements.clearSearch.classList.add("hidden");
  window.clearTimeout(state.searchTimer);
  loadConversations({ keepSelection: true }).catch(handleError);
  elements.search.focus();
});
elements.loadMore.addEventListener("click", () => loadConversations({ append: true }).catch(handleError));
elements.conversationBottom.addEventListener("click", scrollConversationBottom);
elements.conversationTop.addEventListener("click", scrollConversationTop);
elements.previousHuman.addEventListener("click", () => moveHumanMessage(-1));
elements.nextHuman.addEventListener("click", () => moveHumanMessage(1));
elements.transcriptSearch.addEventListener("input", () => {
  state.transcriptQuery = elements.transcriptSearch.value;
  renderEvents();
});
elements.clearTranscriptSearch.addEventListener("click", () => {
  state.transcriptQuery = "";
  elements.transcriptSearch.value = "";
  renderEvents();
  elements.transcriptSearch.focus();
});
for (const button of elements.filterButtons) {
  button.addEventListener("click", () => setTranscriptFilter(button.dataset.transcriptFilter));
}
elements.toggleDetails.addEventListener("click", toggleAllDetails);
elements.toggleWidth.addEventListener("click", () => setFitWidth(!state.fitWidth, { persist: true }));
elements.copyLink.addEventListener("click", copyConversationLink);
elements.reload.addEventListener("click", () => reloadArchive().catch(handleError));
document.querySelector("#footer-details").addEventListener("click", toggleAllDetails);
elements.sessionsButton.addEventListener("click", toggleSidebar);
document.querySelector("#footer-sessions").addEventListener("click", toggleSidebar);
elements.sidebarScrim.addEventListener("click", () => setSidebar(false));
document.querySelector("#scroll-top").addEventListener("click", scrollConversationTop);
window.addEventListener("resize", updateTitleOverflow);
if (typeof ResizeObserver !== "undefined") {
  new ResizeObserver(updateTitleOverflow).observe(elements.titleWrap);
}

document.addEventListener("keydown", (event) => {
  if (event.ctrlKey && event.key.toLowerCase() === "o") {
    event.preventDefault();
    toggleAllDetails();
  } else if (event.key === "/" && document.activeElement !== elements.search) {
    event.preventDefault();
    elements.search.focus();
  } else if (event.key === "Escape") {
    setSidebar(false);
    elements.search.blur();
    elements.transcriptSearch.blur();
  } else if (!isTypingTarget(event.target) && event.altKey && event.key === "ArrowUp") {
    event.preventDefault();
    moveHumanMessage(-1);
  } else if (!isTypingTarget(event.target) && event.altKey && event.key === "ArrowDown") {
    event.preventDefault();
    moveHumanMessage(1);
  } else if (!isTypingTarget(event.target) && ["1", "2", "3", "4"].includes(event.key)) {
    event.preventDefault();
    setTranscriptFilter({ 1: "all", 2: "chat", 3: "tools", 4: "reasoning" }[event.key]);
  } else if (!isTypingTarget(event.target) && event.key.toLowerCase() === "j") {
    event.preventDefault();
    moveEventFocus(1);
  } else if (!isTypingTarget(event.target) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    moveEventFocus(-1);
  } else if (!isTypingTarget(event.target) && event.key === "]") {
    event.preventDefault();
    moveSession(1);
  } else if (!isTypingTarget(event.target) && event.key === "[") {
    event.preventDefault();
    moveSession(-1);
  } else if (event.key === "Enter" && document.activeElement?.classList.contains("event")) {
    event.preventDefault();
    document.activeElement.querySelector(".detail-toggle")?.click();
  }
});

function handleError(error) {
  showToast(error.message);
  elements.footerStatus.textContent = "Request failed";
}

async function start() {
  loadWidthPreference();
  try {
    await loadLocations();
    await loadConversations();
  } catch (error) {
    handleError(error);
    elements.sessionList.replaceChildren(node("div", "no-results", "Could not load archive"));
  }
}

start();
