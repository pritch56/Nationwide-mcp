# Aggregator research notes

**Snapshot dated October 2026.** These notes record what public pages said when the providers were
chosen. Prices, coverage and sign-up rules change, and none of it was verified by opening an account
or calling a live API. Check each source yourself before relying on any line here.

## Why an intermediary is needed

Nationwide Building Society has no API for individual customers. Its Open Banking API can be called
only by firms regulated for account information services, so an individual has to go through an
aggregator (or use statement downloads).

## Options looked at

| Option | Open to individuals? (as read, Oct 2026) | Cost (as read, Oct 2026) | Nationwide coverage (as read, Oct 2026) |
|---|---|---|---|
| TrueLayer Data API | Self-serve Console sign-up. Some individuals report running personal applications in Live mode; TrueLayer itself does not confirm this for private use | No public price list was found | TrueLayer's public provider list contained `ob-nationwide` with scopes `info, accounts, balance, transactions, direct_debits, standing_orders, cards, offline_access` |
| Lunch Flow | Self-serve consumer subscription | Its site advertised roughly US$2.92 per month billed yearly, with a short trial. Treat as indicative only | Not confirmed: the coverage page could not be read |
| Enable Banking | Free "restricted" mode for your own accounts | Free in restricted mode | Doubtful: no Nationwide entry was found in its 2026 changelogs, and some users report no UK banks in restricted mode |
| Statement files | Always | Free | Certain, to the extent Nationwide offers a download for the account type |

Options that appeared closed to individuals at the time: GoCardless Bank Account Data (reported
closed to new sign-ups since July 2025); Plaid, Yapily, Tink, Salt Edge and Moneyhub (business
onboarding only).

## Rate limits

UK Open Banking rules let a bank limit requests made without the customer present. A figure of four
requests per endpoint per account per 24 hours is commonly quoted; it was not verified for
Nationwide or for any of the aggregators above.

## Sources (all read October 2026)

- TrueLayer provider list: <https://auth.truelayer.com/api/providers>
- TrueLayer account data and sign convention: <https://docs.truelayer.com/docs/account-data-requests>
- TrueLayer card data and sign convention: <https://docs.truelayer.com/docs/card-data-requests>
- TrueLayer token endpoint (documents `code_verifier`): <https://docs.truelayer.com/reference/generateaccesstoken>
- TrueLayer extend connection (`POST https://api.truelayer.com/connections/extend`):
  <https://docs.truelayer.com/reference/extendconnection>
- Individuals describing personal use of TrueLayer Live: <https://github.com/ConnorKirkpatrick/actual-truelayer-sync>,
  <https://github.com/actualbudget/actual/issues/6617>
- GoCardless closed to new accounts: <https://actualbudget.org/docs/advanced/bank-sync/gocardless/>
- Lunch Flow: <https://www.lunchflow.app/>, <https://www.lunchflow.app/docs/api/personal-api-overview>
- Enable Banking: <https://enablebanking.com/docs/faq/>, <https://enablebanking.com/docs/api/reference/>,
  <https://github.com/actualbudget/actual/issues/7799>
- Nationwide download formats (forum thread): <https://forums.moneysavingexpert.com/discussion/6408564/cannot-download-transactions-for-nationwide-esavings-account>
