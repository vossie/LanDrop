const resolvedShareBaseUrl =
  (typeof configuredShareBaseUrl === "string" && configuredShareBaseUrl) ||
  (window.LANDROP_CONFIG && window.LANDROP_CONFIG.shareBaseUrl) ||
  "";
const sharedText = document.getElementById("sharedText");
const sharerName = document.getElementById("sharerName");
const textPanel = document.getElementById("textPanel");
const filePanel = document.getElementById("filePanel");
const textTabBtn = document.getElementById("textTabBtn");
const fileTabBtn = document.getElementById("fileTabBtn");
const hiddenText = document.getElementById("hiddenText");
const textHiddenOptions = document.getElementById("textHiddenOptions");
const textPassword = document.getElementById("textPassword");
const saveTextBtn = document.getElementById("saveTextBtn");
const fileInput = document.getElementById("fileInput");
const hiddenFile = document.getElementById("hiddenFile");
const fileHiddenOptions = document.getElementById("fileHiddenOptions");
const filePassword = document.getElementById("filePassword");
const uploadBtn = document.getElementById("uploadBtn");
const textMeta = document.getElementById("textMeta");
const textStatus = document.getElementById("textStatus");
const fileStatus = document.getElementById("fileStatus");
const fileList = document.getElementById("fileList");
const textHistory = document.getElementById("textHistory");
const dropZone = document.getElementById("dropZone");

let pendingTextPush = false;
let activeTab = "text";
const revealedTextIds = new Set();
const revealedTextContent = new Map();
const revealedTextHtml = new Map();
let snapshotInitialized = false;
let latestTextId = null;
let latestFileId = null;
let unreadText = false;
let unreadFiles = false;
let suppressedTextId = null;
let suppressedFileId = null;
let textStatusTimer = null;
let copiedTextId = null;
let copiedTextTimer = null;
let lastRenderedTexts = [];

function updateTabIndicators() {
  textTabBtn.classList.toggle("has-update", unreadText);
  fileTabBtn.classList.toggle("has-update", unreadFiles);
}

function syncTabs() {
  const showingText = activeTab === "text";
  textPanel.classList.toggle("hidden", !showingText);
  filePanel.classList.toggle("hidden", showingText);
  textTabBtn.classList.toggle("active", showingText);
  fileTabBtn.classList.toggle("active", !showingText);
  updateTabIndicators();
}

function setActiveTab(tabName) {
  activeTab = tabName;
  if (tabName === "text") {
    unreadText = false;
  } else {
    unreadFiles = false;
  }
  syncTabs();
}

function clearActiveTabIndicator() {
  if (activeTab === "text" && unreadText) {
    unreadText = false;
    updateTabIndicators();
  } else if (activeTab === "file" && unreadFiles) {
    unreadFiles = false;
    updateTabIndicators();
  }
}

function setTextStatus(message, fade = false) {
  if (textStatusTimer) {
    window.clearTimeout(textStatusTimer);
    textStatusTimer = null;
  }
  textStatus.classList.remove("fading");
  textStatus.textContent = message;

  if (!fade || !message) {
    return;
  }

  textStatusTimer = window.setTimeout(() => {
    textStatus.classList.add("fading");
    textStatusTimer = window.setTimeout(() => {
      textStatus.textContent = "";
      textStatus.classList.remove("fading");
      textStatusTimer = null;
    }, 260);
  }, 1100);
}

