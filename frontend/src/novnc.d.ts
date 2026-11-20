declare module "@novnc/novnc" {
  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, channel: WebSocket);
    scaleViewport: boolean;
    resizeSession: boolean;
    focusOnClick: boolean;
    viewOnly: boolean;
    disconnect(): void;
  }
}
