/**
 * The only place this app talks to the backend.
 *
 * Two session tokens are kept, under separate keys: a student one and an admin
 * one. They are separate principals on the backend (separate tables, separate
 * dependencies), so collapsing them into a single "token" here would be the
 * frontend quietly reintroducing a distinction the API deliberately makes.
 *
 * Note what the student calls do NOT send: a student ID. Every personal request
 * goes to `/me/*` and the server reads the identity from the session. There is
 * no function in this file that takes a student ID, which is the client-side
 * half of the guarantee described in the backend's `auth.py`.
 */

const BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

const STUDENT_TOKEN_KEY = "eurisko.student.token";
const ADMIN_TOKEN_KEY = "eurisko.admin.token";

export type Role = "student" | "admin";

function tokenKey(role: Role): string {
  return role === "admin" ? ADMIN_TOKEN_KEY : STUDENT_TOKEN_KEY;
}

export function getToken(role: Role): string | null {
  return localStorage.getItem(tokenKey(role));
}

export function setToken(role: Role, token: string): void {
  localStorage.setItem(tokenKey(role), token);
}

export function clearToken(role: Role): void {
  localStorage.removeItem(tokenKey(role));
}

/** Thrown for any non-2xx response, carrying the status so callers can branch. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(
  path: string,
  options: { method?: string; body?: unknown; role?: Role; raw?: FormData } = {},
): Promise<T> {
  const { method = "GET", body, role, raw } = options;
  const headers: Record<string, string> = {};

  if (role) {
    const token = getToken(role);
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const response = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: raw ?? (body === undefined ? undefined : JSON.stringify(body)),
  });

  if (!response.ok) {
    // FastAPI puts the message in `detail`; a 422 puts a list there. Both are
    // shown to the user, because a silent failure in an admin panel is worse
    // than an ugly one.
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail =
        typeof payload.detail === "string"
          ? payload.detail
          : JSON.stringify(payload.detail ?? payload);
    } catch {
      /* body was not JSON; the status text will have to do */
    }
    throw new ApiError(response.status, detail);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// --- types mirroring the backend's Pydantic schemas -------------------------

export interface AcademicSummary {
  gpa: string | null;
  quality_points: string;
  gpa_credits: number;
  credits_earned: number;
  credits_in_progress: number;
}

export interface Profile {
  student_id: string;
  first_name: string;
  last_name: string;
  email: string;
  program_code: string;
  program_name: string;
  total_credits_required: number;
  entry_term: string;
  entry_term_name: string;
  expected_graduation_term: string;
  academic_status: string;
  advisor_name: string;
  scenario_note: string | null;
  academics: AcademicSummary;
}

export interface ScheduledClass {
  course_code: string;
  title: string;
  credits: number;
  days: string;
  start_time: string;
  end_time: string;
  room: string;
  instructor: string;
}

export interface Schedule {
  student_id: string;
  term_code: string;
  total_credits: number;
  classes: ScheduledClass[];
}

export interface CourseHistoryEntry {
  term_code: string;
  term_name: string;
  start_date: string;
  course_code: string;
  title: string;
  credits: number;
  grade: string | null;
  grade_points: string | null;
  earns_credit: boolean | null;
  included_in_gpa: boolean | null;
  status: string;
}

export interface CourseHistory {
  student_id: string;
  academics: AcademicSummary;
  courses: CourseHistoryEntry[];
}

export interface CategoryProgress {
  category_id: string;
  category_name: string;
  credits_required: number;
  selection_rule: "ALL" | "ANY_N";
  courses_required: number | null;
  min_grade_points: string | null;
  courses_offered: number;
  courses_counted: number;
  credits_counted: number;
  credits_applied: number;
  credits_remaining: number;
  is_satisfied: boolean;
  courses_in_progress: number;
  credits_in_progress: number;
}

export interface DegreeProgress {
  student_id: string;
  program_code: string;
  program_name: string;
  total_credits_required: number;
  credits_earned: number;
  credits_in_progress: number;
  categories: CategoryProgress[];
  all_categories_satisfied: boolean;
  unsatisfied_categories: string[];
}

export interface ChatResponse {
  conversation_id: number;
  reply: string;
  model_name: string;
  tool_calls: string[];
}

export interface Appointment {
  id: number;
  advisor_name: string;
  proposed_time: string;
  reason: string;
  status: string;
  confirmed_at: string;
  conversation_id: number | null;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_at: string;
  student_id: string | null;
}

export interface AssistantSettings {
  tone: string;
  model_name: string;
  response_length: string;
  temperature: string;
}

export interface DocumentStatus {
  filename: string;
  title: string;
  status: string;
  page_count: number | null;
  uploaded_at: string;
  chunk_count: number;
  embedded_count: number;
  error: string | null;
}

export interface IngestReport {
  filename: string;
  status: string;
  chunk_count: number;
  page_count: number | null;
  unchanged: boolean;
  error: string | null;
}

export interface BrowsePage {
  total: number;
  items: Record<string, unknown>[];
}

export interface FilterOptions {
  programs: string[];
  academic_statuses: string[];
  terms: string[];
  grades: string[];
  subjects: string[];
}

// --- student portal ---------------------------------------------------------

export const student = {
  login: (studentId: string) =>
    request<LoginResponse>("/auth/login", {
      method: "POST",
      body: { student_id: studentId },
    }),
  logout: () => request<void>("/auth/logout", { method: "POST", role: "student" }),
  whoami: () => request<{ student_id: string }>("/auth/me", { role: "student" }),

  profile: () => request<Profile>("/me/profile", { role: "student" }),
  schedule: () => request<Schedule>("/me/schedule", { role: "student" }),
  courses: () => request<CourseHistory>("/me/courses", { role: "student" }),
  degreeProgress: () =>
    request<DegreeProgress>("/me/degree-progress", { role: "student" }),
  appointments: () => request<Appointment[]>("/me/appointments", { role: "student" }),

  chat: (message: string, conversationId: number | null) =>
    request<ChatResponse>("/me/chat", {
      method: "POST",
      role: "student",
      body: { message, conversation_id: conversationId },
    }),
};

// --- admin panel ------------------------------------------------------------

export const admin = {
  login: (password: string) =>
    request<LoginResponse>("/admin/login", { method: "POST", body: { password } }),
  logout: () => request<void>("/admin/logout", { method: "POST", role: "admin" }),

  settings: () => request<AssistantSettings>("/admin/settings", { role: "admin" }),
  saveSettings: (settings: AssistantSettings) =>
    request<AssistantSettings>("/admin/settings", {
      method: "PUT",
      role: "admin",
      body: settings,
    }),

  documents: () => request<DocumentStatus[]>("/admin/documents", { role: "admin" }),
  reingest: (force: boolean) =>
    request<IngestReport[]>(`/admin/documents/reingest?force=${force}`, {
      method: "POST",
      role: "admin",
    }),
  replaceDocument: (filename: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<IngestReport>(`/admin/documents/${encodeURIComponent(filename)}`, {
      method: "POST",
      role: "admin",
      raw: form,
    });
  },

  filters: () => request<FilterOptions>("/admin/filters", { role: "admin" }),
  students: (query: Record<string, string>) =>
    request<BrowsePage>(`/admin/students?${new URLSearchParams(query)}`, {
      role: "admin",
    }),
  courses: (query: Record<string, string>) =>
    request<BrowsePage>(`/admin/courses?${new URLSearchParams(query)}`, {
      role: "admin",
    }),
  enrollments: (query: Record<string, string>) =>
    request<BrowsePage>(`/admin/enrollments?${new URLSearchParams(query)}`, {
      role: "admin",
    }),
};