function formatDate(ts) {
  if (!ts) return "No content yet";
  return new Date(ts * 1000).toLocaleString();
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function lanSharePath(shortCode) {
  return `/s/${encodeURIComponent(shortCode)}`;
}

function lanShareUrl(shortCode) {
  const baseUrl = resolvedShareBaseUrl || window.location.origin;
  return `${baseUrl}${lanSharePath(shortCode)}`;
}

function withPassword(path, password) {
  return `${path}?password=${encodeURIComponent(password)}`;
}

function updateHiddenOptions() {
  textHiddenOptions.classList.toggle("visible", hiddenText.checked);
  fileHiddenOptions.classList.toggle("visible", hiddenFile.checked);
  if (!hiddenText.checked) {
    textPassword.value = "";
  }
  if (!hiddenFile.checked) {
    filePassword.value = "";
  }
}

function isTextFormActive() {
  const active = document.activeElement;
  return (
    active === sharedText ||
    active === textPassword ||
    active === sharerName ||
    active === hiddenText ||
    active === saveTextBtn
  );
}

function getEditorText() {
  return sharedText.innerText.replace(/\u00a0/g, " ").trim();
}

function getEditorHtml() {
  return sharedText.innerHTML.trim();
}

function clearEditor() {
  sharedText.innerHTML = "";
}

function fallbackCopyText(content) {
  const temp = document.createElement("textarea");
  temp.value = content;
  temp.setAttribute("readonly", "");
  temp.style.position = "fixed";
  temp.style.opacity = "0";
  temp.style.pointerEvents = "none";
  document.body.appendChild(temp);
  temp.focus();
  temp.select();

  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch (error) {
    copied = false;
  }

  document.body.removeChild(temp);
  return copied;
}

function showCopiedState(entryId) {
  copiedTextId = entryId;
  if (copiedTextTimer) {
    window.clearTimeout(copiedTextTimer);
  }
  renderTextHistory(lastRenderedTexts);
  copiedTextTimer = window.setTimeout(() => {
    copiedTextId = null;
    copiedTextTimer = null;
    renderTextHistory(lastRenderedTexts);
  }, 1400);
}

async function copyText(content) {
  clearActiveTabIndicator();
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(content);
    } else if (!fallbackCopyText(content)) {
      throw new Error("Fallback copy failed");
    }
    return true;
  } catch (error) {
    if (fallbackCopyText(content)) {
      return true;
    } else {
      setTextStatus("Clipboard copy failed.");
      return false;
    }
  }
}

async function deleteText(id) {
  try {
    const response = await fetch(`/api/text/${encodeURIComponent(id)}`, {
      method: "DELETE"
    });
    if (!response.ok) {
      throw new Error(`Delete failed: ${response.status}`);
    }
    renderSnapshot(await response.json());
    textStatus.textContent = "Text entry deleted.";
  } catch (error) {
    textStatus.textContent = "Text delete failed.";
  }
}

function maskText(content) {
  return content.replace(/[^\s]/g, "*");
}

async function revealProtectedText(entry) {
  if (!entry.password_required) {
    const content = entry.content ?? "";
    revealedTextContent.set(entry.id, content);
    revealedTextHtml.set(entry.id, entry.content_html ?? "");
    revealedTextIds.add(entry.id);
    return true;
  }

  const password = window.prompt("Password required to reveal this text.");
  if (!password) {
    return false;
  }

  try {
    const response = await fetch(`/api/text/${encodeURIComponent(entry.id)}/reveal`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password })
    });
    if (!response.ok) {
      throw new Error(`Reveal failed: ${response.status}`);
    }
    const payload = await response.json();
    revealedTextContent.set(entry.id, payload.content);
    revealedTextHtml.set(entry.id, payload.content_html ?? "");
    revealedTextIds.add(entry.id);
    textStatus.textContent = "Text revealed.";
    return true;
  } catch (error) {
    textStatus.textContent = "Wrong password.";
    return false;
  }
}

function openProtectedPath(path, statusElement) {
  const password = window.prompt("Password required.");
  if (!password) {
    return;
  }
  if (statusElement) {
    statusElement.textContent = "Opening protected item…";
  }
  window.location.href = withPassword(path, password);
}

async function deleteFile(id) {
  try {
    const response = await fetch(`/api/file/${encodeURIComponent(id)}`, {
      method: "DELETE"
    });
    if (!response.ok) {
      throw new Error(`Delete failed: ${response.status}`);
    }
    renderSnapshot(await response.json());
    fileStatus.textContent = "File deleted.";
  } catch (error) {
    fileStatus.textContent = "File delete failed.";
  }
}

