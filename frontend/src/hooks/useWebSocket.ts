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

  // 用 Ref 保存回调，防止因回调函数引用变化导致 WebSocket 重连
  const onMessageRef = useRef(onMessage);
  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);

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
        if (msg.channel && msg.data && onMessageRef.current) {
          onMessageRef.current(msg.channel, msg.data);
        }
      } catch {}
    };

    wsRef.current = ws;
  }, []); // 依赖为空，连接函数永远不会被重新创建，从而彻底避免无限重连！

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
