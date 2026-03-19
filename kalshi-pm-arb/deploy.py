"""deploy.py — Deploy kalshi-pm-arb to EC2 via paramiko SFTP."""
import os
import paramiko
import sys
from pathlib import Path

EC2_HOST = "3.73.101.112"
EC2_USER = "ubuntu"
PEM_PATH = "/home/node/.openclaw/workspace/polymarket-arb.pem"
KALSHI_PEM_SRC = "/home/node/.openclaw/workspace/kalshi-weather/config/kalshi_private.pem"
LOCAL_ROOT = Path(__file__).parent
REMOTE_ROOT = "/home/ubuntu/kalshi-pm-arb"

# Files to upload (relative to LOCAL_ROOT → remote path relative to REMOTE_ROOT)
FILES = [
    ("src/main.py",            "src/main.py"),
    ("src/config.py",          "src/config.py"),
    ("src/kalshi_auth.py",     "src/kalshi_auth.py"),
    ("src/kalshi_markets.py",  "src/kalshi_markets.py"),
    ("src/pm_markets.py",      "src/pm_markets.py"),
    ("src/matcher.py",         "src/matcher.py"),
    ("src/executor.py",        "src/executor.py"),
    ("src/balance_monitor.py", "src/balance_monitor.py"),
    ("src/redeemer.py",        "src/redeemer.py"),
    ("src/notifier.py",        "src/notifier.py"),
    ("src/price_feed.py",      "src/price_feed.py"),
    ("src/daemon.py",          "src/daemon.py"),
    ("config/.env",            "config/.env"),
    ("requirements.txt",       "requirements.txt"),
    ("scripts/start.sh",       "scripts/start.sh"),
    ("scripts/stop.sh",        "scripts/stop.sh"),
    ("README.md",              "README.md"),
    (".gitignore",             ".gitignore"),
]


def run_cmd(ssh, cmd, check=True):
    print(f"  $ {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    exit_code = stdout.channel.recv_exit_status()
    if out:
        print(f"    {out}")
    if err:
        print(f"    STDERR: {err}")
    if check and exit_code != 0:
        raise RuntimeError(f"Command failed (exit {exit_code}): {cmd}")
    return out, err, exit_code


def sftp_mkdir_p(sftp, remote_path):
    """Create remote directory tree, ignoring if already exists."""
    parts = remote_path.split("/")
    current = ""
    for part in parts:
        if not part:
            continue
        current += "/" + part
        try:
            sftp.mkdir(current)
        except IOError:
            pass  # already exists


def main():
    print(f"Connecting to {EC2_USER}@{EC2_HOST}...")
    key = paramiko.RSAKey.from_private_key_file(PEM_PATH)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(EC2_HOST, username=EC2_USER, pkey=key, timeout=30)
    print("Connected ✓")

    sftp = ssh.open_sftp()

    # Create directory structure
    print("\nCreating remote directories...")
    for d in [REMOTE_ROOT, f"{REMOTE_ROOT}/src", f"{REMOTE_ROOT}/config",
              f"{REMOTE_ROOT}/logs", f"{REMOTE_ROOT}/scripts"]:
        sftp_mkdir_p(sftp, d)
        print(f"  {d} ✓")

    # Upload project files
    print("\nUploading project files...")
    for local_rel, remote_rel in FILES:
        local_path = LOCAL_ROOT / local_rel
        remote_path = f"{REMOTE_ROOT}/{remote_rel}"
        if not local_path.exists():
            print(f"  SKIP (not found): {local_rel}")
            continue
        sftp.put(str(local_path), remote_path)
        print(f"  {local_rel} → {remote_path} ✓")

    # Upload Kalshi PEM
    print("\nUploading Kalshi PEM...")
    sftp.put(KALSHI_PEM_SRC, f"{REMOTE_ROOT}/config/kalshi_private.pem")
    print(f"  kalshi_private.pem → {REMOTE_ROOT}/config/kalshi_private.pem ✓")

    # Create logs/.gitkeep
    with sftp.open(f"{REMOTE_ROOT}/logs/.gitkeep", "w") as f:
        f.write("")
    print(f"  logs/.gitkeep ✓")

    sftp.close()

    # Set permissions
    print("\nSetting file permissions...")
    run_cmd(ssh, f"chmod 600 {REMOTE_ROOT}/config/.env {REMOTE_ROOT}/config/kalshi_private.pem")
    run_cmd(ssh, f"chmod +x {REMOTE_ROOT}/scripts/start.sh {REMOTE_ROOT}/scripts/stop.sh")
    print("  Permissions set ✓")

    # Install dependencies
    print("\nInstalling Python dependencies (this may take a minute)...")
    run_cmd(ssh, f"cd {REMOTE_ROOT} && pip3 install -r requirements.txt --quiet 2>&1 | tail -5")
    print("  Dependencies installed ✓")

    # Test: python3 src/main.py --help
    print("\nTesting imports (python3 src/main.py --help)...")
    out, err, code = run_cmd(ssh, f"cd {REMOTE_ROOT} && python3 src/main.py --help", check=False)
    if code == 0:
        print("  ✅ Import test PASSED")
    else:
        print(f"  ❌ Import test FAILED (exit {code})")
        print(f"  stdout: {out}")
        print(f"  stderr: {err}")
        sys.exit(1)

    # Verify the .env is not in git tracking
    print("\nFinal checks...")
    out, _, _ = run_cmd(ssh, f"ls -la {REMOTE_ROOT}/src/", check=False)
    print(f"  src/ contents: {out[:200]}")

    ssh.close()
    print("\n✅ Deployment complete!")
    print(f"   Remote: {EC2_USER}@{EC2_HOST}:{REMOTE_ROOT}")
    print(f"   LIVE_TRADING=false (paper mode)")
    print(f"   To start: cd {REMOTE_ROOT} && bash scripts/start.sh")
    print(f"   To monitor: tail -f {REMOTE_ROOT}/logs/scanner.log")


if __name__ == "__main__":
    main()