function renderTextHistory(texts) {
  lastRenderedTexts = texts;
  textHistory.innerHTML = "";
  if (!texts.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "No text history yet.";
    textHistory.appendChild(li);
    return;
  }

  for (const entry of texts) {
    const li = document.createElement("li");
    li.className = "history-item copyable";
    li.addEventListener("click", async () => {
      if (entry.hidden && !revealedTextIds.has(entry.id)) {
        const revealed = await revealProtectedText(entry);
        if (revealed) {
          renderTextHistory(texts);
        }
        return;
      }

      const content = revealedTextContent.get(entry.id) ?? entry.content;
      if (content !== null) {
        const copied = await copyText(content);
        if (copied) {
          showCopiedState(entry.id);
        }
      }
    });

    const head = document.createElement("div");
    head.className = "history-head";

    const infoTable = document.createElement("table");
    infoTable.className = "entry-table";

    const savedRow = document.createElement("tr");
    const savedHead = document.createElement("th");
    savedHead.textContent = "Saved";
    const savedValue = document.createElement("td");
    savedValue.textContent = formatDate(entry.created_at);
    savedRow.appendChild(savedHead);
    savedRow.appendChild(savedValue);

    const fromRow = document.createElement("tr");
    const fromHead = document.createElement("th");
    fromHead.textContent = "Shared by";
    const fromValue = document.createElement("td");
    fromValue.textContent = entry.sharer_name || "Anonymous";
    if (entry.sharer_ip) {
      fromValue.textContent += ` (${entry.sharer_ip})`;
    }
    fromRow.appendChild(fromHead);
    fromRow.appendChild(fromValue);

    const expiresRow = document.createElement("tr");
    const expiresHead = document.createElement("th");
    expiresHead.textContent = "Expires";
    const expiresValue = document.createElement("td");
    expiresValue.textContent = formatDate(entry.expires_at);
    expiresRow.appendChild(expiresHead);
    expiresRow.appendChild(expiresValue);

    const linkRow = document.createElement("tr");
    const linkHead = document.createElement("th");
    linkHead.textContent = "LAN link";
    const linkValue = document.createElement("td");
    const shareLink = document.createElement("a");
    shareLink.href = lanSharePath(entry.short_code);
    shareLink.textContent = lanShareUrl(entry.short_code);
    shareLink.title = "Open this text directly over the LAN";
    shareLink.addEventListener("click", (event) => {
      event.stopPropagation();
      if (entry.password_required) {
        event.preventDefault();
        openProtectedPath(lanSharePath(entry.short_code), textStatus);
      }
    });
    linkValue.appendChild(shareLink);
    linkRow.appendChild(linkHead);
    linkRow.appendChild(linkValue);

    infoTable.appendChild(savedRow);
    infoTable.appendChild(fromRow);
    infoTable.appendChild(expiresRow);
    infoTable.appendChild(linkRow);

    if (entry.hidden) {
      const toggleBtn = document.createElement("button");
      toggleBtn.type = "button";
      const isRevealed = revealedTextIds.has(entry.id);
      toggleBtn.textContent = isRevealed ? "Hide" : "Reveal";
      toggleBtn.addEventListener("click", async (event) => {
        event.stopPropagation();
        if (revealedTextIds.has(entry.id)) {
          revealedTextIds.delete(entry.id);
          revealedTextContent.delete(entry.id);
          revealedTextHtml.delete(entry.id);
        } else {
          const revealed = await revealProtectedText(entry);
          if (!revealed) {
            return;
          }
        }
        renderTextHistory(texts);
      });
      const revealRow = document.createElement("tr");
      const revealHead = document.createElement("th");
      revealHead.textContent = "Reveal";
      const revealValue = document.createElement("td");
      revealValue.appendChild(toggleBtn);
      revealRow.appendChild(revealHead);
      revealRow.appendChild(revealValue);
      infoTable.appendChild(revealRow);
    }
    head.appendChild(infoTable);

    const bodyRow = document.createElement("div");
    bodyRow.className = "text-row";

    const cardWrap = document.createElement("div");
    cardWrap.className = "text-card-wrap";

    const label = document.createElement("div");
    label.className = "text-card-label";
    label.textContent = "Click to copy...";
    if (copiedTextId === entry.id) {
      const copiedPill = document.createElement("span");
      copiedPill.className = "copied-pill";
      copiedPill.textContent = "Copied";
      label.appendChild(copiedPill);
    }

    const body = document.createElement("div");
    body.className = "history-body text-card";
    if (copiedTextId === entry.id) {
      body.classList.add("flash-copy");
    }
    const isMasked = entry.hidden && !revealedTextIds.has(entry.id);
    if (isMasked) {
      body.classList.add("masked");
    }
    if (isMasked) {
      body.textContent = entry.masked_content || maskText(entry.content || "");
    } else if (entry.rich && entry.content_html) {
      body.innerHTML = revealedTextHtml.get(entry.id) || entry.content_html;
    } else {
      body.textContent = revealedTextContent.get(entry.id) ?? entry.content ?? "";
    }

    const deleteWrap = document.createElement("div");
    deleteWrap.className = "text-card-actions";

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "danger delete-btn";
    deleteBtn.textContent = "🗑";
    deleteBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteText(entry.id);
    });

    const deleteLabel = document.createElement("div");
    deleteLabel.className = "text-delete-label";
    deleteLabel.textContent = "Delete";

    cardWrap.appendChild(label);
    cardWrap.appendChild(body);
    deleteWrap.appendChild(deleteBtn);
    deleteWrap.appendChild(deleteLabel);
    bodyRow.appendChild(cardWrap);
    bodyRow.appendChild(deleteWrap);

    li.appendChild(head);
    li.appendChild(bodyRow);
    textHistory.appendChild(li);
  }
}

