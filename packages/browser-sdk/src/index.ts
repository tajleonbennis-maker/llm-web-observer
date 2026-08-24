export type ObserverContext = {
  tenant_id?: string;
  user_id_hash?: string;
  session_id?: string;
  conversation_id?: string;
  interaction_id?: string;
};

export type ObserverOptions = {
  endpoint: string;
  service: string;
  apiKey?: string;
  context?: ObserverContext;
};

const hex = (bytes: number): string => {
  const value = new Uint8Array(bytes);
  crypto.getRandomValues(value);
  return [...value].map(item => item.toString(16).padStart(2, "0")).join("");
};

export class BrowserObserver {
  private readonly options: ObserverOptions;
  private readonly queue: object[] = [];

  constructor(options: ObserverOptions) {
    this.options = {...options, endpoint: options.endpoint.replace(/\/$/, "")};
  }

  context(context: ObserverContext): void {
    this.options.context = {...this.options.context, ...context};
  }

  interaction(eventType: string, attributes: Record<string, unknown> = {}): {traceId: string; spanId: string} {
    const traceId = hex(16), spanId = hex(8), now = new Date().toISOString();
    this.queue.push({
      schema_version: "0.1", timestamp: now, event_type: eventType, trace_id: traceId,
      span_id: spanId, parent_span_id: null, service: this.options.service, span_kind: "client",
      start_time: now, end_time: now, duration_ms: 0, status: "ok",
      ...this.options.context, attributes,
    });
    void this.flush();
    return {traceId, spanId};
  }

  traceFetch(input: RequestInfo | URL, init: RequestInit = {}, trace?: {traceId: string; spanId: string}): Promise<Response> {
    const active = trace ?? {traceId: hex(16), spanId: hex(8)};
    const headers = new Headers(init.headers);
    headers.set("traceparent", `00-${active.traceId}-${active.spanId}-01`);
    return fetch(input, {...init, headers});
  }

  async flush(): Promise<boolean> {
    if (!this.queue.length) return true;
    const events = this.queue.splice(0, 100);
    try {
      const response = await fetch(`${this.options.endpoint}/v1/events`, {
        method: "POST",
        headers: {"Content-Type": "application/json", ...(this.options.apiKey ? {Authorization: `Bearer ${this.options.apiKey}`} : {})},
        body: JSON.stringify({events}),
        keepalive: true,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return true;
    } catch {
      this.queue.unshift(...events);
      return false;
    }
  }
}
