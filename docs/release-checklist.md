# v1 public-release checklist

Required steps before flipping `github.com/hugooc/OpenKP` from private to public. Status as of 2026-05-11.

## 1. README polish — done 2026-05-04

Outer `README.md` rewritten to welcome a non-developer KP member, point at install, and frame audience + scope. Inner `openkp/README.md` rewritten with the current 22-tool inventory, install steps tight enough for Claude Code to walk through, first-things-to-try examples, write-tool preview/commit semantics, and updated project layout.

What's still rough:
- The "First authenticated run" step (4 in the inner README) describes piping stdio MCP requests as a fallback. In practice everyone goes straight to step 5 (Claude Desktop). Could simplify.
- Linux install path is untested. The README says "macOS (tested) or Linux (untested)" — first Linux user will surface anything that breaks.

## 2. PHI history rewrite — local rewrite DONE 2026-05-10, force-push pending

**Local state:** git history rewritten via `git filter-repo`. HEAD `57ede8e`. All commits scrubbed of PHI in blob content and commit messages. `docs/recon/` removed from history via `--invert-paths`. 28 commits total. 527 tests still pass. See `private/documentation/recon/session-19.md` for the full operational record.

**Author metadata kept by deliberate choice** — `Hugo Campos <2074396+hugooc@users.noreply.github.com>` remains on every commit. Rewriting author/committer would have required `--name-callback` / `--email-callback`, which we explicitly opted out of so the project stays attributed to Hugo as the public author.

**Two accepted residuals in the rewritten history:**
- `Hugo Campos` in commit attribution and the occasional Co-Authored-By trailer.
- `https://github.com/hugooc/OpenKP` URL references in HEAD's README badge and this checklist (a `Restore github.com/hugooc URLs` fixup commit reverted the URL after the substring rule overzealously rewrote it).

Both are explicitly accepted public identifiers.

**Heads up — LICENSE and prose attribution:** the `Hugo Campos==>Test Patient` blob rule also rewrote `openkp/LICENSE`'s copyright line and one mention in this file (originally "2026 Hugo Campos copyright line", now "2026 Test Patient"). If you want your name restored in those non-PHI public attribution spots before the flip, do it as a small commit on top — same pattern as the URL fixup commit. The blob rule is gone, so any new mentions of "Hugo Campos" in commits going forward will not be touched.

**Mirror backup:** `/tmp/openkp-backup-pre-rewrite/` (2.3 MB bare clone of the repo state immediately before the rewrite). Self-cleans on reboot. Keep until GitHub GC is confirmed.

### What still needs to happen (in order):

```bash
# Step 1 — push the rewritten history to the (still-private) GitHub repo
git push --force-with-lease origin main
```

**Push does NOT make the repo public.** It only updates the content of the existing private repo. Visibility is a separate Settings toggle on github.com.

```bash
# Step 2 — file a GitHub support ticket
```

Open a ticket at https://support.github.com asking them to GC unreferenced refs. Without this, the original PHI-bearing commits remain accessible via direct SHA URLs for ~90 days even though they're not reachable from any branch. GitHub support's standard turnaround is 1-3 business days.

Suggested ticket text:

> I just force-pushed a rewritten history to github.com/hugooc/OpenKP (private) using git-filter-repo to remove sensitive personal data from older commits. The original commits still appear to be retrievable via direct SHA URLs. Could you please run garbage collection on the repository so the unreferenced commits become inaccessible? Thanks.

```bash
# Step 3 — verify GC complete (after GitHub support confirms)
# Pick a known pre-rewrite SHA (any commit hash from `git -C /tmp/openkp-backup-pre-rewrite log --oneline`)
git fetch origin <old-pre-rewrite-sha>   # should fail with "Could not find" or similar
```

```bash
# Step 4 — final pre-flip audit
git log -p | grep -iE "<known PHI patterns>"   # should return empty
git ls-files | xargs grep -l "<known PHI patterns>"   # should return empty
# Walk the README install steps from a fresh clone in a clean directory
```

### Step 5 — flip to public

GitHub web UI → Settings → "Danger Zone" → "Change repository visibility" → Public. Type the repo name to confirm.

**This is the actual irreversible reputation moment.** Do it deliberately, with the audit fresh.

## 3. PHI outside the repo (informational, not a publication concern)

These have always lived outside the repo (gitignored or sidecar) and will continue to:

- `docs/research/captures/*.har` — HAR captures contain Kaiser passwords, session cookies, full names, addresses, MRNs, GUIDs, message bodies, lab values. Stay on Hugo's Mac, gitignored.
- `private/documentation/recon/session-*.md` — recon journals with clinical narrative, real provider names, dates of service. Consolidated 2026-05-10 from `~/Desktop/OpenKP Documentation/`. Whole `private/` tree gitignored.
- `private/rewrite/` — replacement tables and audit scripts from the 2026-05-10 history rewrite. Keep until flip-public is complete; can be deleted or archived after.
- `~/.openkp/` — runtime data dir (Kaiser session cookies, audit log, downloaded PDFs). Always lived here.
- macOS Keychain `openkp` entry — Hugo's KP password.

## 4. License + attribution — done 2026-05-04

`openkp/LICENSE` exists with standard MIT text. The copyright line was originally "Copyright (c) 2026 Hugo Campos" but the PHI rewrite's `Hugo Campos==>Test Patient` rule also rewrote it. See item 2's "Heads up — LICENSE" note. Both READMEs reference it.

## 5. Website — done 2026-05-11

Static single-page landing site at [openkp.org](https://openkp.org), hosted on Cloudflare Pages. Source under `site/` (committed in `25a7259`). Codex drafted v1, two review passes aligned voice (CAIHL framing, MCP-client-agnostic at runtime, Claude Code as install assistant, lighter editorial tone in Limits). Favicon + og:image wired into `<head>`, canonical URL set, www → apex redirect via `_redirects`. No build step, no JS framework.

Deploy command (run from repo root):

```bash
wrangler pages deploy site --project-name=openkp --branch=main --commit-dirty=true
```

Today the deploy is direct-upload (wrangler from local). After the PHI force-push and flip-public land, switch the Pages project to auto-deploy from GitHub (`Settings → Builds & deployments → Connect to Git`) so site edits ship on push. Until then, redeploy via wrangler each time `site/` changes.

Custom domains active: `openkp.org` and `www.openkp.org`, both proxied through Cloudflare with auto-SSL. See session-20 for the operational record.

## 6. Cleanup after flip-public

Once the repo is public and you've verified everything is in order:

- `rm -rf /tmp/openkp-backup-pre-rewrite` (or just reboot — it self-cleans).
- `rm private/rewrite/replacements*.txt private/rewrite/candidates.txt` — these contain real LHS values. The Python/shell scripts and `phi-audit.txt` (counts only) can stay as historical reference if you want.
- `private/documentation/` — your call. Most of it (genesis, sample-questions, screenshots) you'll likely keep forever for personal reference.
