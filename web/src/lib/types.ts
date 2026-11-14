// Shapes returned by the FastAPI routers (server/routers/*.py). Only the fields
// the UI reads are typed; everything is optional where the server may omit it.

export interface Yoe {
  min: number | null;
  max: number | null;
  preferred: boolean;
  evidence: string | null;
}

export interface JobCard {
  key: string;
  title: string | null;
  company: string | null;
  tier: string | null;
  lane: string | null;
  status: string;
  tracker_status: string;
  location: string | null;
  workplace_type: string | null;
  country_bucket: string | null;
  source: string | null;
  attribution_name: string | null;
  attribution_url: string | null;
  url: string | null;
  apply_url: string | null;
  age_days: number | null;
  freshness: string | null;
  posting_gone: boolean;
  fit_score: number | null;
  fit_components: Record<string, number> | null;
  why: string | null;
  matched_skills: string[];
  missing_skills: string[];
  yoe: Yoe;
  fresher_friendly: boolean;
  india_eligible: boolean | null;
  comp_summary?: string | null;
  tz_requirement?: string | null;
  timezone_restrictions?: string[];
  region_restrictions?: string[];
  flags: string[];
  reasons?: { rule: string; detail: string; excerpt?: string }[];
  // tracker extras
  status_changed_at?: number | null;
  days_in_status?: number | null;
  submitted_at?: number | null;
  submit_source?: string | null;
  submit_proof_path?: string | null;
  dates?: Record<string, number>;
  warning?: string | null;
  days_waiting?: number | null;
  reminder_due?: boolean;
  days_since_submit?: number | null;
  followup_due_at?: number | null;
}

export interface QueueResponse {
  day: string | null;
  lane: string;
  items: JobCard[];
  count: number;
}

export interface Paged<T> {
  total: number;
  limit: number;
  offset: number;
  items: T[];
  by_rule?: Record<string, number>;
}

export interface JobDetail extends Record<string, unknown> {
  key: string;
  title: string;
  company: string;
  description_text: string | null;
  location_text: string | null;
  workplace_type: string | null;
  url: string | null;
  apply_url: string | null;
  status: string;
  department: string | null;
  comp_summary: string | null;
  tz_requirement_text: string | null;
  artifact_id: number | null;
  fill_status: string | null;
  submitted_at: number | null;
  submit_proof_path: string | null;
  sources: { source: string; url: string | null }[];
}

export interface TrackerColumn {
  status: string;
  count: number;
  items: JobCard[];
}

export interface Cooldown {
  company: string;
  key: string;
  title: string;
  rejected_at: number;
  reapply_after: number;
  days_left: number;
  cooldown_days: number;
}

export interface TrackerResponse {
  columns: TrackerColumn[];
  ready_to_submit: JobCard[];
  followups_due: JobCard[];
  cooldowns: Cooldown[];
}

export interface RunStep {
  name: string;
  status: string;
  seconds?: number;
  result?: unknown;
  error?: string;
}

export interface Run {
  id: number;
  kind: string;
  started_at: number;
  finished_at: number | null;
  status: string;
  error?: string | null;
  summary?: { mode?: string; steps?: RunStep[]; [k: string]: unknown } | null;
}

export interface ArtifactSummary {
  id: number;
  job_key: string;
  version: number;
  base: string | null;
  status: string;
  pages: number | null;
  created_at: number | null;
  approved_at: number | null;
  grounding_clean: boolean | null;
  pdf_url: string | null;
}

export interface Line {
  section: string;
  kind: string;
  fact_ids: string[];
  text: string;
  rephrased: boolean;
  source_text?: string;
  heading?: string;
  kept_original?: string;
}

export interface DiffRow {
  fact_ids: string[];
  section: string;
  op: "same" | "rephrased" | "moved" | "added" | "removed";
  before: string | null;
  after: string | null;
  note?: string;
}

export interface LineVerdict {
  index: number;
  text: string;
  fact_ids: string[];
  supported: boolean;
  offending: string[];
  [k: string]: unknown;
}

export interface Grounding {
  clean: boolean;
  lines: LineVerdict[];
  flags: Record<string, string[]>;
  unsupported_lines: number[];
}

export interface ArtifactReview {
  artifact_id: number;
  job_key: string;
  version: number;
  base: string | null;
  status: string;
  pages: number | null;
  created_at: number | null;
  approved_at: number | null;
  grounding: Grounding | null;
  lines: Line[];
  diff: DiffRow[];
  stale: boolean;
  problem: string | null;
  pdf_url: string | null;
  kept_original: { fact_id: string; reason: string }[];
  user_edited: string[];
  editable?: string[];
  keywords: { surfaced: string[]; attested: string[]; struck: string[] };
}

export interface FillAction {
  label: string;
  kind: string;
  source: string;
  confidence: number;
  needs_user: boolean;
  approved?: boolean;
  required?: boolean;
  note?: string;
}

export interface FillPlanRow {
  job_key: string;
  ats: string | null;
  url: string | null;
  unresolved_count: number;
  created_at: number;
  stale_after: number | null;
  plan: {
    steps: FillAction[][];
    unresolved: { label: string; reason?: string; required?: boolean }[];
    file_kinds?: string[];
    target_url?: string;
  } | null;
}

export interface Outcome {
  status?: string;
  reason?: string;
  code?: string;
  filled?: number;
  left_empty?: number;
  highlighted?: string[];
  submit_selector?: string | null;
  step_reached?: string | null;
}

export interface AutofillSession {
  id: number;
  job_key: string;
  status: string;
  started_at: number;
  finished_at: number | null;
  outcome: Outcome | null;
  open: boolean;
  live?: boolean;
  has_screenshot?: boolean;
  screenshot_path?: string | null;
}

export interface UnansweredQuestion {
  question: string;
  field_type: string;
  options: string[];
  required: boolean;
  reasons: string[];
  jobs: { key: string; title: string; company: string }[];
}

export interface FactRow {
  id: string;
  section: string;
  kind: string;
  parent: string | null;
  text: string;
  status: string;
  sources: string[];
  tech: string[];
  pending_until: string | null;
  pending_reason: string | null;
  note: string | null;
  claims: { id: string; text: string; tech: string[]; status: string; pending_until: string | null }[];
}

export interface AnswerRow {
  path: string;
  value: unknown;
  status: string;
  empty: boolean;
  derived: boolean;
  note?: string;
}

export interface Draft {
  kind: string;
  status: string;
  text: string;
  warnings: string[];
  word_count: number;
  grounding: { clean?: boolean; flags?: Record<string, string[]> };
}

export interface LetterChecks {
  status: "ok" | "needs_review" | "refused";
  warnings: string[];
  word_count: number;
  max_words: number;
  grounding: { clean?: boolean; flags?: Record<string, string[]>; unsupported?: string[] };
}

// GET /api/jobs/{key}/cover-letter/saved (and the save / approve replies).
export interface CoverLetterState {
  saved: boolean;
  artifact_id: number | null;
  artifact_version?: number;
  artifact_status?: string;
  job_artifact_id?: number | null;
  text?: string;
  sha256?: string;
  saved_at?: number;
  checks?: LetterChecks;
  notes?: string[];
  pdf_url?: string | null;
  approved?: boolean;
  approved_at?: number | null;
  usable?: boolean;
  needs_confirmation?: boolean;
  earlier?: { artifact_version: number; text: string };
}

export interface UiConfig {
  demo: boolean;
  banner: string | null;
  autofill: boolean;
}
