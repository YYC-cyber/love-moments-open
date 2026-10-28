SCHEMA_VERSION = 33

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    phone TEXT UNIQUE,
    username TEXT UNIQUE,
    password_hash TEXT,
    nickname TEXT NOT NULL,
    avatar_label TEXT NOT NULL,
    locale TEXT NOT NULL DEFAULT 'zh-CN',
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    device_name TEXT NOT NULL DEFAULT '未知设备',
    user_agent TEXT NOT NULL DEFAULT '',
    ip_address TEXT NOT NULL DEFAULT '',
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);

CREATE TABLE IF NOT EXISTS auth_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username_hash TEXT NOT NULL,
    ip_hash TEXT NOT NULL,
    failed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_failures_window
ON auth_failures(username_hash, ip_hash, failed_at);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    endpoint_hash TEXT NOT NULL UNIQUE,
    subscription_ciphertext TEXT NOT NULL,
    device_name TEXT NOT NULL DEFAULT '未知设备',
    user_agent TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_success_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user_active
ON push_subscriptions(user_id, active, updated_at DESC);

CREATE TABLE IF NOT EXISTS push_outbox (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    tag TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_attempt_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(user_id, tag),
    CHECK(status IN ('pending','processing','sent','failed','expired','cancelled'))
);
CREATE INDEX IF NOT EXISTS idx_push_outbox_dispatch
ON push_outbox(status, next_attempt_at, expires_at);

