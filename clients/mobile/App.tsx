import React, { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Linking, Modal, SafeAreaView, ScrollView, StyleSheet, Text, TextInput, TouchableOpacity, View } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import * as SecureStore from "expo-secure-store";
import { WebView } from "react-native-webview";
import { LoveMomentsClient, createTripToolsInFlightGuard, formatTripExpenseCents, parseTripExpenseAmount, sanitizeDecisions, validateTripOfflineSnapshot, type CalendarEntry, type CalendarRecurrence, type CoupleContext, type DatePlan, type Decision, type DecisionConversionPreview, type ExpressionSuggestion, type FoodProfile, type MealCalendar, type MealCart, type MessageItem, type MomentItem, type PantryItem, type PartnerPreference, type PlaceCandidate, type QuoteItem, type RecipeDetail, type RecipeSummary, type ShoppingList, type StatusCode, type TripEmergencyItem, type TripExpenseList, type TripOfflineIdentity, type TripOfflineSnapshot, type TripPackingItem, type TripSummary, type TripToolsRequestToken, type WeeklyMealPlanDraft, type WishlistItem } from "@love-moments/domain-client";
import { surfaces, type Surface, type UiManifest, type UiNode } from "@love-moments/ui-contract";
import { AppState } from "react-native";
import { createPrivateCache, type CacheIdentity } from "@love-moments/domain-client";

