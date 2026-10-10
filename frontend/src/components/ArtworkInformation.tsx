import { Disclosure } from "@heroui/react";
import { useRef, useState, type ReactNode } from "react";
import { useCompactLayout } from "../hooks/useCompactLayout";
import { ko } from "../i18n/ko";

export function ArtworkInformation({
  children,
}: {
  children: (active: boolean) => ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const trigger = () =>
    root.current?.querySelector<HTMLButtonElement>(
      '[data-slot="disclosure-trigger"]',
    );
  const compact = useCompactLayout((nextCompact) => {
    // Inspect focus before the responsive panel or its trigger becomes hidden.
    if (
      nextCompact &&
      (root.current?.querySelector("dialog[open]") ||
        (document.activeElement &&
          panel.current?.contains(document.activeElement)))
    )
      setExpanded(true);
    if (!nextCompact && document.activeElement === trigger())
      document.getElementById("artwork-title")?.focus({ preventScroll: true });
  });
  const active = !compact || expanded;

  return (
    <Disclosure
      ref={root}
      className="artwork-information"
      isExpanded={active}
      onExpandedChange={(value) => {
        if (!compact || (!value && root.current?.querySelector("dialog[open]")))
          return;
        if (
          !value &&
          document.activeElement &&
          panel.current?.contains(document.activeElement)
        )
          trigger()?.focus({ preventScroll: true });
        setExpanded(value);
      }}
    >
      <Disclosure.Heading className="artwork-information-heading" level={2}>
        <Disclosure.Trigger className="artwork-information-trigger">
          <span>
            {expanded ? ko.foldArtworkInformation : ko.openArtworkInformation}
          </span>
          <Disclosure.Indicator />
        </Disclosure.Trigger>
      </Disclosure.Heading>
      <Disclosure.Content
        ref={panel}
        className="artwork-information-content"
        inert={!active}
      >
        {children(active)}
      </Disclosure.Content>
    </Disclosure>
  );
}
