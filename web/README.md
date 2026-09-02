# `web/` — Lane E

The reconciliation UI. Next.js App Router, TypeScript, Tailwind v4, shadcn/ui
(`base-nova` style on Base UI). No chart library: the tier bars are CSS and the
netting diagram is hand-rolled SVG.

Ten routes: `/` (the run list), `/method`, `/uploads`, and seven under
`/runs/[id]` — the summary, `tiers`, `exceptions`, `records`, `settlements`,
`analyst` and `drift`. `npm run build` lists eleven, the eleventh being
`/_not-found`.

```bash
npm run dev      # http://localhost:3000, MSW answering the API
npm run build    # production build + typecheck
npm run lint
```

## The single API boundary

Every network call in this app goes through `lib/api.ts`. No component calls
`fetch` directly. That is the rule that makes the mock → live swap an
environment change rather than a refactor.

| Variable | Default | Meaning |
|---|---|---|
| `NEXT_PUBLIC_API_BASE` | `http://localhost:8000` | Where the API lives. |
| `NEXT_PUBLIC_API_MOCKING` | `enabled` | `disabled` turns the MSW worker off and lets requests reach the real API. |

### `NEXT_PUBLIC_*` IS INLINED AT **BUILD** TIME. READ THIS BEFORE RECORDING ANYTHING.

Both variables are substituted into the client bundle by `npm run build` and are
**not** read again at `npm start`. Setting them only on `npm start` has no
effect whatsoever: the JavaScript being served already contains whatever was
baked in, so the console will happily answer every screen from MSW while the
terminal shows the real API sitting idle, and **every number on screen is
fiction**.

`npm run dev` is the exception, and it is the reason this was easy to miss — the
dev server re-reads the value per compile, so passing it to `dev` alone works
and taught the wrong lesson.

For a production build, pass them to **both** commands (or put them in
`web/.env.production.local`).

```bash
# Terminal 1 — the real API, from the repository root
cd /e/RazorPay
python -m uv run uvicorn api.main:app --port 8000

# Terminal 2 — build AND serve with the flag on both
cd /e/RazorPay/web
NEXT_PUBLIC_API_MOCKING=disabled NEXT_PUBLIC_API_BASE=http://localhost:8000 npm run build
NEXT_PUBLIC_API_MOCKING=disabled NEXT_PUBLIC_API_BASE=http://localhost:8000 npm start
```

PowerShell, where the environment is set once and both commands inherit it:

```powershell
cd E:\RazorPay
python -m uv run uvicorn api.main:app --port 8000    # terminal 1

cd E:\RazorPay\web                                   # terminal 2
$env:NEXT_PUBLIC_API_MOCKING = 'disabled'
$env:NEXT_PUBLIC_API_BASE    = 'http://localhost:8000'
npm run build
npm start
```

**Verify the bundle rather than trusting the terminal.** The built client chunk
contains the inlined values literally, so grep for them:

```bash
grep -c '"API_MOCKING_ENABLED",0,!1' web/.next/static/chunks/*.js   # must be 1 somewhere
curl -s http://localhost:8000/api/runs | head -c 200                # real run ids, not mock ones
```

`,0,!1` is `false` — mocks off, which is what you want. If you find `,0,!0`
instead, MSW is still on and the build was made without the flag. The same chunk
carries `let t="http://localhost:8000"` when the base URL took. Then open
`http://localhost:3000` and confirm the browser network panel shows requests to
`localhost:8000` and no `mockServiceWorker.js` interception.

For development against the live API, one command is enough:

```bash
NEXT_PUBLIC_API_MOCKING=disabled NEXT_PUBLIC_API_BASE=http://localhost:8000 npm run dev
```

There is deliberately **no** Next.js rewrite, proxy or `no-cors` fetch mode
here. Local-development CORS belongs to Lane D; routing around it would hide
the bug and diverge from the deployed setup. `api/main.py` already allows
`http://localhost:3000`; another origin needs `RECON_CORS_ORIGINS`.

**Turn the LLM analyst toggle OFF in the New run dialog** unless you are
demonstrating the analyst. With a live `GEMINI_API_KEY` in `.env` a run stops at
`55% · llm analyst` for several minutes through four retries and, on the free
tier, is likely to end in a 503 anyway. With the toggle off a 500-record run
completes in a few seconds.

## Types come from the contract

`lib/api-types.ts` is generated, never hand-edited. `api/openapi.yaml` is a
shared contract under change control — read it, generate from it, escalate if
it looks wrong.

```bash
npx openapi-typescript ../api/openapi.yaml -o lib/api-types.ts
```

