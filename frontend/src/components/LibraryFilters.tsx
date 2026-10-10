import { Button, Input, Label, TextField } from "@heroui/react";
import { useState } from "react";
import { X } from "lucide-react";
import { clearFilters } from "../api/library";
import { ko } from "../i18n/ko";

export const filterOptions: Record<string, Record<string, string>> = {
  tag_source: ko.sources,
  tag_match: ko.tagMatches,
  kind: ko.kinds,
  rating: ko.ratings,
  visibility: { public: ko.scopes.public, private: ko.scopes.private },
  archive_status: ko.archiveStates,
  remote_status: ko.remoteStates,
  bookmark_status: ko.bookmarkStates,
};

function TagFilter({
  label,
  tags,
  onChange,
}: {
  label: string;
  tags: string[];
  onChange: (tags: string[]) => void;
}) {
  const [value, setValue] = useState("");
  const add = () => {
    if (value.trim() && !tags.includes(value.trim()) && tags.length < 20) {
      onChange([...tags, value.trim()]);
      setValue("");
    }
  };
  return (
    <div className="tag-filter">
      <TextField value={value} onChange={setValue}>
        <Label>{label}</Label>
        <Input
          maxLength={100}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) {
              e.preventDefault();
              add();
            }
          }}
        />
      </TextField>
      <Button
        size="sm"
        variant="secondary"
        aria-label={`${label} ${ko.add}`}
        isDisabled={!value.trim() || tags.length >= 20}
        onPress={add}
      >
        {ko.add}
      </Button>
      <div className="filter-chips">
        {tags.map((tag) => (
          <button
            type="button"
            key={tag}
            aria-label={`${label} ${tag} ${ko.remove}`}
            onClick={() => onChange(tags.filter((t) => t !== tag))}
          >
            {tag}
            <X size={12} />
          </button>
        ))}
      </div>
    </div>
  );
}

export function LibraryFilters({
  params,
  apply,
  lockedFields = [],
}: {
  params: URLSearchParams;
  apply: (next: URLSearchParams) => void;
  lockedFields?: string[];
}) {
  const [included, setIncluded] = useState(params.getAll("tag"));
  const [excluded, setExcluded] = useState(params.getAll("exclude_tag"));
  const [dateError, setDateError] = useState(false);
  const select = (key: string, options = filterOptions[key], all = true) => (
    <label className="filter-field" key={key}>
      {ko.filterLabels[key]}
      <select name={key} defaultValue={params.get(key) ?? (all ? "" : "all")}>
        {all && <option value="">{ko.all}</option>}
        {Object.entries(options).map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>
    </label>
  );
  return (
    <form
      className="library-filters"
      onSubmit={(e) => {
        e.preventDefault();
        const next = clearFilters(params);
        if (params.get("q")) next.set("q", params.get("q")!);
        for (const [key, value] of new FormData(e.currentTarget)) {
          if (typeof value === "string" && value) next.set(key, value);
        }
        for (const prefix of ["created", "collected"]) {
          const start = next.get(`${prefix}_from`),
            end = next.get(`${prefix}_to`);
          if (start && end && start > end) {
            setDateError(true);
            return;
          }
        }
        setDateError(false);
        included.forEach((tag) => next.append("tag", tag));
        excluded.forEach((tag) => next.append("exclude_tag", tag));
        if (!included.length) next.delete("tag_match");
        apply(next);
      }}
    >
      <TagFilter label={ko.tagInclude} tags={included} onChange={setIncluded} />
      <p className="field-help">{ko.tagHelp}</p>
      {select("tag_match", ko.tagMatches, false)}
      <TagFilter label={ko.tagExclude} tags={excluded} onChange={setExcluded} />
      {select("tag_source")}
      {!lockedFields.includes("artist") && (
        <TextField name="artist" defaultValue={params.get("artist") ?? ""}>
          <Label>{ko.filterLabels.artist}</Label>
          <Input inputMode="numeric" pattern="[0-9]{1,20}" maxLength={20} />
        </TextField>
      )}
      {select("kind")}
      {select("rating")}
      <p className="field-help">{ko.ratingHelp}</p>
      <details
        open={Boolean(
          [
            "visibility",
            "archive_status",
            "remote_status",
            "bookmark_status",
            "created_from",
            "created_to",
            "collected_from",
            "collected_to",
          ].some((key) => params.has(key)),
        )}
      >
        <summary>{ko.moreFilters}</summary>
        <div className="filter-extra">
          {[
            "visibility",
            "archive_status",
            "remote_status",
            "bookmark_status",
          ].map((key) => select(key))}
          {["created", "collected"].map((prefix) => (
            <div className="date-range" key={prefix}>
              {["from", "to"].map((suffix) => {
                const key = `${prefix}_${suffix}`;
                return (
                  <label className="filter-field" key={key}>
                    {ko.filterLabels[key]}
                    <input
                      type="date"
                      name={key}
                      defaultValue={params.get(key) ?? ""}
                    />
                  </label>
                );
              })}
            </div>
          ))}
          <p className="field-help">{ko.dateHelp}</p>
        </div>
      </details>
      {dateError && (
        <p className="status-warning" role="alert">
          {ko.dateError}
        </p>
      )}
      <div className="filter-actions">
        <Button type="submit">{ko.applyFilters}</Button>
        <Button variant="ghost" onPress={() => apply(clearFilters(params))}>
          {ko.clearFilters}
        </Button>
      </div>
    </form>
  );
}
