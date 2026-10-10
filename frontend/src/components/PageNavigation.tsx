import { Button } from "@heroui/react";
import { ko } from "../i18n/ko";

export function PageNavigation({
  page,
  pageSize,
  total,
  onChange,
  label,
}: {
  page: number;
  pageSize: number;
  total: number;
  onChange: (page: number) => void;
  label: string;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (pages <= 1) return null;
  return (
    <nav className="pagination compact" aria-label={label}>
      <Button
        size="sm"
        variant="secondary"
        isDisabled={page <= 1}
        onPress={() => onChange(page - 1)}
      >
        {ko.previous}
      </Button>
      <span aria-live="polite">{ko.pagePosition(page, pages)}</span>
      <Button
        size="sm"
        variant="secondary"
        isDisabled={page >= pages}
        onPress={() => onChange(page + 1)}
      >
        {ko.next}
      </Button>
    </nav>
  );
}
