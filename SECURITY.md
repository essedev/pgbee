# Security

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: **Security > Report a vulnerability** on this repository. Do not open a public issue. You will get an answer within a week; fixes go into a patch release with a note in its release notes.

pgbee is alpha software maintained by one person. Only the latest release gets security fixes.

## Security model

- **Privileges.** The worker's role (`bee_worker`) can call the queue functions and read the `bee` views; it has no access to user tables. The queue functions and the triggers are `SECURITY DEFINER` with a fixed `search_path`, and `EXECUTE` on them is revoked from `PUBLIC`. Management functions (`bee.add_column` and the rest) run with the caller's rights and need the owner of the table.
- **Data leaving the database.** The values of the source columns are sent to the model provider (OpenRouter and the model behind it). The target column receives the model's answer, validated against the declared type.
- **Prompt injection.** Row text is model input and can try to steer the model. Type validation limits the damage (an `enum` column only receives declared values; a `text` or `jsonb` column receives whatever the model wrote), but it does not detect it. Treat derived values from untrusted text as untrusted.
- **SQL.** Dynamic SQL uses `format()` with `%I` for identifiers and bound parameters for values. Prompts and model output are data, never interpolated into SQL.
- **Secrets.** The OpenRouter key lives in the worker's environment, never in the database or in the Docker image.
