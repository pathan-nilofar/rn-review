#!/usr/bin/env python3
"""
rn-review — a React Native code reviewer that reads a diff.

Reviews only ADDED lines in a unified diff, which is the whole point: it comments
on what you wrote, not on the file you happened to touch.

Two modes:
  rules  (default)  deterministic checks, no API key, no network, free
  --llm             sends the diff to Claude for the judgement calls rules cannot make

Usage
  git diff main | python3 rn_review.py
  python3 rn_review.py example.diff
  python3 rn_review.py example.diff --llm
  python3 rn_review.py --selftest
"""

import os
import re
import sys
import json
import argparse
import urllib.request

# ── the rules ────────────────────────────────────────────────────────────────
# Each rule is written from a mistake that costs real render performance in
# React Native. `why` has to explain the cost, not just name the pattern —
# a reviewer who only says "don't do this" teaches nobody.

RULES = [
    dict(
        id="inline-object-prop",
        severity="high",
        pattern=re.compile(r"\b(?!style=)\w+=\{\{\s*[\w'\"]"),
        title="Inline object passed as a prop",
        why="A new object literal is a new reference on every render, so the child "
            "re-renders even when nothing changed. React.memo cannot help here.",
        fix="Hoist it to a constant, or wrap it in useMemo with the real dependencies.",
        before='<PostRow options={{ compact: true }} />',
        after='const ROW_OPTS = { compact: true };          // outside the component\n<PostRow options={ROW_OPTS} />',
    ),
    dict(
        id="inline-style-object",
        severity="medium",
        pattern=re.compile(r"style=\{\{"),
        title="Inline style object",
        why="Allocates a new style object every render and skips the native style "
            "registry, so nothing can be cached across renders.",
        fix="Move it into StyleSheet.create outside the component.",
        before='<View style={{ paddingVertical: 12 }} />',
        after='const s = StyleSheet.create({ row: { paddingVertical: 12 } });\n<View style={s.row} />',
    ),
    dict(
        id="inline-arrow-prop",
        severity="high",
        pattern=re.compile(r"\son(?:Press|Change|ChangeText|Scroll|EndReached|Submit\w*)"
                           r"=\{\s*\(?\s*\)?\s*=>"),
        title="Arrow function created inline in a prop",
        why="A new function identity each render. In a FlatList row this defeats "
            "memoisation for every visible row at once.",
        fix="useCallback with stable deps, or a handler defined outside render.",
        before='<PostRow onPress={() => onOpen(post.id)} />',
        after='const handlePress = useCallback(() => onOpen(post.id), [onOpen, post.id]);\n<PostRow onPress={handlePress} />',
    ),
    dict(
        id="index-as-key",
        severity="high",
        pattern=re.compile(r"key=\{\s*(?:index|i|idx)\s*\}"),
        title="Array index used as key",
        why="On reorder or removal React reuses the wrong element, which shows up as "
            "state attached to the wrong row — a bug that is painful to reproduce.",
        fix="Use a stable id from the data.",
        before='{posts.map((post, index) => <PostRow key={index} ... />)}',
        after='{posts.map(post => <PostRow key={post.id} ... />)}',
    ),
    dict(
        id="scrollview-map",
        severity="high",
        pattern=re.compile(r"<ScrollView[\s\S]{0,400}?\.map\("),
        multiline=True,
        title="ScrollView rendering a mapped list",
        why="ScrollView mounts every child up front. With a list of any real size "
            "this blocks the JS thread and grows memory linearly.",
        fix="FlatList, with keyExtractor and getItemLayout where the row height is known.",
        before='<ScrollView>\n  {posts.map(post => <PostRow ... />)}\n</ScrollView>',
        after='<FlatList\n  data={posts}\n  keyExtractor={p => p.id}\n  renderItem={renderRow}       // defined outside render\n/>',
    ),
    dict(
        id="effect-no-deps",
        severity="medium",
        pattern=re.compile(r"useEffect\(\s*\(\)\s*=>\s*\{[\s\S]*?\}\s*\)\s*;"),
        multiline=True,
        title="useEffect with no dependency array",
        why="Runs after every single render. If it sets state or fetches, that is an "
            "infinite loop waiting for the right conditions.",
        fix="Add a dependency array — [] for mount-only, or list what it actually reads.",
        before='useEffect(() => {\n  load();\n});',
        after='useEffect(() => {\n  load();\n}, []);            // [] for mount-only, or list what it reads',
    ),
    dict(
        id="timer-no-cleanup",
        severity="high",
        pattern=re.compile(r"(setInterval|setTimeout|addEventListener|addListener)\s*\("),
        title="Subscription or timer started",
        why="If the matching clear/remove is missing from the effect's cleanup, it keeps "
            "firing after unmount and updates a component that is gone.",
        fix="Return a cleanup function from useEffect that tears it down.",
        confirm="needs a matching clearInterval / clearTimeout / remove in cleanup",
        before='useEffect(() => {\n  const id = setInterval(tick, 5000);\n}, []);',
        after='useEffect(() => {\n  const id = setInterval(tick, 5000);\n  return () => clearInterval(id);   // the missing line\n}, []);',
    ),
    dict(
        id="async-effect",
        severity="medium",
        pattern=re.compile(r"useEffect\(\s*async\b"),
        title="useEffect callback declared async",
        why="An async function returns a promise, and React treats the return value as "
            "the cleanup function. The cleanup silently never runs.",
        fix="Define an async function inside the effect and call it.",
        before='useEffect(async () => {\n  await load();\n}, []);',
        after='useEffect(() => {\n  (async () => { await load(); })();\n}, []);',
    ),
    dict(
        id="promise-no-catch",
        severity="medium",
        pattern=re.compile(r"\.then\((?![\s\S]{0,300}?\.catch\()"),
        multiline=True,
        title="Promise chain with no .catch",
        why="An unhandled rejection in React Native is invisible in release builds — "
            "the screen just stops updating with no error shown to anyone.",
        fix="Add .catch, or use try/await/catch.",
        before='fetchLatest().then(res => setPosts(res.items));',
        after='fetchLatest()\n  .then(res => setPosts(res.items))\n  .catch(err => report(err));',
    ),
    dict(
        id="left-in-console",
        severity="low",
        pattern=re.compile(r"\bconsole\.(log|warn|debug)\("),
        title="console statement left in",
        why="Ships to production, costs a bridge crossing on every call, and can leak "
            "user data into device logs.",
        fix="Remove it, or strip it in the release build.",
        before="console.log('posts', posts);",
        after='// remove it, or guard with __DEV__',
    ),
    dict(
        id="hardcoded-secret",
        severity="critical",
        pattern=re.compile(r"""(?ix)
            (?:api[_-]?key|secret|token|password|bearer)
            \s*[:=]\s*
            ['"][A-Za-z0-9_\-\.]{12,}['"]
        """),
        title="Possible hardcoded secret",
        why="Anything bundled into the app is extractable — a mobile binary is not a "
            "safe place for a credential, however obfuscated.",
        fix="Move it to the backend, or to native secure storage. Then rotate it, "
            "because it is already in git history.",
        before='const TOKEN = "at_live_2f91c4de77ab0031";',
        after='const TOKEN = await Keychain.getGenericPassword();   // and rotate the old one',
    ),
]

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Only these can be turned into an applyable GitHub suggestion. Everything else needs
# a name or a surrounding block this tool cannot see, and a wrong suggestion that
# someone clicks "apply" on is worse than no suggestion at all.
APPLYABLE = {"left-in-console"}
SKIP_FILE = re.compile(r"\.(test|spec)\.[jt]sx?$|__tests__/|\.snap$|node_modules/")


