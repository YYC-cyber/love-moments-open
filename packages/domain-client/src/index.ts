import { uiManifestSchema, type Surface, type UiManifest } from "@love-moments/ui-contract";

export interface ManifestCache { get(surface: Surface): Promise<unknown>; set(surface: Surface, value: UiManifest): Promise<void>; }

export interface RealtimeEvent {
  sequence: number;
  event_type: string;
  resource_id: string;
  actor_id: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface CoupleMemberContext {
  id: string;
  nickname: string;
  is_me: boolean;
  city?: string;
  timezone: string;
  local_time: string;
  status_text?: string;
  emoji?: string;
  weather?: { temperature: number; apparent_temperature: number; label: string; source: string } | null;
}

export interface CoupleContext {
  members: CoupleMemberContext[];
  anniversary: string;
  anniversary_days: number;
  time_difference_minutes: number;
  widget_version: number;
}

export interface DatePlanVariant {
  id: string;
  style: string;
  name: string;
  summary: string;
  expected_cost_cents: number;
  intensity: string;
  items: Array<{ id: string; startTime: string; title: string; note: string; costCents: number }>;
}

export interface DatePlan {
  id: string;
  title: string;
  city: string;
  plan_date: string;
  status: "draft" | "voting" | "pending_confirmation" | "confirmed" | "completed";
  version: number;
  variants: DatePlanVariant[];
  my_vote?: string | null;
  both_voted: boolean;
  confirmed_by_me: boolean;
}

export interface CallRecord {
  id: string;
  caller_id: string;
  callee_id: string;
  state: "ringing" | "connected" | "rejected" | "cancelled" | "missed" | "ended";
  started_at: string;
  answered_at?: string | null;
  ended_at?: string | null;
  duration_seconds: number;
  note: string;
  is_caller: boolean;
  peer: { id: string; nickname: string; avatar_label: string };
}

export interface CallSignal {
  sequence: number;
  call_id: string;
  sender_id: string;
  type: "offer" | "answer" | "ice";
  payload: Record<string, unknown>;
  created_at: string;
}

export interface WebRtcConfig {
  iceServers: RTCIceServer[];
  turnAvailable: boolean;
  iceTransportPolicy: "all" | "relay";
  provider: string;
  ttlSeconds: number;
  warning?: string;
}

export interface RtcProviderConfig {
  provider: "shengwang" | "webrtc";
  requestedProvider: string;
  shengwangAvailable: boolean;
  sdkVersion: string;
  warning?: string | null;
}

export interface ShengwangRtcToken {
  provider: "shengwang";
  enabled: true;
  appId: string;
  channel: string;
  uid: number;
  token: string;
  expiresAt: string;
  ttlSeconds: number;
  sdkVersion: string;
}

export interface PlaceCandidate { candidate_id: string; provider: "amap" | "google_maps" | "manual"; provider_place_id: string; name: string; formatted_address: string; country: string; province: string; city: string; district: string; latitude?: number | null; longitude?: number | null; category: string; }
export interface TripSummary { id: string; title: string; destination: string; start_date: string; end_date: string; budget_cents: number; status: string; variants: unknown[]; destination_place?: Omit<PlaceCandidate, "candidate_id"> | null; needs_destination_detail?: boolean; }
export interface TripGenerationJob { id: string; status: "queued" | "running" | "completed" | "failed" | "cancelled"; trip_id?: string | null; trip?: TripSummary; error_message?: string | null; }
export interface QuoteItem {
  id: string;
  quote_type: "hotel" | "flight" | "train" | "transport";
  provider: string;
  source_mode: "mock" | "aggregate" | "live" | "manual";
  title: string;
  subtitle?: string;
  amount_cents: number;
  currency: string;
  terms?: string;
  includes_tax: boolean;
  booking_url?: string;
  fetched_at: string;
  selected_at?: string | null;
  details?: { seats?: Array<{ name: string; price_cents: number; remaining: string }>; latitude?: number | null; longitude?: number | null; rating?: number | null; reviews?: number | null };
}
export interface MessageItem { id: string; sender_id: string; sender_name: string; type: string; content: string; sent_at: string; mine: boolean; metadata: Record<string, unknown>; }
export interface MomentItem { id: string; creator_id: string; type: string; title: string; body: string; occurred_at: string; visibility: string; version: number; deleted_at?: string | null; }
export interface MediaItem { id: string; owner_id: string; kind: "image" | "audio" | "video"; original_name: string; mime_type: string; size_bytes: number; visibility: "shared" | "private" | "delayed"; status: "ready" | "trashed" | "purged"; url: string; thumbnail_url: string; width?: number | null; height?: number | null; metadata_stripped: boolean; deleted_at?: string | null; version: number; }
export interface CalendarEntry { id: string; entry_type: "event" | "anniversary" | "todo"; title: string; starts_at: string; completed_at?: string | null; recurrence: string; version: number; }
export interface PartnerPreference { id: string; subject_user_id: string; subject_name?: string; category: string; label: string; value: string; note: string; version: number; }
export interface WishlistItem { id: string; text: string; note: string; completed_at?: string | null; version: number; }
export interface RecipeIngredient { id?: string; name: string; display_quantity?: string; quantity?: number; unit?: string; specification?: string; preparation?: string; ingredient_group?: "main" | "seasoning"; optional?: number | boolean; }
export interface RecipeStep { id?: string; title?: string; instruction: string; timer_seconds?: number | null; heat_level?: string; key_point?: string; position?: number; }
export interface RecipeSummary { id: string; name: string; cuisine: "chinese" | "western"; category: string; meal_slots: Array<"breakfast" | "lunch" | "dinner" | "snack">; base_servings: number; prep_minutes: number; cook_minutes: number; passive_minutes: number; active_minutes: number; total_minutes: number; difficulty: "easy" | "medium" | "hard"; source_type: string; visibility: string; version: number; }
export interface RecipeAllergenDetail { name: string; sources: string[]; basis: string; }
export interface RecipeDetail extends RecipeSummary { ingredients: RecipeIngredient[]; steps: RecipeStep[]; tips: string[]; allergen_tags: string[]; allergen_details?: RecipeAllergenDetail[]; allergen_review_ingredients?: string[]; allergen_note?: string; }
export interface MealWish { id: string; recipe_id?: string | null; title: string; note: string; creator_id: string; creator_name?: string; version: number; created_at: string; }
export interface MealPlanItem { id: string; recipe_id?: string | null; name: string; planned_date: string; meal_slot: "breakfast" | "lunch" | "dinner" | "snack"; planned_time?: string | null; servings: number; status: "selected" | "accepted" | "shopping" | "cooking" | "completed" | "cancelled"; course_type: string; note: string; version: number; recipe_snapshot: Record<string, unknown>; }
export interface MealCart { id?: string | null; title: string; status: string; version: number; items: MealPlanItem[]; }
export interface MealCalendarDay { date: string; meals: Record<MealPlanItem["meal_slot"], MealPlanItem[]>; }
export interface MealCalendar { timezone: string; from: string; to: string; days: MealCalendarDay[]; }
export interface ShoppingItem { id: string; display_name: string; display_quantity: string; category: string; checked: boolean; manual_locked: boolean; version: number; sources?: Array<Record<string, unknown>>; }
export interface ShoppingList { id?: string | null; title: string; status: string; range_start?: string; range_end?: string; version: number; items: ShoppingItem[]; }
export interface ShoppingPreview { preview_id: string; preview: { id: string; range_start: string; range_end: string; meal_count: number; items: Array<Record<string, unknown>>; diff: { added: Array<Record<string, unknown>>; updated: Array<Record<string, unknown>>; removed: Array<Record<string, unknown>>; protected: Array<Record<string, unknown>>; warnings: string[] }; expires_at: string } }

export class LoveMomentsClient {
  private accessToken?: string;
  private pendingUploads = new Map<string, string>();
  constructor(private baseUrl = "", private cache?: ManifestCache) {}
  setAccessToken(token?: string) { this.accessToken = token || undefined; }
  private idempotency(prefix: string) { const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`; return `client:${prefix}:${random}`; }
  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      credentials: "include",
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(this.accessToken ? { Authorization: `Bearer ${this.accessToken}` } : {}),
        ...init?.headers
      }
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(data.error || `请求失败 (${response.status})`), { status: response.status, code: data.code, details: data.details, currentVersion: data.details?.current_version });
    return data as T;
  }
  private async rawRequest<T = Record<string, unknown>>(path: string, body: Blob, headers: Record<string, string>, method = "POST"): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, { method, credentials: "include", headers: { ...(this.accessToken ? { Authorization: `Bearer ${this.accessToken}` } : {}), ...headers }, body });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(data.error || `上传失败 (${response.status})`), { status: response.status, code: data.code, details: data.details });
    return data as T;
  }
  async getManifest(surface: Surface): Promise<UiManifest> {
    try {
      const manifest = uiManifestSchema.parse(await this.request(`/api/v1/ui/${surface}`));
      await this.cache?.set(surface, manifest); return manifest;
    } catch (error) {
      const cached = await this.cache?.get(surface);
      if (cached) return uiManifestSchema.parse({ ...(cached as object), generatedBy: "cache" });
      throw error;
    }
  }
  async login(username: string, password: string, transport: "cookie" | "bearer" = this.baseUrl ? "bearer" : "cookie") {
    const result = await this.request<{ user: { id: string; nickname: string }; pairing: { paired: boolean }; access_token?: string }>("/api/v1/auth/login", { method: "POST", body: JSON.stringify({ username, password, token_transport: transport }) });
    if (result.access_token) this.setAccessToken(result.access_token);
    return result;
  }
  async register(username: string, password: string, nickname: string, transport: "cookie" | "bearer" = this.baseUrl ? "bearer" : "cookie") {
    const result = await this.request<{ user: { id: string; nickname: string }; pairing: { paired: boolean }; access_token?: string }>("/api/v1/auth/register", { method: "POST", body: JSON.stringify({ username, password, nickname, token_transport: transport }) });
    if (result.access_token) this.setAccessToken(result.access_token);
    return result;
  }
  async logout() { const result = await this.request("/api/v1/auth/logout", { method: "POST", body: "{}" }); this.setAccessToken(); return result; }
  me() { return this.request<{ authenticated: boolean }>("/api/v1/auth/me"); }
  bootstrap<T = Record<string, unknown>>() { return this.request<T>("/api/v1/bootstrap"); }
  createInvite() { return this.request<{ code: string; expires_at: string }>("/api/v1/couples/invites", { method: "POST", body: "{}" }); }
  acceptInvite(code: string, anniversary: string) { return this.request("/api/v1/couples/invites/accept", { method: "POST", body: JSON.stringify({ code, anniversary }) }); }
  messages() { return this.request<{ items: MessageItem[] }>("/api/v1/messages"); }
  sendMessage(content: string, idempotencyKey: string) { return this.request<MessageItem>("/api/v1/messages", { method: "POST", body: JSON.stringify({ type: "text", content, idempotency_key: idempotencyKey }) }); }
  messageToMoment(messageId: string, title = "一段想记住的聊天", visibility = "shared") { return this.request<MomentItem>(`/api/v1/messages/${messageId}/moment`, { method: "POST", body: JSON.stringify({ title, visibility }) }); }
  moments() { return this.request<{ items: MomentItem[] }>("/api/v1/moments"); }
  momentTrash() { return this.request<{ items: MomentItem[] }>("/api/v1/moments/trash"); }
  trashMoment(id: string) { return this.request(`/api/v1/moments/${id}/trash`, { method: "POST", body: "{}" }); }
  restoreMoment(id: string, version: number) { return this.request<MomentItem>(`/api/v1/moments/${id}/restore`, { method: "POST", body: JSON.stringify({ expected_version: version }) }); }
  trips() { return this.request<{ items: TripSummary[] }>("/api/v1/trips"); }
  searchPlaces(query: string, regionHint = "", includeGlobal = true) { return this.request<{ items: PlaceCandidate[]; notice: string; configured: boolean }>("/api/v1/places/search", { method: "POST", body: JSON.stringify({ query, region_hint: regionHint, include_global: includeGlobal }) }); }
  startTripGeneration(value: Record<string, unknown>, idempotencyKey = this.idempotency("trip")) { return this.request<TripGenerationJob>("/api/v1/trip-generation-jobs", { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  tripGenerationJob(id: string) { return this.request<TripGenerationJob>(`/api/v1/trip-generation-jobs/${id}`); }
  async createTrip(value: Record<string, unknown>) {
    let job = await this.startTripGeneration(value);
    for (let attempt = 0; attempt < 100; attempt += 1) {
      if (job.status === "completed" && job.trip) return job.trip;
      if (job.status === "failed" || job.status === "cancelled") throw new Error(job.error_message || "旅行方案生成失败");
      await new Promise(resolve => setTimeout(resolve, 1500));
      job = await this.tripGenerationJob(job.id);
    }
    throw new Error("旅行仍在后台生成，请稍后刷新旅行页面");
  }
  tripRoute(id: string, value: Record<string, unknown> = {}) { return this.request<Record<string, unknown>>(`/api/v1/trips/${id}/routes`, { method: "POST", body: JSON.stringify(value) }); }
  quotes(value: Record<string, unknown>) { return this.request<{ items: QuoteItem[]; mock: boolean; notice: string }>("/api/v1/quotes/search", { method: "POST", body: JSON.stringify(value) }); }
  selectQuote(id: string, expectedVersion?: number) { return this.request<QuoteItem>(`/api/v1/quotes/${id}/select`, { method: "POST", body: JSON.stringify({ expected_version: expectedVersion }) }); }
  createChatImport(value: Record<string, unknown>) { return this.request<Record<string, any>>("/api/v1/chat-imports", { method: "POST", body: JSON.stringify(value) }); }
  updateChatImportItem(importId: string, itemId: string, value: Record<string, unknown>) { return this.request(`/api/v1/chat-imports/${importId}/items/${itemId}`, { method: "PATCH", body: JSON.stringify(value) }); }
  confirmChatImport(importId: string, asMoments = false) { return this.request(`/api/v1/chat-imports/${importId}/confirm`, { method: "POST", body: JSON.stringify({ as_moments: asMoments }) }); }
  calendar() { return this.request<{ items: CalendarEntry[] }>("/api/v1/calendar"); }
  createCalendar(value: Record<string, unknown>) { return this.request<CalendarEntry>("/api/v1/calendar", { method: "POST", body: JSON.stringify(value) }); }
  updateCalendar(id: string, value: Record<string, unknown>) { return this.request<CalendarEntry>(`/api/v1/calendar/${id}`, { method: "PATCH", body: JSON.stringify(value) }); }
  completeCalendar(id: string, completed: boolean, version: number) { return this.request<CalendarEntry>(`/api/v1/calendar/${id}/complete`, { method: "POST", body: JSON.stringify({ completed, expected_version: version }) }); }
  deleteCalendar(id: string, version: number) { return this.request(`/api/v1/calendar/${id}`, { method: "DELETE", body: JSON.stringify({ expected_version: version }) }); }
  preferences() { return this.request<{ items: PartnerPreference[] }>("/api/v1/preferences"); }
  createPreference(value: Record<string, unknown>) { return this.request<PartnerPreference>("/api/v1/preferences", { method: "POST", body: JSON.stringify(value) }); }
  updatePreference(id: string, value: Record<string, unknown>) { return this.request<PartnerPreference>(`/api/v1/preferences/${id}`, { method: "PATCH", body: JSON.stringify(value) }); }
  deletePreference(id: string, version: number) { return this.request(`/api/v1/preferences/${id}`, { method: "DELETE", body: JSON.stringify({ expected_version: version }) }); }
  wishlist() { return this.request<{ id: string | null; title: string; items: WishlistItem[] }>("/api/v1/lists/wishlist"); }
  createWishlistItem(value: { text: string; note?: string }) { return this.request<WishlistItem>("/api/v1/lists/wishlist/items", { method: "POST", body: JSON.stringify(value) }); }
  updateWishlistItem(id: string, value: { text: string; note?: string; expected_version: number }) { return this.request<WishlistItem>(`/api/v1/lists/items/${id}`, { method: "PATCH", body: JSON.stringify(value) }); }
  toggleWishlistItem(id: string, completed: boolean) { return this.request<WishlistItem>(`/api/v1/lists/items/${id}`, { method: "PATCH", body: JSON.stringify({ completed }) }); }
  deleteWishlistItem(id: string, version: number) { return this.request(`/api/v1/lists/items/${id}`, { method: "DELETE", body: JSON.stringify({ expected_version: version }) }); }
  recipes(value: { q?: string; cuisine?: "chinese" | "western"; meal_slot?: string; difficulty?: "easy" | "medium" | "hard"; max_minutes?: number; cursor?: string; limit?: number } = {}) { const query = new URLSearchParams(Object.entries(value).filter(([, item]) => item !== undefined).map(([key, item]) => [key, String(item)])); return this.request<{ items: RecipeSummary[]; next_cursor: string | null; total: number }>(`/api/v1/recipes?${query}`); }
  recipe(id: string) { return this.request<RecipeDetail>(`/api/v1/recipes/${id}`); }
  createRecipe(value: Record<string, unknown>) { return this.request<RecipeDetail>("/api/v1/recipes", { method: "POST", body: JSON.stringify(value) }); }
  updateRecipe(id: string, value: Record<string, unknown>) { return this.request<RecipeDetail>(`/api/v1/recipes/${id}`, { method: "PATCH", body: JSON.stringify(value) }); }
  deleteRecipe(id: string, version: number) { return this.request(`/api/v1/recipes/${id}`, { method: "DELETE", body: JSON.stringify({ expected_version: version }) }); }
  generateRecipe(value: Record<string, unknown>) { return this.request<{ id: string; status: string; draft: { id: string; recipe: RecipeDetail }; mock: boolean }>("/api/v1/recipes/generate", { method: "POST", body: JSON.stringify(value) }); }
  searchOnlineRecipes(query: string, provider = "themealdb") { return this.request<{ items: Array<{ id: string; recipe: RecipeDetail }>; provider: string; mock: boolean; notice: string }>("/api/v1/recipes/search-online", { method: "POST", body: JSON.stringify({ query, provider, limit: 5 }) }); }
  confirmRecipeDraft(id: string, visibility: "couple" | "private" = "couple") { return this.request<RecipeDetail>(`/api/v1/recipes/drafts/${id}/confirm`, { method: "POST", body: JSON.stringify({ visibility }) }); }
  importRecipe(draftId: string, visibility: "couple" | "private" = "couple") { return this.request<RecipeDetail>("/api/v1/recipes/import", { method: "POST", body: JSON.stringify({ draft_id: draftId, visibility }) }); }
  mealWishes() { return this.request<{ items: MealWish[] }>("/api/v1/meal-wishes"); }
  createMealWish(value: { recipe_id?: string | null; title?: string; note?: string }) { return this.request<MealWish>("/api/v1/meal-wishes", { method: "POST", body: JSON.stringify(value) }); }
  deleteMealWish(id: string, version: number) { return this.request(`/api/v1/meal-wishes/${id}`, { method: "DELETE", body: JSON.stringify({ expected_version: version }) }); }
  mealCart() { return this.request<MealCart>("/api/v1/meal-cart"); }
  scheduleMeal(value: Record<string, unknown>) { return this.request<MealPlanItem>("/api/v1/meal-cart/items", { method: "POST", body: JSON.stringify({ ...value, idempotency_key: value.idempotency_key || this.idempotency("meal") }) }); }
  updateMeal(id: string, value: Record<string, unknown>) { return this.request<MealPlanItem>(`/api/v1/meal-cart/items/${id}`, { method: "PATCH", body: JSON.stringify(value) }); }
  cancelMeal(id: string, version: number) { return this.request<MealPlanItem>(`/api/v1/meal-cart/items/${id}`, { method: "DELETE", body: JSON.stringify({ expected_version: version }) }); }
  repeatMeal(id: string, value: Record<string, unknown>) { return this.request<MealPlanItem>(`/api/v1/meal-plans/${id}/repeat`, { method: "POST", body: JSON.stringify({ ...value, idempotency_key: value.idempotency_key || this.idempotency("meal-repeat") }) }); }
  mealCalendar(from: string, to: string) { return this.request<MealCalendar>(`/api/v1/meal-calendar?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`); }
  mealArchive(year: number, month: number) { return this.request<MealCalendar & { year: number; month: number }>(`/api/v1/meal-archive?year=${year}&month=${month}`); }
  shoppingList() { return this.request<ShoppingList>("/api/v1/shopping-lists/active"); }
  previewShopping(value: { range_start: string; range_end: string; expected_version?: number }) { return this.request<ShoppingPreview>("/api/v1/shopping-lists/preview", { method: "POST", body: JSON.stringify(value) }); }
  applyShopping(previewId: string, idempotencyKey?: string) { return this.request<ShoppingList>("/api/v1/shopping-lists/apply", { method: "POST", body: JSON.stringify({ preview_id: previewId, idempotency_key: idempotencyKey || this.idempotency("shopping") }) }); }
  createShoppingItem(value: Record<string, unknown>) { return this.request<ShoppingItem>("/api/v1/shopping-lists/items", { method: "POST", body: JSON.stringify(value) }); }
  updateShoppingItem(id: string, value: Record<string, unknown>) { return this.request<ShoppingItem>(`/api/v1/shopping-lists/items/${id}`, { method: "PATCH", body: JSON.stringify(value) }); }
  deleteShoppingItem(id: string, version: number) { return this.request(`/api/v1/shopping-lists/items/${id}`, { method: "DELETE", body: JSON.stringify({ expected_version: version }) }); }
  completeShopping(value: { expected_version: number; allow_incomplete?: boolean }) { return this.request("/api/v1/shopping-lists/complete", { method: "POST", body: JSON.stringify(value) }); }
  timelineMonths() { return this.request<{ items: Array<Record<string, unknown>> }>("/api/v1/timeline/months"); }
  footprints() { return this.request<{ items: Array<Record<string, unknown>>; mock: boolean; notice: string }>("/api/v1/timeline/footprints"); }
  annualReport(year: number) { return this.request<Record<string, unknown>>(`/api/v1/timeline/reports/${year}`); }
  devices() { return this.request<{ items: Array<Record<string, unknown>> }>("/api/v1/account/devices"); }
  revokeDevice(id: string) { return this.request(`/api/v1/account/devices/${id}/revoke`, { method: "POST", body: "{}" }); }
  changePassword(currentPassword: string, newPassword: string, confirmation: string) { return this.request<{ changed: boolean; revoked_sessions: number }>("/api/v1/account/password", { method: "POST", body: JSON.stringify({ current_password: currentPassword, new_password: newPassword, new_password_confirmation: confirmation }) }); }
  unlink(password: string) { return this.request("/api/v1/couple/unlink", { method: "POST", body: JSON.stringify({ password, confirmation: "解除情侣" }) }); }
  deleteAccount(password: string) { return this.request("/api/v1/account/delete", { method: "POST", body: JSON.stringify({ password, confirmation: "注销账号" }) }); }
  exportUrl() { return `${this.baseUrl}/api/v1/account/export.zip`; }
  coupleContext() { return this.request<CoupleContext>("/api/v1/couple/context"); }
  events(after = 0) { return this.request<{ items: RealtimeEvent[]; latest_sequence: number }>(`/api/v1/realtime/events?after=${Math.max(0, after)}`); }
  webrtcConfig() { return this.request<WebRtcConfig>("/api/v1/webrtc/config"); }
  rtcConfig() { return this.request<RtcProviderConfig>("/api/v1/rtc/config"); }
  rtcToken(callId: string) { return this.request<ShengwangRtcToken>(`/api/v1/calls/${callId}/rtc-token`, { method: "POST", body: "{}" }); }
  activeCall() { return this.request<{ call: CallRecord | null }>("/api/v1/calls/active"); }
  startCall() { return this.request<CallRecord>("/api/v1/calls", { method: "POST", body: "{}" }); }
  answerCall(callId: string) { return this.request<CallRecord>(`/api/v1/calls/${callId}/answer`, { method: "POST", body: "{}" }); }
  declineCall(callId: string) { return this.request<CallRecord>(`/api/v1/calls/${callId}/decline`, { method: "POST", body: "{}" }); }
  cancelCall(callId: string) { return this.request<CallRecord>(`/api/v1/calls/${callId}/cancel`, { method: "POST", body: "{}" }); }
  endCall(callId: string, durationSeconds: number, note = "") { return this.request<CallRecord>(`/api/v1/calls/${callId}/end`, { method: "POST", body: JSON.stringify({ duration_seconds: durationSeconds, note }) }); }
  callSignals(callId: string, after = 0) { return this.request<{ items: CallSignal[]; latest_sequence: number }>(`/api/v1/calls/${callId}/signals?after=${Math.max(0, after)}`); }
  sendCallSignal(callId: string, type: CallSignal["type"], payload: Record<string, unknown>) { return this.request<{ sequence: number }>(`/api/v1/calls/${callId}/signal`, { method: "POST", body: JSON.stringify({ type, payload }) }); }
  setStatus(value: { status_code: string; text?: string; expires_at?: string | null }) { return this.request("/api/v1/me/status", { method: "PUT", body: JSON.stringify(value) }); }
  clearStatus() { return this.request("/api/v1/me/status/clear", { method: "POST", body: "{}" }); }
  setLocation(value: { city: string; timezone: string; latitude?: number; longitude?: number }) { return this.request("/api/v1/me/location", { method: "PUT", body: JSON.stringify(value) }); }
  weeklyReview() { return this.request<Record<string, unknown>>("/api/v1/weekly-reviews/current"); }
  answerWeekly(reviewId: string, value: { favorite: string; gratitude: string; next_week: string }) { return this.request(`/api/v1/weekly-reviews/${reviewId}/answer`, { method: "PUT", body: JSON.stringify(value) }); }
  confirmWeekly(reviewId: string) { return this.request(`/api/v1/weekly-reviews/${reviewId}/confirm`, { method: "POST", body: "{}" }); }
  datePlans() { return this.request<{ items: DatePlan[] }>("/api/v1/date-plans"); }
  createDatePlan(value: Record<string, unknown>) { return this.request<DatePlan>("/api/v1/date-plans", { method: "POST", body: JSON.stringify(value) }); }
  voteDatePlan(planId: string, variantId: string) { return this.request<DatePlan>(`/api/v1/date-plans/${planId}/vote`, { method: "PUT", body: JSON.stringify({ variant_id: variantId }) }); }
  composeDatePlan(planId: string, variantId: string, itemIds?: string[]) { return this.request<DatePlan>(`/api/v1/date-plans/${planId}/compose`, { method: "POST", body: JSON.stringify({ variant_id: variantId, item_ids: itemIds }) }); }
  confirmDatePlan(planId: string, version: number) { return this.request<DatePlan>(`/api/v1/date-plans/${planId}/confirm`, { method: "POST", body: JSON.stringify({ version }) }); }
  completeDatePlan(planId: string, value: { note: string; actual_cost?: number; media_id?: string }) { return this.request<DatePlan>(`/api/v1/date-plans/${planId}/complete`, { method: "POST", body: JSON.stringify(value) }); }
  async uploadMedia(file: Blob, name: string, visibility: "shared" | "private" = "shared", thumbnail?: Blob) {
    const fingerprint = `${name}:${file.size}:${file.type}:${visibility}`;
    if (file.type.startsWith("video/") || file.size > 8 * 1024 * 1024) {
      let session: { id: string; received_size: number; chunk_size: number; expected_size: number; status: string } | undefined;
      const remembered = this.pendingUploads.get(fingerprint);
      if (remembered) {
        try { session = await this.request(`/api/v1/media/uploads/${remembered}`); } catch { this.pendingUploads.delete(fingerprint); }
      }
      if (!session || session.status !== "pending" || session.expected_size !== file.size) {
        const created = await this.request<{ id: string; received_size: number; chunk_size: number; expected_size: number; status: string }>("/api/v1/media/uploads", { method: "POST", body: JSON.stringify({ name, mime_type: file.type || "application/octet-stream", size_bytes: file.size, visibility }) });
        session = created;
        this.pendingUploads.set(fingerprint, created.id);
      }
      if (!session) throw new Error("无法创建上传任务");
      const activeSession = session;
      if (thumbnail) {
        await this.rawRequest(`/api/v1/media/uploads/${activeSession.id}/thumbnail`, thumbnail, { "Content-Type": thumbnail.type || "image/jpeg" });
      }
      let offset = Number(activeSession.received_size || 0);
      const chunkSize = Number(activeSession.chunk_size || 4 * 1024 * 1024);
      while (offset < file.size) {
        const chunk = file.slice(offset, Math.min(file.size, offset + chunkSize));
        const status = await this.rawRequest<{ received_size: number }>(`/api/v1/media/uploads/${activeSession.id}/chunk`, chunk, { "Content-Type": "application/octet-stream", "X-Upload-Offset": String(offset) }, "PUT");
        offset = Number(status.received_size);
      }
      const result = await this.request<{ id: string; kind: "image" | "audio" | "video"; url: string; thumbnail_url: string }>(`/api/v1/media/uploads/${activeSession.id}/complete`, { method: "POST", body: "{}" });
      this.pendingUploads.delete(fingerprint);
      return result;
    }
    const response = await fetch(`${this.baseUrl}/api/v1/media`, { method: "POST", credentials: "include", headers: { "Content-Type": file.type || "application/octet-stream", "X-Filename": encodeURIComponent(name), "X-Media-Visibility": visibility, ...(this.accessToken ? { Authorization: `Bearer ${this.accessToken}` } : {}) }, body: file });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(data.error || `上传失败 (${response.status})`), { status: response.status });
    return data as { id: string; kind: "image" | "audio" | "video"; url: string; thumbnail_url: string };
  }
  media() { return this.request<{ items: MediaItem[] }>("/api/v1/media"); }
  mediaTrash() { return this.request<{ items: MediaItem[] }>("/api/v1/media/trash"); }
  mediaAccess(id: string, thumbnail = false, ttlSeconds = 300) { return this.request<{ url: string; expires_at: string }>(`/api/v1/media/${id}/access`, { method: "POST", body: JSON.stringify({ thumbnail, ttl_seconds: ttlSeconds }) }); }
  trashMedia(id: string) { return this.request(`/api/v1/media/${id}/trash`, { method: "POST", body: "{}" }); }
  restoreMedia(id: string, version: number) { return this.request<MediaItem>(`/api/v1/media/${id}/restore`, { method: "POST", body: JSON.stringify({ expected_version: version }) }); }
  purgeMedia(id: string) { return this.request(`/api/v1/media/${id}`, { method: "DELETE", body: "{}" }); }
  async compose(surface: Surface, intent: string, resourceRefs: string[] = []): Promise<UiManifest> {
    let job = await this.request<{ id: string; status: string; manifest?: unknown; errorCode?: string }>(`/api/v1/ui/${surface}/compose`, { method: "POST", body: JSON.stringify({ intent, resourceRefs }) });
    for (let attempt = 0; !job.manifest && attempt < 100; attempt += 1) {
      if (job.status === "failed") throw new Error("智能整理失败，已保留当前稳定页面");
      await new Promise(resolve => setTimeout(resolve, 250));
      job = await this.request(`/api/v1/ui/jobs/${job.id}`);
    }
    return uiManifestSchema.parse(job.manifest);
  }
  preview(actionId: string) { return this.request<{ preview: Record<string, unknown> }>(`/api/v1/ui/actions/${actionId}/preview`, { method: "POST", body: "{}" }); }
  confirm(actionId: string, idempotencyKey: string) { return this.request(`/api/v1/ui/actions/${actionId}/confirm`, { method: "POST", body: JSON.stringify({ idempotencyKey }) }); }
  cancel(actionId: string) { return this.request(`/api/v1/ui/actions/${actionId}/cancel`, { method: "POST", body: "{}" }); }
}
