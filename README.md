# OATutor Content Pipeline

Automated pipeline for generating OATutor-formatted curriculum content from OpenStax textbooks. Scrapes examples and exercises, generates hints and scaffolds via Gemini, validates the output against the OATutor spec, and auto-fixes structural issues.

---

## Directory Structure

```
oatutor/
├── agent.py            # orchestrator — runs the full pipeline
├── scrape.py           # scrapes OpenStax pages into a raw DataFrame
├── generate_hints.py   # sends raw DataFrame to Gemini, returns populated DataFrame
├── validator.py        # validates the xlsx against the OATutor spec
├── fixer.py            # fixes structural issues; uses Gemini for content issues
├── requirements.txt
│
├── gold_workbooks/     # reference xlsx files used to guide Gemini's output
└── input_workbooks/    # pipeline outputs land here (timestamped)
```

---

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
GEMINI_API_KEY=your_key_here
```

---

## Running the Pipeline

### Full pipeline (most common)
```bash
python agent.py --url <openstax_url> --name <stem> --gold <gold_file>
```

Example:
```bash
python agent.py \
  --url https://openstax.org/books/precalculus-2e/pages/8-8-vectors \
  --name vector \
  --gold gold.xlsx
```

This runs all 5 stages:
1. **Scrape** — fetches the OpenStax page and extracts problems into a raw xlsx
2. **Generate** — sends problems to Gemini in chunks, fills in hints/scaffolds/answers
3. **Validate (pre-fix)** — checks the output against the spec, writes issues to `Validator Check` column
4. **Fix** — auto-fixes structural issues; uses Gemini for missing answers/taxonomy/KC
5. **Validate (post-fix)** — confirms fixes took, reports any remaining issues

Output is saved to `input_workbooks/<name>_<YYYYMMDD_HHMM>.xlsx`.

### Common flags

| Flag | What it does |
|------|-------------|
| `--mode examples` | Scrape worked examples only (default) |
| `--mode exercises` | Scrape exercise sections only |
| `--mode both` | Scrape examples then exercises, shared counter |
| `--chunk 3` | Problems per Gemini call (default: 3, increase for speed, decrease if hitting token limits) |
| `--sheet "7.2"` | Target a specific worksheet by name (default: active sheet) |
| `--skip-generate` | Scrape only — outputs raw xlsx without Gemini generation |
| `--skip-fix` | Validate only — no fixes applied |
| `--no-content-fix` | Structural fixes only — skips Gemini content fixes (faster, no API cost) |
| `--gold gold.xlsx` | Filename in `gold_workbooks/` or a full path |

---

## Running Individual Modules

Each module is independently runnable:

```bash
# Validate a sheet and write issues to the Validator Check column
python validator.py input_workbooks/vector_20260410_1400.xlsx

# Validate + auto-fix structural issues in one step
python validator.py input_workbooks/vector_20260410_1400.xlsx --fix

# Fix an already-validated sheet (reads Issue objects from validator)
python fixer.py input_workbooks/vector_20260410_1400.xlsx

# Fix structural issues only (no Gemini API calls)
python fixer.py input_workbooks/vector_20260410_1400.xlsx --no-content
```

---

## The OATutor Spreadsheet Format

Each problem group follows this row structure:

```
problem row        — title, OER src, KC, Taxonomy, License
  step row         — question text, answer, answerType
    hint row (h1)  — concept name + brief explanation, no answer
    scaffold (s1)  — guiding sub-question + answer, dep=h1
    hint row (h2)  — optional second hint, dep=h1
    scaffold (s2)  — dep=h2
    ...
  step row         — repeat for multi-step problems
    ...
[blank row]
```

### Column reference

| Column | Notes |
|--------|-------|
| Problem Name | stem + number, e.g. `vector1`, `trig12` |
| Row Type | `problem`, `step`, `hint`, or `scaffold` |
| Title | question text (step) or concept name (hint/scaffold) |
| Body Text | explanation (hint) or sub-question (scaffold) |
| Answer | required on step and scaffold rows |
| answerType | `numeric`, `algebra`, or `mc` |
| HintID | `h1`/`h2`/`h3` for hints, `s1`/`s2`/`s3` for scaffolds — resets each step |
| Dependency | `h1` depends on nothing; `h2` depends on `h1`; scaffolds depend on their hint |
| mcChoices | pipe-delimited, 2–4 choices, required when answerType=mc |
| Images | space-delimited URLs, populated by scraper when images are detected |
| OER src | source URL, required on problem rows |
| KC | knowledge component tag, required on problem rows |
| Taxonomy | Bloom's level, required on problem rows |

---

## What the Validator Checks

1. Every problem has at least one step
2. HintIDs (`h1`, `h2`, `h3`) and ScaffoldIDs (`s1`, `s2`, `s3`) reset per step and are numbered correctly
3. Dependency chains are valid (no forward references, scaffolds point to their hint)
4. Problem Name ends in a number
5. `OER src`, `KC`, and `Taxonomy` are populated on every problem row
6. Formatting is consistent (LaTeX vs Python-style math) across the sheet
7. `algebra` answers don't contain commas (unless the step explicitly uses `a,b` vector notation)
8. Scaffolds have answers; hints don't
9. `mcChoices` is present when `answerType=mc` and absent otherwise
10. Answer is one of the mc choices when `answerType=mc`

Issues are written to the `Validator Check` column on each problem row so you can filter in Excel/Sheets.

---

## What the Fixer Handles

**Auto-fixed (no API calls):**
- Wrong HintID / ScaffoldID numbering
- Wrong or forward-referencing Dependency
- HintID or Dependency set on a step row
- mcChoices present on a non-mc row
- Answer present on a hint row
- Column-shift repair (Gemini omitted a delimiter, shifting fields left)

**Fixed via Gemini (requires API key):**
- Missing answer on a scaffold
- Missing or invalid answerType
- Missing Taxonomy (classifies using Bloom's levels)
- Missing KC (generates a hyphenated knowledge component tag)

**Flagged for manual review:**
- Missing OER src
- Problem has no steps
- Problem Name doesn't end in a number

---

## Adding a New Gold Standard

The gold standard guides Gemini's output style. To add one:

1. Create or export a well-formed OATutor xlsx
2. Drop it in `gold_workbooks/`
3. Pass it with `--gold your_file.xlsx`

The gold file should have diverse examples of the content type you're generating — Gemini uses it as a style and structure reference.

---

## Common Issues

**Gemini returns malformed CSV** — the column-shift repair in `generate_hints.py` handles the most common case (missing delimiter on empty field). If a chunk still fails after retry, it falls back to raw scraped data and logs the problem name. Re-run with `--chunk 1` to isolate it.

**`GEMINI_API_KEY not set`** — check your `.env` file is in the project root and contains `GEMINI_API_KEY=...` with no spaces around the `=`.

**Gold file not found** — pass just the filename (e.g. `--gold gold.xlsx`) and the agent looks in `gold_workbooks/`. Or pass a full path.

**Images flagged during scrape** — the scraper prints any questions containing images. Upload those images to imgur and manually update the `Images (space delimited)` column with the imgur URLs before uploading to OATutor.
