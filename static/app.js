const state = {
  screen: ["home", "chat", "trip", "timeline", "life"].includes(new URLSearchParams(location.search).get("screen")) ? new URLSearchParams(location.search).get("screen") : (localStorage.getItem("love-screen") || "home"),
  plan: localStorage.getItem("love-plan") || "slow",
  privacy: "双方可见",
  callStartedAt: null,
  callId: null,
  callTimer: null,
  callHeartbeat: null,
  callRecord: null,
  callPeer: null,
  callLocalStream: null,
  callSignalCursor: 0,
  callPendingIce: [],
  callProcessingSignals: false,
  callTransport: null,
  callAttempt: 0,
  callTestAudioCleanup: null,
  callProvider: null,
  callMuted: false,
  agoraClient: null,
  agoraLocalTrack: null,
  agoraRemoteTrack: null,
  data: null,
  selectedItemIds: new Set(),
  editingItemId: null,
  itineraryDetailItemId: null,
  sharedDetail: null,
  pendingRevision: null,
  momentType: "text",
  editingMomentId: null,
  online: true,
  authMode: "login",
  registrationEnabled: true,
  modalTrigger: null,
  manifests: {},
  pinnedSmartSurfaces: new Set(),
  composingSmartSurfaces: new Set(),
  smartIntentDrafts: {},
  smartActionId: null,
  smartActionType: null,
  lifeFilter: "all",
  lifeSection: ["overview", "meals", "wishlist", "calendar", "anniversary", "todo", "preference", "dates"].includes(localStorage.getItem("love-life-section")) ? localStorage.getItem("love-life-section") : "overview",
  mealSection: ["recipes", "wishes", "calendar", "shopping"].includes(localStorage.getItem("love-meal-section")) ? localStorage.getItem("love-meal-section") : "recipes",
  mealCalendarView: ["week", "month"].includes(localStorage.getItem("love-meal-calendar-view")) ? localStorage.getItem("love-meal-calendar-view") : "week",
  mealMonth: new Date(new Date().getFullYear(), new Date().getMonth(), 1),
  mealSelectedDate: new Date().toLocaleDateString("sv-SE"),
  meals: { recipes: [], recipeDrafts: [], recipeNextCursor: null, wishes: [], cart: { items: [] }, calendar: { days: [] }, shopping: { items: [] }, shoppingPreview: null, openWishId: "", wishRecipeCandidates: [] },
  tripSection: ["overview", "assistant", "plans", "itinerary", "stays", "tools"].includes(localStorage.getItem("love-trip-section")) ? localStorage.getItem("love-trip-section") : "overview",
  tripAssistant: { tripId: "", items: [], loading: false },
  tripAssistantLoadSequence: 0,
  tripLiveEpoch: 0,
  tripLive: { tripId: "", ownerUserId: null, context: null, resultAnchorContext: null, result: null, fetchedAt: 0, deliveryMode: "idle", loading: false, error: "", routes: {} },
  timelineView: ["list", "album", "map", "report", "private", "trash"].includes(localStorage.getItem("love-timeline-view")) ? localStorage.getItem("love-timeline-view") : "list",
  timelineRenderToken: 0,
  realtime: null,
  realtimeCursor: 0,
  realtimeRefresh: null,
  selectedDateVariant: null,
  installPrompt: null,
  chatImport: null,
  longPressTimer: null,
  longPressStart: null,
  accountAutoOpened: false,
  smartDeepLinked: false,
  pushDeepLinked: false,
  pushSubscribed: false,
  pushSynced: false,
  pushConfig: null,
  serviceWorkerRegistration: null,
  destinationCandidates: [],
  destinationCandidateId: null,
  destinationExistingPlace: null,
  destinationSearchTimer: null,
  destinationSearchSequence: 0,
  destinationSearchCache: new Map(),
  routeLegs: null,
  tripGenerationResuming: false,
  lifeAssistant: {
    today: null,
    plans: [],
    notifications: [],
    unreadCount: 0,
    summary: null,
    activePlan: null,
    loading: false
  },
  assistantDeepLinked: false
};

const AUTH_SYNC_KEY = "love-auth-sync";

function resetTripLive(tripId = "") {
  state.tripLiveEpoch += 1;
  state.tripLive = {
    tripId, ownerUserId: null, context: null, resultAnchorContext: null, result: null,
    fetchedAt: 0, deliveryMode: "idle", loading: false, error: "", routes: {},
  };
}

function avatarTheme(member) {
  const key = String(member?.id || member?.username || member?.nickname || member?.avatar_label || "love");
  let hash = 2166136261;
  for (const character of key) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return ["plum", "sage", "sand", "rose"][(hash >>> 0) & 3];
}

function applyAvatarIdentity(selector, member, roleLabel) {
  document.querySelectorAll(selector).forEach(element => {
    element.textContent = member?.avatar_label || member?.nickname?.slice(0, 1) || "♡";
    element.dataset.avatarTheme = avatarTheme(member);
    element.title = `${roleLabel}：${member?.nickname || "未命名"}`;
    element.setAttribute("aria-label", `${roleLabel}：${member?.nickname || "未命名"}`);
  });
}

function broadcastAuthChange(action, username = "") {
  localStorage.setItem(AUTH_SYNC_KEY, JSON.stringify({ action, username, at: Date.now() }));
}

window.addEventListener("storage", event => {
  if (event.key === AUTH_SYNC_KEY && event.newValue) window.location.reload();
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || !state.data?.user) return;
  api("/api/v1/auth/me").then(result => {
    if (result.user?.id && result.user.id !== state.data?.user?.id) window.location.reload();
  }).catch(() => {});
});

window.addEventListener("beforeinstallprompt", event => {
  event.preventDefault();
  state.installPrompt = event;
});
window.addEventListener("online", () => { flushMealMutations().then(() => loadMeals({ quiet: true })).catch(() => {}); });

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    cache: "no-store",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error || `请求失败 (${response.status})`);
    error.status = response.status;
    error.code = body.code;
    error.details = body.details;
    if (response.status === 401) {
      resetTripLive();
      state.data = null;
      state.pushSynced = false;
      state.pushSubscribed = false;
      showAccountAuth();
    }
    throw error;
  }
  return body;
}

function tripCreationRequest(payload) {
  const fingerprint = JSON.stringify(payload);
  let pending = null;
  try { pending = JSON.parse(localStorage.getItem("love-trip-generation") || "null"); } catch (_) {}
  if (!pending || pending.fingerprint !== fingerprint) {
    pending = {
      key: globalThis.crypto?.randomUUID?.() || `trip-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      fingerprint,
      jobId: null,
    };
    localStorage.setItem("love-trip-generation", JSON.stringify(pending));
  }
  return pending;
}

async function waitForTripGeneration(jobId, { timeoutMs = 150000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastNetworkError = null;
  while (Date.now() < deadline) {
    try {
      const job = await api(`/api/v1/trip-generation-jobs/${encodeURIComponent(jobId)}`);
      lastNetworkError = null;
      if (job.status === "completed" && job.trip) {
        localStorage.removeItem("love-trip-generation");
        return job.trip;
      }
      if (job.status === "failed" || job.status === "cancelled") {
        localStorage.removeItem("love-trip-generation");
        throw new Error(job.error_message || "旅行方案生成失败，请修改条件后重试");
      }
    } catch (error) {
      if (error.status === 404 || (error.message && !/请求失败|Failed to fetch|NetworkError/i.test(error.message))) throw error;
      lastNetworkError = error;
    }
    await new Promise(resolve => setTimeout(resolve, 1500));
  }
  throw new Error(lastNetworkError ? "网络暂时中断，旅行仍在后台生成；恢复网络后会自动显示" : "旅行仍在后台生成，可以先使用其他页面，完成后会自动显示");
}

async function createTripInBackground(payload) {
  const pending = tripCreationRequest(payload);
  if (!pending.jobId) {
    const job = await api("/api/v1/trip-generation-jobs", {
      method: "POST",
      headers: { "Idempotency-Key": pending.key },
      body: JSON.stringify(payload),
    });
    pending.jobId = job.id;
    localStorage.setItem("love-trip-generation", JSON.stringify(pending));
    if (job.status === "completed" && job.trip) {
      localStorage.removeItem("love-trip-generation");
      return job.trip;
    }
  }
  return waitForTripGeneration(pending.jobId);
}

async function resumePendingTripGeneration() {
  let pending = null;
  try { pending = JSON.parse(localStorage.getItem("love-trip-generation") || "null"); } catch (_) {}
  if (!pending?.jobId || !state.data || state.tripGenerationResuming) return;
  state.tripGenerationResuming = true;
  try {
    const trip = await waitForTripGeneration(pending.jobId, { timeoutMs: 150000 });
    if (!state.data) return;
    state.data.trip = trip;
    state.data.trips = [trip, ...(state.data.trips || []).filter(item => item.id !== trip.id)];
    state.plan = trip.active_variant || "slow";
    localStorage.setItem("love-trip-id", trip.id);
    renderIdentity(); renderPlan(); renderHotel();
    toast("后台旅行规划已完成，五套方案已经可以查看");
  } catch (error) {
    if (!/仍在后台生成|网络暂时中断/.test(error.message || "")) toast(error.message);
  } finally { state.tripGenerationResuming = false; }
}

function isIosDevice() {
  return /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

function isStandaloneApp() {
  return window.matchMedia?.("(display-mode: standalone)").matches || navigator.standalone === true;
}

function decodeApplicationServerKey(value) {
  const padded = String(value || "").replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(String(value || "").length / 4) * 4, "=");
  const raw = atob(padded);
  return Uint8Array.from(raw, char => char.charCodeAt(0));
}

function pushSubscriptionPayload(subscription) {
  if (!subscription) return null;
  const json = typeof subscription.toJSON === "function" ? subscription.toJSON() : subscription;
  return { endpoint: json.endpoint, keys: { p256dh: json.keys?.p256dh, auth: json.keys?.auth } };
}

async function serviceWorkerRegistration() {
  if (!("serviceWorker" in navigator)) throw new Error("当前浏览器不支持后台通知");
  if (!state.serviceWorkerRegistration) {
    state.serviceWorkerRegistration = await navigator.serviceWorker.register("/sw.js");
    await navigator.serviceWorker.ready;
  }
  return state.serviceWorkerRegistration;
}

function renderPushStatus(message, status = "") {
  const statusNode = document.getElementById("pushStatus");
  const summary = document.getElementById("notificationSummary");
  const enable = document.getElementById("pushEnableButton");
  const test = document.getElementById("pushTestButton");
  const disable = document.getElementById("pushDisableButton");
  if (statusNode) {
    statusNode.textContent = message;
    statusNode.className = `push-status ${status}`.trim();
  }
  if (summary) summary.textContent = state.pushSubscribed ? "当前设备已开启" : message;
  if (enable) enable.hidden = state.pushSubscribed;
  if (test) test.hidden = !state.pushSubscribed;
  if (disable) disable.hidden = !state.pushSubscribed;
}

async function refreshPushStatus({ resync = false } = {}) {
  state.pushSubscribed = false;
  if (!("Notification" in window) || !("PushManager" in window) || !("serviceWorker" in navigator)) {
    renderPushStatus("当前浏览器不支持锁屏 Web Push", "warning");
    return false;
  }
  if (!window.isSecureContext) {
    renderPushStatus("请使用 HTTPS 地址后开启锁屏提醒", "warning");
    return false;
  }
  if (isIosDevice() && !isStandaloneApp()) {
    renderPushStatus("iPhone/iPad 请先从分享菜单添加到主屏幕", "warning");
    return false;
  }
  state.pushConfig = await api("/api/v1/push/config");
  if (!state.pushConfig.enabled || !state.pushConfig.public_key) {
    renderPushStatus(state.pushConfig.reason || "服务器尚未启用 Web Push", "warning");
    return false;
  }
  if (Notification.permission === "denied") {
    renderPushStatus("通知已被系统拒绝，请到系统设置中允许恋屿通知", "warning");
    return false;
  }
  const registration = await serviceWorkerRegistration();
  const subscription = await registration.pushManager.getSubscription();
  state.pushSubscribed = Boolean(subscription && Notification.permission === "granted");
  if (state.pushSubscribed && resync && !state.pushSynced) {
    await api("/api/v1/push/subscriptions", { method: "POST", body: JSON.stringify({ subscription: pushSubscriptionPayload(subscription) }) });
    state.pushSynced = true;
  }
  const devices = Math.max(Number(state.pushConfig.active_device_count || 0), state.pushSubscribed ? 1 : 0);
  renderPushStatus(
    state.pushSubscribed ? `当前设备已开启${devices > 1 ? ` · 共 ${devices} 台设备` : ""}` : "当前设备尚未开启消息与来电提醒",
    state.pushSubscribed ? "ready" : ""
  );
  return state.pushSubscribed;
}

async function enablePushNotifications() {
  if (!("Notification" in window) || !("PushManager" in window) || !("serviceWorker" in navigator)) throw new Error("当前浏览器不支持锁屏 Web Push");
  if (!window.isSecureContext) throw new Error("请使用 HTTPS 地址打开恋屿后再开启提醒");
  if (isIosDevice() && !isStandaloneApp()) throw new Error("请先点 Safari 分享按钮，选择“添加到主屏幕”，再从主屏幕打开恋屿");
  // iOS requires the permission prompt to be initiated directly by this tap.
  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new Error("通知未获授权，可在系统设置中允许恋屿通知");
  const config = state.pushConfig || await api("/api/v1/push/config");
  if (!config.enabled || !config.public_key) throw new Error(config.reason || "服务器尚未启用 Web Push");
  const registration = await serviceWorkerRegistration();
  let subscription = await registration.pushManager.getSubscription();
  const expectedKey = decodeApplicationServerKey(config.public_key);
  const existingKey = subscription?.options?.applicationServerKey ? new Uint8Array(subscription.options.applicationServerKey) : null;
  if (subscription && existingKey && (existingKey.length !== expectedKey.length || existingKey.some((byte, index) => byte !== expectedKey[index]))) {
    await subscription.unsubscribe();
    subscription = null;
  }
  if (!subscription) subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: expectedKey });
  await api("/api/v1/push/subscriptions", { method: "POST", body: JSON.stringify({ subscription: pushSubscriptionPayload(subscription) }) });
  state.pushSynced = true;
  state.pushSubscribed = true;
  state.pushConfig = config;
  await refreshPushStatus();
}

async function disablePushNotifications() {
  const registration = await serviceWorkerRegistration();
  const subscription = await registration.pushManager.getSubscription();
  let serverError = null;
  if (subscription) {
    try { await api("/api/v1/push/subscriptions", { method: "DELETE", body: JSON.stringify({ subscription: pushSubscriptionPayload(subscription) }) }); }
    catch (error) { serverError = error; }
    await subscription.unsubscribe();
  }
  state.pushSubscribed = false;
  state.pushSynced = false;
  renderPushStatus("当前设备已关闭消息与来电提醒");
  if (serverError) throw new Error(`浏览器提醒已关闭；服务器记录稍后会自动失效（${serverError.message}）`);
}

async function testPushNotification() {
  const registration = await serviceWorkerRegistration();
  const subscription = await registration.pushManager.getSubscription();
  if (!subscription) throw new Error("当前设备尚未开启后台提醒");
  return api("/api/v1/push/test", { method: "POST", body: JSON.stringify({ subscription: pushSubscriptionPayload(subscription) }) });
}

async function compressImageForUpload(file) {
  if (!file?.type?.startsWith("image/") || file.size < 2 * 1024 * 1024 || !globalThis.createImageBitmap) return file;
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = Math.min(1, 2560 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    canvas.getContext("2d", { alpha: true }).drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close();
    const outputType = file.type === "image/png" ? "image/webp" : "image/jpeg";
    const blob = await new Promise(resolve => canvas.toBlob(resolve, outputType, .86));
    if (!blob || blob.size >= file.size) return file;
    const baseName = (file.name || "photo").replace(/\.[^.]+$/, "");
    return new File([blob], `${baseName}${outputType === "image/webp" ? ".webp" : ".jpg"}`, { type: outputType, lastModified: file.lastModified });
  } catch (_) {
    return file;
  }
}

async function createVideoThumbnail(file) {
  if (!file?.type?.startsWith("video/")) return null;
  return new Promise(resolve => {
    const video = document.createElement("video");
    const source = URL.createObjectURL(file);
    let finished = false;
    const finish = value => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      video.pause();
      video.removeAttribute("src");
      video.load();
      URL.revokeObjectURL(source);
      resolve(value);
    };
    const capture = () => {
      try {
        const width = video.videoWidth || 1280;
        const height = video.videoHeight || 720;
        const scale = Math.min(1, 1280 / Math.max(width, height));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(width * scale));
        canvas.height = Math.max(1, Math.round(height * scale));
        canvas.getContext("2d", { alpha: false }).drawImage(video, 0, 0, canvas.width, canvas.height);
        canvas.toBlob(blob => finish(blob), "image/jpeg", .84);
      } catch (_) { finish(null); }
    };
    const timer = setTimeout(() => finish(null), 8000);
    video.muted = true;
    video.playsInline = true;
    video.preload = "metadata";
    video.addEventListener("error", () => finish(null), { once: true });
    video.addEventListener("loadeddata", () => {
      const target = Number.isFinite(video.duration) ? Math.min(2, Math.max(.1, video.duration * .1)) : 0;
      if (target > 0 && Math.abs(video.currentTime - target) > .05) {
        video.addEventListener("seeked", capture, { once: true });
        try { video.currentTime = target; } catch (_) { capture(); }
      } else capture();
    }, { once: true });
    video.src = source;
    video.load();
  });
}

async function uploadMediaResumable(file, visibility) {
  const resumeKey = `love-upload:${file.name}:${file.size}:${file.lastModified}:${visibility}`;
  let session = null;
  const remembered = localStorage.getItem(resumeKey);
  if (remembered) {
    try {
      session = await api(`/api/v1/media/uploads/${encodeURIComponent(remembered)}`);
      if (Number(session.expected_size) !== file.size || session.status !== "pending") session = null;
    } catch (_) { session = null; }
    if (!session) localStorage.removeItem(resumeKey);
  }
  if (!session) {
    session = await api("/api/v1/media/uploads", { method: "POST", body: JSON.stringify({ name: file.name || "video", mime_type: file.type || "application/octet-stream", size_bytes: file.size, visibility, chunk_size: 4 * 1024 * 1024 }) });
    localStorage.setItem(resumeKey, session.id);
  }
  if (file.type.startsWith("video/")) {
    const thumbnail = await createVideoThumbnail(file);
    if (thumbnail) {
      const thumbnailResponse = await fetch(`/api/v1/media/uploads/${encodeURIComponent(session.id)}/thumbnail`, { method: "POST", cache: "no-store", credentials: "same-origin", headers: { "Content-Type": "image/jpeg" }, body: thumbnail });
      const thumbnailBody = await thumbnailResponse.json().catch(() => ({}));
      if (!thumbnailResponse.ok) throw new Error(thumbnailBody.error || "视频封面生成失败");
    }
  }
  let offset = Number(session.received_size || 0);
  const chunkSize = Number(session.chunk_size || 4 * 1024 * 1024);
  let lastProgress = -1;
  while (offset < file.size) {
    const chunk = file.slice(offset, Math.min(file.size, offset + chunkSize));
    const response = await fetch(`/api/v1/media/uploads/${encodeURIComponent(session.id)}/chunk`, { method: "PUT", cache: "no-store", credentials: "same-origin", headers: { "Content-Type": "application/octet-stream", "X-Upload-Offset": String(offset) }, body: chunk });
    const body = await response.json().catch(() => ({}));
    if (response.status === 409 && Number.isFinite(Number(body.details?.expected_offset))) {
      offset = Number(body.details.expected_offset);
      continue;
    }
    if (!response.ok) throw new Error(body.error || `分片上传失败 (${response.status})`);
    offset = Number(body.received_size);
    const progress = Math.floor((offset / file.size) * 4) * 25;
    if (progress !== lastProgress && progress < 100) { lastProgress = progress; toast(`视频上传 ${progress}% · 中断后可继续`); }
  }
  const result = await api(`/api/v1/media/uploads/${encodeURIComponent(session.id)}/complete`, { method: "POST", body: "{}" });
  localStorage.removeItem(resumeKey);
  return result;
}

async function uploadMedia(file, visibility = "shared") {
  const prepared = await compressImageForUpload(file);
  if (prepared !== file) toast(`照片已安全压缩：${(file.size / 1024 / 1024).toFixed(1)} MB → ${(prepared.size / 1024 / 1024).toFixed(1)} MB`);
  if (prepared.type.startsWith("video/") || prepared.size > 8 * 1024 * 1024) {
    return uploadMediaResumable(prepared, visibility);
  }
  const response = await fetch("/api/v1/media", {
    method: "POST",
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      "Content-Type": prepared.type || "application/octet-stream",
      "X-Filename": encodeURIComponent(prepared.name || "media"),
      "X-Media-Visibility": visibility
    },
    body: prepared
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error || `上传失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return body;
}

function clientKey(prefix) {
  const suffix = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `web:${prefix}:${suffix}`;
}

function money(cents) {
  return `¥ ${(Number(cents || 0) / 100).toLocaleString("zh-CN", { maximumFractionDigits: 0 })}`;
}

function timeLabel(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function shortDate(value) {
  if (!value) return "";
  const date = new Date(`${value}T00:00:00`);
  return date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

function nextYearlyDate(value) {
  const source = new Date(`${String(value).slice(0, 10)}T00:00:00`);
  if (Number.isNaN(source.getTime())) return null;
  const now = new Date(); now.setHours(0, 0, 0, 0);
  const candidate = new Date(now.getFullYear(), source.getMonth(), source.getDate());
  if (candidate < now) candidate.setFullYear(candidate.getFullYear() + 1);
  return candidate;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function navigate(screen, { restoreTimeline = true } = {}) {
  state.screen = screen;
  localStorage.setItem("love-screen", screen);
  document.querySelectorAll(".screen").forEach(el => el.classList.toggle("active", el.dataset.screen === screen));
  document.querySelectorAll(".bottom-nav [data-nav]").forEach(el => el.classList.toggle("active", el.dataset.nav === screen));
  document.querySelector(".app-header").style.display = screen === "home" ? "flex" : "none";
  if (screen === "life") setLifeSection(state.lifeSection, { scroll: false });
  if (screen === "trip") setTripSection(state.tripSection, { scroll: false });
  if (screen === "timeline" && restoreTimeline) setTimelineView(state.timelineView, { scroll: false });
  document.querySelector(".phone-shell").scrollTo({ top: 0, behavior: "smooth" });
  if (state.data) loadSmartSurface(screen, { quiet: true });
  if (screen === "chat" && state.data) api("/api/v1/messages/read", { method: "POST", body: "{}" }).catch(() => {});
}

const smartExamples = {
  home: "今天我们还有什么要确认？",
  chat: "帮我整理适合收藏的聊天灵感",
  trip: "帮我比较五套方案的预算、主题和强度",
  timeline: "整理今年一起去过的城市",
  life: "找出本月还没完成的共同愿望"
};

function validManifest(value, surface) {
  return value && value.schemaVersion === "1.0" && value.surface === surface && Array.isArray(value.sections) && Array.isArray(value.personalOverlay);
}

function manifestHasPendingWrite(manifest) {
  return Boolean(manifest && [...(manifest.sections || []), ...(manifest.personalOverlay || [])].some(node => (node.actions || []).some(action => action.requiresConfirmation)));
}

function offlineSafeManifest(manifest) {
  if (!manifest) return manifest;
  const cleanNodes = nodes => (nodes || []).map(node => ({
    ...node,
    // Write-action IDs belong to a short-lived, user-scoped server draft. They
    // must never be restored from long-lived browser storage.
    actions: (node.actions || []).filter(action => !action.requiresConfirmation)
  }));
  return { ...manifest, sections: cleanNodes(manifest.sections), personalOverlay: cleanNodes(manifest.personalOverlay), generatedBy: "cache" };
}

function smartCacheKey(surface) {
  return surface === "trip" && state.data?.trip?.id ? `love-ui-trip:${state.data.trip.id}` : `love-ui-${surface}`;
}

function saveManifestCache(surface, manifest) {
  const safe = offlineSafeManifest(manifest);
  if (validManifest(safe, surface)) localStorage.setItem(smartCacheKey(surface), JSON.stringify(safe));
}

function surfaceHasDraftIntent(surface) {
  const input = document.querySelector(`[data-smart-form="${surface}"] input`);
  return Boolean(input?.value.trim() || state.smartIntentDrafts[surface]?.trim());
}

function removeSmartActionFromManifests(actionId) {
  for (const [surface, manifest] of Object.entries(state.manifests)) {
    if (!manifest) continue;
    const cleanNodes = nodes => (nodes || []).map(node => ({ ...node, actions: (node.actions || []).filter(action => action.id !== actionId) }));
    const cleaned = { ...manifest, sections: cleanNodes(manifest.sections), personalOverlay: cleanNodes(manifest.personalOverlay) };
    state.manifests[surface] = cleaned;
    saveManifestCache(surface, cleaned);
    if (!manifestHasPendingWrite(cleaned)) state.pinnedSmartSurfaces.delete(surface);
    renderSmartSurface(surface, cleaned);
  }
}

function readLocalJson(key) {
  try { return JSON.parse(localStorage.getItem(key) || "null"); }
  catch (_) { localStorage.removeItem(key); return null; }
}

function openMealCache() {
  if (!("indexedDB" in window)) return Promise.resolve(null);
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("love-moments-meals", 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains("cache")) db.createObjectStore("cache");
      if (!db.objectStoreNames.contains("pending")) db.createObjectStore("pending", { keyPath: "id" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function mealCachePut(storeName, key, value) {
  const db = await openMealCache().catch(() => null); if (!db) return;
  await new Promise((resolve, reject) => { const tx = db.transaction(storeName, "readwrite"); tx.objectStore(storeName).put(value, key); tx.oncomplete = resolve; tx.onerror = () => reject(tx.error); });
  db.close();
}

async function mealCacheDelete(storeName, key) {
  const db = await openMealCache().catch(() => null); if (!db) return;
  await new Promise(resolve => { const tx = db.transaction(storeName, "readwrite"); tx.objectStore(storeName).delete(key); tx.oncomplete = resolve; tx.onerror = resolve; });
  db.close();
}

function mealUserCacheKey(key = "snapshot", userId = state.data?.user?.id) {
  return userId ? `${userId}:${key}` : "";
}

async function mealCacheGet(storeName, key) {
  const db = await openMealCache().catch(() => null); if (!db) return null;
  const value = await new Promise((resolve, reject) => { const request = db.transaction(storeName).objectStore(storeName).get(key); request.onsuccess = () => resolve(request.result || null); request.onerror = () => reject(request.error); }).catch(() => null);
  db.close(); return value;
}

async function queueMealMutation(path, method, body) {
  const userId = state.data?.user?.id; if (!userId) return;
  const db = await openMealCache().catch(() => null); if (!db) return;
  const value = { id: clientKey("offline-meal"), userId, path, method, body, createdAt: new Date().toISOString() };
  await new Promise((resolve, reject) => { const tx = db.transaction("pending", "readwrite"); tx.objectStore("pending").put(value); tx.oncomplete = resolve; tx.onerror = () => reject(tx.error); }); db.close();
}

async function flushMealMutations() {
  const userId = state.data?.user?.id;
  const db = await openMealCache().catch(() => null); if (!db || !navigator.onLine || !userId) return;
  const pending = await new Promise(resolve => { const request = db.transaction("pending").objectStore("pending").getAll(); request.onsuccess = () => resolve(request.result || []); request.onerror = () => resolve([]); });
  for (const item of pending.filter(value => value.userId === userId)) {
    try {
      await api(item.path, { method: item.method, body: JSON.stringify(item.body) });
      await new Promise(resolve => { const tx = db.transaction("pending", "readwrite"); tx.objectStore("pending").delete(item.id); tx.oncomplete = resolve; tx.onerror = resolve; });
    } catch (error) { if (error.status === 409 || error.status === 400 || error.status === 403 || error.status === 404) await new Promise(resolve => { const tx = db.transaction("pending", "readwrite"); tx.objectStore("pending").delete(item.id); tx.oncomplete = resolve; tx.onerror = resolve; }); else break; }
  }
  db.close();
}

async function clearMealOfflineData(userId) {
  if (!userId) return;
  localStorage.removeItem(`love-offline-meals:${userId}`);
  const db = await openMealCache().catch(() => null); if (!db) return;
  await new Promise(resolve => {
    const tx = db.transaction(["cache", "pending"], "readwrite");
    tx.objectStore("cache").delete(mealUserCacheKey("snapshot", userId));
    const request = tx.objectStore("pending").getAll();
    request.onsuccess = () => request.result.filter(item => item.userId === userId).forEach(item => tx.objectStore("pending").delete(item.id));
    tx.oncomplete = resolve; tx.onerror = resolve;
  });
  db.close();
}

function renderSmartSurface(surface, manifest, message = "") {
  const host = document.querySelector(`[data-smart="${surface}"]`);
  if (!host) return;
  if (!validManifest(manifest, surface)) {
    host.innerHTML = `<div class="smart-error">${escapeHtml(message || "智能编排暂时不可用，原页面功能不受影响。")}</div>`;
    return;
  }
  const nodes = [...manifest.sections, ...manifest.personalOverlay];
  const actionLabels = { send_message: "预览发送", select_trip_variant: "预览切换", create_moment: "预览保存", create_calendar_entry: "预览保存", create_wishlist_item: "预览添加" };
  host.innerHTML = `<div class="smart-heading"><span>✦</span><div class="grow"><strong>问恋屿</strong><small>只使用当前页面必要信息 · 写操作先预览</small></div></div>
    <form class="smart-intent" data-smart-form="${surface}"><input maxlength="500" aria-label="告诉恋屿怎样整理本页" value="${escapeHtml(state.smartIntentDrafts[surface] || "")}" placeholder="${escapeHtml(smartExamples[surface])}"><button>智能整理</button></form>
    <div class="smart-layout">${nodes.map(node => {
      const writeAction = (node.actions || []).find(action => action.requiresConfirmation);
      return `<article class="smart-node smart-node-open ${writeAction ? "smart-node-write" : "smart-node-read"} ${node.id.endsWith(":personal") ? "smart-personal" : ""}" data-smart-node="${escapeHtml(node.type)}" role="button" tabindex="0" aria-label="查看${escapeHtml(node.props?.title || node.type)}"><div><strong>${escapeHtml(node.props?.title || node.type)}</strong><small>${escapeHtml(node.props?.subtitle || "")}</small></div>${writeAction ? `<button data-smart-action="${escapeHtml(writeAction.id)}">${escapeHtml(actionLabels[writeAction.type] || "查看建议")}</button>` : `<button data-smart-open="${escapeHtml(node.type)}">查看内容</button>`}</article>`;
    }).join("")}</div>
    <div class="smart-meta"><span>${manifest.generatedBy === "ai" ? "AI 编排" : manifest.generatedBy === "cache" ? "最近缓存布局" : "稳定规则"}</span><span>共享结构 · 个人内容单独叠加</span></div>`;
}

async function loadSmartSurface(surface, { quiet = false } = {}) {
  if (!smartExamples[surface]) return;
  if (quiet && (surfaceHasDraftIntent(surface) || state.composingSmartSurfaces.has(surface) || (state.pinnedSmartSurfaces.has(surface) && manifestHasPendingWrite(state.manifests[surface])))) {
    renderSmartSurface(surface, state.manifests[surface]);
    return;
  }
  const cacheKey = smartCacheKey(surface);
  const cached = readLocalJson(cacheKey);
  if (!state.manifests[surface] && validManifest(cached, surface)) {
    const safeCached = offlineSafeManifest(cached);
    state.manifests[surface] = safeCached;
    saveManifestCache(surface, safeCached);
    renderSmartSurface(surface, safeCached);
  }
  try {
    const tripQuery = surface === "trip" && state.data?.trip?.id ? `?trip_id=${encodeURIComponent(state.data.trip.id)}` : "";
    const manifest = await api(`/api/v1/ui/${surface}${tripQuery}`);
    // A quiet refresh can begin before a composed write draft arrives.  Never
    // let that older response overwrite the newer draft that the user still
    // needs to preview and confirm.
    if (quiet && (surfaceHasDraftIntent(surface) || state.composingSmartSurfaces.has(surface) || (state.pinnedSmartSurfaces.has(surface) && manifestHasPendingWrite(state.manifests[surface])))) {
      renderSmartSurface(surface, state.manifests[surface]);
      return;
    }
    state.pinnedSmartSurfaces.delete(surface);
    state.manifests[surface] = manifest;
    saveManifestCache(surface, manifest);
    renderSmartSurface(surface, manifest);
  } catch (error) {
    if (!state.manifests[surface]) renderSmartSurface(surface, null, error.message);
    if (!quiet) toast(error.message);
  }
}

async function composeSmartSurface(surface, intent, button) {
  state.composingSmartSurfaces.add(surface);
  button.disabled = true; button.textContent = "整理中…";
  try {
    const immediateTravelDestination = surface === "trip" ? extractTravelDestination(intent) : "";
    if (immediateTravelDestination) {
      state.smartIntentDrafts[surface] = "";
      openTripComposer(immediateTravelDestination);
      toast(`已识别目的地“${immediateTravelDestination}”，请补充日期和预算后生成五套真实方案`);
      return;
    }
    let job = await api(`/api/v1/ui/${surface}/compose`, { method: "POST", body: JSON.stringify({ intent, resourceRefs: [], tripId: surface === "trip" ? state.data?.trip?.id : null }) });
    for (let attempt = 0; !job.manifest && attempt < 100; attempt += 1) {
      if (job.status === "failed") throw new Error("智能整理失败，已保留当前稳定页面");
      await new Promise(resolve => setTimeout(resolve, 250));
      job = await api(`/api/v1/ui/jobs/${job.id}`);
    }
    const manifest = job.manifest;
    if (!validManifest(manifest, surface)) throw new Error("智能清单校验失败");
    state.smartIntentDrafts[surface] = "";
    state.manifests[surface] = manifest;
    saveManifestCache(surface, manifest);
    if (manifestHasPendingWrite(manifest)) state.pinnedSmartSurfaces.add(surface);
    else state.pinnedSmartSurfaces.delete(surface);
    renderSmartSurface(surface, manifest);
    const pendingWrite = manifest.sections.flatMap(node => node.actions || []).find(action => action.requiresConfirmation);
    if (pendingWrite?.type === "create_calendar_entry") toast("纪念日保存预览已生成，请点“预览保存”确认");
    else if (pendingWrite?.type === "create_wishlist_item") toast("共同愿望预览已生成，请确认后保存");
    else if (pendingWrite?.type === "create_moment") toast("共同瞬间预览已生成，请确认后保存");
    else if (pendingWrite?.type === "select_trip_variant") toast("旅行方案切换预览已生成，请确认后采用");
    else if (surface === "life" && /(?:添加|新增|创建|记录|保存|安排).*纪念日|纪念日.*(?:添加|新增|创建|记录|保存|安排)/.test(intent)) {
      openCalendarComposer("anniversary");
      toast("还需要标题和有效日期，请填写后保存");
    } else toast(job.errorCode ? "模型暂不可用，已用稳定规则完成" : "页面已按你的意图重新整理，可点卡片查看内容");
  } catch (error) { toast(error.message); }
  finally {
    state.composingSmartSurfaces.delete(surface);
    button.disabled = false; button.textContent = "智能整理";
  }
}

async function previewSmartAction(actionId) {
  let result;
  try {
    result = await api(`/api/v1/ui/actions/${actionId}/preview`, { method: "POST", body: "{}" });
  } catch (error) {
    if ([400, 404, 410].includes(error.status)) {
      removeSmartActionFromManifests(actionId);
      await loadSmartSurface(state.screen, { quiet: false });
      throw new Error("这条建议已经过期，页面已刷新，请重新智能整理");
    }
    throw error;
  }
  state.smartActionId = actionId;
  state.smartActionType = result.type;
  const preview = result.preview || {};
  document.getElementById("smartConfirmTitle").textContent = preview.title || "确认智能建议";
  document.getElementById("smartActionPreview").innerHTML = `<span>修改前</span><strong>${escapeHtml(preview.before || "无")}</strong><span>修改后</span><strong>${escapeHtml(preview.after || "")}</strong><p>${escapeHtml(preview.impact || "确认后立即执行")}</p>`;
  openModal("smartConfirmModal");
}

function openMomentComposer(label = "回忆") {
  const typeMap = { "照片": "photo", "文字": "text", "语音": "voice", "位置": "location", "回忆": "text" };
  state.editingMomentId = null;
  state.momentType = typeMap[label] || "text";
  document.getElementById("momentModalTitle").textContent = "记录这一刻";
  document.getElementById("momentSubmitButton").textContent = "保存到我们的时光";
  document.getElementById("momentTitle").placeholder = `给这段${label}一个名字`;
  document.getElementById("momentTitle").value = "";
  document.getElementById("momentNote").value = "";
  document.getElementById("momentOccurredAt").value = datetimeLocalValue(new Date());
  document.getElementById("momentLocation").value = "";
  document.getElementById("momentMood").value = "";
  document.getElementById("momentMedia").value = "";
  document.getElementById("momentMediaField").hidden = false;
  document.getElementById("momentRevealAt").value = "";
  setMomentPrivacy("双方可见");
  openModal("momentModal");
}

function datetimeLocalValue(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const part = number => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${part(date.getMonth() + 1)}-${part(date.getDate())}T${part(date.getHours())}:${part(date.getMinutes())}`;
}

function setMomentPrivacy(value) {
  state.privacy = value;
  document.querySelectorAll("#momentModal [data-privacy]").forEach(button => button.classList.toggle("active", button.dataset.privacy === value));
  document.getElementById("revealField").hidden = value !== "延时公开";
}

function openMomentEditor(moment) {
  if (!moment || moment.creator_id !== state.data?.user?.id) {
    toast("只能修改自己发布的内容");
    return;
  }
  state.editingMomentId = moment.id;
  state.momentType = moment.type;
  document.getElementById("momentModalTitle").textContent = "修改这段回忆";
  document.getElementById("momentSubmitButton").textContent = "保存修改";
  document.getElementById("momentTitle").value = moment.title || "";
  document.getElementById("momentNote").value = moment.body || "";
  document.getElementById("momentOccurredAt").value = datetimeLocalValue(moment.occurred_at);
  document.getElementById("momentLocation").value = moment.location || "";
  document.getElementById("momentMood").value = moment.mood || "";
  document.getElementById("momentMedia").value = "";
  document.getElementById("momentMediaField").hidden = true;
  document.getElementById("momentRevealAt").value = moment.reveal_at ? datetimeLocalValue(moment.reveal_at) : "";
  setMomentPrivacy(moment.visibility === "private" ? "仅自己" : moment.visibility === "delayed" ? "延时公开" : "双方可见");
  openModal("momentModal");
}

function openCalendarComposer(entryType = "event") {
  const form = document.getElementById("calendarForm");
  form.reset(); delete form.dataset.itemId; delete form.dataset.version;
  document.getElementById("calendarModalTitle").textContent = entryType === "anniversary" ? "添加纪念日" : entryType === "todo" ? "添加双人待办" : "添加共同事项";
  document.getElementById("calendarType").value = entryType;
  document.getElementById("calendarStart").value = new Date().toISOString().slice(0, 10);
  document.getElementById("calendarRecurrence").value = entryType === "anniversary" ? "yearly" : "none";
  openModal("calendarModal");
}

function extractTravelDestination(intent = "") {
  const text = String(intent).replace(/[，。！？,.!?]/g, " ").replace(/\s+/g, " ").trim();
  const patterns = [
    /(?:想|计划|准备|打算)?\s*去\s*([A-Za-z\u4e00-\u9fa5·]{2,16}?)(?:玩|旅游|旅行|度假|逛逛|看看|\s|$)/i,
    /(?:目的地(?:是|为)?|前往)\s*[:：]?\s*([A-Za-z\u4e00-\u9fa5·]{2,16})(?:\s|$)/i
  ];
  for (const pattern of patterns) {
    const candidate = text.match(pattern)?.[1]?.trim();
    if (candidate && !["哪里", "哪儿", "一个地方", "旅行"].includes(candidate)) return candidate;
  }
  return "";
}

function renderDestinationSelection(place = null, candidateId = null) {
  const host = document.getElementById("tripDestinationSelection");
  if (!host) return;
  if (!place) { host.innerHTML = ""; return; }
  const source = ({ amap: "高德地点", google_maps: "Google Maps 地点", manual: "本人确认范围" })[place.provider] || "已确认地点";
  const area = [place.country, place.province, place.city, place.district, place.formatted_address].filter(Boolean).join(" · ");
  host.innerHTML = `<div class="confirmed-place"><strong>✓ 已确认：${escapeHtml(place.name)}</strong><br>${escapeHtml(area || "没有详细地址")} · ${escapeHtml(source)}</div>`;
  state.destinationCandidateId = candidateId;
  state.destinationExistingPlace = candidateId ? null : place;
}

function clearDestinationResolution({ cancelSearch = false } = {}) {
  if (cancelSearch) {
    clearTimeout(state.destinationSearchTimer);
    state.destinationSearchTimer = null;
    state.destinationSearchSequence += 1;
  }
  state.destinationCandidateId = null;
  state.destinationExistingPlace = null;
  state.destinationCandidates = [];
  const list = document.getElementById("tripDestinationCandidates");
  if (list) { list.innerHTML = ""; list.hidden = true; }
  document.getElementById("tripDestination")?.setAttribute("aria-expanded", "false");
  renderDestinationSelection();
}

function renderDestinationCandidates(result) {
  state.destinationCandidates = result.items || [];
  const host = document.getElementById("tripDestinationCandidates");
  const input = document.getElementById("tripDestination");
  host.hidden = false;
  input.setAttribute("aria-expanded", "true");
  const providers = new Set(state.destinationCandidates.map(item => item.provider));
  const caption = providers.has("google_maps") ? "高德与全球地图建议" : providers.has("amap") ? "高德地点建议" : "可确认的地点范围";
  host.innerHTML = state.destinationCandidates.length ? `<div class="destination-candidate-caption"><strong>${caption}</strong><span>点击一项确认</span></div>` + state.destinationCandidates.map(item => {
    const area = [...new Set([item.country, item.province, item.city, item.district, item.formatted_address].filter(Boolean))].join(" · ");
    const source = ({ amap: "高德", google_maps: "Google Maps", manual: "本人确认" })[item.provider] || item.provider;
    return `<button type="button" class="destination-candidate" role="option" data-action="select-destination" data-candidate-id="${escapeHtml(item.candidate_id)}"><span class="destination-candidate-pin" aria-hidden="true">⌖</span><span class="destination-candidate-copy"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(area || "没有详细地址")}</small></span><span class="destination-source-badge">${escapeHtml(source)}</span></button>`;
  }).join("") : `<div class="destination-search-empty">${escapeHtml(result.notice || "没有查到候选地点，请补充城市或国家后重试。")}</div>`;
}

function destinationSearchStatus(message) {
  const node = document.getElementById("tripDestinationSearchState");
  if (node) node.textContent = message;
}

async function searchDestinationCandidates({ includeGlobal = false, announce = false } = {}) {
  const query = document.getElementById("tripDestination").value.trim();
  const regionHint = document.getElementById("tripDestinationRegion").value.trim();
  if (query.length < 2) {
    clearDestinationResolution({ cancelSearch: true });
    destinationSearchStatus("输入两个字，高德会自动推荐具体地点");
    return;
  }
  const cacheKey = `${query}|${regionHint}|${includeGlobal ? "global" : "amap"}`;
  const cached = state.destinationSearchCache.get(cacheKey);
  if (cached && Date.now() - cached.savedAt < 10 * 60 * 1000) {
    renderDestinationCandidates(cached.result);
    destinationSearchStatus(`已找到 ${cached.result.items?.length || 0} 个候选 · 来自缓存`);
    return;
  }
  const sequence = ++state.destinationSearchSequence;
  const host = document.getElementById("tripDestinationCandidates");
  host.hidden = false;
  host.innerHTML = `<div class="destination-search-loading">${includeGlobal ? "正在搜索国内外地点" : "高德正在查找地点"}</div>`;
  document.getElementById("tripDestination").setAttribute("aria-expanded", "true");
  destinationSearchStatus(includeGlobal ? "正在搜索国内外地点…" : "高德正在联想…");
  try {
    const result = await api("/api/v1/places/search", { method: "POST", body: JSON.stringify({ query, region_hint: regionHint, include_global: includeGlobal }) });
    if (sequence !== state.destinationSearchSequence) return;
    state.destinationSearchCache.set(cacheKey, { result, savedAt: Date.now() });
    renderDestinationCandidates(result);
    destinationSearchStatus(result.items?.length ? `找到 ${result.items.length} 个候选，请点选具体地点` : "没有找到，补充城市或国家后再试");
    if (announce) toast(result.items?.length ? `找到 ${result.items.length} 个候选，请点选具体地点` : (result.notice || "没有找到候选地点"));
  } catch (error) {
    if (sequence !== state.destinationSearchSequence) return;
    host.hidden = false;
    host.innerHTML = `<div class="destination-search-empty">${escapeHtml(error.message)}</div>`;
    destinationSearchStatus("地点搜索暂时不可用，可以稍后重试");
    if (announce) toast(error.message);
  }
}

function scheduleDestinationSuggestions(delay = 500) {
  clearTimeout(state.destinationSearchTimer);
  state.destinationSearchTimer = null;
  const query = document.getElementById("tripDestination")?.value.trim() || "";
  if (query.length < 2) {
    destinationSearchStatus("输入两个字，高德会自动推荐具体地点");
    return;
  }
  destinationSearchStatus("继续输入，稍后自动搜索高德…");
  state.destinationSearchTimer = setTimeout(() => {
    state.destinationSearchTimer = null;
    searchDestinationCandidates({ includeGlobal: false }).catch(() => {});
  }, delay);
}

function openTripComposer(destination = "", editingTrip = null) {
  const form = document.getElementById("tripForm");
  form.reset(); clearDestinationResolution({ cancelSearch: true });
  if (editingTrip) form.dataset.tripId = editingTrip.id; else delete form.dataset.tripId;
  const start = new Date(Date.now() + 30 * 86400000);
  const end = new Date(start.getTime() + 2 * 86400000);
  document.getElementById("tripModalTitle").textContent = editingTrip ? "确认旅行地点并重新生成" : "创建一段新旅行";
  document.getElementById("tripDestination").value = editingTrip?.destination || destination;
  const me = state.data?.coupleContext?.members?.find(member => member.is_me);
  document.getElementById("tripDepartureCity").value = editingTrip?.departure_city || me?.city || "";
  const unresolvedLegacyPlace = Boolean(editingTrip?.needs_destination_detail && !editingTrip?.destination_place);
  document.getElementById("tripArrivalCity").value = unresolvedLegacyPlace ? "" : (editingTrip?.arrival_city || "");
  document.getElementById("tripDestinationRegion").value = editingTrip?.destination_place?.city
    || (unresolvedLegacyPlace ? "" : (editingTrip?.arrival_city || ""));
  document.getElementById("tripStart").value = editingTrip?.start_date?.slice(0, 10) || start.toISOString().slice(0, 10);
  document.getElementById("tripEnd").value = editingTrip?.end_date?.slice(0, 10) || end.toISOString().slice(0, 10);
  document.getElementById("tripOutboundTime").value = editingTrip?.outbound_time?.slice(11, 16) || "";
  document.getElementById("tripReturnTime").value = editingTrip?.return_time?.slice(11, 16) || "";
  document.getElementById("tripTravelers").value = String(editingTrip?.travelers || 2);
  document.getElementById("tripRooms").value = String(editingTrip?.rooms || 1);
  document.getElementById("tripHotelBudget").value = String(Number(editingTrip?.hotel_budget_cents || 0) / 100);
  document.getElementById("tripBudget").value = String(Number(editingTrip?.budget_cents || 400000) / 100);
  for (const id of ["tripStart", "tripEnd", "tripBudget"]) document.getElementById(id).disabled = Boolean(editingTrip);
  form.querySelector("button.primary-button").textContent = editingTrip ? "保存地点并重新生成五套方案" : "生成五套旅行方案";
  if (editingTrip?.destination_place) renderDestinationSelection(editingTrip.destination_place);
  navigate("trip");
  setTripSection("overview", { scroll: false });
  openModal("tripModal");
  if (!editingTrip?.destination_place && document.getElementById("tripDestination").value.trim().length >= 2) scheduleDestinationSuggestions(120);
}

function scrollToContent(selector) {
  requestAnimationFrame(() => document.querySelector(selector)?.scrollIntoView({ behavior: "smooth", block: "center" }));
}

function setTripSection(section = "overview", { scroll = true } = {}) {
  const allowed = ["overview", "assistant", "plans", "itinerary", "stays", "tools"];
  state.tripSection = allowed.includes(section) ? section : "overview";
  localStorage.setItem("love-trip-section", state.tripSection);
  document.querySelectorAll("[data-trip-section]").forEach(button => button.classList.toggle("active", button.dataset.tripSection === state.tripSection));
  document.querySelectorAll("[data-trip-panel]").forEach(panel => { panel.hidden = panel.dataset.tripPanel !== state.tripSection; });
  const sticky = document.querySelector(".sticky-trip-actions");
  if (sticky) sticky.hidden = state.tripSection !== "itinerary";
  if (state.tripSection === "assistant") {
    renderTripLive();
    loadTripAssistant({ quiet: true }).catch(error => toast(error.message));
  }
  if (scroll) document.querySelector('[data-screen="trip"] .content')?.scrollIntoView({ block: "start" });
}

function setLifeSection(section = "overview", { scroll = true } = {}) {
  const allowed = ["overview", "meals", "wishlist", "calendar", "anniversary", "todo", "preference", "dates"];
  state.lifeSection = allowed.includes(section) ? section : "overview";
  localStorage.setItem("love-life-section", state.lifeSection);
  document.querySelectorAll("[data-life-section]").forEach(button => button.classList.toggle("active", button.dataset.lifeSection === state.lifeSection));
  document.querySelectorAll("[data-life-panel]").forEach(panel => {
    panel.hidden = !String(panel.dataset.lifePanel || "").split(/\s+/).includes(state.lifeSection);
  });
  const copy = {
    overview: ["我们", "把想做的事，一件件变成回忆"],
    meals: ["今天想吃", "点菜、安排三餐、查看做法并生成采购清单"],
    wishlist: ["愿望清单", "共同添加、编辑，并把完成的愿望沉淀成回忆"],
    calendar: ["共享日历", "查看和管理两个人的共同安排"],
    anniversary: ["纪念日", "重要日期会按年展示，也能随时编辑"],
    todo: ["双人待办", "一起完成生活里的每一件小事"],
    preference: ["懂你备忘", "保存口味、尺码、过敏和礼物偏好"],
    dates: ["智能约会", "五套方案、双方投票和共同确认"]
  };
  const [title, subtitle] = copy[state.lifeSection];
  const pageTitle = document.getElementById("lifePageTitle");
  const pageSubtitle = document.getElementById("lifePageSubtitle");
  if (pageTitle) pageTitle.textContent = title;
  if (pageSubtitle) pageSubtitle.textContent = subtitle;
  const recordCopy = {
    calendar: ["SHARED CALENDAR", "共享日历", "只显示共同事件与安排。", "event"],
    anniversary: ["OUR MILESTONES", "纪念日", "重要日期单独归档并按年提醒。", "anniversary"],
    todo: ["TOGETHER LIST", "双人待办", "查看待完成和已经完成的事项。", "todo"],
    preference: ["KNOW YOU", "懂你备忘", "记录两个人的口味、尺码和礼物偏好。", "preference"]
  };
  if (recordCopy[state.lifeSection]) {
    const [eyebrow, heading, description, filter] = recordCopy[state.lifeSection];
    document.getElementById("lifeSectionEyebrow").textContent = eyebrow;
    document.getElementById("lifeSectionTitle").textContent = heading;
    document.getElementById("lifeSectionDescription").textContent = description;
    const add = document.getElementById("lifeSectionAdd");
    add.dataset.action = filter === "preference" ? "open-preferences" : "open-calendar";
    if (filter === "preference") delete add.dataset.entryType;
    else add.dataset.entryType = filter;
    state.lifeFilter = filter;
    renderLifeData();
  }
  if (state.lifeSection === "meals") {
    setMealSection(state.mealSection, { load: false });
    if (!state.meals.recipes.length) loadMeals({ quiet: true });
  }
  if (scroll) document.querySelector('[data-screen="life"] .content')?.scrollIntoView({ block: "start" });
}

function setLifeFilter(filter = "all") {
  const sections = { event: "calendar", anniversary: "anniversary", todo: "todo", preference: "preference", all: "overview" };
  setLifeSection(sections[filter] || "overview");
}

async function setTimelineView(view = "list", { scroll = true } = {}) {
  state.timelineView = ["list", "album", "map", "report", "private", "trash"].includes(view) ? view : "list";
  localStorage.setItem("love-timeline-view", state.timelineView);
  document.querySelectorAll('[data-screen="timeline"] [data-view]').forEach(button => button.classList.toggle("active", button.dataset.view === state.timelineView));
  const moreViews = new Set(["report", "private", "trash"]);
  const moreMenu = document.querySelector('[data-screen="timeline"] .timeline-more');
  if (moreMenu) moreMenu.open = moreViews.has(state.timelineView);
  const renderToken = ++state.timelineRenderToken;
  await renderTimelineView(state.timelineView, renderToken);
  if (scroll) scrollToContent("#timelineView");
}

async function openSmartNode(type) {
  const timelineViews = { featured_memory: "list", timeline_summary: "list", month_album: "album", footprint_summary: "map", year_report: "report", privacy_status: "private" };
  const lifeSections = { calendar_summary: "calendar", anniversary: "anniversary", todo_summary: "todo", pending_items: "todo", preference_summary: "preference" };
  const mealSections = { recipe_library: "recipes", recipe_detail: "recipes", meal_wishlist: "wishes", meal_cart: "calendar", meal_calendar: "calendar", meal_day_detail: "calendar", meal_archive: "calendar", shopping_list: "shopping" };
  if (timelineViews[type]) { navigate("timeline", { restoreTimeline: false }); await setTimelineView(timelineViews[type]); return; }
  if (type === "wishlist") { navigate("life"); setLifeSection("wishlist"); return; }
  if (mealSections[type]) { navigate("life"); setLifeSection("meals"); setMealSection(mealSections[type]); return; }
  if (lifeSections[type]) { navigate("life"); setLifeSection(lifeSections[type]); return; }
  if (["trip_countdown", "trip_share", "trip_variant_compare", "itinerary_timeline", "budget_summary", "hotel_quote", "risk_alert", "impact_diff", "approval_state"].includes(type)) {
    navigate("trip");
    const section = type === "hotel_quote" ? "stays" : ["itinerary_timeline", "risk_alert", "impact_diff", "approval_state"].includes(type) ? "itinerary" : ["trip_variant_compare", "budget_summary"].includes(type) ? "plans" : "overview";
    setTripSection(section); return;
  }
  if (["message_summary", "favorite_memory"].includes(type)) { navigate("chat"); scrollToContent("#messages"); return; }
  if (type === "suggested_reply") { navigate("chat"); requestAnimationFrame(() => document.getElementById("chatInput")?.focus()); return; }
  if (type === "import_guide") { state.chatImport = null; document.getElementById("chatImportForm").hidden = false; document.getElementById("chatImportDraft").hidden = true; openModal("chatImportModal"); return; }
  if (type === "quick_capture") { openMomentComposer("文字"); return; }
  if (type === "daily_question") { navigate("home"); scrollToContent(".question-card"); return; }
  if (type === "couple_overview") { navigate("home"); scrollToContent("#coupleContextGrid"); return; }
  navigate(state.screen || "home");
}

function renderMessages() {
  if (!state.data) return;
  let previousDay = "";
  const today = new Date();
  const yesterday = new Date(Date.now() - 86400000);
  const localDay = value => new Date(value).toLocaleDateString("sv-SE");
  const todayKey = localDay(today); const yesterdayKey = localDay(yesterday);
  const sameMessageGroup = (left, right) => {
    if (!left || !right || left.type === "trip" || right.type === "trip") return false;
    const leftTime = new Date(left.sent_at).getTime();
    const rightTime = new Date(right.sent_at).getTime();
    return Boolean(left.mine) === Boolean(right.mine)
      && localDay(left.sent_at) === localDay(right.sent_at)
      && Number.isFinite(leftTime) && Number.isFinite(rightTime)
      && Math.abs(rightTime - leftTime) <= 5 * 60 * 1000;
  };
  const html = state.data.messages.map((message, index, messages) => {
    const day = localDay(message.sent_at);
    const divider = day === previousDay ? "" : `<div class="date-divider">${day === todayKey ? "今天" : day === yesterdayKey ? "昨天" : new Date(message.sent_at).toLocaleDateString("zh-CN", { year: "numeric", month: "long", day: "numeric" })}</div>`;
    previousDay = day;
    const groupStart = !sameMessageGroup(messages[index - 1], message);
    const groupEnd = !sameMessageGroup(message, messages[index + 1]);
    const groupClasses = `${groupStart ? " message-group-start" : ""}${groupEnd ? " message-group-end" : ""}`;
    if (message.type === "trip") {
      const linkedTrip = (state.data.trips || []).find(item => item.id === message.metadata?.tripId)
        || (state.data.trips || []).find(item => String(message.content).includes(item.destination));
      const action = linkedTrip ? `<button data-nav="trip" data-trip-id="${escapeHtml(linkedTrip.id)}">查看对应旅行</button>` : `<small class="message-link-missing">这张旧卡片没有关联到现存旅行</small>`;
      return `${divider}<div class="message theirs trip-share message-group-start message-group-end"><span class="pill rose">旅行卡片</span><strong>${escapeHtml(message.content)}</strong><small>${escapeHtml(message.metadata?.subtitle || "共同旅行计划")}</small>${action}</div>`;
    }
    const mediaUrl = message.metadata?.mediaId ? `/api/v1/media/${encodeURIComponent(message.metadata.mediaId)}` : "";
    const imagePreviewUrl = mediaUrl ? `${mediaUrl}?thumbnail=1` : "";
    const media = message.type === "image" && mediaUrl ? `<img class="message-media" src="${imagePreviewUrl}" alt="${escapeHtml(message.content)}" loading="lazy">`
      : message.type === "voice" && mediaUrl ? `<audio controls preload="metadata" src="${mediaUrl}"></audio>`
      : message.type === "video" && mediaUrl ? `<video class="message-media" controls preload="metadata" poster="${imagePreviewUrl}" src="${mediaUrl}"></video>` : "";
    const status = message.mine ? `<small class="message-status">${message.delivery_status === "read" ? "已读" : "已送达"}</small>` : "";
    const meta = groupEnd ? `<span class="message-meta"><time>${timeLabel(message.sent_at)}</time>${status}</span>` : "";
    const side = message.mine ? "mine" : "theirs";
    return `${divider}<div class="message-row ${side}${groupClasses}"><div class="message ${side}" data-message-id="${escapeHtml(message.id)}" title="长按收藏为瞬间">${media}<p>${escapeHtml(message.content)}</p></div>${meta}</div>`;
  }).join("");
  document.getElementById("messages").innerHTML = html || `<div class="empty-view"><p>还没有消息，发一句“想你了”开始聊天吧。</p></div>`;
}

function renderIdentity() {
  if (!state.data?.user || !state.data?.partner) return;
  const { user, partner, couple } = state.data;
  applyAvatarIdentity(".avatar-a", user, "当前账号");
  applyAvatarIdentity(".avatar-b", partner, "伴侣");
  const avatarPair = document.querySelector(".avatar-pair");
  if (avatarPair) avatarPair.setAttribute("aria-label", `当前登录：${user.nickname}（${user.username}），伴侣：${partner.nickname}，查看情侣空间`);
  document.querySelector('[data-screen="chat"] h1').textContent = partner.nickname;
  document.querySelector(".couple-card h2").textContent = `${user.nickname} & ${partner.nickname}`;
  document.querySelector(".couple-card p").textContent = `相遇于 ${couple.anniversary} · 属于你们的共同空间`;
  document.querySelector("#callScreen h2").textContent = partner.nickname;
  document.getElementById("partnerMomentLabel").textContent = `${partner.nickname}的此刻 · 当前账号：${user.nickname}`;
  const anniversary = new Date(`${couple.anniversary}T00:00:00`);
  const days = Math.max(0, Math.floor((Date.now() - anniversary.getTime()) / 86400000));
  document.getElementById("togetherDays").textContent = days.toLocaleString("zh-CN");
  document.getElementById("storyYear").textContent = `OUR STORY · ${new Date().getFullYear()}`;
  document.getElementById("todayLabel").textContent = new Date().toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric" }).toUpperCase();
  const trip = state.data.trip;
  const styleLabels = { slow: "松弛浪漫", classic: "经典充实", value: "性价比", food: "美食寻味", nature: "自然治愈" };
  const switcher = document.getElementById("tripSwitcher");
  const trips = state.data.trips || (trip ? [trip] : []);
  switcher.innerHTML = trips.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)} · ${escapeHtml(String(item.start_date).slice(0, 10))}</option>`).join("");
  if (trip) switcher.value = trip.id;
  switcher.closest(".trip-switcher").hidden = trips.length < 2;
  const deleteTripButton = document.getElementById("deleteTripButton");
  if (deleteTripButton) deleteTripButton.hidden = !trip;
  const editTripButton = document.getElementById("editTripButton");
  if (editTripButton) editTripButton.hidden = !trip;
  const tripDetailButton = document.getElementById("tripDetailButton");
  if (tripDetailButton) { tripDetailButton.hidden = !trip; tripDetailButton.dataset.id = trip?.id || ""; }
  const destinationWarning = document.getElementById("tripDestinationWarning");
  if (trip) {
    document.getElementById("tripTitle").textContent = trip.title;
    const conflictText = trip.date_conflicts?.length ? ` · ⚠ 与 ${trip.date_conflicts.map(item => item.title).join("、")} 日期重叠` : "";
    const destinationText = trip.needs_destination_detail ? " · ⚠ 具体地点尚未确认" : "";
    document.getElementById("tripMeta").textContent = `${shortDate(trip.start_date)} — ${shortDate(trip.end_date)} · ${money(trip.budget_cents)}${conflictText}${destinationText}`;
    const remaining = Math.ceil((new Date(`${trip.start_date}T00:00:00`).getTime() - Date.now()) / 86400000);
    document.getElementById("nextTripSummary").textContent = remaining >= 0 ? `下一站：${trip.destination} · 还有 ${remaining} 天` : `共同回忆：${trip.destination}`;
    document.getElementById("homeTripName").textContent = trip.title;
    document.getElementById("homeTripStatus").textContent = trip.status === "confirmed" ? "双方已确认" : `当前方案 · ${styleLabels[trip.active_variant] || "待选择"}`;
    prefillQuoteForms(trip);
    if (destinationWarning) {
      destinationWarning.hidden = !trip.needs_destination_detail;
      destinationWarning.innerHTML = trip.needs_destination_detail
        ? `<span class="destination-warning-icon" aria-hidden="true">⌖</span><div class="destination-warning-copy"><span>目的地待确认</span><strong>“${escapeHtml(trip.destination)}”可能对应多个地点</strong><small>从高德候选中点选具体地址，再按唯一地点重新规划。</small></div><button data-action="edit-trip">选择地图地点 ›</button>`
        : "";
    }
  } else {
    document.getElementById("tripTitle").textContent = "还没有选择旅行";
    document.getElementById("tripMeta").textContent = "创建后会显示真实日期、预算与冲突提醒";
    document.getElementById("nextTripSummary").textContent = "还没有旅行计划";
    document.getElementById("homeTripName").textContent = "旅行计划";
    document.getElementById("homeTripStatus").textContent = "创建第一次旅行";
    if (destinationWarning) destinationWarning.hidden = true;
  }
  const anniversaries = (state.data.calendar || []).filter(item => item.entry_type === "anniversary");
  const candidates = [{ title: "恋爱纪念日", starts_at: couple.anniversary }, ...anniversaries]
    .map(item => ({ ...item, next: nextYearlyDate(item.starts_at) }))
    .filter(item => item.next)
    .sort((left, right) => left.next - right.next);
  const nextAnniversary = candidates[0];
  if (nextAnniversary) {
    const remaining = Math.max(0, Math.ceil((nextAnniversary.next.getTime() - new Date().setHours(0, 0, 0, 0)) / 86400000));
    document.getElementById("homeAnniversaryName").textContent = nextAnniversary.title;
    document.getElementById("homeAnniversaryCountdown").textContent = remaining === 0 ? "就是今天" : `还有 ${remaining} 天`;
  }
  const moments = state.data.moments || [];
  const cities = new Set(moments.map(moment => moment.location).filter(Boolean));
  document.getElementById("momentCount").textContent = moments.length;
  document.getElementById("wishCount").textContent = state.data.wishlist?.items?.length || 0;
  const wishes = state.data.wishlist?.items || [];
  document.getElementById("lifeWishlistCount").textContent = `完成 ${wishes.filter(item => item.completed_at).length} / ${wishes.length}`;
  document.getElementById("cityCount").textContent = cities.size;
}

function renderCoupleContext() {
  const context = state.data?.coupleContext;
  if (!context?.members?.length) return;
  const me = context.members.find(member => member.is_me);
  const partner = context.members.find(member => !member.is_me);
  const statusText = partner?.status_text || "还没有分享状态";
  const statusAge = partner?.status_age_minutes == null ? "" : partner.status_age_minutes < 60
    ? `${Math.max(1, partner.status_age_minutes)} 分钟前更新`
    : partner.status_age_minutes < 1440 ? `${Math.floor(partner.status_age_minutes / 60)} 小时前更新` : `${Math.floor(partner.status_age_minutes / 1440)} 天前更新`;
  document.getElementById("partnerStatusText").textContent = `${partner?.emoji || "♡"} ${statusText}`;
  document.getElementById("partnerContextText").textContent = partner?.status_text
    ? `${partner.city || "未设置城市"} · ${statusAge}${partner.status_stale ? " · 状态可能已过期" : ""}`
    : (partner?.city ? `${partner.city} · 当地 ${timeLabel(partner.local_time)}` : "点这里也分享你的状态");
  document.querySelector(".weather").textContent = partner?.weather ? `${partner.city} ${partner.weather.temperature}° · ${partner.weather.label}` : (partner?.city ? `${partner.city} · 天气待同步` : "设置城市后同步天气");
  document.getElementById("coupleContextGrid").innerHTML = context.members.map(member => {
    const weather = member.weather ? `${member.weather.temperature}° ${member.weather.label}` : "天气待同步";
    const statusMeta = member.status_age_minutes == null ? "" : member.status_age_minutes < 60 ? `${Math.max(1, member.status_age_minutes)} 分钟前` : member.status_age_minutes < 1440 ? `${Math.floor(member.status_age_minutes / 60)} 小时前` : `${Math.floor(member.status_age_minutes / 1440)} 天前`;
    return `<article class="context-person" ${member.is_me ? 'data-action="edit-status" role="button" tabindex="0"' : ""}><strong>${escapeHtml(member.nickname)} · ${escapeHtml(member.city || "未设置城市")}</strong><span>当地 ${timeLabel(member.local_time)}</span><small>${escapeHtml(weather)}${member.status_text ? ` · ${escapeHtml(member.emoji || "")} ${escapeHtml(member.status_text)}（${escapeHtml(statusMeta)}${member.status_stale ? "，可能已过期" : ""}）` : ""}</small></article>`;
  }).join("");
  if (me?.city) {
    document.getElementById("locationCity").value = me.city;
    document.getElementById("locationTimezone").value = me.timezone || "Asia/Shanghai";
  }
}

function renderWeeklyReview() {
  const review = state.data?.weeklyReview;
  const host = document.getElementById("weeklyPreview");
  if (!review) { host.hidden = true; return; }
  host.hidden = false;
  const summary = review.summary || {};
  host.innerHTML = `<span class="eyebrow ink">OUR WEEK</span><h3>这一周，我们又多了一些故事</h3><p>${summary.momentCount || 0} 个瞬间 · ${summary.completedCount || 0} 件共同小事 · ${summary.callCount || 0} 次通话</p><div class="inline-actions"><button data-action="open-weekly">${review.both_revealed ? "查看共同答案" : review.answered_by_me ? "等待 TA 回答" : "写下我的回顾"}</button></div>`;
  const metrics = `<div class="weekly-metrics"><span><b>${summary.momentCount || 0}</b>瞬间</span><span><b>${summary.completedCount || 0}</b>完成</span><span><b>${Math.round((summary.callSeconds || 0) / 60)}</b>通话分钟</span></div>`;
  const answers = (review.answers || []).map(answer => answer.locked
    ? `<div class="revealed-answer">${escapeHtml(answer.nickname)} 已写好，等你提交后一起揭晓。</div>`
    : `<div class="revealed-answer"><b>${escapeHtml(answer.nickname)}</b><br>喜欢：${escapeHtml(answer.favorite || "")}<br>感谢：${escapeHtml(answer.gratitude || "")}<br>下周：${escapeHtml(answer.next_week || "")}</div>`).join("");
  document.getElementById("weeklySummary").innerHTML = metrics + answers;
  const own = (review.answers || []).find(answer => answer.user_id === state.data.user.id && !answer.locked);
  document.getElementById("weeklyFavorite").value = own?.favorite || "";
  document.getElementById("weeklyGratitude").value = own?.gratitude || "";
  document.getElementById("weeklyNext").value = own?.next_week || "";
  document.getElementById("weeklyForm").hidden = review.status === "confirmed";
  const confirm = document.getElementById("weeklyConfirm");
  confirm.hidden = review.status === "confirmed" || !review.answered_by_me;
  confirm.disabled = Boolean(own?.confirmed_at);
  confirm.textContent = own?.confirmed_at ? "已确认，等待 TA" : "确认本周回顾";
}

function renderDatePlans() {
  const plans = state.data?.datePlans || [];
  const host = document.getElementById("datePlanList");
  const preview = document.getElementById("datePreview");
  if (!plans.length) {
    host.innerHTML = "";
    preview.innerHTML = `<span class="eyebrow ink">NEXT DATE</span><h3>下一次约会，想一起做什么？</h3><p>填写时间、城市和预算，恋屿会准备五套可投票方案。</p><div class="inline-actions"><button data-action="new-date">开始计划</button></div>`;
    return;
  }
  const current = plans[0];
  preview.innerHTML = `<span class="eyebrow ink">NEXT DATE</span><h3>${escapeHtml(current.title)}</h3><p>${escapeHtml(current.city)} · ${shortDate(current.plan_date)} · ${escapeHtml(current.status)}</p><div class="inline-actions"><button data-life-section="dates">查看方案与投票</button></div>`;
  host.innerHTML = plans.map(plan => {
    const variants = (plan.variants || []).map(variant => `<div class="date-variant"><div><strong>${escapeHtml(variant.name)} · ${money(variant.expected_cost_cents)}</strong><small>${escapeHtml(variant.summary)} · ${escapeHtml(variant.intensity)}</small></div>${plan.status === "voting" ? `<button class="${plan.my_vote === variant.id ? "selected" : ""}" data-action="vote-date" data-date-id="${plan.id}" data-variant-id="${variant.id}">${plan.my_vote === variant.id ? "已投票" : "投这套"}</button>` : ""}</div>`).join("");
    let actions = "";
    if (plan.status === "voting" && plan.my_vote) actions = `<button data-action="compose-date" data-date-id="${plan.id}" data-variant-id="${plan.my_vote}">采用我选的方案</button>`;
    if (plan.status === "pending_confirmation") actions = `<button data-action="confirm-date" data-date-id="${plan.id}" data-version="${plan.version}" ${plan.confirmed_by_me ? "disabled" : ""}>${plan.confirmed_by_me ? "已确认，等待 TA" : "确认最终约会"}</button>`;
    if (plan.status === "confirmed") actions = `<button data-action="complete-date" data-date-id="${plan.id}">约会完成，保存回忆</button>`;
    if (plan.status === "completed") actions = `<button data-nav="timeline">已成为恋爱瞬间</button>`;
    return `<article class="date-plan-card card"><header><button class="shared-open" data-action="open-shared-detail" data-resource-type="date_plan" data-id="${escapeHtml(plan.id)}"><span class="eyebrow ink">${escapeHtml(plan.status.toUpperCase())}</span><h3>${escapeHtml(plan.title)}</h3><p>${shortDate(plan.plan_date)} · ${escapeHtml(plan.start_time)}–${escapeHtml(plan.end_time)} · ${escapeHtml(plan.city)}</p><em class="shared-open-hint">查看详情与双方评论 →</em></button><b>${money(plan.budget_cents)}</b></header><div class="date-variants">${variants}</div>${actions ? `<div class="inline-actions">${actions}</div>` : ""}</article>`;
  }).join("");
}

function tripVariant(style) {
  return state.data?.trip?.variants?.find(variant => variant.style === style);
}

function itineraryItemContext(itemId) {
  for (const variant of state.data?.trip?.variants || []) {
    for (const day of variant.days || []) {
      const item = (day.items || []).find(candidate => candidate.id === itemId);
      if (item) return { variant, day, item };
    }
  }
  return null;
}

function itineraryDetailList(title, items) {
  const values = Array.isArray(items) ? items.filter(Boolean) : [];
  if (!values.length) return "";
  return `<section class="itinerary-detail-section"><h3>${escapeHtml(title)}</h3><ul>${values.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul></section>`;
}

function isDiningItineraryItem(item) {
  return item?.type === "meal" || /早餐|早饭|午餐|午饭|晚餐|晚饭|餐厅|餐馆|饭店|小吃|美食|用餐|厨房|restaurant|breakfast|lunch|dinner/i.test(item?.title || "");
}

function renderRestaurantEvidence(item) {
  const evidence = Array.isArray(item.review_evidence) ? item.review_evidence : [];
  if (!evidence.length) return `<section class="restaurant-evidence empty"><h3>公开口碑参考</h3><p>点击“查询真实餐厅”后，系统会检索具体的大众点评、美团和小红书公开结果。没有结果时保持为空，不用模型补写。</p></section>`;
  return `<section class="restaurant-evidence"><h3>公开口碑参考</h3><p>以下是搜索服务实际返回的具体页面，不把摘要当成平台评分。</p><div>${evidence.map(row => `<a class="review-evidence-card" href="${escapeHtml(row.url)}" target="_blank" rel="noopener"><span>${escapeHtml(row.platform)}</span><strong>${escapeHtml(row.title)}</strong>${row.snippet ? `<small>${escapeHtml(row.snippet)}</small>` : ""}<em>查看原始来源 →</em></a>`).join("")}</div></section>`;
}

function openItineraryDetail(itemId) {
  const context = itineraryItemContext(itemId);
  if (!context) { toast("这条行程已经更新，请刷新后重试"); return; }
  const { variant, day, item } = context;
  const details = item.details && typeof item.details === "object" ? item.details : {};
  const hasRichDetails = Array.isArray(details.activities) && details.activities.length
    && Array.isArray(details.highlights) && details.highlights.length
    && details.recommendation_reason;
  const estimatedCost = Number(item.cost_cents || 0) > 0 ? money(item.cost_cents) : "以现场或预约页为准";
  const dining = isDiningItineraryItem(item);
  const areaHint = details.area_hint || item.title;
  const restaurants = item.food_recommendations || [];
  state.itineraryDetailItemId = item.id;
  document.getElementById("itineraryDetailContent").innerHTML = `
    <h2 id="itineraryDetailTitle">${escapeHtml(item.title)}</h2>
    <p class="itinerary-detail-note">${escapeHtml(item.note || "暂无简要说明")}</p>
    <div class="itinerary-detail-meta">
      <span>${escapeHtml(day.date)} · ${escapeHtml(item.start_time)}</span>
      <span>建议停留 ${escapeHtml(item.duration_minutes)} 分钟</span>
      <span>预计费用 ${escapeHtml(estimatedCost)}</span>
      <span>${escapeHtml(variant.name)}</span>
      ${details.location_scope ? `<span>地点范围 ${escapeHtml(details.location_scope)}</span>` : ""}
    </div>
    ${hasRichDetails ? `
      ${itineraryDetailList("具体可以做什么", details.activities)}
      ${itineraryDetailList("这里有什么好玩", details.highlights)}
      <section class="itinerary-reason"><span>为什么推荐</span><p>${escapeHtml(details.recommendation_reason)}</p></section>
      ${itineraryDetailList("实用提醒", details.practical_tips)}
      ${(details.best_time || details.reservation_note) ? `<div class="itinerary-practical">${details.best_time ? `<p><b>最佳时段</b>${escapeHtml(details.best_time)}</p>` : ""}${details.reservation_note ? `<p><b>预约说明</b>${escapeHtml(details.reservation_note)}</p>` : ""}</div>` : ""}
    ` : `<div class="itinerary-detail-empty"><strong>这条旧行程还没有扩展详情</strong><p>点击旅行页的“重新生成”，模型会把具体活动、好玩亮点和推荐原因一起保存；这里不会用通用硬编码冒充真实介绍。</p></div>`}
    ${dining ? `<section class="itinerary-detail-restaurants"><div class="restaurant-detail-heading"><div><span>NEARBY FOOD</span><h3>附近高分餐厅</h3></div><button data-action="refresh-item-food" data-item-id="${escapeHtml(item.id)}" data-search-area="${escapeHtml(areaHint)}">${restaurants.length ? "重新查询" : "查询真实餐厅"}</button></div><p>仅保留搜索点 5 公里内结果，优先显示高德评分较高的餐厅；公开口碑只展示有具体来源的结果。</p>${restaurants.length ? renderFoodRecommendations(item) : `<div class="restaurant-empty">尚未查询，不会用模型虚构店名。</div>`}${renderRestaurantEvidence(item)}</section>` : ""}
    <div class="shared-detail-actions"><button data-action="open-shared-detail" data-resource-type="itinerary_item" data-id="${escapeHtml(item.id)}">查看双方评论</button></div>
  `;
  openModal("itineraryDetailModal");
}

function renderFoodRecommendations(item) {
  const recommendations = item.food_recommendations || [];
  if (!recommendations.length) return "";
  return `<div class="restaurant-options">${recommendations.map(shop => {
    const selected = Boolean(shop.selected_at);
    const rating = shop.rating ? `评分 ${Number(shop.rating).toFixed(1)}` : "暂无评分";
    const cost = shop.average_cost_cents ? `人均 ${money(shop.average_cost_cents)}` : "人均待确认";
    const distance = shop.distance_meters ? `距搜索点 ${(Number(shop.distance_meters) / 1000).toFixed(1)} km` : "";
    const navigation = String(shop.navigation_url || "").startsWith("https://uri.amap.com/") ? `<a href="${escapeHtml(shop.navigation_url)}" target="_blank" rel="noopener">高德导航</a>` : "";
    return `<article class="restaurant-card ${selected ? "selected" : ""}"><header><div><strong>${escapeHtml(shop.name)}</strong><small>${escapeHtml(shop.specialties)}</small></div><b>${selected ? "已选" : rating}</b></header><small>${escapeHtml([shop.district, shop.address].filter(Boolean).join(" · ") || "地址以高德页面为准")}</small><div class="restaurant-meta"><span>${escapeHtml(cost)}</span>${distance ? `<span>${escapeHtml(distance)}</span>` : ""}${shop.opening_hours ? `<span>${escapeHtml(shop.opening_hours)}</span>` : ""}${shop.telephone ? `<span>${escapeHtml(shop.telephone)}</span>` : ""}</div><div class="restaurant-actions"><button data-action="select-food" data-food-id="${escapeHtml(shop.id)}" ${selected ? "disabled" : ""}>${selected ? "已加入行程" : "选择这家"}</button>${navigation}</div></article>`;
  }).join("")}</div>`;
}

function isBroadFoodDestination(destination) {
  return /(?:省|自治区)$|^(?:云南|海南|四川|广东|广西|西藏|新疆|青海|甘肃|陕西|贵州|河北|河南|山东|山西|江苏|浙江|福建|湖南|湖北|安徽|江西|辽宁|吉林|黑龙江)$/.test(destination || "");
}

function foodAreaForDay(day, trip) {
  const stored = localStorage.getItem(`love-food-area-${trip?.id}-${day.day_number}`);
  const existing = (day.items || []).flatMap(item => item.food_recommendations || []).find(shop => shop.search_area)?.search_area;
  return stored || existing || (isBroadFoodDestination(trip?.destination) ? "" : (trip?.destination || ""));
}

function renderFoodDayArea(day, trip) {
  const value = foodAreaForDay(day, trip);
  return `<div class="food-day-area"><label><span>第 ${escapeHtml(day.day_number)} 天用餐区域</span><input value="${escapeHtml(value)}" data-food-area-day="${escapeHtml(day.day_number)}" placeholder="景区、古城、商圈或酒店名称" aria-label="第 ${escapeHtml(day.day_number)} 天用餐区域"></label><button data-action="refresh-food-day" data-day-number="${escapeHtml(day.day_number)}">查本日早中晚餐馆</button><small>只推荐该区域 5 公里内的真实店铺；可让每天使用不同区域。</small></div>`;
}

function renderPlan() {
  const variant = tripVariant(state.plan);
  const summary = variant?.summary || "当前旅行还没有生成这套方案，请点击重新生成。";
  const days = variant?.days || [];
  const items = days.flatMap(day => day.items || []);
  const trip = state.data?.trip;
  const isFoodPlan = state.plan === "food";
  document.getElementById("planSummary").textContent = summary;
  const generationStatus = document.getElementById("planGenerationStatus");
  if (generationStatus) {
    const source = variant?.generation_source || "legacy";
    const label = source === "ai" ? "AI 已生成" : source === "rules" ? "本地安全方案" : "历史方案";
    const detail = variant?.generation_message || (source === "legacy" ? "保存时尚未记录生成来源；重新生成后会显示真实来源。" : "");
    generationStatus.className = `plan-generation-status ${source}`;
    generationStatus.textContent = `${label}${detail ? ` · ${detail}` : ""}`;
  }
  document.getElementById("tripVariantCount").textContent = `已保存 ${state.data?.trip?.variants?.length || 0} 套方案`;
  document.getElementById("tripDayTitle").textContent = variant?.name || "完整行程";
  document.getElementById("tripDayMeta").textContent = days.length ? `${days.length} 天 · ${items.length} 个地点 · ${variant.intensity || "节奏待确认"}` : "尚未生成可编辑行程";
  document.getElementById("itinerary").innerHTML = items.length ? days.map(day => `<section class="itinerary-day"><header><strong>DAY ${escapeHtml(day.day_number)} · ${escapeHtml(day.title)}</strong><small>${escapeHtml(day.date)} · ${escapeHtml(day.intensity || variant.intensity || "")}</small></header>${isFoodPlan ? renderFoodDayArea(day, trip) : ""}${(day.items || []).map(item => {
    const selected = state.selectedItemIds.has(item.id);
    return `<div class="route-item"><time>${escapeHtml(item.start_time)}</time><button class="route-item-detail" data-action="view-itinerary-detail" data-item-id="${escapeHtml(item.id)}" aria-label="查看${escapeHtml(item.title)}的活动和推荐原因"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.note)}</small><em>查看活动、亮点与推荐原因 <b aria-hidden="true">→</b></em></button><button class="mix-toggle ${selected ? "" : "off"}" data-action="toggle-mix" data-item-id="${escapeHtml(item.id)}" aria-pressed="${selected}" aria-label="${escapeHtml(item.title)} ${selected ? "移出" : "加入"}混搭">${selected ? "✓" : "+"}</button></div>${renderFoodRecommendations(item)}`;
  }).join("")}</section>`).join("") : `<div class="empty-view"><span>⌁</span><h3>这套方案尚未生成</h3><p>点击“AI 重新生成”会创建并保存真实方案。</p><button class="primary-button" data-action="generate-trip">AI 重新生成</button></div>`;
  const adoptedStyle = trip?.active_variant;
  document.querySelectorAll(".plan-tabs button").forEach(btn => {
    const item = tripVariant(btn.dataset.plan);
    btn.querySelector("small").textContent = item ? money(item.estimated_budget_cents) : "未生成";
    btn.classList.toggle("active", btn.dataset.plan === state.plan);
    btn.classList.toggle("adopted", btn.dataset.plan === adoptedStyle && ["pending_approval", "confirmed"].includes(trip?.status));
  });
  document.getElementById("planPreviewName").textContent = variant?.name || "尚未生成";
  document.getElementById("planPreviewMeta").textContent = variant ? `${days.length} 天 · ${items.length} 个地点 · ${money(variant.estimated_budget_cents)}` : "请先重新生成方案";
  const adoptButton = document.getElementById("adoptPlanButton");
  const alreadyAdopted = adoptedStyle === state.plan && ["pending_approval", "confirmed"].includes(trip?.status);
  adoptButton.disabled = !variant || alreadyAdopted;
  adoptButton.textContent = alreadyAdopted ? (trip.status === "confirmed" ? "双方已确认" : "已采用这套") : "采用整套方案";
  const approval = trip?.approval_state || { approved_count: 0, required_count: 2, approved_by_me: false };
  const approvalCard = document.getElementById("tripApproval");
  approvalCard.hidden = !["pending_approval", "confirmed"].includes(trip?.status);
  if (!approvalCard.hidden) {
    const confirmed = trip.status === "confirmed";
    document.getElementById("tripApprovalTitle").textContent = confirmed ? `最终方案 v${trip.current_revision} 已生效` : `最终方案 v${trip.current_revision} 等待确认`;
    document.getElementById("tripApprovalMeta").textContent = confirmed ? "你们两个人都已确认，可以按此出行" : `已有 ${approval.approved_count}/${approval.required_count} 人确认${approval.approved_by_me ? " · 你已确认" : ""}`;
    const approveButton = document.getElementById("approveTripButton");
    approveButton.hidden = confirmed;
    approveButton.disabled = approval.approved_by_me;
    approveButton.textContent = approval.approved_by_me ? "已确认，等待 TA" : "确认最终方案";
  }
  document.querySelectorAll(".trip-progress span")[2]?.classList.toggle("active", ["pending_approval", "confirmed"].includes(trip?.status));
  const refreshFoodButton = document.getElementById("refreshFoodButton");
  const foodNotice = document.getElementById("foodRecommendationNotice");
  const restaurantCount = items.reduce((total, item) => total + (item.food_recommendations?.length || 0), 0);
  refreshFoodButton.hidden = !isFoodPlan;
  foodNotice.hidden = !isFoodPlan;
  const missingAreas = isFoodPlan ? days.filter(day => !foodAreaForDay(day, trip)).map(day => day.day_number) : [];
  if (isFoodPlan) foodNotice.textContent = missingAreas.length ? `请先填写第 ${missingAreas.join("、")} 天所在的景区、古城、商圈或酒店；每一天可以不同，避免跨城推荐。` : restaurantCount ? `已保存 ${restaurantCount} 个真实店铺候选。每天都有早餐、午餐和晚餐，可分别选择一家；选择后双方需要重新确认。` : "每一天都会查询早餐、午餐和晚餐，并在本日区域 5 公里内给出多家真实特色店。";
  document.getElementById("mixCount").textContent = state.selectedItemIds.size;
  state.editingItemId = items[1]?.id || items[0]?.id || null;
}

function prefillQuoteForms(trip) {
  if (!trip) return;
  const set = (id, value, overwrite = false) => { const input = document.getElementById(id); if (input && (overwrite || !input.value)) input.value = value || ""; };
  const place = trip.destination_place;
  const uniqueParts = values => [...new Set(values.map(value => String(value || "").trim()).filter(Boolean))];
  const destination = place ? (place.city || place.district || place.province || place.name) : (trip.arrival_city || trip.destination);
  const hotelDestination = place ? uniqueParts([place.city, place.district, place.name]).join(" ") : destination;
  set("hotelDestination", hotelDestination, true); set("hotelCheckIn", String(trip.start_date || "").slice(0, 10), true); set("hotelCheckOut", String(trip.end_date || "").slice(0, 10), true);
  set("flightOrigin", trip.departure_city, true); set("flightDestination", destination, true); set("flightOutbound", String(trip.start_date || "").slice(0, 10), true); set("flightReturn", String(trip.end_date || "").slice(0, 10), true);
  set("trainOrigin", trip.departure_city, true); set("trainDestination", destination, true);
  set("trainDate", String(trip.start_date || "").slice(0, 10), true);
  const unresolved = Boolean(trip.needs_destination_detail);
  const scopeNotice = document.getElementById("tripSearchScopeNotice");
  if (scopeNotice) {
    scopeNotice.hidden = false;
    scopeNotice.textContent = unresolved
      ? "先从高德候选中确认具体地点，才能查询对应地区的路线、酒店、航班和火车，避免同名地点串线。"
      : `本页查询范围已绑定：${uniqueParts([place?.country, place?.province, place?.city, place?.district, place?.name]).join(" · ")}`;
  }
  const routeButton = document.querySelector('[data-action="trip-route"]');
  if (routeButton) { routeButton.disabled = unresolved; routeButton.title = unresolved ? "请先确认具体目的地" : ""; }
  document.querySelectorAll('[data-action="search-quotes"]').forEach(button => {
    button.disabled = unresolved;
    button.title = unresolved ? "请先确认具体目的地" : "";
  });
}

const routeModeLabels = { walking: "步行", transit: "公交", driving: "驾车 / 打车路线参考", bicycling: "骑行", taxi: "打车（仅路线建议）" };

function formatRouteDistance(meters) {
  const value = Number(meters || 0);
  return value < 1000 ? `${Math.max(0, Math.round(value))} 米` : `${(value / 1000).toFixed(value < 10000 ? 1 : 0)} 公里`;
}

function renderRouteMode(mode, route) {
  if (route.unavailable) return `<div class="route-mode-result unavailable"><div><strong>${escapeHtml(routeModeLabels[mode] || mode)}</strong><span>${escapeHtml(route.error || "暂时没有可用路线")}</span></div><small>${route.cached ? "24 小时负缓存" : "高德已查询"} · ${new Date(route.fetched_at).toLocaleString("zh-CN")}</small></div>`;
  return `<div class="route-mode-result"><div><strong>${escapeHtml(routeModeLabels[mode] || mode)}</strong><span>${formatRouteDistance(route.distance_meters)} · 约 ${Math.max(1, Math.round(Number(route.duration_seconds || 0) / 60))} 分钟</span></div><small>${route.cached ? "24 小时缓存" : (route.mock ? "本地演示" : "刚刚查询高德")} · ${new Date(route.fetched_at).toLocaleString("zh-CN")}</small></div>`;
}

function renderRoutePlaceResolver(item, legId) {
  if (item.place) {
    const area = [item.place.city, item.place.district, item.place.formatted_address].filter(Boolean).join(" · ");
    return `<div class="route-place-confirmed"><span>✓ ${escapeHtml(item.place.name)}</span><small>${escapeHtml(area || "已保存高德坐标")}</small><button data-action="route-place-search" data-item-id="${escapeHtml(item.id)}" data-leg-id="${escapeHtml(legId)}">更换</button></div>`;
  }
  return `<div class="route-place-resolver" data-route-item="${escapeHtml(item.id)}"><label><span>确认“${escapeHtml(item.title)}”的地图地点</span><input value="${escapeHtml(item.title)}" aria-label="搜索真实地图地点"></label><button data-action="route-place-search" data-item-id="${escapeHtml(item.id)}" data-leg-id="${escapeHtml(legId)}">搜索高德</button><div class="route-place-candidates"></div></div>`;
}

function renderRouteLegs(result) {
  state.routeLegs = result;
  const host = document.getElementById("tripToolResults");
  const days = new Map();
  (result.legs || []).forEach(leg => {
    if (!days.has(leg.day_id)) days.set(leg.day_id, { title: `DAY ${leg.day_number} · ${leg.day_title}`, date: leg.date, legs: [] });
    days.get(leg.day_id).legs.push(leg);
  });
  const content = [...days.values()].map(day => `<section class="route-day-card"><header><strong>${escapeHtml(day.title)}</strong><small>${escapeHtml(day.date)}</small></header>${day.legs.map(leg => {
    const cached = Object.entries(leg.cached_routes || {}).map(([mode, route]) => renderRouteMode(mode, route)).join("");
    const recommended = (leg.recommended_modes || []).map(mode => `<span>${escapeHtml(routeModeLabels[mode] || mode)}</span>`).join("");
    const unresolved = [leg.origin_item, leg.destination_item].filter((item, index, list) => !item.place && list.findIndex(other => other.id === item.id) === index);
    return `<article class="route-leg-card" data-route-leg="${escapeHtml(leg.id)}"><div class="route-leg-title"><b>${escapeHtml(leg.origin_item.title)}</b><i>→</i><b>${escapeHtml(leg.destination_item.title)}</b></div>${leg.resolved ? `<p>本地直线距离 ${formatRouteDistance(leg.straight_distance_meters)}；建议 ${recommended || "按实际情况选择"}。</p>` : `<p class="route-unresolved">先确认真实地图地点；确认过程会保存 POI ID 和坐标，之后不再重复搜索。</p>${unresolved.map(item => renderRoutePlaceResolver(item, leg.id)).join("")}`}<div class="route-mode-results">${cached}</div>${leg.resolved ? `<div class="route-leg-actions"><button data-action="query-route-leg" data-leg-id="${escapeHtml(leg.id)}">查询步行 / 公交 / 驾车</button><button data-action="query-route-cycling" data-leg-id="${escapeHtml(leg.id)}">按需查骑行</button>${leg.driving_refresh_available ? `<button data-action="refresh-route-driving" data-leg-id="${escapeHtml(leg.id)}">刷新临近出发驾车时间</button>` : ""}</div>` : ""}</article>`;
  }).join("")}</section>`).join("");
  host.innerHTML = `<div class="route-budget-note"><strong>逐段路线 · 按需查询</strong><p>${escapeHtml(result.notice || "")}</p><small>默认三种方式只在展开后查询；相同地点与方式缓存 ${Number(result.cache_hours || 24)} 小时，骑行需单独点击。</small></div>${content || '<div class="empty-view"><p>当前方案每天不足两个地点，暂时没有可计算的路段。</p></div>'}`;
}

async function loadRouteLegs({ scroll = true } = {}) {
  const tripId = state.data?.trip?.id;
  if (!tripId) throw new Error("请先创建旅行");
  const result = await api(`/api/v1/trips/${encodeURIComponent(tripId)}/route-legs`);
  renderRouteLegs(result);
  if (scroll) document.getElementById("tripToolResults")?.scrollIntoView({ behavior: "smooth", block: "start" });
  return result;
}

async function queryRouteLeg(legId, modes, forceRefresh = false, button = null) {
  const original = button?.textContent;
  if (button) { button.disabled = true; button.textContent = "查询中…"; }
  try {
    const result = await api(`/api/v1/trips/${encodeURIComponent(state.data.trip.id)}/route-legs/${encodeURIComponent(legId)}/routes`, { method: "POST", body: JSON.stringify({ modes, force_refresh: forceRefresh }) });
    await loadRouteLegs({ scroll: false });
    const saved = result.cache_hits ? `，复用 ${result.cache_hits} 条缓存` : "";
    toast(`已读取 ${Object.keys(result.routes || {}).length} 种路线${saved}；本次新增 ${result.api_requests} 次高德路线请求`);
  } catch (error) { toast(error.message); }
  finally { if (button) { button.disabled = false; button.textContent = original; } }
}

async function searchRoutePlace(itemId, button) {
  const resolver = button.closest(".route-place-resolver");
  const input = resolver?.querySelector("input");
  if (!input) {
    const item = (state.routeLegs?.legs || []).flatMap(leg => [leg.origin_item, leg.destination_item]).find(value => value.id === itemId);
    if (!item) return;
    const card = button.closest(".route-leg-card");
    card.querySelector(".route-mode-results")?.insertAdjacentHTML("beforebegin", renderRoutePlaceResolver({ ...item, place: null }, card.dataset.routeLeg));
    return;
  }
  const query = input.value.trim();
  if (query.length < 2) { toast("请输入至少两个字的地点名称"); return; }
  const place = state.data.trip.destination_place || {};
  const regionHint = place.city || place.district || place.province || state.data.trip.arrival_city || "";
  const host = resolver.querySelector(".route-place-candidates");
  button.disabled = true; host.innerHTML = "<small>高德正在查找候选地点…</small>";
  try {
    const result = await api("/api/v1/places/search", { method: "POST", body: JSON.stringify({ query, region_hint: regionHint, include_global: false }) });
    const items = (result.items || []).filter(item => item.latitude != null && item.longitude != null);
    host.innerHTML = items.length ? items.map(item => `<button data-action="confirm-route-place" data-item-id="${escapeHtml(itemId)}" data-candidate-id="${escapeHtml(item.candidate_id)}"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml([item.city, item.district, item.formatted_address].filter(Boolean).join(" · "))}</small></button>`).join("") : `<small>${escapeHtml(result.notice || "没有查到带坐标的高德候选，请换更具体的名称。")}</small>`;
  } catch (error) { host.innerHTML = `<small>${escapeHtml(error.message)}</small>`; }
  finally { button.disabled = false; }
}

function quotePayload(type) {
  const tripId = state.data?.trip?.id;
  if (state.data?.trip?.needs_destination_detail) throw new Error("请先从高德候选中确认具体目的地，再查询路线、酒店或交通");
  if (type === "hotel") {
    const destination = document.getElementById("hotelDestination").value.trim();
    if (isBroadFoodDestination(destination)) throw new Error("请把酒店目的地改成具体城市、景区或商圈后再查询");
    return { trip_id: tripId, type, destination, check_in: document.getElementById("hotelCheckIn").value, check_out: document.getElementById("hotelCheckOut").value, adults: document.getElementById("hotelAdults").value };
  }
  if (type === "flight") {
    const destination = document.getElementById("flightDestination").value.trim();
    if (isBroadFoodDestination(destination)) throw new Error("航班到达地需要具体城市或机场代码，不能填写整个省份");
    return { trip_id: tripId, type, origin: document.getElementById("flightOrigin").value.trim(), destination, outbound_date: document.getElementById("flightOutbound").value, return_date: document.getElementById("flightReturn").value, adults: document.getElementById("flightAdults").value };
  }
  const destination = document.getElementById("trainDestination").value.trim();
  if (isBroadFoodDestination(destination)) throw new Error("火车到达地需要具体车站或城市，不能填写整个省份");
  return { trip_id: tripId, type: "train", origin: document.getElementById("trainOrigin").value.trim(), destination, date: document.getElementById("trainDate").value, train_filter: document.getElementById("trainFilter").value };
}

function localDateTimeIso(value = new Date()) {
  const date = value instanceof Date ? value : new Date(value);
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const pad = number => String(Math.abs(number)).padStart(2, "0");
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
  return `${local}${sign}${pad(Math.trunc(offsetMinutes / 60))}:${pad(offsetMinutes % 60)}`;
}

function requestTripLiveContext() {
  if (!navigator.geolocation?.getCurrentPosition) return Promise.reject(new Error("当前浏览器不支持定位，请改用支持定位的 Safari 或 Chrome"));
  return new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(position => {
    const observed = new Date(Number(position.timestamp) || Date.now());
    resolve({
      latitude: Number(position.coords.latitude),
      longitude: Number(position.coords.longitude),
      accuracy_meters: Math.max(1, Number(position.coords.accuracy || 1)),
      observed_at: observed.toISOString(),
      local_time: localDateTimeIso(observed),
    });
  }, error => {
    const messages = {
      1: "定位权限被拒绝，请到浏览器或系统设置中允许恋屿使用位置后重试",
      2: "暂时无法获取位置，请检查定位服务和网络后重试",
      3: "获取位置超时，请到开阔位置后重试",
    };
    reject(new Error(messages[error?.code] || "获取位置失败，请稍后重试"));
  }, { enableHighAccuracy: true, timeout: 12_000, maximumAge: 0 }));
}

function liveContextIsFresh(context) {
  const observed = Date.parse(context?.observed_at || "");
  return Number.isFinite(observed) && Date.now() - observed < 14 * 60 * 1000;
}

const LIVE_RESULT_REUSE_MS = 10 * 60 * 1000;
const LIVE_RESULT_REUSE_METERS = 300;

function liveDistanceMeters(first, second) {
  if (!first || !second) return Number.POSITIVE_INFINITY;
  const toRadians = value => Number(value) * Math.PI / 180;
  const lat1 = toRadians(first.latitude);
  const lat2 = toRadians(second.latitude);
  const deltaLat = lat2 - lat1;
  const deltaLon = toRadians(second.longitude) - toRadians(first.longitude);
  const value = Math.sin(deltaLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(deltaLon / 2) ** 2;
  return 6371000 * 2 * Math.atan2(Math.sqrt(value), Math.sqrt(1 - value));
}

function routeModeLabel(mode) {
  return ({ walking: "步行", transit: "公交", driving: "驾车", bicycling: "骑行" })[mode] || "路线";
}

function renderTripLive() {
  const host = document.getElementById("tripLiveResults");
  const button = document.getElementById("tripLiveRefresh");
  if (!host || !button) return;
  const tripId = state.data?.trip?.id || "";
  const live = state.tripLive.tripId === tripId && state.tripLive.ownerUserId === state.data?.user?.id
    ? state.tripLive
    : { tripId, ownerUserId: null, context: null, resultAnchorContext: null, result: null, fetchedAt: 0, deliveryMode: "idle", loading: false, error: "", routes: {} };
  button.disabled = live.loading || !tripId;
  button.textContent = live.loading ? "定位中…" : live.result ? "重新更新" : "更新当前位置";
  if (!tripId) {
    host.innerHTML = '<div class="empty-view"><p>请先创建并选择一段旅行。</p></div>';
    return;
  }
  if (live.loading && !live.result) {
    host.innerHTML = '<div class="empty-view"><p>正在获取本次位置并整理现场建议…</p></div>';
    return;
  }
  if (live.error && !live.result) {
    host.innerHTML = `<div class="trip-live-error" role="alert"><strong>没有取得当前位置</strong><p>${escapeHtml(live.error)}</p></div>`;
    return;
  }
  if (!live.result) {
    host.innerHTML = '<div class="empty-view"><p>到达目的地后，主动更新一次位置即可获得现场建议。</p></div>';
    return;
  }
  const result = live.result;
  const temperature = result.weather?.temperature_c ?? result.weather?.temperature;
  const weather = result.weather_available && result.weather
    ? `<span>天气：${escapeHtml(result.weather.label || "已获取")}${temperature != null ? ` · ${escapeHtml(temperature)}℃` : ""}</span>`
    : "<span>天气暂不可用</span>";
  const candidates = (result.candidates || []).map(card => {
    const mode = card.recommended_mode || "walking";
    const route = live.routes?.[`${card.item_id}:${mode}`];
    const routeInfo = route?.route
      ? `<div class="live-route-result"><strong>${routeModeLabel(route.route.mode || mode)}约 ${Math.ceil(Number(route.route.duration_seconds || 0) / 60)} 分钟</strong><span>${Number(route.route.distance_meters || 0)} 米 · ${escapeHtml(route.route.provider || result.map_provider || "地图服务")}</span></div>`
      : route?.notice ? `<div class="live-route-result"><span>${escapeHtml(route.notice)}</span></div>` : "";
    return `<article class="trip-live-card"><div><span class="mock-badge">${escapeHtml(card.start_time || "下一站")} · ${escapeHtml(routeModeLabel(mode))}</span><strong>${escapeHtml(card.title || "行程地点")}</strong><small>${escapeHtml(card.place?.formatted_address || card.place?.name || "已确认地点")}</small><p>${escapeHtml(card.reason || "按当前行程顺序推荐")}</p></div><div class="assistant-card-actions"><button data-action="view-itinerary-detail" data-item-id="${escapeHtml(card.item_id)}">查看活动</button><button data-action="assistant-route" data-item-id="${escapeHtml(card.item_id)}" data-mode="${escapeHtml(mode)}">查看路线</button></div>${routeInfo}</article>`;
  }).join("");
  const restaurants = (result.restaurants || []).map(card => {
    const meta = [card.rating != null ? `${Number(card.rating).toFixed(1)} 分` : "评分待确认", card.distance_meters != null ? `${Number(card.distance_meters)} 米` : "距离待确认", card.average_cost_cents ? `人均 ${money(card.average_cost_cents)}` : "人均待确认"];
    const navigation = String(card.navigation_url || "").startsWith("https://uri.amap.com/") ? `<a href="${escapeHtml(card.navigation_url)}" target="_blank" rel="noopener">高德导航</a>` : "";
    const specialties = Array.isArray(card.specialties) ? card.specialties.join("、") : String(card.specialties || "");
    const details = [specialties || "特色菜待查看", card.opening_hours || "营业时间待确认"];
    const sourceLabel = live.deliveryMode === "browser_cache" ? `缓存 · ${card.source_label || "地点服务"}` : (card.source_label || "真实地点服务");
    return `<article class="trip-live-card"><div><span class="mock-badge">${escapeHtml(sourceLabel)}</span><strong>${escapeHtml(card.name || "附近餐厅")}</strong><small>${escapeHtml(card.address || card.category || "地址待确认")}</small><div class="assistant-card-meta">${meta.map(value => `<span>${escapeHtml(value)}</span>`).join("")}</div><p>${details.map(value => escapeHtml(value)).join(" · ")}</p></div>${navigation ? `<div class="assistant-card-actions">${navigation}</div>` : ""}</article>`;
  }).join("");
  const resultStatus = live.deliveryMode === "browser_cache"
    ? `<span>缓存于 ${escapeHtml(new Date(live.fetchedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }))}</span>`
    : "<span>现场信息刚刚查询</span>";
  host.innerHTML = `<div class="trip-live-summary">${weather}<span>第 ${result.current_day_number || "—"} 天</span><span>${result.location_persisted === false ? "位置未保存" : "位置状态待确认"}</span>${resultStatus}<button data-action="assistant-replan-today">调整今天</button></div>${live.error ? `<p class="trip-live-inline-warning">${escapeHtml(live.error)}</p>` : ""}<section><h4>接下来可以去</h4>${candidates || `<p class="form-note">${escapeHtml(result.notice || "当前没有已确认坐标且尚未开始的行程。")}</p>`}</section><section><h4>附近餐厅</h4>${restaurants || `<p class="form-note">${escapeHtml(result.restaurant_notice || "附近暂时没有可展示的真实餐厅。")}</p>`}</section><p class="form-note">${escapeHtml(result.notice || "")}</p>`;
}

async function refreshTripLive() {
  const tripId = state.data?.trip?.id;
  if (!tripId) { toast("请先创建并选择一段旅行"); return; }
  const currentUserId = state.data?.user?.id || null;
  const requestEpoch = ++state.tripLiveEpoch;
  const previous = state.tripLive.tripId === tripId && state.tripLive.ownerUserId === currentUserId ? state.tripLive : null;
  state.tripLive = {
    tripId, ownerUserId: currentUserId, context: previous?.context || null,
    resultAnchorContext: previous?.resultAnchorContext || null, result: previous?.result || null,
    fetchedAt: previous?.fetchedAt || 0, deliveryMode: previous?.deliveryMode || "idle",
    loading: true, error: "", routes: previous?.routes || {},
  };
  renderTripLive();
  try {
    const context = await requestTripLiveContext();
    if (state.tripLiveEpoch !== requestEpoch || state.data?.user?.id !== currentUserId) return;
    const canReuse = Boolean(previous?.result)
      && Date.now() - Number(previous?.fetchedAt || 0) <= LIVE_RESULT_REUSE_MS
      && liveDistanceMeters(previous.resultAnchorContext, context) <= LIVE_RESULT_REUSE_METERS;
    if (canReuse) {
      state.tripLive = { ...previous, tripId, ownerUserId: currentUserId, context, deliveryMode: "browser_cache", loading: false, error: "" };
      renderTripLive();
      toast("位置变化较小，已复用最近的现场建议");
      return;
    }
    const result = await api(`/api/v1/trips/${encodeURIComponent(tripId)}/assistant/live`, {
      method: "POST", body: JSON.stringify({ live_context: context, include_restaurants: true }),
    });
    if (state.tripLiveEpoch !== requestEpoch || state.data?.user?.id !== currentUserId) return;
    state.tripLive = { tripId, ownerUserId: currentUserId, context, resultAnchorContext: context, result, fetchedAt: Date.now(), deliveryMode: "live", loading: false, error: "", routes: {} };
  } catch (error) {
    if (state.tripLiveEpoch !== requestEpoch || state.data?.user?.id !== currentUserId) return;
    state.tripLive = {
      tripId, ownerUserId: currentUserId, context: previous?.context || null,
      resultAnchorContext: previous?.resultAnchorContext || null, result: previous?.result || null,
      fetchedAt: previous?.fetchedAt || 0, deliveryMode: previous?.deliveryMode || "idle",
      loading: false, error: error.message, routes: previous?.routes || {},
    };
  }
  renderTripLive();
}

async function loadTripLiveRoute(itemId, mode) {
  const tripId = state.data?.trip?.id;
  if (!tripId || state.tripLive.ownerUserId !== state.data?.user?.id || !liveContextIsFresh(state.tripLive.context)) {
    toast("当前位置已过期，请先点击“更新当前位置”");
    return;
  }
  const key = `${itemId}:${mode}`;
  const requestEpoch = state.tripLiveEpoch;
  const currentUserId = state.data?.user?.id;
  try {
    const result = await api(`/api/v1/trips/${encodeURIComponent(tripId)}/assistant/navigation`, {
      method: "POST", body: JSON.stringify({ live_context: state.tripLive.context, item_id: itemId, mode }),
    });
    if (state.tripLiveEpoch !== requestEpoch || state.data?.user?.id !== currentUserId) return;
    state.tripLive.routes[key] = result;
    renderTripLive();
  } catch (error) { toast(error.message); }
}

function updateTripAssistantCard(type, id, changes) {
  for (const message of state.tripAssistant.items || []) {
    for (const card of message.cards || []) {
      if (card.type === type && String(card.id || card.draft_id) === String(id)) Object.assign(card, changes);
    }
  }
  renderTripAssistant();
}

function quoteBookingLink(item, fallbackLabel = "跳转查看 / 购买") {
  if (!item?.booking_url) return "";
  const details = item.details && typeof item.details === "object" ? item.details : item;
  const label = details.booking_label || fallbackLabel;
  const copyText = details.booking_mode === "domestic_search" ? String(details.booking_copy_text || item.title || "").trim() : "";
  const copyAttributes = copyText
    ? ` data-action="copy-hotel-booking" data-copy-text="${escapeHtml(copyText)}"`
    : "";
  return `<a href="${escapeHtml(item.booking_url)}" target="_blank" rel="noopener"${copyAttributes}>${escapeHtml(label)}</a>`;
}

function renderQuoteResults(result) {
  if (state.data?.trip && Array.isArray(result.items)) {
    const incomingIds = new Set(result.items.map(item => item.id));
    state.data.trip.quotes = [...result.items, ...(state.data.trip.quotes || []).filter(item => !incomingIds.has(item.id))];
  }
  const selected = state.data?.trip?.quotes?.find(item => item.selected_at && item.quote_type === result.items?.[0]?.quote_type)?.id;
  const cards = (result.items || []).map(item => {
    const seatRows = Array.isArray(item.details?.seats) ? `<div class="seat-options">${item.details.seats.map(seat => `<span><b>${escapeHtml(seat.name)}</b>${money(seat.price_cents)} · 余 ${escapeHtml(seat.remaining)}</span>`).join("")}</div>` : "";
    const isSelected = selected === item.id;
    const sourceLabel = item.source_label || (item.mock ? "模拟数据" : item.source_mode === "aggregate" ? "聚合参考" : item.stale ? "缓存结果" : "实时查询");
    const fetchedAt = item.fetched_at && Number.isFinite(Date.parse(item.fetched_at)) ? new Date(item.fetched_at).toLocaleString("zh-CN") : "时间待确认";
    const originalTitle = item.details?.original_title && item.details.original_title !== item.title ? `<small>英文原名：${escapeHtml(item.details.original_title)}</small>` : "";
    return `<article class="result-card quote-result ${isSelected ? "selected" : ""}" data-quote-id="${escapeHtml(item.id)}"><div class="quote-result-head"><span class="mock-badge">${escapeHtml(sourceLabel)}</span><b>${money(item.amount_cents)}</b></div><strong>${escapeHtml(item.title)}</strong>${originalTitle}<small>${escapeHtml(item.subtitle || "")}</small><p>${escapeHtml(item.terms || "")}</p>${seatRows}<small>来源：${escapeHtml(item.provider)} · 查询于 ${escapeHtml(fetchedAt)} · ${item.includes_tax ? "标价含税/服务费说明以平台为准" : "可能未含税费"}</small><div class="quote-actions"><button data-action="select-quote" data-quote-id="${escapeHtml(item.id)}">${isSelected ? "已加入本次旅行" : "选择这个"}</button>${quoteBookingLink(item)}</div></article>`;
  }).join("");
  const scope = result.search_scope ? `实际搜索范围：${result.search_scope}。` : "";
  document.getElementById("tripToolResults").innerHTML = `<p class="form-note">${escapeHtml(scope + result.notice)} 共找到 ${Number(result.count || result.items?.length || 0)} 条可比较结果。</p>${cards || '<div class="empty-view"><p>没有可展示的结果，请调整条件后重试。</p></div>'}`;
}

function tripAssistantCard(card) {
  if (!card || typeof card !== "object") return "";
  if (card.type === "hotel_quote") {
    const source = card.source_mode === "cached" || card.stale ? "缓存结果" : card.source_mode === "mock" ? "模拟报价" : "实时查询";
    const originalTitle = card.original_title && card.original_title !== card.title ? `<small>英文原名：${escapeHtml(card.original_title)}</small>` : "";
    return `<article class="assistant-result-card"><span class="mock-badge">${escapeHtml(source)}</span><strong>${escapeHtml(card.title || "酒店")}</strong>${originalTitle}<small>${escapeHtml(card.subtitle || "")}</small><b>${money(card.amount_cents)}</b><p>${escapeHtml(card.terms || "最终价格和库存以预订平台为准")}</p><div class="assistant-card-meta"><span>${escapeHtml(card.provider || "未知来源")}</span><span>${card.includes_tax ? "税费说明见平台" : "可能未含税费"}</span></div><div class="assistant-card-actions"><button data-action="assistant-select-quote" data-quote-id="${escapeHtml(card.quote_id)}">选择这个</button>${quoteBookingLink(card, "去平台查看")}</div></article>`;
  }
  if (card.type === "restaurant") {
    const meta = [card.rating != null ? `${Number(card.rating).toFixed(1)} 分` : "暂无评分", card.distance_meters != null ? `${Number(card.distance_meters)} 米` : "距离待确认", card.average_cost_cents ? `人均约 ${money(card.average_cost_cents)}` : "人均待确认"];
    return `<article class="assistant-result-card"><span class="mock-badge">第 ${Number(card.day_number)} 天 · ${escapeHtml(({ breakfast: "早餐", lunch: "午餐", snack: "下午茶", dinner: "晚餐" })[card.meal_type] || "用餐")}</span><strong>${escapeHtml(card.name || "餐厅")}</strong><small>${escapeHtml(card.address || card.category || "")}</small><div class="assistant-card-meta">${meta.map(value => `<span>${escapeHtml(value)}</span>`).join("")}</div><div class="assistant-card-actions"><button data-action="assistant-select-food" data-food-id="${escapeHtml(card.recommendation_id)}">加入行程</button>${card.navigation_url ? `<a href="${escapeHtml(card.navigation_url)}" target="_blank" rel="noopener">高德导航</a>` : ""}</div></article>`;
  }
  if (card.type === "plan_variant") {
    const label = ({ slow: "松弛浪漫", classic: "经典充实", value: "性价比", food: "美食寻味", nature: "自然治愈" })[card.style] || card.style;
    return `<article class="assistant-result-card"><span class="mock-badge">${escapeHtml(label)}</span><strong>${escapeHtml(card.name || "旅行方案")}</strong><p>${escapeHtml(card.summary || "")}</p><div class="assistant-card-meta"><span>${money(card.estimated_budget_cents)}</span><span>${escapeHtml(card.intensity || "")}</span><span>${Number(card.transport_minutes || 0)} 分钟交通</span><span>${Number(card.day_count || 0)} 天</span></div><div class="assistant-card-actions"><button data-action="assistant-open-plan" data-style="${escapeHtml(card.style)}">查看方案</button></div></article>`;
  }
  if (card.type === "itinerary_change") {
    const before = Object.entries(card.before || {}).map(([key, value]) => `${key}: ${value ?? "—"}`).join(" · ");
    const after = Object.entries(card.after || {}).map(([key, value]) => `${key}: ${value ?? "—"}`).join(" · ");
    const status = card.status || "draft";
    const actionArea = status === "confirmed"
      ? `<span class="assistant-action-status">✓ 已确认并保存</span>`
      : status === "cancelled"
        ? `<span class="assistant-action-status muted">已取消</span>`
        : `<button data-action="assistant-confirm-change" data-draft-id="${escapeHtml(card.draft_id)}">确认修改</button><button data-action="assistant-cancel-change" data-draft-id="${escapeHtml(card.draft_id)}">取消</button>`;
    return `<article class="assistant-result-card"><span class="mock-badge">修改预览 · 第 ${Number(card.day_number)} 天</span><strong>${escapeHtml(card.title || "行程")}</strong><small>修改前：${escapeHtml(before)}</small><small>修改后：${escapeHtml(after)}</small><p>确认后才会写入共同旅行；若对方已修改，系统会要求重新预览。</p><div class="assistant-card-actions">${actionArea}</div></article>`;
  }
  if (card.type === "day_replan") {
    const changes = (card.changes || []).map(row => `<li><strong>${escapeHtml(row.title || "行程项目")}</strong><span>${escapeHtml(row.before?.start_time || "—")} → ${escapeHtml(row.after?.start_time || "—")}${row.before?.position !== row.after?.position ? " · 顺序调整" : ""}</span></li>`).join("");
    const warnings = (card.warnings || []).map(value => `<li>${escapeHtml(value)}</li>`).join("");
    const status = card.status || "draft";
    const actionArea = status === "confirmed"
      ? '<span class="assistant-action-status">✓ 已确认并保存</span>'
      : status === "cancelled" || status === "expired"
        ? `<span class="assistant-action-status muted">${status === "expired" ? "草稿已过期" : "已取消"}</span>`
        : `<button data-action="assistant-confirm-change" data-draft-id="${escapeHtml(card.draft_id)}">确认整日调整</button><button data-action="assistant-cancel-change" data-draft-id="${escapeHtml(card.draft_id)}">取消</button>`;
    return `<article class="assistant-result-card"><span class="mock-badge">整日重排 · 第 ${Number(card.day_number)} 天</span><strong>${Number(card.changes?.length || 0)} 项会一起修改</strong><ul class="assistant-change-list">${changes}</ul>${warnings ? `<div class="assistant-warning"><strong>需要留意</strong><ul>${warnings}</ul></div>` : ""}<p>人工锁定和已经开始的项目不会被改动；确认后才会原子写入共同旅行。</p><div class="assistant-card-actions">${actionArea}</div></article>`;
  }
  if (card.type === "next_stop") {
    const mode = card.recommended_mode || "walking";
    return `<article class="assistant-result-card"><span class="mock-badge">下一站 · ${escapeHtml(card.start_time || "时间待确认")}</span><strong>${escapeHtml(card.title || "行程地点")}</strong><small>${escapeHtml(card.place?.formatted_address || card.place?.name || "已确认地点")}</small><p>${escapeHtml(card.reason || "按当前行程推荐")}</p><div class="assistant-card-meta"><span>${Number(card.distance_meters || 0)} 米</span><span>${escapeHtml(routeModeLabel(mode))}</span><span>${card.open_status === "open" ? "营业中" : card.open_status === "closed" ? "可能已打烊" : "营业时间待确认"}</span></div><div class="assistant-card-actions"><button data-action="view-itinerary-detail" data-item-id="${escapeHtml(card.item_id)}">查看活动</button><button data-action="assistant-route" data-item-id="${escapeHtml(card.item_id)}" data-mode="${escapeHtml(mode)}">查看路线</button></div></article>`;
  }
  if (card.type === "quote_group") {
    const labels = { hotel: "酒店", flight: "航班", train: "火车票" };
    const rows = (card.items || []).map(row => {
      const source = row.source_label || card.source_label || "来源待确认";
      const fetchedAt = row.fetched_at && Number.isFinite(Date.parse(row.fetched_at)) ? new Date(row.fetched_at).toLocaleString("zh-CN") : "时间待确认";
      return `<div class="assistant-quote-row"><div><span class="mock-badge">${escapeHtml(source)}</span><strong>${escapeHtml(row.title || labels[card.quote_type] || "报价")}</strong><small>${escapeHtml(row.subtitle || "")}</small><small>${escapeHtml(row.provider || "未知来源")} · ${escapeHtml(fetchedAt)}</small></div><div><b>${money(row.amount_cents)}</b><div class="assistant-card-actions"><button data-action="assistant-select-quote" data-quote-id="${escapeHtml(row.id)}">选择这个</button>${quoteBookingLink(row, "去平台查看")}</div></div></div>`;
    }).join("");
    return `<article class="assistant-result-card quote-group-card"><span class="mock-badge">${escapeHtml(card.source_label || "查询结果")}</span><strong>${escapeHtml(labels[card.quote_type] || "出行报价")}</strong><p>${escapeHtml(card.message || "最终价格、库存及退改规则以第三方平台为准。")}</p>${rows || '<small>当前没有可展示的结果。</small>'}</article>`;
  }
  if (card.type === "trip_recap") {
    const status = card.status || "draft";
    const body = escapeHtml(card.body || "").replace(/\n/g, "<br>");
    const actionArea = status === "confirmed"
      ? '<span class="assistant-action-status">✓ 已进入共同时间轴</span>'
      : status === "cancelled" || status === "expired"
        ? `<span class="assistant-action-status muted">${status === "expired" ? "草稿已过期" : "已取消"}</span>`
        : `<button data-action="assistant-recap-edit" data-draft-id="${escapeHtml(card.id)}" data-version="${Number(card.version || 1)}">编辑草稿</button><button data-action="assistant-recap-confirm" data-draft-id="${escapeHtml(card.id)}">确认进入时光轴</button><button data-action="assistant-recap-cancel" data-draft-id="${escapeHtml(card.id)}">取消</button>`;
    return `<article class="assistant-result-card trip-recap-card"><span class="mock-badge">${card.is_early ? "提前草稿" : "旅行回忆草稿"}</span><strong>${escapeHtml(card.title || "我们的旅行回忆")}</strong><div class="trip-recap-body">${body}</div><p>${escapeHtml(card.notice || "确认后才会进入双方时光轴。")}</p><div class="assistant-card-actions">${actionArea}</div></article>`;
  }
  if (card.type === "clarification") return `<article class="assistant-result-card"><p>${escapeHtml(card.message || "请补充旅行日期、预算或餐次。")}</p></article>`;
  return "";
}

function renderTripAssistant(result = state.tripAssistant) {
  const host = document.getElementById("tripAssistantMessages");
  if (!host) return;
  const trip = state.data?.trip;
  const scope = document.getElementById("tripAssistantScope");
  if (scope) scope.textContent = trip?.destination_place
    ? `搜索范围：${[trip.destination_place.city, trip.destination_place.district, trip.destination_place.name].filter(Boolean).join(" · ")}。选择与修改仍由你确认。`
    : "请先确认具体地图地点，避免酒店和餐厅匹配到异地同名地点。";
  if (!trip) {
    host.innerHTML = `<div class="empty-view"><p>请先创建并选择一段旅行。</p></div>`;
    return;
  }
  if (result.loading && !result.items?.length) {
    host.innerHTML = `<div class="empty-view"><p>正在读取你们的旅行对话…</p></div>`;
    return;
  }
  if (!result.items?.length) {
    host.innerHTML = `<div class="empty-view"><p>还没有旅行对话。你可以从预算、酒店、餐厅或每日行程开始。</p></div>`;
    return;
  }
  host.innerHTML = result.items.map(item => {
    const cards = item.role === "assistant" ? (item.cards || []).map(tripAssistantCard).filter(Boolean).join("") : "";
    const label = item.role === "user" ? escapeHtml(item.author_name || state.data?.user?.nickname || "我") : "恋屿旅行助手";
    return `<div class="assistant-message ${item.role === "user" ? "user" : "assistant"}"><small>${label}</small><div>${escapeHtml(item.content || "")}</div>${cards ? `<div class="assistant-cards">${cards}</div>` : ""}</div>`;
  }).join("");
  requestAnimationFrame(() => host.lastElementChild?.scrollIntoView({ behavior: "smooth", block: "nearest" }));
}

async function loadTripAssistant({ quiet = false } = {}) {
  const tripId = state.data?.trip?.id;
  if (!tripId) { state.tripAssistant = { tripId: "", items: [], loading: false }; renderTripAssistant(); return; }
  const sequence = ++state.tripAssistantLoadSequence;
  if (!quiet) state.tripAssistant.loading = true;
  renderTripAssistant();
  const result = await api(`/api/v1/trips/${encodeURIComponent(tripId)}/assistant`);
  if (sequence !== state.tripAssistantLoadSequence || state.data?.trip?.id !== tripId) return;
  if (state.tripAssistant.tripId === tripId && (state.tripAssistant.items?.length || 0) > (result.items?.length || 0)) return;
  state.tripAssistant = { ...result, tripId, loading: false };
  renderTripAssistant();
}

function currentEditingTripItem() {
  return (state.data?.trip?.variants || []).flatMap(variant => variant.days || []).flatMap(day => day.items || []).find(item => item.id === state.editingItemId) || null;
}

function renderHotel() {
  const selectedHotelQuote = state.data?.trip?.quotes?.find(item => item.quote_type === "hotel" && item.selected_at);
  const hotel = state.data?.trip?.hotels?.[0] || (selectedHotelQuote ? {
    name: selectedHotelQuote.title,
    area: selectedHotelQuote.subtitle || state.data?.trip?.arrival_city || state.data?.trip?.destination || "住宿区域待确认",
    room_type: "已选择的平台方案",
    price_cents: selectedHotelQuote.amount_cents,
    quotes: [selectedHotelQuote],
  } : null);
  const card = document.querySelector(".hotel-card");
  const priceNote = document.querySelector(".price-note");
  const mixButton = card.querySelector(".mix-check");
  if (!hotel) {
    card.hidden = false; card.classList.add("hotel-empty"); mixButton.hidden = true; priceNote.hidden = false;
    card.querySelector(".hotel-info strong").textContent = "还没有保存住宿";
    card.querySelector(".hotel-info > small").textContent = "到“路线报价”查询酒店，选择后会显示在这里";
    card.querySelector(".price-line b").textContent = "—";
    card.querySelector(".price-line span").innerHTML = `<button data-trip-section="tools">去查询酒店</button>`;
    priceNote.textContent = "查询结果只有点“选择这个”后才会加入本次旅行；最终库存与价格以预订平台为准。";
    return;
  }
  card.classList.remove("hotel-empty"); mixButton.hidden = false;
  card.hidden = false; document.querySelector(".price-note").hidden = false;
  card.querySelector(".hotel-info strong").textContent = hotel.name;
  card.querySelector(".hotel-info > small").textContent = `${hotel.area} · ${hotel.room_type}`;
  const quotes = hotel.quotes || [];
  const liveQuotes = Boolean(state.data?.capabilities?.booking_live);
  card.querySelector(".price-line b").textContent = money(quotes[0]?.quote_cents || quotes[0]?.amount_cents || hotel.price_cents);
  card.querySelector(".price-line span").textContent = quotes.length
    ? quotes.map(quote => `${quote.source_mode === "mock" ? "模拟 · " : ""}${quote.provider} ${money(quote.quote_cents || quote.amount_cents)}`).join(" · ")
    : "暂无平台报价，可使用下方手工报价";
  const freshest = quotes[0]?.fetched_at;
  document.querySelector(".price-note").textContent = liveQuotes && freshest
    ? `报价来源已授权 · 查询于 ${new Date(freshest).toLocaleString("zh-CN")} · 最终价格以预订平台为准`
    : quotes.length
      ? "当前为演示价格记录，不代表实时库存；可在路线报价页手工录入真实价格。"
      : "当前仅显示已保存的酒店信息，最终价格以预订平台为准";
}

function renderWishlist() {
  const list = state.data?.wishlist;
  if (!list) return;
  const article = document.querySelector(".wishlist");
  const completed = list.items.filter(item => item.completed_at).length;
  article.innerHTML = `<div class="card-top"><div><span class="eyebrow ink">THIS MONTH</span><h2>${escapeHtml(list.title)}</h2></div><div class="wishlist-head"><span>${completed} / ${list.items.length}</span><button data-action="open-wishlist-editor">＋ 添加</button></div></div>${list.items.length ? list.items.map(item => `<div class="wish-row"><button class="wish-toggle ${item.completed_at ? "checked" : ""}" data-action="toggle-wish" data-id="${escapeHtml(item.id)}" data-completed="${item.completed_at ? "1" : "0"}" aria-label="${item.completed_at ? "恢复" : "完成"}${escapeHtml(item.text)}"><i></i></button><button class="grow shared-open" data-action="open-shared-detail" data-resource-type="wishlist_item" data-id="${escapeHtml(item.id)}">${item.completed_at ? `<s>${escapeHtml(item.text)}</s>` : `<strong>${escapeHtml(item.text)}</strong>`}<small>${escapeHtml(item.note || "暂无备注")}</small><em class="shared-open-hint">查看详情与双方评论 →</em></button><div class="wish-actions"><button data-action="edit-wish" data-id="${escapeHtml(item.id)}">编辑</button><button data-action="delete-wish" data-id="${escapeHtml(item.id)}" data-version="${item.version}">删除</button></div></div>`).join("") : `<div class="empty-view"><span>✓</span><h3>写下第一个共同愿望</h3><p>保存后双方都能查看、编辑和完成。</p></div>`}`;
}

function renderDailyQuestion() {
  const question = state.data?.dailyQuestion;
  if (!question) return;
  document.querySelector(".question-card h3").innerHTML = escapeHtml(question.question).replace("，", "，<br>");
  const answerState = document.getElementById("answerState");
  const answerButton = document.querySelector('[data-action="answer-question"]');
  answerState.classList.remove("unlocked");
  answerButton.disabled = false;
  if (question.answered_by_me && question.partner_answered) {
    document.getElementById("answerState").innerHTML = `<span>你：${escapeHtml(question.answers[state.data.user.id])}</span><span>${escapeHtml(state.data.partner.nickname)}：${escapeHtml(question.answers[state.data.partner.id])}</span>`;
    answerState.classList.add("unlocked");
    answerButton.textContent = "默契答案已解锁";
    answerButton.disabled = true;
  } else if (question.answered_by_me) {
    answerState.innerHTML = `<span>你已回答</span><span class="lock">等待 ${escapeHtml(state.data.partner.nickname)} 回答</span>`;
    answerButton.textContent = "已提交，等待对方";
    answerButton.disabled = true;
  } else {
    answerState.innerHTML = `<span>${question.partner_answered ? `${escapeHtml(state.data.partner.nickname)}已回答` : "等待双方回答"}</span><span class="lock">回答后解锁</span>`;
    answerButton.textContent = "写下我的答案";
  }
}

function momentTypeLabel(type) {
  return ({ text: "文字", photo: "照片", video: "视频", voice: "语音", chat: "聊天收藏", call: "通话", trip: "旅行", anniversary: "纪念日", wishlist: "共同愿望", location: "位置", weekly_review: "每周回顾", date: "约会" })[type] || "瞬间";
}

function momentHasSharedThread(moment) {
  if (moment.visibility === "shared") return true;
  if (moment.visibility !== "delayed" || !moment.reveal_at) return false;
  const revealAt = new Date(moment.reveal_at).getTime();
  return Number.isFinite(revealAt) && revealAt <= Date.now();
}

function momentReactionMarkup(moment) {
  const icons = { heart: "♥", smile: "☺", hug: "拥抱" };
  const existing = (moment.reactions || []).map(item => `<span title="${escapeHtml(item.nickname)}">${escapeHtml(item.avatar_label)} ${icons[item.reaction] || "♥"}</span>`).join("");
  return `${existing}<button data-action="react-moment" data-id="${escapeHtml(moment.id)}" data-reaction="${moment.my_reaction === "heart" ? "none" : "heart"}">${moment.my_reaction === "heart" ? "取消喜欢" : "♡ 喜欢"}</button>`;
}

function renderTimeline() {
  const body = document.getElementById("timelineBody");
  if (!state.data?.moments?.length) {
    document.getElementById("timelineStats").textContent = "0 个瞬间 · 0 座城市";
    body.innerHTML = `<div class="empty-view"><span>◇</span><h3>第一段回忆，等你们来写</h3><p>从一张照片、一句话或一次通话开始。</p><button class="primary-button" data-action="new-moment" data-type="回忆">记录第一个瞬间</button></div>`;
    return;
  }
  const moments = state.data.moments;
  const feature = moments.find(moment => moment.media_url) || moments[0];
  const rest = moments.filter(moment => moment.id !== feature.id);
  const visibilityLabel = feature.visibility === "private" ? "仅自己" : feature.visibility === "delayed" ? "延时公开" : "双方可见";
  const privacyControl = feature.creator_id === state.data.user.id
    ? `<button class="privacy-badge" data-action="privacy" data-moment-id="${escapeHtml(feature.id)}" data-visibility="${escapeHtml(feature.visibility)}">${visibilityLabel}</button>`
    : `<span class="privacy-badge">${visibilityLabel}</span>`;
  const ownerControls = feature.creator_id === state.data.user.id
    ? `<div class="moment-owner-actions"><button class="moment-edit" data-action="edit-moment" data-id="${escapeHtml(feature.id)}">编辑</button><button class="moment-trash" data-action="trash-moment" data-id="${escapeHtml(feature.id)}">移入回收站</button></div>`
    : "";
  const featureMedia = !feature.media_url ? "" : feature.type === "video" ? `<video controls preload="metadata" ${feature.media_id ? `poster="/api/v1/media/${encodeURIComponent(feature.media_id)}?thumbnail=1"` : ""} src="${escapeHtml(feature.media_url)}"></video>` : feature.type === "voice" ? `<audio controls preload="metadata" src="${escapeHtml(feature.media_url)}"></audio>` : `<img src="${escapeHtml(feature.media_url)}" alt="${escapeHtml(feature.title)}" loading="lazy">`;
  const featureDate = new Date(feature.occurred_at).toLocaleDateString("zh-CN");
  const attachmentMedia = ["text", "chat"].includes(feature.type);
  const featureMediaBlock = featureMedia
    ? `<div class="memory-feature-media${attachmentMedia ? " is-attachment" : ""}">${featureMedia}${privacyControl}</div>`
    : `<div class="memory-feature-placeholder"><span>${feature.type === "call" ? "☎" : "✦"}</span>${privacyControl}</div>`;
  body.innerHTML = `<div class="year-mark"><span>${new Date(feature.occurred_at).getFullYear()}</span><i></i></div><article class="memory-feature card">${featureMediaBlock}<div class="memory-feature-body"><div class="memory-feature-meta"><span>${escapeHtml(feature.location || "我们的时光")}</span><time>${featureDate} · ${escapeHtml(momentTypeLabel(feature.type))}</time></div><h2>${escapeHtml(feature.title)}</h2>${feature.body ? `<p>${escapeHtml(feature.body)}</p>` : ""}<div class="memory-feature-actions"><div class="reactions">${momentReactionMarkup(feature)}${momentHasSharedThread(feature) ? `<button class="moment-shared-detail" data-action="open-shared-detail" data-resource-type="moment" data-id="${escapeHtml(feature.id)}">查看详情与评论</button>` : ""}</div>${ownerControls}</div></div></article><div class="timeline-list">${rest.map(moment => `<article class="timeline-item"><div class="timeline-dot">${moment.type === "call" ? "☎" : "✦"}</div><div class="timeline-item-content"><small>${new Date(moment.occurred_at).toLocaleDateString("zh-CN")} · ${escapeHtml(momentTypeLabel(moment.type))}${moment.location ? ` · ${escapeHtml(moment.location)}` : ""}</small>${momentHasSharedThread(moment) ? `<button class="shared-open" data-action="open-shared-detail" data-resource-type="moment" data-id="${escapeHtml(moment.id)}"><h3>${escapeHtml(moment.title)}</h3><p>${escapeHtml(moment.body)}</p><em class="shared-open-hint">查看详情与双方评论 →</em></button>` : `<h3>${escapeHtml(moment.title)}</h3><p>${escapeHtml(moment.body)}</p>`}<div class="reactions">${momentReactionMarkup(moment)}${moment.creator_id === state.data.user.id ? `<button class="moment-edit" data-action="edit-moment" data-id="${escapeHtml(moment.id)}">编辑</button><button class="timeline-trash" data-action="trash-moment" data-id="${escapeHtml(moment.id)}">移入回收站</button>` : ""}</div></div></article>`).join("")}</div>`;
  const cities = new Set(moments.map(moment => moment.location).filter(Boolean));
  document.getElementById("timelineStats").textContent = `${moments.length} 个瞬间 · ${cities.size} 座城市`;
}

function renderMomentSelection(title, moments, emptyText = "这里还没有可显示的共同回忆。") {
  const items = moments || [];
  document.getElementById("timelineBody").innerHTML = `<div class="year-mark"><span>${escapeHtml(title)}</span><i></i></div>` + (items.length
    ? items.map(moment => `<article class="timeline-item" data-moment-id="${escapeHtml(moment.id)}"><div class="timeline-dot">${moment.type === "call" ? "☎" : "✦"}</div><div><small>${new Date(moment.occurred_at).toLocaleDateString("zh-CN")} · ${escapeHtml(moment.location || moment.type)}</small>${momentHasSharedThread(moment) ? `<button class="shared-open" data-action="open-shared-detail" data-resource-type="moment" data-id="${escapeHtml(moment.id)}"><h3>${escapeHtml(moment.title)}</h3><p>${escapeHtml(moment.body || "")}</p><em class="shared-open-hint">查看详情与双方评论 →</em></button>` : `<h3>${escapeHtml(moment.title)}</h3><p>${escapeHtml(moment.body || "")}</p>`}<div class="reactions"><span>${moment.visibility === "private" ? "仅自己可见" : moment.visibility === "delayed" ? "延时公开" : "双方可见"}</span>${moment.creator_id === state.data.user.id ? `<button class="moment-edit" data-action="edit-moment" data-id="${escapeHtml(moment.id)}">编辑</button>` : ""}</div></div></article>`).join("")
    : `<div class="empty-view"><span>◇</span><h3>暂时没有记录</h3><p>${escapeHtml(emptyText)}</p><button class="primary-button" data-action="new-moment" data-type="回忆">记录新的瞬间</button></div>`);
}

function renderOnThisDay() {
  const now = new Date();
  const key = `${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  renderMomentSelection("那年今日", (state.data?.moments || []).filter(moment => String(moment.occurred_at).slice(5, 10) === key), "往年的今天还没有记录；今天写下的内容，明年会在这里重逢。");
}

function renderMonthMoments(month) {
  renderMomentSelection(`${month} 月份回忆`, (state.data?.moments || []).filter(moment => String(moment.occurred_at).slice(0, 7) === month), "这个月还没有共同瞬间。");
}

function renderLocationMoments(location) {
  renderMomentSelection(`${location} · 足迹`, (state.data?.moments || []).filter(moment => moment.location === location), "这个地点来自旅行或位置记录，暂时没有带正文的恋爱瞬间。");
}

function renderLifeData() {
  const host = document.getElementById("lifeDataList");
  if (!host || !state.data) return;
  const entries = (state.data.calendar || []).filter(item => state.lifeFilter === "all" || state.lifeFilter === item.entry_type);
  const preferences = state.lifeFilter === "all" || state.lifeFilter === "preference" ? (state.data.preferences || []) : [];
  const entryLabels = { event: "日历", anniversary: "纪念日", todo: "待办" };
  const records = entries.map(item => `<article class="life-record" data-record-id="${escapeHtml(item.id)}" data-record-type="${escapeHtml(item.entry_type)}"><span class="life-icon ${item.entry_type === "anniversary" ? "rose" : item.entry_type === "todo" ? "sage" : "wine"}">${item.entry_type === "anniversary" ? "♡" : item.entry_type === "todo" ? "✓" : "◇"}</span><button class="grow shared-open" data-action="open-shared-detail" data-resource-type="calendar_entry" data-id="${escapeHtml(item.id)}"><strong>${escapeHtml(item.title)}</strong><small>${entryLabels[item.entry_type]} · ${escapeHtml(String(item.starts_at).slice(0,10))}${item.recurrence !== "none" ? ` · ${escapeHtml(item.recurrence === "yearly" ? "每年显示" : item.recurrence)}` : ""}${item.completed_at ? " · 已完成" : ""}</small>${item.location || item.note ? `<p>${escapeHtml([item.location, item.note].filter(Boolean).join(" · "))}</p>` : ""}<em class="shared-open-hint">查看详情与双方评论 →</em></button><div class="record-actions">${item.entry_type === "todo" ? `<button data-action="toggle-calendar" data-id="${item.id}" data-version="${item.version}" data-completed="${item.completed_at ? "1" : "0"}">${item.completed_at ? "恢复" : "完成"}</button>` : ""}<button data-action="edit-calendar" data-id="${item.id}">编辑</button><button data-action="delete-calendar" data-id="${item.id}" data-version="${item.version}">删除</button></div></article>`);
  const prefs = preferences.map(item => `<article class="life-record" data-record-id="${escapeHtml(item.id)}" data-record-type="preference"><span class="life-icon sand">✎</span><button class="grow shared-open" data-action="open-shared-detail" data-resource-type="preference" data-id="${escapeHtml(item.id)}"><strong>${escapeHtml(item.subject_name || "TA")} · ${escapeHtml(item.label)}</strong><small>${escapeHtml(item.category)} · ${escapeHtml(item.value)}${item.note ? ` · ${escapeHtml(item.note)}` : ""}</small><em class="shared-open-hint">查看详情与双方评论 →</em></button><div class="record-actions"><button data-action="edit-preference" data-id="${item.id}">编辑</button><button data-action="delete-preference" data-id="${item.id}" data-version="${item.version}">删除</button></div></article>`);
  host.innerHTML = [...records, ...prefs].join("") || `<div class="empty-view"><span>♡</span><h3>这个分类还没有内容</h3><p>可通过上方“添加”或对应功能卡创建真实记录。</p></div>`;
}

const mealSlotLabels = { breakfast: "早餐", lunch: "午餐", dinner: "晚餐", snack: "加餐" };
const mealStatusLabels = { selected: "待确认", accepted: "已确认", shopping: "采购中", cooking: "烹饪中", completed: "已完成", cancelled: "已取消" };

function localDateValue(value = new Date()) {
  const dateValue = value instanceof Date ? value : new Date(value);
  return `${dateValue.getFullYear()}-${String(dateValue.getMonth() + 1).padStart(2, "0")}-${String(dateValue.getDate()).padStart(2, "0")}`;
}

function monthBounds(value = state.mealMonth) {
  const start = new Date(value.getFullYear(), value.getMonth(), 1);
  const end = new Date(value.getFullYear(), value.getMonth() + 1, 0);
  return { start: localDateValue(start), end: localDateValue(end) };
}

function weekBounds(value = state.mealSelectedDate) {
  const selected = value instanceof Date ? new Date(value) : new Date(`${value}T00:00:00`);
  const start = new Date(selected);
  start.setDate(selected.getDate() - ((selected.getDay() + 6) % 7));
  const end = new Date(start); end.setDate(start.getDate() + 6);
  return { start: localDateValue(start), end: localDateValue(end), startDate: start, endDate: end };
}

function mealCalendarBounds() {
  return state.mealCalendarView === "week" ? weekBounds() : monthBounds();
}

function setMealSection(section = "recipes", { load = true } = {}) {
  state.mealSection = ["recipes", "wishes", "calendar", "shopping"].includes(section) ? section : "recipes";
  localStorage.setItem("love-meal-section", state.mealSection);
  document.querySelectorAll("[data-meal-section]").forEach(button => button.classList.toggle("active", button.dataset.mealSection === state.mealSection));
  document.querySelectorAll("[data-meal-panel]").forEach(panel => { panel.hidden = panel.dataset.mealPanel !== state.mealSection; });
  if (load && state.data) loadMeals({ quiet: true });
}

async function loadMeals({ quiet = false } = {}) {
  if (!state.data) return;
  const bounds = mealCalendarBounds();
  const query = new URLSearchParams({ limit: "50" });
  const search = document.getElementById("recipeSearchInput")?.value.trim();
  const cuisine = document.getElementById("recipeCuisine")?.value;
  const mealSlot = document.getElementById("recipeMealSlot")?.value;
  const difficulty = document.getElementById("recipeDifficulty")?.value;
  const maxMinutes = document.getElementById("recipeMaxMinutes")?.value;
  if (search) query.set("q", search);
  if (cuisine) query.set("cuisine", cuisine);
  if (mealSlot) query.set("meal_slot", mealSlot);
  if (difficulty) query.set("difficulty", difficulty);
  if (maxMinutes) query.set("max_minutes", maxMinutes);
  try {
    const [recipes, wishes, cart, calendar, shopping] = await Promise.all([
      api(`/api/v1/recipes?${query}`), api("/api/v1/meal-wishes"), api("/api/v1/meal-cart"),
      api(`/api/v1/meal-calendar?from=${bounds.start}&to=${bounds.end}`), api("/api/v1/shopping-lists/active")
    ]);
    state.meals = { ...state.meals, recipes: recipes.items, recipeNextCursor: recipes.next_cursor, recipeTotal: recipes.total, wishes: wishes.items, cart, calendar, shopping };
    const cacheKey = mealUserCacheKey();
    localStorage.setItem(`love-offline-meals:${state.data.user.id}`, JSON.stringify({ ...state.meals, recipeDrafts: [] }));
    mealCachePut("cache", cacheKey, { ...state.meals, recipeDrafts: [] }).catch(() => {});
    localStorage.removeItem("love-offline-meals");
    flushMealMutations().catch(() => {});
    renderMeals();
  } catch (error) {
    const cached = await mealCacheGet("cache", mealUserCacheKey()) || readLocalJson(`love-offline-meals:${state.data.user.id}`);
    if (cached) { state.meals = { ...state.meals, ...cached }; renderMeals(); }
    if (!quiet) toast(error.message);
  }
}

function renderMeals() {
  renderRecipeList(); renderMealWishes(); renderMealCalendar(); renderShoppingList();
  const summary = state.data?.mealSummary || {};
  const label = document.getElementById("lifeMealSummary");
  if (label) label.textContent = summary.next_meal ? `${shortDate(summary.next_meal.date)} · ${summary.next_meal.name}` : `${state.meals.wishes.length} 个想吃 · ${state.meals.shopping?.items?.filter(item => !item.checked).length || 0} 项待采购`;
  const status = document.getElementById("mealStatusNote");
  if (status) status.textContent = `菜谱 ${state.meals.recipeTotal || state.meals.recipes.length} 道 · 想吃 ${state.meals.wishes.length} 项 · 已安排 ${state.meals.cart?.items?.filter(item => item.status !== "cancelled").length || 0} 道 · 待采购 ${state.meals.shopping?.items?.filter(item => !item.checked).length || 0} 项`;
}

function renderRecipeList() {
  const host = document.getElementById("recipeList");
  if (!host) return;
  const drafts = (state.meals.recipeDrafts || []).map(draft => {
    const cover = exactRecipeCover(draft.recipe);
    return `<article class="recipe-card recipe-card-draft">${recipeCoverMarkup(cover, draft.recipe.name)}<span class="recipe-badge">待核对 · ${escapeHtml(draft.source_provider || "智能生成")}</span><strong>${escapeHtml(draft.recipe.dish_name || draft.recipe.name)}</strong><em>${escapeHtml(draft.recipe.variant_name || draft.recipe.cooking_method || "经典做法")}</em>${draft.recipe.name !== (draft.recipe.dish_name || draft.recipe.name) ? `<small class="recipe-display-name">${escapeHtml(draft.recipe.name)}</small>` : ""}<small>${draft.recipe.cuisine === "western" ? "西餐" : "中餐"} · ${draft.recipe.prep_minutes + draft.recipe.cook_minutes} 分钟 · ${draft.recipe.ingredients.length} 种食材</small><div class="meal-status-actions"><button data-action="view-recipe-draft" data-id="${escapeHtml(draft.id)}">查看并核对</button></div></article>`;
  });
  const recipes = (state.meals.recipes || []).map(recipe => `<button class="recipe-card" data-action="view-recipe" data-id="${escapeHtml(recipe.id)}">${recipeCoverMarkup(recipe.cover, recipe.name)}<span class="recipe-badge">${recipe.cuisine === "western" ? "西餐" : "中餐"} · ${escapeHtml(recipe.category)}</span><strong>${escapeHtml(recipe.dish_name || recipe.name)}</strong><em>${escapeHtml(recipe.variant_name || recipe.cooking_method || "经典做法")}${recipe.variant_count > 1 ? ` · 共 ${recipe.variant_count} 种做法` : ""}</em>${recipe.name !== (recipe.dish_name || recipe.name) ? `<small class="recipe-display-name">${escapeHtml(recipe.name)}</small>` : ""}<small>准备 ${recipe.prep_minutes} 分钟 · 烹饪 ${recipe.cook_minutes} 分钟${recipe.passive_minutes ? ` · 等待 ${formatRecipeDuration(recipe.passive_minutes * 60)}` : ""} · ${recipe.base_servings} 人份</small></button>`);
  const more = state.meals.recipeNextCursor ? `<button class="recipe-load-more" data-action="load-more-recipes">继续加载菜谱</button>` : "";
  host.innerHTML = ([...drafts, ...recipes].join("") || `<div class="empty-view"><span>♨</span><h3>没有找到菜谱</h3><p>换个关键词，或创建自己的中文菜谱。</p></div>`) + more;
}

function exactRecipeCover(recipe) {
  if (recipe?.cover?.url) return recipe.cover;
  const media = (recipe?.media || []).find(item => item.media_type === "image" && item.role === "cover" && item.match_status === "verified" && item.availability_status !== "unavailable");
  return media ? { url: media.url, thumbnail_url: media.thumbnail_url, attribution: media.attribution, provider: media.provider } : null;
}

function recipeCoverMarkup(cover, name, large = false) {
  if (!cover?.url) return `<div class="recipe-cover-missing ${large ? "large" : ""}"><span>待补准确图片</span><small>不使用相似菜图代替</small></div>`;
  const source = cover.thumbnail_url || cover.url;
  return `<figure class="recipe-cover ${large ? "large" : ""}"><img data-recipe-cover src="${escapeHtml(source)}" alt="${escapeHtml(name)}的已核对成品图" referrerpolicy="no-referrer"><figcaption>${escapeHtml(cover.attribution || "已核对为当前菜品与做法")}</figcaption><div class="recipe-cover-load-error"><span>图片暂时无法载入</span><small>原记录仍保留，可稍后重试</small></div></figure>`;
}

function domesticVideoSearchUrl(recipe) {
  const words = [recipe?.dish_name || recipe?.name, recipe?.variant_name, recipe?.cooking_method].filter(Boolean).join(" ");
  return `https://search.bilibili.com/all?keyword=${encodeURIComponent(`${words} 做法`)}`;
}

function renderRecipeVideos(recipe) {
  const videos = (recipe.media || []).filter(item => ["video", "link"].includes(item.media_type));
  const cards = videos.map(item => {
    const score = Math.round(Number(item.match_details?.alignment_score || 0) * 100);
    const paired = item.role === "paired" && item.match_status === "verified";
    const differences = item.match_details?.differences || [];
    return `<article class="recipe-video-card"><div><span class="recipe-badge">${paired ? `已核对配套 · ${score}%` : "参考视频"}</span><strong>${escapeHtml(item.title || "外部做菜视频")}</strong><small>${paired ? "食材、调料和主要流程已人工核对" : "仅供参考，请以当前页面食材用量和步骤为准"}${differences.length ? ` · 差异：${escapeHtml(differences.join("；"))}` : ""}</small></div><div class="meal-status-actions"><a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">打开视频</a><button data-action="check-recipe-media" data-id="${escapeHtml(item.id)}">检查链接</button></div></article>`;
  }).join("");
  return `<section class="recipe-media-section"><div class="recipe-media-heading"><div><h3>视频教学</h3><p>配套视频需核对用量、备菜和流程；其余视频明确标为参考。</p></div></div>${cards || `<p class="form-note">暂时没有已核对的视频。可以添加国内平台链接，或按当前做法搜索。</p>`}<div class="recipe-video-actions"><button data-action="add-recipe-video" data-id="${escapeHtml(recipe.id || "")}">＋ 添加视频链接</button><a href="${escapeHtml(domesticVideoSearchUrl(recipe))}" target="_blank" rel="noopener noreferrer">在哔哩哔哩搜索这种做法</a></div></section>`;
}

function renderMealWishes() {
  const host = document.getElementById("mealWishList");
  if (!host) return;
  host.innerHTML = state.meals.wishes.length ? state.meals.wishes.map(item => {
    const openAction = item.recipe_id ? "view-wish-recipe" : "match-wish-recipe";
    return `<article class="meal-row meal-wish-row"><span class="life-icon rose">♡</span><button class="grow meal-wish-open" data-action="${openAction}" data-id="${escapeHtml(item.id)}" data-recipe-id="${escapeHtml(item.recipe_id || "")}"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.creator_name || "我们")}想吃${item.note ? ` · ${escapeHtml(item.note)}` : ""}</small><em>${item.recipe_id ? `已关联：${escapeHtml(item.recipe_name || "可查看菜谱")}` : "点这里选择具体菜谱和做法"}</em></button><div class="meal-row-actions">${item.recipe_id ? `<button data-action="view-wish-recipe" data-id="${escapeHtml(item.id)}" data-recipe-id="${escapeHtml(item.recipe_id)}">看做法</button><button data-action="schedule-wish" data-id="${escapeHtml(item.id)}" data-recipe-id="${escapeHtml(item.recipe_id)}" data-name="${escapeHtml(item.title)}">加入点菜车</button>` : `<button data-action="match-wish-recipe" data-id="${escapeHtml(item.id)}">选择菜谱</button>`}<button data-action="open-shared-detail" data-resource-type="meal_wish" data-id="${escapeHtml(item.id)}">详情与评论</button><button data-action="delete-meal-wish" data-id="${escapeHtml(item.id)}" data-version="${item.version}">删除</button></div></article>`;
  }).join("") : `<div class="empty-view"><span>♡</span><h3>还没有想吃的菜</h3><p>可以从菜谱详情加入，也可以直接写下菜名。</p></div>`;
}

function mealWishById(wishId) {
  return (state.meals.wishes || []).find(item => item.id === wishId);
}

function wishRecipeKeyword(title) {
  const original = String(title || "").trim();
  const cleaned = original
    .replace(/[，。！？、,.!?]/g, " ")
    .replace(/(?:今天|明天|周末|有空时|下次|宝宝|宝贝|我|我们|想要|想吃|吃|做给|给我做|来一份|来一道)/g, " ")
    .replace(/\s+/g, " ").trim();
  return cleaned || original;
}

function renderWishRecipeCandidates(items, keyword) {
  const host = document.getElementById("mealWishRecipeResults");
  if (!host) return;
  state.meals.wishRecipeCandidates = items;
  if (!items.length) {
    host.innerHTML = `<div class="empty-view"><span>♨</span><h3>没有找到“${escapeHtml(keyword)}”</h3><p>换一个更短的菜名，例如把“想吃香煎牛排”改成“牛排”。</p><button data-action="browse-recipes-for-wish">去完整菜谱库继续找</button></div>`;
    return;
  }
  host.innerHTML = `<p class="form-note">找到 ${items.length} 种相关做法。选择后会保存到双方共享的想吃清单。</p><div class="meal-wish-candidate-list">${items.map(recipe => `<article class="meal-wish-candidate">${recipeCoverMarkup(recipe.cover, recipe.name)}<div><span class="recipe-badge">${recipe.cuisine === "western" ? "西餐" : "中餐"} · ${escapeHtml(recipe.category)}</span><strong>${escapeHtml(recipe.dish_name || recipe.name)}</strong><small>${escapeHtml(recipe.variant_name || recipe.cooking_method || "经典做法")} · ${recipe.prep_minutes + recipe.cook_minutes} 分钟 · ${recipe.base_servings} 人份</small></div><div class="meal-wish-candidate-actions"><button data-action="choose-wish-recipe" data-mode="detail" data-recipe-id="${escapeHtml(recipe.id)}" data-name="${escapeHtml(recipe.name)}">选择并看做法</button><button data-action="choose-wish-recipe" data-mode="schedule" data-recipe-id="${escapeHtml(recipe.id)}" data-name="${escapeHtml(recipe.name)}">直接加入点菜车</button></div></article>`).join("")}</div>`;
}

async function searchRecipesForWish(keyword) {
  const value = String(keyword || "").trim();
  const host = document.getElementById("mealWishRecipeResults");
  if (!value) { if (host) host.innerHTML = `<p class="form-note">请输入想找的菜名。</p>`; return; }
  if (host) host.innerHTML = `<div class="loading-state">正在从本地中文菜谱库查找“${escapeHtml(value)}”…</div>`;
  try {
    const result = await api(`/api/v1/recipes?${new URLSearchParams({ q: value, limit: "20" })}`);
    renderWishRecipeCandidates(result.items || [], value);
  } catch (error) {
    if (host) host.innerHTML = `<div class="empty-view"><h3>菜谱暂时无法读取</h3><p>${escapeHtml(error.message)}</p></div>`;
  }
}

async function openWishRecipePicker(wishId) {
  const wish = mealWishById(wishId);
  if (!wish) { toast("这条想吃内容已被修改，请刷新后重试"); return; }
  state.meals.openWishId = wish.id;
  const keyword = wishRecipeKeyword(wish.title);
  document.getElementById("mealWishRecipeTitle").textContent = `给“${wish.title}”选择菜谱`;
  document.getElementById("mealWishRecipeNote").textContent = `${wish.creator_name || "对方"}想吃${wish.note ? `：${wish.note}` : "这道菜"}。选择具体做法后，两个人都能查看食材和步骤。`;
  document.getElementById("mealWishRecipeSearch").value = keyword;
  document.getElementById("mealWishRecipeResults").innerHTML = `<div class="loading-state">正在匹配菜谱…</div>`;
  openModal("mealWishRecipeModal");
  await searchRecipesForWish(keyword);
}

function mealStatusButtons(item) {
  const actions = [];
  if (!["completed", "cancelled"].includes(item.status)) actions.push(`<button data-action="edit-meal" data-id="${item.id}">修改安排</button>`);
  if (item.status === "selected") actions.push(`<button data-action="meal-status" data-id="${item.id}" data-version="${item.version}" data-status="accepted">确认这道菜</button>`);
  if (item.status === "shopping") actions.push(`<button data-action="meal-status" data-id="${item.id}" data-version="${item.version}" data-status="cooking">开始做饭</button>`);
  if (item.status === "cooking") actions.push(`<button data-action="meal-status" data-id="${item.id}" data-version="${item.version}" data-status="completed">完成并归档</button>`);
  if (!["completed", "cancelled"].includes(item.status)) actions.push(`<button data-action="cancel-meal" data-id="${item.id}" data-version="${item.version}">取消</button>`);
  if (["completed", "cancelled"].includes(item.status)) actions.push(`<button data-action="repeat-meal" data-id="${item.id}">再安排一次</button>`);
  return actions.join("");
}

function renderMealCalendar() {
  const calendarHost = document.getElementById("mealCalendar");
  const listHost = document.getElementById("mealDayList");
  if (!calendarHost || !listHost) return;
  const isWeek = state.mealCalendarView === "week";
  const week = weekBounds();
  document.getElementById("mealCalendarTitle").textContent = isWeek
    ? `${shortDate(week.start)}—${shortDate(week.end)}`
    : `${state.mealMonth.getFullYear()}年${state.mealMonth.getMonth() + 1}月`;
  document.querySelectorAll('[data-action="meal-calendar-view"]').forEach(button => button.classList.toggle("active", button.dataset.view === state.mealCalendarView));
  const lookup = new Map((state.meals.calendar?.days || []).map(day => [day.date, day]));
  const first = new Date(state.mealMonth.getFullYear(), state.mealMonth.getMonth(), 1);
  const gridStart = isWeek ? week.startDate : new Date(first);
  if (!isWeek) gridStart.setDate(first.getDate() - ((first.getDay() + 6) % 7));
  const weekdays = ["一", "二", "三", "四", "五", "六", "日"].map(day => `<span class="weekday">${day}</span>`);
  const cells = [];
  for (let index = 0; index < (isWeek ? 7 : 42); index += 1) {
    const day = new Date(gridStart); day.setDate(gridStart.getDate() + index);
    const key = localDateValue(day); const data = lookup.get(key);
    const activeItems = data ? Object.values(data.meals).flat().filter(item => item.status !== "cancelled") : [];
    const hasMeal = activeItems.length > 0;
    const mealNames = !isWeek && hasMeal ? `<small>${activeItems.slice(0, 3).map(item => escapeHtml(item.name)).join("、")}${activeItems.length > 3 ? ` +${activeItems.length - 3}` : ""}</small>` : "";
    cells.push(`<button data-action="select-meal-date" data-date="${key}" aria-label="${key}${hasMeal ? `，有 ${activeItems.length} 项用餐安排` : ""}" class="${!isWeek && day.getMonth() !== state.mealMonth.getMonth() ? "outside " : ""}${hasMeal ? "has-meal " : ""}${state.mealSelectedDate === key ? "selected" : ""}"><span>${day.getDate()}</span>${mealNames}</button>`);
  }
  calendarHost.innerHTML = [...weekdays, ...cells].join("");
  const selected = lookup.get(state.mealSelectedDate);
  if (!selected) { listHost.innerHTML = `<div class="empty-view"><span>♨</span><h3>${shortDate(state.mealSelectedDate)}还没有安排</h3><p>从菜谱中选择一道菜加入这一天。</p></div>`; return; }
  const groups = Object.entries(selected.meals).filter(([, items]) => items.length).map(([slot, items]) => `<section class="meal-day-card meal-slot-group"><h4>${mealSlotLabels[slot]}</h4>${items.map(item => { const overdue = item.planned_date < localDateValue() && !["completed", "cancelled"].includes(item.status); return `<article class="meal-row"><div class="grow"><button class="shared-open" data-action="open-shared-detail" data-resource-type="meal_item" data-id="${escapeHtml(item.id)}"><strong>${escapeHtml(item.name)}</strong><small>${item.planned_time || "未设时间"} · ${item.servings} 人份 · ${overdue ? `未完成（${mealStatusLabels[item.status] || item.status}）` : mealStatusLabels[item.status] || item.status}${item.note ? ` · ${escapeHtml(item.note)}` : ""}</small><em class="shared-open-hint">查看详情与双方评论 →</em></button><div class="meal-status-actions">${mealStatusButtons(item)}</div></div></article>`; }).join("")}</section>`);
  listHost.innerHTML = groups.join("") || `<div class="empty-view"><h3>这一天暂无有效安排</h3></div>`;
}

function renderShoppingList() {
  const host = document.getElementById("shoppingList");
  if (!host) return;
  const shopping = state.meals.shopping || { items: [] };
  if (!shopping.items?.length) { host.innerHTML = `<div class="empty-view"><span>⌑</span><h3>采购清单还是空的</h3><p>先确认菜单，再点击“生成清单”查看差异后保存。</p></div>`; return; }
  let category = ""; const rows = [];
  for (const item of shopping.items) {
    if (item.category !== category) { category = item.category; rows.push(`<h4 class="shopping-category">${escapeHtml(category)}</h4>`); }
    const sources = (item.sources || []).map(source => source.recipe_name).filter(Boolean).join("、");
    rows.push(`<article class="shopping-row ${item.checked ? "checked" : ""}"><input type="checkbox" data-shopping-check="${item.id}" data-version="${item.version}" ${item.checked ? "checked" : ""} aria-label="${escapeHtml(item.display_name)}已购买"><div><strong>${escapeHtml(item.display_name)} · ${escapeHtml(item.display_quantity)}</strong><small>${item.manual_locked ? "手动锁定" : sources ? `来自：${escapeHtml(sources)}` : "手动添加"}</small></div><div class="shopping-row-actions"><button data-action="edit-shopping-item" data-id="${item.id}" aria-label="编辑${escapeHtml(item.display_name)}">编辑</button><button data-action="delete-shopping-item" data-id="${item.id}" data-version="${item.version}" aria-label="删除${escapeHtml(item.display_name)}">×</button></div></article>`);
  }
  host.innerHTML = `<p class="form-note">${shortDate(shopping.range_start)} 至 ${shortDate(shopping.range_end)} · 已买 ${shopping.items.filter(item => item.checked).length}/${shopping.items.length}</p>${rows.join("")}`;
}

async function openRecipeDetail(recipeOrId, draftId = "", wishId = "") {
  let recipe = recipeOrId;
  if (typeof recipeOrId === "string") {
    const cacheKey = mealUserCacheKey(`recipe:${recipeOrId}`);
    try {
      recipe = await api(`/api/v1/recipes/${encodeURIComponent(recipeOrId)}`);
      mealCachePut("cache", cacheKey, recipe).catch(() => {});
    } catch (error) {
      recipe = await mealCacheGet("cache", cacheKey);
      if (!recipe) throw error;
      toast("当前离线，正在显示最近缓存的菜谱");
    }
  }
  state.meals.openRecipe = recipe;
  state.meals.openRecipeDraftId = draftId;
  state.meals.openWishId = wishId;
  const source = recipe.source_type === "built_in" ? "内置中文菜谱" : recipe.source_provider ? `来源：${recipe.source_provider}` : "我们的菜谱";
  const owned = !draftId && recipe.owner_id === state.data?.user?.id && recipe.source_type !== "built_in";
  const baseServings = Math.max(1, Number(recipe.base_servings) || 1);
  const cover = exactRecipeCover(recipe);
  const canAttachImage = draftId || owned || recipe.source_type === "built_in";
  const imageVerifier = canAttachImage ? `<details class="recipe-image-verifier"><summary>维护这张菜谱的成品图</summary><h3>${cover ? "替换准确成品图" : "补充准确成品图"}</h3><p>这里只用于维护菜谱资料；做菜时可以忽略。只能选择这道菜、这种做法的真实成品图。</p><input id="recipeExactCoverInput" type="file" accept="image/jpeg,image/png,image/webp,image/gif"><label class="check-field"><input id="recipeExactCoverConfirmed" type="checkbox"><span>我已核对菜品名称、外观和当前做法版本一致</span></label>${draftId ? "" : `<button data-action="upload-recipe-image" data-id="${escapeHtml(recipe.id)}">${recipe.source_type === "built_in" ? "复制菜谱并保存图片" : "保存这张准确图片"}</button>`}</details>` : "";
  const variants = (recipe.variants || []).map(item => `<button class="${item.id === recipe.id ? "active" : ""}" data-action="open-recipe-variant" data-id="${escapeHtml(item.id)}"><strong>${escapeHtml(item.variant_name || item.cooking_method || "经典做法")}</strong><small>${escapeHtml(item.cooking_method || "")} · ${Number(item.prep_minutes) + Number(item.cook_minutes) + Number(item.passive_minutes || 0)} 分钟</small></button>`).join("");
  const variantPanel = !draftId && variants ? `<section class="recipe-variants"><div class="recipe-media-heading"><div><h3>不同做法</h3><p>同一道菜按具体做法分开保存，食材、调料和步骤不会混用。</p></div>${recipe.variants.length > 1 ? `<button data-action="compare-recipe-variants" data-dish-id="${escapeHtml(recipe.dish_id)}">比较做法</button>` : ""}</div><div class="recipe-variant-list">${variants}</div><div id="recipeVariantComparison"></div></section>` : "";
  document.getElementById("recipeDetailContent").innerHTML = `${recipeCoverMarkup(cover, recipe.name, true)}${imageVerifier}<div class="recipe-detail-head"><span class="recipe-badge">${escapeHtml(recipe.dish_name || recipe.name)} · ${escapeHtml(recipe.variant_name || recipe.cooking_method || "经典做法")}</span><h2 id="recipeDetailTitle">${escapeHtml(recipe.name)}</h2><p>${escapeHtml(source)} · ${baseServings} 人份 · 准备 ${recipe.prep_minutes} 分钟 · 烹饪 ${recipe.cook_minutes} 分钟${recipe.passive_minutes ? ` · 浸泡/冷藏/发酵等待 ${formatRecipeDuration(recipe.passive_minutes * 60)}` : ""}</p>${recipe.method_tags?.length ? `<div class="recipe-method-tags">${recipe.method_tags.map(tag => `<span>${escapeHtml(tag)}</span>`).join("")}</div>` : ""}</div>${variantPanel}<div class="recipe-detail-columns"><div class="recipe-serving-control"><h3>食材和调味</h3><label>按人数换算 <input id="recipeDetailServings" type="number" min="1" max="20" value="${baseServings}" aria-label="按人数换算食材"></label></div><div id="recipeDetailIngredients">${renderRecipeIngredients(recipe, baseServings)}</div><h3>一步一步做</h3><ol id="recipeDetailSteps" class="recipe-step-list">${recipe.steps.map((step, index) => `<li class="recipe-step-card"><div class="recipe-step-number">${index + 1}</div><div><strong>${escapeHtml(step.title || `第 ${index + 1} 步`)}</strong><p>${escapeHtml(step.instruction)}</p><div class="recipe-step-meta">${step.heat_level ? `<span>火力：${escapeHtml(step.heat_level)}</span>` : ""}${step.timer_seconds ? `<span>参考：${formatRecipeDuration(step.timer_seconds)}</span>` : `<span>无需单独计时</span>`}</div>${step.key_point ? `<small>完成判断：${escapeHtml(step.key_point)}</small>` : ""}</div></li>`).join("")}</ol>${recipe.tips?.length ? `<h3>小贴士</h3><ul>${recipe.tips.map(tip => `<li>${escapeHtml(tip)}</li>`).join("")}</ul>` : ""}${renderRecipeAllergens(recipe)}</div>${draftId ? "" : renderRecipeVideos(recipe)}<div class="recipe-detail-actions">${draftId ? `<button data-action="confirm-recipe-draft" data-id="${escapeHtml(draftId)}">核对后保存这份菜谱</button>` : `<button data-action="schedule-recipe" data-id="${escapeHtml(recipe.id)}" data-name="${escapeHtml(recipe.name)}" data-wish-id="${escapeHtml(wishId)}">${wishId ? "加入点菜车" : "安排到用餐日历"}</button>${wishId ? "" : `<button data-action="wish-recipe" data-id="${escapeHtml(recipe.id)}" data-name="${escapeHtml(recipe.name)}">先放进想吃清单</button>`}${owned ? `<button data-action="edit-recipe" data-id="${escapeHtml(recipe.id)}">编辑菜谱</button><button data-action="delete-recipe" data-id="${escapeHtml(recipe.id)}" data-version="${recipe.version}">删除菜谱</button>` : ""}`}</div>`;
  openModal("recipeDetailModal");
}

function readableRecipeQuantity(item, servings, baseServings) {
  if (item.quantity_milli == null) return item.quantity_text || item.display_quantity || "适量";
  let value = (Number(item.quantity_milli) / 1000) * servings / Math.max(1, baseServings);
  let unit = item.canonical_unit === "g" ? "克" : item.canonical_unit === "ml" ? "毫升" : "个";
  if (item.canonical_unit === "g" && value >= 1000) { value /= 1000; unit = "千克"; }
  if (item.canonical_unit === "ml" && value >= 1000) { value /= 1000; unit = "升"; }
  const rounded = Math.round(value * 100) / 100;
  return `${Number.isInteger(rounded) ? rounded : rounded.toFixed(2).replace(/0+$/, "").replace(/\.$/, "")} ${unit}`;
}

function renderRecipeIngredients(recipe, servings) {
  const baseServings = Math.max(1, Number(recipe.base_servings) || 1);
  const rows = group => (recipe.ingredients || []).filter(item => (item.ingredient_group || "main") === group).map(item => `<li><div><strong>${escapeHtml(item.name)}</strong><b>${escapeHtml(readableRecipeQuantity(item, servings, baseServings))}${item.optional ? "（可选）" : ""}</b></div>${item.preparation || item.specification ? `<small>处理：${escapeHtml(item.preparation || item.specification)}</small>` : ""}</li>`).join("");
  const main = rows("main"); const seasoning = rows("seasoning");
  return `${main ? `<h4>主要食材</h4><ul class="recipe-ingredient-list">${main}</ul>` : ""}${seasoning ? `<h4>辅料与调味</h4><ul class="recipe-ingredient-list seasoning-list">${seasoning}</ul><p class="form-note">调味量按当前人数换算；不同品牌咸度有差异，临出锅前先尝再微调。</p>` : ""}`;
}

function renderRecipeAllergens(recipe) {
  const details = Array.isArray(recipe.allergen_details) ? recipe.allergen_details : [];
  const legacyTags = Array.isArray(recipe.allergen_tags) ? recipe.allergen_tags : [];
  const review = Array.isArray(recipe.allergen_review_ingredients) ? recipe.allergen_review_ingredients : [];
  const confirmed = details.length
    ? `<ul class="recipe-allergen-list">${details.map(item => `<li><strong>${escapeHtml(item.name)}</strong><span>来源：${(item.sources || []).map(escapeHtml).join("、") || "当前配料表"}</span></li>`).join("")}</ul>`
    : legacyTags.length
      ? `<p class="recipe-allergens">${legacyTags.map(escapeHtml).join("、")}（刷新后可查看具体来源）</p>`
      : `<p class="recipe-allergens safe">当前配料表未识别到常见过敏原</p>`;
  const packaged = review.length
    ? `<p class="recipe-allergen-review"><strong>请核对包装标签：</strong>${review.map(escapeHtml).join("、")}属于复合/加工食品，不同品牌配方可能不同。</p>`
    : "";
  const note = escapeHtml(recipe.allergen_note || "仅按当前配料表识别；豆角、红豆、绿豆、豌豆等不会自动归为大豆，复合调味料以包装标签为准。");
  return `<section class="recipe-allergen-report"><h3>过敏原提示（按配料识别）</h3>${confirmed}${packaged}<p class="form-note">${note}</p></section>`;
}

function formatRecipeDuration(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value >= 3600) return `${Math.round(value / 3600 * 10) / 10} 小时`;
  if (value >= 60) return `${Math.round(value / 60)} 分钟`;
  return `${Math.round(value)} 秒`;
}

function recipeIngredientEditLine(item) {
  if (item.quantity_milli == null) return `${item.name} | | ${item.quantity_text || "适量"} | ${item.preparation || ""}`;
  const quantity = Number(item.quantity_milli) / 1000;
  const unit = item.canonical_unit === "g" ? "克" : item.canonical_unit === "ml" ? "毫升" : "个";
  return `${item.name} | ${quantity} | ${unit} | ${item.preparation || ""}`;
}

function recipeStepEditLine(step, index) {
  const minutes = step.timer_seconds ? Math.round(Number(step.timer_seconds) / 6) / 10 : "";
  return `${step.title || `第 ${index + 1} 步`} | ${step.instruction} | ${step.heat_level || "按步骤调整"} | ${minutes} | ${step.key_point || "达到步骤描述的状态后再继续"}`;
}

function openRecipeEditor(recipe) {
  if (!recipe) return;
  const form = document.getElementById("customRecipeForm"); form.reset(); form.dataset.recipeId = recipe.id; form.dataset.version = recipe.version;
  document.getElementById("customRecipeTitle").textContent = "编辑中文菜谱";
  document.getElementById("customRecipeDishName").value = recipe.dish_name || recipe.name;
  document.getElementById("customRecipeVariantName").value = recipe.variant_name || "经典做法";
  document.getElementById("customRecipeName").value = recipe.name;
  document.getElementById("customRecipeCookingMethod").value = recipe.cooking_method || recipe.variant_name || "经典做法";
  document.getElementById("customRecipeMethodTags").value = (recipe.method_tags || []).join("、");
  document.getElementById("customRecipeCuisine").value = recipe.cuisine;
  document.getElementById("customRecipeCategory").value = recipe.category;
  document.getElementById("customRecipeServings").value = String(recipe.base_servings);
  document.getElementById("customRecipeMinutes").value = String(recipe.prep_minutes + recipe.cook_minutes);
  document.getElementById("customRecipePassiveMinutes").value = String(recipe.passive_minutes || 0);
  document.getElementById("customRecipeIngredients").value = recipe.ingredients.map(recipeIngredientEditLine).join("\n");
  document.getElementById("customRecipeSteps").value = recipe.steps.map(recipeStepEditLine).join("\n");
  document.getElementById("customRecipeVisibility").value = recipe.visibility;
  document.getElementById("customRecipeCoverExact").checked = false;
  document.getElementById("customRecipeCover").required = false;
  form.querySelector(".primary-button").textContent = "保存修改";
  closeModals(); openModal("customRecipeModal");
}

function openMealSchedule(recipeId, name, wishId = "", servings = 2) {
  const form = document.getElementById("mealScheduleForm");
  form.reset(); delete form.dataset.itemId; delete form.dataset.version;
  form.querySelector(".primary-button").textContent = "加入点菜车和用餐日历";
  document.getElementById("mealScheduleRecipeId").value = recipeId;
  document.getElementById("mealScheduleWishId").value = wishId;
  document.getElementById("mealScheduleTitle").textContent = `安排：${name}`;
  document.getElementById("mealScheduleDate").value = state.mealSelectedDate || localDateValue();
  document.getElementById("mealScheduleServings").value = String(Math.min(20, Math.max(1, Number(servings) || 2)));
  document.getElementById("mealScheduleNote").value = "";
  closeModals(); openModal("mealScheduleModal");
}

function openMealEditor(item) {
  if (!item) return;
  state.meals.editingMeal = { ...item };
  const form = document.getElementById("mealScheduleForm");
  form.reset(); form.dataset.itemId = item.id; form.dataset.version = item.version;
  document.getElementById("mealScheduleRecipeId").value = item.recipe_id || "";
  document.getElementById("mealScheduleWishId").value = "";
  document.getElementById("mealScheduleTitle").textContent = `修改：${item.name}`;
  document.getElementById("mealScheduleDate").value = item.planned_date;
  document.getElementById("mealScheduleSlot").value = item.meal_slot;
  document.getElementById("mealScheduleTime").value = item.planned_time || "";
  document.getElementById("mealScheduleServings").value = String(item.servings);
  document.getElementById("mealScheduleCourse").value = item.course_type;
  document.getElementById("mealScheduleNote").value = item.note || "";
  form.querySelector(".primary-button").textContent = "保存修改";
  closeModals(); openModal("mealScheduleModal");
}

function shoppingEditorValues(item) {
  const canonical = item.canonical_unit;
  if (item.quantity_milli == null) return { quantity: 1, unit: "个" };
  const canonicalAmount = Number(item.quantity_milli) / 1000;
  if (canonical === "g" && canonicalAmount >= 1000) return { quantity: canonicalAmount / 1000, unit: "千克" };
  if (canonical === "ml" && canonicalAmount >= 1000) return { quantity: canonicalAmount / 1000, unit: "升" };
  return { quantity: canonicalAmount, unit: canonical === "g" ? "克" : canonical === "ml" ? "毫升" : "个" };
}

async function renderTimelineView(view, renderToken = state.timelineRenderToken) {
  const host = document.getElementById("timelineBody");
  if (renderToken !== state.timelineRenderToken) return;
  if (view === "list") { renderTimeline(); return; }
  host.innerHTML = `<div class="empty-view"><span>⌁</span><h3>正在整理共同回忆…</h3></div>`;
  try {
    if (view === "album") {
      const result = await api("/api/v1/timeline/months");
      if (renderToken !== state.timelineRenderToken) return;
      host.innerHTML = result.items.length ? `<div class="album-grid">${result.items.map(item => `<button class="album-month" data-action="open-month" data-month="${escapeHtml(item.month)}"><small>${escapeHtml(item.month)}</small><strong>${item.count} 个瞬间</strong><span>${item.media_count} 个媒体记录</span><b>查看本月内容 ›</b></button>`).join("")}</div>` : `<div class="empty-view"><span>◇</span><h3>暂无月份相册</h3><p>保存照片或瞬间后会按月归档。</p></div>`;
    } else if (view === "map") {
      const result = await api("/api/v1/timeline/footprints");
      if (renderToken !== state.timelineRenderToken) return;
      host.innerHTML = `<p class="form-note">${escapeHtml(result.notice || "")}</p>` + (result.items.length ? `<div class="footprint-list">${result.items.map(item => `<button class="footprint-item" data-action="open-location" data-location="${escapeHtml(item.name)}"><strong>${escapeHtml(item.name)}</strong><small>${Number(item.latitude).toFixed(4)}, ${Number(item.longitude).toFixed(4)} · ${escapeHtml(item.coordinate_source)}</small><b>查看相关记录 ›</b></button>`).join("")}</div>` : `<div class="empty-view"><span>⌖</span><h3>还没有足迹</h3><p>给瞬间添加地点后会显示在这里。</p></div>`);
    } else if (view === "report") {
      const report = await api(`/api/v1/timeline/reports/${new Date().getFullYear()}`);
      if (renderToken !== state.timelineRenderToken) return;
      host.innerHTML = `<div class="year-mark"><span>${report.year} 年度报告</span><i></i></div><div class="report-grid"><span><b>${report.moments}</b>瞬间</span><span><b>${report.messages}</b>消息</span><span><b>${report.trips}</b>已结束旅行</span><span><b>${report.planned_trips || 0}</b>计划中旅行</span><span><b>${Math.round(report.call_seconds/60)}</b>通话分钟</span><span><b>${report.cities}</b>回忆城市</span><span><b>${report.completed_todos}</b>完成待办</span></div><p class="form-note">${escapeHtml(report.privacy)}</p>`;
    } else if (view === "private") {
      renderMomentSelection("仅我的内容", (state.data?.moments || []).filter(moment => moment.creator_id === state.data.user.id && moment.visibility !== "shared"), "你还没有仅自己可见或延时公开的瞬间。");
    } else if (view === "trash") {
      const result = await api("/api/v1/moments/trash");
      if (renderToken !== state.timelineRenderToken) return;
      host.innerHTML = `<p class="form-note">本人删除的内容保留 30 天，之后自动永久清理；共同瞬间在此期间双方都可恢复。</p>` + (result.items.length ? result.items.map(item => `<article class="life-record"><div class="grow"><strong>${escapeHtml(item.title)}</strong><small>删除于 ${new Date(item.deleted_at).toLocaleString("zh-CN")}</small></div><div class="record-actions"><button data-action="restore-moment" data-id="${item.id}" data-version="${item.version}">恢复</button>${item.creator_id === state.data.user.id ? `<button data-action="purge-moment" data-id="${item.id}">永久删除</button>` : ""}</div></article>`).join("") : `<div class="empty-view"><span>◇</span><h3>回收站是空的</h3><p>误删的瞬间可以在这里恢复。</p></div>`);
    }
  } catch (error) {
    if (renderToken === state.timelineRenderToken) host.innerHTML = `<div class="empty-view"><h3>暂时无法加载</h3><p>${escapeHtml(error.message)}</p></div>`;
  }
}

async function saveMessageMoment(messageId) {
  try {
    await api(`/api/v1/messages/${encodeURIComponent(messageId)}/moment`, { method: "POST", body: JSON.stringify({ title: "一段想记住的聊天", visibility: "shared" }) });
    await loadBootstrap({ quiet: true }); toast("这段聊天已收藏为恋爱瞬间");
  } catch (error) { toast(error.message); }
}

function renderChatImportDraft(draft) {
  state.chatImport = draft;
  document.getElementById("chatImportForm").hidden = true;
  const host = document.getElementById("chatImportDraft");
  host.hidden = false;
  host.innerHTML = `<p><span class="mock-badge">${escapeHtml(draft.source_label || (draft.mock ? "演示识别" : "真实 OCR"))} · 请逐条确认</span></p>${draft.items.map(item => `<label class="import-row"><select data-import-speaker="${item.id}"><option value="me" ${item.speaker === "me" ? "selected" : ""}>我</option><option value="partner" ${item.speaker === "partner" ? "selected" : ""}>对方</option></select><input data-import-content="${item.id}" value="${escapeHtml(item.content)}" maxlength="4000"><input data-import-selected="${item.id}" type="checkbox" ${item.selected ? "checked" : ""} aria-label="是否导入"></label>`).join("")}<div class="import-actions"><button class="secondary-button" data-action="cancel-import">取消草稿</button><button class="primary-button" data-action="confirm-import">确认并导入</button></div>`;
}

async function loadAccountCenter() {
  const [result, media, trash] = await Promise.all([api("/api/v1/account/devices"), api("/api/v1/media"), api("/api/v1/media/trash")]);
  const user = state.data?.user; const partner = state.data?.partner;
  const identityCard = document.getElementById("accountIdentityCard");
  if (identityCard && user && partner) {
    identityCard.innerHTML = `<span class="avatar" data-avatar-theme="${avatarTheme(user)}" aria-label="当前账号：${escapeHtml(user.nickname)}">${escapeHtml(user.avatar_label)}</span><div><small>当前登录账号</small><strong>${escapeHtml(user.nickname)}（${escapeHtml(user.username)}）</strong><span>伴侣：${escapeHtml(partner.nickname)}（${escapeHtml(partner.username)}）</span></div>`;
  }
  document.getElementById("deviceList").innerHTML = result.items.map(item => `<article class="device-row"><div class="grow"><strong>${escapeHtml(item.device_name)}</strong><small>${item.current ? "当前设备 · " : ""}${new Date(item.last_seen_at).toLocaleString("zh-CN")} · ${escapeHtml(item.ip_address || "未知地址")}</small></div>${!item.current && item.active ? `<button data-action="revoke-device" data-id="${item.id}">撤销</button>` : `<span class="mock-badge">${item.active ? "当前" : "已失效"}</span>`}</article>`).join("");
  const mediaRow = item => `<article class="account-media-row">${["image", "video"].includes(item.kind) ? `<span class="media-preview-wrap"><img src="${escapeHtml(item.thumbnail_url)}" alt="" loading="lazy">${item.kind === "video" ? `<i aria-hidden="true">▶</i>` : ""}</span>` : `<span class="media-kind">♫</span>`}<div class="grow"><strong>${escapeHtml(item.original_name)}</strong><small>${item.visibility === "private" ? "仅自己" : item.visibility === "delayed" ? "延时公开" : "双方可见"} · ${(Number(item.size_bytes) / 1024 / 1024).toFixed(1)} MB${item.metadata_stripped ? " · 隐私信息已清理" : ""}</small></div>${item.owner_id === state.data?.user?.id ? `<button data-action="trash-media" data-id="${item.id}">清理</button>` : ""}</article>`;
  document.getElementById("accountMediaList").innerHTML = media.items.length ? media.items.map(mediaRow).join("") : `<p class="form-note">还没有上传媒体。</p>`;
  document.getElementById("accountMediaTrash").innerHTML = trash.items.length ? trash.items.map(item => `<article class="account-media-row"><div class="grow"><strong>${escapeHtml(item.original_name)}</strong><small>${new Date(item.deleted_at).toLocaleString("zh-CN")}</small></div><button data-action="restore-media" data-id="${item.id}" data-version="${item.version}">恢复</button><button class="danger-text" data-action="purge-media" data-id="${item.id}">永久删除</button></article>`).join("") : `<p class="form-note">回收站为空。</p>`;
  openModal("accountModal");
  refreshPushStatus({ resync: true }).catch(error => renderPushStatus(error.message, "warning"));
}

function lifeAssistantCountLabel(key) {
  return {
    unread_messages: "未读消息",
    unread_notifications: "未读提醒",
    pending_plans: "待确认方案",
    pending_trips: "待确认旅行",
    meal_wishes: "想吃的菜",
    shopping_items: "待采购",
    anniversaries: "纪念日",
    todos: "共同待办"
  }[key] || key;
}

function renderLifeAssistantHome() {
  const assistant = state.lifeAssistant;
  const todayHost = document.getElementById("lifeAssistantToday");
  const priorityHost = document.getElementById("lifeAssistantPriorities");
  const badge = document.getElementById("notificationUnreadBadge");
  if (!todayHost || !priorityHost || !badge) return;
  const counts = assistant.today?.counts || {};
  const visibleCounts = Object.entries(counts).filter(([, value]) => Number(value) > 0);
  todayHost.innerHTML = visibleCounts.length
    ? visibleCounts.map(([key, value]) => '<button type="button" data-action="assistant-count" data-count-key="' + escapeHtml(key) + '"><strong>' + Number(value) + '</strong><span>' + escapeHtml(lifeAssistantCountLabel(key)) + '</span></button>').join("")
    : '<div class="life-assistant-clear"><strong>今天没有遗漏的共同事项</strong><span>新的留言、方案、点菜和待办会自动汇总到这里。</span></div>';
  const priorities = (assistant.today?.priorities || []).slice(0, 3);
  priorityHost.innerHTML = priorities.length
    ? '<h4>优先看看</h4>' + priorities.map(item => '<button type="button" data-action="assistant-priority" data-priority-type="' + escapeHtml(item.type) + '" data-resource-id="' + escapeHtml(item.resource_id) + '" data-url="' + escapeHtml(item.url || "") + '"><span><strong>' + escapeHtml(item.title) + '</strong><small>' + escapeHtml(item.subtitle || "") + '</small></span><b>查看 ›</b></button>').join("")
    : "";
  const unread = Number(assistant.unreadCount || counts.unread_notifications || 0);
  badge.textContent = String(unread);
  badge.hidden = unread < 1;
  const summary = assistant.summary;
  const summaryHost = document.getElementById("dailyCoupleSummary");
  if (summaryHost) {
    summaryHost.hidden = !summary;
    summaryHost.innerHTML = summary
      ? '<span>今晚的双人摘要</span><strong>' + escapeHtml(summary.title) + '</strong><p>' + escapeHtml(summary.body) + '</p>'
      : "";
  }
}

function assistantPlanStatusLabel(status) {
  return {
    generating: "整理中",
    pending_confirmation: "等待双方确认",
    applied: "已生效",
    cancelled: "已取消",
    failed: "生成失败",
    expired: "已过期"
  }[status] || status;
}

function assistantPlanItemHtml(item) {
  const after = item.preview?.after || {};
  const detail = Object.entries(after)
    .map(([key, value]) => '<span><small>' + escapeHtml({ date: "日期", time: "时间", title: "内容", meal: "餐次", recipe: "菜谱", servings: "人数", ingredient_count: "食材项" }[key] || key) + '</small><b>' + escapeHtml(value) + '</b></span>')
    .join("");
  return '<article class="assistant-plan-item"><i aria-hidden="true">✓</i><div><strong>' + escapeHtml(item.preview?.label || item.item_type) + '</strong><div>' + detail + '</div>' + (item.preview?.before ? '<small>' + escapeHtml(item.preview.before) + '</small>' : "") + '</div></article>';
}

function renderAssistantPlan(plan = state.lifeAssistant.activePlan) {
  const host = document.getElementById("assistantPlanContent");
  if (!host) return;
  if (!plan) {
    const plans = state.lifeAssistant.plans || [];
    host.innerHTML = '<h2 id="assistantPlanTitle">共同方案记录</h2>' + (plans.length
      ? '<div class="assistant-plan-list">' + plans.map(item => '<button type="button" data-action="open-assistant-plan" data-id="' + escapeHtml(item.id) + '"><span><strong>' + escapeHtml(item.title) + '</strong><small>' + escapeHtml(assistantPlanStatusLabel(item.status)) + ' · v' + Number(item.version) + '</small></span><b>查看 ›</b></button>').join("") + '</div>'
      : '<div class="empty-view"><span>◇</span><h3>还没有共同方案</h3><p>在首页输入一句话，就能先生成预览给两个人查看。</p></div>');
    return;
  }
  const members = (plan.members || []).map(member => '<span class="' + (member.confirmed ? "confirmed" : "") + '"><i data-avatar-theme="' + escapeHtml(avatarTheme(member)) + '">' + escapeHtml(member.avatar_label || member.nickname?.slice(0, 1) || "♡") + '</i><b>' + escapeHtml(member.nickname) + '</b><small>' + (member.confirmed ? "已确认" : "待确认") + '</small></span>').join("");
  const warning = (plan.warnings || []).length
    ? '<div class="assistant-plan-warning"><strong>确认前请注意</strong>' + plan.warnings.map(item => '<p>' + escapeHtml(item) + '</p>').join("") + '</div>'
    : "";
  const candidates = (plan.candidate_recipes || []).length > 1
    ? '<div class="assistant-plan-candidates"><strong>检测到多个同名菜谱</strong><p>请重新输入更具体的做法名称，再生成方案，避免采购数量对应错误。</p></div>'
    : "";
  const canConfirm = plan.status === "pending_confirmation" && !(plan.warnings || []).some(item => item.includes("多个同名"));
  host.innerHTML = '<div class="assistant-plan-head"><span class="mock-badge">' + escapeHtml(assistantPlanStatusLabel(plan.status)) + ' · v' + Number(plan.version) + '</span><h2 id="assistantPlanTitle">' + escapeHtml(plan.title) + '</h2><p>' + escapeHtml(plan.summary) + '</p></div>'
    + '<p class="assistant-plan-source">识别方式：' + (plan.source === "ai" ? "AI 受控解析" : "本地规则解析") + ' · 菜谱、食材、权限和版本均由恋屿服务端核对</p>'
    + '<div class="assistant-plan-members">' + members + '</div>'
    + warning + candidates
    + '<div class="assistant-plan-items">' + (plan.items || []).map(assistantPlanItemHtml).join("") + '</div>'
    + '<p class="form-note">这份方案两个人现在都能看到；只有当前版本获得双方确认后，才会一次性写入日历、用餐与采购清单。</p>'
    + (plan.status === "pending_confirmation"
      ? '<div class="assistant-plan-actions"><button type="button" class="secondary-button" data-action="cancel-assistant-plan" data-id="' + escapeHtml(plan.id) + '">取消方案</button><button type="button" class="primary-button" data-action="confirm-assistant-plan" data-id="' + escapeHtml(plan.id) + '" ' + (!canConfirm || plan.my_confirmed ? "disabled" : "") + '>' + (plan.my_confirmed ? "我已确认，等待 TA" : "确认当前方案") + '</button></div>'
      : '<div class="assistant-plan-actions"><button type="button" class="secondary-button" data-action="open-assistant-plans">返回方案记录</button></div>');
}

async function loadLifeAssistant({ quiet = false } = {}) {
  try {
    const [today, plans, inbox, summary] = await Promise.all([
      api("/api/v1/assistant/today"),
      api("/api/v1/assistant/plans?limit=30"),
      api("/api/v1/notifications?limit=50"),
      api("/api/v1/assistant/daily-summary")
    ]);
    state.lifeAssistant.today = today;
    state.lifeAssistant.plans = plans.items || [];
    state.lifeAssistant.notifications = inbox.items || [];
    state.lifeAssistant.unreadCount = Number(inbox.unread_count || 0);
    state.lifeAssistant.summary = summary.summary || null;
    renderLifeAssistantHome();
    if (document.getElementById("notificationCenterModal")?.classList.contains("open")) renderNotificationCenter();
    if (!quiet) toast("今天的共同事项已更新");
  } catch (error) {
    if (!quiet) toast(error.message);
  }
}

async function openLifeAssistantPlan(planId) {
  const plan = await api("/api/v1/assistant/plans/" + encodeURIComponent(planId));
  state.lifeAssistant.activePlan = plan;
  renderAssistantPlan(plan);
  openModal("assistantPlanModal");
}

async function openLifeAssistantPlanList() {
  const result = await api("/api/v1/assistant/plans?limit=30");
  state.lifeAssistant.plans = result.items || [];
  state.lifeAssistant.activePlan = null;
  renderAssistantPlan(null);
  openModal("assistantPlanModal");
}

async function confirmLifeAssistantPlan(planId, button) {
  const plan = state.lifeAssistant.activePlan;
  if (!plan || plan.id !== planId) throw new Error("方案已经变化，请重新打开");
  if (button) button.disabled = true;
  try {
    const result = await api("/api/v1/assistant/plans/" + encodeURIComponent(planId) + "/confirm", {
      method: "POST",
      body: JSON.stringify({ expected_version: plan.version, idempotency_key: clientKey("assistant-confirm") })
    });
    state.lifeAssistant.activePlan = result.plan;
    renderAssistantPlan(result.plan);
    await loadLifeAssistant({ quiet: true });
    toast(result.applied ? "双方已确认，方案已经写入共同生活" : "你已确认，方案仍可供双方查看");
  } finally {
    if (button && document.body.contains(button)) button.disabled = false;
  }
}

function notificationDateGroup(createdAt) {
  const current = new Date();
  const value = new Date(createdAt);
  if (!Number.isFinite(value.getTime())) return "更早";
  const today = new Date(current.getFullYear(), current.getMonth(), current.getDate());
  const itemDay = new Date(value.getFullYear(), value.getMonth(), value.getDate());
  const days = Math.round((today.getTime() - itemDay.getTime()) / 86400000);
  if (days <= 0) return "今天";
  if (days === 1) return "昨天";
  return "更早";
}

function renderNotificationCenter() {
  const host = document.getElementById("notificationCenterList");
  if (!host) return;
  const items = state.lifeAssistant.notifications || [];
  const grouped = items.reduce((result, item) => {
    const label = notificationDateGroup(item.created_at);
    const group = result.find(value => value.label === label);
    if (group) group.items.push(item);
    else result.push({ label, items: [item] });
    return result;
  }, []);
  host.innerHTML = items.length
    ? grouped.map(group => '<section class="notification-center-group"><h3>' + group.label + '</h3>' + group.items.map(item => '<article class="notification-center-item ' + (item.read ? "" : "unread") + '"><button type="button" class="notification-center-open" data-action="open-notification" data-id="' + escapeHtml(item.id) + '"><span></span><div><strong>' + escapeHtml(item.title) + '</strong><p>' + escapeHtml(item.body || "") + '</p><small>' + new Date(item.created_at).toLocaleString("zh-CN") + '</small></div></button><button type="button" class="notification-center-archive" data-action="archive-notification" data-id="' + escapeHtml(item.id) + '" aria-label="归档通知">×</button></article>').join("") + '</section>').join("")
    : '<div class="empty-view"><span>◇</span><h3>没有新通知</h3><p>评论、邀请和共同方案变化都会保存在这里。</p></div>';
}

async function openInAppNotification(notificationId) {
  const item = state.lifeAssistant.notifications.find(value => value.id === notificationId);
  if (!item) throw new Error("通知已经归档或不存在");
  if (!item.read) await api("/api/v1/notifications/" + encodeURIComponent(item.id) + "/read", { method: "POST", body: "{}" });
  await loadLifeAssistant({ quiet: true });
  if (item.resource_type === "assistant_plan") {
    closeModals();
    await openLifeAssistantPlan(item.resource_id);
    return;
  }
  if (item.resource_type === "message") {
    closeModals();
    navigate("chat");
    return;
  }
  if (item.resource_type === "daily_summary") {
    closeModals();
    navigate("home");
    document.getElementById("dailyCoupleSummary")?.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  const target = new URL(item.url, location.origin);
  const screen = target.searchParams.get("screen");
  const sharedType = target.searchParams.get("shared_type");
  const sharedId = target.searchParams.get("shared_id");
  if (sharedType && sharedId) {
    closeModals();
    if (screen) navigate(screen);
    await openSharedDetail(sharedType, sharedId);
    return;
  }
  if (screen) {
    closeModals();
    navigate(screen);
  }
}

async function loadBootstrap({ quiet = false } = {}) {
  try {
    const previousUserId = state.data?.user?.id || state.tripLive.ownerUserId;
    const bootstrap = await api("/api/v1/bootstrap");
    if (previousUserId && bootstrap.user?.id !== previousUserId) resetTripLive();
    state.data = bootstrap;
    if (state.data.needs_pairing) { showPairing(); return; }
    const preferredTripId = localStorage.getItem("love-trip-id");
    const preferredTrip = (state.data.trips || []).find(item => item.id === preferredTripId);
    if (preferredTrip) state.data.trip = preferredTrip;
    const availableStyles = (state.data.trip?.variants || []).map(variant => variant.style);
    if (!availableStyles.includes(state.plan)) state.plan = state.data.trip?.active_variant || availableStyles[0] || "slow";
    state.online = true;
    localStorage.setItem("love-offline-trip", JSON.stringify({ user: state.data.user, partner: state.data.partner, couple: state.data.couple, trip: state.data.trip, coupleContext: state.data.coupleContext, weeklyReview: state.data.weeklyReview, datePlans: state.data.datePlans }));
    hideAuth();
    renderIdentity(); renderMessages(); renderPlan(); renderHotel(); renderWishlist(); renderDailyQuestion(); renderTimeline(); renderCoupleContext(); renderWeeklyReview(); renderDatePlans(); renderLifeData();
    await Promise.all([loadMeals({ quiet: true }), loadLifeAssistant({ quiet: true })]);
    // Reapply the user's active sub-page after any background/SSE refresh. This
    // keeps the visible panel and its primary action in sync while the DOM is
    // being updated by real-time events from the partner account.
    setTripSection(state.tripSection, { scroll: false });
    setLifeSection(state.lifeSection, { scroll: false });
    if (state.screen === "timeline") await setTimelineView(state.timelineView, { scroll: false });
    api("/api/v1/couple/context").then(context => { if (state.data) { state.data.coupleContext = context; renderCoupleContext(); } }).catch(() => {});
    api("/api/v1/media/usage").then(usage => {
      const megabytes = (Number(usage.owner_bytes || usage.total_bytes || 0) / 1024 / 1024).toFixed(1);
      const quota = (Number(usage.owner_quota_bytes || 0) / 1024 / 1024 / 1024).toFixed(1);
      const label = document.querySelector(".privacy-entry small");
      if (label) label.textContent = `私密内容、延时公开 · 我的媒体 ${megabytes} MB / ${quota} GB${usage.owner_warning ? " · 即将满额" : ""}`;
    }).catch(() => {});
    startRealtime().catch(() => {});
    syncActiveCall();
    refreshPushStatus({ resync: true }).catch(error => renderPushStatus(error.message, "warning"));
    await loadSmartSurface(state.screen, { quiet: true });
    await openPushDeepLink();
    clearAppNotificationBadge();
    resumePendingTripGeneration();
    const smartTarget = new URLSearchParams(location.search).get("smart");
    if (!state.smartDeepLinked && smartTarget) {
      state.smartDeepLinked = true;
      await openSmartNode(smartTarget);
    }
    if (!state.accountAutoOpened && new URLSearchParams(location.search).get("account") === "1") {
      state.accountAutoOpened = true;
      await loadAccountCenter();
    }
    const assistantPlanTarget = new URLSearchParams(location.search).get("assistant-plan");
    if (!state.assistantDeepLinked && assistantPlanTarget) {
      state.assistantDeepLinked = true;
      await openLifeAssistantPlan(assistantPlanTarget);
    }
    if (!quiet) toast("共同空间已同步");
  } catch (error) {
    state.online = false;
    if (error.status !== 401) {
      loadOfflineSnapshot();
      toast(`暂时无法同步 · ${error.message}`);
    }
  }
}

function loadOfflineSnapshot() {
  const cached = readLocalJson("love-offline-trip");
  if (!cached?.user || !cached?.partner || !cached?.couple) return false;
  state.data = { ...cached, messages: [], moments: [], wishlist: { id: null, title: "离线状态", items: [] }, dailyQuestion: null };
  hideAuth(); renderIdentity(); renderMessages(); renderPlan(); renderHotel(); renderWishlist(); renderTimeline(); renderCoupleContext(); renderWeeklyReview(); renderDatePlans(); renderLifeData();
  const cachedMeals = readLocalJson(`love-offline-meals:${state.data.user.id}`); if (cachedMeals) { state.meals = { ...state.meals, ...cachedMeals }; renderMeals(); }
  setTripSection(state.tripSection, { scroll: false });
  setLifeSection(state.lifeSection, { scroll: false });
  loadSmartSurface(state.screen, { quiet: true });
  return true;
}

async function startRealtime() {
  if (!state.data || state.realtime) return;
  const snapshot = await api(`/api/v1/realtime/events?after=${state.realtimeCursor}`);
  state.realtimeCursor = Math.max(state.realtimeCursor, Number(snapshot.latest_sequence || 0));
  const source = new EventSource(`/api/v1/realtime/stream?after=${state.realtimeCursor}`, { withCredentials: true });
  state.realtime = source;
  const eventNames = ["message.created", "message.read", "presence.changed", "life.updated", "meals.updated", "trip.assistant_updated", "shared_comment.created", "shared_comment.deleted", "date_vote.updated", "date_plan.created", "date_plan.generated", "date_plan.composed", "date_plan.confirmed", "date_plan.completed", "weekly_review.created", "weekly_review.answered", "weekly_review.confirmed", "moment.created", "moment.trashed", "moment.restored", "media.created", "trip.created", "calendar.created", "calendar.updated", "calendar.deleted", "preference.created", "preference.updated", "preference.deleted", "ui.invalidated", "notification.created", "assistant.plan_updated", "assistant.daily_summary_created", "call.invited", "call.answered", "call.signal", "call.rejected", "call.cancelled", "call.ended", "call.missed"];
  const receive = event => {
    let realtimeEvent = null;
    try { realtimeEvent = JSON.parse(event.data); state.realtimeCursor = Math.max(state.realtimeCursor, Number(realtimeEvent.sequence || event.lastEventId || 0)); } catch (_) {}
    if (document.hidden && realtimeEvent?.actor_id !== state.data?.user?.id && "Notification" in window && Notification.permission === "granted") {
      const labels = { "message.created": "TA 发来了一条新消息", "presence.changed": "TA 更新了此刻状态", "date_vote.updated": "约会计划有了新投票", "weekly_review.answered": "TA 写好了本周回顾", "call.invited": "TA 正在呼叫你" };
      if (labels[realtimeEvent?.event_type] && realtimeEvent.event_type !== "call.invited") new Notification("恋屿", { body: labels[realtimeEvent.event_type], icon: "/static/assets/love-moments-icon-192.png", tag: realtimeEvent.event_type });
    }
    if (realtimeEvent?.event_type?.startsWith("call.")) {
      handleCallRealtime(realtimeEvent).catch(error => toast(error.message));
      return;
    }
    if (realtimeEvent?.event_type?.startsWith("shared_comment.") && state.sharedDetail
        && realtimeEvent.resource_id === state.sharedDetail.resource_id
        && document.getElementById("sharedDetailModal").classList.contains("open")) {
      openSharedDetail(state.sharedDetail.resource_type, state.sharedDetail.resource_id, { refresh: true }).catch(() => {});
    }
    clearTimeout(state.realtimeRefresh);
    state.realtimeRefresh = setTimeout(() => loadBootstrap({ quiet: true }), 180);
  };
  eventNames.forEach(name => source.addEventListener(name, receive));
  source.addEventListener("open", () => { state.online = true; document.querySelector(".presence").textContent = "● 实时连接 · 只属于你们的对话"; });
  source.addEventListener("error", () => { state.online = false; document.querySelector(".presence").textContent = "○ 正在重连 · 内容已保留"; });
}

function stopRealtime() {
  state.realtime?.close();
  state.realtime = null;
  clearTimeout(state.realtimeRefresh);
}

function showAccountAuth(mode = "login") {
  if (mode === "register" && !state.registrationEnabled) mode = "login";
  state.authMode = mode;
  const gate = document.getElementById("authGate");
  gate.classList.remove("hidden");
  document.getElementById("authAccountView").hidden = false;
  document.getElementById("pairingView").hidden = true;
  document.querySelector(".auth-panel").classList.toggle("register-mode", mode === "register");
  document.querySelectorAll("[data-auth-mode]").forEach(btn => btn.classList.toggle("active", btn.dataset.authMode === mode));
  document.querySelectorAll('[data-auth-mode="register"]').forEach(btn => { btn.hidden = !state.registrationEnabled; });
  const registrationNote = document.getElementById("registrationNote");
  if (registrationNote) registrationNote.textContent = state.registrationEnabled
    ? "请使用你们各自注册的账号登录。注册时填写的昵称，会成为你在情侣空间里的名字和头像文字。"
    : "这座恋屿已经关闭公开注册，请使用现有的两个账号登录。需要更换账号时，由管理员临时开启注册。";
  document.getElementById("authSubmit").textContent = mode === "register" ? "创建恋屿账号" : "登录";
  document.getElementById("authPassword").autocomplete = mode === "register" ? "new-password" : "current-password";
}

function showPairing() {
  const gate = document.getElementById("authGate");
  gate.classList.remove("hidden");
  document.getElementById("authAccountView").hidden = true;
  document.getElementById("pairingView").hidden = false;
}

function hideAuth() {
  document.getElementById("authGate").classList.add("hidden");
}

async function initializeAuth() {
  try {
    const result = await api("/api/v1/auth/me");
    state.registrationEnabled = result.registration_enabled !== false;
    if (!result.authenticated) { showAccountAuth(); return; }
    if (!result.pairing?.paired) { showPairing(); return; }
    await loadBootstrap({ quiet: true });
  } catch (error) {
    const restored = loadOfflineSnapshot();
    if (!restored) showAccountAuth();
    toast(restored ? "当前显示最近保存的离旅行程" : error.message);
  }
}

function toast(message, action = null) {
  const el = document.getElementById("toast");
  const activeAction = el.querySelector("button[data-action]");
  // Keep a recoverable action (for example, "撤销删除") available for its
  // full window. Background refreshes can emit informational toasts at any
  // moment; replacing the button would make the recovery path disappear just
  // as the user tries to tap it. Once the button is tapped it is disabled, so
  // the restore result or error can replace this toast normally.
  if (!action && activeAction && !activeAction.disabled && el.classList.contains("show")) {
    el.dataset.pendingMessage = String(message);
    return;
  }
  delete el.dataset.pendingMessage;
  el.textContent = message;
  if (action?.label && action?.action) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = action.label;
    button.dataset.action = action.action;
    Object.entries(action.data || {}).forEach(([key, value]) => { button.dataset[key] = String(value); });
    el.appendChild(button);
  }
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    el.classList.remove("show");
    const pendingMessage = el.dataset.pendingMessage;
    delete el.dataset.pendingMessage;
    if (pendingMessage) setTimeout(() => toast(pendingMessage), 0);
  }, action ? 8000 : 2200);
}

function openModal(id) {
  const modal = document.getElementById(id);
  state.modalTrigger = document.activeElement;
  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
  document.querySelector(".phone-shell").setAttribute("inert", "");
  document.querySelector(".bottom-nav")?.setAttribute("inert", "");
  (modal.querySelector("[autofocus], .field input, .field textarea") || modal.querySelector("button"))?.focus();
}

function closeModals() {
  document.querySelectorAll(".modal.open").forEach(modal => { modal.classList.remove("open"); modal.setAttribute("aria-hidden", "true"); });
  document.querySelector(".phone-shell").removeAttribute("inert");
  document.querySelector(".bottom-nav")?.removeAttribute("inert");
  state.modalTrigger?.focus?.();
  state.modalTrigger = null;
}

function renderSharedDetail(detail) {
  state.sharedDetail = detail;
  document.getElementById("sharedCommentForm").hidden = false;
  document.getElementById("sharedDetailKind").textContent = detail.kind_label || "共同内容";
  const fields = (detail.fields || []).map(item => `<div><span>${escapeHtml(item.label)}</span><strong>${escapeHtml(item.value)}</strong></div>`).join("");
  const media = detail.media_url ? `<img class="shared-detail-media" src="${escapeHtml(detail.media_url)}" alt="${escapeHtml(detail.title)}">` : "";
  const actions = detail.recipe_id ? `<div class="shared-detail-actions"><button data-action="open-shared-recipe" data-id="${escapeHtml(detail.recipe_id)}">查看完整菜谱与做法</button></div>` : "";
  document.getElementById("sharedDetailContent").innerHTML = `${media}<h2 id="sharedDetailTitle">${escapeHtml(detail.title)}</h2>${detail.summary ? `<p class="shared-detail-summary">${escapeHtml(detail.summary)}</p>` : `<p class="shared-detail-summary">双方可以在下方继续补充想法。</p>`}${fields ? `<div class="shared-detail-fields">${fields}</div>` : ""}${actions}`;
  document.getElementById("sharedCommentCount").textContent = `${Number(detail.comment_count || 0)} 条`;
  const comments = detail.comments || [];
  document.getElementById("sharedCommentList").innerHTML = comments.length ? comments.map(comment => `<article class="shared-comment"><span class="shared-comment-avatar">${escapeHtml(comment.avatar_label || comment.author_name?.slice(0, 1) || "♡")}</span><div><strong>${escapeHtml(comment.author_name)}</strong><small>${new Date(comment.created_at).toLocaleString("zh-CN")}</small><p>${escapeHtml(comment.body)}</p></div>${comment.mine ? `<button data-action="delete-shared-comment" data-id="${escapeHtml(comment.id)}" data-version="${comment.version}" aria-label="删除我的评论">删除</button>` : ""}</article>`).join("") : `<div class="shared-comment-empty">还没有评论。写下第一句话后，对方登录即可看到。</div>`;
}

async function openSharedDetail(resourceType, resourceId, { refresh = false } = {}) {
  if (!resourceType || !resourceId) return;
  if (!refresh) {
    closeModals();
    document.getElementById("sharedDetailKind").textContent = "正在打开共同内容";
    document.getElementById("sharedDetailContent").innerHTML = `<div class="loading-state">正在读取详情…</div>`;
    document.getElementById("sharedCommentList").innerHTML = "";
    document.getElementById("sharedCommentCount").textContent = "";
    document.getElementById("sharedCommentBody").value = "";
    openModal("sharedDetailModal");
  }
  try {
    const detail = await api(`/api/v1/shared-content/${encodeURIComponent(resourceType)}/${encodeURIComponent(resourceId)}`);
    renderSharedDetail(detail);
  } catch (error) {
    if (refresh) return;
    document.getElementById("sharedDetailContent").innerHTML = `<div class="empty-view"><h3>无法打开这条内容</h3><p>${escapeHtml(error.message)}</p></div>`;
    document.getElementById("sharedCommentForm").hidden = true;
  }
}

async function openPushDeepLink() {
  if (state.pushDeepLinked) return;
  const params = new URLSearchParams(location.search);
  const resourceType = params.get("shared_type") || "";
  const resourceId = params.get("shared_id") || "";
  if (!resourceType && !resourceId) return;
  state.pushDeepLinked = true;
  params.delete("shared_type");
  params.delete("shared_id");
  const remainingQuery = params.toString();
  history.replaceState(history.state, "", `${location.pathname}${remainingQuery ? `?${remainingQuery}` : ""}${location.hash}`);
  const surfaces = {
    moment: "timeline", trip: "trip", itinerary_item: "trip",
    meal_wish: "life", meal_item: "life", wishlist_item: "life",
    calendar_entry: "life", preference: "life", date_plan: "life",
  };
  const surface = surfaces[resourceType];
  if (!surface || !/^[A-Za-z0-9_-]{3,160}$/.test(resourceId)) {
    toast("这条通知链接无效，请从页面中查看共同内容");
    return;
  }
  navigate(surface);
  await openSharedDetail(resourceType, resourceId);
}

function clearAppNotificationBadge() {
  if (typeof navigator.clearAppBadge === "function") {
    navigator.clearAppBadge().catch(() => {});
    return;
  }
  state.serviceWorkerRegistration?.clearAppBadge?.().catch(() => {});
}

async function sendMessage(text) {
  const message = await api("/api/v1/messages", { method: "POST", body: JSON.stringify({ type: "text", content: text, idempotency_key: clientKey("message") }) });
  state.data.messages.push(message);
  renderMessages();
  document.getElementById("messages").lastElementChild?.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function sendMediaMessage(file) {
  const kind = file.type.startsWith("image/") ? "image" : file.type.startsWith("audio/") ? "voice" : file.type.startsWith("video/") ? "video" : null;
  if (!kind) throw new Error("请选择图片、音频或短视频");
  toast("正在保存媒体到这台电脑…");
  const media = await uploadMedia(file, "shared");
  const label = kind === "image" ? "分享了一张照片" : kind === "voice" ? "分享了一段语音" : "分享了一段短视频";
  const message = await api("/api/v1/messages", { method: "POST", body: JSON.stringify({ type: kind, content: label, metadata: { mediaId: media.id, originalName: media.original_name }, idempotency_key: clientKey("media-message") }) });
  state.data.messages.push(message);
  renderMessages();
  toast("媒体已保存并发送");
}

function callTimeText(seconds) {
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function startCallTimer(startedAt = Date.now()) {
  clearInterval(state.callTimer);
  state.callStartedAt = startedAt;
  const refresh = () => { document.getElementById("callTimer").textContent = callTimeText(Math.max(0, Math.floor((Date.now() - state.callStartedAt) / 1000))); };
  refresh();
  state.callTimer = setInterval(refresh, 1000);
}

function showCallScreen(record, incoming = false) {
  const peer = record?.peer || state.data?.partner || {};
  state.callId = record?.id || state.callId;
  state.callRecord = record || state.callRecord;
  if (state.callId) startCallHeartbeat();
  document.getElementById("callTitle").textContent = peer.nickname || "伴侣";
  document.querySelector(".call-avatar").textContent = peer.avatar_label || "♡";
  document.getElementById("incomingCallControls").hidden = !incoming;
  document.getElementById("activeCallControls").hidden = incoming;
  document.getElementById("callTimer").textContent = "00:00";
  document.getElementById("callStatus").textContent = incoming ? "邀请你进行语音通话" : "正在请求麦克风权限…";
  const call = document.getElementById("callScreen");
  call.classList.add("open"); call.setAttribute("aria-hidden", "false");
  document.querySelector(".phone-shell").setAttribute("inert", "");
  call.querySelector(incoming ? "[data-action='accept-call']" : "[data-action='end-call']")?.focus();
}

function closeCallMedia() {
  clearInterval(state.callTimer);
  clearInterval(state.callHeartbeat);
  state.callTimer = null;
  state.callHeartbeat = null;
  state.callPeer?.close();
  const agoraClient = state.agoraClient;
  const agoraLocalTrack = state.agoraLocalTrack;
  const agoraRemoteTrack = state.agoraRemoteTrack;
  state.agoraClient = null;
  state.agoraLocalTrack = null;
  state.agoraRemoteTrack = null;
  try { agoraRemoteTrack?.stop?.(); } catch (_) {}
  try { agoraLocalTrack?.close?.(); } catch (_) {}
  if (agoraClient) agoraClient.leave().catch(() => {});
  state.callLocalStream?.getTracks().forEach(track => track.stop());
  state.callTestAudioCleanup?.();
  const remoteAudio = document.getElementById("remoteCallAudio");
  remoteAudio.pause(); remoteAudio.srcObject = null;
  delete remoteAudio.dataset.remoteTrack;
  delete remoteAudio.dataset.receivedBytes;
  state.callPeer = null;
  state.callLocalStream = null;
  state.callSignalCursor = 0;
  state.callPendingIce = [];
  state.callProcessingSignals = false;
  state.callTransport = null;
  state.callTestAudioCleanup = null;
  state.callProvider = null;
  state.callMuted = false;
}

function startCallHeartbeat() {
  clearInterval(state.callHeartbeat);
  if (!state.callId) return;
  const pulse = async () => {
    const callId = state.callId;
    if (!callId) return;
    try {
      const record = await api(`/api/v1/calls/${callId}/heartbeat`, { method: "POST", body: "{}" });
      if (state.callId === callId) state.callRecord = record;
    } catch (error) {
      if (error.status === 400 || error.status === 404) closeCallScreen("通话已结束");
    }
  };
  pulse();
  state.callHeartbeat = setInterval(pulse, 15000);
}

function closeCallScreen(message = "") {
  closeCallMedia();
  document.getElementById("callScreen").classList.remove("open");
  document.getElementById("callScreen").setAttribute("aria-hidden", "true");
  document.querySelector(".phone-shell").removeAttribute("inert");
  state.callId = null;
  state.callRecord = null;
  state.callStartedAt = null;
  if (message) toast(message);
}

function requireWebRtc() {
  if (!window.isSecureContext) throw new Error("语音通话需要 HTTPS；本机测试可使用 localhost 或 127.0.0.1");
  if (!navigator.mediaDevices?.getUserMedia || typeof RTCPeerConnection !== "function") throw new Error("当前浏览器不支持 WebRTC 语音，请使用最新版 Chrome、Edge 或 Safari");
}

async function requestMicrophone() {
  requireWebRtc();
  const localDiagnostic = ["localhost", "127.0.0.1", "::1"].includes(location.hostname)
    && new URLSearchParams(location.search).get("webrtcTestTone") === "1";
  if (localDiagnostic) {
    // Chromium's fake media device produces an active audio source that the
    // Shengwang SDK advertises correctly. Prefer it in release tests; retain
    // the WebAudio oscillator only as a browser fallback.
    try {
      return await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    } catch (_) {}
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) throw new Error("当前浏览器无法创建通话测试音频");
    const context = new AudioContextClass();
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    const destination = context.createMediaStreamDestination();
    oscillator.frequency.value = 440;
    gain.gain.value = 0.015;
    oscillator.connect(gain).connect(destination);
    oscillator.start();
    state.callTestAudioCleanup = () => {
      try { oscillator.stop(); } catch (_) {}
      context.close().catch(() => {});
    };
    return destination.stream;
  }
  try {
    return await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
  } catch (error) {
    if (error?.name === "NotAllowedError") throw new Error("麦克风权限被拒绝，请在浏览器设置中允许后重试");
    if (error?.name === "NotFoundError") throw new Error("没有找到可用的麦克风");
    throw new Error("无法打开麦克风，请检查设备和浏览器权限");
  }
}

async function describeCallTransport() {
  if (!state.callPeer) return "WebRTC";
  try {
    const reports = await state.callPeer.getStats();
    let pair = null;
    reports.forEach(report => {
      if (report.type === "transport" && report.selectedCandidatePairId) pair = reports.get(report.selectedCandidatePairId);
      if (!pair && report.type === "candidate-pair" && report.state === "succeeded" && (report.selected || report.nominated)) pair = report;
    });
    if (!pair) return "WebRTC";
    const local = reports.get(pair.localCandidateId);
    const remote = reports.get(pair.remoteCandidateId);
    state.callTransport = local?.candidateType === "relay" || remote?.candidateType === "relay" ? "TURN 中继" : "点对点直连";
    return state.callTransport;
  } catch (_) { return "WebRTC"; }
}

async function updateCallAudioDiagnostics() {
  if (!state.callPeer) return;
  try {
    const reports = await state.callPeer.getStats();
    let bytes = 0;
    reports.forEach(report => {
      if (report.type === "inbound-rtp" && report.kind === "audio") bytes += Number(report.bytesReceived || 0);
    });
    document.getElementById("remoteCallAudio").dataset.receivedBytes = String(bytes);
  } catch (_) {}
}

async function sendCallSignal(type, payload) {
  if (!state.callId) return;
  await api(`/api/v1/calls/${state.callId}/signal`, { method: "POST", body: JSON.stringify({ type, payload }) });
}

async function rtcProviderConfig() {
  try {
    return await api("/api/v1/rtc/config");
  } catch (_) {
    return { provider: "webrtc", shengwangAvailable: false };
  }
}

async function issueShengwangToken() {
  if (!state.callId) throw new Error("通话尚未创建");
  return api(`/api/v1/calls/${state.callId}/rtc-token`, { method: "POST", body: "{}" });
}

function updateShengwangAudioDiagnostics() {
  if (!state.agoraRemoteTrack) return;
  try {
    const stats = state.agoraRemoteTrack.getStats?.() || {};
    const bytes = Number(stats.receiveBytes || stats.bytesReceived || 0);
    document.getElementById("remoteCallAudio").dataset.receivedBytes = String(bytes);
  } catch (_) {}
}

async function renewShengwangToken() {
  if (!state.agoraClient || !state.callId) return;
  try {
    const credentials = await issueShengwangToken();
    await state.agoraClient.renewToken(credentials.token);
  } catch (error) {
    document.getElementById("callStatus").textContent = "通话凭证续期失败 · 请重新拨打";
  }
}

async function createShengwangCall() {
  if (!window.AgoraRTC) throw new Error("声网语音组件加载失败，请刷新页面后重试");
  const credentials = await issueShengwangToken();
  const client = window.AgoraRTC.createClient({ mode: "rtc", codec: "vp8" });
  state.callProvider = "shengwang";
  state.agoraClient = client;

  client.on("user-published", async (user, mediaType) => {
    try {
      await client.subscribe(user, mediaType);
      if (mediaType !== "audio" || !user.audioTrack) return;
      state.agoraRemoteTrack?.stop?.();
      state.agoraRemoteTrack = user.audioTrack;
      const diagnostic = document.getElementById("remoteCallAudio");
      diagnostic.dataset.remoteTrack = "playing";
      user.audioTrack.play();
      document.getElementById("callStatus").textContent = "通话中 · 声网 RTC";
      setTimeout(updateShengwangAudioDiagnostics, 1200);
      setTimeout(updateShengwangAudioDiagnostics, 3000);
    } catch (_) {
      document.getElementById("callStatus").textContent = "已连接 · 点一下页面即可播放对方声音";
    }
  });
  client.on("user-unpublished", user => {
    if (state.agoraRemoteTrack && user.audioTrack === state.agoraRemoteTrack) {
      state.agoraRemoteTrack.stop?.();
      state.agoraRemoteTrack = null;
      document.getElementById("callStatus").textContent = "对方音频已暂停";
    }
  });
  client.on("user-left", () => {
    document.getElementById("callStatus").textContent = "对方已离开 · 等待通话记录同步";
  });
  client.on("connection-state-change", current => {
    if (current === "RECONNECTING") document.getElementById("callStatus").textContent = "网络波动 · 声网正在重连";
    if (current === "DISCONNECTED" && state.callId) document.getElementById("callStatus").textContent = "音频连接已断开";
  });
  client.on("token-privilege-will-expire", renewShengwangToken);
  client.on("token-privilege-did-expire", renewShengwangToken);

  const mediaTrack = state.callLocalStream?.getAudioTracks?.()[0];
  if (!mediaTrack) throw new Error("麦克风尚未准备好");
  const localTrack = window.AgoraRTC.createCustomAudioTrack({ mediaStreamTrack: mediaTrack });
  state.agoraLocalTrack = localTrack;
  document.getElementById("callStatus").textContent = "正在连接声网安全音频…";
  await client.join(credentials.appId, credentials.channel, credentials.token, credentials.uid);
  await client.publish([localTrack]);
  document.getElementById("callStatus").textContent = state.callRecord?.state === "connected"
    ? "已接通 · 等待对方音频…"
    : "正在呼叫，等待 TA 接听…";
}

async function connectCallMedia(provider) {
  if (provider === "shengwang") {
    await createShengwangCall();
    return;
  }
  state.callProvider = "webrtc";
  const peer = await createCallPeer();
  if (state.callRecord?.is_caller) {
    const offer = await peer.createOffer({ offerToReceiveAudio: true });
    await peer.setLocalDescription(offer);
    await sendCallSignal("offer", { type: offer.type, sdp: offer.sdp });
  }
}

async function createCallPeer() {
  const config = await api("/api/v1/webrtc/config");
  const peer = new RTCPeerConnection({
    iceServers: config.iceServers || [],
    iceTransportPolicy: config.iceTransportPolicy === "relay" ? "relay" : "all"
  });
  state.callPeer = peer;
  state.callLocalStream.getTracks().forEach(track => peer.addTrack(track, state.callLocalStream));
  peer.onicecandidate = event => {
    if (event.candidate && state.callId) sendCallSignal("ice", event.candidate.toJSON ? event.candidate.toJSON() : event.candidate).catch(error => toast(error.message));
  };
  peer.ontrack = event => {
    const audio = document.getElementById("remoteCallAudio");
    audio.srcObject = event.streams[0] || new MediaStream([event.track]);
    audio.dataset.remoteTrack = event.track?.readyState || "received";
    audio.play().catch(() => { document.getElementById("callStatus").textContent = "已连接 · 点一下页面即可播放对方声音"; });
  };
  peer.onconnectionstatechange = async () => {
    if (peer.connectionState === "connected") {
      const transport = await describeCallTransport();
      document.getElementById("callStatus").textContent = `通话中 · ${transport}`;
      if (!state.callStartedAt) startCallTimer();
      setTimeout(updateCallAudioDiagnostics, 1200);
      setTimeout(updateCallAudioDiagnostics, 2500);
    } else if (peer.connectionState === "failed") {
      document.getElementById("callStatus").textContent = config.turnAvailable ? "连接失败，正在等待重试" : "直连失败 · 请配置 TURN 后重试";
    } else if (peer.connectionState === "disconnected") {
      document.getElementById("callStatus").textContent = "网络波动 · 正在重新连接";
    }
  };
  return peer;
}

async function flushPendingIce() {
  if (!state.callPeer?.remoteDescription) return;
  const pending = state.callPendingIce.splice(0);
  for (const candidate of pending) await state.callPeer.addIceCandidate(candidate).catch(() => {});
}

async function processCallSignals() {
  if (!state.callId || !state.callPeer || state.callProcessingSignals) return;
  state.callProcessingSignals = true;
  try {
    const result = await api(`/api/v1/calls/${state.callId}/signals?after=${state.callSignalCursor}`);
    for (const signal of result.items || []) {
      state.callSignalCursor = Math.max(state.callSignalCursor, Number(signal.sequence || 0));
      if (signal.sender_id === state.data?.user?.id) continue;
      if (signal.type === "offer" && !state.callRecord?.is_caller) {
        if (!state.callPeer.remoteDescription) {
          await state.callPeer.setRemoteDescription(signal.payload);
          await flushPendingIce();
          const answer = await state.callPeer.createAnswer();
          await state.callPeer.setLocalDescription(answer);
          await sendCallSignal("answer", { type: answer.type, sdp: answer.sdp });
        }
      } else if (signal.type === "answer" && state.callRecord?.is_caller) {
        if (!state.callPeer.remoteDescription) {
          await state.callPeer.setRemoteDescription(signal.payload);
          await flushPendingIce();
        }
      } else if (signal.type === "ice") {
        if (state.callPeer.remoteDescription) await state.callPeer.addIceCandidate(signal.payload).catch(() => {});
        else state.callPendingIce.push(signal.payload);
      }
    }
    state.callSignalCursor = Math.max(state.callSignalCursor, Number(result.latest_sequence || 0));
  } finally { state.callProcessingSignals = false; }
}

async function startCall() {
  if (state.callId) return;
  const attempt = ++state.callAttempt;
  showCallScreen({ peer: state.data?.partner }, false);
  try {
    const stream = await requestMicrophone();
    if (attempt !== state.callAttempt) {
      stream.getTracks().forEach(track => track.stop());
      return;
    }
    state.callLocalStream = stream;
    const record = await api("/api/v1/calls", { method: "POST", body: "{}" });
    state.callId = record.id; state.callRecord = record;
    startCallHeartbeat();
    const rtc = await rtcProviderConfig();
    await connectCallMedia(rtc.provider);
  } catch (error) {
    if (attempt !== state.callAttempt) return;
    if (state.callId) await api(`/api/v1/calls/${state.callId}/cancel`, { method: "POST", body: "{}" }).catch(() => {});
    closeCallScreen(error.message);
  }
}

async function showIncomingCall(record) {
  if (!record || (state.callId && state.callId !== record.id)) return;
  showCallScreen(record, true);
  if (document.hidden && !state.pushSubscribed && "Notification" in window && Notification.permission === "granted") {
    new Notification("恋屿语音来电", { body: "对方正在呼叫你，点击进入恋屿接听。", icon: "/static/assets/love-moments-icon-192.png", tag: `call:${record.id}` });
  }
}

async function acceptIncomingCall() {
  if (!state.callId || state.callRecord?.is_caller) return;
  document.getElementById("incomingCallControls").hidden = true;
  document.getElementById("activeCallControls").hidden = false;
  document.getElementById("callStatus").textContent = "正在打开麦克风…";
  try {
    state.callLocalStream = await requestMicrophone();
    state.callRecord = await api(`/api/v1/calls/${state.callId}/answer`, { method: "POST", body: "{}" });
    startCallTimer(new Date(state.callRecord.answered_at || Date.now()).getTime());
    document.getElementById("callStatus").textContent = "正在建立加密音频连接…";
    const rtc = await rtcProviderConfig();
    await connectCallMedia(rtc.provider);
    if (state.callProvider === "webrtc") await processCallSignals();
  } catch (error) {
    const endpoint = state.callRecord?.state === "connected" ? "end" : "decline";
    const payload = endpoint === "end"
      ? JSON.stringify({ duration_seconds: 0, note: "音频连接失败" })
      : "{}";
    await api(`/api/v1/calls/${state.callId}/${endpoint}`, { method: "POST", body: payload }).catch(() => {});
    closeCallScreen(error.message);
  }
}

async function declineIncomingCall() {
  if (!state.callId) return;
  await api(`/api/v1/calls/${state.callId}/decline`, { method: "POST", body: "{}" }).catch(() => {});
  closeCallScreen("已拒绝语音通话");
}

async function handleCallRealtime(event) {
  if (!event || (event.payload?.toUserId && event.payload.toUserId !== state.data?.user?.id)) return;
  if (event.event_type === "call.invited" && event.actor_id !== state.data?.user?.id) {
    const active = await api("/api/v1/calls/active");
    if (active.call?.id === event.resource_id) await showIncomingCall(active.call);
    return;
  }
  if (event.resource_id !== state.callId) return;
  if (event.event_type === "call.answered") {
    const active = await api("/api/v1/calls/active");
    if (active.call) state.callRecord = active.call;
    startCallTimer(new Date(state.callRecord?.answered_at || Date.now()).getTime());
    document.getElementById("callStatus").textContent = state.callProvider === "shengwang" ? "对方已接听 · 等待对方音频…" : "对方已接听 · 正在协商音频…";
    if (state.callProvider === "webrtc") await processCallSignals();
  } else if (event.event_type === "call.signal") {
    if (state.callProvider === "webrtc") await processCallSignals();
  } else if (["call.rejected", "call.cancelled", "call.ended", "call.missed"].includes(event.event_type) && event.actor_id !== state.data?.user?.id) {
    const labels = { "call.rejected": "对方拒绝了通话", "call.cancelled": "对方取消了通话", "call.ended": "对方已挂断", "call.missed": "通话无人接听" };
    closeCallScreen(labels[event.event_type]);
    loadBootstrap({ quiet: true });
  }
}

async function syncActiveCall() {
  if (!state.data || state.callId) return;
  try {
    const result = await api("/api/v1/calls/active");
    if (result.call && !result.call.is_caller && result.call.state === "ringing") await showIncomingCall(result.call);
  } catch (_) {}
}

async function endCall() {
  if (!state.callId) {
    state.callAttempt += 1;
    closeCallScreen("呼叫已取消");
    return;
  }
  const callId = state.callId;
  const seconds = state.callStartedAt ? Math.max(0, Math.floor((Date.now() - state.callStartedAt) / 1000)) : 0;
  try {
    if (state.callRecord?.state === "ringing") {
      if (state.callRecord.is_caller) await api(`/api/v1/calls/${callId}/cancel`, { method: "POST", body: "{}" });
      else await api(`/api/v1/calls/${callId}/decline`, { method: "POST", body: "{}" });
      closeCallScreen("呼叫已取消");
    } else {
      await api(`/api/v1/calls/${callId}/end`, { method: "POST", body: JSON.stringify({ duration_seconds: seconds, note: "" }) });
      closeCallScreen(`通话已结束 · ${seconds} 秒`);
      await loadBootstrap({ quiet: true });
    }
  } catch (error) {
    document.getElementById("callStatus").textContent = "挂断失败 · 请再次尝试";
    toast(error.message);
  }
}

document.addEventListener("click", async event => {
  const authMode = event.target.closest("[data-auth-mode]");
  if (authMode) { showAccountAuth(authMode.dataset.authMode); return; }
  const nav = event.target.closest("[data-nav]");
  if (nav) {
    if (nav.dataset.nav === "trip" && nav.dataset.tripId) {
      const linked = (state.data?.trips || []).find(item => item.id === nav.dataset.tripId);
      if (!linked) { toast("这段旅行已经删除或没有权限查看"); return; }
      state.data.trip = linked; state.plan = linked.active_variant || "slow"; state.selectedItemIds.clear();
      resetTripLive(linked.id);
      localStorage.setItem("love-trip-id", linked.id); renderIdentity(); renderPlan(); renderHotel();
    }
    navigate(nav.dataset.nav); return;
  }
  const plan = event.target.closest("[data-plan]");
  if (plan) {
    state.plan = plan.dataset.plan; localStorage.setItem("love-plan", state.plan); renderPlan();
    return;
  }
  const privacy = event.target.closest("[data-privacy]");
  if (privacy) {
    setMomentPrivacy(privacy.dataset.privacy);
    const revealField = document.getElementById("revealField");
    if (!revealField.hidden && !document.getElementById("momentRevealAt").value) {
      const tomorrow = new Date(Date.now() + 86400000);
      tomorrow.setMinutes(tomorrow.getMinutes() - tomorrow.getTimezoneOffset());
      document.getElementById("momentRevealAt").value = tomorrow.toISOString().slice(0, 16);
    }
    return;
  }
  const view = event.target.closest('[data-screen="timeline"] [data-view]');
  if (view) {
    await setTimelineView(view.dataset.view);
    return;
  }
  const tripSection = event.target.closest("[data-trip-section]");
  if (tripSection) { navigate("trip"); setTripSection(tripSection.dataset.tripSection); return; }
  const assistantPrompt = event.target.closest("[data-assistant-prompt]");
  if (assistantPrompt) {
    const input = document.getElementById("tripAssistantInput");
    input.value = assistantPrompt.dataset.assistantPrompt;
    input.focus();
    return;
  }
  const lifeSection = event.target.closest("[data-life-section]");
  if (lifeSection) { navigate("life"); setLifeSection(lifeSection.dataset.lifeSection); return; }
  const mealSection = event.target.closest("[data-meal-section]");
  if (mealSection) { setMealSection(mealSection.dataset.mealSection); return; }
  const assistantExample = event.target.closest("[data-life-assistant-example]");
  if (assistantExample) {
    const input = document.getElementById("lifeAssistantInput");
    input.value = assistantExample.dataset.lifeAssistantExample;
    input.focus();
    return;
  }
  const smartAction = event.target.closest("[data-smart-action]");
  if (smartAction) {
    try { await previewSmartAction(smartAction.dataset.smartAction); } catch (error) { toast(error.message); }
    return;
  }
  const smartNode = event.target.closest("[data-smart-node]");
  if (smartNode) {
    // A composed write suggestion is presented as one card.  Treat clicking
    // anywhere on that card exactly like clicking its preview button; opening
    // the component's generic editor here would discard the intent payload and
    // show an apparently empty form.
    const embeddedAction = smartNode.querySelector("[data-smart-action]");
    try {
      if (embeddedAction) await previewSmartAction(embeddedAction.dataset.smartAction);
      else await openSmartNode(smartNode.dataset.smartNode);
    } catch (error) { toast(error.message); }
    return;
  }
  const lifeFilter = event.target.closest("[data-life-filter]");
  if (lifeFilter) { setLifeFilter(lifeFilter.dataset.lifeFilter); return; }
  const actionEl = event.target.closest("[data-action]");
  if (!actionEl) return;
  const action = actionEl.dataset.action;
  if (action === "copy-hotel-booking") {
    const copyText = String(actionEl.dataset.copyText || "").trim();
    if (copyText && navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(copyText)
        .then(() => toast(`已复制“${copyText}”，请在携程酒店搜索框粘贴`))
        .catch(() => toast(`请在携程搜索：${copyText}`));
    } else if (copyText) {
      toast(`请在携程搜索：${copyText}`);
    }
    return;
  }
  if (action === "trip-live-refresh") {
    await refreshTripLive();
    return;
  }
  if (action === "assistant-route") {
    actionEl.disabled = true;
    try { await loadTripLiveRoute(actionEl.dataset.itemId, actionEl.dataset.mode || "walking"); }
    finally { actionEl.disabled = false; }
    return;
  }
  if (action === "assistant-replan-today") {
    if (!liveContextIsFresh(state.tripLive.context)) { toast("当前位置已过期，请先点击“更新当前位置”"); return; }
    const input = document.getElementById("tripAssistantInput");
    input.value = "今天晚了 0 分钟，重新安排后面的内容";
    setTripSection("assistant");
    document.getElementById("tripAssistantForm").requestSubmit();
    return;
  }
  if (action === "assistant-recap-edit") {
    const draftId = actionEl.dataset.draftId;
    const card = (state.tripAssistant.items || []).flatMap(item => item.cards || []).find(item => item.type === "trip_recap" && item.id === draftId);
    if (!card) { toast("回忆草稿已刷新，请重新生成"); return; }
    const title = window.prompt("回忆标题", card.title || "我们的旅行回忆");
    if (title === null) return;
    const body = window.prompt("回忆正文", card.body || "");
    if (body === null) return;
    actionEl.disabled = true;
    try {
      const updated = await api(`/api/v1/trips/${encodeURIComponent(state.data.trip.id)}/assistant/recap/${encodeURIComponent(draftId)}`, {
        method: "PATCH", body: JSON.stringify({ title, body, expected_version: Number(card.version || actionEl.dataset.version || 1) }),
      });
      updateTripAssistantCard("trip_recap", draftId, updated);
      toast("回忆草稿已更新，另一方也能看到");
    } catch (error) { toast(error.status === 409 ? "对方已修改草稿，请刷新后再编辑" : error.message); }
    finally { actionEl.disabled = false; }
    return;
  }
  if (action === "assistant-recap-confirm") {
    const draftId = actionEl.dataset.draftId;
    actionEl.disabled = true;
    try {
      await api(`/api/v1/trips/${encodeURIComponent(state.data.trip.id)}/assistant/recap/${encodeURIComponent(draftId)}/confirm`, {
        method: "POST", body: JSON.stringify({ idempotency_key: `recap-confirm-${crypto.randomUUID?.() || Date.now()}` }),
      });
      updateTripAssistantCard("trip_recap", draftId, { status: "confirmed" });
      await loadBootstrap({ quiet: true });
      setTripSection("assistant", { scroll: false });
      toast("旅行回忆已进入双方时光轴");
    } catch (error) { toast(error.message); }
    finally { actionEl.disabled = false; }
    return;
  }
  if (action === "assistant-recap-cancel") {
    const draftId = actionEl.dataset.draftId;
    actionEl.disabled = true;
    try {
      const updated = await api(`/api/v1/trips/${encodeURIComponent(state.data.trip.id)}/assistant/recap/${encodeURIComponent(draftId)}/cancel`, { method: "POST", body: "{}" });
      updateTripAssistantCard("trip_recap", draftId, updated);
      toast("已取消旅行回忆草稿");
    } catch (error) { toast(error.message); }
    finally { actionEl.disabled = false; }
    return;
  }
  if (action === "open-notifications") {
    try {
      const inbox = await api("/api/v1/notifications?limit=50");
      state.lifeAssistant.notifications = inbox.items || [];
      state.lifeAssistant.unreadCount = Number(inbox.unread_count || 0);
      renderLifeAssistantHome();
      renderNotificationCenter();
      openModal("notificationCenterModal");
    } catch (error) { toast(error.message); }
    return;
  }
  if (action === "open-assistant-plans") {
    try { closeModals(); await openLifeAssistantPlanList(); } catch (error) { toast(error.message); }
    return;
  }
  if (action === "open-assistant-plan") {
    try { closeModals(); await openLifeAssistantPlan(actionEl.dataset.id); } catch (error) { toast(error.message); }
    return;
  }
  if (action === "confirm-assistant-plan") {
    try { await confirmLifeAssistantPlan(actionEl.dataset.id, actionEl); } catch (error) { toast(error.status === 409 ? "方案已经变化，请重新确认最新版本" : error.message); }
    return;
  }
  if (action === "cancel-assistant-plan") {
    if (!window.confirm("取消这份共同方案？已经确认的共同内容不会受影响。")) return;
    try {
      const result = await api("/api/v1/assistant/plans/" + encodeURIComponent(actionEl.dataset.id) + "/cancel", { method: "POST", body: "{}" });
      state.lifeAssistant.activePlan = result;
      renderAssistantPlan(result);
      await loadLifeAssistant({ quiet: true });
      toast("共同方案已取消");
    } catch (error) { toast(error.message); }
    return;
  }
  if (action === "open-notification") {
    try { await openInAppNotification(actionEl.dataset.id); } catch (error) { toast(error.message); }
    return;
  }
  if (action === "archive-notification") {
    try {
      await api("/api/v1/notifications/" + encodeURIComponent(actionEl.dataset.id) + "/archive", { method: "POST", body: "{}" });
      await loadLifeAssistant({ quiet: true });
      renderNotificationCenter();
      toast("通知已归档");
    } catch (error) { toast(error.message); }
    return;
  }
  if (action === "read-all-notifications") {
    try {
      await api("/api/v1/notifications/read-all", { method: "POST", body: "{}" });
      await loadLifeAssistant({ quiet: true });
      renderNotificationCenter();
      toast("通知已全部标记为已读");
    } catch (error) { toast(error.message); }
    return;
  }
  if (action === "assistant-priority") {
    if (actionEl.dataset.priorityType === "assistant_plan") {
      try { await openLifeAssistantPlan(actionEl.dataset.resourceId); } catch (error) { toast(error.message); }
    } else if (actionEl.dataset.priorityType === "notification") {
      try { await openInAppNotification(actionEl.dataset.resourceId); } catch (error) { toast(error.message); }
    } else {
      navigate("life"); setLifeSection("todo");
    }
    return;
  }
  if (action === "assistant-count") {
    const key = actionEl.dataset.countKey;
    if (["unread_messages"].includes(key)) navigate("chat");
    else if (["pending_plans"].includes(key)) await openLifeAssistantPlanList();
    else if (["unread_notifications"].includes(key)) document.querySelector('[data-action="open-notifications"]')?.click();
    else if (["meal_wishes", "shopping_items"].includes(key)) { navigate("life"); setLifeSection("meals"); setMealSection(key === "meal_wishes" ? "wishes" : "shopping"); }
    else if (key === "pending_trips") navigate("trip");
    else { navigate("life"); setLifeSection(key === "anniversaries" ? "anniversary" : "todo"); }
    return;
  }
  if (action === "assistant-open-plan") {
    state.plan = actionEl.dataset.style || "slow";
    localStorage.setItem("love-plan", state.plan);
    renderPlan(); setTripSection("plans");
    return;
  }
  if (action === "assistant-select-quote") {
    actionEl.disabled = true;
    try {
      await api(`/api/v1/quotes/${encodeURIComponent(actionEl.dataset.quoteId)}/select`, { method: "POST", body: JSON.stringify({ expected_version: state.data?.trip?.version }) });
      await loadBootstrap({ quiet: true });
      setTripSection("assistant", { scroll: false });
      toast("酒店已加入共同旅行，另一方也能看到");
    } catch (error) { toast(error.status === 409 ? "旅行已被另一方修改，请刷新后再选择" : error.message); }
    finally { actionEl.disabled = false; }
    return;
  }
  if (action === "assistant-select-food") {
    actionEl.disabled = true;
    try {
      const selected = await api(`/api/v1/trips/${encodeURIComponent(state.data.trip.id)}/food-recommendations/${encodeURIComponent(actionEl.dataset.foodId)}/select`, { method: "POST", body: "{}" });
      state.data.trip = selected;
      state.data.trips = (state.data.trips || []).map(item => item.id === selected.id ? selected : item);
      renderPlan(); await loadTripAssistant({ quiet: true });
      toast("餐厅已加入共同旅行，等待双方确认");
    } catch (error) { toast(error.message); }
    finally { actionEl.disabled = false; }
    return;
  }
  if (action === "assistant-confirm-change") {
    actionEl.disabled = true;
    try {
      await api(`/api/v1/trips/${encodeURIComponent(state.data.trip.id)}/assistant/actions/${encodeURIComponent(actionEl.dataset.draftId)}/confirm`, { method: "POST", body: JSON.stringify({ idempotency_key: `assistant-confirm-${crypto.randomUUID?.() || Date.now()}` }) });
      await loadBootstrap({ quiet: true }); await loadTripAssistant({ quiet: true }); setTripSection("assistant", { scroll: false });
      toast("行程修改已保存为新版本");
    } catch (error) { toast(error.status === 409 ? "对方已经修改旅行，请重新提出调整" : error.message); }
    finally { actionEl.disabled = false; }
    return;
  }
  if (action === "assistant-cancel-change") {
    try {
      await api(`/api/v1/trips/${encodeURIComponent(state.data.trip.id)}/assistant/actions/${encodeURIComponent(actionEl.dataset.draftId)}/cancel`, { method: "POST", body: "{}" });
      await loadTripAssistant({ quiet: true }); toast("已取消这次修改草稿");
    } catch (error) { toast(error.message); }
    return;
  }
  if (action === "open-shared-detail") {
    await openSharedDetail(actionEl.dataset.resourceType, actionEl.dataset.id);
    return;
  }
  if (action === "open-trip-detail") {
    if (state.data?.trip?.id) await openSharedDetail("trip", state.data.trip.id);
    return;
  }
  if (action === "open-shared-recipe") {
    try { closeModals(); await openRecipeDetail(actionEl.dataset.id); } catch (error) { toast(error.message); }
    return;
  }
  if (action === "delete-shared-comment") {
    if (!window.confirm("删除这条评论？")) return;
    try {
      await api(`/api/v1/shared-comments/${encodeURIComponent(actionEl.dataset.id)}`, { method: "DELETE", body: JSON.stringify({ expected_version: Number(actionEl.dataset.version) }) });
      await openSharedDetail(state.sharedDetail.resource_type, state.sharedDetail.resource_id, { refresh: true });
      toast("评论已删除");
    } catch (error) { toast(error.message); }
    return;
  }
  if (action === "toggle-wish") {
    try {
      const completed = actionEl.dataset.completed !== "1";
      const updated = await api(`/api/v1/lists/items/${encodeURIComponent(actionEl.dataset.id)}`, { method: "PATCH", body: JSON.stringify({ completed }) });
      const index = state.data.wishlist.items.findIndex(item => item.id === updated.id);
      if (index >= 0) state.data.wishlist.items[index] = updated;
      renderWishlist(); toast(completed ? "心愿已完成并记录" : "已恢复为待完成");
    } catch (error) { toast(error.message); }
    return;
  }
  if (action === "load-more-recipes") {
    if (!state.meals.recipeNextCursor) return;
    actionEl.disabled = true;
    const query = new URLSearchParams({ cursor: state.meals.recipeNextCursor, limit: "50" });
    const search = document.getElementById("recipeSearchInput")?.value.trim();
    const cuisine = document.getElementById("recipeCuisine")?.value;
    const mealSlot = document.getElementById("recipeMealSlot")?.value;
    const difficulty = document.getElementById("recipeDifficulty")?.value;
    const maxMinutes = document.getElementById("recipeMaxMinutes")?.value;
    if (search) query.set("q", search); if (cuisine) query.set("cuisine", cuisine); if (mealSlot) query.set("meal_slot", mealSlot); if (difficulty) query.set("difficulty", difficulty); if (maxMinutes) query.set("max_minutes", maxMinutes);
    try {
      const result = await api(`/api/v1/recipes?${query}`);
      const known = new Set(state.meals.recipes.map(item => item.id));
      state.meals.recipes.push(...result.items.filter(item => !known.has(item.id)));
      state.meals.recipeNextCursor = result.next_cursor; renderRecipeList();
    } catch (error) { toast(error.message); actionEl.disabled = false; }
    return;
  }
  if (action === "view-recipe") {
    try { await openRecipeDetail(actionEl.dataset.id); } catch (error) { toast(error.message); } return;
  }
  if (action === "view-wish-recipe") {
    const wish = mealWishById(actionEl.dataset.id);
    if (!wish?.recipe_id) { await openWishRecipePicker(actionEl.dataset.id); return; }
    try { await openRecipeDetail(wish.recipe_id, "", wish.id); } catch (error) { toast(error.message); } return;
  }
  if (action === "match-wish-recipe") {
    await openWishRecipePicker(actionEl.dataset.id); return;
  }
  if (action === "open-recipe-variant") {
    try { await openRecipeDetail(actionEl.dataset.id, "", state.meals.openWishId || ""); } catch (error) { toast(error.message); } return;
  }
  if (action === "compare-recipe-variants") {
    const recipe = state.meals.openRecipe;
    const ids = (recipe?.variants || []).slice(0, 3).map(item => item.id);
    if (ids.length < 2) { toast("当前只有一种做法，暂时无需比较"); return; }
    actionEl.disabled = true;
    try {
      const comparison = await api("/api/v1/recipes/compare", { method: "POST", body: JSON.stringify({ recipe_ids: ids }) });
      const host = document.getElementById("recipeVariantComparison");
      if (host) host.innerHTML = `<div class="recipe-comparison"><h4>做法对比</h4><div class="recipe-comparison-grid">${comparison.items.map(item => `<article><strong>${escapeHtml(item.variant_name)}</strong><small>${escapeHtml(item.cooking_method)} · ${item.total_minutes} 分钟 · ${escapeHtml(item.difficulty)}</small></article>`).join("")}</div>${comparison.ingredient_differences.length ? `<h4>用量差异</h4><div class="recipe-difference-list">${comparison.ingredient_differences.map(row => `<p><strong>${escapeHtml(row.name)}</strong>${row.values.map((value, index) => `<span>${escapeHtml(comparison.items[index].variant_name)}：${escapeHtml(value)}</span>`).join("")}</p>`).join("")}</div>` : `<p class="form-note">主要食材用量相同，差异集中在烹饪流程。</p>`}</div>`;
    } catch (error) { toast(error.message); } finally { actionEl.disabled = false; }
    return;
  }
  if (action === "view-recipe-draft") {
    const draft = state.meals.recipeDrafts.find(item => item.id === actionEl.dataset.id);
    if (draft) await openRecipeDetail(draft.recipe, draft.id); return;
  }
  if (action === "schedule-recipe" || action === "schedule-wish") {
    const detailServings = action === "schedule-recipe" ? document.getElementById("recipeDetailServings")?.value : 2;
    const wishId = action === "schedule-wish" ? actionEl.dataset.id : (actionEl.dataset.wishId || "");
    openMealSchedule(actionEl.dataset.recipeId || actionEl.dataset.id, actionEl.dataset.name || "这道菜", wishId, detailServings); return;
  }
  if (action === "choose-wish-recipe") {
    const wish = mealWishById(state.meals.openWishId);
    if (!wish) { toast("这条想吃内容已被修改，请刷新后重试"); return; }
    actionEl.disabled = true;
    try {
      const linked = await api(`/api/v1/meal-wishes/${encodeURIComponent(wish.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ recipe_id: actionEl.dataset.recipeId, expected_version: wish.version }),
      });
      state.meals.wishes = state.meals.wishes.map(item => item.id === linked.id ? linked : item);
      renderMealWishes();
      closeModals();
      if (actionEl.dataset.mode === "schedule") {
        openMealSchedule(actionEl.dataset.recipeId, actionEl.dataset.name || linked.title, linked.id, 2);
      } else {
        await openRecipeDetail(actionEl.dataset.recipeId, "", linked.id);
      }
      toast("具体菜谱已保存，两个人现在都能查看");
    } catch (error) {
      toast(error.status === 409 ? `${error.message}，正在刷新想吃清单` : error.message);
      if (error.status === 409) await loadMeals({ quiet: true });
    } finally { actionEl.disabled = false; }
    return;
  }
  if (action === "browse-recipes-for-wish") {
    const keyword = document.getElementById("mealWishRecipeSearch")?.value.trim() || "";
    closeModals();
    document.getElementById("recipeSearchInput").value = keyword;
    setMealSection("recipes", { load: false });
    await loadMeals({ quiet: false });
    toast("已打开完整菜谱库；也可以继续使用在线找菜谱");
    return;
  }
  if (action === "wish-recipe") {
    closeModals(); document.getElementById("mealWishForm").reset(); document.getElementById("mealWishRecipeId").value = actionEl.dataset.id; document.getElementById("mealWishName").value = actionEl.dataset.name; openModal("mealWishModal"); return;
  }
  if (action === "new-meal-wish") {
    document.getElementById("mealWishForm").reset(); document.getElementById("mealWishRecipeId").value = ""; openModal("mealWishModal"); return;
  }
  if (action === "new-custom-recipe") {
    const form = document.getElementById("customRecipeForm"); form.reset(); delete form.dataset.recipeId; delete form.dataset.version;
    document.getElementById("customRecipeTitle").textContent = "创建中文菜谱"; form.querySelector(".primary-button").textContent = "保存菜谱";
    document.getElementById("customRecipeVariantName").value = "经典做法";
    document.getElementById("customRecipeCookingMethod").value = "经典做法";
    document.getElementById("customRecipeCover").required = true;
    openModal("customRecipeModal"); return;
  }
  if (action === "edit-recipe") { openRecipeEditor(state.meals.openRecipe); return; }
  if (action === "delete-recipe") {
    if (!window.confirm("删除这份菜谱？已安排到日历的历史快照不会消失。")) return;
    try {
      await api(`/api/v1/recipes/${encodeURIComponent(actionEl.dataset.id)}`, { method: "DELETE", body: JSON.stringify({ expected_version: Number(actionEl.dataset.version) }) });
      closeModals(); await loadMeals({ quiet: true }); toast("菜谱已删除");
    } catch (error) { toast(error.message); }
    return;
  }
  if (action === "ai-recipe") {
    const promptText = window.prompt("想生成什么菜？也可以写口味、人数和忌口，例如：两人份少辣番茄牛腩");
    if (!promptText) return;
    actionEl.disabled = true;
    try {
      const job = await api("/api/v1/recipes/generate", { method: "POST", body: JSON.stringify({ prompt: promptText }) });
      state.meals.recipeDrafts.unshift(job.draft); renderRecipeList(); await openRecipeDetail(job.draft.recipe, job.draft.id);
      toast(job.mock ? "已用本地中文菜谱生成草稿，请确认后保存" : "智能菜谱草稿已生成，请确认后保存");
    } catch (error) { toast(error.message); } finally { actionEl.disabled = false; }
    return;
  }
  if (action === "online-recipe") {
    const keyword = String(document.getElementById("recipeSearchInput")?.value || "").trim();
    if (!keyword) { toast("请先在搜索框输入菜名或食材，再点击在线找菜谱"); return; }
    const provider = document.getElementById("onlineRecipeProvider")?.value || "tianapi";
    actionEl.disabled = true;
    try {
      const result = await api("/api/v1/recipes/search-online", { method: "POST", body: JSON.stringify({ query: keyword, limit: 5, provider }) });
      state.meals.recipeDrafts = [...result.items, ...state.meals.recipeDrafts]; renderRecipeList();
      toast(`${result.items.length} 份中文草稿已列在最前面；${result.notice || "保存后才进入共同菜谱"}`);
    } catch (error) { toast(error.message); } finally { actionEl.disabled = false; }
    return;
  }
  if (action === "confirm-recipe-draft") {
    try {
      const draft = state.meals.recipeDrafts.find(item => item.id === actionEl.dataset.id);
      const payload = { visibility: "couple" };
      if (!exactRecipeCover(draft?.recipe)) {
        const file = document.getElementById("recipeExactCoverInput")?.files?.[0];
        const confirmed = document.getElementById("recipeExactCoverConfirmed")?.checked;
        if (!file || !confirmed) throw new Error("保存前请上传并确认一张与菜品及当前做法完全对应的成品图");
        const uploaded = await uploadMedia(file, "shared");
        payload.cover_url = uploaded.url;
        payload.cover_confirmed_exact = true;
        payload.cover_attribution = "用户上传并人工核对";
      }
      await api(`/api/v1/recipes/drafts/${encodeURIComponent(actionEl.dataset.id)}/confirm`, { method: "POST", body: JSON.stringify(payload) });
      state.meals.recipeDrafts = state.meals.recipeDrafts.filter(item => item.id !== actionEl.dataset.id); closeModals(); await loadMeals({ quiet: true }); toast("菜谱已保存，两个人都能查看");
    } catch (error) { toast(error.message); } return;
  }
  if (action === "upload-recipe-image") {
    const recipe = state.meals.openRecipe;
    const file = document.getElementById("recipeExactCoverInput")?.files?.[0];
    if (!file || !document.getElementById("recipeExactCoverConfirmed")?.checked) { toast("请选择图片，并确认它与当前菜品及做法一致"); return; }
    actionEl.disabled = true;
    try {
      const uploaded = await uploadMedia(file, recipe.visibility === "private" ? "private" : "shared");
      let target = recipe;
      if (recipe.source_type === "built_in") {
        target = await api(`/api/v1/recipes/${encodeURIComponent(recipe.id)}/copy`, { method: "POST", body: JSON.stringify({ visibility: "couple" }) });
      }
      await api(`/api/v1/recipes/${encodeURIComponent(target.id)}/media`, { method: "POST", body: JSON.stringify({ media_type: "image", role: "cover", url: uploaded.url, thumbnail_url: uploaded.thumbnail_url, provider: "local", title: `${target.name}成品图`, attribution: "用户上传并人工核对", confirmed_exact: true, alignment_score: 1, checked_ingredients: true, checked_steps: true }) });
      await loadMeals({ quiet: true }); await openRecipeDetail(target.id); toast("准确成品图已保存");
    } catch (error) { toast(error.message); } finally { actionEl.disabled = false; }
    return;
  }
  if (action === "add-recipe-video") {
    let recipe = state.meals.openRecipe;
    try {
      if (recipe.source_type === "built_in") {
        toast("请先上传并确认这道菜的准确成品图，再添加视频教程");
        return;
      }
      const form = document.getElementById("recipeVideoForm"); form.reset();
      document.getElementById("recipeVideoRecipeId").value = recipe.id;
      document.getElementById("recipeVideoName").value = `${recipe.dish_name || recipe.name}·${recipe.variant_name || recipe.cooking_method || "经典做法"}`;
      document.getElementById("recipeVideoScore").value = "70";
      closeModals(); openModal("recipeVideoModal");
    } catch (error) { toast(error.message); }
    return;
  }
  if (action === "check-recipe-media") {
    actionEl.disabled = true;
    try {
      const result = await api(`/api/v1/recipe-media/${encodeURIComponent(actionEl.dataset.id)}/check`, { method: "POST", body: "{}" });
      toast(result.check_result || "链接检查完成");
      if (state.meals.openRecipe?.id) await openRecipeDetail(state.meals.openRecipe.id);
    } catch (error) { toast(error.message); } finally { actionEl.disabled = false; }
    return;
  }
  if (action === "delete-meal-wish") {
    if (!window.confirm("从想吃清单移除这项？")) return;
    try { await api(`/api/v1/meal-wishes/${encodeURIComponent(actionEl.dataset.id)}`, { method: "DELETE", body: JSON.stringify({ expected_version: Number(actionEl.dataset.version) }) }); await loadMeals({ quiet: true }); toast("已从想吃清单移除"); } catch (error) { toast(error.message); } return;
  }
  if (action === "select-meal-date") {
    const oldBounds = mealCalendarBounds();
    state.mealSelectedDate = actionEl.dataset.date;
    state.mealMonth = new Date(`${state.mealSelectedDate}T00:00:00`);
    if (state.mealSelectedDate < oldBounds.start || state.mealSelectedDate > oldBounds.end) await loadMeals({ quiet: true });
    else renderMealCalendar();
    return;
  }
  if (action === "meal-calendar-view") {
    state.mealCalendarView = actionEl.dataset.view === "month" ? "month" : "week";
    localStorage.setItem("love-meal-calendar-view", state.mealCalendarView);
    state.mealMonth = new Date(`${state.mealSelectedDate}T00:00:00`);
    await loadMeals({ quiet: true }); return;
  }
  if (action === "meal-today") {
    state.mealSelectedDate = localDateValue(); state.mealMonth = new Date(`${state.mealSelectedDate}T00:00:00`);
    await loadMeals({ quiet: true }); return;
  }
  if (action === "meal-prev-period" || action === "meal-next-period") {
    const direction = action === "meal-next-period" ? 1 : -1;
    const selected = new Date(`${state.mealSelectedDate}T00:00:00`);
    if (state.mealCalendarView === "week") selected.setDate(selected.getDate() + direction * 7);
    else selected.setMonth(selected.getMonth() + direction, 1);
    state.mealSelectedDate = localDateValue(selected); state.mealMonth = new Date(selected);
    await loadMeals({ quiet: true }); return;
  }
  if (action === "edit-meal") {
    const item = state.meals.cart?.items?.find(value => value.id === actionEl.dataset.id);
    if (item) openMealEditor(item); else toast("这条安排已经变化，请刷新后再试");
    return;
  }
  if (action === "archive-meal-cart") {
    if (!state.meals.cart?.id) { toast("当前没有可归档的点菜车"); return; }
    if (!window.confirm("只有全部完成或取消后才能归档。现在尝试归档吗？")) return;
    try {
      await api("/api/v1/meal-cart/archive", { method: "POST", body: JSON.stringify({ expected_version: state.meals.cart.version }) });
      await loadMeals({ quiet: true }); toast("已归档，本月历史仍可在日历查看");
    } catch (error) { toast(error.message); }
    return;
  }
  if (action === "meal-status") {
    try { await api(`/api/v1/meal-cart/items/${encodeURIComponent(actionEl.dataset.id)}`, { method: "PATCH", body: JSON.stringify({ status: actionEl.dataset.status, expected_version: Number(actionEl.dataset.version) }) }); await loadMeals({ quiet: true }); toast(`状态已更新为“${mealStatusLabels[actionEl.dataset.status]}”`); } catch (error) { toast(error.message); } return;
  }
  if (action === "cancel-meal") {
    if (!window.confirm("取消这道菜的安排？历史仍会保留。")) return;
    try { await api(`/api/v1/meal-cart/items/${encodeURIComponent(actionEl.dataset.id)}`, { method: "DELETE", body: JSON.stringify({ expected_version: Number(actionEl.dataset.version) }) }); await loadMeals({ quiet: true }); toast("这道菜已取消"); } catch (error) { toast(error.message); } return;
  }
  if (action === "repeat-meal") {
    const targetDate = window.prompt("再次安排到哪一天？请输入 YYYY-MM-DD", localDateValue(new Date(Date.now() + 86400000)));
    if (!targetDate) return;
    try { await api(`/api/v1/meal-plans/${encodeURIComponent(actionEl.dataset.id)}/repeat`, { method: "POST", body: JSON.stringify({ planned_date: targetDate, idempotency_key: clientKey("meal-repeat") }) }); state.mealMonth = new Date(`${targetDate}T00:00:00`); state.mealSelectedDate = targetDate; await loadMeals({ quiet: true }); toast("已再次安排到用餐日历"); } catch (error) { toast(error.message); } return;
  }
  if (action === "preview-shopping") {
    const start = localDateValue(); const end = new Date(); end.setDate(end.getDate() + 7);
    document.getElementById("shoppingRangeStart").value = start; document.getElementById("shoppingRangeEnd").value = localDateValue(end); document.getElementById("shoppingPreviewContent").innerHTML = ""; document.getElementById("applyShoppingPreview").hidden = true; openModal("shoppingPreviewModal"); return;
  }
  if (action === "build-shopping-preview") {
    try {
      const result = await api("/api/v1/shopping-lists/preview", { method: "POST", body: JSON.stringify({ range_start: document.getElementById("shoppingRangeStart").value, range_end: document.getElementById("shoppingRangeEnd").value, expected_version: state.meals.shopping?.version || 0 }) });
      state.meals.shoppingPreview = result; const preview = result.preview;
      document.getElementById("shoppingPreviewContent").innerHTML = `<div class="shopping-preview-summary"><p class="form-note">包含 ${preview.meal_count} 道已安排菜品，共 ${preview.items.length} 项食材。先看差异，确认后才会改动采购清单。</p>${preview.diff.added.map(item => `<article><b>新增</b> ${escapeHtml(item.display_name)} · ${escapeHtml(item.display_quantity)}</article>`).join("")}${preview.diff.updated.map(item => `<article><b>更新</b> ${escapeHtml(item.after.display_name)} · ${escapeHtml(item.after.display_quantity)}</article>`).join("")}${preview.diff.removed.map(item => `<article><b>移除</b> ${escapeHtml(item.display_name)}</article>`).join("")}</div>`;
      document.getElementById("applyShoppingPreview").hidden = false;
    } catch (error) { toast(error.message); } return;
  }
  if (action === "apply-shopping-preview") {
    if (!state.meals.shoppingPreview) return;
    try { await api("/api/v1/shopping-lists/apply", { method: "POST", body: JSON.stringify({ preview_id: state.meals.shoppingPreview.preview_id, idempotency_key: clientKey("shopping") }) }); state.meals.shoppingPreview = null; closeModals(); setMealSection("shopping", { load: false }); await loadMeals({ quiet: true }); toast("采购清单已生成，双方可同步勾选"); } catch (error) { toast(error.message); } return;
  }
  if (action === "new-shopping-item") {
    const form = document.getElementById("shoppingItemForm"); form.reset(); delete form.dataset.itemId; delete form.dataset.version;
    document.getElementById("shoppingItemTitle").textContent = "添加临时食材"; form.querySelector(".primary-button").textContent = "添加到采购清单";
    openModal("shoppingItemModal"); return;
  }
  if (action === "edit-shopping-item") {
    const item = state.meals.shopping?.items?.find(value => value.id === actionEl.dataset.id);
    if (!item) { toast("这项食材已经变化，请刷新后再试"); return; }
    const form = document.getElementById("shoppingItemForm"); const values = shoppingEditorValues(item);
    form.dataset.itemId = item.id; form.dataset.version = item.version;
    document.getElementById("shoppingItemTitle").textContent = "编辑采购食材";
    document.getElementById("shoppingItemName").value = item.display_name;
    document.getElementById("shoppingItemQuantity").value = String(values.quantity);
    document.getElementById("shoppingItemUnit").value = values.unit;
    document.getElementById("shoppingItemCategory").value = item.category || "其他";
    form.querySelector(".primary-button").textContent = "保存修改";
    openModal("shoppingItemModal"); return;
  }
  if (action === "delete-shopping-item") {
    if (!window.confirm("删除这项食材？")) return;
    try { await api(`/api/v1/shopping-lists/items/${encodeURIComponent(actionEl.dataset.id)}`, { method: "DELETE", body: JSON.stringify({ expected_version: Number(actionEl.dataset.version) }) }); await loadMeals({ quiet: true }); toast("食材已删除"); } catch (error) { toast(error.message); } return;
  }
  if (action === "complete-shopping") {
    const pending = state.meals.shopping?.items?.filter(item => !item.checked).length || 0;
    if (pending && !window.confirm(`还有 ${pending} 项未勾选，仍要完成本次采购吗？`)) return;
    try { await api("/api/v1/shopping-lists/complete", { method: "POST", body: JSON.stringify({ expected_version: state.meals.shopping.version, allow_incomplete: pending > 0 }) }); await loadMeals({ quiet: true }); toast("本次采购已经完成并归档"); } catch (error) { toast(error.message); } return;
  }
  if (action === "open-on-this-day") { navigate("timeline"); document.querySelectorAll('[data-screen="timeline"] [data-view]').forEach(button => button.classList.remove("active")); renderOnThisDay(); scrollToContent("#timelineView"); }
  if (action === "open-anniversaries") { navigate("life"); setLifeSection("anniversary"); }
  if (action === "open-wishlist") { navigate("life"); setLifeSection("wishlist"); }
  if (action === "open-wishlist-editor") {
    const form = document.getElementById("wishlistForm"); form.reset(); delete form.dataset.itemId; delete form.dataset.version;
    document.getElementById("wishlistModalTitle").textContent = "添加共同愿望"; openModal("wishlistModal");
  }
  if (action === "edit-wish") {
    const item = state.data?.wishlist?.items?.find(value => value.id === actionEl.dataset.id);
    if (item) {
      const form = document.getElementById("wishlistForm"); form.dataset.itemId = item.id; form.dataset.version = item.version;
      document.getElementById("wishlistModalTitle").textContent = "编辑共同愿望";
      document.getElementById("wishlistText").value = item.text; document.getElementById("wishlistNote").value = item.note || ""; openModal("wishlistModal");
    }
  }
  if (action === "delete-wish" && window.confirm("删除这个共同愿望？")) {
    const returnToWishlist = state.screen === "life" && state.lifeSection === "wishlist";
    try {
      await api(`/api/v1/lists/items/${actionEl.dataset.id}`, { method: "DELETE", body: JSON.stringify({ expected_version: Number(actionEl.dataset.version) }) });
      await loadBootstrap({ quiet: true });
      if (returnToWishlist && state.screen === "life" && state.lifeSection === "wishlist") setLifeSection("wishlist");
      toast("共同愿望已删除");
    } catch (error) { toast(error.message); }
  }
  if (action === "open-month") renderMonthMoments(actionEl.dataset.month);
  if (action === "open-location") renderLocationMoments(actionEl.dataset.location);
  if (action === "react-moment") {
    try {
      await api(`/api/v1/moments/${encodeURIComponent(actionEl.dataset.id)}/reaction`, { method: "POST", body: JSON.stringify({ reaction: actionEl.dataset.reaction }) });
      await loadBootstrap({ quiet: true });
      toast(actionEl.dataset.reaction === "none" ? "已取消回应" : "回应已同步给对方");
    } catch (error) { toast(error.message); }
  }
  if (action === "send-heart") { actionEl.textContent = "♥"; try { await sendMessage("想你了 ♡"); toast("一颗小心心已送达"); } catch (error) { toast(error.message); } }
  if (action === "edit-status") openModal("statusModal");
  if (action === "edit-location") { closeModals(); openModal("locationModal"); }
  if (action === "clear-status") {
    try { await api("/api/v1/me/status/clear", { method: "POST", body: "{}" }); closeModals(); await loadBootstrap({ quiet: true }); toast("状态已清除"); } catch (error) { toast(error.message); }
  }
  if (action === "open-weekly") { renderWeeklyReview(); openModal("weeklyModal"); }
  if (action === "confirm-weekly") {
    try { state.data.weeklyReview = await api(`/api/v1/weekly-reviews/${state.data.weeklyReview.id}/confirm`, { method: "POST", body: "{}" }); renderWeeklyReview(); if (state.data.weeklyReview.status === "confirmed") await loadBootstrap({ quiet: true }); toast(state.data.weeklyReview.status === "confirmed" ? "本周回顾已进入时光轴" : "已确认，等待 TA"); } catch (error) { toast(error.message); }
  }
  if (action === "new-date") {
    navigate("life"); setLifeSection("dates", { scroll: false });
    const tomorrow = new Date(Date.now() + 86400000); document.getElementById("dateDay").value = tomorrow.toISOString().slice(0, 10); openModal("dateModal");
  }
  if (action === "install-app") {
    if (state.installPrompt) {
      state.installPrompt.prompt();
      await state.installPrompt.userChoice.catch(() => null);
      state.installPrompt = null;
    } else {
      toast(/iPhone|iPad/.test(navigator.userAgent) ? "请点浏览器分享按钮，再选“添加到主屏幕”" : "请使用浏览器菜单中的“安装应用/添加到主屏幕”");
    }
  }
  if (action === "enable-notifications") {
    actionEl.disabled = true;
    try { await enablePushNotifications(); toast("消息与来电提醒已开启，可发送测试通知确认"); }
    catch (error) { renderPushStatus(error.message, "warning"); toast(error.message); }
    finally { actionEl.disabled = false; }
  }
  if (action === "test-notification") {
    actionEl.disabled = true;
    try { await testPushNotification(); toast("测试通知已发送，请查看通知中心"); }
    catch (error) { toast(error.message); }
    finally { actionEl.disabled = false; }
  }
  if (action === "disable-notifications") {
    actionEl.disabled = true;
    try { await disablePushNotifications(); toast("当前设备的锁屏提醒已关闭"); }
    catch (error) { toast(error.message); }
    finally { actionEl.disabled = false; }
  }
  if (action === "new-moment") {
    openMomentComposer(actionEl.dataset.type || "回忆");
  }
  if (action === "edit-moment") {
    const moment = (state.data?.moments || []).find(item => item.id === actionEl.dataset.id);
    if (moment) openMomentEditor(moment);
    else toast("这段回忆已经变化，请刷新后再试");
  }
  if (action === "close-modal") closeModals();
  if (action === "save-moment") {
    const title = document.getElementById("momentTitle").value.trim();
    if (!title) { toast("请先写一个标题"); return; }
    const occurredValue = document.getElementById("momentOccurredAt").value;
    if (!occurredValue) { toast("请选择发生时间"); return; }
    const revealValue = document.getElementById("momentRevealAt").value;
    if (state.privacy === "延时公开" && !revealValue) { toast("请选择公开时间"); return; }
    try {
      const commonPayload = {
        title,
        body: document.getElementById("momentNote").value,
        occurred_at: new Date(occurredValue).toISOString(),
        location: document.getElementById("momentLocation").value.trim(),
        mood: document.getElementById("momentMood").value.trim(),
        visibility: state.privacy,
        reveal_at: revealValue ? new Date(revealValue).toISOString() : null,
      };
      if (state.editingMomentId) {
        const moment = (state.data?.moments || []).find(item => item.id === state.editingMomentId);
        if (!moment) throw new Error("这段回忆已经变化，请刷新后再试");
        await api(`/api/v1/moments/${encodeURIComponent(moment.id)}`, {
          method: "PATCH",
          body: JSON.stringify({ ...commonPayload, expected_version: moment.version }),
        });
      } else {
        const file = document.getElementById("momentMedia").files[0];
        let mediaId = null;
        if (file) {
          const uploaded = await uploadMedia(file, state.privacy === "仅自己" ? "private" : "shared");
          mediaId = uploaded.id;
          state.momentType = uploaded.kind === "image" ? "photo" : uploaded.kind;
        }
        await api("/api/v1/moments", { method: "POST", body: JSON.stringify({ ...commonPayload, type: state.momentType, media_id: mediaId }) });
      }
      const edited = Boolean(state.editingMomentId);
      state.editingMomentId = null;
      closeModals();
      await loadBootstrap({ quiet: true });
      await setTimelineView("list", { scroll: false });
      toast(edited ? "修改已同步给双方" : `已按“${state.privacy}”保存到数据库`);
    } catch (error) { toast(error.status === 409 ? "内容已被更新，请刷新后重新修改" : error.message); }
  }
  if (action === "answer-question") { document.getElementById("dailyAnswer").value = ""; openModal("answerModal"); }
  if (action === "quick-message") { try { await sendMessage(actionEl.textContent.trim()); } catch (error) { toast(error.message); } }
  if (action === "attach") document.getElementById("chatMediaInput").click();
  if (action === "import-chat") {
    state.chatImport = null; document.getElementById("chatImportForm").hidden = false; document.getElementById("chatImportDraft").hidden = true; openModal("chatImportModal");
  }
  if (action === "save-message") await saveMessageMoment(actionEl.dataset.messageId);
  if (action === "start-call") startCall();
  if (action === "accept-call") acceptIncomingCall();
  if (action === "decline-call") declineIncomingCall();
  if (action === "end-call") endCall();
  if (action === "mute") {
    const track = state.callLocalStream?.getAudioTracks?.()[0];
    if (!track) toast("麦克风尚未连接");
    else if (state.agoraLocalTrack) {
      state.callMuted = !state.callMuted;
      await state.agoraLocalTrack.setMuted(state.callMuted);
      actionEl.classList.toggle("selected", state.callMuted);
      toast(state.callMuted ? "已静音" : "麦克风已恢复");
    } else {
      track.enabled = !track.enabled; actionEl.classList.toggle("selected", !track.enabled); toast(track.enabled ? "麦克风已恢复" : "已静音");
    }
  }
  if (action === "speaker") { actionEl.classList.toggle("selected"); toast("网页音频输出由手机或电脑的系统声音设置控制"); }
  if (action === "view-itinerary-detail") openItineraryDetail(actionEl.dataset.itemId);
  if (action === "edit-plan") {
    const item = currentEditingTripItem();
    if (!item) { toast("当前方案还没有可调整的行程项目"); return; }
    state.pendingRevision = null;
    document.getElementById("editItemTitle").value = item.title || "";
    document.getElementById("editItemTime").value = item.start_time || "09:00";
    document.getElementById("editItemNote").value = item.note || "";
    document.getElementById("revisionPreview").hidden = true;
    const applyButton = document.getElementById("applyRevisionButton"); applyButton.hidden = true; applyButton.disabled = true;
    openModal("editModal");
  }
  if (action === "generate-trip") {
    actionEl.disabled = true; actionEl.textContent = "生成中…";
    try {
      state.data.trip = await api(`/api/v1/trips/${state.data.trip.id}/generate`, { method: "POST", body: "{}" });
      state.plan = "slow"; state.selectedItemIds.clear(); renderPlan(); renderHotel();
      const generated = tripVariant("slow");
      toast(generated?.generation_source === "ai"
        ? "大模型已生成五套方案，人工确认前不会覆盖其他内容"
        : `大模型本次未完成，已保留可编辑的本地方案：${generated?.generation_message || "请稍后重试"}`);
    } catch (error) { toast(error.message); }
    finally { actionEl.disabled = false; actionEl.textContent = "重新生成"; }
  }
  if (action === "search-destination") {
    const query = document.getElementById("tripDestination").value.trim();
    if (query.length < 2) { toast("请先填写至少两个字的地点名称"); return; }
    actionEl.disabled = true; actionEl.textContent = "正在搜索国内外地点…";
    try {
      clearDestinationResolution({ cancelSearch: true });
      await searchDestinationCandidates({ includeGlobal: true, announce: true });
    } finally { actionEl.disabled = false; actionEl.textContent = "没找到合适的？搜索国内外更多地点"; }
  }
  if (action === "select-destination") {
    const candidate = state.destinationCandidates.find(item => item.candidate_id === actionEl.dataset.candidateId);
    if (!candidate) { toast("地点候选已经变化，请重新搜索"); return; }
    document.getElementById("tripDestination").value = candidate.name;
    const arrival = candidate.city || candidate.district || candidate.province || document.getElementById("tripDestinationRegion").value.trim();
    document.getElementById("tripArrivalCity").value = arrival;
    document.getElementById("tripDestinationRegion").value = arrival;
    clearTimeout(state.destinationSearchTimer);
    state.destinationSearchSequence += 1;
    renderDestinationSelection(candidate, candidate.candidate_id);
    const candidateList = document.getElementById("tripDestinationCandidates");
    candidateList.innerHTML = ""; candidateList.hidden = true;
    document.getElementById("tripDestination").setAttribute("aria-expanded", "false");
    destinationSearchStatus(`已确认 ${candidate.name} · ${arrival || "地图地点"}`);
    toast(`已确认“${candidate.name}”，不会再自动切换到异地同名地点`);
  }
  if (action === "apply-revision") {
    if (!state.pendingRevision) { toast("请先检查改动影响"); return; }
    try {
      await api(`/api/v1/trips/items/${state.editingItemId}`, { method: "PATCH", body: JSON.stringify({ changes: state.pendingRevision, reason: "用户在行程编辑器中确认修改" }) });
      state.pendingRevision = null; closeModals(); await loadBootstrap({ quiet: true }); navigate("trip"); toast("修改已写入新版本，未影响其他日期");
    } catch (error) { toast(error.message); }
  }
  if (action === "toggle-mix") {
    const id = actionEl.dataset.itemId;
    if (!id) return;
    if (state.selectedItemIds.has(id)) state.selectedItemIds.delete(id); else state.selectedItemIds.add(id);
    renderPlan();
  }
  if (action === "select-all-items") {
    const variant = tripVariant(state.plan);
    (variant?.days || []).flatMap(day => day.items || []).forEach(item => state.selectedItemIds.add(item.id));
    renderPlan(); toast(`已选择当前方案的 ${state.selectedItemIds.size} 个地点`);
  }
  if (action === "clear-mix") {
    state.selectedItemIds.clear(); renderPlan(); toast("已清空混搭选择");
  }
  if (action === "refresh-food" || action === "refresh-food-day") {
    const requestedDay = action === "refresh-food-day" ? Number(actionEl.dataset.dayNumber) : null;
    const areaInputs = [...document.querySelectorAll("[data-food-area-day]")];
    const dayAreas = Object.fromEntries(areaInputs.map(input => [input.dataset.foodAreaDay, input.value.trim()]));
    areaInputs.forEach(input => localStorage.setItem(`love-food-area-${state.data.trip.id}-${input.dataset.foodAreaDay}`, input.value.trim()));
    const relevantDays = requestedDay ? [String(requestedDay)] : Object.keys(dayAreas);
    const missing = relevantDays.filter(day => !dayAreas[day]);
    if (missing.length && isBroadFoodDestination(state.data.trip.destination)) { toast(`请先填写第 ${missing.join("、")} 天的具体游览或住宿区域`); return; }
    actionEl.disabled = true; const original = actionEl.textContent; actionEl.textContent = "正在查询高德…";
    try {
      const result = await api(`/api/v1/trips/${state.data.trip.id}/food-recommendations`, { method: "POST", body: JSON.stringify({ day_areas: dayAreas, day_number: requestedDay }) });
      state.data.trip = result.trip;
      state.data.trips = (state.data.trips || []).map(item => item.id === result.trip.id ? result.trip : item);
      renderPlan(); toast(result.count ? `已找到并保存 ${result.count} 个 5 公里内真实餐馆候选` : result.notice);
    } catch (error) { toast(error.message); }
    finally { actionEl.disabled = false; actionEl.textContent = original; }
  }
  if (action === "refresh-item-food") {
    const itemId = actionEl.dataset.itemId;
    actionEl.disabled = true; const original = actionEl.textContent; actionEl.textContent = "查询中…";
    try {
      const result = await api(`/api/v1/trips/${state.data.trip.id}/food-recommendations`, { method: "POST", body: JSON.stringify({ item_id: itemId, search_area: actionEl.dataset.searchArea || "" }) });
      state.data.trip = result.trip;
      state.data.trips = (state.data.trips || []).map(item => item.id === result.trip.id ? result.trip : item);
      renderPlan(); openItineraryDetail(itemId);
      toast(result.count ? `已保存 ${result.count} 家附近餐厅和 ${result.review_count || 0} 条具体公开口碑来源` : result.notice);
    } catch (error) { toast(error.message); }
    finally { actionEl.disabled = false; actionEl.textContent = original; }
  }
  if (action === "select-food") {
    actionEl.disabled = true;
    try {
      const selected = await api(`/api/v1/trips/${state.data.trip.id}/food-recommendations/${actionEl.dataset.foodId}/select`, { method: "POST", body: "{}" });
      state.data.trip = selected;
      state.data.trips = (state.data.trips || []).map(item => item.id === selected.id ? selected : item);
      renderPlan();
      if (document.getElementById("itineraryDetailModal")?.classList.contains("open") && state.itineraryDetailItemId) openItineraryDetail(state.itineraryDetailItemId);
      toast("餐馆已加入共同旅行，请双方重新确认最终方案");
    } catch (error) { toast(error.message); renderPlan(); }
  }
  if (action === "adopt-plan") {
    actionEl.disabled = true;
    try {
      const selected = await api(`/api/v1/trips/${state.data.trip.id}/select-variant`, { method: "POST", body: JSON.stringify({ style: state.plan }) });
      state.data.trip = selected;
      state.data.trips = (state.data.trips || []).map(item => item.id === selected.id ? selected : item);
      state.selectedItemIds.clear(); renderPlan();
      toast(`已采用“${tripVariant(state.plan)?.name || "当前方案"}”，请确认最终方案`);
    } catch (error) { toast(error.message); renderPlan(); }
  }
  if (action === "build-plan") {
    if (!state.selectedItemIds.size) { toast("请先选择行程地点，或在五套方案中直接采用整套方案"); return; }
    try {
      const result = await api(`/api/v1/trips/${state.data.trip.id}/compose`, { method: "POST", body: JSON.stringify({ selected_item_ids: [...state.selectedItemIds] }) });
      await loadBootstrap({ quiet: true }); toast(`最终方案 v${result.revision} 已保存，等待 ${state.data.partner.nickname} 确认`);
    } catch (error) { toast(error.message); }
  }
  if (action === "approve-trip") {
    actionEl.disabled = true;
    try {
      const tripId = state.data.trip.id;
      // A partner event can arrive while this page is re-rendering. Read the
      // authoritative revision immediately before approval so an ordinary
      // click never submits a stale snapshot from the trip switcher.
      const latestTrip = await api(`/api/v1/trips/${encodeURIComponent(tripId)}`);
      state.data.trip = latestTrip;
      state.data.trips = (state.data.trips || []).map(item => item.id === tripId ? latestTrip : item);
      const result = await api(`/api/v1/trips/${encodeURIComponent(tripId)}/approve`, { method: "POST", body: JSON.stringify({ revision: latestTrip.current_revision }) });
      await loadBootstrap({ quiet: true });
      toast(result.confirmed ? "双方已确认，最终旅行方案正式生效" : `你已确认，等待 ${state.data.partner.nickname}`);
    } catch (error) { toast(error.message); renderPlan(); }
  }
  if (action === "new-trip") {
    openTripComposer();
  }
  if (action === "edit-trip") {
    const trip = state.data?.trip;
    if (!trip) return;
    openTripComposer("", trip);
  }
  if (action === "delete-trip") {
    const trip = state.data?.trip;
    if (!trip) { toast("当前没有可删除的旅行"); return; }
    const confirmed = window.confirm(`确定删除“${trip.title}”吗？\n\n删除后双方都会看不到这次旅行，但审计记录会保留。`);
    if (!confirmed) return;
    actionEl.disabled = true;
    try {
      const deleted = await api(`/api/v1/trips/${encodeURIComponent(trip.id)}`, { method: "DELETE", body: JSON.stringify({ expected_version: trip.version }) });
      localStorage.removeItem("love-trip-id"); localStorage.removeItem(`love-ui-trip:${trip.id}`);
      state.manifests.trip = null; state.pinnedSmartSurfaces.delete("trip");
      await loadBootstrap({ quiet: true });
      navigate("trip"); setTripSection("overview", { scroll: false });
      toast("旅行已删除，另一方会同步看到变化", { label: "撤销删除", action: "restore-trip", data: { tripId: deleted.id, version: deleted.version } });
    } catch (error) { toast(error.status === 409 ? "旅行已被另一方修改，请刷新后再删除" : error.message); }
    finally { actionEl.disabled = false; }
  }
  if (action === "restore-trip") {
    actionEl.disabled = true;
    try {
      const restored = await api(`/api/v1/trips/${encodeURIComponent(actionEl.dataset.tripId)}/restore`, { method: "POST", body: JSON.stringify({ expected_version: Number(actionEl.dataset.version) }) });
      localStorage.setItem("love-trip-id", restored.id);
      // The restore is authoritative once the POST succeeds. Confirm it to the
      // user before the broader page refresh, which can take longer on a slow
      // mobile connection and must not hide the successful recovery feedback.
      toast(`“${restored.title}”已恢复，双方都能重新看到`);
      navigate("trip"); setTripSection("overview", { scroll: false });
      // Refresh in the background. Keeping navigation outside the async tail
      // prevents a late network response from pulling the user back to Travel
      // after they already moved to another page.
      loadBootstrap({ quiet: true });
    } catch (error) { toast(error.status === 409 ? "旅行状态已经变化，请刷新后重试" : error.message); }
  }
  if (action === "trip-route") {
    actionEl.disabled = true; const original = actionEl.textContent; actionEl.textContent = "读取路段…";
    try { await loadRouteLegs(); } catch (error) { toast(error.message); }
    finally { actionEl.disabled = false; actionEl.textContent = original; }
  }
  if (action === "query-route-leg") {
    await queryRouteLeg(actionEl.dataset.legId, ["walking", "transit", "driving"], false, actionEl);
  }
  if (action === "query-route-cycling") {
    await queryRouteLeg(actionEl.dataset.legId, ["bicycling"], false, actionEl);
  }
  if (action === "refresh-route-driving") {
    await queryRouteLeg(actionEl.dataset.legId, ["driving"], true, actionEl);
  }
  if (action === "route-place-search") {
    await searchRoutePlace(actionEl.dataset.itemId, actionEl);
  }
  if (action === "confirm-route-place") {
    actionEl.disabled = true;
    try {
      const result = await api(`/api/v1/trips/${encodeURIComponent(state.data.trip.id)}/itinerary-items/${encodeURIComponent(actionEl.dataset.itemId)}/place`, { method: "POST", body: JSON.stringify({ candidate_id: actionEl.dataset.candidateId, expected_version: state.data.trip.version }) });
      state.data.trip = result.trip;
      state.data.trips = (state.data.trips || []).map(item => item.id === result.trip.id ? result.trip : item);
      renderPlan(); renderRouteLegs(result.route_legs); toast("真实地图地点已保存；只需再确认相邻地点即可计算路线");
    } catch (error) {
      if (error.status === 404 || error.status === 409) {
        await loadBootstrap({ quiet: true });
        await loadRouteLegs({ scroll: false });
        toast("旅行方案刚刚更新，已换成最新路段，请重新选择地点");
      } else toast(error.message);
      actionEl.disabled = false;
    }
  }
  if (action === "trip-quotes") {
    try { renderQuoteResults(await api("/api/v1/quotes/search", { method: "POST", body: JSON.stringify(quotePayload("hotel")) })); } catch (error) { toast(error.message); }
  }
  if (action === "search-quotes") {
    const type = actionEl.dataset.quoteType;
    actionEl.disabled = true; const original = actionEl.textContent; actionEl.textContent = "查询中…";
    try { renderQuoteResults(await api("/api/v1/quotes/search", { method: "POST", body: JSON.stringify(quotePayload(type)) })); document.getElementById("tripToolResults").scrollIntoView({ behavior: "smooth", block: "start" }); }
    catch (error) { toast(error.message); }
    finally { actionEl.disabled = false; actionEl.textContent = original; }
  }
  if (action === "select-quote") {
    try {
      const selected = await api(`/api/v1/quotes/${encodeURIComponent(actionEl.dataset.quoteId)}/select`, { method: "POST", body: JSON.stringify({ expected_version: state.data?.trip?.version }) });
      document.querySelectorAll('.quote-result').forEach(card => { const active = card.dataset.quoteId === selected.id; card.classList.toggle("selected", active); const button = card.querySelector('[data-action="select-quote"]'); if (button) button.textContent = active ? "已加入本次旅行" : "选择这个"; });
      if (state.data?.trip) {
        const current = state.data.trip.quotes || [];
        state.data.trip.quotes = current.some(item => item.id === selected.id)
          ? current.map(item => item.quote_type === selected.quote_type ? ({ ...item, selected_at: item.id === selected.id ? selected.selected_at : null, selected_by: item.id === selected.id ? selected.selected_by : null }) : item)
          : [selected, ...current.map(item => item.quote_type === selected.quote_type ? ({ ...item, selected_at: null, selected_by: null }) : item)];
        if (selected.quote_type === "hotel") renderHotel();
        if (selected.trip_version) state.data.trip.version = selected.trip_version;
        if (selected.revision) state.data.trip.current_revision = selected.revision;
      }
      toast(selected.quote_type === "hotel" && selected.details?.latitude != null ? "住宿已加入共同旅行，并已接入每日往返路线" : "已加入共同旅行，另一方刷新后也能看到选择");
    } catch (error) {
      if (error.status === 409) await loadBootstrap({ quiet: true });
      toast(error.message);
    }
  }
  if (action === "manual-quote") {
    const title = window.prompt("报价名称", "酒店手工报价"); const amount = title && window.prompt("金额（元）", "500");
    if (title && amount !== null) try { const item = await api("/api/v1/quotes/manual", { method: "POST", body: JSON.stringify({ trip_id: state.data.trip.id, type: "hotel", title, amount, provider: "手工录入" }) }); document.getElementById("tripToolResults").innerHTML = `<article class="result-card"><span class="mock-badge">手工录入</span><strong>${escapeHtml(item.title)} · ${money(item.amount_cents)}</strong><small>请到预订平台再次确认库存与退改规则</small></article>`; } catch (error) { toast(error.message); }
  }
  if (action === "privacy") {
    const visibility = actionEl.dataset.visibility === "shared" ? "private" : "shared";
    try {
      await api(`/api/v1/moments/${actionEl.dataset.momentId}`, { method: "PATCH", body: JSON.stringify({ visibility }) });
      await loadBootstrap({ quiet: true }); toast(visibility === "private" ? "已设为仅自己可见" : "已设为双方可见");
    } catch (error) { toast(error.message); }
  }
  if (["open-profile", "privacy-center"].includes(action)) { try { await loadAccountCenter(); } catch (error) { toast(error.message); } }
  if (action === "add-life" || action === "open-calendar") {
    openCalendarComposer(actionEl.dataset.entryType || "event");
  }
  if (action === "open-preferences") {
    navigate("life"); setLifeSection("preference", { scroll: false });
    const form = document.getElementById("preferenceForm"); form.reset(); delete form.dataset.itemId; delete form.dataset.version;
    document.getElementById("preferenceModalTitle").textContent = "记录一条偏好";
    document.getElementById("preferenceSubject").innerHTML = `<option value="${state.data.partner.id}">${escapeHtml(state.data.partner.nickname)}</option><option value="${state.data.user.id}">${escapeHtml(state.data.user.nickname)}</option>`; openModal("preferenceModal");
  }
  if (action === "edit-calendar") {
    const item = (state.data.calendar || []).find(entry => entry.id === actionEl.dataset.id);
    if (item) {
      const form = document.getElementById("calendarForm"); form.dataset.itemId = item.id; form.dataset.version = item.version;
      document.getElementById("calendarModalTitle").textContent = "编辑共同事项";
      document.getElementById("calendarType").value = item.entry_type; document.getElementById("calendarTitle").value = item.title;
      document.getElementById("calendarStart").value = String(item.starts_at).slice(0, 10); document.getElementById("calendarRecurrence").value = item.recurrence;
      document.getElementById("calendarLocation").value = item.location || ""; document.getElementById("calendarNote").value = item.note || ""; openModal("calendarModal");
    }
  }
  if (action === "edit-preference") {
    const item = (state.data.preferences || []).find(entry => entry.id === actionEl.dataset.id);
    if (item) {
      const form = document.getElementById("preferenceForm"); form.dataset.itemId = item.id; form.dataset.version = item.version;
      document.getElementById("preferenceModalTitle").textContent = "编辑偏好";
      document.getElementById("preferenceSubject").innerHTML = `<option value="${state.data.partner.id}">${escapeHtml(state.data.partner.nickname)}</option><option value="${state.data.user.id}">${escapeHtml(state.data.user.nickname)}</option>`;
      document.getElementById("preferenceSubject").value = item.subject_user_id; document.getElementById("preferenceCategory").value = item.category;
      document.getElementById("preferenceLabel").value = item.label; document.getElementById("preferenceValue").value = item.value; document.getElementById("preferenceNote").value = item.note || ""; openModal("preferenceModal");
    }
  }
  if (action === "toggle-calendar") {
    try { await api(`/api/v1/calendar/${actionEl.dataset.id}/complete`, { method: "POST", body: JSON.stringify({ completed: actionEl.dataset.completed !== "1", expected_version: Number(actionEl.dataset.version) }) }); await loadBootstrap({ quiet: true }); toast("待办状态已同步"); } catch (error) { toast(error.status === 409 ? "内容已变化，请刷新后再试" : error.message); }
  }
  if (action === "delete-calendar") {
    if (window.confirm("将这项共同事项移除？")) try { await api(`/api/v1/calendar/${actionEl.dataset.id}`, { method: "DELETE", body: JSON.stringify({ expected_version: Number(actionEl.dataset.version) }) }); await loadBootstrap({ quiet: true }); toast("共同事项已删除"); } catch (error) { toast(error.message); }
  }
  if (action === "delete-preference") {
    if (window.confirm("删除这条偏好备忘？")) try { await api(`/api/v1/preferences/${actionEl.dataset.id}`, { method: "DELETE", body: JSON.stringify({ expected_version: Number(actionEl.dataset.version) }) }); await loadBootstrap({ quiet: true }); toast("偏好已删除"); } catch (error) { toast(error.message); }
  }
  if (action === "trash-moment") {
    if (window.confirm("将这个瞬间移入回收站？")) try { await api(`/api/v1/moments/${actionEl.dataset.id}/trash`, { method: "POST", body: "{}" }); await loadBootstrap({ quiet: true }); await setTimelineView("list", { scroll: false }); toast("瞬间已移入回收站"); } catch (error) { toast(error.message); }
  }
  if (action === "restore-moment") {
    try { await api(`/api/v1/moments/${actionEl.dataset.id}/restore`, { method: "POST", body: JSON.stringify({ expected_version: Number(actionEl.dataset.version) }) }); await loadBootstrap({ quiet: true }); await setTimelineView("trash", { scroll: false }); toast("瞬间已恢复"); } catch (error) { toast(error.message); }
  }
  if (action === "purge-moment") {
    if (window.confirm("永久删除后无法恢复，确定继续？")) try { await api(`/api/v1/moments/${actionEl.dataset.id}`, { method: "DELETE", body: "{}" }); await setTimelineView("trash", { scroll: false }); toast("瞬间已永久清理"); } catch (error) { toast(error.message); }
  }
  if (action === "confirm-import" && state.chatImport) {
    try {
      for (const item of state.chatImport.items) { const content = document.querySelector(`[data-import-content="${item.id}"]`).value; const speaker = document.querySelector(`[data-import-speaker="${item.id}"]`).value; const selected = document.querySelector(`[data-import-selected="${item.id}"]`).checked; await api(`/api/v1/chat-imports/${state.chatImport.id}/items/${item.id}`, { method: "PATCH", body: JSON.stringify({ content, speaker, selected, expected_version: item.version }) }); }
      await api(`/api/v1/chat-imports/${state.chatImport.id}/confirm`, { method: "POST", body: JSON.stringify({ as_moments: false }) }); state.chatImport = null; closeModals(); await loadBootstrap({ quiet: true }); toast("已确认的聊天已导入");
    } catch (error) { toast(error.message); }
  }
  if (action === "cancel-import" && state.chatImport) {
    try { await api(`/api/v1/chat-imports/${state.chatImport.id}/cancel`, { method: "POST", body: "{}" }); } catch (_) {} state.chatImport = null; closeModals(); toast("导入草稿已取消");
  }
  if (action === "revoke-device") {
    try { await api(`/api/v1/account/devices/${actionEl.dataset.id}/revoke`, { method: "POST", body: "{}" }); await loadAccountCenter(); toast("该设备已撤销"); } catch (error) { toast(error.message); }
  }
  if (action === "trash-media") {
    if (window.confirm("把这个未引用的媒体移入回收站？")) try { await api(`/api/v1/media/${actionEl.dataset.id}/trash`, { method: "POST", body: "{}" }); await loadAccountCenter(); toast("媒体已移入回收站"); } catch (error) { toast(error.message); }
  }
  if (action === "restore-media") {
    try { await api(`/api/v1/media/${actionEl.dataset.id}/restore`, { method: "POST", body: JSON.stringify({ expected_version: Number(actionEl.dataset.version) }) }); await loadAccountCenter(); toast("媒体已恢复"); } catch (error) { toast(error.status === 409 ? "媒体状态已变化，请刷新后再试" : error.message); }
  }
  if (action === "purge-media") {
    if (window.confirm("永久删除原图和缩略图后无法恢复，确定继续？")) try { await api(`/api/v1/media/${actionEl.dataset.id}`, { method: "DELETE", body: "{}" }); await loadAccountCenter(); toast("媒体已永久删除"); } catch (error) { toast(error.message); }
  }
  if (action === "export-account") { window.location.assign("/api/v1/account/export.zip"); toast("正在准备完整数据导出"); }
  if (action === "unlink-couple") {
    const password = window.prompt("输入当前密码"); const confirmation = password && window.prompt("请输入：解除情侣");
    if (password && confirmation) try { await api("/api/v1/couple/unlink", { method: "POST", body: JSON.stringify({ password, confirmation }) }); stopRealtime(); resetTripLive(); state.data = null; closeModals(); showPairing(); toast("情侣关系已解除"); } catch (error) { toast(error.message); }
  }
  if (action === "delete-account") {
    const password = window.prompt("注销前建议先导出数据。请输入当前密码"); const confirmation = password && window.prompt("请输入：注销账号");
    if (password && confirmation) try { await api("/api/v1/account/delete", { method: "POST", body: JSON.stringify({ password, confirmation }) }); stopRealtime(); resetTripLive(); state.data = null; closeModals(); showAccountAuth(); toast("账号已注销"); } catch (error) { toast(error.message); }
  }
  if (action === "vote-date") {
    try {
      const updated = await api(`/api/v1/date-plans/${actionEl.dataset.dateId}/vote`, { method: "PUT", body: JSON.stringify({ variant_id: actionEl.dataset.variantId }) });
      const index = state.data.datePlans.findIndex(item => item.id === updated.id); state.data.datePlans[index] = updated; renderDatePlans(); toast(updated.both_voted ? "双方投票已揭晓" : "已秘密投票，等待 TA");
    } catch (error) { toast(error.message); }
  }
  if (action === "compose-date") {
    try {
      const updated = await api(`/api/v1/date-plans/${actionEl.dataset.dateId}/compose`, { method: "POST", body: JSON.stringify({ variant_id: actionEl.dataset.variantId }) });
      const index = state.data.datePlans.findIndex(item => item.id === updated.id); state.data.datePlans[index] = updated; renderDatePlans(); toast("最终约会已保存，等待双方确认");
    } catch (error) { toast(error.message); }
  }
  if (action === "confirm-date") {
    try {
      const updated = await api(`/api/v1/date-plans/${actionEl.dataset.dateId}/confirm`, { method: "POST", body: JSON.stringify({ version: Number(actionEl.dataset.version) }) });
      const index = state.data.datePlans.findIndex(item => item.id === updated.id); state.data.datePlans[index] = updated; renderDatePlans(); toast(updated.status === "confirmed" ? "约会已由双方确认" : "你已确认，等待 TA");
    } catch (error) { toast(error.status === 409 ? "方案已变化，请重新确认" : error.message); }
  }
  if (action === "complete-date") {
    const note = window.prompt("为这次约会写一句回忆", "这次约会，值得好好记住。") || "这次约会，值得好好记住。";
    const actualCost = window.prompt("实际花费（元，可留空）", "") || "0";
    try {
      const updated = await api(`/api/v1/date-plans/${actionEl.dataset.dateId}/complete`, { method: "POST", body: JSON.stringify({ note, actual_cost: actualCost }) });
      const index = state.data.datePlans.findIndex(item => item.id === updated.id); state.data.datePlans[index] = updated; await loadBootstrap({ quiet: true }); toast("约会已经成为共同回忆");
    } catch (error) { toast(error.message); }
  }
  if (action === "confirm-smart-action" && state.smartActionId) {
    actionEl.disabled = true;
    try {
      // Confirming an action refreshes several shared panels. On a slower
      // connection the user may deliberately move to another page while that
      // refresh is still running. Remember the page where confirmation began
      // so this older operation cannot unexpectedly pull them back later.
      const confirmationSurface = state.screen;
      const key = `web:${state.smartActionId}:${Date.now()}`;
      const actionType = state.smartActionType;
      const completedActionId = state.smartActionId;
      const confirmed = await api(`/api/v1/ui/actions/${state.smartActionId}/confirm`, { method: "POST", body: JSON.stringify({ idempotencyKey: key }) });
      removeSmartActionFromManifests(completedActionId); state.smartActionId = null; state.smartActionType = null; closeModals(); await loadBootstrap({ quiet: true });
      const mayAutoNavigate = state.screen === confirmationSurface;
      if (actionType === "create_calendar_entry") {
        if (mayAutoNavigate) { navigate("life"); setLifeSection("anniversary"); }
        const id = confirmed.result?.id;
        if (mayAutoNavigate) requestAnimationFrame(() => { const record = id && document.querySelector(`[data-record-id="${CSS.escape(id)}"]`); record?.classList.add("result-highlight"); record?.scrollIntoView({ behavior: "smooth", block: "center" }); });
        toast(mayAutoNavigate ? "纪念日已保存，可在共同生活中查看和编辑" : "纪念日已保存；你可以稍后在共同生活中查看");
      } else if (actionType === "create_moment") { if (mayAutoNavigate) { navigate("timeline"); await setTimelineView("list"); } toast(mayAutoNavigate ? "瞬间已保存并打开时光轴" : "瞬间已保存到时光轴"); }
      else if (actionType === "create_wishlist_item") { if (mayAutoNavigate) { navigate("life"); setLifeSection("wishlist"); } toast(mayAutoNavigate ? "愿望已保存并打开共同愿望清单" : "愿望已保存到共同清单"); }
      else if (actionType === "send_message") { if (mayAutoNavigate) { navigate("chat"); scrollToContent("#messages"); } toast("消息已发送并可在聊天中查看"); }
      else if (actionType === "select_trip_variant") { if (mayAutoNavigate) { navigate("trip"); setTripSection("plans"); } toast(mayAutoNavigate ? "旅行方案已切换并打开" : "旅行方案已切换"); }
      else toast("建议已确认并执行");
    } catch (error) { toast(error.status === 409 ? "内容已被另一方更新，请重新预览" : error.message); }
    finally { actionEl.disabled = false; }
  }
  if (action === "cancel-smart-action" && state.smartActionId) {
    const cancelledActionId = state.smartActionId;
    try { await api(`/api/v1/ui/actions/${state.smartActionId}/cancel`, { method: "POST", body: "{}" }); } catch (_) {}
    removeSmartActionFromManifests(cancelledActionId); state.smartActionId = null; state.smartActionType = null; closeModals(); toast("已放弃这个建议");
  }
  if (action === "create-invite") {
    try {
      const invite = await api("/api/v1/couples/invites", { method: "POST", body: "{}" });
      const code = document.getElementById("inviteCode"); code.textContent = invite.code; code.hidden = false;
      toast("邀请码 24 小时内有效，只能使用一次");
    } catch (error) { toast(error.message); }
  }
  if (action === "logout") {
    if (state.callId) await endCall();
    const userId = state.data?.user?.id;
    try { await api("/api/v1/auth/logout", { method: "POST", body: "{}" }); } catch (_) {}
    await clearMealOfflineData(userId);
    stopRealtime(); resetTripLive(); state.data = null; state.selectedItemIds.clear(); state.pushSynced = false; state.pushSubscribed = false; localStorage.removeItem("love-offline-trip");
    Object.keys(smartExamples).forEach(surface => localStorage.removeItem(`love-ui-${surface}`));
    broadcastAuthChange("logout");
    showAccountAuth(); toast("已安全退出");
  }
});

document.addEventListener("change", async event => {
  if (event.target.id === "recipeDetailServings") {
    const recipe = state.meals.openRecipe;
    const servings = Math.min(20, Math.max(1, Number(event.target.value) || Number(recipe?.base_servings) || 1));
    event.target.value = String(servings);
    const host = document.getElementById("recipeDetailIngredients");
    if (recipe && host) host.innerHTML = renderRecipeIngredients(recipe, servings);
    return;
  }
  if (event.target.id === "tripSwitcher") {
    const selected = (state.data?.trips || []).find(item => item.id === event.target.value);
    if (selected) {
      state.data.trip = selected; state.plan = selected.active_variant || "slow"; state.selectedItemIds.clear(); localStorage.setItem("love-trip-id", selected.id);
      state.manifests.trip = null; state.pinnedSmartSurfaces.delete("trip"); state.smartIntentDrafts.trip = "";
      state.tripAssistant = { tripId: selected.id, items: [], loading: true };
      resetTripLive(selected.id);
      renderIdentity(); renderPlan(); renderHotel(); await loadSmartSurface("trip", { quiet: true });
      if (state.tripSection === "assistant") await loadTripAssistant({ quiet: true });
      toast(`已打开：${selected.title}`);
    }
    return;
  }
  if (event.target.id === "chatMediaInput") {
    const file = event.target.files?.[0];
    if (!file) return;
    event.target.value = "";
    try { await sendMediaMessage(file); } catch (error) { toast(error.message); }
    return;
  }
  const shoppingCheck = event.target.closest("[data-shopping-check]");
  if (shoppingCheck) {
    try { await api(`/api/v1/shopping-lists/items/${encodeURIComponent(shoppingCheck.dataset.shoppingCheck)}`, { method: "PATCH", body: JSON.stringify({ checked: shoppingCheck.checked, expected_version: Number(shoppingCheck.dataset.version) }) }); await loadMeals({ quiet: true }); }
    catch (error) {
      if (!navigator.onLine || !error.status) {
        const item = state.meals.shopping?.items?.find(value => value.id === shoppingCheck.dataset.shoppingCheck); if (item) item.checked = shoppingCheck.checked;
        await queueMealMutation(`/api/v1/shopping-lists/items/${encodeURIComponent(shoppingCheck.dataset.shoppingCheck)}`, "PATCH", { checked: shoppingCheck.checked, expected_version: Number(shoppingCheck.dataset.version) }); renderShoppingList(); toast("已离线保存，恢复网络后自动同步");
      } else { shoppingCheck.checked = !shoppingCheck.checked; toast(error.message); }
    }
    return;
  }
  const checkbox = event.target.closest("[data-wish-id]");
  if (!checkbox) return;
  try {
    const updated = await api(`/api/v1/lists/items/${checkbox.dataset.wishId}`, { method: "PATCH", body: JSON.stringify({ completed: checkbox.checked }) });
    const index = state.data.wishlist.items.findIndex(item => item.id === updated.id);
    state.data.wishlist.items[index] = updated; renderWishlist(); toast(checkbox.checked ? "心愿已完成并记录" : "已恢复为待完成");
  } catch (error) { checkbox.checked = !checkbox.checked; toast(error.message); }
});

function clearMessageLongPress() {
  clearTimeout(state.longPressTimer);
  state.longPressTimer = null;
  state.longPressStart = null;
}

document.addEventListener("pointerdown", event => {
  const bubble = event.target.closest(".message[data-message-id]");
  if (!bubble || event.target.closest("button,audio,video,a")) return;
  clearMessageLongPress();
  state.longPressStart = { pointerId: event.pointerId, x: event.clientX, y: event.clientY };
  state.longPressTimer = setTimeout(() => {
    state.longPressTimer = null;
    saveMessageMoment(bubble.dataset.messageId);
  }, 650);
});
document.addEventListener("pointermove", event => {
  const start = state.longPressStart;
  if (!start || start.pointerId !== event.pointerId) return;
  if (Math.hypot(event.clientX - start.x, event.clientY - start.y) > 10) clearMessageLongPress();
});
["pointerup", "pointercancel"].forEach(name => document.addEventListener(name, clearMessageLongPress));
document.addEventListener("contextmenu", event => {
  if (event.target.closest(".message[data-message-id]") && !event.target.closest("button,audio,video,a")) event.preventDefault();
});

let softKeyboardBaseline = Math.max(window.innerHeight, window.visualViewport?.height || 0);

function isTextEntry(element) {
  return Boolean(element?.matches?.('input, textarea, select, [contenteditable="true"]'));
}

function syncSoftKeyboardLayout() {
  const viewportHeight = window.visualViewport?.height || window.innerHeight;
  const hasFocusedEntry = isTextEntry(document.activeElement);
  if (!hasFocusedEntry) softKeyboardBaseline = Math.max(window.innerHeight, viewportHeight);
  const keyboardOpen = hasFocusedEntry && softKeyboardBaseline - viewportHeight > 110;
  document.documentElement.classList.toggle("soft-keyboard-open", keyboardOpen);
}

window.visualViewport?.addEventListener("resize", syncSoftKeyboardLayout);
window.addEventListener("resize", syncSoftKeyboardLayout);
document.addEventListener("focusin", () => {
  requestAnimationFrame(syncSoftKeyboardLayout);
  setTimeout(syncSoftKeyboardLayout, 250);
});
document.addEventListener("focusout", () => setTimeout(syncSoftKeyboardLayout, 0));
window.addEventListener("orientationchange", () => {
  document.documentElement.classList.remove("soft-keyboard-open");
  setTimeout(() => {
    softKeyboardBaseline = Math.max(window.innerHeight, window.visualViewport?.height || 0);
    syncSoftKeyboardLayout();
  }, 300);
});

document.addEventListener("input", event => {
  if (event.target.matches?.("[data-food-area-day]")) {
    localStorage.setItem(`love-food-area-${state.data?.trip?.id}-${event.target.dataset.foodAreaDay}`, event.target.value.trim());
  }
  const form = event.target.closest?.("[data-smart-form]");
  if (form && event.target.matches("input")) state.smartIntentDrafts[form.dataset.smartForm] = event.target.value;
});

document.addEventListener("keydown", event => {
  if (!['Enter', ' '].includes(event.key)) return;
  const interactiveCard = event.target.closest('[data-smart-node], .context-person[role="button"]');
  if (!interactiveCard || event.target.closest("button,input,select,textarea,a")) return;
  event.preventDefault(); interactiveCard.click();
});

document.addEventListener("submit", async event => {
  const form = event.target.closest("[data-smart-form]");
  if (!form) return;
  event.preventDefault();
  const input = form.querySelector("input");
  const intent = input.value.trim() || smartExamples[form.dataset.smartForm];
  await composeSmartSurface(form.dataset.smartForm, intent, form.querySelector("button"));
});

document.getElementById("lifeAssistantForm").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.getElementById("lifeAssistantInput");
  const button = event.submitter || event.currentTarget.querySelector("button");
  const intent = input.value.trim();
  if (!intent) return;
  button.disabled = true;
  button.textContent = "正在整理…";
  try {
    const job = await api("/api/v1/assistant/jobs", {
      method: "POST",
      body: JSON.stringify({ intent, client_request_id: clientKey("assistant-job") })
    });
    let completed = job;
    if (job.status !== "completed") completed = await api("/api/v1/assistant/jobs/" + encodeURIComponent(job.id));
    if (!completed.plan) throw new Error(completed.error_message || "方案仍在整理，请稍后查看方案记录");
    input.value = "";
    await loadLifeAssistant({ quiet: true });
    await openLifeAssistantPlan(completed.plan.id);
    toast("方案已生成，两个人现在都能查看");
  } catch (error) { toast(error.message); }
  finally {
    button.disabled = false;
    button.textContent = "预览方案";
  }
});

document.getElementById("tripAssistantForm").addEventListener("submit", async event => {
  event.preventDefault();
  const trip = state.data?.trip;
  const input = document.getElementById("tripAssistantInput");
  const button = document.getElementById("tripAssistantSend");
  const message = input.value.trim();
  if (!trip) { toast("请先创建并选择一段旅行"); return; }
  if (!message) return;
  button.disabled = true; button.textContent = "思考中…";
  try {
    const key = `assistant-message-${crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`}`;
    const liveContext = state.tripLive.tripId === trip.id
      && state.tripLive.ownerUserId === state.data?.user?.id
      && liveContextIsFresh(state.tripLive.context) ? state.tripLive.context : null;
    const result = await api(`/api/v1/trips/${encodeURIComponent(trip.id)}/assistant/messages`, { method: "POST", body: JSON.stringify({ message, idempotency_key: key, ...(liveContext ? { live_context: liveContext } : {}) }) });
    input.value = "";
    state.tripAssistantLoadSequence += 1;
    const currentItems = state.tripAssistant.tripId === trip.id ? (state.tripAssistant.items || []) : [];
    state.tripAssistant = { tripId: trip.id, loading: false, items: [...currentItems, { id: `local-${key}`, role: "user", content: message, author_name: state.data?.user?.nickname, cards: [] }, result.assistant] };
    renderTripAssistant();
    await loadTripAssistant({ quiet: true });
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "发送"; }
});

document.getElementById("sharedCommentForm").addEventListener("submit", async event => {
  event.preventDefault();
  const detail = state.sharedDetail;
  const input = document.getElementById("sharedCommentBody");
  const body = input.value.trim();
  if (!detail || !body) return;
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try {
    await api(`/api/v1/shared-content/${encodeURIComponent(detail.resource_type)}/${encodeURIComponent(detail.resource_id)}/comments`, { method: "POST", body: JSON.stringify({ body }) });
    input.value = "";
    await openSharedDetail(detail.resource_type, detail.resource_id, { refresh: true });
    toast("评论已同步给对方");
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});

document.getElementById("chatForm").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.getElementById("chatInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  try { await sendMessage(text); toast("消息已保存到数据库"); } catch (error) { input.value = text; toast(error.message); }
});

document.getElementById("authForm").addEventListener("submit", async event => {
  event.preventDefault();
  const payload = {
    username: document.getElementById("authUsername").value,
    password: document.getElementById("authPassword").value,
    nickname: document.getElementById("authNickname").value
  };
  try {
    const endpoint = state.authMode === "register" ? "/api/v1/auth/register" : "/api/v1/auth/login";
    const result = await api(endpoint, { method: "POST", body: JSON.stringify(payload) });
    resetTripLive();
    state.pushSynced = false;
    document.getElementById("authPassword").value = "";
    if (!result.pairing?.paired) showPairing(); else await loadBootstrap({ quiet: true });
    broadcastAuthChange(state.authMode, result.user?.username || payload.username);
    toast(state.authMode === "register" ? "账号创建成功" : "登录成功");
  } catch (error) { toast(error.message); }
});

document.getElementById("changePasswordForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  const currentPassword = document.getElementById("currentPassword").value;
  const newPassword = document.getElementById("newPassword").value;
  const confirmation = document.getElementById("confirmNewPassword").value;
  if (newPassword !== confirmation) { toast("两次输入的新密码不一致"); return; }
  button.disabled = true;
  try {
    const result = await api("/api/v1/account/password", { method: "POST", body: JSON.stringify({ current_password: currentPassword, new_password: newPassword, new_password_confirmation: confirmation }) });
    event.target.reset();
    await loadAccountCenter();
    toast(`密码已更换，已退出其他 ${result.revoked_sessions || 0} 个会话`);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});

document.getElementById("answerForm").addEventListener("submit", async event => {
  event.preventDefault();
  const answer = document.getElementById("dailyAnswer").value.trim();
  if (!answer) return;
  const submit = document.getElementById("answerSubmit");
  submit.disabled = true;
  try {
    state.data.dailyQuestion = await api("/api/v1/daily-question/answer", { method: "POST", body: JSON.stringify({ answer }) });
    closeModals(); renderDailyQuestion(); toast(state.data.dailyQuestion.partner_answered ? "双方答案已解锁" : "答案已保存，等待对方");
  } catch (error) { toast(error.message); }
  finally { submit.disabled = false; }
});

document.getElementById("pairForm").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await api("/api/v1/couples/invites/accept", { method: "POST", body: JSON.stringify({
      code: document.getElementById("pairCode").value,
      anniversary: document.getElementById("pairAnniversary").value,
    }) });
    await loadBootstrap({ quiet: true }); toast("绑定成功，欢迎来到你们的共同小岛");
  } catch (error) { toast(error.message); }
});

document.getElementById("statusForm").addEventListener("submit", async event => {
  event.preventDefault();
  const presets = { miss_you: "想你了", busy: "忙碌中", moving: "在路上", home: "已到家", happy: "心情不错", quiet: "想安静一下", custom: "分享此刻" };
  const statusCode = document.getElementById("statusCode").value;
  const hours = Number(document.getElementById("statusDuration").value);
  const expiresAt = hours ? new Date(Date.now() + hours * 3600000).toISOString() : null;
  const text = document.getElementById("statusText").value.trim() || presets[statusCode];
  try {
    await api("/api/v1/me/status", { method: "PUT", body: JSON.stringify({ status_code: statusCode, text, expires_at: expiresAt }) });
    closeModals(); await loadBootstrap({ quiet: true }); toast("你的状态已经同步给 TA");
  } catch (error) { toast(error.message); }
});

document.getElementById("locationForm").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await api("/api/v1/me/location", { method: "PUT", body: JSON.stringify({ city: document.getElementById("locationCity").value.trim(), timezone: document.getElementById("locationTimezone").value.trim() }) });
    closeModals(); await loadBootstrap({ quiet: true }); toast("城市和时区已保存");
  } catch (error) { toast(error.message); }
});

document.getElementById("weeklyForm").addEventListener("submit", async event => {
  event.preventDefault();
  const review = state.data?.weeklyReview;
  if (!review) return;
  try {
    state.data.weeklyReview = await api(`/api/v1/weekly-reviews/${review.id}/answer`, { method: "PUT", body: JSON.stringify({ favorite: document.getElementById("weeklyFavorite").value, gratitude: document.getElementById("weeklyGratitude").value, next_week: document.getElementById("weeklyNext").value }) });
    renderWeeklyReview(); toast(state.data.weeklyReview.both_revealed ? "双方答案已经揭晓" : "已保存，等待 TA 写完");
  } catch (error) { toast(error.message); }
});

document.getElementById("dateForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true; button.textContent = "生成中…";
  try {
    const plan = await api("/api/v1/date-plans", { method: "POST", body: JSON.stringify({ title: document.getElementById("dateTitle").value, plan_date: document.getElementById("dateDay").value, city: document.getElementById("dateCity").value, start_time: document.getElementById("dateStart").value, end_time: document.getElementById("dateEnd").value, budget: document.getElementById("dateBudget").value, preferences: { interest: document.getElementById("datePreference").value } }) });
    state.data.datePlans.unshift(plan); closeModals(); renderDatePlans(); navigate("life"); setLifeSection("dates"); toast("五套约会方案已生成，可以各自投票了");
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "生成五套约会方案"; }
});

document.getElementById("tripForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter;
  button.disabled = true; button.textContent = "生成中…";
  try {
    const editingTrip = form.dataset.tripId ? state.data?.trip : null;
    if (!state.destinationCandidateId && !state.destinationExistingPlace) {
      throw new Error("请先点击“查找并确认具体地点”，核对城市和完整地址后再生成方案");
    }
    const startDate = document.getElementById("tripStart").value; const endDate = document.getElementById("tripEnd").value;
    const outboundClock = document.getElementById("tripOutboundTime").value; const returnClock = document.getElementById("tripReturnTime").value;
    const payload = {
      destination: document.getElementById("tripDestination").value,
      departure_city: document.getElementById("tripDepartureCity").value,
      arrival_city: document.getElementById("tripArrivalCity").value,
      destination_candidate_id: state.destinationCandidateId,
      start_date: startDate, end_date: endDate,
      outbound_time: outboundClock ? `${startDate}T${outboundClock}:00` : null,
      return_time: returnClock ? `${endDate}T${returnClock}:00` : null,
      travelers: document.getElementById("tripTravelers").value,
      rooms: document.getElementById("tripRooms").value,
      hotel_budget: document.getElementById("tripHotelBudget").value,
      budget: document.getElementById("tripBudget").value,
      preferences: { pace: document.getElementById("tripPace").value, transport: document.getElementById("tripTransport").value, interests: document.getElementById("tripInterests").value, diet: document.getElementById("tripDiet").value, hotel: document.getElementById("tripHotel").value, must_visits: document.getElementById("tripMustVisits").value }
    };
    let trip;
    if (editingTrip) {
      trip = await api(`/api/v1/trips/${encodeURIComponent(editingTrip.id)}`, { method: "PATCH", body: JSON.stringify({ ...payload, expected_version: editingTrip.version }) });
      if (state.destinationCandidateId) {
        trip = await api(`/api/v1/trips/${encodeURIComponent(editingTrip.id)}/generate`, { method: "POST", body: JSON.stringify({ preserve_manual: false }) });
      }
    } else {
      button.textContent = "已提交 · 后台规划中…";
      trip = await createTripInBackground(payload);
    }
    state.data.trip = trip; state.data.trips = [trip, ...(state.data.trips || []).filter(item => item.id !== trip.id)]; state.plan = "slow"; state.selectedItemIds.clear(); localStorage.setItem("love-trip-id", trip.id); closeModals(); renderIdentity(); renderPlan(); renderHotel(); navigate("trip"); setTripSection("plans");
    toast(editingTrip ? "具体地点已确认，旧地理范围已清除并重新生成五套方案" : (trip.date_conflicts?.length ? `已创建，但与“${trip.date_conflicts.map(item => item.title).join("、")}”日期重叠，请检查` : "新旅行与五套方案已创建"));
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = form.dataset.tripId ? "保存地点并重新生成五套方案" : "生成五套旅行方案"; }
});

for (const id of ["tripDestination", "tripDestinationRegion", "tripArrivalCity"]) {
  document.getElementById(id).addEventListener("input", () => {
    clearDestinationResolution({ cancelSearch: true });
    if (id !== "tripArrivalCity") scheduleDestinationSuggestions();
  });
}

document.getElementById("tripItemEditForm").addEventListener("submit", async event => {
  event.preventDefault();
  const item = currentEditingTripItem();
  if (!item) { toast("这条行程已变化，请刷新后重试"); return; }
  const changes = {
    title: document.getElementById("editItemTitle").value.trim(),
    start_time: document.getElementById("editItemTime").value,
    note: document.getElementById("editItemNote").value.trim(),
  };
  const changed = Object.fromEntries(Object.entries(changes).filter(([key, value]) => String(value) !== String(item[key] || "")));
  if (!Object.keys(changed).length) { toast("内容没有变化"); return; }
  const button = event.submitter; button.disabled = true; button.textContent = "检查中…";
  try {
    const analysis = await api(`/api/v1/trips/items/${state.editingItemId}/impact`, { method: "PATCH", body: JSON.stringify({ changes: changed }) });
    state.pendingRevision = changed;
    document.getElementById("revisionAfter").textContent = `${changes.start_time} · ${changes.title}`;
    const diffs = (analysis.changes || []).map(change => `${change.field}: ${change.before ?? "无"} → ${change.after ?? "无"}`);
    document.getElementById("revisionWarnings").textContent = [...diffs, ...(analysis.warnings || [])].join("；") || "只更新当前项目";
    document.getElementById("revisionPreview").hidden = false;
    const applyButton = document.getElementById("applyRevisionButton"); applyButton.hidden = false; applyButton.disabled = false;
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "检查这次改动的影响"; }
});

document.getElementById("chatImportForm").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true; button.textContent = "识别中…";
  try {
    const file = document.getElementById("chatImportImage").files?.[0]; let mediaId = null;
    if (file) mediaId = (await uploadMedia(file, "private")).id;
    const draft = await api("/api/v1/chat-imports", { method: "POST", body: JSON.stringify({ media_id: mediaId, text: document.getElementById("chatImportText").value, base_date: new Date().toISOString().slice(0,10) }) });
    renderChatImportDraft(draft);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "生成可编辑草稿"; }
});

document.getElementById("calendarForm").addEventListener("submit", async event => {
  event.preventDefault();
  const originalScreen = state.screen; const originalSection = state.lifeSection;
  try {
    const form = event.target; const editing = form.dataset.itemId;
    const payload = { entry_type: document.getElementById("calendarType").value, title: document.getElementById("calendarTitle").value, starts_at: document.getElementById("calendarStart").value, all_day: true, recurrence: document.getElementById("calendarRecurrence").value, location: document.getElementById("calendarLocation").value, note: document.getElementById("calendarNote").value };
    if (editing) payload.expected_version = Number(form.dataset.version);
    const saved = await api(editing ? `/api/v1/calendar/${editing}` : "/api/v1/calendar", { method: editing ? "PATCH" : "POST", body: JSON.stringify(payload) });
    event.target.reset(); closeModals(); await loadBootstrap({ quiet: true });
    if (state.screen === originalScreen && state.lifeSection === originalSection) {
      navigate("life"); setLifeSection({ event: "calendar", anniversary: "anniversary", todo: "todo" }[saved.entry_type] || "calendar");
      requestAnimationFrame(() => { const record = document.querySelector(`[data-record-id="${CSS.escape(saved.id)}"]`); record?.classList.add("result-highlight"); record?.scrollIntoView({ behavior: "smooth", block: "center" }); });
    }
    toast(`${saved.entry_type === "anniversary" ? "纪念日" : "共同事项"}已保存并显示在列表中`);
  } catch (error) { toast(error.message); }
});

document.getElementById("wishlistForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.target; const editing = form.dataset.itemId;
  const originalScreen = state.screen; const originalSection = state.lifeSection;
  const payload = { text: document.getElementById("wishlistText").value.trim(), note: document.getElementById("wishlistNote").value.trim() };
  if (editing) payload.expected_version = Number(form.dataset.version);
  try {
    await api(editing ? `/api/v1/lists/items/${editing}` : "/api/v1/lists/wishlist/items", { method: editing ? "PATCH" : "POST", body: JSON.stringify(payload) });
    form.reset(); closeModals(); await loadBootstrap({ quiet: true });
    if (state.screen === originalScreen && state.lifeSection === originalSection) { navigate("life"); setLifeSection("wishlist"); }
    toast(editing ? "共同愿望已更新" : "共同愿望已保存，双方都能看到");
  } catch (error) { toast(error.status === 409 ? "愿望已被另一方更新，请刷新后重试" : error.message); }
});

document.getElementById("preferenceForm").addEventListener("submit", async event => {
  event.preventDefault();
  const originalScreen = state.screen; const originalSection = state.lifeSection;
  try {
    const form = event.target; const editing = form.dataset.itemId;
    const payload = { subject_user_id: document.getElementById("preferenceSubject").value, category: document.getElementById("preferenceCategory").value, label: document.getElementById("preferenceLabel").value, value: document.getElementById("preferenceValue").value, note: document.getElementById("preferenceNote").value };
    if (editing) payload.expected_version = Number(form.dataset.version);
    await api(editing ? `/api/v1/preferences/${editing}` : "/api/v1/preferences", { method: editing ? "PATCH" : "POST", body: JSON.stringify(payload) });
    event.target.reset(); closeModals(); await loadBootstrap({ quiet: true });
    if (state.screen === originalScreen && state.lifeSection === originalSection) { navigate("life"); setLifeSection("preference"); }
    toast("偏好备忘已保存");
  } catch (error) { toast(error.message); }
});

document.getElementById("recipeSearchForm").addEventListener("submit", async event => {
  event.preventDefault(); await loadMeals({ quiet: false });
});

document.getElementById("mealWishRecipeSearchForm").addEventListener("submit", async event => {
  event.preventDefault();
  await searchRecipesForWish(document.getElementById("mealWishRecipeSearch").value);
});

document.getElementById("mealWishForm").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    await api("/api/v1/meal-wishes", { method: "POST", body: JSON.stringify({ recipe_id: document.getElementById("mealWishRecipeId").value || null, title: document.getElementById("mealWishName").value.trim(), note: document.getElementById("mealWishNote").value.trim() }) });
    event.target.reset(); closeModals(); setMealSection("wishes", { load: false }); await loadMeals({ quiet: true }); toast("已加入两个人的想吃清单");
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
});

document.getElementById("mealScheduleForm").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  const editing = event.target.dataset.itemId;
  const payload = { planned_date: document.getElementById("mealScheduleDate").value, meal_slot: document.getElementById("mealScheduleSlot").value, planned_time: document.getElementById("mealScheduleTime").value || null, servings: Number(document.getElementById("mealScheduleServings").value), course_type: document.getElementById("mealScheduleCourse").value, note: document.getElementById("mealScheduleNote").value.trim() };
  if (editing) payload.expected_version = Number(event.target.dataset.version);
  else Object.assign(payload, { recipe_id: document.getElementById("mealScheduleRecipeId").value, wish_id: document.getElementById("mealScheduleWishId").value || null, idempotency_key: clientKey("meal") });
  try {
    if (editing) {
      const before = state.meals.editingMeal;
      const changes = [
        before?.planned_date !== payload.planned_date ? `日期：${before?.planned_date} → ${payload.planned_date}` : "",
        before?.meal_slot !== payload.meal_slot ? `餐次：${mealSlotLabels[before?.meal_slot] || before?.meal_slot} → ${mealSlotLabels[payload.meal_slot]}` : "",
        String(before?.planned_time || "") !== String(payload.planned_time || "") ? `时间：${before?.planned_time || "未设置"} → ${payload.planned_time || "未设置"}` : "",
        Number(before?.servings) !== payload.servings ? `人数：${before?.servings} → ${payload.servings}` : ""
      ].filter(Boolean);
      if (changes.length && !window.confirm(`确认修改用餐安排？\n\n${changes.join("\n")}\n\n采购清单如已生成，需要重新预览后才会更新。`)) return;
    }
    await api(editing ? `/api/v1/meal-cart/items/${encodeURIComponent(editing)}` : "/api/v1/meal-cart/items", { method: editing ? "PATCH" : "POST", body: JSON.stringify(payload) });
    state.mealSelectedDate = payload.planned_date; state.mealMonth = new Date(`${payload.planned_date}T00:00:00`); closeModals(); setMealSection("calendar", { load: false }); await loadMeals({ quiet: true }); toast(editing ? "用餐安排已修改" : "已加入点菜车和用餐日历；双方确认后可生成采购清单");
  } catch (error) { toast(error.status === 409 ? `${error.message}，可先在日历中调整已有安排` : error.message); } finally { button.disabled = false; }
});

document.getElementById("customRecipeForm").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const editing = event.target.dataset.recipeId;
    const ingredients = document.getElementById("customRecipeIngredients").value.split(/\n+/).map(line => line.trim()).filter(Boolean).map(line => { const [name, quantity, unit, preparation] = line.split("|").map(value => value.trim()); return { name, quantity: quantity || null, unit: unit || "适量", quantity_text: quantity ? "" : (unit || "适量"), preparation: preparation || "" }; });
    const steps = document.getElementById("customRecipeSteps").value.split(/\n+/).map(line => line.trim()).filter(Boolean).map((line, index) => {
      const parts = line.split("|").map(value => value.trim());
      if (parts.length === 1) return { instruction: line };
      const [title, instruction, heatLevel, minutes, keyPoint] = parts;
      const duration = Number(minutes);
      return {
        title: title || `第 ${index + 1} 步`, instruction,
        heat_level: heatLevel || "按步骤调整",
        timer_seconds: minutes && Number.isFinite(duration) ? Math.round(duration * 60) : null,
        key_point: keyPoint || "达到步骤描述的状态后再继续。"
      };
    });
    const minutes = Number(document.getElementById("customRecipeMinutes").value || 30);
    const visibility = document.getElementById("customRecipeVisibility").value;
    const coverFile = document.getElementById("customRecipeCover").files?.[0];
    const currentRecipe = editing ? state.meals.openRecipe : null;
    if ((!editing || !exactRecipeCover(currentRecipe)) && !coverFile) throw new Error("每份菜谱都需要一张与当前做法准确对应的成品图");
    if (coverFile && !document.getElementById("customRecipeCoverExact").checked) throw new Error("请确认成品图与当前菜品及做法版本一致");
    let uploadedCover = null;
    if (coverFile) uploadedCover = await uploadMedia(coverFile, visibility === "private" ? "private" : "shared");
    const payload = {
      name: document.getElementById("customRecipeName").value.trim(),
      dish_name: document.getElementById("customRecipeDishName").value.trim(),
      variant_name: document.getElementById("customRecipeVariantName").value.trim(),
      cooking_method: document.getElementById("customRecipeCookingMethod").value.trim(),
      method_tags: document.getElementById("customRecipeMethodTags").value.split(/[、,，]+/).map(value => value.trim()).filter(Boolean),
      cuisine: document.getElementById("customRecipeCuisine").value, category: document.getElementById("customRecipeCategory").value.trim(), meal_slots: ["lunch", "dinner"], base_servings: Number(document.getElementById("customRecipeServings").value), prep_minutes: Math.min(15, minutes), cook_minutes: Math.max(0, minutes - Math.min(15, minutes)), passive_minutes: Number(document.getElementById("customRecipePassiveMinutes").value || 0), difficulty: "easy", ingredients, steps, visibility
    };
    if (uploadedCover && !editing) payload.media = [{ media_type: "image", role: "cover", url: uploadedCover.url, thumbnail_url: uploadedCover.thumbnail_url, provider: "local", title: `${payload.name}成品图`, attribution: "用户上传并人工核对", match_status: "verified" }];
    if (!editing) Object.assign(payload, { tips: [], allergen_tags: [] });
    else {
      payload.expected_version = Number(event.target.dataset.version);
      const identityChanged = [
        [currentRecipe?.name, payload.name],
        [currentRecipe?.variant_name, payload.variant_name],
        [currentRecipe?.cooking_method, payload.cooking_method],
      ].some(([before, after]) => String(before || "").trim() !== String(after || "").trim());
      if (identityChanged && !uploadedCover) throw new Error("修改菜品名或做法版本时，需要同时上传并确认新的准确成品图");
      if (identityChanged) payload.replacement_cover = { url: uploadedCover.url, thumbnail_url: uploadedCover.thumbnail_url, confirmed_exact: true };
    }
    const saved = await api(editing ? `/api/v1/recipes/${encodeURIComponent(editing)}` : "/api/v1/recipes", { method: editing ? "PATCH" : "POST", body: JSON.stringify(payload) });
    if (editing && uploadedCover && !payload.replacement_cover) await api(`/api/v1/recipes/${encodeURIComponent(editing)}/media`, { method: "POST", body: JSON.stringify({ media_type: "image", role: "cover", url: uploadedCover.url, thumbnail_url: uploadedCover.thumbnail_url, provider: "local", title: `${payload.name}成品图`, attribution: "用户上传并人工核对", confirmed_exact: true, alignment_score: 1, checked_ingredients: true, checked_steps: true }) });
    event.target.reset(); delete event.target.dataset.recipeId; delete event.target.dataset.version; closeModals(); await loadMeals({ quiet: true }); toast(editing ? "菜谱修改已保存" : "自建菜谱已保存");
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
});

document.getElementById("recipeVideoForm").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const role = document.getElementById("recipeVideoRole").value;
    const ingredientsChecked = document.getElementById("recipeVideoIngredientsChecked").checked;
    const stepsChecked = document.getElementById("recipeVideoStepsChecked").checked;
    const score = Number(document.getElementById("recipeVideoScore").value || 0) / 100;
    if (role === "paired" && (!ingredientsChecked || !stepsChecked)) throw new Error("标为配套教学前，必须核对食材、调料、备菜和主要步骤");
    const payload = {
      media_type: "video", role, url: document.getElementById("recipeVideoUrl").value.trim(),
      title: document.getElementById("recipeVideoName").value.trim(), alignment_score: score,
      confirmed_exact: role === "paired" && ingredientsChecked && stepsChecked,
      checked_ingredients: ingredientsChecked, checked_steps: stepsChecked,
      differences: document.getElementById("recipeVideoDifferences").value.split(/\n+/).map(value => value.trim()).filter(Boolean),
    };
    const recipeId = document.getElementById("recipeVideoRecipeId").value;
    await api(`/api/v1/recipes/${encodeURIComponent(recipeId)}/media`, { method: "POST", body: JSON.stringify(payload) });
    event.target.reset(); closeModals(); await openRecipeDetail(recipeId); toast(role === "paired" ? "配套教学视频已保存" : "参考视频已保存，并标明以当前菜谱为准");
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
});

document.getElementById("shoppingItemForm").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const editing = event.target.dataset.itemId;
    const payload = { name: document.getElementById("shoppingItemName").value.trim(), quantity: Number(document.getElementById("shoppingItemQuantity").value), unit: document.getElementById("shoppingItemUnit").value, category: document.getElementById("shoppingItemCategory").value.trim() };
    if (editing) payload.expected_version = Number(event.target.dataset.version);
    await api(editing ? `/api/v1/shopping-lists/items/${encodeURIComponent(editing)}` : "/api/v1/shopping-lists/items", { method: editing ? "PATCH" : "POST", body: JSON.stringify(payload) });
    event.target.reset(); delete event.target.dataset.itemId; delete event.target.dataset.version; closeModals(); await loadMeals({ quiet: true }); toast(editing ? "采购食材已修改并锁定" : "临时食材已加入采购清单");
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
});

document.querySelectorAll(".modal").forEach(modal => modal.addEventListener("click", event => { if (event.target === modal) closeModals(); }));
document.addEventListener("error", event => {
  if (event.target?.matches?.("img[data-recipe-cover]")) event.target.closest(".recipe-cover")?.classList.add("load-failed");
}, true);
document.addEventListener("keydown", event => { if (event.key === "Escape") closeModals(); });

renderPlan();
navigate(state.screen);
initializeAuth();

// Lightweight same-origin sync for the local MVP. Production can replace this
// polling loop with the WebSocket events defined in SRS.md.
setInterval(async () => {
  if (document.hidden || !state.data || state.screen !== "chat") return;
  try {
    const result = await api("/api/v1/messages");
    if (result.items.length !== state.data.messages.length) {
      state.data.messages = result.items;
      renderMessages();
    }
  } catch (_) {
    // Preserve the last usable local view during transient network failures.
  }
}, 5000);

if ("serviceWorker" in navigator && window.isSecureContext) serviceWorkerRegistration().catch(() => {});
