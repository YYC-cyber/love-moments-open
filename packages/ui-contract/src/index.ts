import { z } from "zod";

export const surfaces = ["home", "chat", "trip", "timeline", "life"] as const;
export const componentTypes = [
  "couple_overview", "anniversary", "trip_countdown", "pending_items", "daily_question", "quick_capture",
  "message_summary", "trip_share", "favorite_memory", "suggested_reply", "import_guide",
  "trip_variant_compare", "itinerary_timeline", "budget_summary", "hotel_quote", "risk_alert", "impact_diff", "approval_state",
  "featured_memory", "timeline_summary", "month_album", "footprint_summary", "year_report", "privacy_status",
  "wishlist", "calendar_summary", "todo_summary", "preference_summary",
  "recipe_library", "recipe_detail", "meal_wishlist", "meal_cart", "meal_calendar", "meal_day_detail", "meal_archive", "shopping_list"
] as const;
export const actionTypes = ["navigate", "send_message", "select_trip_variant", "create_moment", "create_calendar_entry", "create_wishlist_item"] as const;

export const uiActionSchema = z.object({
  id: z.string().min(1).max(120),
  type: z.enum(actionTypes),
  requiresConfirmation: z.boolean(),
  expectedVersions: z.record(z.string(), z.number().int().nonnegative())
}).superRefine((action, context) => {
  if (action.type !== "navigate" && !action.requiresConfirmation) context.addIssue({ code: "custom", message: "写操作必须确认" });
});

export const uiNodeSchema = z.object({
  id: z.string().regex(/^[a-z0-9:_-]{1,80}$/),
  type: z.enum(componentTypes),
  resourceRef: z.string().nullable().optional(),
  variant: z.string().nullable().optional(),
  props: z.record(z.string(), z.unknown()),
  actions: z.array(uiActionSchema).max(6)
});

export const uiManifestSchema = z.object({
  schemaVersion: z.literal("1.0"),
  surface: z.enum(surfaces),
  contextVersion: z.number().int().nonnegative(),
  sharedManifestId: z.string().min(1),
  sections: z.array(uiNodeSchema).max(24),
  personalOverlay: z.array(uiNodeSchema).max(12),
  generatedBy: z.enum(["rules", "ai", "cache"]),
  expiresAt: z.string()
}).superRefine((manifest, context) => {
  if (manifest.sections.length + manifest.personalOverlay.length > 24) context.addIssue({ code: "custom", message: "节点总数不能超过 24" });
});

export type Surface = typeof surfaces[number];
export type UiAction = z.infer<typeof uiActionSchema>;
export type UiNode = z.infer<typeof uiNodeSchema>;
export type UiManifest = z.infer<typeof uiManifestSchema>;
