"use strict";

const $ = (id) => document.getElementById(id);
const labels = {
  queued: "待機中", processing: "読み取り中", review: "要確認",
  saved: "保存済み", failed: "読み取り失敗",
};
const form = $("card-form");
const video = $("video");
let cards = [];
let selected = null;
let detail = null;
let filter = "all";
let dirty = false;
let stream = null;
let cameraRequest = 0;
let reviewing = false;
let uploading = false;
let updating = false;
let listRevision = 0;
let selectionRevision = 0;
let loadingCard = false;
const photos = [];

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "X-Meishi": "1", ...options.headers },
  });
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    throw new Error(typeof result.detail === "string" ? result.detail : "操作に失敗しました。");
  }
  return response.status === 204 ? null : response.json();
}

function notice(message = "", error = false) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
  $("notice").classList.toggle("error", error);
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function matches(card) {
  const normalize = (text) => text.normalize("NFKC").toLowerCase().replace(/\s+/g, "");
  const query = normalize($("search").value);
  return (filter === "all" || card.status === filter)
    && (!query || [card.name, card.company].some((text) => normalize(text).includes(query)));
}

function cardStatus(card) {
  return card.status === "queued" && !card.codex_requested ? "未読み取り" : labels[card.status];
}

function renderList() {
  $("count").textContent = cards.length + "枚の名刺";
  const reviews = cards.filter((card) => card.status === "review").length;
  $("review-count").textContent = reviews || "";
  for (const button of document.querySelectorAll("[data-filter]")) {
    button.setAttribute("aria-pressed", button.dataset.filter === filter);
  }
  const visible = cards.filter(matches);
  const list = $("cards");
  const focusId = document.activeElement?.dataset.card;
  list.replaceChildren();
  for (const card of visible) {
    const item = element("li", "");
    const row = element("button", "", "card-row");
    row.type = "button";
    row.dataset.card = card.id;
    row.setAttribute("aria-pressed", card.id === selected);
    const role = element("span", card.department_title || "—", "role-cell");
    const address = element("span", card.company_address || "—", "address-cell");
    role.title = card.department_title || "";
    address.title = card.company_address || "";
    row.append(
      element("strong", card.name || (card.status === "review" ? "氏名未入力" : "新しい名刺")),
      element("span", card.company || "—", "company"),
      role,
      address,
      element("span", cardStatus(card), "badge " + card.status),
    );
    row.addEventListener("click", () => selectCard(card.id, true));
    item.append(row);
    list.append(item);
    if (focusId === card.id) row.focus({ preventScroll: true });
  }
  $("empty").hidden = visible.length > 0;
  $("empty").textContent = cards.length ? "条件に一致する名刺がありません。" : "名刺を取り込むと、ここに表示されます。";
}

async function refresh() {
  const revision = ++listRevision;
  const next = await request("/api/cards");
  if (revision !== listRevision) return;
  if (JSON.stringify(next) !== JSON.stringify(cards)) {
    cards = next;
    renderList();
  }
  const active = cards.find((card) => card.id === selected);
  if (active && !dirty && !updating && !loadingCard && detail
    && (active.status !== detail.status || active.codex_requested !== detail.codex_requested
      || active.read_error !== detail.read_error)) {
    await selectCard(active.id);
  }
}

function canLeave() {
  return !dirty || confirm("保存していない変更を破棄しますか？");
}

function clearDetail() {
  selectionRevision += 1;
  loadingCard = false;
  selected = null;
  detail = null;
  dirty = false;
  $("details").hidden = true;
  $("card-photo").removeAttribute("src");
  renderList();
}

