import {
  useEffect,
  useLayoutEffect,
  useRef,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { flushSync } from "react-dom";
import type { Models } from "../api/client";
import { ViewerImage } from "./Common";

type Page = Models["ArtworkPage"];
type Sample = { x: number; time: number };
type Gesture = {
  id: number;
  x: number;
  y: number;
  horizontal: boolean;
  samples: Sample[];
};
type Turn = { finish: () => void; cancel: () => void };

const SETTLE_MS = 220;
const DECIDE_PX = 10;
// A release faster than this (px/ms over the last moment of the drag) counts
// as a flick even when the drag itself was short.
const FLICK_SPEED = 0.35;
const FLICK_WINDOW_MS = 100;

// A pinch-zoomed page keeps one-finger panning for looking around.
const pageZoomed = () => (window.visualViewport?.scale ?? 1) > 1.01;

// Touch swipes between the pages of one work. The previous and next pages sit
// beside the current one, so the neighbour slides in under the finger. Vertical
// drags keep scrolling the page, and mouse input keeps using the buttons.
export function SwipeViewer({
  pages,
  page,
  title,
  frameRatio,
  onTurn,
}: {
  pages: Page[];
  page: number;
  title: string;
  frameRatio?: number;
  onTurn: (direction: 1 | -1) => void;
}) {
  const track = useRef<HTMLDivElement>(null);
  const gesture = useRef<Gesture | null>(null);
  const turning = useRef<Turn | null>(null);
  const recenter = useRef(false);

  const place = (offset: number, duration = 0) => {
    const element = track.current;
    if (!element) return;
    element.style.transition = duration
      ? `transform ${duration}ms cubic-bezier(0.2, 0.8, 0.2, 1)`
      : "none";
    element.style.transform = offset
      ? `translate3d(calc(-100% + ${offset}px), 0, 0)`
      : "";
  };

  const turn = (direction: 1 | -1) => {
    const element = track.current;
    if (!element) return;
    place(
      direction === 1 ? -element.clientWidth : element.clientWidth,
      SETTLE_MS,
    );
    const ended = (event: TransitionEvent) => {
      if (event.target === element && event.propertyName === "transform")
        state.finish();
    };
    const cancel = () => {
      window.clearTimeout(timer);
      element.removeEventListener("transitionend", ended);
      if (turning.current === state) turning.current = null;
    };
    const state: Turn = {
      cancel,
      finish: () => {
        if (turning.current !== state) return;
        cancel();
        // Render the next page and re-centre in the same frame, so the slide
        // that just moved into view simply stays where it is.
        recenter.current = true;
        flushSync(() => onTurn(direction));
        if (recenter.current) {
          recenter.current = false;
          place(0);
        }
      },
    };
    const timer = window.setTimeout(() => state.finish(), SETTLE_MS + 100);
    element.addEventListener("transitionend", ended);
    turning.current = state;
  };

  useLayoutEffect(() => {
    if (recenter.current) {
      recenter.current = false;
      place(0);
      return;
    }
    // A button or arrow key changed the page while a swipe was still
    // settling. Drop that swipe so the page moves only once.
    if (turning.current) {
      turning.current.cancel();
      place(0);
    }
  }, [page]);

  useEffect(() => {
    const element = track.current;
    if (!element) return;
    // Stop the page from scrolling once a drag has become a sideways swipe.
    const hold = (event: TouchEvent) => {
      if (gesture.current?.horizontal && event.cancelable)
        event.preventDefault();
    };
    // While the page is pinch-zoomed, let the browser pan in every direction.
    const viewport = window.visualViewport;
    const follow = () => {
      element.style.touchAction = pageZoomed() ? "auto" : "";
    };
    follow();
    element.addEventListener("touchmove", hold, { passive: false });
    viewport?.addEventListener("resize", follow);
    return () => {
      element.removeEventListener("touchmove", hold);
      viewport?.removeEventListener("resize", follow);
      turning.current?.cancel();
    };
  }, []);

  const settle = () => place(0, SETTLE_MS);

  const end = (
    event: ReactPointerEvent<HTMLDivElement>,
    cancelled: boolean,
  ) => {
    const current = gesture.current;
    if (!current || current.id !== event.pointerId) return;
    gesture.current = null;
    if (!current.horizontal) return;
    const dx = event.clientX - current.x;
    const width = track.current?.clientWidth || 1;
    const since =
      current.samples.find(
        (sample) => event.timeStamp - sample.time <= FLICK_WINDOW_MS,
      ) ?? current.samples[current.samples.length - 1];
    const velocity = since
      ? (event.clientX - since.x) / Math.max(1, event.timeStamp - since.time)
      : 0;
    const fast = Math.abs(velocity) > FLICK_SPEED;
    const onward = Math.sign(velocity) === Math.sign(dx);
    const direction = dx < 0 ? 1 : -1;
    const available = direction === 1 ? page < pages.length - 1 : page > 0;
    // A long drag turns unless it is flicked back at the end; a short drag
    // turns when it is flicked onward.
    const far = Math.abs(dx) > width * 0.2 && !(fast && !onward);
    const flick = Math.abs(dx) > 30 && fast && onward;
    if (!cancelled && available && (far || flick)) turn(direction);
    else settle();
  };

  return (
    <div
      ref={track}
      className="viewer-track"
      onPointerDown={(event) => {
        if (event.pointerType === "mouse") return;
        if (!event.isPrimary) {
          // A second finger means a pinch; leave it to the browser.
          if (gesture.current?.horizontal) settle();
          gesture.current = null;
          return;
        }
        turning.current?.finish();
        if (pageZoomed()) return;
        gesture.current = {
          id: event.pointerId,
          x: event.clientX,
          y: event.clientY,
          horizontal: false,
          samples: [{ x: event.clientX, time: event.timeStamp }],
        };
      }}
      onPointerMove={(event) => {
        const current = gesture.current;
        if (!current || current.id !== event.pointerId) return;
        const dx = event.clientX - current.x;
        const dy = event.clientY - current.y;
        current.samples.push({ x: event.clientX, time: event.timeStamp });
        while (
          current.samples.length > 2 &&
          event.timeStamp - current.samples[0].time > FLICK_WINDOW_MS
        )
          current.samples.shift();
        if (!current.horizontal) {
          if (Math.abs(dx) < DECIDE_PX && Math.abs(dy) < DECIDE_PX) return;
          if (Math.abs(dx) <= Math.abs(dy)) {
            gesture.current = null;
            return;
          }
          current.horizontal = true;
          try {
            event.currentTarget.setPointerCapture(event.pointerId);
          } catch {
            // The pointer may already be gone; the move still updates.
          }
        }
        const edge = dx > 0 ? page === 0 : page === pages.length - 1;
        place(edge ? dx / 4 : dx);
      }}
      onPointerUp={(event) => end(event, false)}
      onPointerCancel={(event) => end(event, true)}
      onLostPointerCapture={(event) => {
        // An image handing its implicit touch capture over to the track also
        // bubbles here; only the track losing its own capture ends the swipe.
        if (event.target !== event.currentTarget) return;
        if (gesture.current?.id !== event.pointerId) return;
        if (gesture.current.horizontal) settle();
        gesture.current = null;
      }}
      onDragStart={(event) => event.preventDefault()}
    >
      {[page - 1, page, page + 1].map((index) => {
        const item = pages[index];
        const current = index === page;
        return (
          <div
            key={
              item ? `page-${index}` : index < page ? "edge-start" : "edge-end"
            }
            className="viewer-slide"
            aria-hidden={current ? undefined : true}
            inert={!current}
          >
            {item && (
              <ViewerImage
                src={item.url}
                alt={current ? title : ""}
                width={item.width}
                height={item.height}
                frameRatio={frameRatio}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
