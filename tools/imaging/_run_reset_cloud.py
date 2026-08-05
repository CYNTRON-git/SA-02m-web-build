#!/usr/bin/env python3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ssh"))
from sa02m_remote import connect, run

root = Path(__file__).resolve().parents[2]
script = root / "tools" / "imaging" / "reset-cloud-enrollment.sh"
script.write_bytes(script.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n"))

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.136"
client = connect(host, "root", "cyntron")
sftp = client.open_sftp()
sftp.put(str(script), "/tmp/reset-cloud-enrollment.sh")
sftp.close()
code, out, err = run(
    client,
    "bash /tmp/reset-cloud-enrollment.sh",
    timeout=60,
)
sys.stdout.write(out)
if err:
    sys.stderr.write(err)
print("exit", code)
client.close()
raise SystemExit(code)
