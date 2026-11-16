/* Preview-only event transport over intercepted HTTP. Production assets stay untouched.
 * This tests UI event handling, not native WebSocket handshake/reconnection behavior. */
(() => {
  const NativeWebSocket = window.WebSocket;
  class PreviewWebSocket extends EventTarget {
    static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
    CONNECTING = 0; OPEN = 1; CLOSING = 2; CLOSED = 3;
    constructor(url) {
      super();
      const target = new URL(url, location.href);
      if (target.host !== location.host || target.pathname !== '/ws/chat') return new NativeWebSocket(url);
      this.url = target.href;
      this.readyState = 0;
      this.cursor = -1;
      this.token = target.searchParams.get('token') || '';
      this.poll();
    }
    emit(type, event) {
      this.dispatchEvent(event);
      this['on' + type]?.(event);
    }
    async poll() {
      try {
        const res = await fetch('/__preview__/events?since=' + this.cursor + '&token=' + encodeURIComponent(this.token));
        if (this.readyState === 3) return;
        if (!res.ok) { this.close(res.status === 401 ? 4401 : 1011); return; }
        const data = await res.json();
        if (this.readyState === 3) return;
        if (this.readyState === 0) { this.readyState = 1; this.emit('open', new Event('open')); }
        this.cursor = data.cursor;
        for (const event of data.events) this.emit('message', new MessageEvent('message', {data: JSON.stringify(event)}));
        if (this.readyState !== 3) this.timer = setTimeout(() => this.poll(), 100);
      } catch (_) { if (this.readyState !== 3) this.close(1011); }
    }
    send() { throw new Error('Preview event transport is receive-only'); }
    close(code = 1000) {
      if (this.readyState === 3) return;
      clearTimeout(this.timer);
      this.readyState = 3;
      this.emit('close', new CloseEvent('close', {code}));
    }
  }
  window.WebSocket = PreviewWebSocket;
})();
