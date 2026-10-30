const dashboard = document.getElementById("dashboard");
const accessState = document.getElementById("accessState");
let snapshot = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function toast(message) {
  const element = document.getElementById("toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("show"), 2600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    credentials: "same-origin",
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return body;
}

function bytes(value) {
  const number = Number(value || 0);
  if (number < 1024) return `${number} B`;
  if (number < 1024 ** 2) return `${(number / 1024).toFixed(1)} KB`;
  if (number < 1024 ** 3) return `${(number / 1024 ** 2).toFixed(1)} MB`;
  return `${(number / 1024 ** 3).toFixed(1)} GB`;
}

function growthBytes(value) {
  if (value == null) return "待积累";
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return "无增长";
  return number < 0 ? `已释放 ${bytes(Math.abs(number))}` : `+${bytes(number)}`;
}

function capacityClass(ratio) {
  const bucket = Math.min(100, Math.max(0, Math.ceil(Number(ratio || 0) / 10) * 10));
  return `capacity-fill-${bucket}`;
}

function dateTime(value) {
  if (!value) return "暂无";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "暂无" : date.toLocaleString("zh-CN", { hour12: false });
}

function uptime(seconds) {
  const value = Number(seconds || 0);
  if (value < 60) return `${value} 秒`;
  if (value < 3600) return `${Math.floor(value / 60)} 分钟`;
  if (value < 86400) return `${Math.floor(value / 3600)} 小时`;
  return `${Math.floor(value / 86400)} 天`;
}

function render(data) {
  snapshot = data;
  document.getElementById("privacyNotice").textContent = data.privacy;
  const outstanding = data.events.filter(item => !item.resolved_at);
  const critical = data.checks.some(item => item.status === "critical") || outstanding.some(item => item.severity === "critical");
  const warning = data.checks.some(item => item.status === "warning") || outstanding.length > 0;
  const overall = document.getElementById("overallStatus");
  overall.className = `overall ${critical ? "critical" : warning ? "warning" : "ok"}`;
  overall.innerHTML = `<i></i><strong>${critical ? "需要立即处理" : warning ? "可以继续使用，有待完善项" : "运行状态良好"}</strong><small>更新于 ${escapeHtml(dateTime(data.generated_at))}</small>`;

  document.getElementById("checkGrid").innerHTML = data.checks.map(item => `<article class="check-card ${escapeHtml(item.status)}"><header><strong>${escapeHtml(item.label)}</strong><i></i></header><small>${escapeHtml(item.message)}</small></article>`).join("");
  const metrics = [
    ["运行时间", uptime(data.runtime.uptime_seconds), `Python ${data.runtime.python}`],
    ["部署版本", data.runtime.release?.deployed_sha === "unknown" ? "开发模式" : data.runtime.release.deployed_sha.slice(0, 8), data.runtime.release?.latest_sha ? `GitHub ${data.runtime.release.latest_sha.slice(0, 8)}` : "尚未取得远程版本"],
    ["数据库", bytes(data.storage.database_bytes), `Schema ${data.runtime.schema_version}`],
    ["媒体文件", String(data.storage.media_files), bytes(data.storage.media_bytes)],
    ["磁盘可用", bytes(data.storage.disk_free_bytes), `总计 ${bytes(data.storage.disk_total_bytes)}`],
    ["24 小时变化", growthBytes(data.storage.media_24h_bytes), "每小时记录一次"],
    ["7 天变化", growthBytes(data.storage.media_7d_bytes), "保留 90 天指标"]
  ];
  document.getElementById("metricGrid").innerHTML = metrics.map(item => `<article class="metric"><span>${escapeHtml(item[0])}</span><b>${escapeHtml(item[1])}</b><small>${escapeHtml(item[2])}</small></article>`).join("");

  const configLabels = { large_model: "大模型", amap: "高德路线", shengwang: "声网语音", web_push: "消息与来电提醒", turn: "TURN 回退", qiniu: "七牛云媒体", supabase: "Supabase", backup_mirror: "数据库异盘镜像", ocr: "真实 OCR", booking: "真实报价" };
  document.getElementById("configGrid").innerHTML = Object.entries(configLabels).map(([key, label]) => `<div class="config-item"><span>${label}</span><b class="${data.configured[key] ? "on" : ""}">${data.configured[key] ? "已配置" : "模拟/未启用"}</b></div>`).join("");

  document.getElementById("quotaUsage").innerHTML = data.external_api_quotas.map(item => {
    const ratio = Math.min(100, Number(item.ratio || 0) * 100);
    const state = !item.configured ? "未启用" : item.unlimited ? "不设本地上限" : item.exhausted ? "已停止" : item.warning ? "接近上限" : "安全";
    const reset = item.resets_at ? ` · ${escapeHtml(item.resets_at)} 恢复` : " · 累计额度不会自动恢复";
    const amount = item.unlimited ? `${Number(item.used)} / 无上限` : `${Number(item.used)} / ${Number(item.limit)}`;
    const detail = item.unlimited ? `只记录用量，不在恋屿内阻止请求；供应商余额和限流仍然有效` : `剩余 ${Number(item.remaining)} 次 · ${escapeHtml(item.period_label)}${reset}`;
    return `<article class="quota-card ${item.exhausted ? "exhausted" : item.warning ? "warning" : ""}"><div><strong>${escapeHtml(item.label)}</strong><span>${state}</span></div><b>${amount}</b><small>${detail}</small><div class="capacity-track"><i class="${capacityClass(ratio)}"></i></div></article>`;
  }).join("");

  const vault = data.data_vault;
  const policy = vault.account_export_policy;
  const backup = vault.latest_backup;
  document.getElementById("dataVaultState").innerHTML = [
    ["数据库与外键", vault.database.integrity === "ok" && vault.database.foreign_key_violations === 0 && vault.database.schema_ok, `Schema ${vault.database.schema_actual}/${vault.database.schema_expected} · 外键异常 ${vault.database.foreign_key_violations}`],
    ["账号导出权限", policy.ok, `规则 ${policy.version} · 已覆盖 ${policy.covered}/${policy.required} 张表`],
    ["媒体文件树", vault.media.available && vault.media.missing_files === 0 && vault.media.orphan_files === 0, `缺失 ${vault.media.missing_files} · 孤立 ${vault.media.orphan_files}`],
    ["最近恢复演练", backup.verified, backup.exists ? `${backup.age_hours ?? "未知"} 小时前 · ${backup.restore_drill === "passed" ? "校验与恢复演练通过" : "尚未全部通过"}` : "尚无备份"],
    ["异盘镜像", vault.mirror.configured, vault.mirror.configured ? "已配置第二存储位置" : "未配置；本机磁盘损坏时备份可能一起丢失"]
  ].map(([label, ok, detail]) => `<article class="list-row"><div><strong>${escapeHtml(label)}</strong><small>${escapeHtml(detail)}</small></div><span class="severity ${ok ? "" : "warning"}">${ok ? "正常" : "待完善"}</span></article>`).join("");

  document.getElementById("backupCount").textContent = `${data.backups.count} 份`;
  const backupLocation = `<p class="panel-note">${data.configured.backup_mirror ? "数据库备份已配置到第二存储位置。" : "当前数据库备份仍在本机同一磁盘；七牛云只保存媒体，不能替代 SQLite 异盘备份。"}</p>`;
  document.getElementById("backupList").innerHTML = backupLocation + (data.backups.items.length ? data.backups.items.map(item => `<article class="list-row"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(dateTime(item.created_at))} · ${item.source === "automatic" ? "自动" : "手动"} · 媒体 ${item.media_files} 个 / ${bytes(item.media_bytes)} · ${item.verification_ok ? "恢复演练通过" : "待验证"}</small></div><div class="row-actions"><span class="severity ${item.verification_ok ? "" : "warning"}">${item.verification_ok ? "已验证" : "待验证"}</span><button data-verify-backup="${escapeHtml(item.name)}">重新验证</button></div></article>`).join("") : `<div class="empty">还没有备份，建议立即创建第一份。</div>`);

  const integrity = data.storage.integrity;
  const capacityRows = data.storage.users.map(item => {
    const ratio = Math.min(100, Number(item.media_bytes || 0) / Math.max(1, Number(data.storage.user_quota_bytes || 1)) * 100);
    return `<article class="capacity-row"><div><strong>${escapeHtml(item.nickname)} · ${bytes(item.media_bytes)}</strong><small>个人上限 ${bytes(data.storage.user_quota_bytes)}</small></div><div class="capacity-track"><i class="${capacityClass(ratio)}"></i></div></article>`;
  });
  const coupleRatio = Math.min(100, Number(data.storage.couple_media_bytes || 0) / Math.max(1, Number(data.storage.couple_quota_bytes || 1)) * 100);
  document.getElementById("storageGrowth").textContent = `7 天 ${growthBytes(data.storage.media_7d_bytes)}`;
  document.getElementById("storageUsage").innerHTML = `<article class="capacity-row"><div><strong>情侣空间 · ${bytes(data.storage.couple_media_bytes)}</strong><small>总上限 ${bytes(data.storage.couple_quota_bytes)} · 缺失 ${integrity.missing_files} · 孤立 ${integrity.orphan_files} / ${bytes(integrity.orphan_bytes)}</small></div><div class="capacity-track couple"><i class="${capacityClass(coupleRatio)}"></i></div></article>${capacityRows.join("")}`;

  const watchdog = data.maintenance.watchdog || {};
  const daily = data.maintenance.daily || {};
  document.getElementById("maintenanceState").innerHTML = `<article class="list-row"><div><strong>服务健康守护</strong><small>${watchdog.checked_at ? `${dateTime(watchdog.checked_at)} · ${escapeHtml(watchdog.message || "")}` : "尚未运行；安装 LaunchAgent 后每 5 分钟检查"}</small></div><span class="severity ${watchdog.healthy ? "" : "warning"}">${watchdog.healthy ? "正常" : "待启用"}</span></article><article class="list-row"><div><strong>每日自动备份</strong><small>${daily.completed_at ? `${dateTime(daily.completed_at)} · ${daily.backup ? escapeHtml(daily.backup) : escapeHtml(daily.message || "未完成")}` : "尚未运行；默认每日 03:30 执行"}</small></div><span class="severity ${daily.ok ? "" : "warning"}">${daily.ok ? "成功" : "待启用"}</span></article>`;
  document.getElementById("restoreInstructions").innerHTML = data.restore_instructions.map(item => `<li>${escapeHtml(item)}</li>`).join("");

  document.getElementById("eventCount").textContent = `${outstanding.length} 项待处理`;
  document.getElementById("eventList").innerHTML = data.events.length ? data.events.map(item => {
    const recovery = item.detail && item.detail.recovery;
    const resolutionMessage = recovery ? `根据真实恢复证据自动关闭：${escapeHtml(recovery.message || "状态已恢复")}` : item.resolved_by ? "人工确认" : "系统体检确认恢复";
    const resolution = item.resolved_at ? ` · 已于 ${escapeHtml(dateTime(item.resolved_at))}${resolutionMessage}` : "";
    return `<article class="list-row ${item.resolved_at ? "resolved" : ""}"><div><strong>${escapeHtml(item.summary)}</strong><small>${escapeHtml(item.category)} · 最近 ${escapeHtml(dateTime(item.last_seen_at))} · 共 ${item.occurrences} 次${resolution}</small></div><div class="row-actions"><span class="severity ${escapeHtml(item.severity)}">${item.severity === "critical" ? "严重" : item.severity === "warning" ? "提醒" : "信息"}</span>${item.resolved_at ? "" : `<button data-resolve-event="${escapeHtml(item.id)}">确认已处理</button>`}</div></article>`;
  }).join("") : `<div class="empty">暂时没有异常记录。</div>`;

  document.getElementById("userList").innerHTML = data.users.map(item => `<article class="list-row"><div><strong>${escapeHtml(item.nickname)} · ${escapeHtml(item.username)}</strong><small>${item.deleted_at ? "已注销" : "正常"} · 活跃设备 ${Number(item.active_sessions || 0)} · 最近 ${escapeHtml(dateTime(item.last_seen_at))}</small></div><span class="severity">${Number(item.session_count || 0)} 会话</span></article>`).join("") || `<div class="empty">没有账号。</div>`;

  document.getElementById("sessionList").innerHTML = data.sessions.map(item => `<article class="list-row ${item.revoked_at ? "resolved" : ""}"><div><strong>${escapeHtml(item.nickname)} · ${escapeHtml(item.device_name)}${item.current ? "（当前）" : ""}</strong><small>${escapeHtml(item.ip_address || "未知地址")} · 最近 ${escapeHtml(dateTime(item.last_seen_at))}${item.revoked_at ? " · 已撤销" : ""}</small></div><div class="row-actions">${item.revoked_at ? `<span class="severity">已撤销</span>` : `<button data-revoke-session="${escapeHtml(item.id)}" ${item.current ? "disabled" : ""}>${item.current ? "当前设备" : "撤销"}</button>`}</div></article>`).join("") || `<div class="empty">没有设备会话。</div>`;

  document.getElementById("auditList").innerHTML = data.audits.map(item => `<article class="audit-row"><b>${escapeHtml(item.action)} · ${escapeHtml(item.target_type)}</b><small>${escapeHtml(item.actor_name)} · ${escapeHtml(dateTime(item.created_at))}</small></article>`).join("") || `<div class="empty">暂无操作记录。</div>`;
}

function showAccess(error) {
  dashboard.hidden = true;
  accessState.hidden = false;
  document.getElementById("accessTitle").textContent = error.status === 401 ? "请先登录" : "没有后台权限";
  document.getElementById("accessMessage").textContent = error.status === 401 ? "请返回恋屿，先登录管理员账号，再打开后台。" : "当前账号不在 ADMIN_USERNAMES 管理员白名单中。";
}

async function loadOverview(message = "") {
  try {
    const data = await api("/api/v1/admin/overview");
    dashboard.hidden = false;
    accessState.hidden = true;
    render(data);
    if (message) toast(message);
  } catch (error) {
    if (error.status === 401 || error.status === 403) showAccess(error);
    else toast(error.message);
  }
}

document.getElementById("refreshButton").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try { render(await api("/api/v1/admin/checks", { method: "POST", body: "{}" })); toast("完整体检已更新"); }
  catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});