Regenerate after any rebase that touched the contract — not when something
looks wrong. The generated file once sat three schemas and three required
`Metrics` fields behind the contract for as long as nobody ran the generator,
and the "regenerating breaks `lib/api.ts`, which is exactly what should happen"
safety net in that module never fired because nothing broke what was never
regenerated. `lib/types.ts` re-exports
the generated schema types under short names so no component reaches into
`components["schemas"][...]` inline.

## Conventions that will make the UI lie if you get them wrong

- **Money is an integer number of paise.** `₹493.20` arrives as `49320`. There
  is exactly one formatter, `formatINR` in `lib/money.ts`. Two formatters is two
  roundings.
- **Rates are `0.0`–`1.0` floats**, not percentages.
- **`credit`/`debit` are unsigned magnitudes**; a PSP `amount` is signed.
- **The residual is `reconstruction − net`**, in that order, everywhere, where
  `net` is the wire field and the wire field is the **bank credit**. One
  definition, `residualOf` in `lib/explorer.ts`, used by the listing, the
  breakdown table, the waterfall and the diagram alike. Positive means the batch
  reconstructs to more than the bank actually credited. This matches the
  engine's own evidence line — `residual delta=50 paise (net - credit)`, where
  its `net` is the reconstruction — which the breakdown panel quotes verbatim
  two inches below the number, so the two must agree in sign as well as in
  magnitude. The netting diagram used to hold a second copy of this arithmetic
  with the opposite sign; it does not any more.
- **`Metrics` carries three `itc_*_paise` fields** and they are money like every
  other paise field: integer in, `formatINR` out, Indian grouping.
  `itc_variance_paise` is **signed and is negative on real data** — render the
  sign, never the magnitude.
- **`DriftReport.narrative` may be null and the UI never fills the gap.** The
  drift endpoint runs no model, so it is always null. `material` is computed by
  `core/drift/compare.py` against named thresholds; nothing in `web/`
  recomputes it or applies a threshold of its own.
- **`RunSummary.metrics` is nullable.** A run still executing has no metrics;
  rendering `0%` instead of a loading state is a lie.
- **`narration` renders verbatim**, double spaces and all. The garbling is the
  data.
- **`failed_check` is a typed enum**, read directly. `verifier_reason` is prose
  for a human and is never parsed.

## Mocks

`mocks/handlers.ts`, `mocks/explorer.ts` and `mocks/uploads.ts` together
implement **16 of the contract's 19 operations, across 14 of its 17 paths**
(counted from `api/openapi.yaml`). Three operations have no mock:

- `GET /api/runs/{id}/matches` — no console route calls it, so nothing in the UI
  depends on it, but a page that starts calling it will fall through the mocks
  and must add a handler.
- `POST /api/auth/login` and `POST /api/auth/logout` — the console has no login
  screen. Authentication is off by default (`RECON_AUTH` unset) and the console
  is built for that configuration; mocking a login would be mocking a surface
  the UI does not have.

Fixtures are generated deterministically from a seeded PRNG in
`mocks/fixtures.ts`, so a given run id always produces the same rows and the
exception table pages over a stable `ORDER BY exception_id`.

Run `run_c73b02` carries **5,000 exceptions** and is the fixture the exception
table's pagination is verified against.

The drift mock reproduces `core/drift/compare.py`'s four materiality rules with
its own constants rather than inventing thresholds, so the flags the console
renders under MSW are the flags the live API would send. All three answers are
reachable from the seeded history: a report between the two seed-42 500-record
runs, a 404 for a run with no earlier run on its dataset, and a 409 for any pair
of different sizes.

## `/uploads` — the front door

`/uploads` is where a merchant's own file enters the system: drag-drop or
file-pick, detection by header shape with the confidence shown, the quarantined
rows reviewable with their raw text, and a run started over the selected files.
It is the one route that is not scoped to a run.

A run started there reports `metrics: null` **permanently** and `seed: -1`.
Neither is a bug and neither may be rendered as a number: there is no ground
truth for a merchant's own files, so no rate is measured, and nothing generated
the records, so there is no seed. `lib/labels.ts:isFromUploads` is the test and
the summary page has its own empty state for it, distinct from "still
executing" and from "failed".

## `/runs/{id}/drift` — comparing two runs

`/runs/{id}/drift` compares a run against a baseline. Omitting `?against`
sends no parameter at all and lets the API pick the previous completed run on
the same dataset — the client could not reproduce that choice if it wanted to,
because `RunSummary` carries no `dataset_id`.
