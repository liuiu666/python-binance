/**
 * WebSocket 连接管理 Hook
 * 自动连接 /ws, 支持频道订阅和自动重连
 */
import { useEffect, useRef, useCallback, useState } from 'react';

interface UseWebSocketOptions {
  onMessage?: (channel: string, data: any) => void;
}

export function useWebSocket({ onMessage }: UseWebSocketOptions = {}) {
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const subscriptionsRef = useRef<Set<string>>(new Set());

  const connect = useCallback(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

    ws.onopen = () => {
      setConnected(true);
      // 重新订阅之前的频道
      subscriptionsRef.current.forEach((channel) => {
        ws.send(JSON.stringify({ action: 'subscribe', channel }));
      });
    };

    ws.onclose = () => {
      setConnected(false);
      // 3 秒后自动重连
      setTimeout(connect, 3000);
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.channel && msg.data && onMessage) {
          onMessage(msg.channel, msg.data);
        }
      } catch {}
    };

    wsRef.current = ws;
  }, [onMessage]);

  useEffect(() => {
    connect();
    return () => {
      wsRef.current?.close();
    };
  }, [connect]);

  const subscribe = useCallback((channel: string) => {
    subscriptionsRef.current.add(channel);
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: 'subscribe', channel }));
    }
  }, []);

  const unsubscribe = useCallback((channel: string) => {
    subscriptionsRef.current.delete(channel);
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: 'unsubscribe', channel }));
    }
  }, []);

  return { connected, subscribe, unsubscribe };
}
