import sys
import termios
import tty
import select

import os

def getch():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch_bytes = os.read(fd, 1)
        if not ch_bytes:
            return ""
        ch = ch_bytes.decode("utf-8", errors="ignore")
        seq = [ch]
        while True:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                next_bytes = os.read(fd, 1)
                if next_bytes:
                    seq.append(next_bytes.decode("utf-8", errors="ignore"))
            else:
                break
        return "".join(seq)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)

print("Press any key (like Arrow Keys) to see its raw representation. Press Ctrl+C to exit.")
try:
    while True:
        key = getch()
        print(f"Key: {repr(key)}")
except KeyboardInterrupt:
    print("\nExiting.")
