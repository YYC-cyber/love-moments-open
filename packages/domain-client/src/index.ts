import { uiManifestSchema, type Surface, type UiManifest } from "@love-moments/ui-contract";

const h5SurfaceNames = new Set(["home", "chat", "trip", "timeline", "life"]);
const h5ComponentScreens = new Map<string, Surface>([
  ["couple_overview", "home"], ["daily_question", "home"], ["quick_capture", "home"],
  ["message_summary", "chat"], ["favorite_memory", "chat"], ["suggested_reply", "chat"], ["import_guide", "chat"], ["trip_share", "trip"],
  ["trip_countdown", "trip"], ["trip_variant_compare", "trip"], ["itinerary_timeline", "trip"], ["budget_summary", "trip"], ["hotel_quote", "trip"], ["risk_alert", "trip"], ["impact_diff", "trip"], ["approval_state", "trip"],
  ["featured_memory", "timeline"], ["timeline_summary", "timeline"], ["month_album", "timeline"], ["footprint_summary", "timeline"], ["year_report", "timeline"], ["privacy_status", "timeline"],
  ["wishlist", "life"], ["calendar_summary", "life"], ["anniversary", "life"], ["todo_summary", "life"], ["preference_summary", "life"], ["pending_items", "life"],
]);

/** Convert an untrusted manifest card into a same-origin H5 route. Unknown
 * component types never become query parameters or executable paths. */
export function buildH5SmartLink(surface: Surface | string, componentType: string): string {
  const fallback = h5SurfaceNames.has(surface) ? surface as Surface : "home";
  const mapped = h5ComponentScreens.get(componentType);
  const target = mapped || fallback;
  return `/?screen=${encodeURIComponent(target)}${mapped ? `&smart=${encodeURIComponent(componentType)}` : ""}`;
}

export interface ManifestCache { get(surface: Surface): Promise<unknown>; set(surface: Surface, value: UiManifest): Promise<void>; scope?(): string; }

export interface CacheIdentity { user_id: string; couple_id: string; permission_version: number; }
export interface PrivateStorage {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  getAllKeys(): Promise<readonly string[]>;
  multiRemove(keys: readonly string[]): Promise<void>;
}
export function createPrivateCache(storage: PrivateStorage) {
  let identity: CacheIdentity | null = null;
  const partition = (value: CacheIdentity) => [value.user_id, value.couple_id, value.permission_version].map(item => encodeURIComponent(String(item))).join(":");
  const key = (name: string) => identity ? `love-private:${partition(identity)}:${name}` : null;
  return {
    scope: () => identity ? partition(identity) : "",
    async setIdentity(next: CacheIdentity | null) {
      const old = identity;
      identity = next;
      if (old && (!next || partition(old) !== partition(next))) {
        const prefix = `love-private:${encodeURIComponent(old.user_id)}:`;
        await storage.multiRemove((await storage.getAllKeys()).filter(item => item.startsWith(prefix)));
      }
    },
    async getItem(name: string) { const current = key(name); const value = current ? await storage.getItem(current) : null; return current === key(name) ? value : null; },
    async setItem(name: string, value: string) { const current = key(name); if (current) { await storage.setItem(current, value); if (current !== key(name)) await storage.multiRemove([current]); } },
    async removeItem(name: string) { const current = key(name); if (current) { await storage.multiRemove([current]); } }
  };
}

export interface TripToolsRequestScope { authEpoch: number; identity: string; tripId: string; }
export interface TripToolsRequestToken extends TripToolsRequestScope { generation: number; }
export function createTripToolsInFlightGuard() {
  let generation = 0;
  let current: TripToolsRequestScope | null = null;
  const equal = (left: TripToolsRequestScope, right: TripToolsRequestScope) => (
    left.authEpoch === right.authEpoch && left.identity === right.identity && left.tripId === right.tripId
  );
  return {
    begin(scope: TripToolsRequestScope): TripToolsRequestToken {
      generation += 1;
      current = { ...scope };
      return { ...scope, generation };
    },
    invalidate() { generation += 1; current = null; },
    isCurrent(token: TripToolsRequestToken, scope: TripToolsRequestScope) {
      return Boolean(current) && token.generation === generation && equal(token, scope) && equal(current!, scope);
    },
  };
}

/** Convert a user-entered major-unit decimal to integer cents without any
 * floating-point multiplication or rounding. */
export function parseTripExpenseAmount(value: string): number | null {
  if (typeof value !== "string") return null;
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(value.trim());
  if (!match) return null;
  const whole = match[1].replace(/^0+(?=\d)/, "");
  const fraction = (match[2] || "").padEnd(2, "0");
  const digits = `${whole}${fraction}`.replace(/^0+(?=\d)/, "");
  const cents = parseInt(digits, 10);
  return Number.isSafeInteger(cents) && cents > 0 ? cents : null;
}

export function formatTripExpenseCents(value: number, currency = "CNY"): string {
  if (!Number.isSafeInteger(value)) return "—";
  const negative = value < 0;
  const absolute = BigInt(negative ? -value : value);
  const amount = `${absolute / 100n}.${String(absolute % 100n).padStart(2, "0")}`;
  const prefix = currency === "CNY" ? "¥" : `${currency} `;
  return `${negative ? "-" : ""}${prefix}${amount}`;
}

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
  status_code?: StatusCode;
  expires_at?: string | null;
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
  weather_fit?: string;
  generation_source?: "ai" | "rules" | "legacy_unknown";
  generation_message?: string;
  items: Array<{ id: string; startTime: string; durationMinutes?: number; title: string; note: string; costCents: number; recommendationSource?: ProvenanceCode; placeSource?: ProvenanceCode; priceSource?: ProvenanceCode; weatherSource?: ProvenanceCode | "unavailable"; weatherProvider?: string; weatherEvidenceAt?: string; weatherRisk?: string; commuteMinutes?: number; commuteMode?: string; distanceMeters?: number; commuteSource?: ProvenanceCode; provider?: string; providerPlaceId?: string; placeCandidateId?: string; formattedAddress?: string; latitude?: number | null; longitude?: number | null; evidenceAt?: string; needsPlaceSelection?: boolean }>;
}

export type ProvenanceCode = "human" | "real_api" | "rules" | "ai" | "mock" | "cache" | "legacy_unknown";

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
  generation_source?: "ai" | "rules" | "legacy_unknown";
  generation_message?: string;
  preferences?: { timezone_confirmed?: boolean; [key: string]: unknown };
  location?: Omit<PlaceCandidate, "candidate_id"> | null;
  busy_intervals?: Array<{ start: string; end: string; all_day: boolean }>;
  weather?: { source_type: ProvenanceCode | "unavailable"; original_source_type?: ProvenanceCode; provider?: string; evidence_at?: string; cache_age_seconds?: number | null; summary?: string; precipitation_probability?: number | null; temperature_min?: number | null; temperature_max?: number | null };
}

export interface DecisionOption { id: string; label: string; position: number; }
export interface DecisionCommon { id: string; title: string; selection_mode: "single" | "multiple"; voting_round: number; closes_at: string; options_locked: boolean; options: DecisionOption[]; submitted_by_me: boolean; partner_submitted: boolean; creator_id: string; version: number; created_at: string; updated_at: string; }
export type Decision =
  | (DecisionCommon & { status: "open" | "awaiting_extension" | "cancelled" })
  | (DecisionCommon & { status: "revealed" | "pending_confirmation" | "confirmed" | "converted"; votes: Array<{ user_id: string; option_ids: string[]; options: DecisionOption[] }>; resolution: { kind: string; option_ids?: string[] } });
