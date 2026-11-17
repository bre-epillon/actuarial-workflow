# Property Pricing Tool: the contract with the pipeline

| File | What it is |
|---|---|
| `Property Pricing Tool v1.08.xlsb` | The tool, **cleared of all client data** (inputs empty; formulas, lists and model parameters unchanged). The pipeline opens it read-only and saves a filled copy per request. **Not in git** (it is the company's pricing model): copy the blank template into this folder by hand, with exactly this name. `doctor` warns while it is missing; a pricing job fails with a clear message and can be retried once it is there. |
| `contract.json` | Where every input and output lives (sheet, cell or table column), its type, the label next to it, and the tool's dropdown lists. Derived from the v1.11 input maps of the pricing database tool, corrected for the v1.08 layout. |
| `vocabulary.md` | The dropdown values in readable form, given to the agent (generated: `build_vocabulary.py`). |

## How a request flows

1. The agent extracts the inputs into `extracted.json` (`../schema.json`):
   terms, layers, covers, deductions, loss history, and **where** the schedule
   of values is and how its columns map to the tool. It never copies the SOV
   rows and never calculates.
2. `../postprocess.py` (no AI) checks every amount and quote against the
   sources, checks values against the tool's lists, reads the SOV rows from the
   CSV (adding up split value columns, leaving out and reconciling against the
   totals row), and writes the inputs into the tool's input cells. It only
   writes cells that have a value, so the tool's own defaults (formulas) stay.
3. Excel calculates (`pipeline/excel.py`, through PowerShell; needs Microsoft Excel).
4. The pipeline reads back every written cell (did the tool keep it?), the
   labels next to them and the version cell (has the layout changed?), and
   the tool's outputs (asset count and PD total vs what was written, technical
   premium, adequacy, error values). Results: Teams card, `computed.json`, and
   the filled workbook in `output/property_pricing/`.

## When the tool changes (new version)

1. Clear the client data from the new workbook (inputs only; keep formulas and
   lists), save it here as `Property Pricing Tool v<version>.xlsb`, and delete
   the old one.
2. Update `contract.json`: `template`, `version`, `version_check`, and any cells
   that moved (the v1.11 maps, for example, insert a UWY column in
   `6_Experience_Rating`). Re-read the dropdown lists into `lists`.
3. `uv run python routes/property_pricing/tool/build_vocabulary.py`
4. `uv run python -m pytest`, then process `tests/fixtures/property_quote_request.eml`
   once (`--once`) and open the filled workbook.

If someone replaces the workbook without updating the contract, every run stops
with "the workbook's layout differs from tool/contract.json" instead of
writing inputs into the wrong cells.
