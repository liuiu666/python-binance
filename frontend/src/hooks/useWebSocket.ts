/**
 * WebSocket 连接管理 Hook
 * 自动连接 /ws, 支持频道订阅和自动重连
 * 组件卸载时停止重连, 避免后台残留连接
 */
import { useEffect, useRef, useCallback, useState } from 'react';

interface UseWebSocketOptions {
  onMessage?: (channel: string, data: any) => void;
}

export function useWebSocket({ onMessage }: UseWebSocketOptions = {}) {
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const subscriptionsRef = useRef<Set<string>>(new Set());
  // 控制是否允许重连: 组件卸载时设为 false
  const shouldReconnectRef = useRef(true);
  // 保存重连定时器, 卸载时清除
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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
      // 只有在组件仍挂载时才自动重连
      if (shouldReconnectRef.current) {
        reconnectTimerRef.current = setTimeout(connect, 3000);
      }
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
    shouldReconnectRef.current = true;
    connect();
    return () => {
      // 卸载: 停止重连 + 清除定时器 + 关闭连接
      shouldReconnectRef.current = false;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
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