async function selectCard(id, scroll = false) {
  if (updating || (id === selected && dirty)) return;
  if (id !== selected && !canLeave()) return;
  const revision = ++selectionRevision;
  loadingCard = true;
  try {
    const result = await request("/api/cards/" + id);
    if (revision !== selectionRevision || dirty && selected === id) return;
    selected = id;
    detail = result;
    dirty = false;
    renderList();
    renderDetail();
    if (scroll && matchMedia("(max-width: 780px)").matches) {
      $("details").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  } catch (error) {
    if (revision === selectionRevision) notice(error.message, true);
  } finally {
    if (revision === selectionRevision) loadingCard = false;
  }
}

function renderDetail() {
  const status = detail.status;
  $("details").hidden = false;
  $("detail-title").textContent = status === "saved" ? "名刺の詳細" : "読み取り結果";
  $("detail-status").textContent = status === "failed"
    ? detail.read_error || "読み取れませんでした。再試行するか手入力してください。"
    : cardStatus(detail);
  const photo = "/api/cards/" + detail.id + "/photo";
  if ($("card-photo").getAttribute("src") !== photo) $("card-photo").src = photo;
  form.hidden = Boolean(detail.codex_requested) || status === "processing";
  $("reread").hidden = form.hidden || status === "saved";
  $("ocr-source").hidden = !detail.raw_text;
  $("raw-text").textContent = detail.raw_text;
  $("save").textContent = status === "saved" ? "変更を保存" : "確認して保存";
  for (const input of form.querySelectorAll("[name]")) input.value = detail[input.name];
}

async function changeCard(action) {
  if (!selected || updating || loadingCard) return;
  const id = selected;
  const url = "/api/cards/" + id;
  updating = true;
  for (const button of $("details").querySelectorAll("button, input, textarea")) button.disabled = true;
  try {
    if (action === "save") {
      const values = Object.fromEntries([...form.querySelectorAll("[name]")].map((input) => [input.name, input.value]));
      detail = await request(url, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values),
      });
      dirty = false;
      if (filter === "review") clearDetail();
      else renderDetail();
      notice("名刺を保存しました。");
    } else if (action === "reread") {
      detail = await request(url + "/reread", { method: "POST" });
      dirty = false;
      renderDetail();
      notice("読み取り待ちに追加しました。");
    } else {
      await request(url, { method: "DELETE" });
      clearDetail();
      notice("名刺を削除しました。");
    }
    await refresh();
  } catch (error) {
    notice(error.message, true);
  } finally {
    updating = false;
    for (const button of $("details").querySelectorAll("button, input, textarea")) button.disabled = false;
  }
}

function stopCamera() {
  cameraRequest += 1;
  stream?.getTracks().forEach((track) => track.stop());
  stream = null;
  video.srcObject = null;
  $("viewfinder").hidden = true;
  $("shutter").hidden = true;
  $("start-camera").hidden = false;
  $("start-camera").disabled = false;
}

async function startCamera() {
  if (!navigator.mediaDevices?.getUserMedia) {
    $("camera-tip").textContent = "カメラにはHTTPS接続が必要です。画像を選んで取り込めます。";
    return;
  }
  const revision = ++cameraRequest;
  $("start-camera").disabled = true;
  try {
    const next = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 } }, audio: false });
    if (revision !== cameraRequest) {
      next.getTracks().forEach((track) => track.stop());
      return;
    }
    stream = next;
    video.srcObject = stream;
    await video.play();
    if (revision !== cameraRequest) return;
    $("viewfinder").hidden = false;
    $("shutter").hidden = false;
    $("start-camera").hidden = true;
    $("camera-tip").textContent = "枠に名刺を合わせて撮影してください。";
  } catch {
    if (revision !== cameraRequest) return;
    stopCamera();
    $("camera-tip").textContent = "カメラを開けませんでした。権限を確認するか、画像を選んでください。";
  } finally {
    if (revision === cameraRequest) $("start-camera").disabled = false;
  }
}

function addPhoto(file) {
  if (!file || uploading) return;
  if (file.size > 12 * 1024 * 1024) {
    notice("画像は12MB以下にしてください。", true);
    return;
  }
  if (photos.length >= 20) {
    notice("一度に取り込めるのは20枚までです。", true);
    return;
  }
  const id = crypto.randomUUID();
  photos.push({ id, file, preview: URL.createObjectURL(file) });
  renderQueue();
}

function captureFrame() {
  if (!video.videoWidth || !stream) return;
  const revision = cameraRequest;
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);
  canvas.toBlob((blob) => {
    if (revision === cameraRequest) addPhoto(blob);
  }, "image/jpeg", 0.94);
}

function removePhoto(item) {
  URL.revokeObjectURL(item.preview);
  photos.splice(photos.indexOf(item), 1);
}

