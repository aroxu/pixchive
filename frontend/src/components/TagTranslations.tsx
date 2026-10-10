import type { Models } from "../api/client";
import { ko } from "../i18n/ko";

export function TagTranslations({
  translations,
}: {
  translations: Models["TagTranslationView"][];
}) {
  if (!translations.length) return null;
  return (
    <ul className="tag-translations" aria-label={ko.tagTranslations}>
      {translations.map((translation) => (
        <li
          key={`${translation.language}-${translation.source}-${translation.name}`}
        >
          <span>{translation.name}</span>
          <small>
            {ko.tagTranslationInfo(
              ko.translationLanguages[translation.language] ??
                translation.language,
              ko.translationSources[translation.source] ?? translation.source,
            )}
          </small>
        </li>
      ))}
    </ul>
  );
}