# ── diff parsing ─────────────────────────────────────────────────────────────

def parse_diff(text):
    """Yield (filename, line_number, added_line) for added lines only."""
    filename, lineno = None, 0
    for raw in text.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            filename = path[2:] if path.startswith(("a/", "b/")) else path
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            lineno = int(m.group(1)) if m else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            if filename:
                yield filename, lineno, raw[1:]
            lineno += 1
        elif not raw.startswith("-"):
            lineno += 1


def review(diff_text):
    findings = []
    added = list(parse_diff(diff_text))

    # whole-file added text, so multi-line patterns (ScrollView + .map) can match
    by_file = {}
    for fn, ln, line in added:
        by_file.setdefault(fn, []).append((ln, line))

    for fn, lines in by_file.items():
        if SKIP_FILE.search(fn):
            continue
        if not fn.endswith((".js", ".jsx", ".ts", ".tsx")):
            continue

        blob = "\n".join(l for _, l in lines)
        # offset of each blob line, so a multiline match maps back to a real line number
        offsets, pos = [], 0
        for ln, line in lines:
            offsets.append((pos, ln, line))
            pos += len(line) + 1

        def line_at(offset):
            hit = offsets[0] if offsets else (0, 0, "")
            for o, ln, line in offsets:
                if o <= offset:
                    hit = (o, ln, line)
                else:
                    break
            return hit[1], hit[2]

        for rule in RULES:
            if rule.get("multiline"):
                seen = set()
                for m in rule["pattern"].finditer(blob):
                    ln, line = line_at(m.start())
                    if ln in seen:
                        continue
                    seen.add(ln)
                    findings.append(dict(
                        file=fn, line=ln, rule=rule["id"],
                        severity=rule["severity"], title=rule["title"],
                        why=rule["why"], fix=rule["fix"],
                        confirm=rule.get("confirm"),
                        before=rule.get("before"), after=rule.get("after"),
                        code=line.strip()[:120],
                    ))
            else:
                for ln, line in lines:
                    if rule["pattern"].search(line):
                        findings.append(dict(
                            file=fn, line=ln, rule=rule["id"],
                            severity=rule["severity"], title=rule["title"],
                            why=rule["why"], fix=rule["fix"],
                            confirm=rule.get("confirm"),
                            before=rule.get("before"), after=rule.get("after"),
                            code=line.strip()[:120],
                        ))

    findings.sort(key=lambda f: (SEV_ORDER[f["severity"]], f["file"], f["line"]))
    return findings


