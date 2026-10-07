# Contribution branch

Base: upstream main d150d4d. Branch: contribution/build-reliability-and-wizard.
This directory is separate from the private working builder. It contains upstream
sample configurations, not customer configurations. Customer profile integration
and server-policy customization are intentionally excluded.

Public-copy verification: 37 Python tests and mocked desktop/mobile wizard tests
passed. Upstream sample configurations match HEAD exactly. A read-only scan of
101 source/documentation files found no matches for 20 customer-specific values
from the private JSON files (servers, keys, password, identifiers and branding).
No toolchains, workspace, transfer archives or customer profile files were copied.
This is a broad review branch, not a claim that every platform has been tested.

Post docs/ISSUE-23-UPDATE.md as a comment on the existing audit issue. Review the
diff before committing. Prefer splitting this broad contribution into smaller PRs
if the maintainer requests that. No commit, push, issue comment or PR has been
published automatically.

Create a fork of VenimK/DVForge on GitHub, then from this directory run:

```sh
git remote rename origin upstream
git remote add origin https://github.com/YOUR-GITHUB-NAME/DVForge.git
git status --short
git diff --check
git add app.py builder farm/queue.py farm/worker.py web tests docs/COMMUNITY-REPORT.md docs/REMOTE-ARCHITECTURE.md docs/ISSUE-23-UPDATE.md docs/CONTRIBUTION-READY.md
git commit -m "Improve build reliability, dependency approval and wizard UX"
git push -u origin contribution/build-reliability-and-wizard
```

Open a pull request from your fork's contribution branch to VenimK/DVForge main.
Do not copy private files into this directory before pushing.
