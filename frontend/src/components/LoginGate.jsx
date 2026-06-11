import React, { useState } from "react";

export default function LoginGate({ onLoginSuccess }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password })
    })
      .then(res => {
        if (!res.ok) throw new Error("账号或密码不正确");
        return res.json();
      })
      .then(data => {
        window.localStorage.setItem("btc_auth_token", data.token);
        window.localStorage.setItem("btc_username", data.username);
        onLoginSuccess();
      })
      .catch(err => {
        setError(err.message || "登录失败");
      })
      .finally(() => {
        setLoading(false);
      });
  };

  return (
    <div style={{
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      minHeight: "100vh",
      background: "#0d1117",
      fontFamily: "system-ui, sans-serif"
    }}>
      <form onSubmit={handleSubmit} style={{
        width: "100%",
        maxWidth: "380px",
        padding: "35px",
        borderRadius: "8px",
        background: "#161b22",
        border: "1px solid #30363d",
        boxShadow: "0 10px 25px rgba(0,0,0,0.5)"
      }}>
        <div style={{ textAlign: "center", marginBottom: "30px" }}>
          <h2 style={{ color: "#27c3a5", margin: "0 0 8px 0", fontSize: "24px" }}>BTC 实盘仪表盘</h2>
          <span style={{ color: "#8b949e", fontSize: "12px" }}>登录后查看监控与操作交易</span>
        </div>
        {error ? (
          <div style={{
            background: "rgba(228, 88, 88, 0.15)",
            border: "1px solid rgba(228, 88, 88, 0.3)",
            color: "#e45858",
            padding: "10px",
            borderRadius: "4px",
            fontSize: "13px",
            marginBottom: "18px"
          }}>
            {error}
          </div>
        ) : null}
        <label style={{ display: "block", marginBottom: "15px" }}>
          <span style={{ color: "#c9d1d9", fontSize: "12px", fontWeight: "bold", display: "block", marginBottom: "6px" }}>用户名</span>
          <input
            type="text"
            required
            value={username}
            onChange={e => setUsername(e.target.value)}
            style={{
              width: "100%",
              padding: "10px",
              boxSizing: "border-box",
              borderRadius: "4px",
              border: "1px solid #30363d",
              background: "#0d1117",
              color: "#c9d1d9",
              outline: "none"
            }}
          />
        </label>
        <label style={{ display: "block", marginBottom: "25px" }}>
          <span style={{ color: "#c9d1d9", fontSize: "12px", fontWeight: "bold", display: "block", marginBottom: "6px" }}>密码</span>
          <input
            type="password"
            required
            value={password}
            onChange={e => setPassword(e.target.value)}
            style={{
              width: "100%",
              padding: "10px",
              boxSizing: "border-box",
              borderRadius: "4px",
              border: "1px solid #30363d",
              background: "#0d1117",
              color: "#c9d1d9",
              outline: "none"
            }}
          />
        </label>
        <button type="submit" disabled={loading} style={{
          width: "100%",
          padding: "12px",
          borderRadius: "4px",
          border: "none",
          background: "linear-gradient(90deg, #27c3a5, #22ab8f)",
          color: "#0d1117",
          fontWeight: "bold",
          fontSize: "14px",
          cursor: loading ? "not-allowed" : "pointer"
        }}>
          {loading ? "登录中..." : "登 录"}
        </button>
      </form>
    </div>
  );
}
