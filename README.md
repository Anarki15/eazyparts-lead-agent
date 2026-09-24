# eAZyparts Lead Agent

An AI assistant that answers Meta ad leads on WhatsApp and Messenger. It checks live stock on eazyparts.co.za and
sends a checkout link, or it captures a complete sourcing request (VIN, part, OEM/alternative choice, town) and hands the
chat to a person in the Chatwoot inbox.

The full blueprint is in the Claude doc "eAZyparts Lead Agent — Blueprint".

## What's in here

| File | What it does |
| --- | --- |
| `app/prompt.py` | The playbook: how the agent talks, when it hands over. **Edit wording here.** |
| `app/catalogue.py` | Stock search over the Shopify product feed (part number → VIN → make/model/part/side/year) |
| `app/agent.py` | Claude tool-use loop with 5 tools: search_stock, get_product, make_checkout_link, save_lead_card, hand_over |
| `app/leads.py` | Writes lead cards to Google Sheets (via Apps Script) and a CSV backup |
| `app/server.py` | Web service: Chatwoot bot webhook, Meta lead-form webhook, `/test` chat page, kill switch |
| `data/sample_products.json` | 21 real eAZyparts listings (sample variant IDs) for offline testing |
| `tests/` | 14 automated tests (run `pytest`) |

## 1. Try it on your own computer (10 minutes)

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...        # from console.anthropic.com
export CATALOGUE_SOURCE=live               # or "sample" for the 21 test products
uvicorn app.server:app --reload
```
Open http://localhost:8000/test and play the customer.

## 2. Deploy (developer, about 1 day)

1. Push this folder to a private GitHub repo. In Render, choose **New → Blueprint** and pick the repo (`render.yaml` sets everything up).
2. Fill in the secret values Render asks for (see the table below). Set `TEST_PAGE_KEY` so the test page is private.
3. **Chatwoot:** go to Settings → Integrations → Agent Bots → add a bot with the webhook `https://<render-url>/webhooks/chatwoot`, then copy its token into `CHATWOOT_BOT_TOKEN`. Connect the bot to the WhatsApp and Messenger inboxes. New chats start as *pending* (bot). The agent flips them to *open* when it hands over.
4. **Meta lead forms (backup path):** in the Meta app, go to Webhooks → Page → subscribe to `leadgen` with URL `https://<render-url>/webhooks/meta-leads` and the verify token `META_VERIFY_TOKEN`. You need a Page access token with `leads_retrieval`.
5. **WhatsApp template:** create the template `eazyparts_lead_opener` (category Marketing, English):
   > Hi {{1}}, thanks for your enquiry about {{2}} on eAZyparts. I'm the eAZyparts assistant and can check our stock right now. Reply YES to start, or tap Talk to a person.

   Add the buttons *Yes, check stock* and *Talk to a person*.
6. **Google Sheet:** in the lead tracker, go to Extensions → Apps Script, paste the code below, then Deploy → Web app (Execute as: me, Access: anyone with the link). Copy the URL into `SHEETS_WEBHOOK_URL`.

```javascript
function doPost(e) {
  const row = JSON.parse(e.postData.contents);
  const sh = SpreadsheetApp.getActive().getSheetByName('Agent Leads') || SpreadsheetApp.getActive().insertSheet('Agent Leads');
  const cols = ["timestamp","lead_id","channel","customer_name","phone","campaign","make","model","year","vin","part","side",
                "part_number","photos","preference","delivery_town","stock_result","outcome","handover_reason","priority","status"];
  if (sh.getLastRow() === 0) sh.appendRow(cols);
  sh.appendRow(cols.map(c => row[c] || ''));
  return ContentService.createTextOutput('ok');
}
```

## Settings

| Variable | What it is |
| --- | --- |
| `ANTHROPIC_API_KEY` | Claude API key. Set a monthly spend limit in the console |
| `AGENT_MODEL` | `claude-haiku-4-5` (default). Switch to a Sonnet model if photo reading needs to be sharper |
| `AGENT_ENABLED` | `false` = kill switch: every chat goes straight to staff |
| `CATALOGUE_SOURCE` / `SHOPIFY_STORE_URL` | `live` + your store URL |
| `CHATWOOT_URL`, `CHATWOOT_BOT_TOKEN` | Inbox connection |
| `META_VERIFY_TOKEN`, `META_PAGE_TOKEN` | Lead-form webhook |
| `WA_PHONE_NUMBER_ID`, `WA_TOKEN`, `WA_OPENER_TEMPLATE` | WhatsApp Cloud API, for the lead-form opener |
| `SHEETS_WEBHOOK_URL` | Apps Script web-app URL |
| `TEST_PAGE_KEY` | Password for `/test?key=...` |

## Notes

- Stock search reads the public `/products.json` feed and caches it for 30 minutes (about 4,800 products = 20 pages), and re-checks the one product live before sending a checkout link. For a very large catalogue, swap in the Shopify Storefront API search. Only `catalogue.py` needs to change.
- Checkout links are Shopify cart permalinks (`/cart/{variant}:1`) tagged with the lead ID, so orders can be traced back to chats.
- 2-hour and 24-hour unpaid reminders are not built yet. Add them as a scheduled job once the phase 1 numbers are in.
