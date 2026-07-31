import sys

sys.stdin.buffer.read()
sys.stdout.buffer.write(b"A" * (1024 * 1024 + 128) + "\nYou've hit your session limit · resets 11am (Asia/Tokyo)\n".encode())
sys.stdout.buffer.flush()
sys.stderr.buffer.write(b"password=super-secret " + b"B" * (1024 * 1024 + 128) + b"\n")
sys.stderr.buffer.flush()
