import re
import sqlite3


SCHEMA_VERSION = 34
V34_SCHEMA_MARKER = "-- V34 FOUNDATION TABLES --"

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
    version INTEGER NOT NULL DEFAULT 1,
    item_kind TEXT NOT NULL DEFAULT 'legacy_unknown'
        CHECK(item_kind IN ('legacy_unknown','general','gift','meal','travel')),
    deleted_at TEXT,
    created_at TEXT,
    updated_at TEXT
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
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT,
    resource_version INTEGER CHECK(resource_version IS NULL OR resource_version >= 1),
    usage_scope TEXT NOT NULL DEFAULT 'legacy_unknown'
        CHECK(usage_scope IN ('legacy_unknown','selected_content')),
    request_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1)
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
    created_at TEXT NOT NULL,
    captured_at TEXT,
    date_source TEXT NOT NULL DEFAULT 'legacy_unknown'
        CHECK(date_source IN ('legacy_unknown','user_confirmed','media_capture','trip_date','system_provisional')),
    source_type TEXT NOT NULL DEFAULT 'legacy_unknown'
        CHECK(source_type IN ('human','real_api','rules','ai','mock','cache','legacy_unknown')),
    duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0)
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
    updated_at TEXT NOT NULL,
    participant_a_id TEXT REFERENCES users(id),
    participant_b_id TEXT REFERENCES users(id)
        CHECK(participant_b_id IS NULL OR (participant_a_id IS NOT NULL AND participant_b_id <> participant_a_id)),
    blocks_time INTEGER NOT NULL DEFAULT 0 CHECK(blocks_time IN (0,1)),
    source_type TEXT NOT NULL DEFAULT 'legacy_unknown'
        CHECK(source_type IN ('human','real_api','rules','ai','mock','cache','legacy_unknown'))
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