function renderFiles(files) {
  fileList.innerHTML = "";
  if (!files.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "No file history yet.";
    fileList.appendChild(li);
    return;
  }

  for (const file of files) {
    const li = document.createElement("li");
    li.className = "history-item";

    const head = document.createElement("div");
    head.className = "history-head";

    const card = document.createElement("div");
    card.className = "file-card";

    const top = document.createElement("div");
    top.className = "file-card-top";

    const details = document.createElement("div");
    const name = document.createElement("div");
    name.className = "file-name";
    name.textContent = file.name;
    const infoTable = document.createElement("table");
    infoTable.className = "entry-table";

    const sizeRow = document.createElement("tr");
    const sizeHead = document.createElement("th");
    sizeHead.textContent = "Size";
    const sizeValue = document.createElement("td");
    sizeValue.textContent = formatSize(file.size);
    sizeRow.appendChild(sizeHead);
    sizeRow.appendChild(sizeValue);

    const fromRow = document.createElement("tr");
    const fromHead = document.createElement("th");
    fromHead.textContent = "Shared by";
    const fromValue = document.createElement("td");
    fromValue.textContent = file.sharer_name || "Anonymous";
    if (file.sharer_ip) {
      fromValue.textContent += ` (${file.sharer_ip})`;
    }
    fromRow.appendChild(fromHead);
    fromRow.appendChild(fromValue);

    const uploadedRow = document.createElement("tr");
    const uploadedHead = document.createElement("th");
    uploadedHead.textContent = "Uploaded";
    const uploadedValue = document.createElement("td");
    uploadedValue.textContent = formatDate(file.created_at);
    uploadedRow.appendChild(uploadedHead);
    uploadedRow.appendChild(uploadedValue);

    const expiresRow = document.createElement("tr");
    const expiresHead = document.createElement("th");
    expiresHead.textContent = "Expires";
    const expiresValue = document.createElement("td");
    expiresValue.textContent = formatDate(file.expires_at);
    expiresRow.appendChild(expiresHead);
    expiresRow.appendChild(expiresValue);

    const linkRow = document.createElement("tr");
    const linkHead = document.createElement("th");
    linkHead.textContent = "LAN link";
    const linkValue = document.createElement("td");

    const shareLink = document.createElement("a");
    shareLink.href = lanSharePath(file.short_code);
    shareLink.textContent = lanShareUrl(file.short_code);
    shareLink.title = "Open this file directly over the LAN";
    if (file.password_required) {
      shareLink.addEventListener("click", (event) => {
        event.preventDefault();
        openProtectedPath(lanSharePath(file.short_code), fileStatus);
      });
    }
    linkValue.appendChild(shareLink);
    linkRow.appendChild(linkHead);
    linkRow.appendChild(linkValue);

    infoTable.appendChild(sizeRow);
    infoTable.appendChild(fromRow);
    infoTable.appendChild(uploadedRow);
    infoTable.appendChild(expiresRow);
    infoTable.appendChild(linkRow);
    if (file.password_required) {
      const accessRow = document.createElement("tr");
      const accessHead = document.createElement("th");
      accessHead.textContent = "Access";
      const accessValue = document.createElement("td");
      accessValue.textContent = "Password protected";
      accessRow.appendChild(accessHead);
      accessRow.appendChild(accessValue);
      infoTable.appendChild(accessRow);
    }

    details.appendChild(name);
    details.appendChild(infoTable);

    const actions = document.createElement("div");
    actions.className = "file-card-actions";

    const link = document.createElement("a");
    link.className = "file-link";
    link.href = `/download/${encodeURIComponent(file.id)}`;
    link.textContent = "Download";
    link.addEventListener("click", () => {
      clearActiveTabIndicator();
    });
    if (file.password_required) {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        openProtectedPath(`/download/${encodeURIComponent(file.id)}`, fileStatus);
      });
    }

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "danger";
    deleteBtn.textContent = "Delete";
    deleteBtn.addEventListener("click", () => deleteFile(file.id));

    actions.appendChild(link);
    actions.appendChild(deleteBtn);
    top.appendChild(details);
    top.appendChild(actions);
    card.appendChild(top);
    head.appendChild(card);
    li.appendChild(head);
    fileList.appendChild(li);
  }
}

