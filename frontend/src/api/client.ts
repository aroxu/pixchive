import type { components } from "./schema";
import { ko } from "../i18n/ko";

export { newIdempotencyKey } from "./idempotency";

export type Models = components["schemas"];
let csrfToken: string | null = null;
export function setCsrf(token: string | null) {
  csrfToken = token;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
        ...options.headers,
      },
    });
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw new ApiError(0, ko.connectionFailed);
  }
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    if (response.status === 401 && path !== "/session")
      window.dispatchEvent(new Event("session-expired"));
    throw new ApiError(
      response.status,
      typeof error.message === "string" ? error.message : ko.requestFailed,
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

export const body = (value: unknown) => JSON.stringify(value);
