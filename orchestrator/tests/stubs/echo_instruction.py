import sys

data = sys.stdin.buffer.read()
out_path = sys.argv[1] if len(sys.argv) > 1 else None
if out_path:
    with open(out_path, "wb") as fh:
        fh.write(data)
sys.stdout.buffer.write(data)
sys.exit(0)
