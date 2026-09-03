# rn-review

A React Native code reviewer that reads a diff and comments on the added lines.

I lead code review on a mobile team and mentor five developers. The same handful of
mistakes came back every sprint — an inline object prop that quietly defeats
`React.memo`, an index used as a key, a `ScrollView` mounting two hundred rows. None
of them are hard to spot. All of them are easy to miss at 6pm on a Friday.

So I wrote down the checks I was doing by hand.

```
$ git diff main | python3 rn_review.py

9 finding(s)
──────────────────────────────────────────────────────────────

!! [critical] Possible hardcoded secret
   src/screens/Feed.tsx:12
   > const ANALYTICS_TOKEN = "at_live_2f91c4de77ab0031";
   why: Anything bundled into the app is extractable — a mobile binary is not a
        safe place for a credential, however obfuscated.
   fix: Move it to the backend, or to native secure storage. Then rotate it,
        because it is already in git history.

 ! [high] ScrollView rendering a mapped list
   src/screens/Feed.tsx:12
   > <ScrollView> ... .map(
   why: ScrollView mounts every child up front. With a list of any real size this
        blocks the JS thread and grows memory linearly.
   fix: FlatList, with keyExtractor and getItemLayout where the row height is known.
```

## Why an agent and not a linter

ESLint already covers the mechanical half of this, and it should stay in the pipeline.
Two things it does not do:

**It does not read a diff.** ESLint reports on files. On a mature codebase that means
four hundred pre-existing warnings, and the six lines you actually wrote today are lost
in them. This tool reports on added lines only, so the output is always about the change
under review.

**It does not explain the cost.** `react/jsx-no-bind` tells you not to bind in JSX.
It does not tell you that in a `FlatList` row it breaks memoisation for every visible
row at once. The `why` field is the point of this tool — I want a junior developer to
finish reading a comment knowing something they did not know before, not just having
been told off.

## Two modes

**Rules** (default) — deterministic, no API key, no network, free, instant. Ten checks
covering render performance, lifecycle correctness and one security case.

**`--llm`** — adds a Claude pass for the judgement calls rules cannot make: stale closures,
race conditions, wrong hook dependencies, iOS/Android behaviour differences. It is told
what the rules already caught so it does not repeat them, and told to say "nothing of
substance" rather than pad.

The split is deliberate. Anything expressible as a pattern should be a pattern — it is
free, instant and never wrong in a different way each run. The model is for what is left.

## Install

None. Python 3.8+, standard library only.

```bash
git diff main | python3 rn_review.py     # review your branch
python3 rn_review.py example.diff        # try the sample
python3 rn_review.py --html report.html  # visual report, open in a browser
python3 rn_review.py --json              # machine-readable
python3 rn_review.py --selftest          # built-in checks
```

For the LLM pass:

```bash
export ANTHROPIC_API_KEY=sk-...
git diff main | python3 rn_review.py --llm
```

### The report

`--html` writes a standalone report — severity bar, colour-coded findings, the offending
line, and the reasoning. One file, no assets, no server. Useful for attaching to a PR, or
for looking at a whole release branch at once.

Exits non-zero when anything `critical` or `high` is found, so it can gate CI:

```yaml
- run: git diff origin/main | python3 rn_review.py
```

## What it checks

| Rule | Severity | The actual cost |
|---|---|---|
| Hardcoded secret | critical | Extractable from any shipped binary |
| Inline object prop | high | New reference each render, `React.memo` cannot help |
| Inline arrow in a prop | high | New function identity, breaks row memoisation |
| Index as key | high | React reuses the wrong element on reorder |
| `ScrollView` + `.map` | high | Mounts every child, blocks the JS thread |
| Timer or subscription | high | Fires after unmount without a cleanup |
| `useEffect` with no deps | medium | Runs every render; an infinite loop waiting to happen |
| `async useEffect` | medium | Returns a promise, so cleanup silently never runs |
| `.then` with no `.catch` | medium | Rejections are invisible in release builds |
| Console left in | low | Ships to production, costs a bridge crossing |

## What it gets wrong

Being specific about this, because a reviewer that hides its failure modes gets
switched off after the third false positive.

- **`timer-no-cleanup` cannot see the cleanup.** It flags every `setInterval` and
  `addListener` and asks you to confirm. Correct cleanups get flagged too. Deliberate —
  a missed unmount leak costs more than three seconds of reading.
- **Regex is not a parser.** A pattern inside a string literal or a comment can match.
  Rare in practice, and worth it for having no dependencies and no parse step.
- **Diff context only.** It sees added lines, so it cannot know that the object you
  passed inline is already memoised twenty lines above.
- **No cross-file reasoning.** It reviews a diff, not an architecture.

The LLM pass covers some of this, at the cost of an API call and non-determinism.

## Design notes

- **Added lines only.** Reviewing whole files buries the change in pre-existing noise.
- **Every rule carries `why` and `fix`.** A finding without a cost is nagging.
- **Severity drives the exit code**, so CI blocks on `critical` and `high` and stays
  quiet about a stray `console.log`.
- **Model pinned** to `claude-sonnet-5` so a review is reproducible.
- **Test files are skipped** — an index key in a fixture is not a bug.
- **One self-check, no framework.** `--selftest` asserts that every rule fires, that
  removed lines are never reported, that test files are skipped, that findings sort by
  severity, and that a multi-line `useEffect` is still caught.

## Next

- Post findings as inline GitHub PR comments
- Trend the report across releases, so the rule that keeps firing becomes visible
- A project config for per-repo severity
- Rules for `react-native-reanimated` worklets and `FlatList` prop misuse

---

Built by [Nilofar Pathan](https://pathan-nilofar.github.io) — senior React Native
developer, six years across iOS and Android.
