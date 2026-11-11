#!/usr/bin/env python3
"""Cross-check the Komodo resource files against the compose files.

Fails (exit 1) when:
  * stacks.toml, builds.toml, builders.toml or servers.toml is not valid TOML;
  * a compose file interpolates ${VAR} with no default and the stack's
    `environment` in stacks.toml does not map VAR (Komodo would hand compose
    an empty value: pinchflat's Traefik rule and scrutiny's Signal alerts
    broke this way in 2026-11);
  * auto_update_skip_services names a service the compose file doesn't have.

Stacks whose run_directory has no compose file are reported but don't fail
(retired entries are kept on purpose).

Usage: check-stack-env.py [repo_root]    (needs PyYAML; yamllint pulls it in)
"""
import os
import re
import sys
import tomllib

import yaml

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
errors, notes = [], []


def load_toml(name):
    path = os.path.join(ROOT, name)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        errors.append(f"{name}: invalid TOML: {exc}")
        return {}


def mapped_env(block):
    keys = set()
    for line in (block or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


def interpolations(text):
    """Yield (var, has_default) for each compose interpolation in text."""
    i, n = 0, len(text)
    while i < n:
        if text[i] != "$":
            i += 1
            continue
        if text.startswith("$$", i):          # escaped literal $
            i += 2
            continue
        if text.startswith("${", i):
            depth, j = 0, i + 1
            while j < n:
                depth += {"{": 1, "}": -1}.get(text[j], 0)
                if depth == 0:
                    break
                j += 1
            inner = text[i + 2:j]
            m = NAME.match(inner)
            if m:
                rest = inner[m.end():]
                has_default = rest.startswith((":-", "-"))
                yield m.group(0), has_default
                if rest.startswith((":-", "-", ":+", "+")):
                    yield from interpolations(rest.lstrip(":").lstrip("-+"))
            i = j + 1
            continue
        m = NAME.match(text, i + 1)
        if m:
            yield m.group(0), False
            i = m.end()
            continue
        i += 1


def strings(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield str(k)
            yield from strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from strings(v)
    elif isinstance(node, str):
        yield node


for name in ("builds.toml", "builders.toml", "servers.toml"):
    load_toml(name)

for stack in load_toml("stacks.toml").get("stack", []):
    cfg = stack.get("config", {})
    run_dir = (cfg.get("run_directory") or "").rstrip("/")
    files = cfg.get("file_paths") or ["compose.yaml"]
    label = f"{stack.get('name')} ({run_dir})"
    env = mapped_env(cfg.get("environment"))
    services = set()
    found = False
    for rel in files:
        path = os.path.join(ROOT, run_dir, rel)
        if not os.path.exists(path):
            continue
        found = True
        with open(path) as fh:
            doc = yaml.safe_load(fh) or {}
        services |= set((doc.get("services") or {}).keys())
        missing = sorted({v for s in strings(doc)
                          for v, has_default in interpolations(s)
                          if not has_default and v not in env})
        if missing:
            errors.append(f"{label}: {rel} uses {', '.join(missing)} "
                          "but stacks.toml does not map it")
    if not found:
        notes.append(f"{label}: no compose file ({', '.join(files)})")
        continue
    for svc in cfg.get("auto_update_skip_services", []) or []:
        if svc not in services:
            errors.append(f"{label}: auto_update_skip_services names "
                          f"'{svc}', which is not a service")

for line in notes:
    print(f"note: {line}")
for line in errors:
    print(f"::error::{line}")
print(f"{len(errors)} problem(s)")
sys.exit(1 if errors else 0)