export interface DecisionConversionPreview { decision_id: string; decision_version: number; target: "date_plan" | "meal_wish" | "trip" | "hotel_search" | "restaurant_search" | "calendar_todo"; source_type: "human"; requires_confirmation: true; saved: false; notice: string; editor: string; values: Record<string, unknown>; missing_fields: string[]; next_screen: string; }

const decisionStatuses = new Set(["open", "awaiting_extension", "cancelled", "revealed", "pending_confirmation", "confirmed", "converted"]);
const revealedDecisionStatuses = new Set(["revealed", "pending_confirmation", "confirmed", "converted"]);
const decisionOption = (value: unknown): DecisionOption | null => {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  if (typeof item.id !== "string" || typeof item.label !== "string") return null;
  return { id: item.id, label: item.label, position: Number(item.position) || 0 };
};

/** Runtime allowlist for untrusted/cached decision JSON.
 * Open decisions deliberately discard any accidental vote or resolution keys.
 */
export function sanitizeDecision(value: unknown): Decision | null {
  if (!value || typeof value !== "object") return null;
  const input = value as Record<string, unknown>;
  const status = String(input.status || "");
  if (!decisionStatuses.has(status) || typeof input.id !== "string" || typeof input.title !== "string") return null;
  const mode = input.selection_mode === "multiple" ? "multiple" : input.selection_mode === "single" ? "single" : null;
  if (!mode || !Array.isArray(input.options)) return null;
  const options = input.options.map(decisionOption).filter((item): item is DecisionOption => Boolean(item));
  if (options.length < 2) return null;
  const common: DecisionCommon = {
    id: input.id, title: input.title, selection_mode: mode,
    voting_round: Number(input.voting_round) || 1, closes_at: String(input.closes_at || ""),
    options_locked: Boolean(input.options_locked), options,
    submitted_by_me: Boolean(input.submitted_by_me), partner_submitted: Boolean(input.partner_submitted),
    creator_id: String(input.creator_id || ""), version: Number(input.version) || 1,
    created_at: String(input.created_at || ""), updated_at: String(input.updated_at || ""),
  };
  if (!revealedDecisionStatuses.has(status)) {
    return { ...common, status: status as "open" | "awaiting_extension" | "cancelled" };
  }
  const votes = Array.isArray(input.votes) ? input.votes.flatMap(raw => {
    if (!raw || typeof raw !== "object") return [];
    const vote = raw as Record<string, unknown>;
    const voteOptions = Array.isArray(vote.options) ? vote.options.map(decisionOption).filter((item): item is DecisionOption => Boolean(item)) : [];
    const optionIds = Array.isArray(vote.option_ids) ? vote.option_ids.filter((item): item is string => typeof item === "string") : [];
    return [{ user_id: String(vote.user_id || ""), option_ids: optionIds, options: voteOptions }];
  }) : [];
  const rawResolution = input.resolution && typeof input.resolution === "object" ? input.resolution as Record<string, unknown> : {};
  const resolution = {
    kind: String(rawResolution.kind || ""),
    ...(Array.isArray(rawResolution.option_ids) ? { option_ids: rawResolution.option_ids.filter((item): item is string => typeof item === "string") } : {}),
  };
  return { ...common, status: status as "revealed" | "pending_confirmation" | "confirmed" | "converted", votes, resolution };
}

export function sanitizeDecisions(value: unknown): Decision[] {
  return Array.isArray(value) ? value.map(sanitizeDecision).filter((item): item is Decision => Boolean(item)) : [];
}

