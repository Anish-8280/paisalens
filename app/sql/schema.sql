-- Money is stored as integer paise, always positive. `direction` carries the sign.
CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY,
    txn_hash     TEXT    NOT NULL UNIQUE,  -- makes re-importing overlapping statements a no-op
    account      TEXT    NOT NULL,
    txn_date     TEXT    NOT NULL,         -- ISO 8601, YYYY-MM-DD
    narration    TEXT    NOT NULL,         -- raw bank text: never exposed to the model
    merchant     TEXT    NOT NULL,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    direction    TEXT    NOT NULL CHECK (direction IN ('debit', 'credit')),
    txn_type     TEXT    NOT NULL CHECK (txn_type IN (
        'purchase', 'refund', 'p2p', 'fee', 'cash_withdrawal',
        'internal_transfer', 'card_payment', 'investment', 'income'
    )),
    category     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (txn_date);

-- The views below are the only objects the agent can query. None expose `narration`,
-- which keeps raw bank text (account numbers, UPI IDs, attacker-written remarks) away
-- from the model.

-- What "spending" means: money that really left, net of refunds. Excludes transfers
-- between your own accounts, credit card bill payments (the card purchases already
-- count), investments and income.
CREATE VIEW IF NOT EXISTS spending AS
SELECT id, account, txn_date, merchant, category, txn_type,
       CASE direction WHEN 'credit' THEN -amount_paise ELSE amount_paise END AS net_amount_paise
FROM transactions
WHERE txn_type = 'refund'
   OR (direction = 'debit' AND txn_type IN ('purchase', 'p2p', 'fee', 'cash_withdrawal'));

CREATE VIEW IF NOT EXISTS investments AS
SELECT id, account, txn_date, merchant, category, amount_paise
FROM transactions
WHERE txn_type = 'investment' AND direction = 'debit';

CREATE VIEW IF NOT EXISTS income_received AS
SELECT id, account, txn_date, merchant, category, amount_paise
FROM transactions
WHERE direction = 'credit' AND txn_type IN ('income', 'p2p');

-- Everything else, including transfers and card payments, for questions about those.
CREATE VIEW IF NOT EXISTS all_transactions AS
SELECT id, account, txn_date, merchant, category, txn_type, direction, amount_paise
FROM transactions;
