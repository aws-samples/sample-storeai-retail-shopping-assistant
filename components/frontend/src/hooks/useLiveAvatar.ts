"use client";
import { useRef, useState, useCallback, useEffect } from "react";
import { useCognito } from "@/context/CognitoAuthContext";
import { avatarSenderUrl } from "@/lib/avatar";

/**
 * Hook for LiveAvatar text relay.
 * Sends agent response text → avatar speaks with lip-sync.
 */
export function useLiveAvatar() {
  const { idToken } = useCognito();
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);

  const connect = useCallback(() => {
    wsRef.current?.close();
    const ws = new WebSocket(avatarSenderUrl(idToken));
    ws.onopen = () => setConnected(true);
    ws.onclose = () => { if (wsRef.current === ws) setConnected(false); };
    wsRef.current = ws;
  }, [idToken]);

  useEffect(() => () => { wsRef.current?.close(); }, [idToken]);

  const speak = useCallback((text: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify({ type: "speak", text }));
  }, []);

  const interrupt = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify({ type: "interrupt" }));
  }, []);

  const disconnect = useCallback(() => {
    wsRef.current?.close();
    setConnected(false);
  }, []);

  return { connected, connect, speak, interrupt, disconnect };
}
