import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { LoveMomentsClient, type ManifestCache } from "@love-moments/domain-client";
import { surfaces, type Surface, type UiManifest, type UiNode } from "@love-moments/ui-contract";
import "./style.css";

const cache: ManifestCache = {
  async get(surface) { try { return JSON.parse(localStorage.getItem(`love-react-${surface}`) || "null"); } catch { localStorage.removeItem(`love-react-${surface}`); return null; } },
  async set(surface, value) { localStorage.setItem(`love-react-${surface}`, JSON.stringify(value)); }
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

  const load = () => {
    setError("");
    client.getManifest(surface).then(value => { setManifest(value); setLoginRequired(false); }).catch((cause: Error & { status?: number }) => { setError(cause.message); if (cause.status === 401) setLoginRequired(true); });
  };
  useEffect(load, [surface]);

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
    const screenByType: Partial<Record<UiNode["type"], Surface>> = {
      couple_overview: "home", daily_question: "home", quick_capture: "home",
      message_summary: "chat", favorite_memory: "chat", suggested_reply: "chat", import_guide: "chat", trip_share: "trip",
      trip_countdown: "trip", trip_variant_compare: "trip", itinerary_timeline: "trip", budget_summary: "trip", hotel_quote: "trip", risk_alert: "trip", impact_diff: "trip", approval_state: "trip",
      featured_memory: "timeline", timeline_summary: "timeline", month_album: "timeline", footprint_summary: "timeline", year_report: "timeline", privacy_status: "timeline",
      wishlist: "life", calendar_summary: "life", anniversary: "life", todo_summary: "life", preference_summary: "life", pending_items: "life"
    };
    const screen = screenByType[node.type] || surface;
    window.location.assign(`/?screen=${screen}&smart=${node.type}`);
  }
  async function confirm() { if (!preview) return; await client.confirm(preview.id, `web:${preview.id}:${Date.now()}`); setPreview(undefined); load(); }
  async function cancel() { if (preview) await client.cancel(preview.id).catch(() => undefined); setPreview(undefined); }

  if (loginRequired) return <main className="login"><header><i>L</i><div><h1>恋屿</h1><p>LOVE MOMENTS</p></div></header><section className="login-card"><h2>登录共同小岛</h2><form onSubmit={login}><input value={username} onChange={event => setUsername(event.target.value)} autoComplete="username" placeholder="账号"/><input value={password} onChange={event => setPassword(event.target.value)} type="password" autoComplete="current-password" placeholder="密码"/><button>登录</button></form>{error && <p className="error">{error}</p>}</section></main>;

  return <main><header><i>L</i><div><h1>恋屿</h1><p>LOVE MOMENTS · INTELLIGENT UI</p></div></header><section className="hero"><p>属于两个人的动态生活界面</p><h2>{labels[surface]}</h2></section><form onSubmit={compose}><input value={intent} onChange={event => setIntent(event.target.value)} placeholder={examples[surface]} maxLength={500}/><button>问恋屿</button></form>{error && <p className="error">{error}</p>}<section className="layout">{manifest && [...manifest.sections, ...manifest.personalOverlay].map(node => <NodeCard key={node.id} node={node} onAction={openAction} onOpen={openNode}/>)}</section>{manifest && <footer>{manifest.generatedBy} · {manifest.sharedManifestId}</footer>}<nav>{surfaces.map(item => <button className={item === surface ? "active" : ""} onClick={() => setSurface(item)} key={item}>{labels[item]}</button>)}</nav>{preview && <div className="dialog" role="dialog" aria-modal="true"><div><h3>{String(preview.data.title || "确认建议")}</h3><p>{String(preview.data.before || "无")} → {String(preview.data.after || "")}</p><small>{String(preview.data.impact || "")}</small><button onClick={confirm}>确认执行</button><button className="quiet" onClick={cancel}>取消</button></div></div>}</main>;
}

createRoot(document.getElementById("root")!).render(<App/>);
