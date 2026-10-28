import React, { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Linking, Modal, SafeAreaView, ScrollView, StyleSheet, Text, TextInput, TouchableOpacity, View } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import * as SecureStore from "expo-secure-store";
import { WebView } from "react-native-webview";
import { LoveMomentsClient, type CalendarEntry, type MealCalendar, type MealCart, type MessageItem, type MomentItem, type PartnerPreference, type PlaceCandidate, type QuoteItem, type RecipeDetail, type RecipeSummary, type ShoppingList, type TripSummary, type WishlistItem } from "@love-moments/domain-client";
import { surfaces, type Surface, type UiManifest, type UiNode } from "@love-moments/ui-contract";

const API = (process.env.EXPO_PUBLIC_API_URL || "").trim().replace(/\/$/, "");
const ACCESS_TOKEN_KEY = "love-access-token";
const manifestCache: { get(surface: Surface): Promise<unknown>; set(surface: Surface, value: UiManifest): Promise<void> } = {
  async get(surface) { try { return JSON.parse(await AsyncStorage.getItem(`ui:${surface}`) || "null"); } catch { return null; } },
  async set(surface, value) { await AsyncStorage.setItem(`ui:${surface}`, JSON.stringify(value)); }
};
function localDateAfter(days: number) {
  const value = new Date();
  value.setDate(value.getDate() + days);
  return value.toLocaleDateString("sv-SE");
}
const labels: Record<Surface, string> = { home: "首页", chat: "消息", trip: "旅行", timeline: "时光", life: "我们" };
type Person = { id: string; nickname: string; avatar_label?: string };
type Bootstrap = { needs_pairing?: boolean; user: Person; partner?: Person; couple?: { anniversary: string }; messages?: MessageItem[]; moments?: MomentItem[]; trip?: TripSummary; trips?: TripSummary[]; calendar?: CalendarEntry[]; preferences?: PartnerPreference[]; wishlist?: { title: string; items: WishlistItem[] } };

function Button({ title, onPress, quiet = false, disabled = false }: { title: string; onPress: () => void; quiet?: boolean; disabled?: boolean }) {
  return <TouchableOpacity disabled={disabled} style={[s.button, quiet && s.quietButton, disabled && s.disabled]} onPress={onPress}><Text style={quiet ? s.wine : s.white}>{title}</Text></TouchableOpacity>;
}
function Field({ value, onChangeText, placeholder, secureTextEntry = false }: { value: string; onChangeText: (text: string) => void; placeholder: string; secureTextEntry?: boolean }) {
  return <TextInput style={s.input} value={value} onChangeText={onChangeText} placeholder={placeholder} secureTextEntry={secureTextEntry} autoCapitalize="none" />;
}
function Card({ children }: { children: React.ReactNode }) { return <View style={s.card}>{children}</View>; }
function stepDuration(seconds?: number | null) {
  if (!seconds) return "";
  if (seconds >= 3600) return `${Math.round(seconds / 3600 * 10) / 10} 小时`;
  return `${Math.max(1, Math.round(seconds / 60))} 分钟`;
}

