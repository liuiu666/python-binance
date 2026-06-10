import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("115.190.218.128", username="root", password="Sl,123321", timeout=10)

def run(cmd, t=15):
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=t)
    stdout.channel.settimeout(t)
    try:
        return stdout.read().decode("utf-8", errors="replace").strip()
    except:
        return "(timeout)"

# Step 1: Disable both services first
print("1. Disable:", run("systemctl disable codex-btc codex-price 2>&1", 15))

# Step 2: Kill the processes directly
print("2. Kill node:", run("kill -9 2743077 2>&1", 10))
print("3. Kill price_proxy:", run("kill -9 2742749 2>&1", 10))

import time
time.sleep(3)

# Step 3: Verify
print("4. Port check:", run("ss -tlnp | grep 3000 || echo PORT_FREE", 10))
print("5. Procs check:", run("ps aux | grep -E 'server.js|price_proxy|signal_btc' | grep -v grep || echo NO_PROCESSES", 10))
print("6. Service status:", run("systemctl is-active codex-btc codex-price 2>&1", 10))

ssh.close()