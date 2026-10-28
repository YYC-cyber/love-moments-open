import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { LoveMomentsClient, buildH5SmartLink, createPrivateCache, type CacheIdentity, type ManifestCache } from "@love-moments/domain-client";
import { surfaces, type Surface, type UiManifest, type UiNode } from "@love-moments/ui-contract";
import "./style.css";

const privateCache = createPrivateCache({
  async getItem(key) { return localStorage.getItem(key); },
  async setItem(key, value) { localStorage.setItem(key, value); },
  async getAllKeys() { return Object.keys(localStorage); },
  async multiRemove(keys) { keys.forEach(key => localStorage.removeItem(key)); }
});
const identityKey = "love-react-scope";
try { const identity = JSON.parse(localStorage.getItem(identityKey) || "null") as CacheIdentity | null; if (identity) void privateCache.setIdentity(identity); } catch { localStorage.removeItem(identityKey); }
surfaces.forEach(surface => localStorage.removeItem(`love-react-${surface}`));
const cache: ManifestCache = {
  scope: privateCache.scope,
  async get(surface) { try { return JSON.parse(await privateCache.getItem(`react-ui:${surface}`) || "null"); } catch { return null; } },
  async set(surface, value) { await privateCache.setItem(`react-ui:${surface}`, JSON.stringify(value)); }
};
const client = new LoveMomentsClient("", cache);
const labels: Record<Surface, string> = { home: "首页", chat: "消息", trip: "旅行", timeline: "时光", life: "我们" };
const examples: Record<Surface, string> = { home: "今天有什么要确认？", chat: "整理聊天灵感", trip: "比较预算与强度", timeline: "整理今年的城市", life: "找出未完成愿望" };

function NodeCard({ node, onAction, onOpen }: { node: UiNode; onAction: (id: string) => void; onOpen: (node: UiNode) => void }) {
  const writeAction = node.actions.find(action => action.requiresConfirmation);
  return <article className="node" role="button" tabIndex={0} onClick={() => onOpen(node)} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") onOpen(node); }}><div><strong>{String(node.props.title || node.type)}</strong><small>{String(node.props.subtitle || "")}</small></div>{writeAction ? <button onClick={event => { event.stopPropagation(); onAction(writeAction.id); }}>预览操作</button> : <button onClick={event => { event.stopPropagation(); onOpen(node); }}>查看内容</button>}</article>;
}

function App() {
  const [surface, setSurface] = useState<Surface>("home");
  const [manifest, setManifest] = useState<UiManifest>();
  const [intent, setIntent] = useState("");
  const [error, setError] = useState("");
  const [loginRequired, setLoginRequired] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [preview, setPreview] = useState<{ id: string; data: Record<string, unknown> }>();

  const load = () => { void (async () => {
    setError("");
    try {
      try {
        const value = await client.bootstrap<{ user: { id: string }; couple?: { id: string; version: number }; permission_version: number }>();
        const identity = { user_id: value.user.id, couple_id: value.couple?.id || "unpaired", permission_version: value.permission_version };
        await privateCache.setIdentity(identity);
        localStorage.setItem(identityKey, JSON.stringify(identity));
      } catch (cause) { if ((cause as { status?: number }).status) throw cause; }
      setManifest(await client.getManifest(surface)); setLoginRequired(false);
    } catch (cause) {
      const failure = cause as Error & { status?: number }; setError(failure.message);
      if (failure.status === 401 || failure.status === 403) { await privateCache.setIdentity(null); localStorage.removeItem(identityKey); setManifest(undefined); setIntent(""); setPreview(undefined); setLoginRequired(true); }
    }
  })(); };
  useEffect(load, [surface]);
  useEffect(() => {
    const changed = (event: StorageEvent) => { if (event.key === "love-auth-sync" || (event.key === "love-active-scope" && event.oldValue && event.oldValue !== event.newValue)) { void privateCache.setIdentity(null); localStorage.removeItem(identityKey); setManifest(undefined); setIntent(""); setPreview(undefined); load(); } };
    window.addEventListener("storage", changed); return () => window.removeEventListener("storage", changed);
  }, [surface]);

  async function login(event: React.FormEvent) {
    event.preventDefault();
    try { await client.login(username, password); setPassword(""); setLoginRequired(false); load(); }
    catch (cause) { setError((cause as Error).message); }
  }
  async function compose(event: React.FormEvent) {
    event.preventDefault();
    try { setManifest(await client.compose(surface, intent || examples[surface])); setIntent(""); }
    catch (cause) { setError((cause as Error).message); }
  }
  async function openAction(id: string) { const result = await client.preview(id); setPreview({ id, data: result.preview }); }
  function openNode(node: UiNode) {
    window.location.assign(buildH5SmartLink(surface, node.type));
  }
  async function confirm() { if (!preview) return; await client.confirm(preview.id, `web:${preview.id}:${Date.now()}`); setPreview(undefined); load(); }
  async function cancel() { if (preview) await client.cancel(preview.id).catch(() => undefined); setPreview(undefined); }

  if (loginRequired) return <main className="login"><header><i>L</i><div><h1>恋屿</h1><p>LOVE MOMENTS</p></div></header><section className="login-card"><h2>登录共同小岛</h2><form onSubmit={login}><input value={username} onChange={event => setUsername(event.target.value)} autoComplete="username" placeholder="账号"/><input value={password} onChange={event => setPassword(event.target.value)} type="password" autoComplete="current-password" placeholder="密码"/><button>登录</button></form>{error && <p className="error">{error}</p>}</section></main>;

  return <main><header><i>L</i><div><h1>恋屿</h1><p>LOVE MOMENTS · INTELLIGENT UI</p></div></header><section className="hero"><p>属于两个人的动态生活界面</p><h2>{labels[surface]}</h2></section><form onSubmit={compose}><input value={intent} onChange={event => setIntent(event.target.value)} placeholder={examples[surface]} maxLength={500}/><button>问恋屿</button></form>{error && <p className="error">{error}</p>}<section className="layout">{manifest && [...manifest.sections, ...manifest.personalOverlay].map(node => <NodeCard key={node.id} node={node} onAction={openAction} onOpen={openNode}/>)}</section>{manifest && <footer>{manifest.generatedBy} · {manifest.sharedManifestId}</footer>}<nav>{surfaces.map(item => <button className={item === surface ? "active" : ""} onClick={() => setSurface(item)} key={item}>{labels[item]}</button>)}</nav>{preview && <div className="dialog" role="dialog" aria-modal="true"><div><h3>{String(preview.data.title || "确认建议")}</h3><p>{String(preview.data.before || "无")} → {String(preview.data.after || "")}</p><small>{String(preview.data.impact || "")}</small><button onClick={confirm}>确认执行</button><button className="quiet" onClick={cancel}>取消</button></div></div>}</main>;
}

createRoot(document.getElementById("root")!).render(<App/>);