document.getElementById("backupButton").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "备份中…";
  try { await api("/api/v1/admin/backups", { method: "POST", body: "{}" }); await loadOverview("安全备份已完成"); }
  catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "立即备份"; }
});

document.addEventListener("click", async event => {
  const resolve = event.target.closest("[data-resolve-event]");
  if (resolve) {
    resolve.disabled = true;
    try { await api(`/api/v1/admin/events/${resolve.dataset.resolveEvent}/resolve`, { method: "POST", body: "{}" }); await loadOverview("异常已标记为处理完成"); }
    catch (error) { toast(error.message); resolve.disabled = false; }
    return;
  }
  const revoke = event.target.closest("[data-revoke-session]");
  if (revoke && window.confirm("撤销后该设备需要重新登录，确定继续？")) {
    revoke.disabled = true;
    try { await api(`/api/v1/admin/sessions/${revoke.dataset.revokeSession}/revoke`, { method: "POST", body: "{}" }); await loadOverview("设备会话已撤销"); }
    catch (error) { toast(error.message); revoke.disabled = false; }
    return;
  }
  const verify = event.target.closest("[data-verify-backup]");
  if (verify) {
    verify.disabled = true;
    verify.textContent = "验证中…";
    try { await api(`/api/v1/admin/backups/${encodeURIComponent(verify.dataset.verifyBackup)}/verify`, { method: "POST", body: "{}" }); await loadOverview("备份恢复演练通过"); }
    catch (error) { toast(error.message); verify.disabled = false; verify.textContent = "重新验证"; }
  }
});

window.addEventListener("online", () => toast("网络已恢复，正在重新体检"));
window.addEventListener("offline", () => toast("当前离线，后台数据暂时不会更新"));
loadOverview();
