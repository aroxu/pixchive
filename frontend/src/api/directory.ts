export function directoryOrigin(origin: unknown, kind: "artists" | "tags") {
  const fallback = `/${kind}`;
  if (typeof origin !== "string") return fallback;
  const [path] = origin.split("?", 1);
  return path === fallback ? origin : fallback;
}
