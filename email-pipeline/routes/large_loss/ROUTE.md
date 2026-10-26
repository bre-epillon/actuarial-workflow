---
name: large_loss
description: >
  Notifications from cedents or brokers about an individual large loss or
  catastrophe event affecting our treaties: first advice, updates to the
  estimate or reserve, or cash calls. NOT for routine bordereaux, quarterly
  accounts, or premium/production data.
examples: ["Large Loss Advice - <cedent> - <date of loss>", "Loss update: <event name>", "Cash call request"]
allowed_tools: [Read, Write, Glob, Grep]
---
STUB: extraction only. No calculation is done in this route yet.

Your job is to EXTRACT facts from the notification, not to compute anything.

1. Read ./extracted/summary.json, the email body, and any attachments
   (CSV versions of Excel sheets are provided next to the originals).
2. Write ./output/large_loss/extracted.json with these fields
   (use null when a value is not stated; never infer or calculate):
   - cedent, broker, treaty_reference, underwriting_year
   - event_name, date_of_loss, line_of_business, country
   - currency
   - gross_loss_amount, previous_estimate (if this is an update)
   - our_share_stated (only if the email states it)
   - source: for every non-null field, the file name and a short quote where it was found
3. Write the summary.md as instructed in the task, listing the key extracted
   values and every field you left null.

(Next step, not yet implemented: deterministic netting via a pinned version
of LargeLossesNetting, called by the pipeline on extracted.json.)
