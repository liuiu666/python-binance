"""测试 Key 是否属于现货测试网而非合约测试网"""
import hmac
import hashlib
import time
import httpx

API_KEY = "Pj4PyMhS6GmElbhQVi0n48WvFBEaGHEsT9njacTuBejXLYk7yWyQIDttI0tFLoIf"
API_SECRET = "8ELgLtB7IFLEbek3DAOtw9orZkXeKbSQpnAL6o4gmi8GDlnsZT1kxZINQqEYVKWb"

# 合约测试网
FUTURES = "https://testnet.binancefuture.com"
# 现货测试网
SPOT = "https://testnet.binance.vision"

ts = int(time.time() * 1000)
query = f"timestamp={ts}"
sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

# 合约测试网
c1 = httpx.Client(base_url=FUTURES, headers={"X-MBX-APIKEY": API_KEY})
r1 = c1.post(f"/fapi/v1/listenKey?{query}&signature={sig}")
print(f"合约测试网 listenKey => {r1.status_code}  {r1.text[:200]}")

# 现货测试网
ts2 = int(time.time() * 1000)
query2 = f"timestamp={ts2}"
sig2 = hmac.new(API_SECRET.encode(), query2.encode(), hashlib.sha256).hexdigest()
c2 = httpx.Client(base_url=SPOT, headers={"X-MBX-APIKEY": API_KEY})
r2 = c2.post(f"/api/v3/userDataStream?{query2}&signature={sig2}")
print(f"现货测试网 listenKey => {r2.status_code}  {r2.text[:200]}")

r3 = c2.get(f"/api/v3/account?{query2}&signature={sig2}")
print(f"现货测试网 account  => {r3.status_code}  {r3.text[:300]}")

c1.close()
c2.close()
