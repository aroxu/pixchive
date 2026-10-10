import { Button } from "@heroui/react";
import { Minus, Plus, X } from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import { LocalImage } from "./Common";
import { ko } from "../i18n/ko";

export function FullscreenViewer({
  dialog,
  src,
  title,
  controls,
  previous,
  next,
}: {
  dialog: RefObject<HTMLDialogElement | null>;
  src: string | null;
  title: string;
  controls: ReactNode;
  previous: () => void;
  next: () => void;
}) {
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const stage = useRef<HTMLDivElement>(null);
  const gesture = useRef<{
    id: number;
    x: number;
    y: number;
    panX: number;
    panY: number;
  } | null>(null);
  const reset = () => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
    gesture.current = null;
  };
  const scale = (step: number) => {
    setZoom((value) => Math.min(4, Math.max(1, value + step)));
    setPan({ x: 0, y: 0 });
  };
  useEffect(() => {
    reset();
  }, [src]);
  return (
    <dialog
      ref={dialog}
      className="fullscreen-viewer"
      aria-label={ko.viewer}
      onClose={reset}
      onKeyDown={(e) => {
        if (
          e.target instanceof HTMLInputElement ||
          e.target instanceof HTMLSelectElement
        )
          return;
        if (["ArrowLeft", "ArrowRight", "+", "=", "-", "0"].includes(e.key)) {
          e.preventDefault();
          e.stopPropagation();
          if (e.key === "ArrowLeft") previous();
          if (e.key === "ArrowRight") next();
          if (e.key === "+" || e.key === "=") scale(0.5);
          if (e.key === "-") scale(-0.5);
          if (e.key === "0") reset();
        }
      }}
    >
      <div className="fullscreen-toolbar">
        <span className="viewer-title">{title}</span>
        <Button
          variant="secondary"
          isIconOnly
          aria-label={ko.zoomOut}
          isDisabled={zoom <= 1}
          onPress={() => scale(-0.5)}
        >
          <Minus size={18} />
        </Button>
        <span aria-live="polite">{Math.round(zoom * 100)}%</span>
        <Button
          variant="secondary"
          isIconOnly
          aria-label={ko.zoomIn}
          isDisabled={zoom >= 4}
          onPress={() => scale(0.5)}
        >
          <Plus size={18} />
        </Button>
        <Button variant="ghost" onPress={reset}>
          {ko.fit}
        </Button>
        <Button
          variant="secondary"
          isIconOnly
          aria-label={ko.close}
          onPress={() => dialog.current?.close()}
        >
          <X size={22} />
        </Button>
      </div>
      <div
        className={`zoom-stage ${zoom > 1 ? "is-zoomed" : ""}`}
        ref={stage}
        onDoubleClick={() => (zoom === 1 ? scale(1) : reset())}
        onPointerDown={(e) => {
          if (!e.isPrimary || e.button !== 0) return;
          gesture.current = {
            id: e.pointerId,
            x: e.clientX,
            y: e.clientY,
            panX: pan.x,
            panY: pan.y,
          };
          e.currentTarget.setPointerCapture(e.pointerId);
        }}
        onPointerMove={(e) => {
          const start = gesture.current;
          if (!start || start.id !== e.pointerId || zoom === 1) return;
          const bounds = e.currentTarget.getBoundingClientRect();
          const limitX = (bounds.width * (zoom - 1)) / 2;
          const limitY = (bounds.height * (zoom - 1)) / 2;
          setPan({
            x: Math.max(
              -limitX,
              Math.min(limitX, start.panX + e.clientX - start.x),
            ),
            y: Math.max(
              -limitY,
              Math.min(limitY, start.panY + e.clientY - start.y),
            ),
          });
        }}
        onPointerUp={(e) => {
          const start = gesture.current;
          if (!start || start.id !== e.pointerId) return;
          const dx = e.clientX - start.x,
            dy = e.clientY - start.y;
          gesture.current = null;
          if (
            zoom === 1 &&
            Math.abs(dx) > 60 &&
            Math.abs(dx) > Math.abs(dy) * 1.5
          ) {
            if (dx < 0) next();
            else previous();
          }
        }}
        onPointerCancel={() => {
          gesture.current = null;
        }}
        onLostPointerCapture={() => {
          gesture.current = null;
        }}
        onDragStart={(e) => e.preventDefault()}
      >
        <div
          className="zoom-image"
          style={{
            transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
          }}
        >
          <LocalImage key={src} src={src} alt={title} />
        </div>
      </div>
      {controls}
      <p className="viewer-help">{ko.viewerHelp}</p>
    </dialog>
  );
}
