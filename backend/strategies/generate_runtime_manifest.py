"""从固定依赖和构建配置生成策略运行时能力清单。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import platform
import sys
from pathlib import Path
from typing import Any


RUNTIME_VERSION = "python3.12-runtime-v3"
BASE_IMAGE = (
  "python:3.12-slim@sha256:"
  "57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
)
SUPPORTED_IMPORTS = {
  "numpy": "numpy",
  "pandas": "pandas",
  "scipy": "scipy",
  "statsmodels": "statsmodels",
  "TA-Lib": "talib",
}


def _sha256(path: Path) -> str:
  return hashlib.sha256(path.read_bytes()).hexdigest()


def _direct_versions(path: Path) -> dict[str, str]:
  versions: dict[str, str] = {}
  for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
      continue
    distribution, separator, version = line.partition("==")
    if not separator:
      raise ValueError(f"直接依赖必须精确固定版本: {distribution}")
    versions[distribution.partition("[")[0]] = version
  return versions


def _verified_standard_library() -> list[str]:
  available: list[str] = []
  for name in sorted(sys.stdlib_module_names):
    try:
      if importlib.util.find_spec(name) is not None:
        available.append(name)
    except (ImportError, AttributeError, ValueError):
      continue
  return available


def _canonical_digest(manifest: dict[str, Any]) -> str:
  encoded = json.dumps(
    manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
  ).encode("utf-8")
  return hashlib.sha256(encoded).hexdigest()


def build_manifest(backend_dir: Path) -> dict[str, Any]:
  if sys.version_info[:2] != (3, 12) or sys.platform != "linux":
    raise RuntimeError("运行时 manifest 必须在 Python 3.12 Linux 基础镜像中生成")
  machine = platform.machine().lower()
  if machine not in {"amd64", "x86_64"}:
    raise RuntimeError("运行时 manifest 当前只支持 Linux x86_64")
  versions = _direct_versions(backend_dir / "requirements.in")
  packages = []
  for distribution, import_name in SUPPORTED_IMPORTS.items():
    version = versions.get(distribution)
    if version is None:
      raise ValueError(f"支持包未在直接依赖中固定: {distribution}")
    packages.append({
      "distribution": distribution,
      "importName": import_name,
      "version": version,
    })
  manifest: dict[str, Any] = {
    "schemaVersion": 1,
    "runtimeVersion": RUNTIME_VERSION,
    "pythonVersion": "3.12",
    "platform": "linux-x86_64",
    "baseImage": BASE_IMAGE,
    "requirementsLockSha256": _sha256(backend_dir / "requirements.lock"),
    "supportedPackages": packages,
    "standardLibraryModules": _verified_standard_library(),
  }
  manifest["capabilityDigest"] = _canonical_digest(manifest)
  return manifest


def main() -> None:
  backend_dir = Path(__file__).resolve().parents[1]
  output_path = Path(__file__).with_name("runtime_manifest.json")
  manifest = build_manifest(backend_dir)
  output_path.write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
  )


if __name__ == "__main__":
  main()
