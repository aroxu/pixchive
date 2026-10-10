import { Button } from "@heroui/react";
import { ArrowRight } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { PageHeading } from "../components/Common";
import { PixivCollectionCards } from "../components/PixivCollectionCards";
import { DownloadPolicyCard, StorageCard } from "../components/SettingsCards";
import { ko } from "../i18n/ko";

export function SetupPage() {
  const navigate = useNavigate();
  const [policyReady, setPolicyReady] = useState(false);
  return (
    <>
      <PageHeading
        title={ko.setupGuide}
        description={ko.setupGuideDescription}
        action={
          <Button variant="secondary" onPress={() => navigate("/library")}>
            {ko.library}
            <ArrowRight size={16} />
          </Button>
        }
      />
      <p className="notice">{ko.supported}</p>
      <p className="section-description">{ko.setupLater}</p>
      <div className="settings-primary">
        <StorageCard title={ko.setupStorage} />
        <PixivCollectionCards
          onboarding
          syncDisabled={!policyReady}
          beforeSync={
            <DownloadPolicyCard
              title={ko.setupDownloadPolicy}
              onReadinessChange={setPolicyReady}
            />
          }
        />
      </div>
    </>
  );
}