export default function App() {
  const client = useMemo(() => new LoveMomentsClient(API, manifestCache), []);
  const [surface, setSurface] = useState<Surface>("home");
  const [data, setData] = useState<Bootstrap>();
  const [manifest, setManifest] = useState<UiManifest>();
  const [loginRequired, setLoginRequired] = useState(false);
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [nickname, setNickname] = useState("");
  const [invite, setInvite] = useState("");
  const [pairAnniversary, setPairAnniversary] = useState("");
  const [message, setMessage] = useState("");
  const [tripForm, setTripForm] = useState({ destination: "", region_hint: "", start_date: localDateAfter(30), end_date: localDateAfter(32), budget: "4000" });
  const [placeCandidates, setPlaceCandidates] = useState<PlaceCandidate[]>([]);
  const [selectedPlace, setSelectedPlace] = useState<PlaceCandidate | null>(null);
  const [lifeForm, setLifeForm] = useState({ title: "", date: localDateAfter(0), type: "todo" });
  const [editingLife, setEditingLife] = useState<CalendarEntry | null>(null);
  const [prefForm, setPrefForm] = useState({ label: "", value: "" });
  const [editingPreference, setEditingPreference] = useState<PartnerPreference | null>(null);
  const [wishText, setWishText] = useState("");
  const [editingWish, setEditingWish] = useState<WishlistItem | null>(null);
  const [ocrText, setOcrText] = useState("");
  const [ocrDraft, setOcrDraft] = useState<Record<string, any> | null>(null);
  const [timelineMode, setTimelineMode] = useState<"moments" | "private" | "trash" | "report" | "months" | "map">("moments");
  const [trash, setTrash] = useState<MomentItem[]>([]);
  const [report, setReport] = useState<Record<string, unknown>>();
  const [timelineItems, setTimelineItems] = useState<Array<Record<string, unknown>>>([]);
  const [tripTool, setTripTool] = useState("");
  const [quoteType, setQuoteType] = useState<"hotel" | "flight" | "train">("hotel");
  const [quoteForm, setQuoteForm] = useState({ origin: "", destination: "", outbound: new Date(Date.now() + 86400000).toISOString().slice(0, 10), returnDate: new Date(Date.now() + 3 * 86400000).toISOString().slice(0, 10) });
  const [quoteItems, setQuoteItems] = useState<QuoteItem[]>([]);
  const [quoteNotice, setQuoteNotice] = useState("");
  const [quoteBusy, setQuoteBusy] = useState(false);
  const [webPanel, setWebPanel] = useState<"call" | "account" | "app" | null>(null);
  const [token, setToken] = useState("");
  const [mealMode, setMealMode] = useState<"recipes" | "calendar" | "shopping">("recipes");
  const [recipeSearch, setRecipeSearch] = useState("");
  const [recipes, setRecipes] = useState<RecipeSummary[]>([]);
  const [recipeDetail, setRecipeDetail] = useState<RecipeDetail | null>(null);
  const [mealDate, setMealDate] = useState(new Date().toLocaleDateString("sv-SE"));
  const [mealSlot, setMealSlot] = useState<"breakfast" | "lunch" | "dinner" | "snack">("dinner");
  const [mealCart, setMealCart] = useState<MealCart>({ id: null, title: "我们的点菜车", status: "active", version: 0, items: [] });
  const [mealCalendar, setMealCalendar] = useState<MealCalendar>();
  const [shopping, setShopping] = useState<ShoppingList>({ id: null, title: "采购清单", status: "active", version: 0, items: [] });
  const mealEventCursor = useRef(0);

  async function loadMealData() {
    try {
      const [library, cart, calendar, list] = await Promise.all([client.recipes({ q: recipeSearch, limit: 50 }), client.mealCart(), client.mealCalendar(mealDate, mealDate), client.shoppingList()]);
      setRecipes(library.items); setMealCart(cart); setMealCalendar(calendar); setShopping(list);
      await AsyncStorage.setItem("love-mobile-meals", JSON.stringify({ recipes: library.items, cart, calendar, list }));
    } catch (error) {
      const cached = await AsyncStorage.getItem("love-mobile-meals");
      if (cached) { const value = JSON.parse(cached); setRecipes(value.recipes || []); setMealCart(value.cart); setMealCalendar(value.calendar); setShopping(value.list); }
      else throw error;
    }
  }

  async function refresh() {
    try {
      const value = await client.bootstrap<Bootstrap>();
      setData(value); setLoginRequired(false); await AsyncStorage.setItem("love-bootstrap", JSON.stringify(value));
      if (!value.needs_pairing) client.getManifest(surface).then(setManifest).catch(() => undefined);
    } catch (error) {
      const status = (error as Error & { status?: number }).status;
      if (status === 401) setLoginRequired(true);
      else { const cached = await AsyncStorage.getItem("love-bootstrap"); if (cached) setData(JSON.parse(cached)); else Alert.alert("无法同步", (error as Error).message); }
    }
  }
  useEffect(() => { void (async () => {
    if (!API) { setLoginRequired(true); return; }
    let value = await SecureStore.getItemAsync(ACCESS_TOKEN_KEY);
    if (!value) {
      value = await AsyncStorage.getItem(ACCESS_TOKEN_KEY);
      if (value) await SecureStore.setItemAsync(ACCESS_TOKEN_KEY, value);
      await AsyncStorage.removeItem(ACCESS_TOKEN_KEY);
    }
    setToken(value || ""); client.setAccessToken(value || undefined); await refresh();
  })(); }, []);
  useEffect(() => { if (data && !data.needs_pairing) client.getManifest(surface).then(setManifest).catch(() => undefined); }, [surface]);
  useEffect(() => { if (data && !data.needs_pairing && surface === "life") void loadMealData().catch(error => Alert.alert("点菜数据加载失败", error.message)); }, [surface, mealDate]);
  useEffect(() => {
    if (!data || data.needs_pairing) return;
    const timer = setInterval(() => { void client.events(mealEventCursor.current).then(result => {
      mealEventCursor.current = Math.max(mealEventCursor.current, result.latest_sequence || 0);
      if (result.items.some(item => item.event_type === "meals.updated")) void loadMealData();
    }).catch(() => undefined); }, 4000);
    return () => clearInterval(timer);
  }, [data?.user?.id, surface, mealDate, recipeSearch]);

  async function authenticate() {
    try {
      const result = authMode === "login" ? await client.login(username, password, "bearer") : await client.register(username, password, nickname, "bearer");
      if (result.access_token) { setToken(result.access_token); await SecureStore.setItemAsync(ACCESS_TOKEN_KEY, result.access_token); }
      setPassword(""); await refresh();
    } catch (error) { Alert.alert("操作失败", (error as Error).message); }
  }
  async function pair() { try { await client.acceptInvite(invite, pairAnniversary); await refresh(); } catch (error) { Alert.alert("绑定失败", (error as Error).message); } }
  async function logout() {
    let revokeWarning = "";
    try {
      await client.logout();
    } catch (error) {
      revokeWarning = (error as Error).message;
      client.setAccessToken();
    } finally {
      await SecureStore.deleteItemAsync(ACCESS_TOKEN_KEY);
      await AsyncStorage.multiRemove(["love-bootstrap", ...surfaces.map(item => `ui:${item}`)]);
      setToken(""); setData(undefined); setManifest(undefined); setLoginRequired(true);
      if (revokeWarning) Alert.alert("已在此设备退出", `服务器暂时无法撤销旧会话：${revokeWarning}。服务器恢复后可在设备中心撤销该会话。`);
    }
  }
  async function send() { if (!message.trim()) return; try { await client.sendMessage(message.trim(), `mobile:${Date.now()}:message`); setMessage(""); await refresh(); } catch (error) { Alert.alert("发送失败", (error as Error).message); } }
  async function searchTripPlaces() { try { const result = await client.searchPlaces(tripForm.destination, tripForm.region_hint); setPlaceCandidates(result.items); if (!result.items.length) Alert.alert("没有找到", result.notice || "请补充国家、城市或区县后重试"); } catch (error) { Alert.alert("地点查询失败", (error as Error).message); } }
  async function createTrip() { if (!selectedPlace) { Alert.alert("请先确认地点", "同名地点可能位于不同城市。请先查询并选择带地址的具体地点。"); return; } try { await client.createTrip({ ...tripForm, destination_candidate_id: selectedPlace.candidate_id, budget: Number(tripForm.budget), preferences: { pace: "松弛", transport: "公共交通" } }); setTripForm(value => ({ ...value, destination: "", region_hint: "" })); setSelectedPlace(null); setPlaceCandidates([]); await refresh(); } catch (error) { Alert.alert("创建失败", (error as Error).message); } }
  async function searchQuotes(trip: TripSummary) {
    setQuoteBusy(true);
    try {
      const destination = quoteForm.destination.trim() || trip.destination;
      const payload = quoteType === "hotel"
        ? { trip_id: trip.id, type: quoteType, destination, check_in: trip.start_date.slice(0, 10), check_out: trip.end_date.slice(0, 10), adults: 2 }
        : quoteType === "flight"
          ? { trip_id: trip.id, type: quoteType, origin: quoteForm.origin.trim(), destination, outbound_date: quoteForm.outbound, return_date: quoteForm.returnDate, adults: 2 }
          : { trip_id: trip.id, type: quoteType, origin: quoteForm.origin.trim(), destination, date: quoteForm.outbound };
      const result = await client.quotes(payload);
      setQuoteItems(result.items); setQuoteNotice(result.notice); setTripTool("");
    } catch (error) { Alert.alert("查询失败", (error as Error).message); }
    finally { setQuoteBusy(false); }
  }
  async function selectQuote(item: QuoteItem) {
    try {
      const selected = await client.selectQuote(item.id);
      setQuoteItems(items => items.map(value => value.quote_type === selected.quote_type ? { ...value, selected_at: value.id === selected.id ? selected.selected_at : null } : value));
    } catch (error) { Alert.alert("保存失败", (error as Error).message); }
  }
  async function saveLife() { try { const payload = { entry_type: lifeForm.type, title: lifeForm.title, starts_at: lifeForm.date, all_day: true, recurrence: lifeForm.type === "anniversary" ? "yearly" : "none", ...(editingLife ? { expected_version: editingLife.version } : {}) }; if (editingLife) await client.updateCalendar(editingLife.id, payload); else await client.createCalendar(payload); setLifeForm(value => ({ ...value, title: "" })); setEditingLife(null); await refresh(); } catch (error) { Alert.alert("保存失败", (error as Error).message); } }
  async function savePreference() { if (!data?.partner) return; try { const payload = { subject_user_id: editingPreference?.subject_user_id || data.partner.id, category: editingPreference?.category || "其他", label: prefForm.label, value: prefForm.value, ...(editingPreference ? { expected_version: editingPreference.version } : {}) }; if (editingPreference) await client.updatePreference(editingPreference.id, payload); else await client.createPreference(payload); setPrefForm({ label: "", value: "" }); setEditingPreference(null); await refresh(); } catch (error) { Alert.alert("保存失败", (error as Error).message); } }
  async function saveWish() { if (!wishText.trim()) return; try { if (editingWish) await client.updateWishlistItem(editingWish.id, { text: wishText.trim(), note: editingWish.note || "", expected_version: editingWish.version }); else await client.createWishlistItem({ text: wishText.trim() }); setWishText(""); setEditingWish(null); await refresh(); } catch (error) { Alert.alert("保存失败", (error as Error).message); } }
  async function makeOcrDraft() { try { setOcrDraft(await client.createChatImport({ text: ocrText, base_date: new Date().toISOString().slice(0, 10) })); } catch (error) { Alert.alert("识别失败", (error as Error).message); } }
  async function confirmOcr() { if (!ocrDraft) return; try { for (const item of ocrDraft.items || []) await client.updateChatImportItem(ocrDraft.id, item.id, { content: item.content, speaker: item.speaker, selected: item.selected, expected_version: item.version }); await client.confirmChatImport(ocrDraft.id); setOcrDraft(null); setOcrText(""); await refresh(); } catch (error) { Alert.alert("导入失败", (error as Error).message); } }

  async function openSmartNode(node: UiNode) {
    if (["featured_memory", "timeline_summary"].includes(node.type)) { setTimelineMode("moments"); setSurface("timeline"); return; }
    if (node.type === "privacy_status") { setTimelineMode("private"); setSurface("timeline"); return; }
    if (node.type === "month_album") { const value = await client.timelineMonths(); setTimelineItems(value.items); setTimelineMode("months"); setSurface("timeline"); return; }
    if (node.type === "footprint_summary") { const value = await client.footprints(); setTimelineItems(value.items); setTimelineMode("map"); setSurface("timeline"); return; }
    if (node.type === "year_report") { setReport(await client.annualReport(new Date().getFullYear())); setTimelineMode("report"); setSurface("timeline"); return; }
    if (["wishlist", "calendar_summary", "anniversary", "todo_summary", "preference_summary", "pending_items"].includes(node.type)) { setSurface("life"); return; }
    if (["recipe_library", "recipe_detail", "meal_wishlist", "meal_cart", "meal_calendar", "meal_day_detail", "meal_archive", "shopping_list"].includes(node.type)) { setMealMode(node.type === "shopping_list" ? "shopping" : node.type.includes("recipe") ? "recipes" : "calendar"); setSurface("life"); return; }
    if (["message_summary", "favorite_memory", "suggested_reply", "import_guide"].includes(node.type)) { setSurface("chat"); return; }
    if (["trip_countdown", "trip_share", "trip_variant_compare", "itinerary_timeline", "budget_summary", "hotel_quote", "risk_alert", "impact_diff", "approval_state"].includes(node.type)) { setSurface("trip"); return; }
    if (node.type === "quick_capture") { setWebPanel("app"); return; }
    setSurface("home");
  }

  async function previewSmartAction(node: UiNode) {
    const action = node.actions.find(item => item.requiresConfirmation);
    if (!action) return;
    try {
      const value = await client.preview(action.id);
      const summary = String(value.preview.before || "无") + " → " + String(value.preview.after || "") + "\n" + String(value.preview.impact || "");
      Alert.alert(String(value.preview.title || "确认建议"), summary, [
        { text: "取消", style: "cancel" },
        { text: "确认执行", onPress: () => { void client.confirm(action.id, "mobile:" + action.id + ":" + Date.now()).then(async () => { await refresh(); if (action.type === "create_calendar_entry" || action.type === "create_wishlist_item") setSurface("life"); else if (action.type === "create_moment") setSurface("timeline"); else if (action.type === "send_message") setSurface("chat"); else if (action.type === "select_trip_variant") setSurface("trip"); Alert.alert("已完成", "内容已保存，可在对应页面查看。"); }).catch(error => Alert.alert("执行失败", error.message)); } }
      ]);
    } catch (error) { Alert.alert("无法预览", (error as Error).message); }
  }

  const renderSmartCards = () => manifest ? <View style={s.smartSection}><Text style={s.smartTitle}>✦ 问恋屿 · 点卡片查看真实内容</Text>{[...manifest.sections, ...manifest.personalOverlay].map(node => { const writeAction = node.actions.find(action => action.requiresConfirmation); return <Card key={node.id}><TouchableOpacity accessibilityRole="button" onPress={() => void openSmartNode(node)}><Text style={s.title}>{String(node.props.title || node.type)}</Text><Text style={s.sub}>{String(node.props.subtitle || "")}</Text></TouchableOpacity>{writeAction && <Button quiet title="预览并确认操作" onPress={() => void previewSmartAction(node)}/>}</Card>; })}</View> : null;

  const daysTogether = useMemo(() => data?.couple ? Math.max(0, Math.floor((Date.now() - new Date(`${data.couple.anniversary}T00:00:00`).getTime()) / 86400000)) : 0, [data?.couple]);

  if (loginRequired || (!data && !token)) return <SafeAreaView style={s.safe}><View style={s.login}><Text style={s.brand}>恋屿 · LOVE MOMENTS</Text><Text style={s.loginTitle}>{authMode === "login" ? "登录共同小岛" : "创建恋屿账号"}</Text>{authMode === "register" && <Field value={nickname} onChangeText={setNickname} placeholder="昵称" />}<Field value={username} onChangeText={setUsername} placeholder="账号"/><Field value={password} onChangeText={setPassword} secureTextEntry placeholder="密码至少 8 位"/><Button title={authMode === "login" ? "登录" : "注册"} onPress={() => void authenticate()}/><Button quiet title={authMode === "login" ? "还没有账号？注册" : "已有账号？登录"} onPress={() => setAuthMode(authMode === "login" ? "register" : "login")}/></View></SafeAreaView>;

  if (!API) return <SafeAreaView style={s.safe}><ScrollView contentContainerStyle={s.login}><Text style={s.brand}>恋屿</Text><Text style={s.loginTitle}>需要设置服务器地址</Text><Text style={s.note}>构建前请设置 EXPO_PUBLIC_API_URL 为两台设备都能访问的 HTTPS 地址，例如你的 Tailscale 或 Sakura 映射地址。应用不会再把手机错误地连接到 127.0.0.1。</Text></ScrollView></SafeAreaView>;
  if (data?.needs_pairing) return <SafeAreaView style={s.safe}><ScrollView contentContainerStyle={s.login}><Text style={s.brand}>PAIR WITH YOUR LOVE</Text><Text style={s.loginTitle}>组成情侣账号</Text><Text style={s.note}>两个人分别注册。一人生成邀请码，另一人输入邀请码并确认正式在一起的日期。</Text><Button title="生成我的邀请码" onPress={() => void client.createInvite().then(value => Alert.alert("邀请码", `${value.code}\n24 小时内有效，只能使用一次`)).catch(error => Alert.alert("失败", error.message))}/><Field value={invite} onChangeText={setInvite} placeholder="输入伴侣邀请码"/><Field value={pairAnniversary} onChangeText={setPairAnniversary} placeholder="在一起日期 YYYY-MM-DD"/><Button quiet title="接受邀请" onPress={() => void pair()}/></ScrollView></SafeAreaView>;

  const renderHome = () => <><View style={s.hero}><Text style={s.heroSmall}>OUR STORY</Text><Text style={s.heroTitle}>一起走过 {daysTogether.toLocaleString()} 天</Text><Text style={s.heroSub}>{data?.partner?.nickname} 与 {data?.user.nickname} 的共同空间</Text></View><Card><Text style={s.title}>账号与数据</Text><Text style={s.sub}>登录令牌保存在系统安全存储中；设备、ZIP 导出、解绑和注销都在受保护的账号中心完成。</Text><Button quiet title="打开账号中心" onPress={() => setWebPanel("account")}/><Button quiet title="退出当前设备" onPress={() => void logout()}/></Card></>;
  const renderChat = () => <><View style={s.row}><Text style={s.pageTitle}>与 {data?.partner?.nickname}</Text><Button title="语音" onPress={() => setWebPanel("call")}/></View>{(data?.messages || []).slice(-50).map(item => <TouchableOpacity key={item.id} onLongPress={() => void client.messageToMoment(item.id).then(() => Alert.alert("已收藏", "这段聊天已进入时光")).catch(error => Alert.alert("失败", error.message))} style={[s.bubble, item.mine && s.mine]}><Text style={item.mine ? s.white : s.ink}>{item.content}</Text><Text style={[s.tiny, item.mine && s.light]}>长按收藏 · {new Date(item.sent_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</Text></TouchableOpacity>)}<View style={s.row}><TextInput style={[s.input, s.grow]} value={message} onChangeText={setMessage} placeholder="和 TA 说点什么"/><Button title="发送" onPress={() => void send()}/></View><Card><Text style={s.title}>导入外部聊天</Text><Field value={ocrText} onChangeText={setOcrText} placeholder="粘贴聊天文字；截图可在完整 H5 上传"/><Button quiet title="生成可编辑 OCR 草稿" onPress={() => void makeOcrDraft()}/>{ocrDraft && <><Text style={s.warning}>{ocrDraft.mock ? "演示识别，请逐条检查" : "OCR 草稿"}</Text>{(ocrDraft.items || []).map((item: any, index: number) => <View key={item.id} style={s.row}><TouchableOpacity onPress={() => { const next = { ...ocrDraft, items: [...ocrDraft.items] }; next.items[index] = { ...item, speaker: item.speaker === "me" ? "partner" : "me" }; setOcrDraft(next); }}><Text style={s.pill}>{item.speaker === "me" ? "我" : "对方"}</Text></TouchableOpacity><TextInput style={[s.input, s.grow]} value={item.content} onChangeText={text => { const next = { ...ocrDraft, items: [...ocrDraft.items] }; next.items[index] = { ...item, content: text }; setOcrDraft(next); }}/></View>)}<Button title="确认导入" onPress={() => void confirmOcr()}/></>}</Card></>;
  const renderTrip = () => <>
    <Text style={s.pageTitle}>我们的旅行</Text>
    {(data?.trips || []).map((item, index) => <Card key={item.id}>
      <Text style={s.title}>{item.title}</Text>
      <Text style={s.sub}>{item.start_date}—{item.end_date} · ¥{item.budget_cents / 100} · {item.status}</Text>
      {item.needs_destination_detail && <Text style={s.warning}>具体地图地点尚未确认，现有行程可能遇到异地同名；请在完整 H5 的旅行设置中确认后重新生成。</Text>}
      <Text style={s.tiny}>五套方案：{item.variants?.length || 0}</Text>
      <View style={s.tabs}><Button quiet title="查看路线" onPress={() => void client.tripRoute(item.id).then(value => { setTripTool(`${value.mock ? "模拟" : "高德"}路线 · ${Math.round(Number(value.distance_meters) / 100) / 10} km · ${Math.round(Number(value.duration_seconds) / 60)} 分钟`); setQuoteItems([]); }).catch(error => Alert.alert("路线失败", error.message))}/></View>
      {index === 0 && <>
        <Text style={s.sectionTitle}>查询真实价格并选择</Text>
        <View style={s.tabs}>{(["hotel", "flight", "train"] as const).map(type => <TouchableOpacity key={type} style={[s.pillButton, quoteType === type && s.pillActive]} onPress={() => { setQuoteType(type); setQuoteItems([]); }}><Text style={s.wine}>{type === "hotel" ? "酒店" : type === "flight" ? "机票" : "火车票"}</Text></TouchableOpacity>)}</View>
        {quoteType !== "hotel" && <Field value={quoteForm.origin} onChangeText={origin => setQuoteForm({ ...quoteForm, origin })} placeholder={quoteType === "flight" ? "出发城市或机场代码" : "出发站，例如杭州东"}/>}
        <Field value={quoteForm.destination} onChangeText={destination => setQuoteForm({ ...quoteForm, destination })} placeholder={quoteType === "hotel" ? `目的地（默认 ${item.destination}）` : quoteType === "flight" ? `到达城市或机场（默认 ${item.destination}）` : "到达站，例如上海虹桥"}/>
        {quoteType !== "hotel" && <Field value={quoteForm.outbound} onChangeText={outbound => setQuoteForm({ ...quoteForm, outbound })} placeholder="YYYY-MM-DD"/>}
        {quoteType === "flight" && <Field value={quoteForm.returnDate} onChangeText={returnDate => setQuoteForm({ ...quoteForm, returnDate })} placeholder="返程 YYYY-MM-DD（可选）"/>}
        <Button disabled={quoteBusy} title={quoteBusy ? "查询中…" : `查询${quoteType === "hotel" ? "酒店" : quoteType === "flight" ? "航班" : "车次与余票"}`} onPress={() => void searchQuotes(item)}/>
      </>}
    </Card>)}
    {tripTool ? <Card><Text style={s.title}>路线结果</Text><Text style={s.sub}>{tripTool}</Text></Card> : null}
    {quoteItems.length > 0 && <View><Text style={s.note}>{quoteNotice} · 共 {quoteItems.length} 条</Text>{quoteItems.map(item => <Card key={item.id}>
      <View style={s.row}><Text style={[s.pill, s.grow]}>{item.source_mode === "mock" ? "模拟" : item.source_mode === "aggregate" ? "聚合参考" : "实时查询"}</Text><Text style={s.quotePrice}>¥{(item.amount_cents / 100).toLocaleString("zh-CN")}</Text></View>
      <Text style={s.title}>{item.title}</Text><Text style={s.sub}>{item.subtitle}</Text><Text style={s.sub}>{item.terms}</Text>
      {item.details?.seats?.map(seat => <Text key={`${item.id}-${seat.name}`} style={s.tiny}>{seat.name} · ¥{seat.price_cents / 100} · 余 {seat.remaining}</Text>)}
      <Text style={s.tiny}>来源：{item.provider} · 查询于 {new Date(item.fetched_at).toLocaleString("zh-CN")}</Text>
      <View style={s.tabs}><Button quiet disabled={Boolean(item.selected_at)} title={item.selected_at ? "已加入本次旅行" : "选择这个"} onPress={() => void selectQuote(item)}/>{item.booking_url ? <Button quiet title="跳转查看 / 购买" onPress={() => void Linking.openURL(item.booking_url!).catch(() => Alert.alert("无法打开", "请检查购买链接"))}/>: null}</View>
    </Card>)}</View>}
    <Card><Text style={s.title}>创建任意目的地</Text><Text style={s.sub}>先确认地图地点，避免把异地同名景区混在一起。</Text><Field value={tripForm.destination} onChangeText={destination => { setTripForm({ ...tripForm, destination }); setSelectedPlace(null); setPlaceCandidates([]); }} placeholder="地点名称，例如迪士尼、西湖"/><Field value={tripForm.region_hint} onChangeText={region_hint => { setTripForm({ ...tripForm, region_hint }); setSelectedPlace(null); setPlaceCandidates([]); }} placeholder="国家 / 城市提示，例如上海、东京"/><Button quiet title="查找并确认具体地点" onPress={() => void searchTripPlaces()}/>{placeCandidates.map(item => <TouchableOpacity key={item.candidate_id} style={[s.mealNativeRow, selectedPlace?.candidate_id === item.candidate_id && s.pillActive]} onPress={() => setSelectedPlace(item)}><Text style={s.title}>{selectedPlace?.candidate_id === item.candidate_id ? "✓ " : ""}{item.name}</Text><Text style={s.tiny}>{[item.country, item.province, item.city, item.district, item.formatted_address].filter(Boolean).join(" · ")} · {item.provider === "amap" ? "高德" : item.provider === "google_maps" ? "Google Maps" : "本人确认"}</Text></TouchableOpacity>)}<View style={s.row}><TextInput style={[s.input, s.grow]} value={tripForm.start_date} onChangeText={start_date => setTripForm({ ...tripForm, start_date })} placeholder="YYYY-MM-DD"/><TextInput style={[s.input, s.grow]} value={tripForm.end_date} onChangeText={end_date => setTripForm({ ...tripForm, end_date })} placeholder="YYYY-MM-DD"/></View><Field value={tripForm.budget} onChangeText={budget => setTripForm({ ...tripForm, budget })} placeholder="预算（元）"/><Button title="生成五套方案" onPress={() => void createTrip()}/></Card>
  </>;
  const renderTimeline = () => <><Text style={s.pageTitle}>我们的时光</Text><View style={s.tabs}><Button quiet title="时间线" onPress={() => setTimelineMode("moments")}/><Button quiet title="月份" onPress={() => void client.timelineMonths().then(value => { setTimelineItems(value.items); setTimelineMode("months"); })}/><Button quiet title="足迹" onPress={() => void client.footprints().then(value => { setTimelineItems(value.items); setTimelineMode("map"); })}/><Button quiet title="仅我" onPress={() => setTimelineMode("private")}/><Button quiet title="回收站" onPress={() => void client.momentTrash().then(value => { setTrash(value.items); setTimelineMode("trash"); })}/><Button quiet title="年度报告" onPress={() => void client.annualReport(new Date().getFullYear()).then(value => { setReport(value); setTimelineMode("report"); })}/></View>{timelineMode === "moments" && (data?.moments || []).map(item => <Card key={item.id}><Text style={s.title}>{item.title}</Text><Text style={s.sub}>{item.body}</Text><Button quiet title="移入回收站" onPress={() => void client.trashMoment(item.id).then(refresh)}/></Card>)}{timelineMode === "private" && (data?.moments || []).filter(item => item.creator_id === data?.user.id && item.visibility !== "shared").map(item => <Card key={item.id}><Text style={s.title}>{item.title}</Text><Text style={s.sub}>{item.body} · 仅自己可见</Text></Card>)}{timelineMode === "months" && timelineItems.map((item, index) => <Card key={String(item.month || index)}><Text style={s.title}>{String(item.month)}</Text><Text style={s.sub}>{String(item.count)} 个瞬间 · {String(item.media_count)} 个媒体记录</Text></Card>)}{timelineMode === "map" && timelineItems.map((item, index) => <Card key={String(item.name || index)}><Text style={s.title}>{String(item.name)}</Text><Text style={s.sub}>{Number(item.latitude).toFixed(4)}, {Number(item.longitude).toFixed(4)} · {String(item.coordinate_source)}</Text></Card>)}{timelineMode === "trash" && trash.map(item => <Card key={item.id}><Text style={s.title}>{item.title}</Text><Button quiet title="恢复" onPress={() => void client.restoreMoment(item.id, item.version).then(async () => { setTrash((await client.momentTrash()).items); await refresh(); })}/></Card>)}{timelineMode === "report" && report && <Card><Text style={s.title}>{String(report.year)} 年度报告</Text><Text style={s.sub}>{String(report.moments)} 个瞬间 · {String(report.messages)} 条消息 · {String(report.trips)} 次旅行 · {Math.round(Number(report.call_seconds || 0) / 60)} 分钟通话</Text></Card>}</>;
  const renderMealWorkspace = () => <Card><Text style={s.title}>今天想吃 · 原生点菜</Text><Text style={s.sub}>中文菜谱、单日菜单和采购勾选会与另一方实时同步。</Text><View style={s.tabs}>{(["recipes", "calendar", "shopping"] as const).map(mode => <TouchableOpacity key={mode} style={[s.pillButton, mealMode === mode && s.pillActive]} onPress={() => setMealMode(mode)}><Text style={s.wine}>{mode === "recipes" ? "菜谱" : mode === "calendar" ? "日历" : "采购"}</Text></TouchableOpacity>)}</View>
    {mealMode === "recipes" && <><View style={s.row}><TextInput style={[s.input, s.grow]} value={recipeSearch} onChangeText={setRecipeSearch} placeholder="搜索中文菜谱"/><Button quiet title="搜索" onPress={() => void loadMealData()}/></View>{recipeDetail ? <Card><Text style={s.title}>{recipeDetail.name}</Text><Text style={s.sub}>{recipeDetail.cuisine === "western" ? "西餐" : "中餐"} · 准备 {recipeDetail.prep_minutes} 分钟 · 烹饪 {recipeDetail.cook_minutes} 分钟{recipeDetail.passive_minutes ? ` · 等待 ${stepDuration(recipeDetail.passive_minutes * 60)}` : ""} · {recipeDetail.base_servings} 人份</Text>{(["main", "seasoning"] as const).map(group => <View key={group}><Text style={s.sectionTitle}>{group === "main" ? "主要食材" : "辅料与调味"}</Text>{recipeDetail.ingredients.filter(item => (item.ingredient_group || "main") === group).map((item, index) => <View key={item.id || `${group}-${index}`} style={s.ingredientLine}><Text style={s.ingredientName}>• {item.name} · {item.display_quantity || `${item.quantity || ""}${item.unit || ""}`}{item.optional ? "（可选）" : ""}</Text>{item.preparation ? <Text style={s.tiny}>处理：{item.preparation}</Text> : null}</View>)}</View>)}<Text style={s.sectionTitle}>详细步骤</Text>{recipeDetail.steps.map((item, index) => <View key={item.id || index} style={s.stepCard}><Text style={s.stepTitle}>{index + 1}. {item.title || `第 ${index + 1} 步`}</Text><Text style={s.sub}>{item.instruction}</Text><Text style={s.stepMeta}>{[item.heat_level ? `火力：${item.heat_level}` : "", item.timer_seconds ? `约 ${stepDuration(item.timer_seconds)}` : "无需计时"].filter(Boolean).join(" · ")}</Text>{item.key_point ? <Text style={s.keyPoint}>完成判断：{item.key_point}</Text> : null}</View>)}<Text style={s.sectionTitle}>过敏原提示（按配料识别）</Text>{recipeDetail.allergen_details?.length ? recipeDetail.allergen_details.map(item => <View key={item.name} style={s.ingredientLine}><Text style={s.ingredientName}>{item.name}</Text><Text style={s.tiny}>来源：{item.sources.join("、")}</Text></View>) : <Text style={s.sub}>当前配料表未识别到常见过敏原</Text>}{recipeDetail.allergen_review_ingredients?.length ? <Text style={s.keyPoint}>请核对包装标签：{recipeDetail.allergen_review_ingredients.join("、")}属于复合或加工食品，不同品牌配方可能不同。</Text> : null}<Text style={s.tiny}>{recipeDetail.allergen_note || "豆角、红豆、绿豆、豌豆等不会自动归为大豆；复合调味料以包装标签为准。"}</Text><Field value={mealDate} onChangeText={setMealDate} placeholder="YYYY-MM-DD"/><View style={s.tabs}>{(["breakfast", "lunch", "dinner", "snack"] as const).map(slot => <TouchableOpacity key={slot} style={[s.pillButton, mealSlot === slot && s.pillActive]} onPress={() => setMealSlot(slot)}><Text>{slot === "breakfast" ? "早餐" : slot === "lunch" ? "午餐" : slot === "dinner" ? "晚餐" : "加餐"}</Text></TouchableOpacity>)}</View><Button title="安排到用餐日历" onPress={() => void client.scheduleMeal({ recipe_id: recipeDetail.id, planned_date: mealDate, meal_slot: mealSlot, servings: 2, idempotency_key: `mobile:meal:${Date.now()}` }).then(async () => { await loadMealData(); setMealMode("calendar"); }).catch(error => Alert.alert("安排失败", error.message))}/><Button quiet title="返回菜谱列表" onPress={() => setRecipeDetail(null)}/></Card> : recipes.map(item => <TouchableOpacity key={item.id} style={s.mealNativeRow} onPress={() => void client.recipe(item.id).then(setRecipeDetail).catch(error => Alert.alert("读取失败", error.message))}><Text style={s.title}>{item.name}</Text><Text style={s.tiny}>{item.cuisine === "western" ? "西餐" : "中餐"} · 准备 {item.prep_minutes} 分钟 · 烹饪 {item.cook_minutes} 分钟{item.passive_minutes ? ` · 等待 ${stepDuration(item.passive_minutes * 60)}` : ""} · 查看食材和做法 ›</Text></TouchableOpacity>)}</>}
    {mealMode === "calendar" && <><Field value={mealDate} onChangeText={setMealDate} placeholder="YYYY-MM-DD"/>{(mealCalendar?.days[0] ? Object.entries(mealCalendar.days[0].meals).flatMap(([slot, items]) => items.map(item => <View key={item.id} style={s.mealNativeRow}><Text style={s.title}>{slot === "breakfast" ? "早餐" : slot === "lunch" ? "午餐" : slot === "dinner" ? "晚餐" : "加餐"} · {item.name}</Text><Text style={s.tiny}>{item.servings} 人份 · {item.status}</Text>{item.status === "selected" && <Button quiet title="确认这道菜" onPress={() => void client.updateMeal(item.id, { status: "accepted", expected_version: item.version }).then(loadMealData)}/>}</View>)) : [])}{mealCart.items.length === 0 && <Text style={s.note}>这一天还没有安排。</Text>}</>}
    {mealMode === "shopping" && <>{shopping.items.length ? shopping.items.map(item => <TouchableOpacity key={item.id} style={s.mealNativeRow} onPress={() => void client.updateShoppingItem(item.id, { checked: !item.checked, expected_version: item.version }).then(loadMealData).catch(error => Alert.alert("同步失败", error.message))}><Text style={[s.title, item.checked && s.doneText]}>{item.checked ? "✓ " : "○ "}{item.display_name} · {item.display_quantity}</Text><Text style={s.tiny}>{item.category}{item.manual_locked ? " · 手动锁定" : ""}</Text></TouchableOpacity>) : <Text style={s.note}>H5 中确认采购范围后，清单会显示在这里。</Text>}</>}
  </Card>;
  const renderLife = () => <><Text style={s.pageTitle}>共同生活</Text>{renderMealWorkspace()}{(data?.calendar || []).map(item => <Card key={item.id}><Text style={s.title}>{item.title}</Text><Text style={s.sub}>{item.entry_type} · {item.starts_at.slice(0, 10)} {item.completed_at ? "· 已完成" : ""}</Text><View style={s.tabs}>{item.entry_type === "todo" && <Button quiet title={item.completed_at ? "恢复待办" : "标记完成"} onPress={() => void client.completeCalendar(item.id, !item.completed_at, item.version).then(refresh)}/>}<Button quiet title="编辑" onPress={() => { setEditingLife(item); setLifeForm({ title: item.title, date: item.starts_at.slice(0, 10), type: item.entry_type }); }}/><Button quiet title="删除" onPress={() => void client.deleteCalendar(item.id, item.version).then(refresh).catch(error => Alert.alert("删除失败", error.message))}/></View></Card>)}<Card><Text style={s.title}>{editingLife ? "编辑共同事项" : "添加日历 / 纪念日 / 待办"}</Text><Field value={lifeForm.title} onChangeText={title => setLifeForm({ ...lifeForm, title })} placeholder="标题"/><View style={s.tabs}>{["todo", "event", "anniversary"].map(type => <TouchableOpacity key={type} style={s.pillButton} onPress={() => setLifeForm({ ...lifeForm, type })}><Text>{type === "todo" ? "待办" : type === "event" ? "日历" : "纪念日"}</Text></TouchableOpacity>)}</View><Field value={lifeForm.date} onChangeText={date => setLifeForm({ ...lifeForm, date })} placeholder="YYYY-MM-DD"/><Button title={editingLife ? "保存修改" : "保存共同事项"} onPress={() => void saveLife()}/>{editingLife && <Button quiet title="取消编辑" onPress={() => { setEditingLife(null); setLifeForm(value => ({ ...value, title: "" })); }}/>}</Card>{(data?.preferences || []).map(item => <Card key={item.id}><Text style={s.title}>{item.subject_name} · {item.label}</Text><Text style={s.sub}>{item.category} · {item.value}</Text><View style={s.tabs}><Button quiet title="编辑" onPress={() => { setEditingPreference(item); setPrefForm({ label: item.label, value: item.value }); }}/><Button quiet title="删除" onPress={() => void client.deletePreference(item.id, item.version).then(refresh).catch(error => Alert.alert("删除失败", error.message))}/></View></Card>)}<Card><Text style={s.title}>{editingPreference ? "编辑偏好" : "记录对方偏好"}</Text><Field value={prefForm.label} onChangeText={label => setPrefForm({ ...prefForm, label })} placeholder="名称，例如奶茶甜度"/><Field value={prefForm.value} onChangeText={value => setPrefForm({ ...prefForm, value })} placeholder="内容，例如三分糖"/><Button title={editingPreference ? "保存修改" : "保存偏好"} onPress={() => void savePreference()}/>{editingPreference && <Button quiet title="取消编辑" onPress={() => { setEditingPreference(null); setPrefForm({ label: "", value: "" }); }}/>}</Card></>;
  const content = surface === "home" ? renderHome() : surface === "chat" ? renderChat() : surface === "trip" ? renderTrip() : surface === "timeline" ? renderTimeline() : renderLife();
  const injected = `window.__LOVE_TOKEN=${JSON.stringify(token)};document.cookie='lm_session='+encodeURIComponent(window.__LOVE_TOKEN)+'; Path=/; SameSite=Lax';const _f=window.fetch;window.fetch=(i,o={})=>_f(i,{...o,headers:{...(o.headers||{}),Authorization:'Bearer '+window.__LOVE_TOKEN}});true;`;

  return <SafeAreaView style={s.safe}><ScrollView contentContainerStyle={s.body}><Text style={s.brand}>恋屿 · LOVE MOMENTS</Text>{renderSmartCards()}{content}</ScrollView><View style={s.nav}>{surfaces.map(item => <TouchableOpacity key={item} onPress={() => setSurface(item)} accessibilityRole="tab"><Text style={item === surface ? s.active : s.inactive}>{labels[item]}</Text></TouchableOpacity>)}</View><Modal visible={Boolean(webPanel)} animationType="slide" onRequestClose={() => setWebPanel(null)}><SafeAreaView style={s.safe}><Button quiet title={webPanel === "call" ? "关闭通话页" : webPanel === "account" ? "关闭账号中心" : "关闭完整功能页"} onPress={() => setWebPanel(null)}/>{webPanel && <WebView source={{ uri: `${API}/?screen=${webPanel === "call" ? "chat" : "home"}${webPanel === "account" ? "&account=1" : ""}` }} injectedJavaScriptBeforeContentLoaded={injected} mediaPlaybackRequiresUserAction={false} allowsInlineMediaPlayback />}</SafeAreaView></Modal></SafeAreaView>;
}

const s = StyleSheet.create({
  safe: { flex: 1, backgroundColor: "#f8f2ec" }, body: { padding: 20, paddingBottom: 96 }, brand: { color: "#7d2638", fontWeight: "700", letterSpacing: 2, marginBottom: 18 }, pageTitle: { fontSize: 27, fontWeight: "700", color: "#2a2020", marginBottom: 14 },
  hero: { padding: 26, borderRadius: 24, backgroundColor: "#7d2638", marginBottom: 14 }, heroSmall: { color: "#eecfd2", fontSize: 10, letterSpacing: 2 }, heroTitle: { color: "white", fontSize: 27, fontWeight: "700", marginTop: 8 }, heroSub: { color: "#eecfd2", marginTop: 8 },
  card: { marginBottom: 10, padding: 16, borderRadius: 17, backgroundColor: "#fffaf6" }, title: { fontWeight: "700", color: "#2a2020" }, sectionTitle: { fontWeight: "700", color: "#2a2020", marginTop: 16, marginBottom: 8 }, quotePrice: { color: "#7d2638", fontWeight: "700", fontSize: 18 }, sub: { marginTop: 5, color: "#8d7b78", lineHeight: 20 }, tiny: { marginTop: 6, fontSize: 10, color: "#8d7b78" }, light: { color: "#f0dfe2" }, ink: { color: "#2a2020" }, warning: { color: "#7d2638", marginVertical: 8 }, smartSection: { marginBottom: 12 }, smartTitle: { color: "#7d2638", fontWeight: "700", marginBottom: 8 },
  row: { flexDirection: "row", gap: 8, alignItems: "center" }, grow: { flex: 1 }, tabs: { flexDirection: "row", gap: 6, marginBottom: 10, flexWrap: "wrap" }, pillButton: { backgroundColor: "#ead6d8", padding: 9, borderRadius: 99 }, pillActive: { borderWidth: 2, borderColor: "#7d2638" }, pill: { backgroundColor: "#ead6d8", padding: 9, borderRadius: 9, color: "#7d2638" },
  button: { padding: 12, borderRadius: 12, backgroundColor: "#7d2638", marginTop: 8 }, quietButton: { backgroundColor: "#ead6d8" }, disabled: { opacity: .5 }, white: { color: "white", textAlign: "center" }, wine: { color: "#7d2638", textAlign: "center" },
  input: { padding: 13, borderRadius: 13, backgroundColor: "#fffaf6", marginBottom: 9, borderWidth: 1, borderColor: "#eaded7" }, bubble: { alignSelf: "flex-start", maxWidth: "82%", padding: 13, borderRadius: 16, backgroundColor: "#fffaf6", marginBottom: 8 }, mine: { alignSelf: "flex-end", backgroundColor: "#7d2638" },
  nav: { position: "absolute", left: 0, right: 0, bottom: 0, flexDirection: "row", justifyContent: "space-around", padding: 16, backgroundColor: "#fffaf6" }, active: { color: "#7d2638", fontWeight: "700" }, inactive: { color: "#8d7b78" },
  mealNativeRow: { padding: 13, marginTop: 8, borderRadius: 14, borderWidth: 1, borderColor: "#eaded7", backgroundColor: "#fffaf6" }, doneText: { color: "#8d7b78", textDecorationLine: "line-through" },
  ingredientLine: { paddingVertical: 7, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: "#eaded7" }, ingredientName: { color: "#2a2020", fontWeight: "600" },
  stepCard: { padding: 13, marginTop: 9, borderRadius: 13, backgroundColor: "#f8eeea" }, stepTitle: { color: "#7d2638", fontWeight: "700" }, stepMeta: { marginTop: 8, color: "#7d2638", fontSize: 12 }, keyPoint: { marginTop: 7, color: "#5e4c49", fontSize: 12, lineHeight: 18 },
  login: { flexGrow: 1, justifyContent: "center", padding: 28 }, loginTitle: { marginVertical: 24, fontSize: 28, color: "#2a2020", fontWeight: "700" }, note: { color: "#8d7b78", lineHeight: 22, marginBottom: 12 }
});
