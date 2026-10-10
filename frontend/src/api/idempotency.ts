export function newIdempotencyKey(): string {
  // getRandomValues is available on HTTP hosts as well as secure origins.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join(
    "",
  );
}
