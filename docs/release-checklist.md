# v1 public-release checklist

Required steps before flipping `github.com/testuser/OpenKP` from private to public. Status as of 2026-05-04.

## 1. README polish (done 2026-05-04)

Outer `README.md` rewritten to welcome a non-developer KP member, point at install, and frame audience + scope. Inner `openkp/README.md` rewritten with the current 22-tool inventory, install steps tight enough for Claude Code to walk through, first-things-to-try examples, write-tool preview/commit semantics, and updated project layout.

What's still rough:
- The "First authenticated run" step (4 in the inner README) describes piping stdio MCP requests as a fallback. In practice everyone goes straight to step 5 (Claude Desktop). Could simplify.
- Linux install path is untested. The README says "macOS (tested) or Linux (untested)" — first Linux user will surface anything that breaks.

## 2. PHI history rewrite (not started — REQUIRED before public)

Working-state PHI scrub is **complete** as of 2026-05-04 (this commit removes recon/, scrubs DOB / GUID / MRN / ZIP / provider names from current files).

**But:** the entire git history before this commit still contains the original real values in old blob objects. `git log -p` reveals everything. Until the history is rewritten, the repo cannot be made public.

What needs doing:

```
# 1. Local mirror backup before any rewrite
git clone --mirror . /tmp/openkp-backup-pre-rewrite

# 2. Install git-filter-repo (the actively maintained replacement for git-filter-branch)
brew install git-filter-repo

# 3. Run filter-repo with replacement rules
git filter-repo \
  --replace-text replacements.txt \
  --invert-paths --path docs/recon

# replacements.txt contents:
#   1970-01-01==>1970-01-01
#   1234567==>1234567
#   14776978==>12345678
#   90210==>90210
#   PROVIDER ONE MD==>DR. EXAMPLE PROVIDER
#   PROVIDER TWO MD==>DR. SECOND EXAMPLE
#   (any others surfaced during a final pre-publish audit)
```

After local rewrite:

1. Force-push to origin (`git push --force-with-lease origin main`).
2. Open a private GitHub support ticket asking them to garbage-collect unreferenced refs. Without this step, the original commits remain accessible via direct SHA URLs for ~90 days. GitHub support's standard turnaround is 1-3 business days.
3. Verify by attempting `git fetch origin <old-sha>` — should fail.
4. Only after GC confirmation: flip repo to public.

## 3. PHI in pre-rewrite captures and recon files (not in repo)

These live outside the repo (gitignored), so they're not a publication concern, but the user should know where they are:

- `docs/research/captures/*.har` — HAR captures contain Kaiser passwords (in form-post bodies), session cookies, full names, addresses, MRNs, GUIDs, message bodies, lab values. Stay on Hugo's Mac, gitignored.
- `~/Desktop/OpenKP Documentation/recon/session-*.md` — moved out of the repo on 2026-05-04. Contain clinical narrative, real provider names, dates of service. Stay local.

## 4. License + attribution (done 2026-05-04)

`openkp/LICENSE` exists with standard MIT text and a 2026 Test Patient copyright line. Both READMEs reference it.

## 5. Final pre-flip audit

Right before flipping public:

- `git log -p | grep -iE "<known PHI patterns>"` — empty.
- `git ls-files | xargs grep -l "<known PHI patterns>"` — empty.
- Repo cloned fresh elsewhere, walked through the README install steps end-to-end.
