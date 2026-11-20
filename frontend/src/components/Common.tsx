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

// Reuses one <img> while its source changes, so the browser keeps the previous
// picture on screen until the next one is ready. Known page dimensions reserve
// the final box before loading, which keeps the page height and scroll stable.
// `frameRatio` (height / width) fixes one frame for every page of a work so
// portrait and landscape pages share the same height and the controls stay put.
export function ViewerImage({
  src,
  alt,
  width,
  height,
  frameRatio,
}: {
  src: string | null;
  alt: string;
  width?: number | null;
  height?: number | null;
  frameRatio?: number;
}) {
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const [loadedSrc, setLoadedSrc] = useState<string | null>(null);
  const frame = frameRatio ? { aspectRatio: `1 / ${frameRatio}` } : undefined;
  if (!src || failedSrc === src)
    return (
      <div
        className={
          frame ? "image-fallback viewer-image-fallback" : "image-fallback"
        }
        style={frame}
        role="img"
        aria-label={ko.imageUnavailable}
      >
        <ImageOff size={30} />
        <span>{ko.noPreview}</span>
      </div>
    );
  const sized = Boolean(width && height && width > 0 && height > 0);
  return (
    <>
      <img
        src={src}
        alt={alt}
        width={sized ? (width ?? undefined) : undefined}
        height={sized ? (height ?? undefined) : undefined}
        style={frame}
        decoding="async"
        onLoad={() => setLoadedSrc(src)}
        onError={() => setFailedSrc(src)}
      />
      {loadedSrc !== src && (
        <span className="viewer-image-loading" role="status">
          <Spinner size="sm" aria-label={ko.loading} />
        </span>
      )}
    </>
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