CREATE TABLE IF NOT EXISTS couples (
    id TEXT PRIMARY KEY,
    member_a TEXT NOT NULL REFERENCES users(id),
    member_b TEXT NOT NULL REFERENCES users(id),
    paired_at TEXT NOT NULL,
    anniversary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_couples_member_a ON couples(member_a, status);
CREATE INDEX IF NOT EXISTS idx_couples_member_b ON couples(member_b, status);
CREATE TRIGGER IF NOT EXISTS prevent_duplicate_active_couple
BEFORE INSERT ON couples
WHEN NEW.status = 'active' AND (
    NEW.member_a = NEW.member_b OR EXISTS (
        SELECT 1 FROM couples
        WHERE status = 'active' AND (
            member_a IN (NEW.member_a, NEW.member_b)
            OR member_b IN (NEW.member_a, NEW.member_b)
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'user already paired');
END;

CREATE TABLE IF NOT EXISTS invites (
    id TEXT PRIMARY KEY,
    creator_id TEXT NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    sender_id TEXT NOT NULL REFERENCES users(id),
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    sent_at TEXT NOT NULL,
    read_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_couple_time ON messages(couple_id, sent_at);

CREATE TABLE IF NOT EXISTS call_records (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    caller_id TEXT NOT NULL REFERENCES users(id),
    callee_id TEXT REFERENCES users(id),
    started_at TEXT NOT NULL,
    answered_at TEXT,
    ended_at TEXT,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'ringing',
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_call_records_state
ON call_records(couple_id, state, started_at DESC);

CREATE TABLE IF NOT EXISTS call_signals (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES call_records(id) ON DELETE CASCADE,
    sender_id TEXT NOT NULL REFERENCES users(id),
    signal_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_call_signals_call_sequence
ON call_signals(call_id, sequence);

CREATE TABLE IF NOT EXISTS moments (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    creator_id TEXT NOT NULL REFERENCES users(id),
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    location TEXT,
    mood TEXT,
    visibility TEXT NOT NULL DEFAULT 'shared',
    reveal_at TEXT,
    media_url TEXT,
    media_id TEXT REFERENCES media_assets(id),
    source_message_id TEXT REFERENCES messages(id),
    deleted_at TEXT,
    purged_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_moments_couple_time ON moments(couple_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS moment_reactions (
    moment_id TEXT NOT NULL REFERENCES moments(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reaction TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (moment_id, user_id),
    CHECK(reaction IN ('heart','smile','hug'))
);

CREATE TABLE IF NOT EXISTS shared_comments (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    author_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    CHECK(resource_type IN (
        'moment','trip','itinerary_item','meal_wish','meal_item',
        'wishlist_item','calendar_entry','preference','date_plan'
    ))
);
CREATE INDEX IF NOT EXISTS idx_shared_comments_resource
ON shared_comments(couple_id, resource_type, resource_id, deleted_at, created_at);

CREATE TABLE IF NOT EXISTS trips (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    title TEXT NOT NULL,
    destination TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    budget_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    status TEXT NOT NULL DEFAULT 'planning',
    active_variant TEXT,
    current_revision INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    departure_city TEXT NOT NULL DEFAULT '',
    arrival_city TEXT NOT NULL DEFAULT '',
    outbound_time TEXT,
    return_time TEXT,
    travelers INTEGER NOT NULL DEFAULT 2,
    rooms INTEGER NOT NULL DEFAULT 1,
    hotel_budget_cents INTEGER NOT NULL DEFAULT 0,
    creation_job_id TEXT UNIQUE,
    deleted_at TEXT,
    deleted_by TEXT REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trip_generation_jobs (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    trip_id TEXT REFERENCES trips(id),
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, idempotency_key),
    CHECK(status IN ('queued','running','completed','failed','cancelled'))
);
CREATE INDEX IF NOT EXISTS idx_trip_generation_jobs_queue
ON trip_generation_jobs(status, created_at);

CREATE TABLE IF NOT EXISTS place_search_candidates (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    query TEXT NOT NULL,
    region_hint TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_place_candidates_owner_expiry
ON place_search_candidates(user_id, expires_at);

CREATE TABLE IF NOT EXISTS trip_destination_places (
    trip_id TEXT PRIMARY KEY REFERENCES trips(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_place_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    formatted_address TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    province TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    category TEXT NOT NULL DEFAULT '',
    confirmed_by TEXT NOT NULL REFERENCES users(id),
    confirmed_at TEXT NOT NULL,
    CHECK(provider IN ('amap','google_maps','manual'))
);
CREATE TABLE IF NOT EXISTS trip_preferences (
    trip_id TEXT NOT NULL REFERENCES trips(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    pace TEXT NOT NULL,
    interests TEXT NOT NULL,
    transport TEXT NOT NULL,
    diet TEXT NOT NULL,
    hotel TEXT NOT NULL,
    must_visits TEXT NOT NULL,
    PRIMARY KEY (trip_id, user_id)
);

CREATE TABLE IF NOT EXISTS plan_variants (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id),
    style TEXT NOT NULL,
    name TEXT NOT NULL,
    summary TEXT NOT NULL,
    estimated_budget_cents INTEGER NOT NULL,
    intensity TEXT NOT NULL,
    transport_minutes INTEGER NOT NULL,
    generation_source TEXT NOT NULL DEFAULT 'legacy',
    generation_message TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS trip_days (
    id TEXT PRIMARY KEY,
    variant_id TEXT NOT NULL REFERENCES plan_variants(id),
    day_number INTEGER NOT NULL,
    date TEXT NOT NULL,
    title TEXT NOT NULL,
    intensity TEXT NOT NULL,
    position INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS itinerary_items (
    id TEXT PRIMARY KEY,
    day_id TEXT NOT NULL REFERENCES trip_days(id),
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    place_id TEXT,
    start_time TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    cost_cents INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    locked INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS itinerary_item_places (
    itinerary_item_id TEXT PRIMARY KEY REFERENCES itinerary_items(id) ON DELETE CASCADE,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_place_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    formatted_address TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    province TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    confirmed_by TEXT NOT NULL REFERENCES users(id),
    confirmed_at TEXT NOT NULL,
    CHECK(provider IN ('amap','google_maps','manual'))
);
CREATE INDEX IF NOT EXISTS idx_itinerary_item_places_trip
ON itinerary_item_places(trip_id, itinerary_item_id);

CREATE TABLE IF NOT EXISTS route_cache (
    cache_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    mode TEXT NOT NULL,
    origin_place_id TEXT NOT NULL DEFAULT '',
    destination_place_id TEXT NOT NULL DEFAULT '',
    origin_latitude REAL NOT NULL,
    origin_longitude REAL NOT NULL,
    destination_latitude REAL NOT NULL,
    destination_longitude REAL NOT NULL,
    result_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_route_cache_expiry
ON route_cache(expires_at);

CREATE TABLE IF NOT EXISTS hotel_options (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id),
    name TEXT NOT NULL,
    area TEXT NOT NULL,
    room_type TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    selected INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS booking_links (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    url TEXT NOT NULL,
    quote_cents INTEGER NOT NULL,
    terms TEXT NOT NULL,
    includes_tax INTEGER NOT NULL DEFAULT 1,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_revisions (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id),
    revision INTEGER NOT NULL,
    parent_revision INTEGER,
    author_type TEXT NOT NULL,
    diff_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_revisions_trip_revision ON plan_revisions(trip_id, revision);

CREATE TABLE IF NOT EXISTS approvals (
    trip_id TEXT NOT NULL REFERENCES trips(id),
    revision INTEGER NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (trip_id, revision, user_id)
);

CREATE TABLE IF NOT EXISTS shared_lists (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    type TEXT NOT NULL,
    title TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shared_list_items (
    id TEXT PRIMARY KEY,
    list_id TEXT NOT NULL REFERENCES shared_lists(id),
    creator_id TEXT NOT NULL REFERENCES users(id),
    text TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    completed_at TEXT,
    position INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS daily_answers (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    question_date TEXT NOT NULL,
    question TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    answer TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(couple_id, question_date, user_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    couple_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_events (
    id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    occurrences INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_system_events_status_time
ON system_events(resolved_at, severity, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS ui_manifests (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    user_id TEXT REFERENCES users(id),
    surface TEXT NOT NULL,
    context_version INTEGER NOT NULL,
    context_hash TEXT NOT NULL,
    schema_version TEXT NOT NULL DEFAULT '1.0',
    manifest_json TEXT NOT NULL,
    source TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ui_manifests_lookup
ON ui_manifests(couple_id, user_id, surface, expires_at, created_at DESC);

CREATE TABLE IF NOT EXISTS ui_generation_jobs (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    surface TEXT NOT NULL,
    intent_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    result_manifest_id TEXT REFERENCES ui_manifests(id),
    error_code TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS ui_action_drafts (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    creator_id TEXT NOT NULL REFERENCES users(id),
    action_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    expected_versions_json TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    result_json TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ui_action_drafts_owner
ON ui_action_drafts(couple_id, creator_id, status, expires_at);

CREATE TABLE IF NOT EXISTS ai_context_grants (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    surface TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_context_grants_owner
ON ai_context_grants(couple_id, user_id, expires_at);

CREATE TABLE IF NOT EXISTS realtime_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    event_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_realtime_events_couple_sequence
ON realtime_events(couple_id, sequence);

CREATE TABLE IF NOT EXISTS media_assets (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    owner_id TEXT NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,
    original_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    thumbnail_path TEXT,
    thumbnail_checksum TEXT,
    size_bytes INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    metadata_stripped INTEGER NOT NULL DEFAULT 0,
    visibility TEXT NOT NULL DEFAULT 'shared',
    reveal_at TEXT,
    status TEXT NOT NULL DEFAULT 'ready',
    storage_provider TEXT NOT NULL DEFAULT 'local',
    storage_key TEXT,
    checksum TEXT,
    deleted_at TEXT,
    purged_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_assets_couple_time
ON media_assets(couple_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_assets_couple_status
ON media_assets(couple_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS system_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    database_bytes INTEGER NOT NULL,
    media_bytes INTEGER NOT NULL,
    media_files INTEGER NOT NULL,
    disk_free_bytes INTEGER NOT NULL,
    backup_age_hours REAL,
    unresolved_events INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_system_metrics_time
ON system_metrics(captured_at DESC);

CREATE TABLE IF NOT EXISTS upload_sessions (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    owner_id TEXT NOT NULL REFERENCES users(id),
    original_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'shared',
    expected_size INTEGER NOT NULL,
    received_size INTEGER NOT NULL DEFAULT 0,
    expected_checksum TEXT,
    chunk_size INTEGER NOT NULL DEFAULT 4194304,
    temp_path TEXT NOT NULL,
    thumbnail_temp_path TEXT,
    media_id TEXT REFERENCES media_assets(id),
    status TEXT NOT NULL DEFAULT 'pending',
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_upload_sessions_owner_status
ON upload_sessions(owner_id, status, expires_at);

CREATE TABLE IF NOT EXISTS couple_statuses (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    couple_id TEXT NOT NULL REFERENCES couples(id),
    status_code TEXT NOT NULL,
    emoji TEXT NOT NULL,
    text TEXT NOT NULL,
    expires_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_couple_statuses_couple
ON couple_statuses(couple_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS user_locations (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    city TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT '中国',
    latitude REAL,
    longitude REAL,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_reviews (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    week_start TEXT NOT NULL,
    week_end TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    moment_id TEXT REFERENCES moments(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(couple_id, week_start)
);

CREATE TABLE IF NOT EXISTS weekly_review_answers (
    review_id TEXT NOT NULL REFERENCES weekly_reviews(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    favorite TEXT NOT NULL,
    gratitude TEXT NOT NULL,
    next_week TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    confirmed_at TEXT,
    PRIMARY KEY(review_id, user_id)
);

CREATE TABLE IF NOT EXISTS date_plans (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    creator_id TEXT NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    plan_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    city TEXT NOT NULL,
    budget_cents INTEGER NOT NULL,
    preferences_json TEXT NOT NULL,
    selected_variant_id TEXT,
    final_items_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft',
    version INTEGER NOT NULL DEFAULT 1,
    moment_id TEXT REFERENCES moments(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_date_plans_couple_date
ON date_plans(couple_id, plan_date DESC);

CREATE TABLE IF NOT EXISTS date_plan_variants (
    id TEXT PRIMARY KEY,
    date_plan_id TEXT NOT NULL REFERENCES date_plans(id) ON DELETE CASCADE,
    style TEXT NOT NULL,
    name TEXT NOT NULL,
    summary TEXT NOT NULL,
    expected_cost_cents INTEGER NOT NULL,
    intensity TEXT NOT NULL,
    weather_fit TEXT NOT NULL,
    items_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(date_plan_id, style)
);

CREATE TABLE IF NOT EXISTS date_plan_votes (
    date_plan_id TEXT NOT NULL REFERENCES date_plans(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id),
    variant_id TEXT NOT NULL REFERENCES date_plan_variants(id),
    revealed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(date_plan_id, user_id)
);

CREATE TABLE IF NOT EXISTS date_plan_confirmations (
    date_plan_id TEXT NOT NULL REFERENCES date_plans(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY(date_plan_id, version, user_id)
);

CREATE TABLE IF NOT EXISTS widget_snapshots (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    couple_id TEXT NOT NULL REFERENCES couples(id),
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mutation_idempotency (
    couple_id TEXT NOT NULL REFERENCES couples(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(couple_id, user_id, operation, idempotency_key)
);

CREATE TABLE IF NOT EXISTS chat_imports (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    creator_id TEXT NOT NULL REFERENCES users(id),
    media_id TEXT REFERENCES media_assets(id),
    provider TEXT NOT NULL,
    source_label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    raw_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_imports_owner
ON chat_imports(couple_id, creator_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS chat_import_items (
    id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES chat_imports(id) ON DELETE CASCADE,
    speaker TEXT NOT NULL,
    content TEXT NOT NULL,
    occurred_at TEXT,
    confidence REAL NOT NULL DEFAULT 0,
    selected INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_chat_import_items_import
ON chat_import_items(import_id, position);

CREATE TABLE IF NOT EXISTS calendar_entries (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    creator_id TEXT NOT NULL REFERENCES users(id),
    entry_type TEXT NOT NULL,
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    all_day INTEGER NOT NULL DEFAULT 0,
    recurrence TEXT NOT NULL DEFAULT 'none',
    location TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    completed_at TEXT,
    deleted_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calendar_entries_couple_date
ON calendar_entries(couple_id, starts_at, deleted_at);

CREATE TABLE IF NOT EXISTS partner_preferences (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    creator_id TEXT NOT NULL REFERENCES users(id),
    subject_user_id TEXT NOT NULL REFERENCES users(id),
    category TEXT NOT NULL,
    label TEXT NOT NULL,
    value TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    deleted_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_partner_preferences_couple
ON partner_preferences(couple_id, category, deleted_at);

CREATE TABLE IF NOT EXISTS place_snapshots (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    name TEXT NOT NULL,
    city TEXT NOT NULL DEFAULT '',
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    coordinate_source TEXT NOT NULL,
    visited_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(couple_id, resource_type, resource_id, name)
);
CREATE INDEX IF NOT EXISTS idx_place_snapshots_couple_time
ON place_snapshots(couple_id, visited_at DESC);

CREATE TABLE IF NOT EXISTS quote_snapshots (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    trip_id TEXT REFERENCES trips(id),
    quote_type TEXT NOT NULL,
    provider TEXT NOT NULL,
    source_mode TEXT NOT NULL,
    title TEXT NOT NULL,
    subtitle TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    terms TEXT NOT NULL DEFAULT '',
    includes_tax INTEGER NOT NULL DEFAULT 1,
    booking_url TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT NOT NULL,
    expires_at TEXT,
    created_by TEXT REFERENCES users(id),
    selected_at TEXT,
    selected_by TEXT REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_quote_snapshots_trip
ON quote_snapshots(couple_id, trip_id, quote_type, fetched_at DESC);

CREATE TABLE IF NOT EXISTS trip_food_recommendations (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id),
    itinerary_item_id TEXT NOT NULL REFERENCES itinerary_items(id),
    meal_type TEXT NOT NULL,
    specialties TEXT NOT NULL,
    name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    telephone TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    rating REAL,
    average_cost_cents INTEGER,
    opening_hours TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    distance_meters INTEGER,
    search_area TEXT NOT NULL DEFAULT '',
    amap_poi_id TEXT NOT NULL DEFAULT '',
    navigation_url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    selected_at TEXT,
    selected_by TEXT REFERENCES users(id),
    fetched_at TEXT NOT NULL,
    UNIQUE(trip_id, itinerary_item_id, amap_poi_id)
);
CREATE INDEX IF NOT EXISTS idx_trip_food_recommendations
ON trip_food_recommendations(trip_id, itinerary_item_id, selected_at);

CREATE TABLE IF NOT EXISTS trip_food_review_evidence (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    itinerary_item_id TEXT NOT NULL REFERENCES itinerary_items(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    title TEXT NOT NULL,
    snippet TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    source_query TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    UNIQUE(itinerary_item_id, url)
);
CREATE INDEX IF NOT EXISTS idx_trip_food_review_evidence
ON trip_food_review_evidence(trip_id, itinerary_item_id, platform);

CREATE TABLE IF NOT EXISTS external_api_usage (
    provider TEXT NOT NULL,
    period_type TEXT NOT NULL CHECK(period_type IN ('day', 'month', 'lifetime')),
    period_key TEXT NOT NULL,
    used_count INTEGER NOT NULL DEFAULT 0 CHECK(used_count >= 0),
    limit_count INTEGER NOT NULL CHECK(limit_count >= 0),
    last_operation TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, period_key)
);
CREATE INDEX IF NOT EXISTS idx_external_api_usage_period
ON external_api_usage(period_type, period_key, updated_at DESC);

CREATE TABLE IF NOT EXISTS recipe_dishes (
    id TEXT PRIMARY KEY,
    couple_id TEXT REFERENCES couples(id),
    owner_id TEXT REFERENCES users(id),
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    cuisine TEXT NOT NULL CHECK(cuisine IN ('chinese','western')),
    category TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK(visibility IN ('system','couple','private')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recipe_dishes_scope
ON recipe_dishes(couple_id, visibility, normalized_name);

CREATE TABLE IF NOT EXISTS recipes (
    id TEXT PRIMARY KEY,
    dish_id TEXT REFERENCES recipe_dishes(id),
    couple_id TEXT REFERENCES couples(id),
    owner_id TEXT REFERENCES users(id),
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    variant_name TEXT NOT NULL DEFAULT '经典做法',
    cooking_method TEXT NOT NULL DEFAULT '',
    method_tags_json TEXT NOT NULL DEFAULT '[]',
    cuisine TEXT NOT NULL CHECK(cuisine IN ('chinese','western')),
    category TEXT NOT NULL,
    meal_slots_json TEXT NOT NULL DEFAULT '[]',
    base_servings INTEGER NOT NULL CHECK(base_servings BETWEEN 1 AND 20),
    prep_minutes INTEGER NOT NULL DEFAULT 0 CHECK(prep_minutes BETWEEN 0 AND 1440),
    cook_minutes INTEGER NOT NULL DEFAULT 0 CHECK(cook_minutes BETWEEN 0 AND 1440),
    passive_minutes INTEGER NOT NULL DEFAULT 0 CHECK(passive_minutes BETWEEN 0 AND 10080),
    difficulty TEXT NOT NULL CHECK(difficulty IN ('easy','medium','hard')),
    tips_json TEXT NOT NULL DEFAULT '[]',
    allergen_tags_json TEXT NOT NULL DEFAULT '[]',
    source_type TEXT NOT NULL CHECK(source_type IN ('built_in','user','ai','external')),
    source_provider TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL CHECK(visibility IN ('system','couple','private')),
    cover_media_id TEXT REFERENCES media_assets(id),
    version INTEGER NOT NULL DEFAULT 1,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recipes_scope
ON recipes(couple_id, visibility, cuisine, deleted_at, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_recipes_name
ON recipes(normalized_name, deleted_at);
-- idx_recipes_dish is created by the migration after older databases receive
-- the dish_id column. Creating it here would make an in-place v17 upgrade fail
-- before ALTER TABLE can run.

CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id TEXT PRIMARY KEY,
    recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    quantity_milli INTEGER,
    quantity_text TEXT NOT NULL DEFAULT '',
    canonical_unit TEXT NOT NULL,
    unit_family TEXT NOT NULL CHECK(unit_family IN ('mass','volume','count','vague')),
    specification TEXT NOT NULL DEFAULT '',
    preparation TEXT NOT NULL DEFAULT '',
    ingredient_group TEXT NOT NULL DEFAULT 'main' CHECK(ingredient_group IN ('main','seasoning')),
    optional INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe
ON recipe_ingredients(recipe_id, position);

CREATE TABLE IF NOT EXISTS recipe_steps (
    id TEXT PRIMARY KEY,
    recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    instruction TEXT NOT NULL,
    timer_seconds INTEGER,
    title TEXT NOT NULL DEFAULT '',
    heat_level TEXT NOT NULL DEFAULT '',
    key_point TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recipe_steps_recipe
ON recipe_steps(recipe_id, position);

CREATE TABLE IF NOT EXISTS recipe_media (
    id TEXT PRIMARY KEY,
    recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    step_id TEXT REFERENCES recipe_steps(id) ON DELETE CASCADE,
    media_type TEXT NOT NULL CHECK(media_type IN ('image','video','link')),
    role TEXT NOT NULL CHECK(role IN ('cover','step','paired','reference','search')),
    provider TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    thumbnail_url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    attribution TEXT NOT NULL DEFAULT '',
    license_note TEXT NOT NULL DEFAULT '',
    match_details_json TEXT NOT NULL DEFAULT '{}',
    match_status TEXT NOT NULL DEFAULT 'unverified' CHECK(match_status IN ('verified','reference','unverified','rejected')),
    availability_status TEXT NOT NULL DEFAULT 'unknown' CHECK(availability_status IN ('unknown','healthy','suspect','unavailable','manual_check')),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT,
    last_ok_at TEXT,
    next_check_at TEXT,
    created_by TEXT REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recipe_media_recipe
ON recipe_media(recipe_id, media_type, role, created_at);
CREATE INDEX IF NOT EXISTS idx_recipe_media_due
ON recipe_media(media_type, next_check_at, availability_status);

CREATE TABLE IF NOT EXISTS recipe_media_checks (
    id TEXT PRIMARY KEY,
    media_id TEXT NOT NULL REFERENCES recipe_media(id) ON DELETE CASCADE,
    checked_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    status_code INTEGER,
    result TEXT NOT NULL,
    error_type TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_recipe_media_checks_media
ON recipe_media_checks(media_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS meal_wishes (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    creator_id TEXT NOT NULL REFERENCES users(id),
    recipe_id TEXT REFERENCES recipes(id),
    title TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_meal_wishes_couple
ON meal_wishes(couple_id, deleted_at, created_at DESC);

CREATE TABLE IF NOT EXISTS meal_carts (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','archived')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_meal_carts_one_active
ON meal_carts(couple_id) WHERE status='active';

CREATE TABLE IF NOT EXISTS meal_cart_items (
    id TEXT PRIMARY KEY,
    cart_id TEXT NOT NULL REFERENCES meal_carts(id),
    couple_id TEXT NOT NULL REFERENCES couples(id),
    recipe_id TEXT REFERENCES recipes(id),
    recipe_snapshot_json TEXT NOT NULL,
    planned_date TEXT NOT NULL,
    meal_slot TEXT NOT NULL CHECK(meal_slot IN ('breakfast','lunch','dinner','snack')),
    planned_time TEXT,
    servings INTEGER NOT NULL CHECK(servings BETWEEN 1 AND 20),
    ordered_by TEXT NOT NULL REFERENCES users(id),
    cook_user_id TEXT REFERENCES users(id),
    status TEXT NOT NULL CHECK(status IN ('selected','accepted','shopping','cooking','completed','cancelled')),
    course_type TEXT NOT NULL DEFAULT 'main',
    note TEXT NOT NULL DEFAULT '',
    completed_at TEXT,
    archived_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meal_items_calendar
ON meal_cart_items(couple_id, planned_date, meal_slot, status);
CREATE INDEX IF NOT EXISTS idx_meal_items_cart
ON meal_cart_items(cart_id, planned_date, meal_slot, created_at);

CREATE TABLE IF NOT EXISTS shopping_lists (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    cart_id TEXT REFERENCES meal_carts(id),
    title TEXT NOT NULL,
    range_start TEXT NOT NULL,
    range_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','completed','archived')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    archived_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_shopping_lists_one_active
ON shopping_lists(couple_id) WHERE status='active';

CREATE TABLE IF NOT EXISTS shopping_list_items (
    id TEXT PRIMARY KEY,
    shopping_list_id TEXT NOT NULL REFERENCES shopping_lists(id) ON DELETE CASCADE,
    canonical_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    quantity_milli INTEGER,
    quantity_text TEXT NOT NULL DEFAULT '',
    canonical_unit TEXT NOT NULL,
    unit_family TEXT NOT NULL,
    category TEXT NOT NULL,
    specification TEXT NOT NULL DEFAULT '',
    checked INTEGER NOT NULL DEFAULT 0,
    manual_locked INTEGER NOT NULL DEFAULT 0,
    manually_added INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shopping_items_list
ON shopping_list_items(shopping_list_id, category, position);

CREATE TABLE IF NOT EXISTS shopping_item_sources (
    shopping_item_id TEXT NOT NULL REFERENCES shopping_list_items(id) ON DELETE CASCADE,
    meal_item_id TEXT NOT NULL REFERENCES meal_cart_items(id) ON DELETE CASCADE,
    recipe_ingredient_id TEXT NOT NULL,
    contributed_quantity_milli INTEGER,
    PRIMARY KEY(shopping_item_id, meal_item_id, recipe_ingredient_id)
);

CREATE TABLE IF NOT EXISTS recipe_provider_cache (
    cache_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recipe_provider_cache_expiry
ON recipe_provider_cache(provider, expires_at);

CREATE TABLE IF NOT EXISTS recipe_drafts (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    creator_id TEXT NOT NULL REFERENCES users(id),
    source_type TEXT NOT NULL CHECK(source_type IN ('ai','external')),
    source_provider TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','cancelled','expired')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_recipe_drafts_owner
ON recipe_drafts(couple_id, creator_id, status, expires_at);

CREATE TABLE IF NOT EXISTS recipe_generation_jobs (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    job_type TEXT NOT NULL CHECK(job_type IN ('generate','localize')),
    provider TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed')),
    result_draft_id TEXT REFERENCES recipe_drafts(id),
    error_code TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_recipe_jobs_owner
ON recipe_generation_jobs(couple_id, user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS trip_assistant_messages (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    couple_id TEXT NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
    author_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT NOT NULL,
    intent_type TEXT NOT NULL DEFAULT 'general',
    cards_json TEXT NOT NULL DEFAULT '[]',
    idempotency_key TEXT,
    reply_to TEXT REFERENCES trip_assistant_messages(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(couple_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_trip_assistant_messages_trip
ON trip_assistant_messages(trip_id, created_at, id);

CREATE TABLE IF NOT EXISTS trip_assistant_action_drafts (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    couple_id TEXT NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
    creator_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action_type TEXT NOT NULL CHECK(action_type IN ('itinerary_change','day_replan')),
    payload_json TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    expected_trip_version INTEGER NOT NULL,
    expected_item_version INTEGER NOT NULL,
    expected_item_versions_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','confirmed','cancelled','expired')),
    idempotency_key TEXT,
    result_json TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(couple_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_trip_assistant_drafts_owner
ON trip_assistant_action_drafts(trip_id, creator_id, status, expires_at);

CREATE TABLE IF NOT EXISTS trip_memory_drafts (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    couple_id TEXT NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
    creator_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    source_summary_json TEXT NOT NULL DEFAULT '{}',
    is_early INTEGER NOT NULL DEFAULT 0,
    expected_trip_version INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','confirmed','cancelled','expired')),
    idempotency_key TEXT,
    result_json TEXT,
    moment_id TEXT REFERENCES moments(id),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(couple_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_trip_memory_drafts_trip
ON trip_memory_drafts(trip_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS life_assistant_plans (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
    creator_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending_confirmation'
        CHECK(status IN ('generating','pending_confirmation','applied','cancelled','failed','expired')),
    version INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'rules' CHECK(source IN ('rules','ai')),
    request_hash TEXT NOT NULL,
    expected_versions_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_life_assistant_plans_couple
ON life_assistant_plans(couple_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_life_assistant_plans_expiry
ON life_assistant_plans(status, expires_at);

CREATE TABLE IF NOT EXISTS life_assistant_plan_items (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES life_assistant_plans(id) ON DELETE CASCADE,
    item_type TEXT NOT NULL
        CHECK(item_type IN ('calendar.create','todo.create','meal.schedule','meal.wish','shopping.merge')),
    payload_json TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    position INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_life_assistant_plan_items_plan
ON life_assistant_plan_items(plan_id, position);

CREATE TABLE IF NOT EXISTS life_assistant_confirmations (
    plan_id TEXT NOT NULL REFERENCES life_assistant_plans(id) ON DELETE CASCADE,
    plan_version INTEGER NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, plan_version, user_id)
);
CREATE INDEX IF NOT EXISTS idx_life_assistant_confirmations_user
ON life_assistant_confirmations(user_id, confirmed_at DESC);

CREATE TABLE IF NOT EXISTS life_assistant_jobs (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
    creator_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_request_id TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
    result_plan_id TEXT REFERENCES life_assistant_plans(id) ON DELETE SET NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(couple_id, creator_id, client_request_id)
);
CREATE INDEX IF NOT EXISTS idx_life_assistant_jobs_owner
ON life_assistant_jobs(couple_id, creator_id, created_at DESC);

CREATE TABLE IF NOT EXISTS in_app_notifications (
    id TEXT PRIMARY KEY,
    recipient_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    actor_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    couple_id TEXT NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    dedupe_key TEXT NOT NULL,
    read_at TEXT,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(recipient_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_in_app_notifications_inbox
ON in_app_notifications(recipient_id, archived_at, read_at, created_at DESC);

CREATE TABLE IF NOT EXISTS daily_couple_summaries (
    id TEXT PRIMARY KEY,
    couple_id TEXT NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
    local_date TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'rules' CHECK(source IN ('rules','ai')),
    created_at TEXT NOT NULL,
    UNIQUE(couple_id, local_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_couple_summaries_couple
ON daily_couple_summaries(couple_id, local_date DESC);

CREATE TABLE IF NOT EXISTS couple_assistant_settings (
    couple_id TEXT PRIMARY KEY REFERENCES couples(id) ON DELETE CASCADE,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    daily_summary_enabled INTEGER NOT NULL DEFAULT 1,
    daily_summary_time TEXT NOT NULL DEFAULT '21:30',
    updated_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);
"""