function requireSanitizedDecision(value: unknown): Decision {
  const decision = sanitizeDecision(value);
  if (!decision) throw new Error("双人决策响应格式无效");
  return decision;
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

export interface PlaceCandidate { candidate_id: string; provider: "amap" | "geoapify" | "google_maps" | "manual"; provider_place_id: string; name: string; formatted_address: string; country: string; province: string; city: string; district: string; latitude?: number | null; longitude?: number | null; category: string; source_type?: ProvenanceCode; original_source_type?: ProvenanceCode; evidence_at?: string; created_at?: string; cache_hit?: boolean; cache_age_seconds?: number; scope_confirmed?: boolean; scope_status?: "confirmed" | "unconfirmed"; }
export interface TripSummary { id: string; title: string; destination: string; start_date: string; end_date: string; budget_cents: number; status: string; variants: unknown[]; destination_place?: Omit<PlaceCandidate, "candidate_id"> | null; needs_destination_detail?: boolean; }
export interface TripPackingItem { id: string; label: string; assigned_to: string; assigned_name: string; checked: boolean; position: number; source_type: ProvenanceCode; version: number; created_at: string; updated_at: string; }
export interface TripEmergencyItem { id: string; entry_type: "hotel" | "booking" | "contact" | "insurance" | "hospital" | "return" | "other"; title: string; details: Record<string, string | number>; visibility: "private" | "shared"; visibility_label: string; source_type: ProvenanceCode; version: number; created_at: string; updated_at: string; editable_by_me: boolean; }
export type TripExpenseCategory = "transport" | "lodging" | "food" | "tickets" | "shopping" | "other";
export interface TripExpense {
  id: string; trip_id: string; amount_cents: number; currency: string;
  category: TripExpenseCategory; split_mode: "equal" | "personal";
  payer_id: string; payer_name: string; beneficiary_id: string | null; beneficiary_name: string | null;
  note: string; occurred_on: string; settled_at: string | null; settled_by_name: string | null;
  source_type: "human"; editable: boolean; deleted: boolean; deleted_at: string | null;
  version: number; created_at: string; updated_at: string;
}
export interface TripExpenseBalanceMember { user_id: string; nickname: string; paid_cents: number; owed_cents: number; net_cents: number; }
export interface TripExpenseBalance {
  current_user_id: string; net_cents: number; direction: "partner_owes_me" | "i_owe_partner" | "balanced";
  partner_name: string; unsettled_count: number; total_active_cents: number; total_unsettled_cents: number;
  by_member: TripExpenseBalanceMember[];
}
export interface TripExpenseList {
  trip: { id: string; title: string; currency: string };
  members: Array<{ id: string; nickname: string; avatar_label: string }>;
  items: TripExpense[]; deleted_items: TripExpense[]; balance: TripExpenseBalance;
}
export interface TripOfflineIdentity { user_id: string; couple_id: string; permission_version: number; }
export interface TripOfflineSnapshot {
  schema_version: 1;
  identity: TripOfflineIdentity;
  trip: { id: string; title: string; destination: string; start_date: string; end_date: string; currency: string; confirmed_revision: number; version: number };
  members: Array<{ id: string; nickname: string; avatar_label: string }>;
  itinerary: Array<{ day_number: number; date: string; title: string; items: Array<{ id: string; type: string; title: string; start_time: string; duration_minutes: number; note: string; place: { name: string; address: string; latitude: number; longitude: number; provider: string } | null }> }>;
  packing: { items: TripPackingItem[] };
  emergency: { items: TripEmergencyItem[] };
  generated_at: string;
  expires_at: string;
}
export function validateTripOfflineSnapshot(value: unknown, identity: TripOfflineIdentity, tripId: string, nowMs = Date.now()): TripOfflineSnapshot | null {
  if (!value || typeof value !== "object") return null;
  const snapshot = value as Partial<TripOfflineSnapshot>;
  if (snapshot.schema_version !== 1 || !snapshot.identity || !snapshot.trip) return null;
  if (snapshot.identity.user_id !== identity.user_id || snapshot.identity.couple_id !== identity.couple_id || Number(snapshot.identity.permission_version) !== Number(identity.permission_version)) return null;
  if (snapshot.trip.id !== tripId || !Array.isArray(snapshot.members) || !Array.isArray(snapshot.itinerary) || !Array.isArray(snapshot.packing?.items) || !Array.isArray(snapshot.emergency?.items)) return null;
  const expiresAt = Date.parse(String(snapshot.expires_at || ""));
  const generatedAt = Date.parse(String(snapshot.generated_at || ""));
  if (!Number.isFinite(generatedAt) || !Number.isFinite(expiresAt) || generatedAt > nowMs + 5 * 60 * 1000 || expiresAt <= nowMs || expiresAt <= generatedAt || expiresAt - generatedAt > 24 * 60 * 60 * 1000 + 1000) return null;
  return snapshot as TripOfflineSnapshot;
}
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
export interface MemoryProvenance { source_type: ProvenanceCode; source_label: string; date_source: "legacy_unknown" | "user_confirmed" | "media_capture" | "trip_date" | "system_provisional"; date_source_label: string; }
export interface MemoryDescription { id: string; author_id: string; nickname: string; body: string; visibility: "private" | "shared"; source_type: ProvenanceCode; version: number; created_at: string; updated_at: string; merge_pending?: boolean; }
export interface MemoryMergeProposal { proposal_id: string; version: number; body: string; proposer_id: string; can_confirm: boolean; }
export interface MemorySearchFilters { q?: string; start_date?: string; end_date?: string; type?: "all" | "moment" | "trip" | "text" | "photo" | "video" | "voice" | "chat" | "call" | "anniversary" | "wishlist" | "location" | "weekly_review" | "date"; person_id?: string; cursor?: string; limit?: number; }
export interface MemorySearchItem { resource_type: "moment" | "trip"; resource_id: string; event_at: string; event_type: string; title: string; summary: string; location: string; primary_person_id?: string | null; primary_person_name?: string | null; version: number; visibility: string; descriptions: MemoryDescription[]; provenance: MemoryProvenance; }
export interface MemorySearchResult { items: MemorySearchItem[]; next_cursor: string | null; total: number; parsed_filters: { keyword: string; start_date: string | null; end_date: string | null; content_type: string; person_id: string | null; location: string | null }; empty_reason: string | null; }
export interface MemoryCompletionSuggestion { id: string; resource_type: "moment" | "trip"; resource_id: string; title: string; suggestion_kind: "missing_text" | "missing_location" | "missing_partner_description" | "provisional_date"; status: "pending" | "snoozed" | "dismissed" | "completed"; remind_at?: string | null; notified_at?: string | null; version: number; created_at: string; updated_at: string; provenance: MemoryProvenance; }
export interface MomentItem { id: string; creator_id?: string; type: string; title: string; body: string; occurred_at: string; visibility: string; version: number; deleted_at?: string | null; provenance?: MemoryProvenance; }
export interface MediaItem { id: string; owner_id: string; kind: "image" | "audio" | "video"; original_name: string; mime_type: string; size_bytes: number; visibility: "shared" | "private" | "delayed"; status: "ready" | "trashed" | "purged"; url: string; thumbnail_url: string; width?: number | null; height?: number | null; metadata_stripped: boolean; captured_at?: string | null; date_source?: MemoryProvenance["date_source"]; source_type?: ProvenanceCode; deleted_at?: string | null; version: number; }
export interface VoiceTranscript { id: string; body: string; language: string; method: "browser" | "manual"; source_type: "real_api" | "human"; status: "draft" | "ready" | "failed"; visibility: "shared" | "private" | "delayed"; reveal_at?: string | null; version: number; created_at: string; updated_at: string; }
export interface VoiceMemory { moment: MomentItem; media: MediaItem & { duration_ms?: number | null }; transcript: VoiceTranscript | null; }
export interface FutureLetter { id: string; title: string; body: string | null; status: "draft" | "sealed" | "delivered"; deliver_at?: string | null; sealed_at?: string | null; delivered_at?: string | null; converted: boolean; version: number; created_at: string; updated_at: string; is_author: boolean; }
export interface AlbumExportSelection { resource_type: "moment" | "media"; resource_id: string; }
export interface AlbumExportPreviewItem extends AlbumExportSelection { title: string; summary: string; event_at: string; location?: string | null; visibility: string; source_type: ProvenanceCode; date_source: string; version: number; default_selected: boolean; has_image: boolean; }
export interface AlbumExportPreview { template: "year" | "trip"; audience: "personal" | "shared"; year?: number | null; trip_id?: string | null; title: string; date_range: string; date_filter_notice?: string | null; total: number; truncated: boolean; items: AlbumExportPreviewItem[]; }
export interface AlbumExportJob { id: string; template: "year" | "trip"; year?: number | null; trip_id?: string | null; audience: "personal" | "shared"; status: "queued" | "running" | "completed" | "failed" | "cancelled" | "expired"; failure_code?: string | null; size_bytes?: number | null; version: number; expires_at: string; completed_at?: string | null; download_ready: boolean; warnings: string[]; }
export interface AiContextGrant {
  id: string;
  surface: string;
  resource_type: string;
  resource_id: string;
  purpose: string;
  usage_scope: "legacy_unknown" | "selected_content";
  expires_at: string;
  created_at: string;
  last_used_at?: string | null;
  revoked_at?: string | null;
  version: number;
  status: "active" | "expired" | "revoked";
  idempotent?: boolean;
  notice?: string;
}
export interface AccountPrivacyCenter {
  generated_at: string;
  month: string;
  timezone: string;
  usage: {
    attempts: number;
    sent_attempts: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    unknown_cost_attempts: number;
    status_counts: Record<string, number>;
    feature_counts: Record<string, number>;
    costs: Array<{ currency: string; amount: number }>;
  };
  grants: AiContextGrant[];
  notice: string;
}
export type CalendarRecurrence = "none" | "daily" | "weekly" | "monthly" | "yearly";
export interface CalendarEntry { id: string; entry_type: "event" | "anniversary" | "todo"; title: string; starts_at: string; ends_at?: string | null; all_day: boolean; location?: string; note?: string; participant_ids: string[]; blocks_time: boolean; source_type: ProvenanceCode; completed_at?: string | null; recurrence: CalendarRecurrence; version: number; }
export interface CalendarMutation {
  entry_type: CalendarEntry["entry_type"];
  title: string;
  starts_at: string;
  ends_at?: string | null;
  all_day: boolean;
  location?: string;
  note?: string;
  participant_ids: string[];
  blocks_time: boolean;
  recurrence: CalendarRecurrence;
  expected_version?: number;
}
export interface PartnerPreference { id: string; subject_user_id: string; subject_name?: string; category: string; label: string; value: string; note: string; version: number; }
export type StatusCode = "busy" | "want_chat" | "need_company" | "feeling_low" | "moving" | "home" | "custom";
export interface WishlistClaim { id: string; item_id: string; status: "active" | "released" | "cancelled" | "completed"; version: number; reveal_claimant: boolean; private_note?: string; created_at: string; updated_at: string; ended_at?: string | null; }
export interface WishlistItem { id: string; creator_id: string; text: string; note: string; item_kind: "general" | "gift" | "meal" | "travel"; is_creator: boolean; can_claim?: boolean; my_claim?: WishlistClaim | null; completed_at?: string | null; version: number; }
export interface ExpressionSuggestion { text: string; mode: "gentle" | "needs" | "summary"; provider: string; model: string; source: "message" | "draft"; }
export interface RecipeIngredient { id?: string; name: string; display_quantity?: string; quantity?: number; unit?: string; specification?: string; preparation?: string; ingredient_group?: "main" | "seasoning"; optional?: number | boolean; }
export interface RecipeStep { id?: string; title?: string; instruction: string; timer_seconds?: number | null; heat_level?: string; key_point?: string; position?: number; }
export interface RecipeSummary { id: string; name: string; cuisine: "chinese" | "western"; category: string; meal_slots: Array<"breakfast" | "lunch" | "dinner" | "snack">; base_servings: number; prep_minutes: number; cook_minutes: number; passive_minutes: number; active_minutes: number; total_minutes: number; difficulty: "easy" | "medium" | "hard"; source_type: string; visibility: string; version: number; }
export interface RecipeAllergenDetail { name: string; sources: string[]; basis: string; }
export interface RecipeFoodSafety { status: "safe" | "needs_review" | "blocked"; severe_conflicts: Array<{ user_id: string; allergen_key: string; sources: string[] }>; avoid_conflicts: Array<{ user_id: string; allergen_key: string; sources: string[] }>; ingredient_avoidances: Array<{ ingredient_id?: string | null; canonical_name: string; user_ids: string[] }>; review_ingredients: string[]; note: string; }
export interface RecipeDetail extends RecipeSummary { ingredients: RecipeIngredient[]; steps: RecipeStep[]; tips: string[]; allergen_tags: string[]; allergen_details?: RecipeAllergenDetail[]; allergen_review_ingredients?: string[]; allergen_note?: string; food_safety?: RecipeFoodSafety; }
export interface MealWish { id: string; recipe_id?: string | null; title: string; note: string; creator_id: string; creator_name?: string; version: number; created_at: string; }
export interface MealPlanItem { id: string; recipe_id?: string | null; name: string; planned_date: string; meal_slot: "breakfast" | "lunch" | "dinner" | "snack"; planned_time?: string | null; servings: number; status: "selected" | "accepted" | "shopping" | "cooking" | "completed" | "cancelled"; course_type: string; note: string; version: number; recipe_snapshot: Record<string, unknown>; }
export interface MealCart { id?: string | null; title: string; status: string; version: number; items: MealPlanItem[]; }
export interface MealCalendarDay { date: string; meals: Record<MealPlanItem["meal_slot"], MealPlanItem[]>; }
export interface MealCalendar { timezone: string; from: string; to: string; days: MealCalendarDay[]; }
export interface ShoppingItem { id: string; display_name: string; display_quantity: string; category: string; category_key: ShoppingCategoryKey; category_label: string; checked: boolean; manual_locked: boolean; version: number; sources?: Array<Record<string, unknown>>; }
export interface ShoppingList { id?: string | null; title: string; status: string; range_start?: string; range_end?: string; version: number; items: ShoppingItem[]; }
export interface ShoppingPreview { preview_id: string; preview: { id: string; range_start: string; range_end: string; meal_count: number; items: Array<Record<string, unknown>>; diff: { added: Array<Record<string, unknown>>; updated: Array<Record<string, unknown>>; removed: Array<Record<string, unknown>>; protected: Array<Record<string, unknown>>; warnings: string[] }; expires_at: string } }
export interface FoodAllergy { allergen_key: "蛋类" | "乳制品" | "含麸质谷物" | "可能含麸质谷物" | "甲壳类" | "鱼类" | "软体动物" | "花生" | "坚果" | "芝麻" | "大豆"; severity: "avoid" | "severe"; }
export interface FoodAvoidance { ingredient_id: string; canonical_name: string; category_key: ShoppingCategoryKey; category_label: string; }
export interface FoodProfile { user_id: string; nickname: string; allergies: FoodAllergy[]; avoidances: FoodAvoidance[]; preferences: { liked_tags: string[]; disliked_tags: string[] }; version: number; updated_at?: string | null; }
export type PantryState = "have" | "low" | "out";
export type StorageLocation = "ambient" | "chilled" | "frozen";
export type ShoppingCategoryKey = "produce" | "meat_eggs" | "seafood" | "dairy" | "staples_oils" | "seasonings" | "frozen" | "household" | "other";
export interface PantryItem { id: string; ingredient_id: string; canonical_name: string; aliases: string[]; category_key: ShoppingCategoryKey; category_label: string; state: PantryState; storage_location?: StorageLocation | null; expires_on?: string | null; quantity_note: string; is_expired: boolean; expires_soon: boolean; changed?: boolean; version: number; }
export interface ShoppingCategoryPreference { key: ShoppingCategoryKey; label: string; position: number; version: number; }
export type WeeklyMealPlanStatus = "draft" | "pending_confirmation" | "applied" | "cancelled" | "expired" | "failed";
export interface WeeklyMealPlanItem { id: string; date: string; meal_slot: "breakfast" | "lunch" | "dinner" | "snack"; planned_time: string; recipe_id: string | null; recipe_version: number | null; recipe: { id: string; name: string; active_minutes: number; passive_minutes: number } | null; servings: number; course_type: string; status: "planned" | "needs_selection"; reason_codes: string[]; reason_labels: string[]; availability_label: string; likely_on_hand: string[]; }
export interface WeeklyMealPlanDraft { id: string | null; starts_on?: string; ends_on?: string; status: WeeklyMealPlanStatus | "empty"; source_type?: "rules"; version?: number; expires_at?: string; items: WeeklyMealPlanItem[]; warnings: string[]; confirmations?: { member_a_confirmed: boolean; member_b_confirmed: boolean; both_confirmed: boolean }; shopping_preview?: { required_items: Array<Record<string, unknown>>; likely_on_hand: string[]; diff: Record<string, number> }; unresolved_wish_ids?: string[]; result?: { meal_item_ids: string[]; shopping_list_id: string | null; created_count: number; shopping_diff: Record<string, number> } | null; }
export type SubstitutionSafetyStatus = "allowed" | "needs_review" | "blocked";
export interface IngredientSubstitutionShoppingPreview {
  expected_list_version: number;
  range_start: string;
  range_end: string;
  diff: Record<string, number>;
  changes: { add: string[]; update: string[]; remove: string[]; protected: string[] };
}
export interface IngredientSubstitutionSuggestion {
  id: string;
  from_ingredient: { id: string; name: string };
  to_ingredient: { id: string; name: string };
  ratio_note: string;
  suitable_methods: string[];
  flavor_change: string;
  texture_change: string;
  effect_note: string;
  added_allergens: Array<{ name: string; basis: string }>;
  safety_status: SubstitutionSafetyStatus;
  safety_reason_codes: string[];
  safety_message: string;
  can_apply: boolean;
  source_type: "rules";
  source_label: string;
  expires_at?: string | null;
  status: "suggested" | "applied" | "dismissed" | "expired";
  version: number;
  shopping_preview?: IngredientSubstitutionShoppingPreview;
}
export interface IngredientSubstitutionPreview {
  meal_item_id: string;
  meal_version: number;
  source_ingredient: { snapshot_ref: string; ingredient_id: string; name: string };
  items: IngredientSubstitutionSuggestion[];
  shopping_preview: IngredientSubstitutionShoppingPreview;
  message?: string;
}
export interface IngredientSubstitutionApplyResult {
  substitution_id: string;
  status: "applied";
  meal_item: { id: string; version: number };
  shopping_list: { id: string; version: number };
  shopping_diff: Record<string, number>;
  shopping_preview: IngredientSubstitutionShoppingPreview;
  source_type: "rules";
  idempotent: boolean;
}
export interface CookTimer { id: string; session_id: string; step_index: number; label: string; duration_seconds: number; started_at: string; ends_at: string; status: "running" | "completed" | "cancelled"; version: number; }
export interface CookPantrySuggestion { ingredient_ref: string; name: string; ingredient_group: string; optional: boolean; pantry_state: PantryState | null; pantry_version: number; allowed_states: Array<"low" | "out">; selected_state: null; }
export interface CookPantryApplyItem { id: string; ingredient_ref: string; name: string; state: PantryState; version: number; changed: boolean; undo?: Record<string, unknown>; }
export interface CookSession { id: string; recipe_id?: string | null; meal_item_id?: string | null; name: string; visibility: "private" | "shared"; status: "active" | "completed" | "cancelled"; current_step: number; version: number; server_now: string; resumed: boolean; recipe_snapshot?: RecipeDetail & Record<string, unknown>; timers?: CookTimer[]; pantry_suggestions?: CookPantrySuggestion[]; source_meal_version?: number | null; }
export type CookStartInput =
  | { recipe_id: string; meal_item_id?: never; visibility: "private" | "shared"; expected_meal_version?: never }
  | { recipe_id?: never; meal_item_id: string; visibility: "shared"; expected_meal_version: number };

export class LoveMomentsClient {
  private accessToken?: string;
  private pendingUploads = new Map<string, string>();
  constructor(private baseUrl = "", private cache?: ManifestCache) {}
  setAccessToken(token?: string) { if (this.accessToken !== token) this.pendingUploads.clear(); this.accessToken = token || undefined; }
  private idempotency(prefix: string) { const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`; return `client:${prefix}:${random}`; }
  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const requestToken = this.accessToken;
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
    if (requestToken !== this.accessToken) throw Object.assign(new Error("账号已变化，请重新加载"), { status: 409 });
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
    const scope = this.cache?.scope?.();
    try {
      const manifest = uiManifestSchema.parse(await this.request(`/api/v1/ui/${surface}`));
      if (scope !== this.cache?.scope?.()) throw Object.assign(new Error("账号或权限已变化"), { status: 409 });
      await this.cache?.set(surface, manifest); return manifest;
    } catch (error) {
      if ((error as { status?: number }).status) throw error;
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
  tripPacking(id: string) { return this.request<{ trip_id: string; trip_version: number; items: TripPackingItem[] }>(`/api/v1/trips/${id}/packing`); }
  generateTripPacking(id: string, tripVersion: number, idempotencyKey = this.idempotency("packing-generate")) { return this.request<{ trip_id: string; trip_version: number; items: TripPackingItem[]; evidence: Record<string, unknown> }>(`/api/v1/trips/${id}/packing/generate`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ expected_trip_version: tripVersion }) }); }
  createTripPackingItem(id: string, value: { label: string; assigned_to: string; expected_trip_version: number }, idempotencyKey = this.idempotency("packing-create")) { return this.request<TripPackingItem>(`/api/v1/trips/${id}/packing`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  updateTripPackingItem(tripId: string, itemId: string, value: { checked?: boolean; assigned_to?: string; label?: string; expected_version: number }) { return this.request<TripPackingItem>(`/api/v1/trips/${tripId}/packing/${itemId}`, { method: "PATCH", body: JSON.stringify(value) }); }
  deleteTripPackingItem(tripId: string, itemId: string, version: number) { return this.request(`/api/v1/trips/${tripId}/packing/${itemId}`, { method: "DELETE", body: JSON.stringify({ expected_version: version }) }); }
  tripEmergency(id: string) { return this.request<{ trip_id: string; trip_version: number; permission_version: number; items: TripEmergencyItem[] }>(`/api/v1/trips/${id}/emergency`); }
  searchTripHospitals(id: string, query = "医院 急救") { return this.request<{ items: PlaceCandidate[]; configured: boolean; notice: string; expires_at?: string }>(`/api/v1/trips/${id}/hospitals/search`, { method: "POST", body: JSON.stringify({ query }) }); }
  createTripEmergency(id: string, value: Record<string, unknown>, idempotencyKey = this.idempotency("emergency-create")) { return this.request<TripEmergencyItem & { permission_version: number }>(`/api/v1/trips/${id}/emergency`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  updateTripEmergency(tripId: string, entryId: string, value: Record<string, unknown>) { return this.request<TripEmergencyItem & { permission_version: number }>(`/api/v1/trips/${tripId}/emergency/${entryId}`, { method: "PATCH", body: JSON.stringify(value) }); }
  deleteTripEmergency(tripId: string, entryId: string, version: number) { return this.request<{ id: string; deleted: true; version: number; permission_version: number }>(`/api/v1/trips/${tripId}/emergency/${entryId}`, { method: "DELETE", body: JSON.stringify({ expected_version: version }) }); }
  tripExpenses(id: string) { return this.request<TripExpenseList>(`/api/v1/trips/${id}/expenses`); }
  createTripExpense(id: string, value: Record<string, unknown>, idempotencyKey = this.idempotency("expense-create")) { return this.request<TripExpense>(`/api/v1/trips/${id}/expenses`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  updateTripExpense(tripId: string, expenseId: string, value: Record<string, unknown>, idempotencyKey = this.idempotency("expense-update")) { return this.request<TripExpense>(`/api/v1/trips/${tripId}/expenses/${expenseId}`, { method: "PATCH", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  deleteTripExpense(tripId: string, expenseId: string, version: number, idempotencyKey = this.idempotency("expense-delete")) { return this.request<TripExpense>(`/api/v1/trips/${tripId}/expenses/${expenseId}`, { method: "DELETE", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ expected_version: version }) }); }
  restoreTripExpense(tripId: string, expenseId: string, version: number, idempotencyKey = this.idempotency("expense-restore")) { return this.request<TripExpense>(`/api/v1/trips/${tripId}/expenses/${expenseId}/restore`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ expected_version: version }) }); }
  settleTripExpenses(tripId: string, expenses: Array<{ id: string; expected_version: number }>, idempotencyKey = this.idempotency("expense-settle")) { return this.request<{ trip_id: string; settled_count: number; settled_at: string; settled_by_name: string; items: TripExpense[]; notice: string }>(`/api/v1/trips/${tripId}/expenses/settle`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ expenses }) }); }
  tripOfflineSnapshot(id: string) { return this.request<TripOfflineSnapshot>(`/api/v1/trips/${id}/offline-snapshot`); }
  quotes(value: Record<string, unknown>) { return this.request<{ items: QuoteItem[]; mock: boolean; notice: string }>("/api/v1/quotes/search", { method: "POST", body: JSON.stringify(value) }); }
  selectQuote(id: string, expectedVersion?: number) { return this.request<QuoteItem>(`/api/v1/quotes/${id}/select`, { method: "POST", body: JSON.stringify({ expected_version: expectedVersion }) }); }
  createChatImport(value: Record<string, unknown>) { return this.request<Record<string, any>>("/api/v1/chat-imports", { method: "POST", body: JSON.stringify(value) }); }
  updateChatImportItem(importId: string, itemId: string, value: Record<string, unknown>) { return this.request(`/api/v1/chat-imports/${importId}/items/${itemId}`, { method: "PATCH", body: JSON.stringify(value) }); }
  confirmChatImport(importId: string, asMoments = false) { return this.request(`/api/v1/chat-imports/${importId}/confirm`, { method: "POST", body: JSON.stringify({ as_moments: asMoments }) }); }
  calendar() { return this.request<{ items: CalendarEntry[] }>("/api/v1/calendar"); }
  createCalendar(value: CalendarMutation) { return this.request<CalendarEntry>("/api/v1/calendar", { method: "POST", body: JSON.stringify(value) }); }
  updateCalendar(id: string, value: CalendarMutation) { return this.request<CalendarEntry>(`/api/v1/calendar/${id}`, { method: "PATCH", body: JSON.stringify(value) }); }
  completeCalendar(id: string, completed: boolean, version: number) { return this.request<CalendarEntry>(`/api/v1/calendar/${id}/complete`, { method: "POST", body: JSON.stringify({ completed, expected_version: version }) }); }
  deleteCalendar(id: string, version: number) { return this.request(`/api/v1/calendar/${id}`, { method: "DELETE", body: JSON.stringify({ expected_version: version }) }); }
  preferences() { return this.request<{ items: PartnerPreference[] }>("/api/v1/preferences"); }
  createPreference(value: Record<string, unknown>) { return this.request<PartnerPreference>("/api/v1/preferences", { method: "POST", body: JSON.stringify(value) }); }
  updatePreference(id: string, value: Record<string, unknown>) { return this.request<PartnerPreference>(`/api/v1/preferences/${id}`, { method: "PATCH", body: JSON.stringify(value) }); }
  deletePreference(id: string, version: number) { return this.request(`/api/v1/preferences/${id}`, { method: "DELETE", body: JSON.stringify({ expected_version: version }) }); }
  wishlist() { return this.request<{ id: string | null; title: string; items: WishlistItem[] }>("/api/v1/lists/wishlist"); }
  createWishlistItem(value: { text: string; note?: string; item_kind?: WishlistItem["item_kind"] }) { return this.request<WishlistItem>("/api/v1/lists/wishlist/items", { method: "POST", body: JSON.stringify(value) }); }
  updateWishlistItem(id: string, value: { text: string; note?: string; expected_version: number }) { return this.request<WishlistItem>(`/api/v1/lists/items/${id}`, { method: "PATCH", body: JSON.stringify(value) }); }
  toggleWishlistItem(id: string, completed: boolean) { return this.request<WishlistItem>(`/api/v1/lists/items/${id}`, { method: "PATCH", body: JSON.stringify({ completed }) }); }
  deleteWishlistItem(id: string, version: number) { return this.request(`/api/v1/lists/items/${id}`, { method: "DELETE", body: JSON.stringify({ expected_version: version }) }); }
  claimWishlistItem(id: string, value: { expected_version: number; private_note?: string; reveal_claimant?: boolean }, idempotencyKey = this.idempotency("wishlist-claim")) { return this.request<WishlistClaim>(`/api/v1/lists/items/${id}/claim`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  releaseWishlistClaim(id: string, version: number, idempotencyKey = this.idempotency("wishlist-release")) { return this.request<WishlistClaim>(`/api/v1/lists/items/${id}/claim/release`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ expected_version: version }) }); }
  completeWishlistClaim(id: string, value: { expected_version: number; reveal_claimant: boolean; create_moment: boolean }, idempotencyKey = this.idempotency("wishlist-complete")) { return this.request<{ id: string; item_id: string; completed: true; revealed: boolean; moment_id?: string | null }>(`/api/v1/lists/items/${id}/claim/complete`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
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
  foodProfiles() { return this.request<{ profiles: FoodProfile[] }>("/api/v1/food-profiles"); }
  updateMyFoodProfile(value: { allergies: FoodAllergy[]; avoidances: Array<{ ingredient_id: string }>; preferences: { liked_tags: string[]; disliked_tags: string[] }; expected_version: number }, idempotencyKey = this.idempotency("food-profile")) { return this.request<FoodProfile>("/api/v1/food-profiles/me", { method: "PUT", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  pantryItems() { return this.request<{ items: PantryItem[] }>("/api/v1/pantry/items"); }
  upsertPantryItem(value: { ingredient_id?: string; name?: string; state: PantryState; storage_location?: StorageLocation | null; expires_on?: string | null; quantity_note?: string; expected_version: number }, idempotencyKey = this.idempotency("pantry")) { return this.request<PantryItem>("/api/v1/pantry/items", { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  updatePantryItem(id: string, value: { state?: PantryState; storage_location?: StorageLocation | null; expires_on?: string | null; quantity_note?: string; expected_version: number }, idempotencyKey = this.idempotency("pantry-update")) { return this.request<PantryItem>(`/api/v1/pantry/items/${id}`, { method: "PATCH", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  stockShoppingItem(id: string, value: { expected_version: number; confirm_food?: boolean; storage_location?: StorageLocation | null; expires_on?: string | null; quantity_note?: string }, idempotencyKey = this.idempotency("shopping-stock")) { return this.request<{ shopping_item_id: string; changed: boolean; pantry_item: PantryItem }>(`/api/v1/shopping-lists/items/${id}/stock`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  shoppingCategoryOrder() { return this.request<{ categories: ShoppingCategoryPreference[] }>("/api/v1/shopping-category-order"); }
  updateShoppingCategoryOrder(categories: ShoppingCategoryKey[], expectedVersions: Record<ShoppingCategoryKey, number>, idempotencyKey = this.idempotency("shopping-categories")) { return this.request<{ categories: ShoppingCategoryPreference[] }>("/api/v1/shopping-category-order", { method: "PUT", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ categories, expected_versions: expectedVersions }) }); }
  activeWeeklyMealPlan() { return this.request<WeeklyMealPlanDraft>("/api/v1/weekly-meal-plans/active"); }
  createWeeklyMealPlan(value: { schema_version: 1; starts_on: string; ends_on: string; meal_slots: Array<{ meal_slot: WeeklyMealPlanItem["meal_slot"]; planned_time: string; max_active_minutes: number }>; servings: number; availability_mode: "either_partner" | "both_partners" }, idempotencyKey = this.idempotency("weekly-plan")) { return this.request<WeeklyMealPlanDraft>("/api/v1/weekly-meal-plans/drafts", { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  updateWeeklyMealPlanItem(draftId: string, itemId: string, value: { expected_version: number; recipe_id?: string; date?: string; meal_slot?: WeeklyMealPlanItem["meal_slot"]; planned_time?: string; swap_with_item_id?: string | null }, idempotencyKey = this.idempotency("weekly-plan-item")) { return this.request<WeeklyMealPlanDraft>(`/api/v1/weekly-meal-plans/drafts/${draftId}/items/${itemId}`, { method: "PATCH", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  confirmWeeklyMealPlan(draftId: string, expectedVersion: number, idempotencyKey = this.idempotency("weekly-plan-confirm")) { return this.request<WeeklyMealPlanDraft>(`/api/v1/weekly-meal-plans/drafts/${draftId}/confirm`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ expected_version: expectedVersion }) }); }
  cancelWeeklyMealPlan(draftId: string, expectedVersion: number, idempotencyKey = this.idempotency("weekly-plan-cancel")) { return this.request<WeeklyMealPlanDraft>(`/api/v1/weekly-meal-plans/drafts/${draftId}/cancel`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ expected_version: expectedVersion }) }); }
  previewIngredientSubstitutions(mealId: string, value: { snapshot_ingredient_ref: string; expected_meal_version: number; expected_shopping_list_version: number }, idempotencyKey = this.idempotency("ingredient-substitution-preview")) { return this.request<IngredientSubstitutionPreview>(`/api/v1/meal-cart/items/${mealId}/ingredient-substitutions/preview`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  ingredientSubstitutions(mealId: string) { return this.request<{ meal_item_id: string; meal_version: number; items: IngredientSubstitutionSuggestion[] }>(`/api/v1/meal-cart/items/${mealId}/ingredient-substitutions`); }
  applyIngredientSubstitution(mealId: string, substitutionId: string, value: { expected_meal_version: number; expected_shopping_list_version: number; expected_version: number }, idempotencyKey = this.idempotency("ingredient-substitution-apply")) { return this.request<IngredientSubstitutionApplyResult>(`/api/v1/meal-cart/items/${mealId}/ingredient-substitutions/${substitutionId}/apply`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  dismissIngredientSubstitution(mealId: string, substitutionId: string, expectedVersion: number, idempotencyKey = this.idempotency("ingredient-substitution-dismiss")) { return this.request<{ substitution_id: string; status: "dismissed"; version: number }>(`/api/v1/meal-cart/items/${mealId}/ingredient-substitutions/${substitutionId}/dismiss`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ expected_version: expectedVersion }) }); }
  cookSessions(status: "active" | "completed" | "cancelled" | "all" = "active") { return this.request<{ items: CookSession[] }>(`/api/v1/cook-sessions?status=${encodeURIComponent(status)}`); }
  startCookSession(value: CookStartInput, idempotencyKey = this.idempotency("cook-start")) { return this.request<CookSession>("/api/v1/cook-sessions", { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  cookSession(id: string) { return this.request<CookSession>(`/api/v1/cook-sessions/${id}`); }
  setCookStep(id: string, stepIndex: number, expectedVersion: number, idempotencyKey = this.idempotency("cook-step")) { return this.request<CookSession>(`/api/v1/cook-sessions/${id}/step`, { method: "PATCH", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ step_index: stepIndex, expected_version: expectedVersion }) }); }
  startCookTimer(id: string, value: { step_index: number; duration_seconds: number; label: string }, idempotencyKey = this.idempotency("cook-timer")) { return this.request<CookTimer>(`/api/v1/cook-sessions/${id}/timers`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  cancelCookTimer(id: string, timerId: string, expectedVersion: number, idempotencyKey = this.idempotency("cook-timer-cancel")) { return this.request<CookTimer>(`/api/v1/cook-sessions/${id}/timers/${timerId}/cancel`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ expected_version: expectedVersion }) }); }
  completeCookSession(id: string, expectedVersion: number, idempotencyKey = this.idempotency("cook-complete")) { return this.request<CookSession>(`/api/v1/cook-sessions/${id}/complete`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ expected_version: expectedVersion }) }); }
  cancelCookSession(id: string, expectedVersion: number, idempotencyKey = this.idempotency("cook-cancel")) { return this.request<CookSession>(`/api/v1/cook-sessions/${id}/cancel`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ expected_version: expectedVersion }) }); }
  applyCookPantryUpdates(id: string, expectedVersion: number, updates: Array<{ ingredient_ref: string; state: "low" | "out"; expected_pantry_version: number }>, idempotencyKey = this.idempotency("cook-pantry")) { return this.request<{ session_id: string; version: number; items: CookPantryApplyItem[] }>(`/api/v1/cook-sessions/${id}/pantry`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ expected_version: expectedVersion, updates }) }); }
  previewShopping(value: { range_start: string; range_end: string; expected_version?: number }) { return this.request<ShoppingPreview>("/api/v1/shopping-lists/preview", { method: "POST", body: JSON.stringify(value) }); }
  applyShopping(previewId: string, idempotencyKey?: string) { return this.request<ShoppingList>("/api/v1/shopping-lists/apply", { method: "POST", body: JSON.stringify({ preview_id: previewId, idempotency_key: idempotencyKey || this.idempotency("shopping") }) }); }
  createShoppingItem(value: Record<string, unknown>) { return this.request<ShoppingItem>("/api/v1/shopping-lists/items", { method: "POST", body: JSON.stringify(value) }); }
  updateShoppingItem(id: string, value: Record<string, unknown>) { return this.request<ShoppingItem>(`/api/v1/shopping-lists/items/${id}`, { method: "PATCH", body: JSON.stringify(value) }); }
  deleteShoppingItem(id: string, version: number) { return this.request(`/api/v1/shopping-lists/items/${id}`, { method: "DELETE", body: JSON.stringify({ expected_version: version }) }); }
  completeShopping(value: { expected_version: number; allow_incomplete?: boolean; stock_checked_item_ids?: string[]; stock_item_versions?: Record<string, number>; confirm_food_item_ids?: string[] }, idempotencyKey = this.idempotency("shopping-complete")) { return this.request("/api/v1/shopping-lists/complete", { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  searchMemories(value: MemorySearchFilters = {}) { return this.request<MemorySearchResult>("/api/v1/memories/search", { method: "POST", body: JSON.stringify(value) }); }
  refreshMemoryCompletion() { return this.request<{ created: number }>("/api/v1/memories/completion/refresh", { method: "POST", body: "{}" }); }
  memoryCompletion() { return this.request<{ items: MemoryCompletionSuggestion[]; total: number }>("/api/v1/memories/completion"); }
  respondMemoryCompletion(id: string, value: Record<string, unknown>) { return this.request<MemoryCompletionSuggestion>(`/api/v1/memories/completion/${encodeURIComponent(id)}/respond`, { method: "POST", body: JSON.stringify(value) }); }
  memoryDescriptions(type: "moment" | "trip", id: string) { return this.request<{ resource_type: string; resource_id: string; parent_version: number; parent_visibility: string; items: MemoryDescription[]; merge_proposal: MemoryMergeProposal | null }>(`/api/v1/memories/${type}/${encodeURIComponent(id)}/descriptions`); }
  saveMemoryDescription(type: "moment" | "trip", id: string, value: { body: string; visibility: "private" | "shared"; expected_version: number }) { return this.request<MemoryDescription>(`/api/v1/memories/${type}/${encodeURIComponent(id)}/descriptions/me`, { method: "PUT", body: JSON.stringify(value) }); }
  proposeMemoryMerge(id: string, value: { body: string; expected_parent_version: number; expected_versions: Record<string, number> }) { return this.request<{ moment_id: string; proposal_id: string; version: number; body: string; proposer_id: string }>(`/api/v1/memories/moments/${encodeURIComponent(id)}/merge-proposal`, { method: "POST", body: JSON.stringify(value) }); }
  confirmMemoryMerge(id: string, proposalId: string, expectedVersion: number) { return this.request<MomentItem>(`/api/v1/memories/moments/${encodeURIComponent(id)}/merge-confirm`, { method: "POST", body: JSON.stringify({ proposal_id: proposalId, expected_version: expectedVersion }) }); }
  voiceMemory(momentId: string) { return this.request<VoiceMemory>(`/api/v1/voice-memories/${encodeURIComponent(momentId)}`); }
  createVoiceMemory(value: { media_id: string; title: string; note?: string; occurred_at: string; location?: string; mood?: string; visibility: "shared" | "private" | "delayed"; reveal_at?: string | null; duration_ms: number; transcript?: { body: string; language?: string; method: "browser" | "manual"; status: "draft" | "ready" | "failed" } }, idempotencyKey = this.idempotency("voice-memory")) { return this.request<VoiceMemory>("/api/v1/voice-memories", { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  updateVoiceTranscript(id: string, value: { body: string; language?: string; expected_version: number }) { return this.request<VoiceTranscript>(`/api/v1/voice-transcripts/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(value) }); }
  futureLetters(scope: "mine" | "delivered" = "mine") { return this.request<{ items: FutureLetter[]; total: number }>(`/api/v1/future-letters?scope=${scope}`); }
  futureLetter(id: string) { return this.request<FutureLetter>(`/api/v1/future-letters/${encodeURIComponent(id)}`); }
  createFutureLetter(value: { title: string; body: string }, idempotencyKey = this.idempotency("future-letter")) { return this.request<FutureLetter>("/api/v1/future-letters", { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  updateFutureLetter(id: string, value: { title: string; body: string; expected_version: number }) { return this.request<FutureLetter>(`/api/v1/future-letters/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(value) }); }
  sealFutureLetter(id: string, deliverAt: string, expectedVersion: number) { return this.request<FutureLetter>(`/api/v1/future-letters/${encodeURIComponent(id)}/seal`, { method: "POST", body: JSON.stringify({ deliver_at: deliverAt, expected_version: expectedVersion }) }); }
  cancelFutureLetter(id: string, expectedVersion: number) { return this.request<FutureLetter>(`/api/v1/future-letters/${encodeURIComponent(id)}/cancel`, { method: "POST", body: JSON.stringify({ expected_version: expectedVersion }) }); }
  deleteFutureLetter(id: string, expectedVersion: number) { return this.request<{ id: string; deleted: true }>(`/api/v1/future-letters/${encodeURIComponent(id)}`, { method: "DELETE", body: JSON.stringify({ expected_version: expectedVersion }) }); }
  convertFutureLetter(id: string, expectedVersion: number, idempotencyKey = this.idempotency("future-moment")) { return this.request<{ letter_id: string; moment_id: string; letter_version_before: number; letter_version: number }>(`/api/v1/future-letters/${encodeURIComponent(id)}/convert-to-moment`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ expected_version: expectedVersion }) }); }
  previewAlbumExport(value: { template: "year"; year: number; audience: "personal" | "shared" } | { template: "trip"; trip_id: string; audience: "personal" | "shared" }) { return this.request<AlbumExportPreview>("/api/v1/album-exports/preview", { method: "POST", body: JSON.stringify(value) }); }
  createAlbumExport(value: ({ template: "year"; year: number } | { template: "trip"; trip_id: string }) & { audience: "personal" | "shared"; selection: AlbumExportSelection[]; expected_versions: Record<string, number> }, idempotencyKey = this.idempotency("album")) { return this.request<AlbumExportJob>("/api/v1/album-exports", { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  albumExports() { return this.request<{ items: AlbumExportJob[] }>("/api/v1/album-exports"); }
  albumExport(id: string) { return this.request<AlbumExportJob>(`/api/v1/album-exports/${encodeURIComponent(id)}`); }
  cancelAlbumExport(id: string, expectedVersion: number) { return this.request<AlbumExportJob>(`/api/v1/album-exports/${encodeURIComponent(id)}/cancel`, { method: "POST", body: JSON.stringify({ expected_version: expectedVersion }) }); }
  albumExportDownloadUrl(id: string) { return `${this.baseUrl}/api/v1/album-exports/${encodeURIComponent(id)}/download`; }
  timelineMonths() { return this.request<{ items: Array<Record<string, unknown>> }>("/api/v1/timeline/months"); }
  footprints() { return this.request<{ items: Array<Record<string, unknown>>; mock: boolean; notice: string }>("/api/v1/timeline/footprints"); }
  annualReport(year: number) { return this.request<Record<string, unknown>>(`/api/v1/timeline/reports/${year}`); }
  privacyCenter() { return this.request<AccountPrivacyCenter>("/api/v1/account/privacy"); }
  revokeAiContextGrant(id: string, expectedVersion: number) { return this.request<AiContextGrant>(`/api/v1/account/privacy/grants/${encodeURIComponent(id)}/revoke`, { method: "POST", body: JSON.stringify({ expected_version: expectedVersion }) }); }
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
  setStatus(value: { status_code: StatusCode; text?: string; duration_minutes: number }) { return this.request("/api/v1/me/status", { method: "PUT", body: JSON.stringify(value) }); }
  clearStatus() { return this.request("/api/v1/me/status/clear", { method: "POST", body: "{}" }); }
  suggestExpression(value: { mode: ExpressionSuggestion["mode"]; message_id?: string; draft?: string }, idempotencyKey = this.idempotency("expression")) { return this.request<ExpressionSuggestion>("/api/v1/expression/suggestions", { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(value) }); }
  setLocation(value: { city: string; timezone: string; latitude?: number; longitude?: number }) { return this.request("/api/v1/me/location", { method: "PUT", body: JSON.stringify(value) }); }
  weeklyReview() { return this.request<Record<string, unknown>>("/api/v1/weekly-reviews/current"); }
  answerWeekly(reviewId: string, value: { favorite: string; gratitude: string; next_week: string }) { return this.request(`/api/v1/weekly-reviews/${reviewId}/answer`, { method: "PUT", body: JSON.stringify(value) }); }
  confirmWeekly(reviewId: string) { return this.request(`/api/v1/weekly-reviews/${reviewId}/confirm`, { method: "POST", body: "{}" }); }
  async decisions() { const value = await this.request<{ items?: unknown }>("/api/v1/decisions"); return { items: sanitizeDecisions(value.items) }; }
  async createDecision(value: { title: string; selection_mode: "single" | "multiple"; options: string[]; closes_at: string }) { return requireSanitizedDecision(await this.request<unknown>("/api/v1/decisions", { method: "POST", body: JSON.stringify(value) })); }
  async voteDecision(id: string, optionIds: string[], version: number, idempotencyKey = this.idempotency("decision-vote")) { return requireSanitizedDecision(await this.request<unknown>(`/api/v1/decisions/${id}/vote`, { method: "PUT", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ option_ids: optionIds, expected_version: version }) })); }
  async resolveDecision(id: string, action: "confirm" | "revote" | "random_draw", version: number, optionId?: string) { return requireSanitizedDecision(await this.request<unknown>(`/api/v1/decisions/${id}/resolve`, { method: "POST", body: JSON.stringify({ action, option_id: optionId, expected_version: version }) })); }
  decisionConversionPreview(id: string, target: DecisionConversionPreview["target"], optionId?: string) { return this.request<DecisionConversionPreview>(`/api/v1/decisions/${id}/conversion-preview`, { method: "POST", body: JSON.stringify({ target, option_id: optionId }) }); }
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
