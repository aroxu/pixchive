import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye } from "lucide-react";
import { Link } from "react-router-dom";
import { api, body, type Models } from "../api/client";
import { ErrorState, Loading, PageHeading } from "../components/Common";
import {
  BackupCard,
  DownloadPolicyCard,
  ConversionCard,
  ExclusionsCard,
  ScheduleCard,
  StorageCard,
} from "../components/SettingsCards";
import { ko } from "../i18n/ko";
import { PixivCollectionCards } from "../components/PixivCollectionCards";

export function SettingsPage() {
  const client = useQueryClient();
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<Models["SettingsView"]>("/settings"),
  });
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api<Models["Account"][]>("/accounts"),
    refetchInterval: 3000,
  });
  const display = useMutation({
    mutationFn: (value: boolean) =>
      api<Models["SettingsView"]>("/settings", {
        method: "PATCH",
        body: body({ show_sensitive: value }),
      }),
    onSuccess: (data) => {
      client.setQueryData(["settings"], data);
      for (const key of ["artwork-history", "artwork-snapshot"])
        client.removeQueries({ queryKey: [key] });
      for (const key of [
        "artworks",
        "artwork",
        "artists",
        "tags",
        "neighbors",
        "artwork-files",
      ])
        void client.invalidateQueries({ queryKey: [key] });
    },
  });
  return (
    <>
      <PageHeading
        title={ko.settings}
        description={ko.settingsDescription}
        action={
          <Link className="text-link" to="/setup">
            {ko.setupGuideLink}
          </Link>
        }
      />
      <div className="settings-layout">
        <div className="settings-primary">
          <PixivCollectionCards />
          <ScheduleCard
            connected={accounts.data?.[0]?.status === "connected"}
          />
          <DownloadPolicyCard />
          <ConversionCard />
          <section className="settings-card">
            <div className="section-title">
              <Eye size={20} />
              <h2>{ko.display}</h2>
            </div>
            {settings.isPending ? (
              <Loading />
            ) : settings.error ? (
              <ErrorState
                error={settings.error}
                retry={() => void settings.refetch()}
              />
            ) : (
              <label className="toggle-row">
                <span>
                  {ko.showSensitive}
                  <small>{ko.displayHelp}</small>
                </span>
                <input
                  type="checkbox"
                  role="switch"
                  checked={settings.data.show_sensitive}
                  disabled={display.isPending}
                  onChange={(e) => display.mutate(e.target.checked)}
                />
              </label>
            )}
            {display.error && <ErrorState error={display.error} />}
          </section>
          <ExclusionsCard />
        </div>
        <div className="settings-secondary">
          <StorageCard />
          <BackupCard />
        </div>
      </div>
    </>
  );
}
