# Examples

`sample-statements/sample-current-account.csv` is a **synthetic** statement file: every name, amount
and account detail in it is made up. It exists so that you can try the server without a bank account
or an aggregator sign-up (see "60-second quick start" in the main README).

It follows the CSV layout that `src/nationwide_mcp/providers/statement.py` expects. That layout was
written from memory of Nationwide's export format, not from a real export, so this file shows what
the parser accepts and is **not** evidence of what Nationwide actually produces.

Never put a real statement export in this repository.