-- V34 FOUNDATION TABLES --
CREATE TABLE IF NOT EXISTS decisions (
 id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL REFERENCES couples(id),
 creator_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 200),
 selection_mode TEXT NOT NULL CHECK(selection_mode IN ('single','multiple')),
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','awaiting_extension','revealed','pending_confirmation','confirmed','cancelled','converted')),
 voting_round INTEGER NOT NULL DEFAULT 1 CHECK(voting_round >= 1),
 closes_at TEXT NOT NULL, revealed_at TEXT, options_locked_at TEXT,
 resolution_json TEXT CHECK(resolution_json IS NULL OR (json_valid(resolution_json) AND json_type(resolution_json)='object')),
 conversion_json TEXT CHECK(conversion_json IS NULL OR (json_valid(conversion_json) AND json_type(conversion_json)='object')),
 client_request_id TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(couple_id,creator_id,client_request_id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_queue ON decisions(couple_id,status,updated_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_due ON decisions(status,closes_at) WHERE deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS decision_options (
 id TEXT PRIMARY KEY NOT NULL, decision_id TEXT NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
 label TEXT NOT NULL CHECK(length(trim(label)) BETWEEN 1 AND 300),
 position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 5),
 version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 UNIQUE(decision_id,position), UNIQUE(decision_id,id)
);
CREATE TABLE IF NOT EXISTS decision_votes (
 decision_id TEXT NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
 voting_round INTEGER NOT NULL CHECK(voting_round >= 1), user_id TEXT NOT NULL REFERENCES users(id),
 option_id TEXT NOT NULL, submitted_at TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 PRIMARY KEY(decision_id,voting_round,user_id,option_id),
 FOREIGN KEY(decision_id,option_id) REFERENCES decision_options(decision_id,id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_decision_votes_owner ON decision_votes(user_id,decision_id,voting_round);
CREATE TABLE IF NOT EXISTS decision_confirmations (
 decision_id TEXT NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
 decision_version INTEGER NOT NULL CHECK(decision_version >= 1), user_id TEXT NOT NULL REFERENCES users(id),
 confirmed_at TEXT NOT NULL, PRIMARY KEY(decision_id,decision_version,user_id)
);
CREATE TABLE IF NOT EXISTS wishlist_claims (
 id TEXT PRIMARY KEY NOT NULL, item_id TEXT NOT NULL REFERENCES shared_list_items(id) ON DELETE CASCADE,
 claimant_id TEXT NOT NULL REFERENCES users(id),
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','released','cancelled','completed')),
 private_note TEXT NOT NULL DEFAULT '', reveal_claimant INTEGER NOT NULL DEFAULT 0 CHECK(reveal_claimant IN (0,1)),
 item_version_at_claim INTEGER NOT NULL CHECK(item_version_at_claim >= 1),
 client_request_id TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, ended_at TEXT,
 UNIQUE(claimant_id,client_request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wishlist_claim_one_active ON wishlist_claims(item_id) WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_wishlist_claim_owner ON wishlist_claims(claimant_id,status,updated_at DESC);
CREATE TABLE IF NOT EXISTS ingredient_catalog (
 id TEXT PRIMARY KEY NOT NULL, couple_id TEXT REFERENCES couples(id),
 canonical_name TEXT NOT NULL CHECK(length(trim(canonical_name)) BETWEEN 1 AND 100),
 normalized_name TEXT NOT NULL CHECK(length(normalized_name)>0),
 aliases_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(aliases_json) AND json_type(aliases_json)='array'),
 allergens_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(allergens_json) AND json_type(allergens_json)='array'),
 category TEXT NOT NULL DEFAULT 'other' CHECK(category IN ('produce','meat_eggs','seafood','dairy','staples_oils','seasonings','frozen','household','other')),
 source_type TEXT NOT NULL DEFAULT 'human' CHECK(source_type IN ('human','real_api','rules','ai','mock','cache','legacy_unknown')),
 version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ingredient_system_name ON ingredient_catalog(normalized_name) WHERE couple_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ingredient_couple_name ON ingredient_catalog(couple_id,normalized_name) WHERE couple_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS food_profiles (
 couple_id TEXT NOT NULL REFERENCES couples(id), user_id TEXT NOT NULL REFERENCES users(id),
 allergies_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(allergies_json) AND json_type(allergies_json)='array'),
 avoidances_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(avoidances_json) AND json_type(avoidances_json)='array'),
 preferences_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(preferences_json) AND json_type(preferences_json)='object'),
 version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1), updated_at TEXT NOT NULL,
 PRIMARY KEY(couple_id,user_id)
);
CREATE TABLE IF NOT EXISTS pantry_items (
 id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL REFERENCES couples(id),
 ingredient_id TEXT NOT NULL REFERENCES ingredient_catalog(id), updated_by TEXT NOT NULL REFERENCES users(id),
 state TEXT NOT NULL CHECK(state IN ('have','low','out')),
 storage_location TEXT CHECK(storage_location IS NULL OR storage_location IN ('ambient','chilled','frozen')),
 expires_on TEXT, quantity_note TEXT NOT NULL DEFAULT '',
 version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(couple_id,ingredient_id)
);
CREATE INDEX IF NOT EXISTS idx_pantry_expiry ON pantry_items(couple_id,expires_on) WHERE state <> 'out' AND expires_on IS NOT NULL;
CREATE TABLE IF NOT EXISTS shopping_category_preferences (
 couple_id TEXT NOT NULL REFERENCES couples(id),
 category TEXT NOT NULL CHECK(category IN ('produce','meat_eggs','seafood','dairy','staples_oils','seasonings','frozen','household','other')),
 position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 8),
 version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1), updated_at TEXT NOT NULL,
 PRIMARY KEY(couple_id,category)
);
CREATE INDEX IF NOT EXISTS idx_shopping_category_order ON shopping_category_preferences(couple_id,position,category);
CREATE TABLE IF NOT EXISTS weekly_meal_plan_drafts (
 id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL REFERENCES couples(id), creator_id TEXT NOT NULL REFERENCES users(id),
 starts_on TEXT NOT NULL, ends_on TEXT NOT NULL CHECK(ends_on >= starts_on),
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','pending_confirmation','applied','cancelled','expired','failed')),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),
 preview_json TEXT NOT NULL CHECK(json_valid(preview_json) AND json_type(preview_json)='object'),
 expected_versions_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(expected_versions_json) AND json_type(expected_versions_json)='object'),
 source_type TEXT NOT NULL CHECK(source_type IN ('human','real_api','rules','ai','mock','cache','legacy_unknown')),
 confirmed_a_id TEXT REFERENCES users(id), confirmed_a_version INTEGER CHECK(confirmed_a_version IS NULL OR confirmed_a_version >= 1), confirmed_a_at TEXT,
 confirmed_b_id TEXT REFERENCES users(id), confirmed_b_version INTEGER CHECK(confirmed_b_version IS NULL OR confirmed_b_version >= 1), confirmed_b_at TEXT,
 result_json TEXT CHECK(result_json IS NULL OR (json_valid(result_json) AND json_type(result_json)='object')),
 client_request_id TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 expires_at TEXT NOT NULL, applied_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(couple_id,creator_id,client_request_id),
 CHECK((confirmed_a_id IS NULL AND confirmed_a_version IS NULL AND confirmed_a_at IS NULL) OR (confirmed_a_id IS NOT NULL AND confirmed_a_version IS NOT NULL AND confirmed_a_at IS NOT NULL)),
 CHECK((confirmed_b_id IS NULL AND confirmed_b_version IS NULL AND confirmed_b_at IS NULL) OR (confirmed_b_id IS NOT NULL AND confirmed_b_version IS NOT NULL AND confirmed_b_at IS NOT NULL)),
 CHECK(confirmed_a_id IS NULL OR confirmed_b_id IS NULL OR confirmed_a_id <> confirmed_b_id)
);
CREATE INDEX IF NOT EXISTS idx_weekly_meal_drafts_owner ON weekly_meal_plan_drafts(couple_id,creator_id,status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_weekly_meal_drafts_expiry ON weekly_meal_plan_drafts(status,expires_at);
CREATE TABLE IF NOT EXISTS cook_sessions (
 id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL REFERENCES couples(id), creator_id TEXT NOT NULL REFERENCES users(id),
 recipe_id TEXT REFERENCES recipes(id) ON DELETE SET NULL, meal_item_id TEXT REFERENCES meal_cart_items(id) ON DELETE SET NULL,
 recipe_snapshot_json TEXT NOT NULL CHECK(json_valid(recipe_snapshot_json) AND json_type(recipe_snapshot_json)='object'),
 visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','shared')),
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','cancelled')),
 current_step INTEGER NOT NULL DEFAULT 0 CHECK(current_step >= 0),
 client_request_id TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 started_at TEXT NOT NULL, updated_at TEXT NOT NULL, ended_at TEXT,
 UNIQUE(couple_id,creator_id,client_request_id)
);
CREATE INDEX IF NOT EXISTS idx_cook_sessions_owner ON cook_sessions(couple_id,creator_id,status,updated_at DESC);
CREATE TABLE IF NOT EXISTS cook_timers (
 id TEXT PRIMARY KEY NOT NULL, session_id TEXT NOT NULL REFERENCES cook_sessions(id) ON DELETE CASCADE,
 step_index INTEGER NOT NULL CHECK(step_index >= 0), label TEXT NOT NULL DEFAULT '',
 duration_seconds INTEGER NOT NULL CHECK(duration_seconds BETWEEN 1 AND 604800),
 started_at TEXT NOT NULL, ends_at TEXT NOT NULL CHECK(ends_at > started_at),
 status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed','cancelled')),
 client_request_id TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1), updated_at TEXT NOT NULL,
 UNIQUE(session_id,client_request_id)
);
CREATE INDEX IF NOT EXISTS idx_cook_timers_due ON cook_timers(session_id,status,ends_at);
CREATE TABLE IF NOT EXISTS ingredient_substitutions (
 id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL REFERENCES couples(id), creator_id TEXT NOT NULL REFERENCES users(id),
 from_ingredient_id TEXT NOT NULL REFERENCES ingredient_catalog(id), to_ingredient_id TEXT NOT NULL REFERENCES ingredient_catalog(id),
 recipe_id TEXT REFERENCES recipes(id) ON DELETE SET NULL,
 meal_item_id TEXT REFERENCES meal_cart_items(id) ON DELETE CASCADE,
 snapshot_ingredient_ref TEXT,
 meal_version INTEGER CHECK(meal_version IS NULL OR meal_version >= 1),
 expected_versions_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(expected_versions_json) AND json_type(expected_versions_json)='object'),
 rule_key TEXT,
 rule_version INTEGER CHECK(rule_version IS NULL OR rule_version >= 1),
 ratio_note TEXT NOT NULL DEFAULT '', suitable_methods_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(suitable_methods_json) AND json_type(suitable_methods_json)='array'),
 effect_note TEXT NOT NULL DEFAULT '', added_allergens_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(added_allergens_json) AND json_type(added_allergens_json)='array'),
 safety_status TEXT NOT NULL DEFAULT 'needs_review' CHECK(safety_status IN ('needs_review','allowed','blocked')),
 profile_versions_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(profile_versions_json) AND json_type(profile_versions_json)='object'),
 source_type TEXT NOT NULL CHECK(source_type IN ('human','real_api','rules','ai','mock','cache','legacy_unknown')),
 status TEXT NOT NULL DEFAULT 'suggested' CHECK(status IN ('suggested','applied','dismissed','expired')),
 client_request_id TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(from_ingredient_id <> to_ingredient_id), CHECK(status <> 'applied' OR safety_status='allowed'),
 UNIQUE(couple_id,creator_id,client_request_id)
);
CREATE INDEX IF NOT EXISTS idx_ingredient_substitutions_owner ON ingredient_substitutions(couple_id,creator_id,status,created_at DESC);
CREATE TABLE IF NOT EXISTS trip_item_preferences (
 id TEXT PRIMARY KEY NOT NULL, trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
 itinerary_item_id TEXT NOT NULL REFERENCES itinerary_items(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id), negotiation_round INTEGER NOT NULL DEFAULT 1 CHECK(negotiation_round >= 1),
 preference TEXT NOT NULL CHECK(preference IN ('must','okay','avoid')),
 expected_item_version INTEGER NOT NULL CHECK(expected_item_version >= 1), submitted_at TEXT,
 override_confirmed_at TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1), updated_at TEXT NOT NULL,
 UNIQUE(trip_id,itinerary_item_id,negotiation_round,user_id)
);
CREATE INDEX IF NOT EXISTS idx_trip_preferences_owner ON trip_item_preferences(trip_id,user_id,negotiation_round,submitted_at);
CREATE TABLE IF NOT EXISTS trip_realism_checks (
 id TEXT PRIMARY KEY NOT NULL, trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
 creator_id TEXT NOT NULL REFERENCES users(id), trip_version INTEGER NOT NULL CHECK(trip_version >= 1),
 expected_item_versions_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(expected_item_versions_json) AND json_type(expected_item_versions_json)='object'),
 findings_json TEXT NOT NULL CHECK(json_valid(findings_json) AND json_type(findings_json)='array'),
 source_type TEXT NOT NULL CHECK(source_type IN ('human','real_api','rules','ai','mock','cache','legacy_unknown')),
 status TEXT NOT NULL DEFAULT 'current' CHECK(status IN ('current','stale','dismissed')),
 client_request_id TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 checked_at TEXT NOT NULL, expires_at TEXT, UNIQUE(trip_id,creator_id,client_request_id)
);
CREATE INDEX IF NOT EXISTS idx_trip_realism_checks_current ON trip_realism_checks(trip_id,status,checked_at DESC);
CREATE TABLE IF NOT EXISTS trip_packing_items (
 id TEXT PRIMARY KEY NOT NULL, trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
 creator_id TEXT NOT NULL REFERENCES users(id), label TEXT NOT NULL CHECK(length(trim(label)) BETWEEN 1 AND 200),
 assigned_user_id TEXT REFERENCES users(id), assignment TEXT NOT NULL DEFAULT 'shared' CHECK(assignment IN ('shared','person','not_needed')),
 checked INTEGER NOT NULL DEFAULT 0 CHECK(checked IN (0,1)), position INTEGER NOT NULL DEFAULT 0 CHECK(position >= 0),
 source_type TEXT NOT NULL CHECK(source_type IN ('human','real_api','rules','ai','mock','cache','legacy_unknown')),
 client_request_id TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK((assignment='person' AND assigned_user_id IS NOT NULL) OR (assignment<>'person' AND assigned_user_id IS NULL)),
 UNIQUE(trip_id,creator_id,client_request_id)
);
CREATE INDEX IF NOT EXISTS idx_trip_packing_active ON trip_packing_items(trip_id,deleted_at,position);
CREATE TABLE IF NOT EXISTS trip_emergency_entries (
 id TEXT PRIMARY KEY NOT NULL, trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
 creator_id TEXT NOT NULL REFERENCES users(id),
 entry_type TEXT NOT NULL CHECK(entry_type IN ('hotel','booking','contact','insurance','hospital','return','other')),
 title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 200),
 details_json TEXT NOT NULL CHECK(json_valid(details_json) AND json_type(details_json)='object'),
 visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','shared')),
 source_type TEXT NOT NULL CHECK(source_type IN ('human','real_api','rules','ai','mock','cache','legacy_unknown')),
 position INTEGER NOT NULL DEFAULT 0 CHECK(position >= 0), client_request_id TEXT,
 version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1), deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(trip_id,creator_id,client_request_id)
);
CREATE INDEX IF NOT EXISTS idx_trip_emergency_visible ON trip_emergency_entries(trip_id,creator_id,visibility,deleted_at,position);
CREATE TABLE IF NOT EXISTS trip_expenses (
 id TEXT PRIMARY KEY NOT NULL, trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
 creator_id TEXT NOT NULL REFERENCES users(id), payer_id TEXT NOT NULL REFERENCES users(id),
 amount_minor INTEGER NOT NULL CHECK(typeof(amount_minor)='integer' AND amount_minor BETWEEN 1 AND 9007199254740991),
 currency TEXT NOT NULL CHECK(length(currency)=3 AND currency GLOB '[A-Z][A-Z][A-Z]'),
 category TEXT NOT NULL CHECK(category IN ('transport','lodging','food','tickets','shopping','other')),
 split_mode TEXT NOT NULL DEFAULT 'equal' CHECK(split_mode IN ('equal','personal')),
 beneficiary_id TEXT REFERENCES users(id), note TEXT NOT NULL DEFAULT '', occurred_on TEXT NOT NULL,
 settled_at TEXT, settled_by TEXT REFERENCES users(id),
 client_request_id TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK((split_mode='equal' AND beneficiary_id IS NULL) OR (split_mode='personal' AND beneficiary_id IS NOT NULL)),
 CHECK((settled_at IS NULL AND settled_by IS NULL) OR (settled_at IS NOT NULL AND settled_by IS NOT NULL)),
 UNIQUE(trip_id,creator_id,client_request_id)
);
CREATE INDEX IF NOT EXISTS idx_trip_expenses_balance ON trip_expenses(trip_id,currency,settled_at,occurred_on) WHERE deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS memory_provenance (
 id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL REFERENCES couples(id), owner_id TEXT REFERENCES users(id),
 moment_id TEXT REFERENCES moments(id) ON DELETE CASCADE,
 media_id TEXT REFERENCES media_assets(id) ON DELETE CASCADE,
 trip_id TEXT REFERENCES trips(id) ON DELETE CASCADE,
 recipe_id TEXT REFERENCES recipes(id) ON DELETE CASCADE,
 calendar_entry_id TEXT REFERENCES calendar_entries(id) ON DELETE CASCADE,
 shared_list_item_id TEXT REFERENCES shared_list_items(id) ON DELETE CASCADE,
 source_type TEXT NOT NULL DEFAULT 'legacy_unknown' CHECK(source_type IN ('human','real_api','rules','ai','mock','cache','legacy_unknown')),
 date_source TEXT NOT NULL DEFAULT 'legacy_unknown' CHECK(date_source IN ('legacy_unknown','user_confirmed','media_capture','trip_date','system_provisional')),
 evidence_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(evidence_json) AND json_type(evidence_json)='object'),
 version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK((moment_id IS NOT NULL)+(media_id IS NOT NULL)+(trip_id IS NOT NULL)+(recipe_id IS NOT NULL)+(calendar_entry_id IS NOT NULL)+(shared_list_item_id IS NOT NULL)=1),
 UNIQUE(moment_id), UNIQUE(media_id), UNIQUE(trip_id), UNIQUE(recipe_id), UNIQUE(calendar_entry_id), UNIQUE(shared_list_item_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_provenance_owner ON memory_provenance(couple_id,owner_id,source_type);
CREATE TABLE IF NOT EXISTS memory_descriptions (
 id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL REFERENCES couples(id), author_id TEXT NOT NULL REFERENCES users(id),
 moment_id TEXT REFERENCES moments(id) ON DELETE CASCADE, trip_id TEXT REFERENCES trips(id) ON DELETE CASCADE,
 body TEXT NOT NULL DEFAULT '', visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','shared')),
 source_type TEXT NOT NULL CHECK(source_type IN ('human','real_api','rules','ai','mock','cache','legacy_unknown')),
 merge_proposal_json TEXT CHECK(merge_proposal_json IS NULL OR (json_valid(merge_proposal_json) AND json_type(merge_proposal_json)='object')),
 merge_confirmed_by TEXT REFERENCES users(id), merge_confirmed_version INTEGER CHECK(merge_confirmed_version IS NULL OR merge_confirmed_version >= 1), merge_confirmed_at TEXT,
 version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1), deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK((moment_id IS NOT NULL)+(trip_id IS NOT NULL)=1), UNIQUE(moment_id,author_id), UNIQUE(trip_id,author_id),
 CHECK((merge_confirmed_by IS NULL AND merge_confirmed_version IS NULL AND merge_confirmed_at IS NULL) OR
       (merge_confirmed_by IS NOT NULL AND merge_confirmed_version IS NOT NULL AND merge_confirmed_at IS NOT NULL AND merge_proposal_json IS NOT NULL)),
 CHECK(merge_confirmed_by IS NULL OR merge_confirmed_by <> author_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_descriptions_owner ON memory_descriptions(couple_id,author_id,visibility,deleted_at,updated_at DESC);
CREATE TABLE IF NOT EXISTS memory_completion_suggestions (
 id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL REFERENCES couples(id), user_id TEXT NOT NULL REFERENCES users(id),
 moment_id TEXT REFERENCES moments(id) ON DELETE CASCADE, trip_id TEXT REFERENCES trips(id) ON DELETE CASCADE,
 suggestion_kind TEXT NOT NULL CHECK(suggestion_kind IN ('missing_text','missing_location','missing_partner_description','provisional_date')),
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','snoozed','dismissed','completed')),
 remind_at TEXT, notified_at TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK((moment_id IS NOT NULL)+(trip_id IS NOT NULL)=1),
 UNIQUE(moment_id,user_id,suggestion_kind), UNIQUE(trip_id,user_id,suggestion_kind)
);
CREATE INDEX IF NOT EXISTS idx_memory_completion_owner ON memory_completion_suggestions(couple_id,user_id,status,remind_at);
CREATE TABLE IF NOT EXISTS voice_transcripts (
 id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL REFERENCES couples(id), owner_id TEXT NOT NULL REFERENCES users(id),
 media_id TEXT NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
 moment_id TEXT REFERENCES moments(id) ON DELETE SET NULL,
 body TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT 'zh-CN',
 method TEXT NOT NULL CHECK(method IN ('manual','browser','provider','legacy_unknown')),
 source_type TEXT NOT NULL CHECK(source_type IN ('human','real_api','rules','ai','mock','cache','legacy_unknown')),
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','ready','failed')),
 visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','shared','delayed')),
 reveal_at TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(media_id,owner_id)
);
CREATE INDEX IF NOT EXISTS idx_voice_transcripts_owner ON voice_transcripts(couple_id,owner_id,visibility,status,updated_at DESC);
CREATE TABLE IF NOT EXISTS future_letters (
 id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL REFERENCES couples(id), author_id TEXT NOT NULL REFERENCES users(id),
 recipient_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','sealed','delivered','deleted')),
 deliver_at TEXT, sealed_at TEXT, delivered_at TEXT, deleted_at TEXT,
 delivered_moment_id TEXT REFERENCES moments(id) ON DELETE SET NULL,
 client_request_id TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(author_id <> recipient_id),
 CHECK(status <> 'sealed' OR (deliver_at IS NOT NULL AND sealed_at IS NOT NULL)),
 CHECK(status <> 'delivered' OR delivered_at IS NOT NULL),
 UNIQUE(couple_id,author_id,client_request_id)
);
CREATE INDEX IF NOT EXISTS idx_future_letters_author ON future_letters(author_id,status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_future_letters_due ON future_letters(deliver_at,id) WHERE status='sealed';
CREATE INDEX IF NOT EXISTS idx_future_letters_shared ON future_letters(couple_id,delivered_at DESC) WHERE status='delivered';
CREATE TABLE IF NOT EXISTS album_export_jobs (
 id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL REFERENCES couples(id), creator_id TEXT NOT NULL REFERENCES users(id),
 template TEXT NOT NULL CHECK(template IN ('year','trip')), year INTEGER CHECK(year IS NULL OR year BETWEEN 1 AND 9999),
 trip_id TEXT REFERENCES trips(id) ON DELETE CASCADE,
 audience TEXT NOT NULL DEFAULT 'personal' CHECK(audience IN ('personal','shared')),
 selection_json TEXT NOT NULL CHECK(json_valid(selection_json) AND json_type(selection_json)='array'),
 expected_versions_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(expected_versions_json) AND json_type(expected_versions_json)='object'),
 status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','completed','failed','cancelled','expired')),
 artifact_relative_path TEXT, artifact_sha256 TEXT, size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes >= 0),
 failure_code TEXT, client_request_id TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
 CHECK((template='year' AND year IS NOT NULL AND trip_id IS NULL) OR (template='trip' AND trip_id IS NOT NULL AND year IS NULL)),
 CHECK(status<>'completed' OR (artifact_relative_path IS NOT NULL AND artifact_sha256 IS NOT NULL AND size_bytes IS NOT NULL AND completed_at IS NOT NULL)),
 UNIQUE(couple_id,creator_id,client_request_id)
);
CREATE INDEX IF NOT EXISTS idx_album_jobs_owner ON album_export_jobs(couple_id,creator_id,status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_album_jobs_expiry ON album_export_jobs(status,expires_at);
CREATE TABLE IF NOT EXISTS ai_usage_events (
 id TEXT PRIMARY KEY NOT NULL, couple_id TEXT REFERENCES couples(id), user_id TEXT REFERENCES users(id),
 actor_kind TEXT NOT NULL CHECK(actor_kind IN ('user','system')),
 feature TEXT NOT NULL CHECK(length(feature) BETWEEN 1 AND 100), provider TEXT NOT NULL, model TEXT NOT NULL,
 request_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt >= 1),
 status TEXT NOT NULL CHECK(status IN ('started','succeeded','failed','timeout','rejected','unknown')),
 request_sent INTEGER NOT NULL DEFAULT 0 CHECK(request_sent IN (0,1)),
 started_at TEXT NOT NULL, finished_at TEXT,
 latency_ms INTEGER CHECK(latency_ms IS NULL OR latency_ms >= 0),
 input_tokens INTEGER CHECK(input_tokens IS NULL OR (typeof(input_tokens)='integer' AND input_tokens >= 0)),
 output_tokens INTEGER CHECK(output_tokens IS NULL OR (typeof(output_tokens)='integer' AND output_tokens >= 0)),
 total_tokens INTEGER CHECK(total_tokens IS NULL OR (typeof(total_tokens)='integer' AND total_tokens >= 0)),
 estimated_cost REAL CHECK(estimated_cost IS NULL OR estimated_cost >= 0),
 cost_currency TEXT CHECK(cost_currency IS NULL OR (length(cost_currency)=3 AND cost_currency GLOB '[A-Z][A-Z][A-Z]')),
 pricing_version TEXT, failure_code TEXT,
 CHECK(actor_kind<>'user' OR (user_id IS NOT NULL AND couple_id IS NOT NULL)),
 CHECK(actor_kind<>'system' OR user_id IS NULL),
 CHECK(status<>'started' OR finished_at IS NULL),
 CHECK(status='started' OR finished_at IS NOT NULL),
 CHECK(status<>'rejected' OR request_sent=0),
 CHECK(status NOT IN ('succeeded','timeout') OR request_sent=1),
 CHECK(estimated_cost IS NULL OR (cost_currency IS NOT NULL AND pricing_version IS NOT NULL)),
 UNIQUE(request_id,attempt,provider,model)
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_owner ON ai_usage_events(couple_id,user_id,started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_usage_feature ON ai_usage_events(feature,status,started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_usage_unfinished ON ai_usage_events(started_at) WHERE status='started';
CREATE TABLE IF NOT EXISTS mutation_receipts (
 id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL REFERENCES couples(id), actor_id TEXT NOT NULL REFERENCES users(id),
 resource_type TEXT NOT NULL CHECK(resource_type IN ('calendar_entry','trip','media','shared_list_item','pantry_item')),
 resource_id TEXT NOT NULL,
 operation TEXT NOT NULL CHECK(operation IN ('calendar.complete','calendar.soft_delete','trip.soft_delete','media.trash','wishlist.complete','wishlist.soft_delete','pantry.set_state')),
 inverse_schema_version INTEGER NOT NULL DEFAULT 1 CHECK(inverse_schema_version=1),
 inverse_json TEXT NOT NULL CHECK(json_valid(inverse_json) AND json_type(inverse_json)='object'),
 expected_post_version INTEGER NOT NULL CHECK(expected_post_version >= 1),
 status TEXT NOT NULL DEFAULT 'available' CHECK(status IN ('available','undone','expired','invalidated')),
 resulting_version INTEGER CHECK(resulting_version IS NULL OR resulting_version >= 1),
 client_request_id TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL CHECK(expires_at > created_at), consumed_at TEXT,
 UNIQUE(couple_id,actor_id,operation,client_request_id),
 CHECK((resource_type='calendar_entry' AND operation IN ('calendar.complete','calendar.soft_delete')) OR
       (resource_type='trip' AND operation='trip.soft_delete') OR
       (resource_type='media' AND operation='media.trash') OR
       (resource_type='shared_list_item' AND operation IN ('wishlist.complete','wishlist.soft_delete')) OR
       (resource_type='pantry_item' AND operation='pantry.set_state')),
 CHECK(status<>'undone' OR (resulting_version IS NOT NULL AND consumed_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_mutation_receipts_owner ON mutation_receipts(couple_id,actor_id,status,expires_at);
CREATE INDEX IF NOT EXISTS idx_mutation_receipts_resource ON mutation_receipts(couple_id,resource_type,resource_id,expected_post_version);
"""


REQUIRED_TABLES_V33 = frozenset(
    """
    schema_migrations users sessions auth_failures push_subscriptions push_outbox
    couples invites messages call_records call_signals moments moment_reactions shared_comments
    trips trip_generation_jobs place_search_candidates trip_destination_places trip_preferences
    plan_variants trip_days itinerary_items itinerary_item_places route_cache hotel_options
    booking_links plan_revisions approvals shared_lists shared_list_items daily_answers audit_events
    system_events ui_manifests ui_generation_jobs ui_action_drafts ai_context_grants realtime_events
    media_assets system_metrics upload_sessions couple_statuses user_locations weekly_reviews
    weekly_review_answers date_plans date_plan_variants date_plan_votes date_plan_confirmations
    widget_snapshots mutation_idempotency chat_imports chat_import_items calendar_entries
    partner_preferences place_snapshots quote_snapshots trip_food_recommendations
    trip_food_review_evidence external_api_usage recipe_dishes recipes recipe_ingredients
    recipe_steps recipe_media recipe_media_checks meal_wishes meal_carts meal_cart_items
    shopping_lists shopping_list_items shopping_item_sources recipe_provider_cache recipe_drafts
    recipe_generation_jobs trip_assistant_messages trip_assistant_action_drafts trip_memory_drafts
    life_assistant_plans life_assistant_plan_items life_assistant_confirmations life_assistant_jobs
    in_app_notifications daily_couple_summaries couple_assistant_settings
    """.split()
)

V34_NEW_TABLES = frozenset(
    """
    decisions decision_options decision_votes decision_confirmations wishlist_claims
    ingredient_catalog food_profiles pantry_items shopping_category_preferences
    weekly_meal_plan_drafts cook_sessions cook_timers ingredient_substitutions
    trip_item_preferences trip_realism_checks trip_packing_items trip_emergency_entries trip_expenses
    memory_provenance memory_descriptions memory_completion_suggestions voice_transcripts
    future_letters album_export_jobs ai_usage_events mutation_receipts
    """.split()
)

REQUIRED_TABLES_V34 = REQUIRED_TABLES_V33 | V34_NEW_TABLES

FOUNDATION_LIFECYCLE_REGISTRY = {
    "trip_emergency_entries": {
        "export": "_export_trip_emergency_rows", "unlink": "_downgrade_trip_packing_rows",
        "account_delete": "_delete_trip_emergency_rows",
    },
    "voice_transcripts": {
        "export": "_export_voice_transcript_rows", "unlink": None,
        "account_delete": "_delete_voice_transcript_rows",
    },
    "future_letters": {
        "export": "_export_future_letter_rows", "unlink": "_unlink_future_letter_rows",
        "account_delete": "_delete_future_letter_rows",
    },
    "memory_descriptions": {
        "export": "_export_memory_description_rows", "unlink": None,
        "account_delete": "_delete_memory_description_rows",
    },
    "album_export_jobs": {
        "export": "_export_album_export_rows", "unlink": "_revoke_album_export_rows",
        "account_delete": "_revoke_album_export_rows",
    },
}


def _coverage_entry(table: str) -> dict[str, object]:
    """Return a conservative lifecycle classification, never row data."""
    operational = {
        "schema_migrations", "auth_failures", "system_events", "system_metrics",
        "audit_events", "realtime_events", "push_outbox", "mutation_idempotency",
        "ai_usage_events", "mutation_receipts",
    }
    secret = {
        "sessions", "push_subscriptions", "ai_context_grants", "decision_votes",
        "wishlist_claims", "trip_emergency_entries", "future_letters",
    }
    introduced = 34 if table in V34_NEW_TABLES else 33
    if table in operational:
        export_policy = "operational_excluded"
    elif table in secret:
        export_policy = "owner_filtered_redacted"
    else:
        export_policy = "scope_filtered"
    return {
        "introduced_in": introduced,
        "export_policy": export_policy,
        "unlink_policy": "revalidate_or_invalidate",
        "delete_policy": "scrub_private_retain_safe_history",
        "backup_policy": "full_database",
        "lifecycle_handlers": FOUNDATION_LIFECYCLE_REGISTRY.get(table),
    }


TABLE_LIFECYCLE_COVERAGE = {
    table: _coverage_entry(table) for table in sorted(REQUIRED_TABLES_V34)
}


ACCOUNT_EXPORT_POLICY_VERSION = 1
ACCOUNT_EXPORT_STRATEGIES = frozenset(
    {"user_filtered", "couple_filtered", "parent_filtered", "aggregate_only", "operational_excluded"}
)
_ACCOUNT_EXPORT_OPERATIONAL_EXCLUDED = frozenset(
    {
        "auth_failures", "push_subscriptions", "push_outbox", "call_signals",
        "system_events", "audit_events", "realtime_events", "system_metrics",
        "upload_sessions", "mutation_idempotency", "mutation_receipts",
    }
)
_ACCOUNT_EXPORT_AGGREGATE_ONLY = frozenset({"ai_usage_events", "external_api_usage"})
_ACCOUNT_EXPORT_USER_FILTERED = frozenset(
    {
        "users", "sessions", "invites", "user_locations", "chat_imports",
        "widget_snapshots", "ui_generation_jobs", "ui_action_drafts",
        "ai_context_grants", "in_app_notifications", "memory_completion_suggestions",
        "voice_transcripts", "future_letters", "album_export_jobs", "wishlist_claims",
        "ingredient_substitutions",
    }
)
_ACCOUNT_EXPORT_PARENT_FILTERED = frozenset(
    {
        "decision_options", "decision_votes", "decision_confirmations", "moment_reactions",
        "shared_comments", "trip_generation_jobs", "trip_destination_places", "trip_preferences",
        "plan_variants", "trip_days", "itinerary_items", "itinerary_item_places", "route_cache",
        "hotel_options", "booking_links", "plan_revisions", "approvals", "shared_list_items",
        "weekly_review_answers", "date_plan_variants", "date_plan_votes", "date_plan_confirmations",
        "chat_import_items", "trip_food_recommendations", "trip_food_review_evidence",
        "recipe_ingredients", "recipe_steps", "recipe_media", "recipe_media_checks",
        "meal_cart_items", "shopping_list_items", "shopping_item_sources", "cook_timers",
        "trip_item_preferences", "trip_realism_checks", "trip_packing_items",
        "trip_emergency_entries", "trip_expenses", "memory_provenance", "memory_descriptions",
        "life_assistant_plan_items", "life_assistant_confirmations",
    }
)


def _account_export_policy(table: str) -> dict[str, str]:
    if table in _ACCOUNT_EXPORT_OPERATIONAL_EXCLUDED:
        return {
            "strategy": "operational_excluded",
            "handler": "exclude_operational_data",
            "reason": "包含凭据、内部队列、审计或撤销实现数据，不属于用户可携带内容。",
        }
    if table in _ACCOUNT_EXPORT_AGGREGATE_ONLY:
        return {
            "strategy": "aggregate_only",
            "handler": "export_account_aggregates",
            "reason": "只导出本人可理解的聚合，不导出逐次运营记录。",
        }
    if table in _ACCOUNT_EXPORT_USER_FILTERED:
        return {
            "strategy": "user_filtered",
            "handler": "export_account_data",
            "reason": "按当前账号身份、资源权限和公开状态过滤。",
        }
    if table in _ACCOUNT_EXPORT_PARENT_FILTERED:
        return {
            "strategy": "parent_filtered",
            "handler": "export_account_data",
            "reason": "仅随已经通过权限检查的父资源导出。",
        }
    return {
        "strategy": "couple_filtered",
        "handler": "export_account_data",
        "reason": "仅导出当前有效情侣空间内双方可见或本人拥有的内容。",
    }


ACCOUNT_EXPORT_POLICIES = {
    table: _account_export_policy(table) for table in sorted(REQUIRED_TABLES_V34)
}


def account_export_policy_status() -> dict[str, object]:
    missing = sorted(REQUIRED_TABLES_V34 - set(ACCOUNT_EXPORT_POLICIES))
    unexpected = sorted(set(ACCOUNT_EXPORT_POLICIES) - REQUIRED_TABLES_V34)
    invalid = sorted(
        table for table, policy in ACCOUNT_EXPORT_POLICIES.items()
        if policy.get("strategy") not in ACCOUNT_EXPORT_STRATEGIES
        or not policy.get("handler") or not policy.get("reason")
    )
    by_strategy = {
        strategy: sum(1 for policy in ACCOUNT_EXPORT_POLICIES.values() if policy["strategy"] == strategy)
        for strategy in sorted(ACCOUNT_EXPORT_STRATEGIES)
    }
    return {
        "ok": not missing and not unexpected and not invalid,
        "version": ACCOUNT_EXPORT_POLICY_VERSION,
        "covered": len(ACCOUNT_EXPORT_POLICIES) - len(invalid),
        "required": len(REQUIRED_TABLES_V34),
        "missing": missing,
        "unexpected": unexpected,
        "invalid": invalid,
        "by_strategy": by_strategy,
    }


V34_REQUIRED_COLUMNS = {
    "ai_context_grants": frozenset(
        {"revoked_at", "last_used_at", "resource_version", "usage_scope", "request_id", "version"}
    ),
    "calendar_entries": frozenset(
        {"participant_a_id", "participant_b_id", "blocks_time", "source_type"}
    ),
    "shared_list_items": frozenset({"item_kind", "deleted_at", "created_at", "updated_at"}),
    "media_assets": frozenset({"captured_at", "date_source", "source_type", "duration_ms"}),
}

def _normalize_schema_sql(value: str | None) -> str:
    normalized = re.sub(r"\bIF\s+NOT\s+EXISTS\b", "", str(value or ""), flags=re.IGNORECASE)
    normalized = re.sub(r"[\s\"`\[\]]+", "", normalized).rstrip(";").lower()
    return normalized


_V34_SQL = SCHEMA_SQL.split(V34_SCHEMA_MARKER, 1)[1]
V34_TABLE_SQL_CONTRACT = {
    match.group(1): match.group(0).rstrip().rstrip(";")
    for match in re.finditer(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z0-9_]+)\s*\(.*?\)\s*;",
        _V34_SQL,
        flags=re.IGNORECASE | re.DOTALL,
    )
}
V34_INDEX_SQL_CONTRACT = {
    match.group(2): match.group(0).rstrip().rstrip(";")
    for match in re.finditer(
        r"CREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+([A-Za-z0-9_]+)\s+.*?;",
        _V34_SQL,
        flags=re.IGNORECASE | re.DOTALL,
    )
}
V34_INDEX_SQL_CONTRACT.update(
    {
        "idx_ai_grants_active": "CREATE INDEX idx_ai_grants_active ON ai_context_grants(couple_id,user_id,purpose,expires_at) WHERE revoked_at IS NULL",
        "idx_ai_grants_resource": "CREATE INDEX idx_ai_grants_resource ON ai_context_grants(couple_id,user_id,resource_type,resource_id)",
        "idx_calendar_busy": "CREATE INDEX idx_calendar_busy ON calendar_entries(couple_id,starts_at,ends_at) WHERE deleted_at IS NULL AND blocks_time=1",
        "idx_shared_list_items_active": "CREATE INDEX idx_shared_list_items_active ON shared_list_items(list_id,deleted_at,position)",
        "idx_media_owner_visibility": "CREATE INDEX idx_media_owner_visibility ON media_assets(couple_id,owner_id,visibility,status,created_at DESC)",
        "idx_ingredient_substitutions_meal": "CREATE INDEX idx_ingredient_substitutions_meal ON ingredient_substitutions(meal_item_id,status,created_at DESC) WHERE meal_item_id IS NOT NULL",
    }
)
V34_REQUIRED_INDEXES = frozenset(V34_INDEX_SQL_CONTRACT)


def _extract_check_contracts(value: str | None) -> tuple[str, ...]:
    sql = str(value or "")
    contracts: list[str] = []
    for match in re.finditer(r"\bCHECK\s*\(", sql, flags=re.IGNORECASE):
        start = match.end()
        depth = 1
        position = start
        while position < len(sql) and depth:
            if sql[position] == "(":
                depth += 1
            elif sql[position] == ")":
                depth -= 1
            position += 1
        if depth == 0:
            contracts.append(_normalize_schema_sql(sql[start:position - 1]))
    # Constraint declaration order is not semantically relevant and guarded
    # ALTERs append checks at the end. Sorting gives a deterministic multiset:
    # duplicate checks remain visible while the approved same-v34 additive
    # substitution migration converges to the same contract.
    return tuple(sorted(contracts))


def _extract_unique_contracts(value: str | None) -> tuple[tuple[str, str], ...]:
    """Read UNIQUE columns and conflict semantics without losing duplicates."""
    sql = str(value or "")
    contracts: list[tuple[str, str]] = []
    for match in re.finditer(r"\bUNIQUE\s*\(", sql, flags=re.IGNORECASE):
        start = match.end()
        depth = 1
        position = start
        while position < len(sql) and depth:
            if sql[position] == "(":
                depth += 1
            elif sql[position] == ")":
                depth -= 1
            position += 1
        if depth == 0:
            conflict = re.match(
                r"\s*ON\s+CONFLICT\s+(ROLLBACK|ABORT|FAIL|IGNORE|REPLACE)\b",
                sql[position:],
                flags=re.IGNORECASE,
            )
            # SQLite's omitted table-UNIQUE policy is ABORT. Explicit ABORT is
            # therefore accepted as the same contract, while every other
            # strategy changes write behavior and must remain observable.
            policy = str(conflict.group(1) if conflict else "ABORT").lower()
            contracts.append((_normalize_schema_sql(sql[start:position - 1]), policy))
    return tuple(sorted(contracts))


def _foreign_key_contract(rows) -> tuple[tuple[object, ...], ...]:
    """Keep SQLite FK group ids, composite sequence and duplicate rows exact."""
    return tuple(
        (
            int(row[0]), int(row[1]), str(row[3]), str(row[2]), str(row[4]),
            str(row[5]), str(row[6]), str(row[7]),
        )
        for row in sorted(rows, key=lambda value: (int(value[0]), int(value[1])))
    )


def _build_v34_pragma_contracts() -> tuple[
    dict[str, frozenset[str]],
    dict[str, tuple[tuple[object, ...], ...]],
    dict[str, dict[str, tuple[str, int, str, int, int]]],
    dict[str, tuple[tuple[str, str], ...]],
    dict[str, tuple[str, ...]],
]:
    columns: dict[str, frozenset[str]] = {}
    foreign_keys: dict[str, tuple[tuple[object, ...], ...]] = {}
    column_specs: dict[str, dict[str, tuple[str, int, str, int, int]]] = {}
    unique_constraints: dict[str, tuple[tuple[str, str], ...]] = {}
    checks: dict[str, tuple[str, ...]] = {}
    canonical = sqlite3.connect(":memory:")
    canonical.row_factory = sqlite3.Row
    try:
        for table, statement in V34_TABLE_SQL_CONTRACT.items():
            canonical.execute(statement)
            table_info = canonical.execute(f"PRAGMA table_xinfo({table})").fetchall()
            columns[table] = frozenset(str(row["name"]) for row in table_info)
            column_specs[table] = {
                str(row["name"]): (
                    str(row["type"] or "").upper(), int(row["notnull"]),
                    _normalize_schema_sql(row["dflt_value"]), int(row["pk"]), int(row["hidden"]),
                )
                for row in table_info
            }
            foreign_keys[table] = _foreign_key_contract(
                canonical.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            )
            unique_constraints[table] = _extract_unique_contracts(statement)
            checks[table] = _extract_check_contracts(statement)
    finally:
        canonical.close()
    return columns, foreign_keys, column_specs, unique_constraints, checks


(
    V34_TABLE_COLUMNS,
    V34_TABLE_FOREIGN_KEYS,
    V34_TABLE_COLUMN_SPECS,
    V34_TABLE_UNIQUE_CONSTRAINTS,
    V34_TABLE_CHECKS,
) = _build_v34_pragma_contracts()


def schema_inventory(db, *, expected_version: int | None = None) -> dict[str, object]:
    """Inspect the database without changing it or trusting the module version."""
    actual_version = int(db.execute("PRAGMA user_version").fetchone()[0])
    target_version = actual_version if expected_version is None else int(expected_version)
    required = REQUIRED_TABLES_V34 if target_version >= 34 else REQUIRED_TABLES_V33
    actual = {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    missing_tables = sorted(required - actual)
    uncovered_tables = sorted(actual - set(TABLE_LIFECYCLE_COVERAGE))
    missing_columns: dict[str, list[str]] = {}
    missing_foreign_keys: dict[str, list[list[str]]] = {}
    malformed_constraints: list[str] = []
    missing_indexes: list[str] = []
    malformed_indexes: list[str] = []
    unexpected_indexes: list[str] = []
    if target_version >= 34:
        column_contracts = {**V34_TABLE_COLUMNS, **V34_REQUIRED_COLUMNS}
        for table, expected in column_contracts.items():
            actual_columns = {
                str(row[1]) for row in db.execute(f"PRAGMA table_xinfo({table})").fetchall()
            }
            if expected - actual_columns:
                missing_columns[table] = sorted(expected - actual_columns)
        for table, expected in V34_TABLE_FOREIGN_KEYS.items():
            actual_foreign_keys = _foreign_key_contract(
                db.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            )
            unmatched = list(actual_foreign_keys)
            missing = []
            for value in expected:
                if value in unmatched:
                    unmatched.remove(value)
                else:
                    missing.append(value)
            if missing:
                missing_foreign_keys[table] = [list(value) for value in missing]
        actual_table_sql = {
            str(row[0]): str(row[1] or "")
            for row in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        for table in sorted(V34_TABLE_SQL_CONTRACT):
            if table not in actual_table_sql:
                continue
            actual_info = db.execute(f"PRAGMA table_xinfo({table})").fetchall()
            actual_specs = {
                str(row[1]): (
                    str(row[2] or "").upper(), int(row[3]),
                    _normalize_schema_sql(row[4]), int(row[5]), int(row[6]),
                )
                for row in actual_info
            }
            actual_foreign_keys = _foreign_key_contract(
                db.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            )
            actual_uniques = _extract_unique_contracts(actual_table_sql[table])
            actual_checks = _extract_check_contracts(actual_table_sql[table])
            if (
                actual_specs != V34_TABLE_COLUMN_SPECS[table]
                or actual_foreign_keys != V34_TABLE_FOREIGN_KEYS[table]
                or actual_uniques != V34_TABLE_UNIQUE_CONSTRAINTS[table]
                or actual_checks != V34_TABLE_CHECKS[table]
            ):
                malformed_constraints.append(table)
        actual_index_sql = {
            str(row[0]): str(row[1] or "")
            for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index'").fetchall()
        }
        missing_indexes = sorted(V34_REQUIRED_INDEXES - set(actual_index_sql))
        malformed_indexes = sorted(
            name for name, expected_sql in V34_INDEX_SQL_CONTRACT.items()
            if name in actual_index_sql
            and _normalize_schema_sql(actual_index_sql[name]) != _normalize_schema_sql(expected_sql)
        )
        unexpected_indexes = sorted(
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
                f"AND tbl_name IN ({','.join('?' for _ in V34_TABLE_SQL_CONTRACT)})",
                tuple(V34_TABLE_SQL_CONTRACT),
            ).fetchall()
            if str(row[0]) not in V34_INDEX_SQL_CONTRACT
        )
    structural_ok = not (
        missing_tables or uncovered_tables or missing_columns or missing_foreign_keys
        or malformed_constraints or missing_indexes or malformed_indexes or unexpected_indexes
    )
    return {
        "actual_version": actual_version,
        "expected_version": SCHEMA_VERSION,
        "upgrade_required": actual_version < SCHEMA_VERSION,
        "unsupported_newer": actual_version > SCHEMA_VERSION,
        "missing_tables": missing_tables,
        "uncovered_tables": uncovered_tables,
        "missing_columns": missing_columns,
        "missing_foreign_keys": missing_foreign_keys,
        "malformed_constraints": malformed_constraints,
        "missing_indexes": missing_indexes,
        "malformed_indexes": malformed_indexes,
        "unexpected_indexes": unexpected_indexes,
        "structural_ok": structural_ok,
        "ok": structural_ok and actual_version == target_version and actual_version <= SCHEMA_VERSION,
    }