function renderQueue() {
  $("queue-count").textContent = (reviewing ? "撮影した名刺 " : "撮影済み ") + photos.length + "枚";
  $("review-batch").hidden = reviewing;
  $("review-batch").disabled = !photos.length;
  $("camera-area").hidden = reviewing;
  $("batch-actions").hidden = !reviewing;
  $("continue").disabled = uploading;
  $("upload").disabled = uploading || !photos.length;
  $("upload").textContent = uploading ? "取り込み中…" : photos.length + "枚を取り込む";
  $("photos").classList.toggle("reviewing", reviewing);
  $("photos").replaceChildren();
  photos.forEach((item, index) => {
    const tile = element("div", "", "photo-tile");
    const preview = document.createElement("img");
    preview.alt = (index + 1) + "枚目の名刺";
    preview.src = item.preview;
    preview.addEventListener("error", () => preview.replaceWith(element("span", preview.alt, "preview-fallback")));
    const remove = element("button", "×", "remove");
    remove.type = "button";
    remove.disabled = uploading;
    remove.setAttribute("aria-label", preview.alt + "を取り除く");
    remove.addEventListener("click", () => {
      removePhoto(item);
      renderQueue();
    });
    tile.append(preview, remove);
    $("photos").append(tile);
  });
}

function show(view) {
  if (uploading) return;
  const capture = view === "capture";
  if (!capture) stopCamera();
  $("dashboard").hidden = capture;
  $("capture").hidden = !capture;
  $("nav-list").setAttribute("aria-pressed", !capture);
  $("nav-capture").setAttribute("aria-pressed", capture);
  if (capture) renderQueue();
  else refresh().catch((error) => notice(error.message, true));
  window.scrollTo(0, 0);
}

async function uploadBatch() {
  if (uploading || !photos.length) return;
  uploading = true;
  renderQueue();
  const failures = [];
  let accepted = 0;
  for (const item of [...photos]) {
    const data = new FormData();
    data.append("photo", item.file, item.file.name || "card.jpg");
    try {
      await request("/api/cards/" + item.id, { method: "POST", body: data });
      accepted += 1;
      removePhoto(item);
    } catch (error) {
      failures.push(error.message);
    }
    renderQueue();
  }
  uploading = false;
  if (!failures.length) {
    reviewing = false;
    filter = "all";
    $("search").value = "";
    show("list");
  }
  renderQueue();
  const message = accepted ? accepted + "枚を取り込みました。結果は名刺一覧に表示されます。" : "";
  const failure = failures.length ? failures.length + "枚を取り込めませんでした。 " + failures[0] : "";
  notice([message, failure].filter(Boolean).join(" "), !!failures.length);
}

$("nav-list").addEventListener("click", () => show("list"));
$("nav-capture").addEventListener("click", () => show("capture"));
$("add").addEventListener("click", () => show("capture"));
$("search").addEventListener("input", renderList);
for (const button of document.querySelectorAll("[data-filter]")) {
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    renderList();
  });
}
$("close-detail").addEventListener("click", () => { if (canLeave()) clearDetail(); });
form.addEventListener("input", () => { dirty = true; });
form.addEventListener("submit", (event) => { event.preventDefault(); changeCard("save"); });
$("reread").addEventListener("click", () => {
  const warning = dirty ? "保存していない変更は破棄されます。" : "";
  if (confirm("この名刺の画像をOpenAIに送信し、読み取りをやり直しますか？" + warning)) {
    changeCard("reread");
  }
});
$("delete").addEventListener("click", () => {
  if (confirm("この名刺と写真を削除しますか？")) changeCard("delete");
});
$("start-camera").addEventListener("click", startCamera);
$("shutter").addEventListener("click", captureFrame);
$("picker").addEventListener("change", (event) => {
  for (const file of event.target.files) addPhoto(file);
  event.target.value = "";
});
$("review-batch").addEventListener("click", () => { reviewing = true; stopCamera(); renderQueue(); });
$("continue").addEventListener("click", () => { reviewing = false; renderQueue(); });
$("upload").addEventListener("click", uploadBatch);
window.addEventListener("beforeunload", (event) => {
  if (dirty || photos.length) event.preventDefault();
});
window.addEventListener("pagehide", stopCamera);

renderList();
refresh().catch((error) => notice(error.message, true));
setInterval(() => {
  if (!document.hidden && !$("dashboard").hidden) refresh().catch((error) => notice(error.message, true));
}, 2500);
