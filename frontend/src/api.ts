/**
 * Centralised API client for the PackTrack frontend.
 *
 * All HTTP requests to the backend should go through `apiRequest` or
 * `fetchImageBlob`. Auth headers, idempotency keys, same-origin safety,
 * and error extraction are handled here.
 */

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
export const API_BASE = (
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1"
).replace(/\/+$/, "");

export const API_ORIGIN = API_BASE.replace(/\/api\/v1\/?$/, "");

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
export type AuthContext = {
  userId: string;
  tenantId: string;
};

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function buildHeaders(auth: AuthContext, includeJson: boolean = true): HeadersInit {
  const headers: Record<string, string> = {
    "X-User-Id": auth.userId,
    "X-Tenant-Id": auth.tenantId,
  };
  if (includeJson) {
    headers["Content-Type"] = "application/json";
  }
  return headers;
}

export function resolveApiPath(path: string): string {
  if (path.startsWith("http://") || path.startsWith("https://")) {
    return path;
  }
  if (path.startsWith("/api/")) {
    return `${API_ORIGIN}${path}`;
  }
  const relativePath = path.startsWith("/") ? path.slice(1) : path;
  return `${API_BASE}/${relativePath}`;
}

function getErrorMessage(res: unknown): string | null {
  if (typeof res === "string") {
    return res;
  }
  if (!isRecord(res)) {
    return null;
  }
  if ("detail" in res && typeof res.detail === "string") {
    return res.detail;
  }
  if ("message" in res && typeof res.message === "string") {
    return res.message;
  }
  if ("error" in res && typeof res.error === "string") {
    return res.error;
  }
  return null;
}

export function extractErrorMessage(payload: unknown): string {
  return getErrorMessage(payload) ?? "Request failed";
}

function createIdempotencyKey(path: string): string {
  const pathSlug = path
    .replace(/[^a-zA-Z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase()
    .slice(0, 40);
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${pathSlug || "request"}-${crypto.randomUUID()}`;
  }
  return `${pathSlug || "request"}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function requiresIdempotencyKey(path: string, method: string): boolean {
  if (method !== "POST") {
    return false;
  }
  const normalized = path.split("?")[0];
  if (normalized === "/batches/upload-zip/presign") {
    return true;
  }
  if (normalized === "/batches") {
    return true;
  }
  if (/^\/batches\/[^/]+\/finalise-zip$/.test(normalized)) {
    return true;
  }
  if (/^\/batches\/[^/]+\/finalise$/.test(normalized)) {
    return true;
  }
  if (/^\/batches\/[^/]+\/run$/.test(normalized)) {
    return true;
  }
  if (/^\/batches\/[^/]+\/reports\/export$/.test(normalized)) {
    return true;
  }
  if (normalized === "/documents/upload/finalise") {
    return true;
  }
  if (normalized.startsWith("/pipeline/run/")) {
    return true;
  }
  return /^\/reports\/[^/]+\/export$/.test(normalized);
}

// ---------------------------------------------------------------------------
// Public API functions
// ---------------------------------------------------------------------------
export async function apiRequest<T>(
  path: string,
  auth: AuthContext,
  init?: RequestInit,
): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const headers = new Headers(buildHeaders(auth, !(init?.body instanceof FormData)));
  if (init?.headers) {
    new Headers(init.headers).forEach((value, key) => headers.set(key, value));
  }
  if (requiresIdempotencyKey(path, method) && !headers.has("Idempotency-Key")) {
    headers.set("Idempotency-Key", createIdempotencyKey(path));
  }

  const response = await fetch(resolveApiPath(path), {
    ...init,
    method,
    headers,
  });

  const contentType = response.headers.get("content-type") ?? "";
  let payload: unknown = null;
  if (contentType.includes("application/json")) {
    payload = await response.json();
  } else {
    payload = await response.text();
  }

  if (!response.ok) {
    throw new Error(extractErrorMessage(payload));
  }

  return payload as T;
}

export async function fetchImageBlob(
  path: string,
  auth: AuthContext,
  signal?: AbortSignal,
): Promise<string> {
  const resolved = resolveApiPath(path);
  // Only send auth headers to same-origin URLs to avoid leaking tenant info.
  const isSameOrigin =
    resolved.startsWith("/") || resolved.startsWith(API_ORIGIN);
  const headers = isSameOrigin ? buildHeaders(auth, false) : {};
  const response = await fetch(resolved, { headers, signal });
  if (!response.ok) {
    throw new Error(`Image fetch failed (${response.status})`);
  }
  const blob = await response.blob();
  return URL.createObjectURL(blob);
}