function renderSnapshot(snapshot) {
  const nextTextId = snapshot.texts && snapshot.texts.length ? snapshot.texts[0].id : null;
  const nextFileId = snapshot.files && snapshot.files.length ? snapshot.files[0].id : null;

  if (snapshotInitialized) {
    if (nextTextId && nextTextId !== latestTextId && nextTextId !== suppressedTextId) {
      unreadText = true;
    }
    if (nextFileId && nextFileId !== latestFileId && nextFileId !== suppressedFileId) {
      unreadFiles = true;
    }
  }

  if (nextTextId === suppressedTextId) {
    suppressedTextId = null;
  }
  if (nextFileId === suppressedFileId) {
    suppressedFileId = null;
  }

  latestTextId = nextTextId;
  latestFileId = nextFileId;
  if (!snapshotInitialized) {
    snapshotInitialized = true;
  }

  if (!pendingTextPush && !isTextFormActive()) {
    clearEditor();
  }
  textMeta.textContent = `Last update: ${formatDate(snapshot.updated_at)} • Auto-delete after 24 hours`;
  renderTextHistory(snapshot.texts || []);
  renderFiles(snapshot.files || []);
  updateTabIndicators();
}

async function fetchState() {
  try {
    const response = await fetch("/api/state");
    if (!response.ok) {
      throw new Error(`State request failed: ${response.status}`);
    }
    renderSnapshot(await response.json());
  } catch (error) {
    textStatus.textContent = "Could not refresh shared data.";
  }
}

async function saveText() {
  const content = getEditorText();
  if (!content) {
    textStatus.textContent = "Paste some text first.";
    return;
  }
  const submittedText = content;
  const submittedHtml = getEditorHtml();
  const submittedHidden = hiddenText.checked;
  const submittedPassword = textPassword.value.trim();

  pendingTextPush = true;
  textStatus.textContent = "Saving…";
  try {
    const response = await fetch("/api/text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: submittedText,
        html: submittedHtml,
        hidden: submittedHidden,
        password: textPassword.value,
        name: sharerName.value
      })
    });
    if (!response.ok) {
      throw new Error(`Save failed: ${response.status}`);
    }
    const snapshot = await response.json();
    if (snapshot.texts && snapshot.texts.length > 0) {
      suppressedTextId = snapshot.texts[0].id;
      unreadText = false;
    }
    renderSnapshot(snapshot);
    clearEditor();
    hiddenText.checked = false;
    textPassword.value = "";
    updateHiddenOptions();
    textStatus.textContent = "Text added to history.";
  } catch (error) {
    textStatus.textContent = "Text save failed.";
  } finally {
    pendingTextPush = false;
  }
}

async function uploadFile(file = fileInput.files[0]) {
  if (!file) {
    fileStatus.textContent = "Choose a file first.";
    return;
  }
  if (hiddenFile.checked && !filePassword.value.trim()) {
    fileStatus.textContent = "Add a password before uploading a hidden file.";
    filePassword.focus();
    return;
  }

  const formData = new FormData();
  formData.append("file", file);
  formData.append("hidden", hiddenFile.checked ? "true" : "false");
  formData.append("password", filePassword.value);
  formData.append("name", sharerName.value);
  fileStatus.textContent = `Uploading ${file.name}…`;

  try {
    const response = await fetch("/api/upload", {
      method: "POST",
      body: formData
    });
    if (!response.ok) {
      const message = await response.text();
      throw new Error(message || `Upload failed: ${response.status}`);
    }
    const snapshot = await response.json();
    if (snapshot.files && snapshot.files.length > 0) {
      suppressedFileId = snapshot.files[0].id;
      unreadFiles = false;
    }
    renderSnapshot(snapshot);
    fileStatus.textContent = `Uploaded ${file.name}.`;
    fileInput.value = "";
    hiddenFile.checked = false;
    filePassword.value = "";
    updateHiddenOptions();
  } catch (error) {
    fileStatus.textContent = error.message || "Upload failed.";
  }
}

saveTextBtn.addEventListener("click", saveText);
hiddenText.addEventListener("change", updateHiddenOptions);
hiddenFile.addEventListener("change", updateHiddenOptions);
textTabBtn.addEventListener("click", () => setActiveTab("text"));
fileTabBtn.addEventListener("click", () => setActiveTab("file"));
textPanel.addEventListener("click", clearActiveTabIndicator);
filePanel.addEventListener("click", clearActiveTabIndicator);
uploadBtn.addEventListener("click", () => uploadFile());
fileInput.addEventListener("change", () => {
  if (fileInput.files && fileInput.files.length > 0) {
    uploadFile();
  }
});

sharedText.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    saveText();
  }
});

dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropZone.classList.add("active");
});

dropZone.addEventListener("dragleave", () => {
  dropZone.classList.remove("active");
});

dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropZone.classList.remove("active");
  const droppedFile = event.dataTransfer?.files?.[0];
  if (droppedFile) {
    uploadFile(droppedFile);
  }
});

fetchState();
updateHiddenOptions();
syncTabs();
setInterval(fetchState, 2000);
