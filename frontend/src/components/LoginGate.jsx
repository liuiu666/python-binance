import { useState } from "react";
import { LockKeyhole } from "lucide-react";

export default function LoginGate({ onLoginSuccess }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = event => {
    event.preventDefault();
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
        window.localStorage.setItem("btc_username", data.username || username);
        onLoginSuccess();
      })
      .catch(err => setError(err.message || "登录失败"))
      .finally(() => setLoading(false));
  };

  return (
    <main className="login-page">
      <form className="login-card" onSubmit={handleSubmit}>
        <div className="login-icon"><LockKeyhole size={22} /></div>
        <h1>BTC 策略控制台</h1>
        <p>登录后查看策略、数据采集和下单记录。</p>

        {error ? <div className="login-error">{error}</div> : null}

        <label>
          <span>用户名</span>
          <input value={username} required autoFocus onChange={event => setUsername(event.target.value)} />
        </label>
        <label>
          <span>密码</span>
          <input type="password" value={password} required onChange={event => setPassword(event.target.value)} />
        </label>
        <button type="submit" disabled={loading}>{loading ? "登录中..." : "登录"}</button>
      </form>
    </main>
  );
}
