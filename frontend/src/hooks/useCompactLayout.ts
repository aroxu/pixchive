import { useCallback, useRef, useSyncExternalStore } from "react";

// Keep this threshold aligned with the mobile layout in styles/app.css.
const compactLayout = window.matchMedia("(max-width: 760px)");

export function useCompactLayout(beforeChange?: (compact: boolean) => void) {
  const handler = useRef(beforeChange);
  handler.current = beforeChange;
  const subscribe = useCallback((onChange: () => void) => {
    const listener = () => {
      handler.current?.(compactLayout.matches);
      onChange();
    };
    compactLayout.addEventListener("change", listener);
    return () => compactLayout.removeEventListener("change", listener);
  }, []);
  return useSyncExternalStore(
    subscribe,
    () => compactLayout.matches,
    () => false,
  );
}
