# PaisaLens

Ask questions about your own money in plain English and get exact numbers, with the SQL that produced them.

**Status: early v1, command line only.** This covers numeric questions over bank and card transactions. RAG, the API, the web UI and charts aren't built yet ([Roadmap](#roadmap)).

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

### Using your own statement

Export a CSV with the columns `date` (DD/MM/YYYY), `account`, `narration`, `debit` and `credit`. Pass your name so transfers to yourself are recognised:

```bash
python -m app.ingest.statements my_statement.csv --self-name "YOUR NAME"
```

Re-importing a file, or importing statements that overlap, doesn't create duplicates. Merchant rules are in `app/ingest/categorise.py`, and anything they don't recognise is filed under `Other` or `People`.

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
- [ ] Charts, FastAPI service and web UI
- [ ] Docker and CI
- [ ] PDF statement parsers
- [ ] Recurring payment detection
- [ ] Mutual fund overlap (needs fund portfolio data, which CAS statements don't include)

## Disclaimer

PaisaLens gives insights about your own data, not financial, tax or investment advice. Verify important figures against your statements.

## License

MIT
