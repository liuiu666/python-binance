"""Download and analyze second-level data from the production server."""
import paramiko
import os
import sys

HOST = "115.190.218.128"
USER = "root"
PASS = "Sl,123321"
REMOTE_CSV = "/opt/btc-binary-options/data/btcusdt_1s_trades.csv"
LOCAL_CSV = os.path.join(os.path.dirname(__file__), "server_1s_trades.csv")

def ssh_exec(ssh, cmd):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=30)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return out, err

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting to {HOST}...")
    ssh.connect(HOST, username=USER, password=PASS, timeout=15)
    print("Connected.")

    # 1. Basic file info
    print("\n=== FILE INFO ===")
    out, _ = ssh_exec(ssh, f"wc -l {REMOTE_CSV}; ls -lh {REMOTE_CSV}; head -1 {REMOTE_CSV}")
    print(out)

    # 2. Time range
    print("=== TIME RANGE ===")
    out, _ = ssh_exec(ssh, f"head -2 {REMOTE_CSV} | tail -1; echo '...'; tail -2 {REMOTE_CSV}")
    print(out)

    # 3. Download CSV
    print(f"\n=== DOWNLOADING CSV -> {LOCAL_CSV} ===")
    sftp = ssh.open_sftp()
    sftp.get(REMOTE_CSV, LOCAL_CSV)
    sftp.close()
    fsize = os.path.getsize(LOCAL_CSV)
    print(f"Downloaded: {fsize:,} bytes")

    ssh.close()
    print("\nDone. Now run analysis locally.")

if __name__ == "__main__":
    main()
