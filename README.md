# PaisaLens

Ask questions about your own money in plain English and get exact numbers, with the SQL that produced them.

**Status: early v1.** This covers numeric questions over bank and card transactions, through a small web page, an HTTP API or the command line. RAG and charts aren't built yet ([Roadmap](#roadmap)).

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

The response has `answer`, `status` (`ok`, `refused`, `step_limit` or `max_tokens`), `sql`, `steps` and `usage`. `GET /health` checks the database and never calls the model, so it's free to probe.

### Using your own statement

Export a CSV with the columns `date` (DD/MM/YYYY), `account`, `narration`, `debit` and `credit`. Pass your name so transfers to yourself are recognised:

```bash
python -m app.ingest.statements my_statement.csv --self-name "YOUR NAME"
```

Re-importing a file, or importing statements that overlap, doesn't create duplicates. Merchant rules are in `app/ingest/categorise.py`, and anything they don't recognise is filed under `Other` or `People`.

## Deploying

The `Dockerfile` builds one self-contained image: the service, the web page and a read-only database of the synthetic data. It runs on any host that can run a container and supplies HTTPS. It listens on `$PORT` (default 8000), and `/health` works as the health check path.

> **This is a public demo setup. There's no login**, so anyone who can reach the service can ask about whatever data it holds. Don't deploy it with real statements.

Before making it public:

1. **Set a spend limit on the API key** in the Anthropic Console, using a key used only for this service. Limits inside the app reset whenever the service restarts, so this is your real cap.
2. **Run exactly one instance.** Usage limits are held in memory, so each extra instance gets its own allowance.
3. **Set `FORWARDED_ALLOW_IPS` for your host.** Without it the app sees every visitor as the host's proxy, so all visitors share one per-minute limit. Use your proxy's address range. Use `*` only if the proxy is the sole way to reach the container, because otherwise visitors can fake their address to dodge the limit.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | none, required for `/ask` | Set it as a secret on your host. Never put it in the image. |
| `MODEL` | `claude-sonnet-5-5` | Claude model to use |
| `RATE_LIMIT_PER_MINUTE` | `5` | Questions per visitor per minute |
| `DAILY_QUESTION_LIMIT` | `200` | Questions per day across all visitors |
| `MAX_CONCURRENT_QUESTIONS` | `4` | Questions handled at once. Extra ones get a 503. |
| `AS_OF_DATE` | `2026-11-02` in the image | The demo's fixed "today", matching its static data |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies trusted to report the visitor's address |
| `AGENT_MAX_STEPS` | `8` | Model calls allowed per question |

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

Your question, the SQL and the query results go to the Anthropic API. Results can include merchant names, amounts and dates. **Names of people you pay over UPI appear as merchants**, because they aren't yet pseudonymised. Consider this before using real statements.

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
- [ ] Charts
- [ ] Login, before any deployment holds real data
- [ ] PDF statement parsers
- [ ] Recurring payment detection
- [ ] Mutual fund overlap (needs fund portfolio data, which CAS statements don't include)

## Disclaimer

PaisaLens gives insights about your own data, not financial, tax or investment advice. Verify important figures against your statements.

## License

MIT
