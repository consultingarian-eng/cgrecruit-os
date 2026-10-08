import { useEffect, useRef } from "react";

/**
 * Subscribe to the backend SSE stream. Calls onEvent() the instant the server
 * pushes a status change — no polling lag. Auto-reconnects on drop.
 *
 * @param {() => void} onEvent  Called on every non-ping server push.
 * @param {boolean}    enabled  Pass false to skip (e.g. no pipeline selected yet).
 */
export function useServerEvents(onEvent, enabled = true) {
    const onEventRef = useRef(onEvent);
    onEventRef.current = onEvent;

    useEffect(() => {
        if (!enabled) return;

        let es;
        let retryTimer;
        let closed = false;

        const connect = () => {
            es = new EventSource("/api/events/stream", { withCredentials: true });

            es.onmessage = (e) => {
                if (e.data && e.data !== "connected") {
                    onEventRef.current();
                }
            };

            es.onerror = () => {
                es.close();
                if (!closed) {
                    // Back off 3 s then reconnect
                    retryTimer = setTimeout(connect, 3000);
                }
            };
        };

        connect();

        return () => {
            closed = true;
            clearTimeout(retryTimer);
            es?.close();
        };
    }, [enabled]);
}
