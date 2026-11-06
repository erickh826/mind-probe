"""Static guards for the CI/CD workflows (text checks; no YAML library needed).

Prints one PASS/FAIL line per check. Run by dryrun/run.sh:
    python3 deploy/backend/dryrun/check-workflows.py .github/workflows
"""
import re
import sys
from pathlib import Path

wf = Path(sys.argv[1])
results = []


def check(label, cond):
    results.append(f"{'PASS' if cond else 'FAIL'} workflow: {label}")


files = {p.name: p.read_text(encoding="utf-8") for p in sorted(wf.glob("*.y*ml"))}
deploy = files.get("backend-deploy.yml", "")
rollback = files.get("backend-rollback.yml", "")
ci = files.get("ci.yml", "")
check("backend-deploy.yml, backend-rollback.yml and ci.yml exist", bool(deploy and rollback and ci))

TS_PIN = re.compile(r"^\s+uses: tailscale/github-action@[0-9a-f]{40}\b", re.M)
TS_ANY = re.compile(r"tailscale/github-action@")
RUN_CD = "run: bash deploy/backend/ci-deploy.sh"

for name, text in (("backend-deploy.yml", deploy), ("backend-rollback.yml", rollback)):
    check(f"{name}: job runs in the production environment", re.search(r"^    environment: production$", text, re.M) is not None)
    check(f"{name}: top-level permissions are contents: read only", re.search(r"^permissions:\n  contents: read\n\n", text, re.M) is not None)
    check(f"{name}: id-token: write only at job level", re.findall(r"^( *)id-token: write", text, re.M) == ["      "])
    check(f"{name}: shares the backend-production queue, never cancelled", "group: backend-production\n  cancel-in-progress: false" in text)
    check(f"{name}: tailscale action pinned to a full commit SHA", len(TS_PIN.findall(text)) == 1 and len(TS_ANY.findall(text)) == 1)
    check(f"{name}: tailscale client version pinned", re.search(r"^\s+version: \d+\.\d+\.\d+$", text, re.M) is not None)
    check(f"{name}: OIDC only (no oauth-secret, no authkey)", "oauth-secret" not in text and "authkey" not in text
          and "audience: ${{ secrets.TS_AUDIENCE }}" in text)
    check(f"{name}: runner node tagged tag:mindprobe-ci", "tags: tag:mindprobe-ci\n" in text)
    check(f"{name}: tailnet address required", 'DEPLOY_REQUIRE_TAILNET: "1"' in text)
    ci_check = text.find("DEPLOY_CHECK_ONLY: \"1\"")
    ts = text.find("tailscale/github-action@")
    runs = [m.start() for m in re.finditer(re.escape(RUN_CD), text)]
    check(f"{name}: inputs checked before joining the tailnet, host run after it",
          len(runs) == 2 and 0 <= ci_check < runs[0] < ts < runs[1])
    leave = text.find("sudo tailscale logout")
    check(f"{name}: explicit tailnet logout and key removal in an always() step",
          leave > runs[-1] and "if: always()\n        run: |\n          rm -f \"$DEPLOY_SSH_KEY_FILE\"" in text)

check("backend-deploy.yml: mode defaults to the read-only preflight",
      re.search(r"options: \[preflight, deploy\]\n\s+default: preflight\n", deploy) is not None)
check("backend-deploy.yml: triggered by workflow_dispatch only",
      re.search(r"^on:\n  workflow_dispatch:\n", deploy, re.M) is not None and "\n  push:" not in deploy and "pull_request" not in deploy)
check("backend-rollback.yml: triggered by workflow_dispatch only",
      re.search(r"^on:\n  workflow_dispatch:\n", rollback, re.M) is not None and "\n  push:" not in rollback and "pull_request" not in rollback)
check("ci.yml: no id-token and no secrets", "id-token" not in ci and "secrets." not in ci)
for name, text in files.items():
    check(f"{name}: no ssh-keyscan, self-hosted runner or pull_request_target",
          "ssh-keyscan" not in text and "self-hosted" not in text and "pull_request_target" not in text)
    if name not in ("backend-deploy.yml", "backend-rollback.yml"):
        check(f"{name}: no deploy or Tailscale secrets", "DEPLOY_" not in text and "TS_" not in text)

print("\n".join(results))
