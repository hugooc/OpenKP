# v1 public-release checklist — closed 2026-05-11

Historical record of the steps taken to flip OpenKP from private development to a public repo at `github.com/hugooc/OpenKP`. All hard blockers closed. The remaining items are post-launch cleanups (item 6).

## 1. README polish — done 2026-05-04

Outer `README.md` rewritten to welcome a non-developer KP member, point at install, and frame audience + scope. Inner `openkp/README.md` rewritten with the current 22-tool inventory, install steps tight enough for Claude Code to walk through, first-things-to-try examples, write-tool preview/commit semantics, and updated project layout.

What's still rough:
- The "First authenticated run" step (4 in the inner README) describes piping stdio MCP requests as a fallback. In practice everyone goes straight to step 5 (Claude Desktop). Could simplify.
- Linux install path is untested. The README says "macOS (tested) or Linux (untested)" — first Linux user will surface anything that breaks.

## 2. PHI history rewrite + public release — done 2026-05-11

**Rewrite (local) done 2026-05-10** via `git filter-repo`. Post-rewrite HEAD `57ede8e`. All commits scrubbed of PHI in blob content and commit messages. `docs/recon/` removed from history via `--invert-paths`. 28 commits total at rewrite time, 527 tests still pass. Full operational record in `private/documentation/recon/session-19.md`.

**Public release via fresh-repo strategy** rather than the originally planned force-push + GC sequence. On 2026-05-11:

1. A new public repo `hugooc/OpenKP` was created.
2. The rewritten history was pushed to the new public repo as its initial state.
3. The old private working repo (whatever its name was) was set aside. The historical private archive at `hugooc/OpenKP-private-archive` retains a 2026-04-25 snapshot of the early original PHI-bearing history (25 commits — only the first phase of development).

**Why this beat the force-push route:** the public repo never contained the PHI commits to begin with, so there's nothing for GitHub GC of unreferenced refs to clean up. No support ticket required. No 1-3 business day wait. No risk of direct-SHA URLs leaking PHI for 90 days. The original force-push plan (documented in earlier versions of this file) assumed flipping the existing private repo to public; the fresh-repo path sidesteps the whole GC problem.

**Author metadata kept by deliberate choice** — `Hugo Campos <2074396+hugooc@users.noreply.github.com>` remains on every commit in the rewritten history. Rewriting author/committer would have required `--name-callback` / `--email-callback`, explicitly opted out of so the project stays attributed to Hugo as the public author.

**Two accepted residuals in the rewritten history:**
- `Hugo Campos` in commit attribution and the occasional Co-Authored-By trailer.
- `https://github.com/hugooc/OpenKP` URL references in HEAD's README badge and this checklist.

Both are explicitly accepted public identifiers.

**LICENSE and prose attribution:** the `Hugo Campos==>Test Patient` blob rule rewrote `openkp/LICENSE`'s copyright line and one mention in this file (now "2026 Test Patient" in both spots). If you want your name restored as the public copyright holder, do it as a small commit on top. The blob rule is gone, so new mentions of "Hugo Campos" going forward won't be touched.

**Mirror backups (informational):**
- `/tmp/openkp-backup-pre-rewrite/` was the local mirror at rewrite time. Self-cleans on reboot; likely already gone.
- `hugooc/OpenKP-private-archive` on GitHub holds the 2026-04-25 partial snapshot. Persistent. Private.
- The complete pre-rewrite history (28 commits including the late-phase PHI) exists only in whatever local clones you happen to have. If the `/tmp` backup is gone and no other clones exist, that history is unrecoverable. The rewritten version is the authoritative version going forward.

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

Today the deploy is direct-upload (wrangler from local). Now that the public repo is live, the Pages project can be switched to GitHub auto-deploy any time via the Cloudflare dashboard (`Settings → Builds & deployments → Connect to Git`) so site edits ship on push. Until then, redeploy via wrangler each time `site/` changes.

Custom domains active: `openkp.org` and `www.openkp.org`, both proxied through Cloudflare with auto-SSL. See session-20 for the operational record.

## 6. Post-launch cleanups

The public repo is live. The following are housekeeping items, none blocking:

- `/tmp/openkp-backup-pre-rewrite/` — self-cleans on reboot; nothing to do.
- `private/rewrite/replacements*.txt` and `private/rewrite/candidates.txt` contain real LHS values from the rewrite. Safe to delete now. The Python/shell scripts and `phi-audit.txt` (counts only) can stay as historical reference if you want.
- `private/documentation/` — your call. Most of it (genesis, sample-questions, screenshots, recon journals) you'll likely keep forever for personal reference.
- **Pages → GitHub auto-deploy:** optional swap from wrangler direct-upload to Git-connected auto-deploy. See item 5.
- **LICENSE + release-checklist attribution:** ~~optional fixup commit to restore "Hugo Campos" as the copyright holder in `openkp/LICENSE` and the one mention in this file (currently both say "Test Patient" due to the PHI-rewrite blob rule). See item 2.~~ **Done 2026-05-27** as part of the PolyForm Noncommercial relicense (see ADR-007). `openkp/LICENSE` now carries the `Required Notice: Copyright (c) 2026 Hugo Campos` line at the top of the new license text. Item 4's historical statement that "openkp/LICENSE exists with standard MIT text" is preserved as a record of v1 launch state.
- **`hugooc/OpenKP-private-archive`:** the partial 2026-04-25 snapshot can be deleted or archived at your discretion. It is not load-bearing for the public repo.
