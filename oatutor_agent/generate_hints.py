import io
import os
import re
import string
import pandas as pd
import google.genai as genai
from google.genai import types
from dotenv import load_dotenv


# Expected columns in Gemini's output (in order)
OUTPUT_COLUMNS = [
    'Problem Name', 'Row Type', 'Title', 'Body Text', 'Answer',
    'answerType', 'HintID', 'Dependency', 'mcChoices',
    'Images (space delimited)', 'Parent', 'OER src', 'openstax KC', 'KC',
    'Taxonomy', 'License',
]
EXPECTED_FIELDS = len(OUTPUT_COLUMNS)  # 16


class HintGenerator:

    # ── Prompt templates ─────────────────────────────────────────────────────

    _SYSTEM_PROMPT = string.Template("""
You are a deterministic curriculum CSV transformation agent. Return only valid semicolon-delimited CSV. No markdown, no code blocks, no commentary, no extra text.

INPUTS
1) GOLD STANDARD CSV — reference for structure, pedagogy, and formatting.
2) NEW INPUT CSV — scraped OpenStax problems and steps to be transformed.

GOAL
Add hints, scaffolds, and answers to every step in the NEW INPUT CSV. Do not create, remove, or rename any problem or step rows unless an algebraic split is explicitly required by the exercise rules below.

OUTPUT FORMAT
The output must begin with exactly this header line:
Problem Name;Row Type;Title;Body Text;Answer;answerType;HintID;Dependency;mcChoices;Images (space delimited);Parent;OER src;openstax KC;KC;Taxonomy;License

Every row must contain exactly 16 fields and therefore exactly 15 semicolons. Empty fields must still be delimited — write field1;;field3, never skip the semicolon. Never add a leading or trailing semicolon.

ROW STRUCTURE (per problem group, followed by one blank row)
  problem → step → hint(s) → scaffold(s) [repeat step block if multiple steps]

PROBLEM ROWS
  Row Type = problem. Title = descriptive lesson-style title. Do not alter OpenStax metadata or source links.

STEP ROWS
  Row Type = step. Title = the actual question, LaTeX preferred. If a step says "verify an identity", rewrite Title as "evaluate the expression".
  Answer = concise answer, LaTeX preferred. answerType = numeric, algebraic, or mc.
  If a question demands multiple answers, break it into multiple step rows with one answer each.

HINT ROWS (1–3 per step)
  Row Type = hint. HintID = h1, h2, h3 (restart each step).
  Title = short concept phrase. Body Text = brief explanation.
  Answer and answerType = empty. h2 Dependency = h1; h1 Dependency = empty.

SCAFFOLD ROWS (one per hint)
  Row Type = scaffold. HintID = s1, s2, s3 (restart each step). Dependency = corresponding hN.
  Title = short instructional label. Body Text = guiding sub-question.
  Answer = concise answer. answerType = numeric, algebraic, or mc (4 pipe-delimited choices in mcChoices).

$exercise_rules
GOLD STANDARD CSV:
$gold_csv

NEW INPUT CSV:
$new_csv

Transform the NEW INPUT CSV using the GOLD STANDARD as reference. Generate hints and scaffolds for every step. Return only the final semicolon-delimited CSV starting with the header line above.
""")

    _EXERCISE_RULES = string.Template("""
EXERCISE-SPECIFIC RULES (apply only to the problems in this chunk)
The following exercise types are present: $exercise_types

verbal     — Convert the step into a multiple choice question. 4 plausible mcChoices (pipe-delimited). answerType = mc.
algebraic  — Split multi-answer steps into one step per answer. Otherwise treat as standard.
graphical  — Phrase scaffolds to guide reading/constructing the graph. Prefer answerType = mc when numeric answer is ambiguous.
numeric    — answerType = numeric. Answers must be exact values.
technology — Focus hints on the mathematical setup, not calculator mechanics.
general    — Apply standard rules.
""")

    # Exercise types that should trigger the exercise rules block
    EXERCISE_TYPES = {'Conceptual', 'Algebraic', 'Graphical', 'Numeric', 'Technology', 'Extensions', 'Applications'}

    def __init__(self, model_name: str = 'gemini-2.5-flash'):
        load_dotenv()
        key = os.getenv('GEMINI_API_KEY')
        if not key:
            raise EnvironmentError('GEMINI_API_KEY not set in environment or .env file')
        self.client = genai.Client(api_key=key)
        self.model_name = model_name

    # ── Column-shift repair ──────────────────────────────────────────────────

    @staticmethod
    def _repair_shifted_rows(raw_csv: str) -> str:
        """
        Gemini sometimes omits a semicolon for an empty field, causing all
        subsequent fields in that row to shift left by one. This method detects
        rows with fewer than EXPECTED_FIELDS fields and attempts to reinsert the
        missing delimiter by matching known-pattern columns against their expected
        position.

        Strategy: for each under-length row, try inserting a blank field at each
        possible position and pick the insertion point that best satisfies the
        column type constraints (e.g. col 1 = Row Type in {problem,step,hint,scaffold},
        col 5 = answerType in known set, col 6 = HintID matching h\d+|s\d+).
        """
        VALID_ROW_TYPES    = {'problem', 'step', 'hint', 'scaffold', ''}
        VALID_ANSWER_TYPES = {'numeric', 'algebraic', 'algebra', 'mc', 'string', ''}
        HINT_ID_RE         = re.compile(r'^[hs]\d+$')

        def score(fields: list[str]) -> int:
            """Higher score = better field alignment."""
            if len(fields) != EXPECTED_FIELDS:
                return -1
            score = 0
            if fields[1].strip().lower() in VALID_ROW_TYPES:   score += 3  # Row Type
            if fields[5].strip().lower() in VALID_ANSWER_TYPES: score += 2  # answerType
            if HINT_ID_RE.match(fields[6].strip()) or fields[6].strip() == '': score += 2  # HintID
            if HINT_ID_RE.match(fields[7].strip()) or fields[7].strip() == '': score += 1  # Dependency
            return score

        repaired_lines = []
        for line in raw_csv.splitlines():
            fields = line.split(';')
            if len(fields) == EXPECTED_FIELDS:
                repaired_lines.append(line)
                continue
            if len(fields) > EXPECTED_FIELDS:
                # Too many fields — can happen if a text field contains an unescaped semicolon.
                # Leave as-is and let pandas handle it (it will truncate extra fields).
                repaired_lines.append(line)
                continue

            # Under-length: try inserting a blank field at each position
            best_line  = line
            best_score = -1
            for insert_pos in range(len(fields) + 1):
                candidate = fields[:insert_pos] + [''] + fields[insert_pos:]
                s = score(candidate)
                if s > best_score:
                    best_score = s
                    best_line  = ';'.join(candidate)

            repaired_lines.append(best_line)

        return '\n'.join(repaired_lines)

    # ── Core generation ──────────────────────────────────────────────────────

    def _build_prompt(self, gold_csv: str, new_csv: str, exercise_types: list[str]) -> str:
        real_exercise_types = [t for t in exercise_types if t in self.EXERCISE_TYPES]
        if real_exercise_types:
            exercise_rules = self._EXERCISE_RULES.substitute(
                exercise_types=', '.join(real_exercise_types)
            )
        else:
            exercise_rules = ''

        try:
            return self._SYSTEM_PROMPT.substitute(
                exercise_rules=exercise_rules,
                gold_csv=gold_csv,
                new_csv=new_csv,
            )
        except KeyError as e:
            raise ValueError(f'Prompt template is missing placeholder: {e}')

    def _call_gemini(self, prompt: str) -> str:
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.0),
        )
        text = response.text.strip()
        # Strip accidental markdown fences
        if text.startswith('```'):
            text = text.split('\n', 1)[-1].rsplit('\n', 1)[0].strip()
        return text

    def _parse_csv(self, raw: str) -> pd.DataFrame:
        repaired = self._repair_shifted_rows(raw)
        return pd.read_csv(
            io.StringIO(repaired),
            sep=';',
            dtype=str,
            on_bad_lines='warn',   # warn instead of silently skipping
        ).fillna('')

    def populate_dataframe_with_gemini(
        self,
        gold_csv: str,           # pre-serialized once by the caller
        new_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Send a single chunk DataFrame to Gemini and return the populated result.
        gold_csv is passed as a pre-serialized string so it is only computed once
        across all chunks.
        """
        # Always drop _exercise_type before sending — collect it first
        exercise_types: list[str] = []
        if '_exercise_type' in new_df.columns:
            exercise_types = new_df['_exercise_type'].dropna().unique().tolist()
            new_df = new_df.drop(columns=['_exercise_type'])

        new_csv = new_df.to_csv(index=False, sep=';')
        prompt  = self._build_prompt(gold_csv, new_csv, exercise_types)

        print('  Sending to Gemini...')
        raw = self._call_gemini(prompt)
        return self._parse_csv(raw)

    # ── Chunked processing ───────────────────────────────────────────────────

    def process_dataframe_in_chunks(
        self,
        gold_df: pd.DataFrame,
        new_df: pd.DataFrame,
        problems_per_chunk: int = 3,
        max_retries: int = 1,
    ) -> pd.DataFrame:
        """
        Split new_df into chunks of problems_per_chunk, call Gemini on each,
        and concatenate results.

        On failure, retries once with half the chunk size before falling back
        to raw data. Fallen-back problems are logged at the end so they can be
        manually reviewed.
        """
        # Serialize gold once — reused across every chunk call
        gold_csv = gold_df.to_csv(index=False, sep=';')

        unique_problems = new_df['Problem Name'].dropna().replace('', pd.NA).dropna().unique()
        total_chunks    = (len(unique_problems) + problems_per_chunk - 1) // problems_per_chunk

        processed_chunks: list[pd.DataFrame] = []
        fallback_problems: list[str]          = []

        for i in range(0, len(unique_problems), problems_per_chunk):
            batch_names = unique_problems[i : i + problems_per_chunk]
            chunk_df    = new_df[new_df['Problem Name'].isin(batch_names)].copy()
            chunk_num   = (i // problems_per_chunk) + 1

            print(f'\nChunk {chunk_num}/{total_chunks}: {", ".join(batch_names)}')

            result = self._try_process_chunk(gold_csv, chunk_df, batch_names, max_retries)

            if result is not None:
                processed_chunks.append(result)
            else:
                print(f'  Falling back to raw data for: {", ".join(batch_names)}')
                fallback_problems.extend(batch_names)
                processed_chunks.append(chunk_df)

        if not processed_chunks:
            raise ValueError('No chunks were successfully processed.')

        if fallback_problems:
            print(f'\n⚠ {len(fallback_problems)} problem(s) fell back to raw data and need manual review:')
            for name in fallback_problems:
                print(f'  - {name}')

        print('\nAll chunks processed. Assembling final DataFrame...')
        return pd.concat(processed_chunks, ignore_index=True)

    def _try_process_chunk(
        self,
        gold_csv: str,
        chunk_df: pd.DataFrame,
        batch_names: list[str],
        max_retries: int,
    ) -> pd.DataFrame | None:
        """
        Attempt to process a chunk, retrying with smaller sub-chunks on failure.
        Returns None if all attempts fail.
        """
        try:
            return self.populate_dataframe_with_gemini(gold_csv, chunk_df.copy())
        except Exception as e:
            print(f'  Error: {e}')

        if max_retries > 0 and len(batch_names) > 1:
            print(f'  Retrying as {len(batch_names)} individual problems...')
            sub_results = []
            for name in batch_names:
                single_df = chunk_df[chunk_df['Problem Name'] == name].copy()
                try:
                    sub_results.append(
                        self.populate_dataframe_with_gemini(gold_csv, single_df)
                    )
                    print(f'    {name}: OK')
                except Exception as e2:
                    print(f'    {name}: failed ({e2}), using raw')
                    sub_results.append(single_df)
            return pd.concat(sub_results, ignore_index=True)

        return None