# ── optional LLM pass ────────────────────────────────────────────────────────

MODEL = "claude-sonnet-5"   # pinned so results are reproducible

LLM_PROMPT = """You are reviewing a React Native pull request diff.

The deterministic rules already caught: {caught}

Report ONLY what rules cannot catch — logic errors, race conditions, incorrect
hook dependencies, state that will go stale, platform differences between iOS and
Android, accessibility gaps. Skip style opinions and anything already listed.

For each finding give: file, approximate line, one sentence on the defect, and one
sentence on the concrete failure it causes. If nothing of substance, say so plainly.
Be brief. A short review that is right beats a long one that pads.

DIFF:
{diff}"""


def llm_review(diff_text, caught):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return "  (set ANTHROPIC_API_KEY to enable the --llm pass)"

    body = json.dumps({
        "model": MODEL,
        "max_tokens": 1200,
        "messages": [{
            "role": "user",
            "content": LLM_PROMPT.format(
                caught=", ".join(sorted(caught)) or "nothing",
                diff=diff_text[:24000],
            ),
        }],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        return data["content"][0]["text"]
    except Exception as e:
        return f"  (LLM pass failed: {e})"


# ── output ───────────────────────────────────────────────────────────────────

ICON = {"critical": "!!", "high": " !", "medium": " ~", "low": " ."}


def render(findings):
    if not findings:
        return "No issues found in the added lines.\n"
    out = [f"\n{len(findings)} finding(s)\n" + "─" * 62]
    for f in findings:
        out.append(f"\n{ICON[f['severity']]} [{f['severity']}] {f['title']}")
        out.append(f"   {f['file']}:{f['line']}")
        out.append(f"   > {f['code']}")
        out.append(f"   why: {f['why']}")
        out.append(f"   fix: {f['fix']}")
        if f.get("confirm"):
            out.append(f"   check: {f['confirm']}")
        if f.get("after"):
            out.append("")
            for ln in str(f["before"]).splitlines():
                out.append(f"     - {ln}")
            for ln in str(f["after"]).splitlines():
                out.append(f"     + {ln}")
    counts = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    summary = "  ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda kv: SEV_ORDER[kv[0]]))
    out.append("\n" + "─" * 62 + f"\n{summary}\n")
    return "\n".join(out)


# ── GitHub: fetch a PR, post a review ────────────────────────────────────────

GH_API = "https://api.github.com"
PR_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def gh_token():
    """Env var first, then whatever the gh CLI already has. No prompting, no storing."""
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if t:
        return t
    try:
        import subprocess
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def gh_request(path, token, accept="application/vnd.github+json", data=None, method=None):
    req = urllib.request.Request(
        GH_API + path,
        data=json.dumps(data).encode() if data is not None else None,
        method=method or ("POST" if data is not None else "GET"),
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "rn-review",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read().decode()
    return raw if "diff" in accept else json.loads(raw)


def parse_pr_url(url):
    m = PR_URL.search(url)
    if not m:
        raise SystemExit(f"Not a pull request URL: {url}\n"
                         "Expected: https://github.com/owner/repo/pull/123")
    return m.group(1), m.group(2), int(m.group(3))


def fetch_pr(owner, repo, num, token):
    diff = gh_request(f"/repos/{owner}/{repo}/pulls/{num}", token,
                      accept="application/vnd.github.v3.diff")
    meta = gh_request(f"/repos/{owner}/{repo}/pulls/{num}", token)
    return diff, meta


def _comment_body(f):
    """GitHub renders a ```suggestion block as a one-click apply. Only emit one where
    the replacement is unambiguous — otherwise show the shape and let a human write it."""
    body = (f"**{f['severity'].upper()} — {f['title']}**\n\n{f['why']}\n\n"
            f"**Fix:** {f['fix']}")
    if f.get("confirm"):
        body += f"\n\n**Check:** {f['confirm']}"
    if f["rule"] in APPLYABLE:
        body += "\n\n```suggestion\n```"          # delete the line
    elif f.get("after"):
        body += f"\n\n<details><summary>What it should look like</summary>\n\n"
        body += f"```diff\n- {f['before']}\n+ {f['after']}\n```\n\n</details>"
    return body


def post_review(owner, repo, num, sha, findings, token):
    """Inline comments where GitHub accepts them, the rest collected in the body."""
    counts = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    summary = "  ·  ".join(f"**{counts[s]}** {s}"
                           for s in sorted(counts, key=lambda k: SEV_ORDER[k]))

    comments = [{
        "path": f["file"],
        "line": max(1, f["line"]),
        "side": "RIGHT",
        "body": _comment_body(f),
    } for f in findings]

    body = (f"### rn-review\n\n{summary or 'No issues found.'}\n\n"
            f"<sub>Rules pass over added lines only. "
            f"[How it works](https://github.com/pathan-nilofar/rn-review)</sub>")

    blocking = any(f["severity"] in ("critical", "high") for f in findings)
    payload = {
        "commit_id": sha,
        "body": body,
        "event": "COMMENT",          # never REQUEST_CHANGES — a bot should not block a human
        "comments": comments,
    }

    try:
        return gh_request(f"/repos/{owner}/{repo}/pulls/{num}/reviews", token, data=payload)
    except urllib.error.HTTPError as e:
        # a line outside the diff hunk is rejected; fall back to one summary comment
        detail = e.read().decode()[:200]
        lines = "\n".join(
            f"- **{f['severity']}** `{f['file']}:{f['line']}` — {f['title']}. {f['fix']}"
            for f in findings)
        return gh_request(f"/repos/{owner}/{repo}/pulls/{num}/reviews", token,
                          data={"commit_id": sha, "event": "COMMENT",
                                "body": body + "\n\n" + lines +
                                        f"\n\n<sub>inline anchoring failed: {detail}</sub>"})


# ── html report ──────────────────────────────────────────────────────────────

HTML_TMPL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>rn-review — {n} finding(s)</title><style>
:root{{--bg:#07080c;--panel:#11131b;--line:#1d212e;--ink:#eef1f7;--muted:#98a0b4;--dim:#6b7488;
--critical:#f87171;--high:#fb923c;--medium:#fbbf24;--low:#5eead4}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:var(--bg);color:var(--ink);padding:40px 20px}}
.wrap{{max-width:940px;margin:0 auto}}
h1{{font-size:26px;letter-spacing:-.03em;margin-bottom:6px}}
.meta{{color:var(--dim);font-size:13.5px;margin-bottom:28px}}
.bar{{display:flex;height:8px;border-radius:99px;overflow:hidden;margin-bottom:10px;background:var(--line)}}
.bar i{{display:block}}
.counts{{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--muted);margin-bottom:32px}}
.counts b{{color:var(--ink)}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}}
.f{{background:var(--panel);border:1px solid var(--line);border-left-width:3px;
border-radius:10px;padding:20px 22px;margin-bottom:14px}}
.f h3{{font-size:16.5px;letter-spacing:-.01em;margin-bottom:4px;display:flex;
align-items:center;gap:9px;flex-wrap:wrap}}
.sev{{font-size:10.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
padding:3px 8px;border-radius:5px}}
.loc{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;
color:var(--dim);margin-bottom:12px}}
pre{{background:#0a0c12;border:1px solid var(--line);border-radius:8px;padding:12px 14px;
overflow-x:auto;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:12.5px;color:#c4b8ff;margin-bottom:12px}}
pre.d{{margin-top:6px;line-height:1.55}}
pre.d span{{display:block}}
.del{{color:#f87171;background:rgba(248,113,113,.07)}}
.add{{color:#5eead4;background:rgba(94,234,212,.07)}}
.row{{display:flex;gap:9px;font-size:14px;margin-bottom:6px}}
.row span:first-child{{color:var(--dim);min-width:44px;flex-shrink:0}}
.row span:last-child{{color:var(--muted)}}
.check{{color:var(--medium)!important}}
.none{{text-align:center;padding:60px 20px;color:var(--muted)}}
footer{{margin-top:36px;padding-top:20px;border-top:1px solid var(--line);
font-size:12.5px;color:var(--dim)}}
footer a{{color:var(--low);text-decoration:none}}
</style></head><body><div class="wrap">
<h1>rn-review</h1>
<p class="meta">{n} finding(s) across {files} file(s) &middot; {when}</p>
{bar}{counts}{body}
<footer>Generated by <a href="https://github.com/pathan-nilofar/rn-review">rn-review</a>
&middot; rules pass only, no model involved &middot; added lines only</footer>
</div></body></html>"""

SEV_COLOR = {"critical": "#f87171", "high": "#fb923c", "medium": "#fbbf24", "low": "#5eead4"}


def render_html(findings, path):
    import datetime, html as H
    when = datetime.datetime.now().strftime("%d %b %Y, %H:%M")
    files = len({f["file"] for f in findings})

    counts = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1

    if findings:
        total = len(findings)
        bar = '<div class="bar">' + "".join(
            f'<i style="width:{counts[s]/total*100:.1f}%;background:{SEV_COLOR[s]}"></i>'
            for s in sorted(counts, key=lambda k: SEV_ORDER[k])) + "</div>"
        cnt = '<div class="counts">' + "".join(
            f'<span><span class="dot" style="background:{SEV_COLOR[s]}"></span>'
            f'<b>{counts[s]}</b> {s}</span>'
            for s in sorted(counts, key=lambda k: SEV_ORDER[k])) + "</div>"
        rows = []
        for f in findings:
            c = SEV_COLOR[f["severity"]]
            chk = (f'<div class="row"><span>check</span>'
                   f'<span class="check">{H.escape(f["confirm"])}</span></div>'
                   if f.get("confirm") else "")
            diff = ""
            if f.get("after"):
                minus = "".join(f'<span class="del">- {H.escape(l)}</span>\n'
                                for l in str(f["before"]).splitlines())
                plus = "".join(f'<span class="add">+ {H.escape(l)}</span>\n'
                               for l in str(f["after"]).splitlines())
                diff = f'<div class="row"><span>code</span></div><pre class="d">{minus}{plus}</pre>' 
            rows.append(
                f'<div class="f" style="border-left-color:{c}">'
                f'<h3><span class="sev" style="background:{c}22;color:{c}">{f["severity"]}</span>'
                f'{H.escape(f["title"])}</h3>'
                f'<div class="loc">{H.escape(f["file"])}:{f["line"]}</div>'
                f'<pre>{H.escape(f["code"])}</pre>'
                f'<div class="row"><span>why</span><span>{H.escape(f["why"])}</span></div>'
                f'<div class="row"><span>fix</span><span>{H.escape(f["fix"])}</span></div>'
                f'{chk}{diff}</div>')
        body = "".join(rows)
    else:
        bar = cnt = ""
        body = '<div class="none">No issues found in the added lines.</div>'

    open(path, "w", encoding="utf-8").write(
        HTML_TMPL.format(n=len(findings), files=files, when=when,
                         bar=bar, counts=cnt, body=body))
    return path


# ── self-check ───────────────────────────────────────────────────────────────

SAMPLE = """diff --git a/src/Feed.tsx b/src/Feed.tsx
--- a/src/Feed.tsx
+++ b/src/Feed.tsx
@@ -1,4 +1,14 @@
+const API_KEY = "sk_live_9f3ab21c77de4410";
+export function Feed({ posts }) {
+  useEffect(() => { fetchPosts(); });
+  return (
+    <ScrollView>
+      {posts.map((p, index) => (
+        <Row key={index} style={{ padding: 8 }} config={{ dark: true }}
+             onPress={() => open(p.id)} />
+      ))}
+    </ScrollView>
+  );
+}
"""


def selftest():
    found = review(SAMPLE)
    ids = {f["rule"] for f in found}
    expected = {
        "hardcoded-secret", "effect-no-deps", "scrollview-map",
        "index-as-key", "inline-style-object", "inline-object-prop",
        "inline-arrow-prop",
    }
    missing = expected - ids
    assert not missing, f"rules failed to fire: {missing}"

    # added lines only — a removed line must never be reported
    removed = SAMPLE.replace("+const API_KEY", "-const API_KEY")
    assert not any(f["rule"] == "hardcoded-secret" for f in review(removed)), \
        "reported a removed line"

    # test files are skipped
    test_diff = SAMPLE.replace("src/Feed.tsx", "src/Feed.test.tsx")
    assert review(test_diff) == [], "did not skip a test file"

    # critical sorts above low
    assert found[0]["severity"] == "critical", "findings not sorted by severity"

    # a multi-line useEffect with no dep array must still be caught
    ml = """diff --git a/a.tsx b/a.tsx
--- a/a.tsx
+++ b/a.tsx
@@ -1,2 +1,6 @@
+  useEffect(() => {
+    load();
+    track();
+  });
"""
    assert any(f["rule"] == "effect-no-deps" for f in review(ml)), \
        "missed a multi-line useEffect"

    # an inline style must not be double-reported as a generic object prop
    st = """diff --git a/b.tsx b/b.tsx
--- a/b.tsx
+++ b/b.tsx
@@ -1,2 +1,2 @@
+  <View style={{ flex: 1 }} />
"""
    ids_st = [f["rule"] for f in review(st)]
    assert ids_st.count("inline-style-object") == 1 and "inline-object-prop" not in ids_st, \
        f"duplicate report on inline style: {ids_st}"

    # the html report must render without raising, and contain a real finding
    import tempfile, os as _os
    tmp = _os.path.join(tempfile.gettempdir(), "_rnreview_selftest.html")
    render_html(found, tmp)
    page = open(tmp, encoding="utf-8").read()
    assert "hardcoded secret" in page.lower() and "<html" in page, "html report is malformed"
    assert "No issues found" in open(render_html([], tmp), encoding="utf-8").read(), \
        "empty report does not render"
    _os.remove(tmp)

    print(f"selftest passed — {len(found)} findings, {len(ids)} distinct rules fired, html report ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Review a React Native diff.")
    ap.add_argument("diff", nargs="?", help="diff file, or a GitHub PR URL (default: stdin)")
    ap.add_argument("--post", action="store_true",
                    help="post the review back to the PR (requires a PR URL)")
    ap.add_argument("--llm", action="store_true", help="add a Claude pass for judgement calls")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--html", metavar="FILE", help="write a visual report you can open in a browser")
    ap.add_argument("--selftest", action="store_true", help="run the built-in checks")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    pr = None
    if a.diff and "github.com" in a.diff and "/pull/" in a.diff:
        token = gh_token()
        if not token:
            print("No GitHub token. Set GITHUB_TOKEN, or run: gh auth login")
            return 2
        owner, repo, num = parse_pr_url(a.diff)
        print(f"Fetching {owner}/{repo} PR #{num} ...")
        text, meta = fetch_pr(owner, repo, num, token)
        pr = dict(owner=owner, repo=repo, num=num, token=token,
                  sha=meta["head"]["sha"], title=meta["title"],
                  files=meta.get("changed_files", 0))
        print(f'  "{pr["title"]}"  ·  {pr["files"]} file(s) changed\n')
    elif a.diff:
        text = open(a.diff, encoding="utf-8").read()
    else:
        text = sys.stdin.read()

    if not text.strip():
        print("Nothing to review. Try:\n"
              "  git diff main | python3 rn_review.py\n"
              "  python3 rn_review.py https://github.com/owner/repo/pull/12")
        return 1

    findings = review(text)

    if a.post:
        if not pr:
            print("--post needs a PR URL, not a diff file.")
            return 2
        if not findings:
            print("Nothing to post — no findings.")
            return 0
        res = post_review(pr["owner"], pr["repo"], pr["num"], pr["sha"], findings, pr["token"])
        print(f"Posted {len(findings)} finding(s) → {res.get('html_url', 'ok')}")
        return 1 if any(f["severity"] in ("critical", "high") for f in findings) else 0

    if a.html:
        render_html(findings, a.html)
        print(f"Report written to {a.html}  ({len(findings)} finding(s))")
    elif a.json:
        print(json.dumps(findings, indent=2))
    else:
        print(render(findings))
        if a.llm:
            print("Claude pass — judgement calls the rules cannot make")
            print("─" * 62)
            print(llm_review(text, {f["rule"] for f in findings}))

    # non-zero exit if anything serious, so CI can fail the build
    return 1 if any(f["severity"] in ("critical", "high") for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