const API = (process.env.EXPO_PUBLIC_API_URL || "").trim().replace(/\/$/, "");
const ACCESS_TOKEN_KEY = "love-access-token";
const CACHE_IDENTITY_KEY = "love-cache-identity";
const COOK_RECIPE_ROUTE_KEY = "cook_recipe";
const privateCache = createPrivateCache(AsyncStorage);
const manifestCache = {
  scope: privateCache.scope,
  async get(surface: Surface) { try { return JSON.parse(await privateCache.getItem(`ui:${surface}`) || "null"); } catch { return null; } },
  async set(surface: Surface, value: UiManifest) { await privateCache.setItem(`ui:${surface}`, JSON.stringify(value)); }
};
function localDateAfter(days: number) {
  const value = new Date();
  value.setDate(value.getDate() + days);
  return value.toLocaleDateString("sv-SE");
}
function calendarApiTime(value: string, allDay: boolean) {
  if (allDay || !value) return value || null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) throw new Error("日历时间格式无效");
  return parsed.toISOString();
}
const labels: Record<Surface, string> = { home: "首页", chat: "消息", trip: "旅行", timeline: "时光", life: "我们" };
type Person = { id: string; nickname: string; avatar_label?: string };
type Bootstrap = { needs_pairing?: boolean; permission_version?: number; user: Person; partner?: Person; couple?: { id: string; version: number; anniversary: string }; coupleContext?: CoupleContext; messages?: MessageItem[]; moments?: MomentItem[]; trip?: TripSummary; trips?: TripSummary[]; calendar?: CalendarEntry[]; preferences?: PartnerPreference[]; wishlist?: { title: string; items: WishlistItem[] }; decisions?: Decision[]; datePlans?: DatePlan[] };
type LifeFormState = { title: string; allDay: boolean; startsAt: string; endsAt: string; participantIds: string[]; blocksTime: boolean; type: CalendarEntry["entry_type"]; recurrence: CalendarRecurrence };
function emptyLifeForm(): LifeFormState { return { title: "", allDay: true, startsAt: localDateAfter(0), endsAt: "", participantIds: [], blocksTime: false, type: "todo", recurrence: "none" }; }

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
function provenanceLabel(value?: string) {
  return ({ human: "真人填写", real_api: "真实接口", rules: "规则推导", ai: "AI 建议", mock: "模拟数据", cache: "历史缓存", legacy_unknown: "来源未记录", unavailable: "暂无数据" } as Record<string, string>)[value || ""] || "来源未记录";
}
function mobileEmergencyLines(item: TripEmergencyItem) {
  const labels: Record<string, string> = { address: "地址", phone: "电话", check_in_at: "入住", check_out_at: "退房", front_desk_note: "前台说明", provider: "平台", booking_type: "订单类型", booking_reference: "订单号", support_phone: "客服电话", status_note: "订单说明", name: "姓名 / 地点", relationship: "关系", note: "说明", policy_number: "保单号", assistance_phone: "救援电话", coverage_summary: "保障摘要", valid_from: "生效日", valid_to: "失效日", transport_type: "交通方式", carrier: "承运方", service_number: "班次", departure_place: "出发地", departure_at: "出发时间", arrival_place: "到达地", arrival_at: "到达时间", verified_at: "核验时间" };
  return Object.entries(item.details || {}).map(([key, value]) => {
    const text = String(value ?? "");
    const safe = ["booking_reference", "policy_number"].includes(key) && text.length > 4 ? `${text.slice(0, 2)}••••${text.slice(-2)}` : text;
    return `${labels[key] || key}：${safe}`;
  });
}
function tripToolsIdentityKey(identity: TripOfflineIdentity | null) {
  return identity ? `${identity.user_id}:${identity.couple_id}:${identity.permission_version}` : "";
}
const statusLabels: Record<StatusCode, string> = { busy: "忙碌中", want_chat: "想聊天", need_company: "需要陪伴", feeling_low: "心情不好", moving: "在路上", home: "已到家", custom: "自定义" };

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
  const [lifeForm, setLifeForm] = useState<LifeFormState>(emptyLifeForm);
  const [editingLife, setEditingLife] = useState<CalendarEntry | null>(null);
  const [prefForm, setPrefForm] = useState({ label: "", value: "" });
  const [editingPreference, setEditingPreference] = useState<PartnerPreference | null>(null);
  const [wishText, setWishText] = useState("");
  const [wishKind, setWishKind] = useState<WishlistItem["item_kind"]>("general");
  const [editingWish, setEditingWish] = useState<WishlistItem | null>(null);
  const [statusCode, setStatusCode] = useState<StatusCode>("busy");
  const [statusText, setStatusText] = useState("");
  const [expressionSuggestion, setExpressionSuggestion] = useState<ExpressionSuggestion | null>(null);
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
  const [tripPacking, setTripPacking] = useState<TripPackingItem[]>([]);
  const [tripEmergency, setTripEmergency] = useState<TripEmergencyItem[]>([]);
  const [tripToolsTripId, setTripToolsTripId] = useState("");
  const [tripOfflineReadOnly, setTripOfflineReadOnly] = useState(false);
  const [tripOfflineSnapshot, setTripOfflineSnapshot] = useState<TripOfflineSnapshot | null>(null);
  const [tripExpenses, setTripExpenses] = useState<TripExpenseList | null>(null);
  const [tripExpenseTripId, setTripExpenseTripId] = useState("");
  const [tripExpenseAmount, setTripExpenseAmount] = useState("");
  const [tripExpenseCategory, setTripExpenseCategory] = useState<"transport" | "lodging" | "food" | "tickets" | "shopping" | "other">("food");
  const [tripExpenseSplit, setTripExpenseSplit] = useState<"equal" | "personal">("equal");
  const [tripExpensePayer, setTripExpensePayer] = useState("");
  const [tripExpenseBeneficiary, setTripExpenseBeneficiary] = useState("");
  const [tripExpenseDate, setTripExpenseDate] = useState(new Date().toLocaleDateString("sv-SE"));
  const [tripExpenseNote, setTripExpenseNote] = useState("");
  const [selectedTripExpenses, setSelectedTripExpenses] = useState<string[]>([]);
  const [webPanel, setWebPanel] = useState<"call" | "account" | "app" | "memory" | "album" | "meals" | "cook" | "trip-tools" | null>(null);
  const [cookRoute, setCookRoute] = useState("");
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
  const [foodProfiles, setFoodProfiles] = useState<FoodProfile[]>([]);
  const [pantryItems, setPantryItems] = useState<PantryItem[]>([]);
  const [weeklyPlan, setWeeklyPlan] = useState<WeeklyMealPlanDraft>({ id: null, status: "empty", items: [], warnings: [] });
  const [pantryName, setPantryName] = useState("");
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [datePlans, setDatePlans] = useState<DatePlan[]>([]);
  const [decisionSelections, setDecisionSelections] = useState<Record<string, string[]>>({});
  const [decisionPrefill, setDecisionPrefill] = useState<DecisionConversionPreview | null>(null);
  const mealEventCursor = useRef(0);
  const authEpoch = useRef(0);
  const manifestEpoch = useRef(0);
  const tripToolsGuard = useRef(createTripToolsInFlightGuard());
  const tripToolsIdentityRef = useRef<TripOfflineIdentity | null>(null);
  const tripToolsTargetRef = useRef("");
  const tripExpensesGuard = useRef(createTripToolsInFlightGuard());
  const tripExpensesTargetRef = useRef("");

  function currentTripToolsScope(tripId = tripToolsTargetRef.current) {
    return { authEpoch: authEpoch.current, identity: tripToolsIdentityKey(tripToolsIdentityRef.current), tripId };
  }
  function isTripToolsRequestCurrent(request: TripToolsRequestToken) {
    return tripToolsGuard.current.isCurrent(request, currentTripToolsScope());
  }
  function currentTripExpensesScope(tripId = tripExpensesTargetRef.current) {
    return { authEpoch: authEpoch.current, identity: tripToolsIdentityKey(tripToolsIdentityRef.current), tripId };
  }
  function isTripExpensesRequestCurrent(request: TripToolsRequestToken) {
    return tripExpensesGuard.current.isCurrent(request, currentTripExpensesScope());
  }

  async function clearPrivateState() {
    authEpoch.current++;
    tripToolsGuard.current.invalidate();
    tripExpensesGuard.current.invalidate();
    tripToolsIdentityRef.current = null;
    tripToolsTargetRef.current = "";
    tripExpensesTargetRef.current = "";
    await privateCache.setIdentity(null);
    await SecureStore.deleteItemAsync(CACHE_IDENTITY_KEY);
    await AsyncStorage.multiRemove((await AsyncStorage.getAllKeys()).filter(key => key.startsWith("love-private:") || key === "love-bootstrap" || key === "love-mobile-meals" || key.startsWith("ui:")));
    setMessage(""); setExpressionSuggestion(null); setOcrText(""); setOcrDraft(null); setManifest(undefined); setRecipes([]); setRecipeDetail(null);
    setData(undefined); setMealCalendar(undefined); setMealCart({ id: null, title: "我们的点菜车", status: "active", version: 0, items: [] }); setShopping({ id: null, title: "采购清单", status: "active", version: 0, items: [] }); setFoodProfiles([]); setPantryItems([]); setWeeklyPlan({ id: null, status: "empty", items: [], warnings: [] }); setPantryName(""); setDecisions([]); setDatePlans([]); setDecisionSelections({}); setDecisionPrefill(null);
    setLifeForm(emptyLifeForm()); setPrefForm({ label: "", value: "" }); setWishText(""); setWishKind("general"); setEditingWish(null); setEditingLife(null); setEditingPreference(null); setStatusText("");
    setTripForm({ destination: "", region_hint: "", start_date: localDateAfter(30), end_date: localDateAfter(32), budget: "4000" }); setPlaceCandidates([]); setSelectedPlace(null); setQuoteItems([]); setQuoteNotice(""); setTripTool(""); setTripPacking([]); setTripEmergency([]); setTripToolsTripId(""); setTripOfflineReadOnly(false); setTripOfflineSnapshot(null); setTripExpenses(null); setTripExpenseTripId(""); setTripExpenseAmount(""); setTripExpenseNote(""); setSelectedTripExpenses([]); setTrash([]); setReport(undefined); setTimelineItems([]); mealEventCursor.current = 0;
  }

  async function loadMealData() {
    const epoch = authEpoch.current;
    try {
      const [library, cart, calendar, list, profiles, pantry, weekly] = await Promise.all([client.recipes({ q: recipeSearch, limit: 50 }), client.mealCart(), client.mealCalendar(mealDate, mealDate), client.shoppingList(), client.foodProfiles(), client.pantryItems(), client.activeWeeklyMealPlan()]);
      if (epoch !== authEpoch.current) return;
      setRecipes(library.items); setMealCart(cart); setMealCalendar(calendar); setShopping(list); setFoodProfiles(profiles.profiles); setPantryItems(pantry.items); setWeeklyPlan(weekly);
      await privateCache.setItem("meals", JSON.stringify({ recipes: library.items, cart, calendar, list, profiles: profiles.profiles, pantry: pantry.items, weekly }));
    } catch (error) {
      if ((error as { status?: number }).status || epoch !== authEpoch.current) throw error;
      const cached = await privateCache.getItem("meals");
      if (cached) { const value = JSON.parse(cached); setRecipes(value.recipes || []); setMealCart(value.cart); setMealCalendar(value.calendar); setShopping(value.list); setFoodProfiles(value.profiles || []); setPantryItems(value.pantry || []); setWeeklyPlan(value.weekly || { id: null, status: "empty", items: [], warnings: [] }); }
      else throw error;
    }
  }

  async function refresh() {
    let epoch = authEpoch.current;
    try {
      const value = await client.bootstrap<Bootstrap>();
      if (epoch !== authEpoch.current) return;
      const identity: CacheIdentity = { user_id: value.user.id, couple_id: value.couple?.id || "unpaired", permission_version: value.permission_version ?? value.couple?.version ?? 0 };
      const previous = await SecureStore.getItemAsync(CACHE_IDENTITY_KEY);
      if (epoch !== authEpoch.current) return;
      if (previous && previous !== JSON.stringify(identity)) { await clearPrivateState(); epoch = authEpoch.current; }
      await privateCache.setIdentity(identity);
      if (epoch !== authEpoch.current) return;
      await SecureStore.setItemAsync(CACHE_IDENTITY_KEY, JSON.stringify(identity));
      if (epoch !== authEpoch.current) return;
      const nextTripIdentity = identity as TripOfflineIdentity;
      if (tripToolsIdentityKey(tripToolsIdentityRef.current) !== tripToolsIdentityKey(nextTripIdentity)) { tripToolsGuard.current.invalidate(); tripExpensesGuard.current.invalidate(); }
      tripToolsIdentityRef.current = nextTripIdentity;
      const safeDecisions = sanitizeDecisions(value.decisions);
      const safeValue = { ...value, decisions: safeDecisions };
      setData(safeValue); setDecisions(safeDecisions); setDatePlans(value.datePlans || []); setLoginRequired(false); await privateCache.setItem("bootstrap", JSON.stringify(safeValue));
      if (!value.needs_pairing) void loadManifestForSurface(surface);
    } catch (error) {
      const status = (error as Error & { status?: number }).status;
      if (status === 401 || status === 403) { await clearPrivateState(); setLoginRequired(true); }
      else { const cached = status ? null : await privateCache.getItem("bootstrap"); if (cached) { const value = JSON.parse(cached) as Bootstrap; const safeDecisions = sanitizeDecisions(value.decisions); setData({ ...value, decisions: safeDecisions }); setDecisions(safeDecisions); setDatePlans(value.datePlans || []); } else Alert.alert("无法同步", (error as Error).message); }
    }
  }
  async function loadManifestForSurface(nextSurface: Surface) {
    const requestEpoch = ++manifestEpoch.current;
    const accountEpoch = authEpoch.current;
    try {
      const nextManifest = await client.getManifest(nextSurface);
      if (requestEpoch !== manifestEpoch.current || accountEpoch !== authEpoch.current) return;
      setManifest(nextManifest);
    } catch { /* Stable local content remains visible while a fresh manifest is unavailable. */ }
  }
  useEffect(() => { void (async () => {
    if (!API) { setLoginRequired(true); return; }
    let value = await SecureStore.getItemAsync(ACCESS_TOKEN_KEY);
    if (!value) {
      value = await AsyncStorage.getItem(ACCESS_TOKEN_KEY);
      if (value) await SecureStore.setItemAsync(ACCESS_TOKEN_KEY, value);
      await AsyncStorage.removeItem(ACCESS_TOKEN_KEY);
    }
    if (value) {
      const saved = await SecureStore.getItemAsync(CACHE_IDENTITY_KEY);
      if (saved) { try { const identity = JSON.parse(saved) as TripOfflineIdentity; await privateCache.setIdentity(identity); tripToolsIdentityRef.current = identity; } catch { await clearPrivateState(); } }
    } else await clearPrivateState();
    await AsyncStorage.multiRemove(["love-bootstrap", "love-mobile-meals", ...surfaces.map(item => `ui:${item}`)]);
    setToken(value || ""); client.setAccessToken(value || undefined); await refresh();
  })(); }, []);
  useEffect(() => { if (data && !data.needs_pairing) void loadManifestForSurface(surface); }, [surface]);
  useEffect(() => { const subscription = AppState.addEventListener("change", next => { if (next === "active" && token) void refresh(); }); return () => subscription.remove(); }, [token]);
  useEffect(() => {
    if (!token) return;
    const now = Date.now();
    const nextExpiry = Math.min(...(data?.coupleContext?.members || [])
      .map(member => member.expires_at ? new Date(member.expires_at).getTime() : Number.POSITIVE_INFINITY)
      .filter(value => Number.isFinite(value) && value > now));
    if (!Number.isFinite(nextExpiry)) return;
    const timer = setTimeout(() => { void refresh(); }, Math.max(0, nextExpiry - now + 50));
    return () => clearTimeout(timer);
  }, [token, JSON.stringify(data?.coupleContext?.members || [])]);
  useEffect(() => { if (data && !data.needs_pairing && surface === "life") void loadMealData().catch(error => Alert.alert("点菜数据加载失败", error.message)); }, [surface, mealDate]);
  useEffect(() => {
    if (!data || data.needs_pairing) return;
    const accountEpoch = authEpoch.current;
    const timer = setInterval(() => { void client.events(mealEventCursor.current).then(result => {
      if (accountEpoch !== authEpoch.current) return;
      mealEventCursor.current = Math.max(mealEventCursor.current, result.latest_sequence || 0);
      if (result.items.some(item => ["meals.updated", "kitchen.updated"].includes(item.event_type))) void loadMealData();
      if (result.items.some(item => item.event_type === "trip.tools_updated")) {
        void refresh().then(() => {
          const activeTripId = tripToolsTargetRef.current;
          if (activeTripId) void refreshNativeTripToolsFromEvent(activeTripId);
          const expenseTripId = tripExpensesTargetRef.current;
          if (expenseTripId) void loadNativeTripExpenses(expenseTripId);
        });
      }
    }).catch(() => undefined); }, 4000);
    return () => clearInterval(timer);
  }, [data?.user?.id, surface, mealDate, recipeSearch, tripToolsTripId]);

  async function authenticate() {
    try {
      await clearPrivateState();
      const result = authMode === "login" ? await client.login(username, password, "bearer") : await client.register(username, password, nickname, "bearer");
      if (result.access_token) { setToken(result.access_token); await SecureStore.setItemAsync(ACCESS_TOKEN_KEY, result.access_token); }
      setPassword(""); await refresh();
    } catch (error) { Alert.alert("操作失败", (error as Error).message); }
  }
  async function pair() { try { await client.acceptInvite(invite, pairAnniversary); await refresh(); } catch (error) { Alert.alert("绑定失败", (error as Error).message); } }
  async function closeWebPanel() { setWebPanel(null); await refresh(); }
  async function logout() {
    let revokeWarning = "";
    try {
      await client.logout();
    } catch (error) {
      revokeWarning = (error as Error).message;
      client.setAccessToken();
    } finally {
      await SecureStore.deleteItemAsync(ACCESS_TOKEN_KEY);
      await clearPrivateState();
      setToken(""); setData(undefined); setManifest(undefined); setLoginRequired(true);
      if (revokeWarning) Alert.alert("已在此设备退出", `服务器暂时无法撤销旧会话：${revokeWarning}。服务器恢复后可在设备中心撤销该会话。`);
    }
  }
  async function send() { if (!message.trim()) return; try { await client.sendMessage(message.trim(), `mobile:${Date.now()}:message`); setMessage(""); await refresh(); } catch (error) { Alert.alert("发送失败", (error as Error).message); } }
  async function searchTripPlaces() { try { const result = await client.searchPlaces(tripForm.destination, tripForm.region_hint); setPlaceCandidates(result.items); if (!result.items.length) Alert.alert("没有找到", result.notice || "请补充国家、城市或区县后重试"); } catch (error) { Alert.alert("地点查询失败", (error as Error).message); } }
  async function createTrip() { if (!selectedPlace) { Alert.alert("请先确认地点", "同名地点可能位于不同城市。请先查询并选择带地址的具体地点。"); return; } try { await client.createTrip({ ...tripForm, destination_candidate_id: selectedPlace.candidate_id, budget: Number(tripForm.budget), preferences: { pace: "松弛", transport: "公共交通" } }); setTripForm(value => ({ ...value, destination: "", region_hint: "" })); setSelectedPlace(null); setPlaceCandidates([]); await refresh(); } catch (error) { Alert.alert("创建失败", (error as Error).message); } }
  async function loadNativeTripTools(tripId: string) {
    const identity = tripToolsIdentityRef.current;
    if (!identity) return;
    tripToolsTargetRef.current = tripId;
    const request = tripToolsGuard.current.begin(currentTripToolsScope(tripId));
    try {
      const [packing, emergency] = await Promise.all([client.tripPacking(tripId), client.tripEmergency(tripId)]);
      if (!isTripToolsRequestCurrent(request)) return;
      let offlineSnapshot: TripOfflineSnapshot | null = null;
      try {
        const snapshot = await client.tripOfflineSnapshot(tripId);
        if (!isTripToolsRequestCurrent(request)) return;
        const valid = validateTripOfflineSnapshot(snapshot, identity, tripId);
        if (valid) {
          await privateCache.setItem(`trip-offline:${tripId}`, JSON.stringify(valid));
          if (!isTripToolsRequestCurrent(request)) return;
          offlineSnapshot = valid;
        }
      } catch (error) {
        if (!isTripToolsRequestCurrent(request)) return;
        const status = (error as Error & { status?: number }).status;
        if ([401, 403, 404].includes(Number(status))) {
          await privateCache.removeItem(`trip-offline:${tripId}`);
          if (!isTripToolsRequestCurrent(request)) return;
        }
      }
      if (!isTripToolsRequestCurrent(request)) return;
      setTripPacking(packing.items); setTripEmergency(emergency.items); setTripToolsTripId(tripId); setTripOfflineReadOnly(false); setTripOfflineSnapshot(offlineSnapshot);
    } catch (error) {
      if (!isTripToolsRequestCurrent(request)) return;
      const status = (error as Error & { status?: number }).status;
      if ([401, 403, 404].includes(Number(status))) {
        await privateCache.removeItem(`trip-offline:${tripId}`);
        if (!isTripToolsRequestCurrent(request)) return;
        throw error;
      }
      if (status) throw error;
      const raw = await privateCache.getItem(`trip-offline:${tripId}`);
      if (!isTripToolsRequestCurrent(request)) return;
      let parsed: unknown = null; try { parsed = raw ? JSON.parse(raw) : null; } catch { parsed = null; }
      const snapshot = validateTripOfflineSnapshot(parsed, identity, tripId);
      if (!snapshot) {
        await privateCache.removeItem(`trip-offline:${tripId}`);
        if (!isTripToolsRequestCurrent(request)) return;
        throw error;
      }
      if (!isTripToolsRequestCurrent(request)) return;
      setTripPacking(snapshot.packing.items); setTripEmergency(snapshot.emergency.items); setTripToolsTripId(tripId); setTripOfflineReadOnly(true); setTripOfflineSnapshot(snapshot);
    }
  }
  async function refreshNativeTripToolsFromEvent(tripId: string) {
    if (!tripId || tripToolsTargetRef.current !== tripId) return;
    await loadNativeTripTools(tripId);
  }
  async function updateNativePacking(tripId: string, item: TripPackingItem, value: { checked?: boolean; assigned_to?: string }) {
    if (tripOfflineReadOnly) { Alert.alert("需要联网", "离线副本只用于查看，行李修改不会排队保存。"); return; }
    const request = tripToolsGuard.current.begin(currentTripToolsScope(tripId));
    try {
      const updated = await client.updateTripPackingItem(tripId, item.id, { ...value, expected_version: item.version });
      if (!isTripToolsRequestCurrent(request)) return;
      setTripPacking(items => items.map(current => current.id === updated.id ? updated : current));
      void loadNativeTripTools(tripId);
    } catch (error) {
      if (!isTripToolsRequestCurrent(request)) return;
      Alert.alert("更新失败", (error as Error).message);
    }
  }
  async function loadNativeTripExpenses(tripId: string) {
    if (!tripToolsIdentityRef.current) return;
    tripExpensesTargetRef.current = tripId;
    const request = tripExpensesGuard.current.begin(currentTripExpensesScope(tripId));
    try {
      const result = await client.tripExpenses(tripId);
      if (!isTripExpensesRequestCurrent(request)) return;
      setTripExpenses(result); setTripExpenseTripId(tripId);
      setSelectedTripExpenses(ids => ids.filter(id => result.items.some(item => item.id === id && !item.settled_at)));
      if (!tripExpensePayer) setTripExpensePayer(data?.user.id || result.members[0]?.id || "");
      if (!tripExpenseBeneficiary) setTripExpenseBeneficiary(data?.user.id || result.members[0]?.id || "");
    } catch (error) {
      if (!isTripExpensesRequestCurrent(request)) return;
      Alert.alert("需要联网", `旅行花费只在线查看和保存：${(error as Error).message}`);
    }
  }
  async function saveNativeTripExpense() {
    const tripId = tripExpenseTripId; const amount = parseTripExpenseAmount(tripExpenseAmount);
    if (!tripId || !amount) { Alert.alert("金额格式不正确", "请输入大于 0 且最多两位小数的金额，例如 12.34。"); return; }
    const request = tripExpensesGuard.current.begin(currentTripExpensesScope(tripId));
    try {
      await client.createTripExpense(tripId, { amount_cents: amount, payer_id: tripExpensePayer, category: tripExpenseCategory, split_mode: tripExpenseSplit, ...(tripExpenseSplit === "personal" ? { beneficiary_id: tripExpenseBeneficiary } : {}), occurred_on: tripExpenseDate, note: tripExpenseNote.trim() });
      if (!isTripExpensesRequestCurrent(request)) return;
      setTripExpenseAmount(""); setTripExpenseNote(""); await loadNativeTripExpenses(tripId);
    } catch (error) { if (isTripExpensesRequestCurrent(request)) Alert.alert("保存失败", `请确认网络和填写内容：${(error as Error).message}`); }
  }
  async function deleteNativeTripExpense(tripId: string, expenseId: string, version: number) {
    const request = tripExpensesGuard.current.begin(currentTripExpensesScope(tripId));
    try {
      await client.deleteTripExpense(tripId, expenseId, version);
      if (!isTripExpensesRequestCurrent(request)) return;
      setSelectedTripExpenses(ids => ids.filter(id => id !== expenseId)); await loadNativeTripExpenses(tripId);
    } catch (error) { if (isTripExpensesRequestCurrent(request)) Alert.alert("删除失败", (error as Error).message); }
  }
  async function restoreNativeTripExpense(tripId: string, expenseId: string, version: number) {
    const request = tripExpensesGuard.current.begin(currentTripExpensesScope(tripId));
    try {
      await client.restoreTripExpense(tripId, expenseId, version);
      if (!isTripExpensesRequestCurrent(request)) return;
      await loadNativeTripExpenses(tripId);
    } catch (error) { if (isTripExpensesRequestCurrent(request)) Alert.alert("恢复失败", (error as Error).message); }
  }
  async function settleNativeTripExpenses() {
    const tripId = tripExpenseTripId;
    const selected = (tripExpenses?.items || []).filter(item => !item.settled_at && selectedTripExpenses.includes(item.id));
    if (!tripId || !selected.length) { Alert.alert("请选择记录", "请先选择至少一笔待结清花费。"); return; }
    const request = tripExpensesGuard.current.begin(currentTripExpensesScope(tripId));
    try {
      const result = await client.settleTripExpenses(tripId, selected.map(item => ({ id: item.id, expected_version: item.version })));
      if (!isTripExpensesRequestCurrent(request)) return;
      setSelectedTripExpenses([]); await loadNativeTripExpenses(tripId); Alert.alert("已记录", result.notice);
    } catch (error) { if (isTripExpensesRequestCurrent(request)) Alert.alert("结清失败", (error as Error).message); }
  }
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
  async function saveLife() { try {
    if (lifeForm.blocksTime && !lifeForm.participantIds.length) throw new Error("占用约会时间时，请至少选择一位参与人");
    if (lifeForm.blocksTime && !lifeForm.allDay && !lifeForm.endsAt) throw new Error("定时占用必须填写结束时间");
    const payload = { entry_type: lifeForm.type, title: lifeForm.title, starts_at: calendarApiTime(lifeForm.startsAt, lifeForm.allDay) || "", ends_at: lifeForm.allDay ? (lifeForm.endsAt || null) : calendarApiTime(lifeForm.endsAt, false), all_day: lifeForm.allDay, participant_ids: lifeForm.participantIds, blocks_time: lifeForm.blocksTime, recurrence: lifeForm.recurrence, ...(editingLife ? { expected_version: editingLife.version } : {}) };
    if (editingLife) await client.updateCalendar(editingLife.id, payload); else await client.createCalendar(payload);
    setLifeForm(emptyLifeForm()); setEditingLife(null); await refresh();
  } catch (error) { Alert.alert("保存失败", (error as Error).message); } }
  function toggleDecisionChoice(decision: Decision, optionId: string) {
    const current = new Set(decisionSelections[decision.id] || []);
    if (decision.selection_mode === "single") current.clear();
    if (current.has(optionId)) current.delete(optionId); else current.add(optionId);
    setDecisionSelections(value => ({ ...value, [decision.id]: [...current] }));
  }
  async function voteDecision(decision: Decision) {
    const optionIds = decisionSelections[decision.id] || [];
    if (!optionIds.length) { Alert.alert("请先选择", "至少选择一个选项后再秘密提交。"); return; }
    try {
      const updated = await client.voteDecision(decision.id, optionIds, decision.version);
      setDecisions(items => items.map(item => item.id === updated.id ? updated : item));
      setDecisionSelections(value => { const next = { ...value }; delete next[decision.id]; return next; });
    } catch (error) { Alert.alert("提交失败", (error as Error).message); }
  }
  async function confirmDecision(decision: Extract<Decision, { status: "revealed" | "pending_confirmation" | "confirmed" | "converted" }>, optionId: string) {
    try { const updated = await client.resolveDecision(decision.id, "confirm", decision.version, optionId); setDecisions(items => items.map(item => item.id === updated.id ? updated : item)); }
    catch (error) { Alert.alert("确认失败", (error as Error).message); }
  }
  async function previewDecisionMeal(decision: Extract<Decision, { status: "revealed" | "pending_confirmation" | "confirmed" | "converted" }>, optionId?: string) {
    try { setDecisionPrefill(await client.decisionConversionPreview(decision.id, "meal_wish", optionId)); }
    catch (error) { Alert.alert("无法转换", (error as Error).message); }
  }
  async function saveDecisionMealPrefill() {
    if (!decisionPrefill || decisionPrefill.target !== "meal_wish") return;
    try {
      await client.createMealWish({ title: String(decisionPrefill.values.title || ""), note: String(decisionPrefill.values.note || "") });
      setDecisionPrefill(null); await loadMealData(); Alert.alert("已保存", "这道菜现在进入两个人的想吃清单。");
    } catch (error) { Alert.alert("保存失败", (error as Error).message); }
  }
  async function voteDateVariant(plan: DatePlan, variantId: string) {
    try { const updated = await client.voteDatePlan(plan.id, variantId); setDatePlans(items => items.map(item => item.id === updated.id ? updated : item)); }
    catch (error) { Alert.alert("投票失败", (error as Error).message); }
  }
  async function composeDateVariant(plan: DatePlan, variantId: string) {
    try { const updated = await client.composeDatePlan(plan.id, variantId); setDatePlans(items => items.map(item => item.id === updated.id ? updated : item)); }
    catch (error) { Alert.alert("生成最终方案失败", (error as Error).message); }
  }
  async function confirmDateSelection(plan: DatePlan) {
    try { const updated = await client.confirmDatePlan(plan.id, plan.version); setDatePlans(items => items.map(item => item.id === updated.id ? updated : item)); }
    catch (error) { Alert.alert("确认失败", (error as Error).message); }
  }
  async function savePreference() { if (!data?.partner) return; try { const payload = { subject_user_id: editingPreference?.subject_user_id || data.partner.id, category: editingPreference?.category || "其他", label: prefForm.label, value: prefForm.value, ...(editingPreference ? { expected_version: editingPreference.version } : {}) }; if (editingPreference) await client.updatePreference(editingPreference.id, payload); else await client.createPreference(payload); setPrefForm({ label: "", value: "" }); setEditingPreference(null); await refresh(); } catch (error) { Alert.alert("保存失败", (error as Error).message); } }
  async function saveWish() { if (!wishText.trim()) return; try { if (editingWish) await client.updateWishlistItem(editingWish.id, { text: wishText.trim(), note: editingWish.note || "", expected_version: editingWish.version }); else await client.createWishlistItem({ text: wishText.trim(), item_kind: wishKind }); setWishText(""); setWishKind("general"); setEditingWish(null); await refresh(); } catch (error) { Alert.alert("保存失败", (error as Error).message); } }
  async function claimWish(item: WishlistItem) { try { await client.claimWishlistItem(item.id, { expected_version: item.version }); await refresh(); Alert.alert("已私密认领", "只有你能看到认领状态，对方不会收到提示。"); } catch (error) { Alert.alert("认领失败", (error as Error).message); } }
  async function releaseWish(item: WishlistItem) { if (!item.my_claim) return; try { await client.releaseWishlistClaim(item.id, item.my_claim.version); await refresh(); } catch (error) { Alert.alert("取消失败", (error as Error).message); } }
  function completeClaim(item: WishlistItem) { if (!item.my_claim) return; Alert.alert("礼物准备好了", "是否向对方公开由你完成？", [{ text: "取消", style: "cancel" }, { text: "匿名完成", onPress: () => void client.completeWishlistClaim(item.id, { expected_version: item.my_claim!.version, reveal_claimant: false, create_moment: false }).then(refresh).catch(error => Alert.alert("完成失败", error.message)) }, { text: "公开完成", onPress: () => void client.completeWishlistClaim(item.id, { expected_version: item.my_claim!.version, reveal_claimant: true, create_moment: false }).then(refresh).catch(error => Alert.alert("完成失败", error.message)) }]); }
  async function saveStatus() { try { await client.setStatus({ status_code: statusCode, text: statusText.trim() || undefined, duration_minutes: 120 }); setStatusText(""); await refresh(); } catch (error) { Alert.alert("状态保存失败", (error as Error).message); } }
  async function assistExpression(messageId?: string) { const source = messageId ? undefined : message.trim(); if (!messageId && !source) { Alert.alert("请先输入文字"); return; } const epoch = authEpoch.current; try { const value = await client.suggestExpression({ mode: "gentle", ...(messageId ? { message_id: messageId } : { draft: source }) }); if (epoch === authEpoch.current) setExpressionSuggestion(value); } catch (error) { if (epoch === authEpoch.current) Alert.alert("暂时无法整理", `${(error as Error).message}\n原草稿仍在输入框中。`); } }
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
  const visibleMembers = (data?.coupleContext?.members || []).map(member => {
    const expiry = member.expires_at ? new Date(member.expires_at).getTime() : NaN;
    return Number.isFinite(expiry) && expiry <= Date.now() ? { ...member, status_text: undefined, status_code: undefined, emoji: undefined } : member;
  });

  if (loginRequired || (!data && !token)) return <SafeAreaView style={s.safe}><View style={s.login}><Text style={s.brand}>恋屿 · LOVE MOMENTS</Text><Text style={s.loginTitle}>{authMode === "login" ? "登录共同小岛" : "创建恋屿账号"}</Text>{authMode === "register" && <Field value={nickname} onChangeText={setNickname} placeholder="昵称" />}<Field value={username} onChangeText={setUsername} placeholder="账号"/><Field value={password} onChangeText={setPassword} secureTextEntry placeholder="密码至少 8 位"/><Button title={authMode === "login" ? "登录" : "注册"} onPress={() => void authenticate()}/><Button quiet title={authMode === "login" ? "还没有账号？注册" : "已有账号？登录"} onPress={() => setAuthMode(authMode === "login" ? "register" : "login")}/></View></SafeAreaView>;

  if (!API) return <SafeAreaView style={s.safe}><ScrollView contentContainerStyle={s.login}><Text style={s.brand}>恋屿</Text><Text style={s.loginTitle}>需要设置服务器地址</Text><Text style={s.note}>构建前请设置 EXPO_PUBLIC_API_URL 为两台设备都能访问的 HTTPS 地址，例如你的 Tailscale 或 Sakura 映射地址。应用不会再把手机错误地连接到 127.0.0.1。</Text></ScrollView></SafeAreaView>;
  if (data?.needs_pairing) return <SafeAreaView style={s.safe}><ScrollView contentContainerStyle={s.login}><Text style={s.brand}>PAIR WITH YOUR LOVE</Text><Text style={s.loginTitle}>组成情侣账号</Text><Text style={s.note}>两个人分别注册。一人生成邀请码，另一人输入邀请码并确认正式在一起的日期。</Text><Button title="生成我的邀请码" onPress={() => void client.createInvite().then(value => Alert.alert("邀请码", `${value.code}\n24 小时内有效，只能使用一次`)).catch(error => Alert.alert("失败", error.message))}/><Field value={invite} onChangeText={setInvite} placeholder="输入伴侣邀请码"/><Field value={pairAnniversary} onChangeText={setPairAnniversary} placeholder="在一起日期 YYYY-MM-DD"/><Button quiet title="接受邀请" onPress={() => void pair()}/></ScrollView></SafeAreaView>;

  const renderHome = () => <><View style={s.hero}><Text style={s.heroSmall}>OUR STORY</Text><Text style={s.heroTitle}>一起走过 {daysTogether.toLocaleString()} 天</Text><Text style={s.heroSub}>{data?.partner?.nickname} 与 {data?.user.nickname} 的共同空间</Text></View><Card><Text style={s.title}>今天的状态</Text>{visibleMembers.map(member => <Text key={member.id} style={s.sub}>{member.nickname}：{member.status_text ? `${member.emoji || "♡"} ${member.status_text}` : "还没有分享状态"}</Text>)}<View style={s.tabs}>{(["busy", "want_chat", "need_company", "feeling_low", "moving", "home", "custom"] as StatusCode[]).map(code => <TouchableOpacity key={code} style={[s.pillButton, statusCode === code && s.pillActive]} onPress={() => setStatusCode(code)}><Text>{statusLabels[code]}</Text></TouchableOpacity>)}</View><Field value={statusText} onChangeText={setStatusText} placeholder={statusCode === "custom" ? "自定义状态（必填）" : "补充一句（可选）"}/><Button quiet title="分享 2 小时" onPress={() => void saveStatus()}/></Card><Card><Text style={s.title}>账号与数据</Text><Text style={s.sub}>登录令牌保存在系统安全存储中；设备、ZIP 导出、解绑和注销都在受保护的账号中心完成。</Text><Button quiet title="打开账号中心" onPress={() => setWebPanel("account")}/><Button quiet title="退出当前设备" onPress={() => void logout()}/></Card></>;
  const renderChat = () => <><View style={s.row}><Text style={s.pageTitle}>与 {data?.partner?.nickname}</Text><Button title="语音" onPress={() => setWebPanel("call")}/></View>{(data?.messages || []).slice(-50).map(item => <View key={item.id}><TouchableOpacity onLongPress={() => void client.messageToMoment(item.id).then(() => Alert.alert("已收藏", "这段聊天已进入时光")).catch(error => Alert.alert("失败", error.message))} style={[s.bubble, item.mine && s.mine]}><Text style={item.mine ? s.white : s.ink}>{item.content}</Text><Text style={[s.tiny, item.mine && s.light]}>长按收藏 · {new Date(item.sent_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</Text></TouchableOpacity>{item.mine && item.type === "text" && <Button quiet title="帮我把这句话表达得更温和" onPress={() => void assistExpression(item.id)}/>}</View>)}<View style={s.row}><TextInput style={[s.input, s.grow]} value={message} onChangeText={setMessage} placeholder="和 TA 说点什么"/><Button title="发送" onPress={() => void send()}/></View><Button quiet title="帮我表达当前草稿" onPress={() => void assistExpression()}/>{expressionSuggestion && <Card><Text style={s.title}>表达建议（不会自动发送）</Text><Text style={s.sub}>{expressionSuggestion.text}</Text><Button title="放回输入框" onPress={() => { setMessage(expressionSuggestion.text); setExpressionSuggestion(null); }}/><Button quiet title="保留原稿" onPress={() => setExpressionSuggestion(null)}/></Card>}<Card><Text style={s.title}>导入外部聊天</Text><Field value={ocrText} onChangeText={setOcrText} placeholder="粘贴聊天文字；截图可在完整 H5 上传"/><Button quiet title="生成可编辑 OCR 草稿" onPress={() => void makeOcrDraft()}/>{ocrDraft && <><Text style={s.warning}>{ocrDraft.mock ? "演示识别，请逐条检查" : "OCR 草稿"}</Text>{(ocrDraft.items || []).map((item: any, index: number) => <View key={item.id} style={s.row}><TouchableOpacity onPress={() => { const next = { ...ocrDraft, items: [...ocrDraft.items] }; next.items[index] = { ...item, speaker: item.speaker === "me" ? "partner" : "me" }; setOcrDraft(next); }}><Text style={s.pill}>{item.speaker === "me" ? "我" : "对方"}</Text></TouchableOpacity><TextInput style={[s.input, s.grow]} value={item.content} onChangeText={text => { const next = { ...ocrDraft, items: [...ocrDraft.items] }; next.items[index] = { ...item, content: text }; setOcrDraft(next); }}/></View>)}<Button title="确认导入" onPress={() => void confirmOcr()}/></>}</Card></>;
  const renderTrip = () => <>
    <Text style={s.pageTitle}>我们的旅行</Text>
    {(data?.trips || []).map((item, index) => <Card key={item.id}>
      <Text style={s.title}>{item.title}</Text>
      <Text style={s.sub}>{item.start_date}—{item.end_date} · ¥{item.budget_cents / 100} · {item.status}</Text>
      {item.needs_destination_detail && <Text style={s.warning}>具体地图地点尚未确认，现有行程可能遇到异地同名；请在完整 H5 的旅行设置中确认后重新生成。</Text>}
      <Text style={s.tiny}>五套方案：{item.variants?.length || 0}</Text>
      <View style={s.tabs}><Button quiet title="查看路线" onPress={() => void client.tripRoute(item.id).then(value => { setTripTool(`${value.mock ? "模拟" : "高德"}路线 · ${Math.round(Number(value.distance_meters) / 100) / 10} km · ${Math.round(Number(value.duration_seconds) / 60)} 分钟`); setQuoteItems([]); }).catch(error => Alert.alert("路线失败", error.message))}/><Button quiet title="行李与应急" onPress={() => void loadNativeTripTools(item.id).catch(error => Alert.alert("读取失败", error.message))}/><Button quiet title="花费与结算" onPress={() => void loadNativeTripExpenses(item.id)}/></View>
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
    {tripToolsTripId ? <Card><Text style={s.title}>行李与应急</Text>{tripOfflineReadOnly && <Text style={s.warning}>离线副本 · 只读 · 到期 {tripOfflineSnapshot ? new Date(tripOfflineSnapshot.expires_at).toLocaleString("zh-CN") : "未知"}</Text>}<Text style={s.sectionTitle}>行李</Text>{tripPacking.length ? tripPacking.map(item => <View key={item.id} style={s.mealNativeRow}><TouchableOpacity disabled={tripOfflineReadOnly} onPress={() => void updateNativePacking(tripToolsTripId, item, { checked: !item.checked })}><Text style={[s.title, item.checked && s.doneText]}>{item.checked ? "✓ " : "○ "}{item.label}</Text></TouchableOpacity><Text style={s.tiny}>{item.assigned_name} · {item.source_type === "human" ? "手动添加" : "规则建议"}</Text><View style={s.tabs}><Button quiet disabled={tripOfflineReadOnly} title="共同" onPress={() => void updateNativePacking(tripToolsTripId, item, { assigned_to: "shared" })}/>{(data?.coupleContext?.members || []).map(member => <Button key={member.id} quiet disabled={tripOfflineReadOnly} title={`给${member.nickname}`} onPress={() => void updateNativePacking(tripToolsTripId, item, { assigned_to: member.id })}/>)}</View></View>) : <Text style={s.sub}>还没有行李项目，请在完整功能页生成建议。</Text>}<Text style={s.sectionTitle}>应急卡（只读）</Text>{tripEmergency.length ? tripEmergency.map(item => <View key={item.id} style={s.mealNativeRow}><Text style={s.title}>{item.title}</Text><Text style={s.tiny}>{item.visibility_label} · {["real_api", "cache"].includes(item.source_type) ? "真实地图核验" : "真人填写"}</Text>{mobileEmergencyLines(item).map(line => <Text key={`${item.id}-${line}`} style={s.sub}>{line}</Text>)}</View>) : <Text style={s.sub}>当前没有你有权查看的应急卡。</Text>}<Button quiet title="在完整功能页编辑应急卡" onPress={() => setWebPanel("trip-tools")}/></Card> : null}
    {tripExpenseTripId && tripExpenses ? <Card>
      <Text style={s.title}>花费与线下结清</Text>
      <Text style={s.warning}>{tripExpenses.balance.direction === "partner_owes_me" ? `${tripExpenses.balance.partner_name}应线下补给我 ${formatTripExpenseCents(tripExpenses.balance.net_cents)}` : tripExpenses.balance.direction === "i_owe_partner" ? `我应线下补给${tripExpenses.balance.partner_name} ${formatTripExpenseCents(Math.abs(tripExpenses.balance.net_cents))}` : "目前已经结清"}</Text>
      <Text style={s.tiny}>双方可见 · 真人填写 · 这里只记录状态，不会付款或转账</Text>
      <Text style={s.sectionTitle}>记录一笔花费</Text>
      <Field value={tripExpenseAmount} onChangeText={setTripExpenseAmount} placeholder="金额（元），例如 12.34"/>
      <View style={s.tabs}>{tripExpenses.members.map(member => <TouchableOpacity key={member.id} style={[s.pillButton, tripExpensePayer === member.id && s.pillActive]} onPress={() => setTripExpensePayer(member.id)}><Text>{member.nickname}付款</Text></TouchableOpacity>)}</View>
      <View style={s.tabs}>{(["transport", "lodging", "food", "tickets", "shopping", "other"] as const).map(category => <TouchableOpacity key={category} style={[s.pillButton, tripExpenseCategory === category && s.pillActive]} onPress={() => setTripExpenseCategory(category)}><Text>{({ transport: "交通", lodging: "住宿", food: "餐饮", tickets: "门票", shopping: "购物", other: "其他" } as const)[category]}</Text></TouchableOpacity>)}</View>
      <View style={s.tabs}><Button quiet title={tripExpenseSplit === "equal" ? "✓ 两人均分" : "两人均分"} onPress={() => setTripExpenseSplit("equal")}/><Button quiet title={tripExpenseSplit === "personal" ? "✓ 一人承担" : "一人承担"} onPress={() => setTripExpenseSplit("personal")}/></View>
      {tripExpenseSplit === "personal" && <View style={s.tabs}>{tripExpenses.members.map(member => <TouchableOpacity key={member.id} style={[s.pillButton, tripExpenseBeneficiary === member.id && s.pillActive]} onPress={() => setTripExpenseBeneficiary(member.id)}><Text>{member.nickname}承担</Text></TouchableOpacity>)}</View>}
      <Field value={tripExpenseDate} onChangeText={setTripExpenseDate} placeholder="消费日期 YYYY-MM-DD"/>
      <Field value={tripExpenseNote} onChangeText={setTripExpenseNote} placeholder="备注，例如晚餐"/>
      <Button title="记录花费" onPress={() => void saveNativeTripExpense()}/>
      <Text style={s.sectionTitle}>待线下结清</Text>
      {tripExpenses.items.filter(item => !item.settled_at).length ? tripExpenses.items.filter(item => !item.settled_at).map(item => <View key={item.id} style={s.mealNativeRow}><TouchableOpacity onPress={() => setSelectedTripExpenses(ids => ids.includes(item.id) ? ids.filter(id => id !== item.id) : [...ids, item.id])}><Text style={s.title}>{selectedTripExpenses.includes(item.id) ? "✓" : "○"} {item.note || "旅行花费"} · {formatTripExpenseCents(item.amount_cents, item.currency)}</Text></TouchableOpacity><Text style={s.tiny}>{item.payer_name}付款 · {item.split_mode === "equal" ? "两人均分" : `${item.beneficiary_name}承担`} · {item.occurred_on}</Text><Button quiet title="删除（可恢复）" onPress={() => Alert.alert("删除旅行花费", "删除后仍可在下方恢复。", [{ text: "取消", style: "cancel" }, { text: "删除", style: "destructive", onPress: () => void deleteNativeTripExpense(tripExpenseTripId, item.id, item.version) }])}/></View>) : <Text style={s.sub}>当前没有待结清花费。</Text>}
      <Button disabled={!selectedTripExpenses.length} title={`标记所选已线下结清（${selectedTripExpenses.length}）`} onPress={() => Alert.alert("确认线下结清", "这里只记录结清状态，不会发起付款。", [{ text: "取消", style: "cancel" }, { text: "确认", onPress: () => void settleNativeTripExpenses() }])}/>
      <Text style={s.sectionTitle}>已线下结清</Text>
      {tripExpenses.items.filter(item => item.settled_at).map(item => <View key={item.id} style={s.mealNativeRow}><Text style={s.title}>{item.note || "旅行花费"} · {formatTripExpenseCents(item.amount_cents, item.currency)}</Text><Text style={s.tiny}>{item.settled_by_name}已标记 · {item.settled_at ? new Date(item.settled_at).toLocaleString("zh-CN") : ""}</Text><Button quiet title="删除（可恢复）" onPress={() => void deleteNativeTripExpense(tripExpenseTripId, item.id, item.version)}/></View>)}
      <Text style={s.sectionTitle}>最近删除</Text>
      {tripExpenses.deleted_items.length ? tripExpenses.deleted_items.map(item => <View key={item.id} style={s.mealNativeRow}><Text style={s.title}>{item.note || "旅行花费"} · {formatTripExpenseCents(item.amount_cents, item.currency)}</Text><Button quiet title="恢复" onPress={() => void restoreNativeTripExpense(tripExpenseTripId, item.id, item.version)}/></View>) : <Text style={s.sub}>没有可恢复记录。</Text>}
    </Card> : null}
    {tripTool ? <Card><Text style={s.title}>路线结果</Text><Text style={s.sub}>{tripTool}</Text></Card> : null}
    {quoteItems.length > 0 && <View><Text style={s.note}>{quoteNotice} · 共 {quoteItems.length} 条</Text>{quoteItems.map(item => <Card key={item.id}>
      <View style={s.row}><Text style={[s.pill, s.grow]}>{item.source_mode === "mock" ? "模拟" : item.source_mode === "aggregate" ? "聚合参考" : "实时查询"}</Text><Text style={s.quotePrice}>¥{(item.amount_cents / 100).toLocaleString("zh-CN")}</Text></View>
      <Text style={s.title}>{item.title}</Text><Text style={s.sub}>{item.subtitle}</Text><Text style={s.sub}>{item.terms}</Text>
      {item.details?.seats?.map(seat => <Text key={`${item.id}-${seat.name}`} style={s.tiny}>{seat.name} · ¥{seat.price_cents / 100} · 余 {seat.remaining}</Text>)}
      <Text style={s.tiny}>来源：{item.provider} · 查询于 {new Date(item.fetched_at).toLocaleString("zh-CN")}</Text>
      <View style={s.tabs}><Button quiet disabled={Boolean(item.selected_at)} title={item.selected_at ? "已加入本次旅行" : "选择这个"} onPress={() => void selectQuote(item)}/>{item.booking_url ? <Button quiet title="跳转查看 / 购买" onPress={() => void Linking.openURL(item.booking_url!).catch(() => Alert.alert("无法打开", "请检查购买链接"))}/>: null}</View>
    </Card>)}</View>}
    <Card><Text style={s.title}>创建任意目的地</Text><Text style={s.sub}>先确认地图地点，避免把异地同名景区混在一起。</Text><Field value={tripForm.destination} onChangeText={destination => { setTripForm({ ...tripForm, destination }); setSelectedPlace(null); setPlaceCandidates([]); }} placeholder="地点名称，例如迪士尼、西湖"/><Field value={tripForm.region_hint} onChangeText={region_hint => { setTripForm({ ...tripForm, region_hint }); setSelectedPlace(null); setPlaceCandidates([]); }} placeholder="国家 / 城市提示，例如上海、东京"/><Button quiet title="查找并确认具体地点" onPress={() => void searchTripPlaces()}/>{placeCandidates.map(item => <TouchableOpacity key={item.candidate_id} style={[s.mealNativeRow, selectedPlace?.candidate_id === item.candidate_id && s.pillActive]} onPress={() => setSelectedPlace(item)}><Text style={s.title}>{selectedPlace?.candidate_id === item.candidate_id ? "✓ " : ""}{item.name}</Text><Text style={s.tiny}>{[item.country, item.province, item.city, item.district, item.formatted_address].filter(Boolean).join(" · ")} · {item.provider === "amap" ? "高德" : item.provider === "google_maps" ? "Google Maps" : "手动范围（未地图核验）"}</Text></TouchableOpacity>)}<View style={s.row}><TextInput style={[s.input, s.grow]} value={tripForm.start_date} onChangeText={start_date => setTripForm({ ...tripForm, start_date })} placeholder="YYYY-MM-DD"/><TextInput style={[s.input, s.grow]} value={tripForm.end_date} onChangeText={end_date => setTripForm({ ...tripForm, end_date })} placeholder="YYYY-MM-DD"/></View><Field value={tripForm.budget} onChangeText={budget => setTripForm({ ...tripForm, budget })} placeholder="预算（元）"/><Button title="生成五套方案" onPress={() => void createTrip()}/></Card>
  </>;
  const renderTimeline = () => <><Text style={s.pageTitle}>我们的时光</Text><View style={s.tabs}><Button quiet title="搜索、语音与未来信" onPress={() => setWebPanel("memory")}/><Button quiet title="制作纸质纪念册" onPress={() => setWebPanel("album")}/><Button quiet title="时间线" onPress={() => setTimelineMode("moments")}/><Button quiet title="月份" onPress={() => void client.timelineMonths().then(value => { setTimelineItems(value.items); setTimelineMode("months"); })}/><Button quiet title="足迹" onPress={() => void client.footprints().then(value => { setTimelineItems(value.items); setTimelineMode("map"); })}/><Button quiet title="仅我" onPress={() => setTimelineMode("private")}/><Button quiet title="回收站" onPress={() => void client.momentTrash().then(value => { setTrash(value.items); setTimelineMode("trash"); })}/><Button quiet title="年度报告" onPress={() => void client.annualReport(new Date().getFullYear()).then(value => { setReport(value); setTimelineMode("report"); })}/></View>{timelineMode === "moments" && (data?.moments || []).map(item => <Card key={item.id}><Text style={s.title}>{item.title}</Text><Text style={s.sub}>{item.body}</Text><Button quiet title="移入回收站" onPress={() => void client.trashMoment(item.id).then(refresh)}/></Card>)}{timelineMode === "private" && (data?.moments || []).filter(item => item.creator_id === data?.user.id && item.visibility !== "shared").map(item => <Card key={item.id}><Text style={s.title}>{item.title}</Text><Text style={s.sub}>{item.body} · 仅自己可见</Text></Card>)}{timelineMode === "months" && timelineItems.map((item, index) => <Card key={String(item.month || index)}><Text style={s.title}>{String(item.month)}</Text><Text style={s.sub}>{String(item.count)} 个瞬间 · {String(item.media_count)} 个媒体记录</Text></Card>)}{timelineMode === "map" && timelineItems.map((item, index) => <Card key={String(item.name || index)}><Text style={s.title}>{String(item.name)}</Text><Text style={s.sub}>{Number(item.latitude).toFixed(4)}, {Number(item.longitude).toFixed(4)} · {String(item.coordinate_source)}</Text></Card>)}{timelineMode === "trash" && trash.map(item => <Card key={item.id}><Text style={s.title}>{item.title}</Text><Button quiet title="恢复" onPress={() => void client.restoreMoment(item.id, item.version).then(async () => { setTrash((await client.momentTrash()).items); await refresh(); })}/></Card>)}{timelineMode === "report" && report && <Card><Text style={s.title}>{String(report.year)} 年度报告</Text><Text style={s.sub}>{String(report.moments)} 个瞬间 · {String(report.messages)} 条消息 · {String(report.trips)} 次旅行 · {Math.round(Number(report.call_seconds || 0) / 60)} 分钟通话</Text><Button quiet title="在完整版中制作 / 下载" onPress={() => setWebPanel("album")}/></Card>}</>;
  async function stockMobileShoppingItem(item: ShoppingList["items"][number]) {
    const run = async (confirmFood: boolean) => {
      try { await client.stockShoppingItem(item.id, { expected_version: item.version, confirm_food: confirmFood }); await loadMealData(); }
      catch (error) { Alert.alert("入库失败", (error as Error).message); }
    };
    if (item.category_key === "other") Alert.alert("确认是食品", "“其他”分类只有明确确认可食用后才能放入库存。", [{ text: "取消", style: "cancel" }, { text: "确认入库", onPress: () => void run(true) }]);
    else await run(false);
  }
  async function addMobilePantryItem() {
    if (!pantryName.trim()) return;
    const name = pantryName.trim();
    const current = pantryItems.find(item => item.canonical_name === name || item.aliases.includes(name));
    try { await client.upsertPantryItem({ ...(current ? { ingredient_id: current.ingredient_id } : { name }), state: "have", expected_version: current?.version || 0 }); setPantryName(""); await loadMealData(); }
    catch (error) { Alert.alert("添加失败", (error as Error).message); }
  }
  async function createMobileWeeklyPlan() {
    try {
      const value = await client.createWeeklyMealPlan({ schema_version: 1, starts_on: localDateAfter(0), ends_on: localDateAfter(6), servings: 2, availability_mode: "either_partner", meal_slots: [{ meal_slot: "breakfast", planned_time: "08:00", max_active_minutes: 30 }, { meal_slot: "dinner", planned_time: "19:00", max_active_minutes: 60 }] });
      setWeeklyPlan(value);
    } catch (error) { Alert.alert("生成失败", (error as Error).message); }
  }
  async function confirmMobileWeeklyPlan() {
    if (!weeklyPlan.id || !weeklyPlan.version) return;
    try { setWeeklyPlan(await client.confirmWeeklyMealPlan(weeklyPlan.id, weeklyPlan.version)); await loadMealData(); }
    catch (error) { Alert.alert("确认失败", (error as Error).message); }
  }
  const renderMealWorkspace = () => <Card><Text style={s.title}>今天想吃 · 原生点菜</Text><Text style={s.sub}>中文菜谱、单日菜单和采购勾选会与另一方实时同步。</Text><View style={s.tabs}>{(["recipes", "calendar", "shopping"] as const).map(mode => <TouchableOpacity key={mode} style={[s.pillButton, mealMode === mode && s.pillActive]} onPress={() => setMealMode(mode)}><Text style={s.wine}>{mode === "recipes" ? "菜谱" : mode === "calendar" ? "日历" : "采购"}</Text></TouchableOpacity>)}</View>
    {mealMode === "recipes" && recipeDetail && <Button title="打开烹饪模式" onPress={() => { setCookRoute(`${COOK_RECIPE_ROUTE_KEY}=${encodeURIComponent(recipeDetail.id)}`); setWebPanel("cook"); }}/>}
    {mealMode === "recipes" && <><View style={s.row}><TextInput style={[s.input, s.grow]} value={recipeSearch} onChangeText={setRecipeSearch} placeholder="搜索中文菜谱"/><Button quiet title="搜索" onPress={() => void loadMealData()}/></View>{recipeDetail ? <Card><Text style={s.title}>{recipeDetail.name}</Text><Text style={s.sub}>{recipeDetail.cuisine === "western" ? "西餐" : "中餐"} · 准备 {recipeDetail.prep_minutes} 分钟 · 烹饪 {recipeDetail.cook_minutes} 分钟{recipeDetail.passive_minutes ? ` · 等待 ${stepDuration(recipeDetail.passive_minutes * 60)}` : ""} · {recipeDetail.base_servings} 人份</Text>{(["main", "seasoning"] as const).map(group => <View key={group}><Text style={s.sectionTitle}>{group === "main" ? "主要食材" : "辅料与调味"}</Text>{recipeDetail.ingredients.filter(item => (item.ingredient_group || "main") === group).map((item, index) => <View key={item.id || `${group}-${index}`} style={s.ingredientLine}><Text style={s.ingredientName}>• {item.name} · {item.display_quantity || `${item.quantity || ""}${item.unit || ""}`}{item.optional ? "（可选）" : ""}</Text>{item.preparation ? <Text style={s.tiny}>处理：{item.preparation}</Text> : null}</View>)}</View>)}<Text style={s.sectionTitle}>详细步骤</Text>{recipeDetail.steps.map((item, index) => <View key={item.id || index} style={s.stepCard}><Text style={s.stepTitle}>{index + 1}. {item.title || `第 ${index + 1} 步`}</Text><Text style={s.sub}>{item.instruction}</Text><Text style={s.stepMeta}>{[item.heat_level ? `火力：${item.heat_level}` : "", item.timer_seconds ? `约 ${stepDuration(item.timer_seconds)}` : "无需计时"].filter(Boolean).join(" · ")}</Text>{item.key_point ? <Text style={s.keyPoint}>完成判断：{item.key_point}</Text> : null}</View>)}<Text style={s.sectionTitle}>过敏原提示（按配料识别）</Text>{recipeDetail.allergen_details?.length ? recipeDetail.allergen_details.map(item => <View key={item.name} style={s.ingredientLine}><Text style={s.ingredientName}>{item.name}</Text><Text style={s.tiny}>来源：{item.sources.join("、")}</Text></View>) : <Text style={s.sub}>当前配料表未识别到常见过敏原</Text>}{recipeDetail.allergen_review_ingredients?.length ? <Text style={s.keyPoint}>请核对包装标签：{recipeDetail.allergen_review_ingredients.join("、")}属于复合或加工食品，不同品牌配方可能不同。</Text> : null}<Text style={s.tiny}>{recipeDetail.allergen_note || "豆角、红豆、绿豆、豌豆等不会自动归为大豆；复合调味料以包装标签为准。"}</Text>{recipeDetail.food_safety ? <View style={s.stepCard}><Text style={recipeDetail.food_safety.status === "blocked" ? s.warning : s.title}>两人资料核对：{recipeDetail.food_safety.status === "safe" ? "未发现冲突" : recipeDetail.food_safety.status === "blocked" ? "命中严重过敏，请勿制作" : "需要再次核对"}</Text><Text style={s.tiny}>{recipeDetail.food_safety.note}</Text></View> : null}<Field value={mealDate} onChangeText={setMealDate} placeholder="YYYY-MM-DD"/><View style={s.tabs}>{(["breakfast", "lunch", "dinner", "snack"] as const).map(slot => <TouchableOpacity key={slot} style={[s.pillButton, mealSlot === slot && s.pillActive]} onPress={() => setMealSlot(slot)}><Text>{slot === "breakfast" ? "早餐" : slot === "lunch" ? "午餐" : slot === "dinner" ? "晚餐" : "加餐"}</Text></TouchableOpacity>)}</View><Button title="安排到用餐日历" onPress={() => void client.scheduleMeal({ recipe_id: recipeDetail.id, planned_date: mealDate, meal_slot: mealSlot, servings: 2, idempotency_key: `mobile:meal:${Date.now()}` }).then(async () => { await loadMealData(); setMealMode("calendar"); }).catch(error => Alert.alert("安排失败", error.message))}/><Button quiet title="返回菜谱列表" onPress={() => setRecipeDetail(null)}/></Card> : recipes.map(item => <TouchableOpacity key={item.id} style={s.mealNativeRow} onPress={() => void client.recipe(item.id).then(setRecipeDetail).catch(error => Alert.alert("读取失败", error.message))}><Text style={s.title}>{item.name}</Text><Text style={s.tiny}>{item.cuisine === "western" ? "西餐" : "中餐"} · 准备 {item.prep_minutes} 分钟 · 烹饪 {item.cook_minutes} 分钟{item.passive_minutes ? ` · 等待 ${stepDuration(item.passive_minutes * 60)}` : ""} · 查看食材和做法 ›</Text></TouchableOpacity>)}</>}
    {mealMode === "calendar" && <><Text style={s.sectionTitle}>双方确认的一周菜单</Text>{weeklyPlan.id ? <View style={s.decisionBlock}><Text style={s.title}>{weeklyPlan.starts_on} 至 {weeklyPlan.ends_on}</Text><Text style={s.tiny}>{weeklyPlan.status === "pending_confirmation" ? "已有一人确认" : weeklyPlan.status === "draft" ? "等待双方确认" : weeklyPlan.status}</Text>{weeklyPlan.items.map(item => <Text key={item.id} style={s.sub}>{item.date} · {item.meal_slot === "breakfast" ? "早餐" : item.meal_slot === "dinner" ? "晚餐" : item.meal_slot} · {item.recipe?.name || "待选择"}</Text>)}{["draft", "pending_confirmation"].includes(weeklyPlan.status) && !weeklyPlan.items.some(item => item.status !== "planned") ? <Button title="确认当前菜单" onPress={() => void confirmMobileWeeklyPlan()}/> : null}</View> : <Button quiet title="生成未来 7 天菜单" onPress={() => void createMobileWeeklyPlan()}/>}<Field value={mealDate} onChangeText={setMealDate} placeholder="YYYY-MM-DD"/>{(mealCalendar?.days[0] ? Object.entries(mealCalendar.days[0].meals).flatMap(([slot, items]) => items.map(item => <View key={item.id} style={s.mealNativeRow}><Text style={s.title}>{slot === "breakfast" ? "早餐" : slot === "lunch" ? "午餐" : slot === "dinner" ? "晚餐" : "加餐"} · {item.name}</Text><Text style={s.tiny}>{item.servings} 人份 · {item.status}</Text>{item.status === "selected" && <Button quiet title="确认这道菜" onPress={() => void client.updateMeal(item.id, { status: "accepted", expected_version: item.version }).then(loadMealData)}/>} {["selected", "accepted", "shopping"].includes(item.status) && <><Text style={s.tiny}>食材替代建议会先核对比例、做法变化与双方饮食资料；只有安全门禁通过后才可应用。</Text><Button quiet title="食材替代建议" onPress={() => setWebPanel("meals")}/></>}{["accepted", "shopping", "cooking"].includes(item.status) && <Button title="打开烹饪模式" onPress={() => { setCookRoute(`cook_meal=${encodeURIComponent(item.id)}`); setWebPanel("cook"); }}/>}</View>)) : [])}{mealCart.items.length === 0 && <Text style={s.note}>这一天还没有安排。</Text>}</>}
    {mealMode === "shopping" && <>{shopping.items.length ? shopping.items.map(item => <View key={item.id} style={s.mealNativeRow}><TouchableOpacity onPress={() => void client.updateShoppingItem(item.id, { checked: !item.checked, expected_version: item.version }).then(loadMealData).catch(error => Alert.alert("同步失败", error.message))}><Text style={[s.title, item.checked && s.doneText]}>{item.checked ? "✓ " : "○ "}{item.display_name} · {item.display_quantity}</Text><Text style={s.tiny}>{item.category_label}{item.manual_locked ? " · 手动锁定" : ""}</Text></TouchableOpacity>{item.checked && item.category_key !== "household" ? <Button quiet title="放入家中库存" onPress={() => void stockMobileShoppingItem(item)}/> : null}</View>) : <Text style={s.note}>H5 中确认采购范围后，清单会显示在这里。</Text>}<Text style={s.sectionTitle}>两个人的饮食资料</Text>{foodProfiles.map(profile => <View key={profile.user_id} style={s.mealNativeRow}><Text style={s.title}>{profile.nickname}</Text><Text style={s.tiny}>过敏：{profile.allergies.map(item => `${item.allergen_key}（${item.severity === "severe" ? "严重" : "避免"}）`).join("、") || "未记录"}</Text><Text style={s.tiny}>忌口：{profile.avoidances.map(item => item.canonical_name).join("、") || "未记录"}</Text></View>)}<Button quiet title="编辑我的饮食资料" onPress={() => setWebPanel("app")}/><Text style={s.sectionTitle}>家中食材</Text>{pantryItems.map(item => <View key={item.id} style={s.mealNativeRow}><Text style={s.title}>{item.canonical_name} · {item.state === "have" ? "有" : item.state === "low" ? "少" : "无"}</Text><View style={s.tabs}>{(["have", "low", "out"] as const).map(value => <Button key={value} quiet title={value === "have" ? "有" : value === "low" ? "少" : "无"} onPress={() => void client.updatePantryItem(item.id, { state: value, expected_version: item.version }).then(loadMealData).catch(error => Alert.alert("更新失败", error.message))}/>)}</View></View>)}<View style={s.row}><TextInput style={[s.input, s.grow]} value={pantryName} onChangeText={setPantryName} placeholder="添加家中食材"/><Button quiet title="记为有" onPress={() => void addMobilePantryItem()}/></View></>}
  </Card>;
  const renderDecisionWorkspace = () => <Card><Text style={s.title}>双人决策</Text><Text style={s.sub}>先各自选择，双方提交后才会一起揭晓。复杂创建可打开完整功能页。</Text><Button quiet title="创建新决策" onPress={() => setWebPanel("app")}/>{decisions.map(decision => {
    const revealed = decision.status === "revealed" || decision.status === "pending_confirmation" || decision.status === "confirmed" || decision.status === "converted";
    const labels = new Map(decision.options.map(option => [option.id, option.label]));
    const resolutionIds = revealed && Array.isArray(decision.resolution.option_ids) ? decision.resolution.option_ids : [];
    const confirmIds = revealed ? (resolutionIds.length ? resolutionIds : [...new Set(decision.votes.flatMap(vote => vote.option_ids))]) : [];
    return <View key={decision.id} style={s.decisionBlock}><Text style={s.title}>{decision.title}</Text><Text style={s.tiny}>{revealed ? "双方选择已揭晓" : `${decision.submitted_by_me ? "你已秘密提交" : "你还未提交"} · ${decision.partner_submitted ? "TA 已提交" : "TA 尚未提交"}`}</Text>{decision.options.map(option => <TouchableOpacity disabled={decision.status !== "open"} key={option.id} style={[s.mealNativeRow, (decisionSelections[decision.id] || []).includes(option.id) && s.pillActive]} onPress={() => toggleDecisionChoice(decision, option.id)}><Text style={s.title}>{(decisionSelections[decision.id] || []).includes(option.id) ? "✓ " : "○ "}{option.label}</Text></TouchableOpacity>)}{decision.status === "open" && <Button title={decision.submitted_by_me ? "更新我的选择" : "秘密提交"} onPress={() => void voteDecision(decision)}/>} {revealed && <>{decision.votes.map((vote, index) => <Text key={`${decision.id}-${vote.user_id}`} style={s.sub}>{index === 0 ? "一方" : "另一方"}：{vote.options.map(option => option.label).join("、") || "未选择"}</Text>)}{(decision.status === "revealed" || decision.status === "pending_confirmation") && confirmIds.map(optionId => <Button key={optionId} quiet title={`共同确认“${labels.get(optionId) || "这个选项"}”`} onPress={() => void confirmDecision(decision, optionId)}/>)}{decision.status === "confirmed" && resolutionIds.map(optionId => <Button key={`prefill-${optionId}`} quiet title={`预填“${labels.get(optionId) || "这个选项"}”到想吃清单`} onPress={() => void previewDecisionMeal(decision, optionId)}/>)}</>}</View>;
  })}{decisionPrefill && <View style={s.prefill}><Text style={s.title}>尚未保存</Text><Text style={s.sub}>{String(decisionPrefill.values.title || "")}</Text><Text style={s.tiny}>{decisionPrefill.notice}</Text><Button title="确认保存到想吃清单" onPress={() => void saveDecisionMealPrefill()}/><Button quiet title="取消预填" onPress={() => setDecisionPrefill(null)}/></View>}<Text style={s.sectionTitle}>实用约会方案</Text><Text style={s.tiny}>地点、天气、价格与推荐理由分别标注来源；规则预算不是实时价格。</Text>{datePlans.length ? datePlans.map(plan => <View key={plan.id} style={s.decisionBlock}><Text style={s.title}>{plan.title}</Text><Text style={s.sub}>{plan.plan_date} · {plan.city} · {plan.status}</Text>{plan.generation_message ? <Text style={s.tiny}>{plan.generation_message}</Text> : null}{plan.preferences?.timezone_confirmed === false ? <Text style={s.warning}>目的地时区尚未确认，日历冲突按双方当前共同设置估算；出行前请核对当地时间。</Text> : null}{plan.variants.map(variant => <View key={variant.id} style={s.dateVariant}><Text style={s.title}>{variant.name} · 约 ¥{(variant.expected_cost_cents / 100).toFixed(0)}</Text><Text style={s.sub}>{variant.summary} · {variant.intensity}</Text><Text style={s.tiny}>方案：{provenanceLabel(variant.generation_source)}{variant.weather_fit ? ` · 天气适配：${variant.weather_fit}` : ""}</Text>{variant.items.map(item => <View key={item.id} style={s.dateItem}><Text style={s.title}>{item.startTime} · {item.title}</Text><Text style={s.sub}>{item.note}</Text><Text style={s.tiny}>预计通勤：{item.commuteMode || "待核对"} {item.commuteMinutes || 0} 分钟（{provenanceLabel(item.commuteSource || "rules")}）</Text><Text style={s.tiny}>{item.weatherRisk || "天气数据不可用，出发前请确认"}</Text><Text style={s.tiny}>地点：{provenanceLabel(item.placeSource)}{item.provider ? ` · ${item.provider}` : ""} · 推荐：{provenanceLabel(item.recommendationSource)} · 价格：{provenanceLabel(item.priceSource)} · 天气：{provenanceLabel(item.weatherSource)}{item.weatherProvider ? ` · ${item.weatherProvider}` : ""}</Text></View>)}{plan.status === "voting" && <Button quiet title={plan.my_vote === variant.id ? "已投这套" : "投这套"} onPress={() => void voteDateVariant(plan, variant.id)}/>} {plan.status === "voting" && plan.my_vote === variant.id && <Button title="采用这套作为最终方案" onPress={() => void composeDateVariant(plan, variant.id)}/>}</View>)}{plan.status === "pending_confirmation" && <Button disabled={plan.confirmed_by_me} title={plan.confirmed_by_me ? "已确认，等待 TA" : "确认最终约会"} onPress={() => void confirmDateSelection(plan)}/>}</View>) : <Text style={s.note}>还没有约会方案，可在完整功能页选择明确地点后生成五套方案。</Text>}</Card>;
  const renderLife = () => <><Text style={s.pageTitle}>共同生活</Text>{renderDecisionWorkspace()}{renderMealWorkspace()}
    <Card><Text style={s.title}>共同愿望</Text><Text style={s.sub}>礼物心愿可由伴侣私密认领；创建者在完成前不会看到任何认领痕迹。</Text>{(data?.wishlist?.items || []).map(item => <View key={item.id} style={s.mealNativeRow}><Text style={[s.title, item.completed_at && s.doneText]}>{item.text}</Text><Text style={s.tiny}>{({ general: "共同愿望", gift: "礼物心愿", meal: "想吃", travel: "想去" } as const)[item.item_kind]}{item.note ? ` · ${item.note}` : ""}</Text>{!item.is_creator && item.my_claim?.private_note ? <Text style={s.tiny}>我的私密备注：{item.my_claim.private_note}</Text> : null}<View style={s.tabs}>{(item.item_kind !== "gift" || item.is_creator) && <Button quiet title={item.completed_at ? "恢复" : "完成"} onPress={() => void client.toggleWishlistItem(item.id, !item.completed_at).then(refresh).catch(error => Alert.alert("更新失败", error.message))}/>} {!item.is_creator && item.item_kind === "gift" && !item.completed_at && !item.my_claim && item.can_claim && <Button quiet title="私密认领" onPress={() => void claimWish(item)}/>} {!item.is_creator && item.my_claim && <><Button title="我已准备好" onPress={() => completeClaim(item)}/><Button quiet title="取消认领" onPress={() => void releaseWish(item)}/></>}{(item.item_kind !== "gift" || item.is_creator) && <><Button quiet title="编辑" onPress={() => { setEditingWish(item); setWishText(item.text); setWishKind(item.item_kind); }}/><Button quiet title="删除" onPress={() => void client.deleteWishlistItem(item.id, item.version).then(refresh).catch(error => Alert.alert("删除失败", error.message))}/></>}</View></View>)}<Text style={s.sectionTitle}>{editingWish ? "编辑愿望" : "添加愿望"}</Text><Field value={wishText} onChangeText={setWishText} placeholder="想一起完成什么"/>{!editingWish && <View style={s.tabs}>{(["general", "gift", "meal", "travel"] as WishlistItem["item_kind"][]).map(kind => <TouchableOpacity key={kind} style={[s.pillButton, wishKind === kind && s.pillActive]} onPress={() => setWishKind(kind)}><Text>{({ general: "共同", gift: "礼物", meal: "想吃", travel: "想去" } as const)[kind]}</Text></TouchableOpacity>)}</View>}<Button title={editingWish ? "保存修改" : "添加愿望"} onPress={() => void saveWish()}/>{editingWish && <Button quiet title="取消编辑" onPress={() => { setEditingWish(null); setWishText(""); setWishKind("general"); }}/>}</Card>
    {(data?.calendar || []).map(item => <Card key={item.id}><Text style={s.title}>{item.title}</Text><Text style={s.sub}>{item.entry_type} · {item.all_day ? item.starts_at.slice(0, 10) : item.starts_at}{item.ends_at ? ` → ${item.ends_at}` : ""} {item.completed_at ? "· 已完成" : ""}</Text><Text style={s.tiny}>{item.participant_ids.length ? `参与：${item.participant_ids.length} 人` : "未指定参与人"}{item.blocks_time ? " · 占用约会时间" : ""}{item.recurrence !== "none" ? ` · 重复：${item.recurrence}` : ""}</Text><View style={s.tabs}>{item.entry_type === "todo" && <Button quiet title={item.completed_at ? "恢复待办" : "标记完成"} onPress={() => void client.completeCalendar(item.id, !item.completed_at, item.version).then(refresh)}/>}<Button quiet title="编辑" onPress={() => { setEditingLife(item); setLifeForm({ title: item.title, allDay: item.all_day, startsAt: item.all_day ? item.starts_at.slice(0, 10) : item.starts_at, endsAt: item.all_day ? (item.ends_at || "").slice(0, 10) : item.ends_at || "", participantIds: item.participant_ids || [], blocksTime: item.blocks_time, type: item.entry_type, recurrence: item.recurrence }); }}/><Button quiet title="删除" onPress={() => void client.deleteCalendar(item.id, item.version).then(refresh).catch(error => Alert.alert("删除失败", error.message))}/></View></Card>)}
    <Card><Text style={s.title}>{editingLife ? "编辑共同事项" : "添加日历 / 纪念日 / 待办"}</Text><Field value={lifeForm.title} onChangeText={title => setLifeForm({ ...lifeForm, title })} placeholder="标题"/><View style={s.tabs}>{(["todo", "event", "anniversary"] as const).map(type => <TouchableOpacity key={type} style={s.pillButton} onPress={() => setLifeForm({ ...lifeForm, type })}><Text>{type === "todo" ? "待办" : type === "event" ? "日历" : "纪念日"}</Text></TouchableOpacity>)}</View><Button quiet title={lifeForm.allDay ? "全天事项（点此改为定时）" : "定时事项（点此改为全天）"} onPress={() => setLifeForm(value => { const nextAllDay = !value.allDay; return { ...value, allDay: nextAllDay, startsAt: nextAllDay ? value.startsAt.slice(0, 10) : `${value.startsAt.slice(0, 10)}T09:00`, endsAt: value.endsAt ? (nextAllDay ? value.endsAt.slice(0, 10) : `${value.endsAt.slice(0, 10)}T10:00`) : "" }; })}/><Field value={lifeForm.startsAt} onChangeText={startsAt => setLifeForm({ ...lifeForm, startsAt })} placeholder={lifeForm.allDay ? "开始日期 YYYY-MM-DD" : "开始 YYYY-MM-DDTHH:mm"}/><Field value={lifeForm.endsAt} onChangeText={endsAt => setLifeForm({ ...lifeForm, endsAt })} placeholder={lifeForm.allDay ? "结束日期（多日事项可填） YYYY-MM-DD" : "结束 YYYY-MM-DDTHH:mm"}/><Text style={s.sectionTitle}>重复</Text><View style={s.tabs}>{(["none", "daily", "weekly", "monthly", "yearly"] as CalendarRecurrence[]).map(recurrence => <TouchableOpacity key={recurrence} style={[s.pillButton, lifeForm.recurrence === recurrence && s.pillActive]} onPress={() => setLifeForm({ ...lifeForm, recurrence })}><Text>{({ none: "不重复", daily: "每天", weekly: "每周", monthly: "每月", yearly: "每年" } as Record<CalendarRecurrence, string>)[recurrence]}</Text></TouchableOpacity>)}</View><Text style={s.sectionTitle}>参与人</Text><View style={s.tabs}>{[data?.user, data?.partner].filter(Boolean).map(person => <TouchableOpacity key={person!.id} style={[s.pillButton, lifeForm.participantIds.includes(person!.id) && s.pillActive]} onPress={() => setLifeForm({ ...lifeForm, participantIds: lifeForm.participantIds.includes(person!.id) ? lifeForm.participantIds.filter(id => id !== person!.id) : [...lifeForm.participantIds, person!.id] })}><Text>{lifeForm.participantIds.includes(person!.id) ? "✓ " : ""}{person!.nickname}</Text></TouchableOpacity>)}</View><Button quiet title={lifeForm.blocksTime ? "✓ 占用约会时间" : "不占用约会时间"} onPress={() => setLifeForm({ ...lifeForm, blocksTime: !lifeForm.blocksTime })}/><Button title={editingLife ? "保存修改" : "保存共同事项"} onPress={() => void saveLife()}/>{editingLife && <Button quiet title="取消编辑" onPress={() => { setEditingLife(null); setLifeForm(emptyLifeForm()); }}/>}</Card>
    {(data?.preferences || []).map(item => <Card key={item.id}><Text style={s.title}>{item.subject_name} · {item.label}</Text><Text style={s.sub}>{item.category} · {item.value}</Text><View style={s.tabs}><Button quiet title="编辑" onPress={() => { setEditingPreference(item); setPrefForm({ label: item.label, value: item.value }); }}/><Button quiet title="删除" onPress={() => void client.deletePreference(item.id, item.version).then(refresh).catch(error => Alert.alert("删除失败", error.message))}/></View></Card>)}<Card><Text style={s.title}>{editingPreference ? "编辑偏好" : "记录对方偏好"}</Text><Field value={prefForm.label} onChangeText={label => setPrefForm({ ...prefForm, label })} placeholder="名称，例如奶茶甜度"/><Field value={prefForm.value} onChangeText={value => setPrefForm({ ...prefForm, value })} placeholder="内容，例如三分糖"/><Button title={editingPreference ? "保存修改" : "保存偏好"} onPress={() => void savePreference()}/>{editingPreference && <Button quiet title="取消编辑" onPress={() => { setEditingPreference(null); setPrefForm({ label: "", value: "" }); }}/>}</Card></>;
  const content = surface === "home" ? renderHome() : surface === "chat" ? renderChat() : surface === "trip" ? renderTrip() : surface === "timeline" ? renderTimeline() : renderLife();
  const injected = `window.__LOVE_TOKEN=${JSON.stringify(token)};document.cookie='lm_session='+encodeURIComponent(window.__LOVE_TOKEN)+'; Path=/; SameSite=Lax';const _f=window.fetch;window.fetch=(i,o={})=>_f(i,{...o,headers:{...(o.headers||{}),Authorization:'Bearer '+window.__LOVE_TOKEN}});true;`;

  const webPanelUrl = webPanel === "call" ? `${API}/?screen=chat` : webPanel === "account" ? `${API}/?screen=home&account=1` : webPanel === "memory" ? `${API}/?screen=timeline&memory_search=1&memory_tools=1` : webPanel === "album" ? `${API}/?screen=timeline&memory_search=1&memory_tools=1&album=1` : webPanel === "meals" ? `${API}/?screen=life&life_section=meals&meal_section=calendar` : webPanel === "cook" ? `${API}/?screen=life&life_section=meals&${cookRoute}` : webPanel === "trip-tools" ? `${API}/?screen=trip&trip_tool=emergency&trip_id=${encodeURIComponent(tripToolsTripId)}` : `${API}/?screen=home`;
  return <SafeAreaView style={s.safe}><ScrollView contentContainerStyle={s.body}><Text style={s.brand}>恋屿 · LOVE MOMENTS</Text>{renderSmartCards()}{content}</ScrollView><View style={s.nav}>{surfaces.map(item => <TouchableOpacity key={item} onPress={() => setSurface(item)} accessibilityRole="tab"><Text style={item === surface ? s.active : s.inactive}>{labels[item]}</Text></TouchableOpacity>)}</View><Modal visible={Boolean(webPanel)} animationType="slide" onRequestClose={() => void closeWebPanel()}><SafeAreaView style={s.safe}><Button quiet title={webPanel === "call" ? "关闭通话页" : webPanel === "account" ? "关闭账号中心" : webPanel === "memory" || webPanel === "album" ? "关闭回忆工具" : webPanel === "meals" ? "关闭食材替代页" : "关闭完整功能页"} onPress={() => void closeWebPanel()}/>{webPanel && <WebView source={{ uri: webPanelUrl }} injectedJavaScriptBeforeContentLoaded={injected} mediaPlaybackRequiresUserAction={false} allowsInlineMediaPlayback />}</SafeAreaView></Modal></SafeAreaView>;
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
  decisionBlock: { paddingTop: 14, marginTop: 12, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: "#eaded7" },
  prefill: { marginTop: 14, padding: 13, borderRadius: 14, borderWidth: 1, borderColor: "#7d2638", backgroundColor: "#fff3f2" },
  dateVariant: { marginTop: 10, padding: 12, borderRadius: 14, backgroundColor: "#f8eeea" }, dateItem: { marginTop: 8, paddingTop: 8, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: "#dacbc6" },
  ingredientLine: { paddingVertical: 7, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: "#eaded7" }, ingredientName: { color: "#2a2020", fontWeight: "600" },
  stepCard: { padding: 13, marginTop: 9, borderRadius: 13, backgroundColor: "#f8eeea" }, stepTitle: { color: "#7d2638", fontWeight: "700" }, stepMeta: { marginTop: 8, color: "#7d2638", fontSize: 12 }, keyPoint: { marginTop: 7, color: "#5e4c49", fontSize: 12, lineHeight: 18 },
  login: { flexGrow: 1, justifyContent: "center", padding: 28 }, loginTitle: { marginVertical: 24, fontSize: 28, color: "#2a2020", fontWeight: "700" }, note: { color: "#8d7b78", lineHeight: 22, marginBottom: 12 }
});
