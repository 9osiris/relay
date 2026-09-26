#!/usr/bin/env python3
# relay log: one json line per proxied request, with size-based rotation.

import json
import os
import threading
import time

MAX_BYTES = 10 * 1024 * 1024
KEEP = 5


class RequestLogger:
    def __init__(self, path, max_bytes=MAX_BYTES, keep=KEEP):
        self.path = os.path.expanduser(path)
        self.max_bytes = max_bytes
        self.keep = keep
        self.lock = threading.Lock()

    def _rotate(self):
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return
        for i in range(self.keep - 1, 0, -1):
            src = "%s.%d" % (self.path, i)
            dst = "%s.%d" % (self.path, i + 1)
            if os.path.exists(src):
                os.replace(src, dst)
        if os.path.exists(self.path):
            os.replace(self.path, self.path + ".1")

    def log(self, entry):
        entry = dict(entry)
        entry.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%S"))
        line = json.dumps(entry)
        with self.lock:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._rotate()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
