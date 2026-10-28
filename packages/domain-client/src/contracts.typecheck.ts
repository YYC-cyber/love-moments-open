import { sanitizeDecision, type Decision, type PlaceCandidate } from "./index";

// Compile-time gates for providers emitted by the Python place adapter.
const geoapifyCandidate: PlaceCandidate = {
  candidate_id: "candidate-1",
  provider: "geoapify",
  provider_place_id: "provider-1",
  name: "候选地点",
  formatted_address: "示例地址",
  country: "中国",
  province: "",
  city: "",
  district: "",
  category: "poi",
};
void geoapifyCandidate;

function assertOpenDecisionHasNoSecrets(openDecision: Extract<Decision, { status: "open" }>) {
  // @ts-expect-error 未揭晓分支在类型层不能读取秘密选票。
  void openDecision.votes;
  // @ts-expect-error 未揭晓分支在类型层不能读取最终结果。
  void openDecision.resolution;
}
void assertOpenDecisionHasNoSecrets;

// Runtime gate: clients call this before caching untrusted bootstrap JSON. The
// returned union intentionally has no secret keys when status is unrevealed.
function assertRuntimeSanitizerNarrowsSecrets() {
  const sanitized = sanitizeDecision({
    id: "decision-1",
    title: "周末吃什么",
    status: "open",
    selection_mode: "single",
    voting_round: 1,
    closes_at: "2026-10-31T12:00:00Z",
    options_locked: false,
    options: [{ id: "option-1", label: "火锅", position: 1 }, { id: "option-2", label: "烤肉", position: 2 }],
    submitted_by_me: false,
    partner_submitted: false,
    creator_id: "user-1",
    version: 1,
    created_at: "2026-10-27T00:00:00Z",
    updated_at: "2026-10-27T00:00:00Z",
    votes: [{ user_id: "partner", option_ids: ["option-1"] }],
    resolution: { kind: "match", option_ids: ["option-1"] },
  });
  if (sanitized?.status === "open") {
    // @ts-expect-error 运行时脱敏后的公开分支同样不可读取秘密字段。
    void sanitized.votes;
  }
}
void assertRuntimeSanitizerNarrowsSecrets;
