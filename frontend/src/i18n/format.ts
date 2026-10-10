const locale = "ko-KR";

export function formatBytes(value: number) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toLocaleString(locale, { maximumFractionDigits: unit < 2 ? 0 : 1 })} ${units[unit]}`;
}

export function formatDateTime(seconds: number | null | undefined) {
  return seconds ? new Date(seconds * 1000).toLocaleString(locale) : null;
}

export function formatCount(value: number) {
  return value.toLocaleString(locale);
}

export function formatRemoteDateTime(value: string | null | undefined) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleString(locale);
}
