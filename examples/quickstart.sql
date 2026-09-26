-- pgbee quickstart: a support ticket table and three derived columns.
-- Run with: docker compose exec -T postgres psql -U pgbee < examples/quickstart.sql
-- Cost: a few hundredths of a cent. Each column below has a spending cap anyway.

CREATE EXTENSION IF NOT EXISTS vector;

DROP TABLE IF EXISTS ticket;
CREATE TABLE ticket (
  id   serial PRIMARY KEY,
  body text NOT NULL
);

INSERT INTO ticket (body) VALUES
  ('Checkout returns error 500 since this morning, nobody can pay. We are losing orders.'),
  ('I was charged twice for the Pro plan this month, please refund one of the payments.'),
  ('How do I export my customer list to CSV?'),
  ('I cannot log in: the password reset email never arrives.'),
  ('Great support last week, thanks to Anna for the quick help!'),
  ('The invoice shows the wrong VAT number, can you issue a corrected one?'),
  ('Order confirmation emails arrive three hours late, customers keep calling us.'),
  ('Please delete my account and all my data.');

-- 1. A classification with a decision model (fast, cheap, calibrated confidence).
SELECT bee.add_column('ticket', 'category', array['body'], 'enum', 'decision',
  p_prompt => 'What is this support ticket about?',
  p_model => 'typesafe/jev-1.13',
  p_output_schema => '{
    "bug": "something is broken or does not work as it should",
    "billing": "charges, refunds, invoices, plans",
    "account": "login, password, account data, deletion",
    "question": "how to do something, feature questions",
    "feedback": "praise or general comments"
  }',
  p_config => '{"budget_usd": 0.10}');

-- 2. Free text with an LLM.
SELECT bee.add_column('ticket', 'summary', array['body'], 'text',
  p_prompt => 'Summarize the request in at most ten words.',
  p_model => 'openai/gpt-6-luna',
  p_backend_config => '{"reasoning": {"effort": "low"}}',
  p_config => '{"budget_usd": 0.10}');

-- 3. An embedding for similarity search.
SELECT bee.add_column('ticket', 'embedding', array['body'], 'vector', 'embedding',
  p_model => 'openai/text-embedding-3-small',
  p_output_schema => '{"dimensions": 1536}',
  p_config => '{"budget_usd": 0.10}');

-- The worker fills the columns within seconds. Then try:
--   SELECT id, category, summary FROM ticket ORDER BY id;
--   SELECT * FROM bee.needs_review;          -- answers below the confidence threshold
--   INSERT INTO ticket (body) VALUES ('The app crashes when I upload a photo');
--   UPDATE ticket SET category = 'bug' WHERE id = 3;   -- a human override: it sticks
--   SELECT column_name, spent_usd FROM bee.budgets;
