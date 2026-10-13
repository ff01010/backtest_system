"""只读卷容量探针；仅返回 statvfs 容量元数据。"""

from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from typing import Any


POSTGRES_MOUNT = Path(os.getenv("STORAGE_PROBE_POSTGRES_MOUNT", "/volumes/postgres"))
ARTIFACT_MOUNT = Path(os.getenv("STORAGE_PROBE_ARTIFACT_MOUNT", "/volumes/artifacts"))


def capacity_snapshot() -> dict[str, Any]:
  return {
    "observedAt": datetime.now(timezone.utc).isoformat(),
    "volumes": {
      "postgres_volume_free": _volume_capacity(POSTGRES_MOUNT),
      "artifact_volume_free": _volume_capacity(ARTIFACT_MOUNT),
    },
  }


def _volume_capacity(path: Path) -> dict[str, int]:
  stats = os.statvfs(path)
  return {
    "availableBytes": stats.f_bavail * stats.f_frsize,
    "totalBytes": stats.f_blocks * stats.f_frsize,
  }


class CapacityHandler(BaseHTTPRequestHandler):
  def do_GET(self) -> None:  # noqa: N802
    if self.path not in {"/health", "/v1/capacity"}:
      self.send_error(404)
      return
    try:
      payload = {"status": "ok"} if self.path == "/health" else capacity_snapshot()
      encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
      self.send_response(200)
    except OSError as error:
      encoded = json.dumps({"status": "unavailable", "error": error.errno}).encode("utf-8")
      self.send_response(503)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(encoded)))
    self.end_headers()
    self.wfile.write(encoded)

  def log_message(self, _format: str, *_args: object) -> None:
    return


def main() -> None:
  port = int(os.getenv("STORAGE_PROBE_PORT", "8091"))
  ThreadingHTTPServer(("0.0.0.0", port), CapacityHandler).serve_forever()


if __name__ == "__main__":
  main()
