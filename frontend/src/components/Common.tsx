import { Button, Spinner } from "@heroui/react";
import { ImageOff, RefreshCw } from "lucide-react";
import { useState } from "react";
import { ko } from "../i18n/ko";

export function Loading() {
  return (
    <div className="loading" role="status">
      <Spinner size="sm" />
      {ko.loading}
    </div>
  );
}
export function ErrorState({
  error,
  retry,
}: {
  error: Error;
  retry?: () => void;
}) {
  return (
    <div className="error-box" role="alert">
      <p>{error.message}</p>
      {retry && (
        <Button variant="secondary" onPress={retry}>
          <RefreshCw size={16} />
          {ko.retry}
        </Button>
      )}
    </div>
  );
}
export function LocalImage({
  src,
  alt,
  className = "",
}: {
  src: string | null;
  alt: string;
  className?: string;
}) {
  return (
    <LocalImageSource key={src} src={src} alt={alt} className={className} />
  );
}

function LocalImageSource({
  src,
  alt,
  className = "",
}: {
  src: string | null;
  alt: string;
  className?: string;
}) {
  const [failed, setFailed] = useState(false);
  return src && !failed ? (
    <img
      className={className}
      src={src}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  ) : (
    <div
      className={`image-fallback ${className}`}
      role="img"
      aria-label={ko.imageUnavailable}
    >
      <ImageOff size={30} />
      <span>{ko.noPreview}</span>
    </div>
  );
}
export function PageHeading({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow?: string;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <header className="page-heading">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </header>
  );
}
