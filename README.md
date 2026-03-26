# OATutor Curriculum Generation Pipeline

Automated pipeline that scrapes OpenStax textbooks and generates upload-ready curriculum content for the [OATutor](https://oatutor.com) platform using Gemini 2.5 Flash. Reduces manual content authoring time by ~85%.

## What it does

Given an OpenStax textbook URL and a gold standard spreadsheet, the pipeline:

1. Fetches raw HTML via the OpenStax archive API
2. Converts MathML → LaTeX (uses native annotations where available, custom recursive compiler as fallback)
3. Scrapes **examples** (all) and **exercises** (Verbal and Algebraic sections only)
4. Populates structured DataFrames of problem and step rows
5. Sends chunks to Gemini, which generates hints, scaffolds, and answers in OATutor format
6. Runs a post-processing pass to convert any free-text scaffold answers that are unreasonable to grade (functions, multi-value answers, complex expressions) to multiple choice

## Output format

Semicolon-delimited CSV, 16 columns:

```
Problem Name;Row Type;Title;Body Text;Answer;answerType;HintID;Dependency;mcChoices;Images (space delimited);Parent;OER src;openstax KC;KC;Taxonomy;License
```

Each problem group follows the structure:
```
problem → step → hint(s) → scaffold(s)
```
followed by a blank separator row. Hints have a concept-label Title and explanatory Body Text. Scaffolds have a guiding Title, sub-question Body Text, a concise Answer, and an answerType of `numeric`, `algebraic`, or `mc`.

## Setup

```bash
pip install google-generativeai beautifulsoup4 pandas openpyxl gspread python-dotenv requests
```

Create a `.env` file:
```
GEMINI_API_KEY=your_key_here
```

For Google Sheets integration, place your `credentials.json` service account file in the project root.

## Usage

```python
from oa_tutor_agent import OATutorAgent
import pandas as pd

gold_df = pd.read_excel("gold.xlsx", dtype=str).fillna('')

agent = OATutorAgent(
    name="trig72",
    sheet_name="7.2 - Sum and Difference Identities",
    book_url="https://openstax.org/books/precalculus-2e/pages/7-2-sum-and-difference-identities",
    gold_df=gold_df
)

agent.generate_curriculum()
# Outputs: trig72_raw.xlsx, trig72_final_ready_for_upload.xlsx
```

Then run the post-processing modifier:

```python
from additional_modifier import additional_modifier

modified_df = additional_modifier("trig72_final_ready_for_upload.xlsx", problems_per_chunk=5)
modified_df.to_excel("trig72_modified.xlsx", index=False)
```

## Project structure

```
oa_tutor_agent.py       # Main agent class
additional_modifier.py  # Post-processing MC conversion pass
gold.xlsx               # Gold standard reference spreadsheet
credentials.json        # Google service account (not committed)
.env                    # API key (not committed)
```

## How the Gemini prompt works

The pipeline uses a compact semicolon-delimited CSV prompt rather than verbose natural language instructions. Key design decisions:

- **Anchor header line** hardcoded verbatim in the prompt to prevent column drift
- **16-semicolon rule** stated explicitly — Gemini must emit exactly 16 semicolons per row
- **Exercise-type rules** injected conditionally — only chunks containing exercises get the verbal/algebraic transformation rules, keeping example-only chunks lean
- **Chunked processing** (3 problems/chunk for main pass, 5 for modifier) to stay within output token limits
- **Fallback to raw data** if a chunk fails, so no content is silently dropped

## Prompt design notes

The Gemini prompt instructs:
- **Verbal exercises** → convert to multiple choice with 4 plausible distractors
- **Algebraic exercises** → split into one step per answer if the question demands multiple values
- **Hints**: Title = concept label, Body Text = helpful explanation
- **Scaffolds**: Title = instructional label, Body Text = guiding sub-question, Answer = concise single value where possible

## Known limitations

- MathML rendering varies across OpenStax books — the custom LaTeX compiler covers common structures (`mfrac`, `msup`, `msqrt`, `mfenced`, etc.) but may miss edge cases in less common books
- Exercise section scraping is scoped to `os-section-exercises-container` → `data-depth="2"` sections titled "Verbal" or "Algebraic" — other section types are ignored by design but can be added to `EXERCISE_SECTIONS_TO_SCRAPE`
- The agent holds `population_index` and `problem_number` as mutable instance state, so calling population methods multiple times on the same instance accumulates numbering across calls (by design, to allow examples and exercises to share a continuous index)

- 11:27 PM

## Next Steps

# Code Cleanup

The agent currently holds `population_index` and `problem_number` as mutable instance state, which makes it fragile to reuse across calls. These should be local to each population method. `df_example_population` and `df_exercise_population` are nearly identical and should be unified into a single df_population method with a parameter controlling whether exercise type tagging happens. The Gemini prompt should be a class-level constant rather than defined inside the method body on every call. `generate_curriculum` does too much (emergent from the specific deliverable I had when developing this) — scraping, populating, processing, and exporting should be separated so a failure at any stage doesn't require restarting from scratch.

Agentic Modifications

The most impactful upgrade is replacing the current prompt-in, CSV-out pattern with proper function calling. Instead of injecting the gold standard and chunk CSVs as text, Gemini would be given tools like get_chunk(problem_names), get_gold_standard(), validate_output(csv), and write_chunk(csv) and would decide how to orchestrate them. This makes the pipeline genuinely agentic — the model is choosing what to call and when, rather than receiving a single giant prompt and returning a single response. Gemini 2.5 Flash supports this natively via the Google GenAI SDK.

A lighter-weight but still meaningful addition would be self-correction: after each chunk is parsed, run a validation step that checks row count, semicolon count per row, and that no new problem names were introduced, then feed failures back to Gemini with the specific error for a retry before falling back to raw data. Currently the fallback is silent — the agent gives up without attempting to fix the issue.

RAG integration (discussed earlier) would also fit naturally here — embedding the OATutor library at startup and retrieving topically similar worked examples per chunk to inject as few-shot demonstrations, which would significantly improve hint and scaffold quality for math domains the model handles less reliably.
