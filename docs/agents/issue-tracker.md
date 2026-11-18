# Issue tracker

GitHub Issues for this repo (`origin`). PRs from outside collaborators as a request surface: **off**.

Use the REST API only, through `gh api`, never `gh issue` or `gh pr` (GraphQL is blocked here):

- Read an issue: `gh api repos/<owner>/<repo>/issues/<n>` and `.../issues/<n>/comments`
- Create an issue: `gh api repos/<owner>/<repo>/issues -f title=... -F body=@<file>`
- Comment: `gh api repos/<owner>/<repo>/issues/<n>/comments -F body=@<file>`
- Labels: `gh api repos/<owner>/<repo>/issues/<n>/labels -f "labels[]=<label>"`
- Blocking edges: write "Blocked by #n" in the issue body (no native blocking links).
- Sub-issues: `gh api repos/<owner>/<repo>/issues/<parent>/sub_issues -F sub_issue_id=<child issue id>`

Never add the `factory` label. Only the owner adds it.
