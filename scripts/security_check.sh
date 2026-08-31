#!/usr/bin/env bash
# Run local dependency, tracked-text secret, and container hardening checks.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-${ROOT_DIR}/.venv/bin/python}"
API_IMAGE="${API_IMAGE:-control-tower-api:ci}"
DASHBOARD_IMAGE="${DASHBOARD_IMAGE:-control-tower-dashboard:ci}"

fail() { printf 'security check failed: %s\n' "$1" >&2; exit 1; }

command -v git >/dev/null 2>&1 || fail "git is required"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || fail "Python 3 is required"

"$PYTHON" - "$ROOT_DIR" <<'PY'
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
paths = subprocess.run(
    ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
    cwd=root,
    check=True,
    capture_output=True,
).stdout.split(b"\0")
tracked_paths = {
    raw.decode("utf-8")
    for raw in subprocess.run(
        ["git", "ls-files", "--cached", "-z"], cwd=root, check=True, capture_output=True
    ).stdout.split(b"\0")
    if raw
}

private_key = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
credential_assignment = re.compile(
    r"(?i)(?:\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\b|"
    r"\b[A-Z][A-Z0-9]*_(?:PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY)\b)"
    r"\s*[:=]\s*[\"']?([^\s\"'`,;)}]+)"
)
authenticated_url = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:([^@\s/]{8,})@")
placeholder_markers = (
    "synthetic",
    "placeholder",
    "example",
    "changeme",
    "change-me",
    "replace",
    "your_",
    "your-",
    "dummy",
    "fixture",
    "test-secret",
    "not-a-secret",
    "redacted",
    "control_tower",
    "localhost",
)
secret_filenames = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.test",
    "id_rsa",
    "id_ed25519",
}
secret_suffixes = {".key", ".pem", ".p12", ".pfx"}

findings: list[tuple[str, int, str]] = []
checked = 0
for raw_path in paths:
    if not raw_path:
        continue
    relative = raw_path.decode("utf-8")
    filename = Path(relative).name
    forbidden_name = (
        filename == ".env"
        or (filename.startswith(".env.") and filename != ".env.example")
        or filename in secret_filenames
        or Path(relative).suffix.lower() in secret_suffixes
    )
    if forbidden_name:
        if relative in tracked_paths:
            findings.append((relative, 1, "forbidden secret-bearing filename"))
        continue
    if relative.startswith("tests/") or relative.startswith("tests/fixtures/") or "/fixtures/" in relative:
        continue
    path = root / relative
    if not path.is_file():
        continue
    data = path.read_bytes()
    if b"\0" in data:
        continue
    checked += 1
    text = data.decode("utf-8", errors="replace")
    for line_number, line in enumerate(text.splitlines(), 1):
        if private_key.search(line):
            findings.append((relative, line_number, "private-key marker"))
            continue
        for match in credential_assignment.finditer(line):
            value = match.group(1).lower()
            if (
                len(value) >= 12
                and not any(marker in value for marker in placeholder_markers)
                and not any(character in value for character in "${}")
            ):
                findings.append((relative, line_number, "credential assignment"))
                break
        if authenticated_url.search(line):
            value = authenticated_url.search(line).group(1).lower()
            if not any(marker in value for marker in placeholder_markers):
                findings.append((relative, line_number, "credential-bearing URL"))

if findings:
    for relative, line_number, kind in findings:
        print(f"{relative}:{line_number}: {kind}", file=sys.stderr)
    raise SystemExit(f"tracked-text secret scan found {len(findings)} high-confidence marker(s)")

print(f"tracked-text secret scan passed: {checked} text files checked")
PY

if command -v pip-audit >/dev/null 2>&1; then
    pip-audit --local
elif "$PYTHON" -m pip_audit --version >/dev/null 2>&1; then
    "$PYTHON" -m pip_audit --local
else
    printf 'dependency audit skipped: pip-audit is not installed\n'
fi

command -v docker >/dev/null 2>&1 || fail "docker is required"

for image in "$API_IMAGE" "$DASHBOARD_IMAGE"; do
    [[ -n "$(docker image inspect "$image" 2>/dev/null)" ]] || fail "image is not available: $image"
    user="$(docker image inspect --format '{{.Config.User}}' "$image")"
    [[ "$user" == "10001:10001" ]] || fail "$image runtime user is not 10001:10001"
    volumes="$(docker image inspect --format '{{json .Config.Volumes}}' "$image")"
    [[ "$volumes" == "null" ]] || fail "$image declares writable volumes"
    docker run --rm --entrypoint python "$image" -c \
        'import os; assert os.getuid() == 10001; assert not os.access("/app", os.W_OK)' \
        || fail "$image does not enforce a non-writable /app runtime"
done

printf 'container hardening passed: images run as UID/GID 10001 with no writable /app volume\n'
