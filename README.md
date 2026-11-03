# PaisaLens

Ask questions about your own money in plain English and get exact numbers, with the SQL that produced them.

**[Try the live demo](https://mymoneyanalysis.onrender.com)**, which opens on made-up data. The first load can be slow. To keep your own statements on your own machine, [run it on your computer](#using-your-own-statement).

**Status: early v1.** You can start separate analyses from PDF or CSV statements, browse a dashboard, spot recurring payments, correct categories and ask numeric questions with follow-ups, through a web page, an HTTP API or the command line. Document search (RAG) isn't built yet ([Roadmap](#roadmap)).

> The repo contains synthetic data only. Keep real statements out of it.

## Why this design

Semantic search can find transactions that look relevant, but it can't add them up. "Total spent on Swiggy in 2026" needs an exact `SUM`, so numeric questions go through text-to-SQL.

Correct SQL isn't enough either. A plain `SUM` of all debits counts credit card purchases twice (once when bought, once when the bill is paid), treats moving money between your own accounts as spending, and ignores refunds. PaisaLens therefore classifies each transaction at ingestion and gives the model a `spending` view that already applies those rules.

## Getting started

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env        # add your API key

python scripts/generate_synthetic_data.py
python -m app.ingest.statements data/sample/transactions.csv
python -m app.agent.loop "How much did I spend on food delivery last month?"
```

The sample data ends in October 2026, so set `AS_OF_DATE=2026-11-02` in `.env` for relative dates to work against it.

To run the web page and API locally, start the service and open http://localhost:8000:

```bash
uvicorn app.main:app
```

Or run the production image with Docker, using the key in `.env`:

```bash
docker compose up --build
```

### API

```bash
curl -s http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Top 5 merchants by spend in September 2026"}'
```

The response has `answer`, `status` (`ok`, `refused`, `step_limit` or `max_tokens`), `sql`, `steps`, `usage` and, for multi-row results, a `table`. Send earlier turns in `history` for follow-up questions. `GET /health` checks the database and never calls the model, so it's free to probe.

### Using your own statement

Run the app with `docker compose up --build`, open http://localhost:8000 and click **+ New analysis**. It takes PDF or CSV statements. Each analysis is stored separately, so its data never mixes with another, and you can add more statements under **Manage**.

- **Formats:** it finds the date, description and debit/credit (or amount) columns itself, and handles password-protected PDFs. Scanned PDFs aren't supported.
- **Checking the import:** if the statement has a running balance, each row is checked against it and any mismatch is reported. Still compare the totals with your statement, because this is tested on generated statements and each bank's layout differs.
- **Repeat imports:** re-importing a file, or statements that overlap, doesn't create duplicates.
- **Categories:** merchant rules are in `app/ingest/categorise.py`, and anything they don't recognise is filed under `Other` or `People`. Correct a merchant's category on the **Transactions** tab, and the correction is reapplied to later uploads.

**Privacy:** the uploaded file isn't kept, only the transactions read from it. They stay in a Docker volume on your computer, and **Delete analysis** under **Manage** removes them. Overview and Transactions use no AI model. Questions are off for each new analysis until you turn them on, because they send results to your AI provider.

## Deploying

The `Dockerfile` builds one self-contained image: the service, the web page and a read-only database of the synthetic data. It runs on any host that can run a container and supplies HTTPS. It listens on `$PORT` (default 8000), and `/health` works as the health check path.

> **Never set `ENABLE_UPLOADS` on a public deployment.** It gives everyone the same shared space, and is meant only for `compose.yaml`, which listens on your own machine. For uploads on a public site, set `PRIVATE_UPLOADS=true` instead.

### Private uploads on a public site

With `PRIVATE_UPLOADS=true`, each visitor gets temporary space tied to a random cookie, and can only reach their own analyses. There are no accounts.

- **Deleted automatically:** after `SESSION_TTL_MINUTES` of inactivity, and whenever the server restarts. On a free instance that sleeps when idle, this can be much sooner.
- **Limits:** 5 MB per file, 3 analyses per visitor, and `UPLOADS_PER_MINUTE`, `DAILY_UPLOAD_LIMIT` and `MAX_VISITORS` to protect the server.
- **What it doesn't do:** data isn't encrypted on the server's disk, and whoever operates the deployment could read it while it exists. Clearing cookies or switching browser loses access, with no recovery. This suits trying the app, not long-term storage of real statements.
- **AI questions:** these stay off for each analysis until the visitor turns them on, and they use your API key and quota.

Before making it public:

1. **Set a spend limit on the API key** in the Anthropic Console, using a key used only for this service. Limits inside the app reset whenever the service restarts, so this is your real cap.
2. **Run exactly one instance.** Usage limits are held in memory, so each extra instance gets its own allowance.
3. **Set `FORWARDED_ALLOW_IPS` for your host.** Without it the app sees every visitor as the host's proxy, so all visitors share one per-minute limit. Use your proxy's address range. Use `*` only if the proxy is the sole way to reach the container, because otherwise visitors can fake their address to dodge the limit.

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` | none, one is required for `/ask` | The key for the provider `MODEL` uses. Set it as a secret on your host. Never put it in the image. |
| `MODEL` | `claude-haiku-5-5` if unset | `gemini-*` and `gemma-*` use Google, `claude-*` uses Anthropic. Compare models using the eval. |
| `RATE_LIMIT_PER_MINUTE` | `5` | Questions per visitor per minute |
| `DAILY_QUESTION_LIMIT` | `200` | Questions per day across all visitors |
| `MAX_CONCURRENT_QUESTIONS` | `4` | Questions handled at once. Extra ones get a 503. |
| `AS_OF_DATE` | `2026-11-02` in the image | The demo's fixed "today", matching its static data |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies trusted to report the visitor's address |
| `AGENT_MAX_STEPS` | `8` | Model calls allowed per question |
| `PRIVATE_UPLOADS` | off | Per-visitor temporary uploads on a public site |
| `SESSION_TTL_MINUTES` | `120` | Inactivity before a visitor's data is deleted |
| `UPLOADS_PER_MINUTE` | `3` | Uploads per visitor per minute |
| `DAILY_UPLOAD_LIMIT` | `100` | Uploads per day across all visitors |
| `MAX_VISITORS` | `100` | Visitors with uploaded data at one time |

Logs record status, timing and token counts per question, but not the questions or answers.

## How a question is answered

1. Claude receives the question, the date and a description of the views.
2. It writes SQL and calls `query_transactions`, its only tool.
3. The query is checked, then run on a read-only connection. Rejected queries go back to Claude as errors so it can correct them.
4. Claude answers using formatted figures from the result. The loop stops after `AGENT_MAX_STEPS`.

## Safety

- **SQL is checked twice.** A `sqlglot` parse allows one `SELECT` over approved views. Independently, a SQLite authorizer on a `mode=ro` connection denies writes, `ATTACH`, `PRAGMA`, system tables and raw-text columns. Tests confirm the second layer holds with the first switched off.
- **Limits are visible.** A query over the row limit is flagged as truncated rather than silently cut short, and queries time out.
- **Raw narrations don't reach the model.** The views leave out the narration text, which holds account numbers, UPI IDs and remarks anyone can write by sending you ₹1.
- **The agent can't act.** It has no write or network tools, so injected text has little to work with.
- **The page can't leak through markup.** Answers are shown as plain text, and a strict Content Security Policy blocks images, inline scripts and outside connections.
- **The container is locked down.** The image runs as a non-root user with a read-only database, and `compose.yaml` adds a read-only filesystem and drops Linux capabilities.

### What does leave your machine

Your question, the SQL and the query results go to your model provider. Results can include merchant names, amounts and dates. **Names of people you pay over UPI appear as merchants**, because they aren't yet pseudonymised. Consider this before using real statements.

**Don't use the free tier of the Gemini API with real statements.** Google's pricing page says free-tier data for most models is used to improve its products, so keep it to the synthetic data.

## Evaluation

```bash
python eval/run_eval.py --mode gold --split all   # free: checks the gold SQL runs
python eval/run_eval.py --mode agent --split dev  # calls Claude and costs money
```

There are 100+ questions with gold SQL, split into `dev` for tuning and `test` for reporting. Each question gets two scores: whether a query returned the right values, and whether the written answer quotes them. Results are saved to `eval/results/`.

No agent results are published yet.

Keep these limits in mind when reading scores:

- About 40 questions come from two templates (merchant by month, category by month), so the effective variety is lower than the count suggests.
- The generator, parser rules and gold SQL share an author, so classification accuracy on this data says little about real statements.
- Only the `spending` total is checked independently of the SQL views.

## Tests

```bash
pytest
```

The tests make no API calls. The agent loop runs against a scripted fake client, which checks the plumbing but not Claude's behaviour.

## Roadmap

- [ ] Baseline agent results on the `test` split
- [ ] Evaluation on anonymised real statements
- [ ] LLM categorisation for merchants the rules miss, plus stored user corrections
- [ ] Pseudonymise people's names before they reach the model
- [ ] Conversation memory for follow-up questions
- [ ] RAG over fund factsheets and finance documents, tagged by financial year and tax regime
- [ ] Testing the PDF parser on real statements from major banks
- [ ] Local model support, so questions on your own data can stay private
- [ ] Accounts and encryption at rest, for keeping real data on a server
- [ ] Mutual fund overlap (needs fund portfolio data, which CAS statements don't include)

## Disclaimer

PaisaLens gives insights about your own data, not financial, tax or investment advice. Verify important figures against your statements.

## License

MIT
