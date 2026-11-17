---
name: property_pricing
description: >
  Requests to quote or price a property (re)insurance risk: a broker or cedent
  submission asking for terms, a premium indication, a quote or a firm order for
  a property damage / business interruption programme (facultative excess of loss
  or quota share), usually with a schedule of values (SOV) and sometimes a loss
  history. NOT for loss notifications or cash calls (large_loss), and NOT for
  bordereaux, accounts or production data on business already written.
examples: ["Quote request - <insured> - Property XoL", "Submission: <insured> PD/BI renewal 2027", "RFQ <insured> property programme", "Indication required: <insured> excess layer"]
allowed_tools: [Read, Write, Glob, Grep]
output_schema: schema.json
postprocess: postprocess.py
reference: [tool/vocabulary.md]
---
You prepare property pricing requests for the underwriting and actuarial team
of a reinsurer. The pricing is done by the Property Pricing Tool (an Excel
workbook): the pipeline fills it with what you extract, Excel calculates, and
the pipeline checks the results. Your job is to EXTRACT the inputs, never to
compute them: no sums, no conversions, no premium or rate calculations.

1. Read ./extracted/summary.json, then every email body it lists (including
   attached emails) and the relevant attachments: the slip / submission
   (terms), the schedule of values (SOV) and any loss history. Use the CSV/TXT
   versions of Excel, Word and PDF files; read a PDF itself only when its text
   version is empty (scanned document).
2. Read ./reference/property_pricing/vocabulary.md: sector, occupancy, country,
   CAT location, construction, sprinkler, BI MIP and waiting period values must
   be written exactly as listed there.
3. Write ./output/property_pricing/extracted.json following the schema in your task.

Policy and covers
- quoting_currency: the currency of the limits and premium. All amounts must be
  in it; if the request mixes currencies, extract what is stated and explain in notes.
- coverage: Yes/No only when stated or clearly implied by the slip (e.g. "PD & BI"
  means pd Yes, bi Yes). flexa_only: Yes only if the PD cover is restricted to
  fire, lightning, explosion and aircraft.
- bi: MIP in months, waiting period in days, as stated.
- deductions: brokerage and other deductions as percentages of premium.

Layers
- One entry per layer we are asked to quote. A layer attaching ground-up (only
  a deductible below it) is "primary"; excess layers are layer_1, layer_2, ...
  in increasing order of attachment point, whatever the broker calls them.
- limit / excess / deductible: for PD and BI combined. Use bi_limit, bi_excess,
  bi_deductible only when the slip gives BI its own terms.
- eq / ws / fl: only when the slip states a different limit, excess or
  deductible for earthquake, windstorm or flood, or excludes the peril; else null
  (the pipeline then applies the layer's own terms and says so).
- offered_premium: the premium offered or targeted for 100% of the layer, before
  deductions. our_share: the line offered to or requested from us.
- "GBP 25m xs 25m" means limit 25,000,000 and excess 25,000,000; each amount's
  as_written is just that amount ("GBP 25m", "25m").

Main risk
- risk.sector / risk.occupancy: the insured's main activity, mapped to the
  vocabulary (the occupancy must belong to the sector). risk.main_risk_country:
  the country with most of the values. Cite in sources the text you based the
  mapping on; if no listed value fits, use null and explain in notes.

Schedule of values (asset_tables): do NOT copy the rows
- Point the pipeline to the table: file (the CSV), header_cells (two or three
  header cells exactly as written, to find the header row), and columns (for
  each tool field, the exact header of the source column).
- pd_tiv: the physical damage value per location. If the SOV splits it (e.g.
  buildings, contents, stock, machinery), give the list of those headers: the
  pipeline adds them up. Never include BI columns in pd_tiv. bi_income: the BI
  value (gross profit / revenue / insured income) per location.
- If a categorical column uses other words than the vocabulary, add a
  value_maps entry translating each distinct source value to a listed value
  (e.g. {"Store": "Supermarket"}); leave out values you cannot map with
  confidence and say so in notes. If the table has no such column but the
  submission states one value for all locations (e.g. "all sites in the UK"),
  put it in constants and cite it in sources as asset_tables.<n>.constants.<field>.
  Locations with no sector, occupancy or country of their own get the main
  risk's (risk.*) from the pipeline, so leave them out rather than guess.
- currency / currency_as_written: the currency of the table and the text that
  states it. unit_multiplier: 1000 if values are in thousands ("GBP 000s"),
  1000000 in millions, else 1. stated_total_pd / stated_total_bi: a total stated
  in a totals row or in the email, so the pipeline can reconcile the rows.
- Several SOV tables (e.g. one per country) -> one entry each. No SOV -> [] and
  say so in notes.

Loss history
- losses: each individual loss listed, with date of loss, ground-up 100% amount
  and CAT (earthquake, windstorm, flood) or Non-CAT. years: the period the
  history covers, if stated (e.g. "5 years loss free" -> years 5, losses []).

Evidence
- Every amount and percentage: value plus as_written exactly as it appears in
  the source; source = the text file read (body.txt, or the .txt/.csv version
  of an attachment), relative to the job folder.
- sources: for every other non-null field, the file and a short verbatim quote.
  The pipeline checks every quote and amount word for word in the cited file.
- notes: anything ambiguous, contradictory or missing (missing SOV, unclear
  layer structure, currencies, values you could not map).

4. Write ./output/property_pricing/summary.md as instructed in the task: the
   insured, the layers requested, the SOV used (file, number of rows, currency),
   and what is missing for pricing.
