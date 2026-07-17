"use strict";

const SESSION_PAGE_SIZE = 50;

const state = {
  locations: [],
  conversations: [],
  activeConversation: null,
  detailsExpanded: false,
  hasMoreConversations: false,
  listRequest: 0,
  conversationRequest: 0,
  searchTimer: null,
  transcriptFilter: "all",
  transcriptQuery: "",
};

const elements = {
  content: document.querySelector("#content"),
  conversation: document.querySelector("#conversation"),
  copyLink: document.querySelector("#copy-link"),
  clearSearch: document.querySelector("#clear-search"),
  clearTranscriptSearch: document.querySelector("#clear-transcript-search"),
  empty: document.querySelector("#empty-state"),
  archiveStatus: document.querySelector("#archive-status"),
  location: document.querySelector("#location-select"),
  loadMore: document.querySelector("#load-more"),
  reload: document.querySelector("#reload-button"),
  search: document.querySelector("#search-input"),
  sessionList: document.querySelector("#session-list"),
  sessionCount: document.querySelector("#session-count"),
  sessionsButton: document.querySelector("#sessions-button"),
  sidebar: document.querySelector("#sidebar"),
  sidebarScrim: document.querySelector("#sidebar-scrim"),
  title: document.querySelector("#conversation-title"),
  subtitle: document.querySelector("#conversation-subtitle"),
  provider: document.querySelector("#conversation-provider"),
  metadata: document.querySelector("#metadata-strip"),
  transcript: document.querySelector("#transcript"),
  toggleDetails: document.querySelector("#toggle-details"),
  detailLabel: document.querySelector("#detail-button-label"),
  footerStatus: document.querySelector("#footer-status"),
  filterButtons: [...document.querySelectorAll("[data-transcript-filter]")],
  transcriptSearch: document.querySelector("#transcript-search"),
  transcriptMatchCount: document.querySelector("#transcript-match-count"),
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
  if (!append) {
    state.conversationRequest += 1;
    elements.sessionList.replaceChildren(node("div", "loading", "Loading sessions…"));
  }
  elements.sessionList.setAttribute("aria-busy", "true");
  elements.loadMore.disabled = true;
  const offset = append ? state.conversations.length : 0;
  const params = new URLSearchParams({
    limit: String(SESSION_PAGE_SIZE + 1),
    offset: String(offset),
  });
  if (elements.location.value) params.set("location", elements.location.value);
  if (elements.search.value.trim()) params.set("search", elements.search.value.trim());
  let response;
  try {
    response = await request(`/api/conversations?${params}`);
  } catch (error) {
    if (requestId !== state.listRequest) return;
    elements.sessionList.setAttribute("aria-busy", "false");
    elements.loadMore.disabled = false;
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
  const requestId = ++state.conversationRequest;
  elements.footerStatus.textContent = "Loading transcript…";
  elements.conversation.setAttribute("aria-busy", "true");
  try {
    const detail = await request(`/api/conversations/${id}`);
    if (requestId !== state.conversationRequest) return;
    state.activeConversation = detail;
    state.detailsExpanded = false;
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
    elements.conversation.setAttribute("aria-busy", "false");
  } catch (error) {
    if (requestId !== state.conversationRequest) return;
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
  elements.content.scrollTo({ top: 0 });
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
  for (const event of state.activeConversation.events) {
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
  if (entry.raw) values.push(event?.raw_json);
  return values.filter(Boolean).join("\n").toLocaleLowerCase();
}

function matchesTranscriptQuery(entry) {
  const query = state.transcriptQuery.trim().toLocaleLowerCase();
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

function renderMarkdown(value) {
  const root = node("div", "message-text markdown");
  const lines = value.replace(/\r\n?/g, "\n").split("\n");
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
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
    while (index < lines.length && lines[index].trim() && !startsMarkdownBlock(lines[index])) {
      paragraphLines.push(lines[index]);
      index += 1;
    }
    const paragraph = node("p");
    appendInlineMarkdown(paragraph, paragraphLines.join("\n"));
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
    const result = toolSection("OUTPUT", entry.tool.result.body);
    if ((entry.tool.result.body || "").length > 900) {
      const disclosure = node("details", "tool-output-disclosure");
      disclosure.append(
        node("summary", "", `Output · ${entry.tool.result.body.length.toLocaleString()} characters`),
        result,
      );
      card.append(disclosure);
    } else {
      card.append(result);
    }
  } else if (!entry.tool.complete) {
    card.append(node("div", "tool-pending", "Awaiting result"));
  } else {
    card.append(node("div", "tool-finished", "Completed without textual output"));
  }
  return card;
}

function renderEvents() {
  elements.transcript.replaceChildren();
  elements.transcript.classList.toggle("raw-mode", state.detailsExpanded);
  const conversationEntries = conversationItems();
  for (const button of elements.filterButtons) {
    const filter = button.dataset.transcriptFilter;
    button.setAttribute("aria-pressed", String(filter === state.transcriptFilter));
    button.disabled = state.detailsExpanded;
    const count = itemsForFilter(conversationEntries, filter).length;
    button.querySelector(".filter-count").textContent = String(count);
    button.setAttribute("aria-label", `${button.firstElementChild.textContent}: ${count} events`);
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
    hasQuery ? `Find in transcript; ${entries.length} matches` : "Find in transcript",
  );
  if (!entries.length) {
    elements.transcript.append(node(
      "div",
      "no-results transcript-empty",
      hasQuery ? `No events contain “${state.transcriptQuery.trim()}”.` : "No events match this view.",
    ));
    elements.footerStatus.textContent = `0 visible · ${state.detailsExpanded ? "raw" : state.transcriptFilter}`;
    return;
  }
  for (const entry of entries) elements.transcript.append(renderEvent(entry));
  const matchStatus = hasQuery ? ` · ${entries.length}/${visibleEntries.length} matches` : "";
  elements.footerStatus.textContent = `${entries.length} visible · ${state.detailsExpanded ? "raw" : state.transcriptFilter}${matchStatus}`;
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
  const seenSequences = new Set();
  for (const detailEvent of entry.events || [event]) {
    if (!seenSequences.has(detailEvent.sequence)) details.append(renderEventDetails(detailEvent));
    seenSequences.add(detailEvent.sequence);
  }
  details.classList.toggle("hidden", !entry.raw);
  wrapper.append(details);
  toggle.addEventListener("click", () => {
    const expanded = toggle.getAttribute("aria-expanded") !== "true";
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

function isTypingTarget(target) {
  return target instanceof HTMLInputElement
    || target instanceof HTMLSelectElement
    || target instanceof HTMLTextAreaElement
    || target?.isContentEditable;
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
elements.copyLink.addEventListener("click", copyConversationLink);
elements.reload.addEventListener("click", () => reloadArchive().catch(handleError));
document.querySelector("#footer-details").addEventListener("click", toggleAllDetails);
elements.sessionsButton.addEventListener("click", toggleSidebar);
document.querySelector("#footer-sessions").addEventListener("click", toggleSidebar);
elements.sidebarScrim.addEventListener("click", () => setSidebar(false));
document.querySelector("#scroll-top").addEventListener("click", () => elements.content.scrollTo({ top: 0 }));

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
  try {
    await loadLocations();
    await loadConversations();
  } catch (error) {
    handleError(error);
    elements.sessionList.replaceChildren(node("div", "no-results", "Could not load archive"));
  }
}

start();
