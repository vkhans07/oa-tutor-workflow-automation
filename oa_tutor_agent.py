import re
import json
import gspread
import requests
from bs4 import BeautifulSoup
from bs4.element import NavigableString
import pandas as pd
from dataclasses import dataclass, field
import google.genai as genai
from google.genai import types
from google.genai.types import GenerationConfig
import io

class OATutorAgent:
    
    @dataclass
    class Question:
        title: str
        body_text: str = ''
        problems: list[str] = field(default_factory=list)

    def __init__(self, name: str, sheet_name: str,  book_url: str, credentials_path: str='credentials.json', model_name: str='gemini-2.5-flash', gold_df: pd.DataFrame = None,):
        self.name = name
        self.sheet_name = sheet_name
        self.book_url = book_url
        self.credentials_path = credentials_path
        self.api_key = self.load_api_key()
        self.client = genai.Client(api_key=self.api_key)
        self.model_name = model_name
        self.PART_PREFIX = re.compile(r'^[ⓐⓑⓒⓓⓔⓕⓖⓗ]|\([a-h]\)\s*')
        self.columns = ['Problem Name', 'Row Type', 'Title', 'Body Text', 'Answer',
       'answerType', 'HintID', 'Dependency', 'mcChoices',
       'Images (space delimited)', 'Parent', 'OER src', 'openstax KC', 'KC',
       'Taxonomy', 'License']
        self.gold_df = gold_df
        self.population_index = 0
        self.problem_number = 1


    def load_api_key(self) -> str:
        from dotenv import load_dotenv
        import os
        load_dotenv()
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise ValueError("GEMINI_API_KEY not found in environment variables.")
        return key

    def load_data(self, url: str, worksheet_name: str) -> pd.DataFrame:
        gc = gspread.service_account(filename='credentials.json')
        sheet = gc.open_by_url(url).worksheet(worksheet_name)
        list_of_lists = sheet.get_all_values()
        df = pd.DataFrame(list_of_lists[1:], columns=list_of_lists[0])
        df = df.iloc[:, :16] # keep only up to 'P'
        df = df.loc[:, ~df.columns.str.contains('^Unnamed')]
        df = df.loc[:, df.columns != '']
        return df
    
    def set_gemini_model(self, model_name: str):
        self.model_name = model_name

    def strip_part_prefix(self, text: str) -> str:
        return self.PART_PREFIX.sub('', text).strip()

    def parse_mathml(self, node) -> str:
        """
        Recursively converts a BeautifulSoup MathML node into a clean LaTeX string.
        """
        if isinstance(node, NavigableString):
            return str(node).strip()
        
        if not hasattr(node, 'name') or not node.name:
            return ""

        name = node.name.lower()
        
        # EXPLICITLY IGNORE ANNOTATION AND ANNOTATION-XML so semantic math doesn't double up!
        if name in ['annotation', 'annotation-xml']:
            return ""

        # Base cases: text/symbol nodes
        if name in ['mi', 'mn', 'mo', 'mtext']:
            text = node.get_text(strip=True)
            return text

        # Extract valid children recursively
        children = [c for c in node.children if not (isinstance(c, NavigableString) and not str(c).strip())]
        parsed_children = [self.parse_mathml(c) for c in children]

        # Map MathML structures to LaTeX commands
        if name == 'mfrac':
            if len(parsed_children) >= 2:
                return f"\\frac{{{parsed_children[0]}}}{{{parsed_children[1]}}}"
        elif name == 'msup':
            if len(parsed_children) >= 2:
                return f"{parsed_children[0]}^{{{parsed_children[1]}}}"
        elif name == 'msub':
            if len(parsed_children) >= 2:
                return f"{parsed_children[0]}_{{{parsed_children[1]}}}"
        elif name == 'msubsup':
            if len(parsed_children) >= 3:
                return f"{parsed_children[0]}_{{{parsed_children[1]}}}^{{{parsed_children[2]}}}"
        elif name == 'msqrt':
            inner = "".join(parsed_children)
            return f"\\sqrt{{{inner}}}"
        elif name == 'mroot':
            if len(parsed_children) >= 2:
                return f"\\sqrt[{parsed_children[1]}]{{{parsed_children[0]}}}"
        elif name == 'mfenced':
            open_delim = node.get('open', '(')
            close_delim = node.get('close', ')')
            inner = "".join(parsed_children)
            return f"\\left{open_delim}{inner}\\right{close_delim}"

        return "".join(parsed_children)

    def extract_text_bs(self, element) -> str:
        """
        Extracts text natively using BeautifulSoup, converting MathML into LaTeX.
        Intelligently extracts author-provided LaTeX when available to prevent double text.
        """
        if not element:
            return ""
            
        for math_wrapper in element.find_all(['math', 'mjx-container']):
            actual_math = math_wrapper if math_wrapper.name == 'math' else math_wrapper.find('math')
            
            if actual_math:
                # 1. First, check if OpenStax provided the perfect LaTeX natively in an annotation!
                annotation = actual_math.find('annotation', attrs={'encoding': 'application/x-tex'})
                if annotation and annotation.text:
                    latex_code = annotation.text.strip()
                else:
                    # 2. If no annotation exists, fall back to our custom compiler
                    latex_code = self.parse_mathml(actual_math)
                    
                # Replace the entire HTML node (visuals + math tags) with the clean LaTeX
                math_wrapper.replace_with(f" $${latex_code}$$ ")
            else:
                math_wrapper.decompose()

        # Now extract the plain text
        text = element.get_text(separator=' ', strip=True)
        
        # Clean up excessive spacing
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def get_raw_html_via_api(self, url: str) -> str:
        headers = {'User-Agent': 'Mozilla/5.0'}
        print(f"Fetching base URL: {url}")
        response = requests.get(url, headers=headers)
        response.encoding = 'utf-8' # FORCE UTF-8 to prevent Mojibake gibberish

        archive_match = re.search(r'(https://openstax\.org/apps/archive/[^/]+/contents/[^/"\'\s]+\.json)', response.text)
        if not archive_match:
            archive_match = re.search(r'(/apps/archive/[^/]+/contents/[^/"\'\s]+\.json)', response.text)
            
        if archive_match:
            archive_url = archive_match.group(1)
            if archive_url.startswith('/'):
                archive_url = 'https://openstax.org' + archive_url
                
            print(f"Discovered Archive API Endpoint: {archive_url}")
            api_resp = requests.get(archive_url, headers=headers)
            api_resp.encoding = 'utf-8' 
            
            if api_resp.status_code == 200:
                api_data = api_resp.json()
                if 'content' in api_data:
                    print("Successfully fetched raw chapter content from API!")
                    return api_data['content']

        print("Archive URL not found, falling back to raw source HTML...")
        return response.text
    
    def scrape_examples(self, url: str) -> list[Question]:
        raw_html = self.get_raw_html_via_api(url)
        soup = BeautifulSoup(raw_html, 'html.parser')
        examples = []

        example_elements = soup.find_all(attrs={"data-type": "example"})
        if not example_elements:
            example_elements = soup.find_all("example")

        for item in example_elements:
            title_el = item.find(attrs={"data-type": "title"}) or item.find("title")
            title = self.extract_text_bs(title_el) if title_el else ""

            problem_el = item.find(attrs={"data-type": "problem"}) or item.find("problem")
            if not problem_el:
                continue

            title_in_problem = problem_el.find(attrs={"data-type": "title"}) or problem_el.find("title")
            if title_in_problem:
                title_in_problem.decompose()

            circled_items = problem_el.select("ol.circled > li, list > item")

            if len(circled_items) > 1:
                body_text_el = problem_el.find(["p", "para"])
                body_text = self.extract_text_bs(body_text_el) if body_text_el else ""
                problems = [self.strip_part_prefix(self.extract_text_bs(part)) for part in circled_items]
            else:
                body_text = ''
                problems = [self.extract_text_bs(problem_el)]

            examples.append(self.Question(title=title, body_text=body_text, problems=problems))

        return examples
    
    # Only these section headings will be scraped from the exercises section
    EXERCISE_SECTIONS_TO_SCRAPE = {'verbal', 'algebraic'}

    def scrape_exercises(self, url: str) -> list['OATutorAgent.Question']:
        import copy
        raw_html = self.get_raw_html_via_api(url)
        soup = BeautifulSoup(raw_html, 'html.parser')
        exercises = []

        # Scope to the exercises container first, then search only data-depth="2"
        # sections within it — these map to named groups like Verbal, Algebraic, etc.
        # This prevents false matches from titled sections elsewhere on the page.
        exercises_container = soup.find(class_="os-section-exercises-container") \
                              or soup.find(class_="section-exercises") \
                              or soup  # fallback if structure differs

        for section in exercises_container.find_all('section', attrs={"data-depth": "2"}):
            title_el = section.find(attrs={"data-type": "title"}, recursive=False) \
                       or section.find(attrs={"data-type": "title"})
            if not title_el:
                continue

            section_title = title_el.get_text(strip=True)
            if section_title.lower() not in self.EXERCISE_SECTIONS_TO_SCRAPE:
                continue

            # Collect any shared instruction line that precedes the exercises
            # (e.g. "For the following exercises, assume α is in the first quadrant.")
            instruction_text = ""
            for sibling in section.children:
                if hasattr(sibling, 'get_text') and 'following exercises' in sibling.get_text().lower():
                    instruction_text = self.extract_text_bs(copy.copy(sibling)) + " "
                    break

            for item in section.find_all(attrs={"data-type": "exercise"}):
                problem_container = item.find(class_="os-problem-container")
                if not problem_container:
                    continue

                problem_clone = copy.copy(problem_container)

                # Handle multi-part questions (ol.circled)
                circled_items = problem_clone.select("ol.circled > li, list > item")

                if len(circled_items) > 1:
                    for circled in problem_clone.select("ol.circled, list"):
                        circled.decompose()
                    body_text = instruction_text + self.extract_text_bs(problem_clone)
                    problems = [self.strip_part_prefix(self.extract_text_bs(copy.copy(part))) for part in circled_items]
                else:
                    body_text = instruction_text
                    problems = [self.extract_text_bs(problem_clone)]

                exercises.append(self.Question(
                    title=section_title,
                    body_text=body_text.strip(),
                    problems=problems
                ))

        return exercises
    
    def df_example_population(self, questions: list[Question], use_default_stem: bool = True, problem_name_stem: str = '') -> pd.DataFrame:
        if use_default_stem and problem_name_stem:
            raise ValueError("Cannot use default stem and custom stem simultaneously. Please choose one.")
        if use_default_stem:
            problem_name_stem = self.name
        new_data = pd.DataFrame(columns=self.columns, dtype=str)

        for q in questions:
            if len(q.problems) == 0:
                self.problem_number += 1
                continue

            new_data.loc[self.population_index, 'Problem Name'] = problem_name_stem + str(self.problem_number)
            new_data.loc[self.population_index, 'Row Type'] = 'problem'
            new_data.loc[self.population_index, 'Title'] = q.title
            new_data.loc[self.population_index, 'Body Text'] = q.body_text
            new_data.loc[self.population_index, 'OER src'] = self.book_url
            self.population_index += 1

            for p in q.problems:
                new_data.loc[self.population_index, 'Problem Name'] = problem_name_stem + str(self.problem_number)
                new_data.loc[self.population_index, 'Row Type'] = 'step'
                new_data.loc[self.population_index, 'Title'] = p
                self.population_index += 1

            self.problem_number += 1

        return new_data.fillna('')
    
    # Maps section title keywords → exercise type tag passed to Gemini
    EXERCISE_TYPE_MAP = {
        'verbal':      'verbal',
        'algebraic':   'algebraic',
        'graphical':   'graphical',
        'numeric':     'numeric',
        'technology':  'technology',
        'extensions':  'extensions',
        'real-world':  'real-world',
    }

    def _detect_exercise_type(self, section_title: str) -> str:
        """Returns a normalised exercise type string based on the section heading."""
        lower = section_title.lower()
        for keyword, tag in self.EXERCISE_TYPE_MAP.items():
            if keyword in lower:
                return tag
        return 'general'

    def df_exercise_population(self, questions: list[Question], use_default_stem: bool = True, problem_name_stem: str = '') -> pd.DataFrame:
        if use_default_stem and problem_name_stem:
            raise ValueError("Cannot use default stem and custom stem simultaneously. Please choose one.")
        if use_default_stem:
            problem_name_stem = self.name

        # Include a temporary _exercise_type column so Gemini knows which rules to apply.
        # This column is stripped before the final output is written.
        exercise_columns = self.columns + ['_exercise_type']
        new_data = pd.DataFrame(columns=exercise_columns, dtype=str)

        for q in questions:
            if len(q.problems) == 0:
                self.problem_number += 1
                continue

            exercise_type = self._detect_exercise_type(q.title)

            new_data.loc[self.population_index, 'Problem Name'] = problem_name_stem + str(self.problem_number)
            new_data.loc[self.population_index, 'Row Type'] = 'problem'
            new_data.loc[self.population_index, 'Title'] = q.title
            new_data.loc[self.population_index, 'Body Text'] = q.body_text
            new_data.loc[self.population_index, 'OER src'] = self.book_url
            new_data.loc[self.population_index, '_exercise_type'] = exercise_type
            self.population_index += 1

            for p in q.problems:
                new_data.loc[self.population_index, 'Problem Name'] = problem_name_stem + str(self.problem_number)
                new_data.loc[self.population_index, 'Row Type'] = 'step'
                new_data.loc[self.population_index, 'Title'] = p
                new_data.loc[self.population_index, '_exercise_type'] = exercise_type
                self.population_index += 1

            self.problem_number += 1

        return new_data.fillna('')
    
    
    def populate_dataframe_with_gemini(self, gold_df: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
        """
        Passes a gold standard DataFrame and an incomplete DataFrame to Gemini.
        If the chunk contains exercise rows (identified by the _exercise_type column),
        exercise-specific transformation rules are appended to the prompt.
        Returns a fully populated Pandas DataFrame matching the curriculum structure.
        """

        # Detect whether this chunk contains exercises and collect the types present
        has_exercises = '_exercise_type' in new_df.columns and new_df['_exercise_type'].str.strip().ne('').any()
        if has_exercises:
            exercise_types_present = new_df['_exercise_type'].dropna().unique().tolist()
            # Strip the temp column before sending to Gemini — it's for our logic only
            new_df = new_df.drop(columns=['_exercise_type'])

        # 1. Convert DataFrames to CSV strings
        gold_csv = gold_df.to_csv(index=False)
        new_csv = new_df.to_csv(index=False, sep=';')

        EXERCISE_RULES = """
EXERCISE-SPECIFIC RULES (apply only to the problems in this chunk)
The following exercise types are present: [EXERCISE_TYPES]

verbal — Convert the step into a multiple choice question. Generate 4 plausible mcChoices (pipe-delimited). Set answerType = mc and list the correct answer under Answer.

algebraic — If the step demands more than one distinct answer (e.g. "find all values of x and y"), split it into one step row per answer with a single concise answer each. Otherwise treat as normal.

graphical — Steps may describe or reference a graph. Phrase scaffold questions to guide reading or constructing the graph. answerType = mc is preferred when an exact numeric answer is ambiguous.

numeric — Treat as standard numeric steps. answerType = numeric. Answers must be exact values.

technology — Treat as standard steps. Focus hints on the mathematical setup rather than calculator mechanics.

general / other — Apply standard rules.
"""

        # 2. Base system prompt
        SYSTEM_PROMPT = """
You are a deterministic curriculum CSV transformation agent. Return only valid semicolon-delimited CSV. No markdown, no code blocks, no commentary, no extra text.

INPUTS
1) GOLD STANDARD CSV — reference for structure, pedagogy, and formatting.
2) NEW INPUT CSV — scraped OpenStax problems and steps to be transformed.

GOAL
Add hints, scaffolds, and answers to every step in the NEW INPUT CSV. Do not create, remove, or rename any problem or step rows unless an algebraic split is explicitly required by the exercise rules below.

OUTPUT FORMAT
The output must begin with exactly this header line:
Problem Name;Row Type;Title;Body Text;Answer;answerType;HintID;Dependency;mcChoices;Images (space delimited);Parent;OER src;openstax KC;KC;Taxonomy;License

Every row must contain exactly 16 semicolons. Empty fields must still be delimited (write ;; not ;). Never add a leading or trailing semicolon.

ROW STRUCTURE (per problem group, followed by one blank row)
problem → step → hint(s) → scaffold(s) [repeat step block if multiple steps]

PROBLEM ROWS
Row Type = problem. Title = descriptive lesson-style title (e.g. "Finding the Exact Value Using the Cosine Difference Formula"). Do not alter OpenStax metadata or source links. Columns after Parent are empty for non-problem rows.

STEP ROWS
Row Type = step. Title = the actual question, LaTeX preferred. If a step says "verify an identity", rewrite Title as "evaluate the expression" so a concrete answer exists. Do not modify mathematical meaning.
Answer = concise answer, LaTeX preferred. answerType = numeric (exact value), algebraic (expression/variable), or mc (multiple choice; mcChoices = choice1|choice2|choice3|choice4, correct answer listed under Answer).
If a question demands multiple answers (e.g. "Solve the triangle..."), break it into multiple step rows with distinct questions and single answers. If a single answer is not possible, use answerType = mc.

HINT ROWS (1–3 per step)
Row Type = hint. HintID = h1, h2, h3 (restart each step).
Title = short phrase naming the concept (e.g. "Applying the cosine difference formula").
Body Text = brief helpful explanation or context (e.g. "Recall that cos(A−B) = cos(A)cos(B) + sin(A)sin(B)").
Answer and answerType = empty. h2 may list h1 in Dependency; otherwise Dependency is empty.

SCAFFOLD ROWS (one per hint)
Row Type = scaffold. HintID = s1, s2, s3 (restart each step). Dependency = corresponding hN.
Title = short instructional label (e.g. "Identify A and B in the expression").
Body Text = guiding sub-question or partial step (e.g. "In cos(5π/4 − π/6), what are A and B?").
Answer = concise answer, LaTeX preferred. answerType = numeric, algebraic, or mc. Ask for a single value when possible; otherwise use mc with 4 choices.

DEPENDENCIES
Hints may depend on earlier hints. Scaffolds must reference their corresponding hint. Use HintID values only.

[EXERCISE_RULES]
GOLD STANDARD CSV:
[GOLD_STANDARD_CSV]

NEW INPUT CSV:
[NEW_INPUT_CSV]

Transform the NEW INPUT CSV using the GOLD STANDARD as reference. Generate hints and scaffolds for every step. Return only the final semicolon-delimited CSV starting with the header line above.
"""

        # 3. Inject exercise rules only when needed
        if has_exercises:
            filled_exercise_rules = EXERCISE_RULES.replace(
                '[EXERCISE_TYPES]', ', '.join(exercise_types_present)
            )
            final_prompt = SYSTEM_PROMPT.replace('[EXERCISE_RULES]', filled_exercise_rules)
        else:
            final_prompt = SYSTEM_PROMPT.replace('[EXERCISE_RULES]', '')

        # 4. Inject data
        final_prompt = final_prompt.replace('[GOLD_STANDARD_CSV]', gold_csv)
        final_prompt = final_prompt.replace('[NEW_INPUT_CSV]', new_csv)

        print("Sending data to Gemini for transformation...")
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=final_prompt,
            config=types.GenerateContentConfig(temperature=0.0)
        )

        # 5. Strip accidental markdown fences
        clean_csv_string = response.text.strip()
        if clean_csv_string.startswith("```"):
            clean_csv_string = clean_csv_string.split("\n", 1)[-1].rsplit("\n", 1)[0].strip()

        # 6. Parse back into a DataFrame
        try:
            completed_df = pd.read_csv(io.StringIO(clean_csv_string), sep=';', dtype=str, on_bad_lines='skip').fillna('')
            return completed_df
        except Exception as e:
            print(f"Failed to parse CSV. Raw output was:\n{clean_csv_string}")
            raise e

    def process_dataframe_in_chunks(self, gold_df: pd.DataFrame, new_df: pd.DataFrame, problems_per_chunk: int = 3) -> pd.DataFrame:
        """
        Chunks the new_df by Problem Name, passes each chunk to Gemini, 
        and concatenates the results to avoid token limits.
        """
        
        # Get a list of all unique problem names (e.g., ['trig1', 'trig2', ...])
        # We use dropna() just in case there are blank rows at the bottom
        unique_problems = new_df['Problem Name'].dropna().unique()
        
        processed_chunks = []
        
        total_chunks = (len(unique_problems) + problems_per_chunk - 1) // problems_per_chunk
        
        for i in range(0, len(unique_problems), problems_per_chunk):
            # Grab the next batch of problem names
            batch_names = unique_problems[i : i + problems_per_chunk]
            
            # Filter the DataFrame to only include rows for these specific problems
            chunk_df = new_df[new_df['Problem Name'].isin(batch_names)]
            
            chunk_num = (i // problems_per_chunk) + 1
            print(f"Processing chunk {chunk_num} of {total_chunks} (Problems: {', '.join(batch_names)})...")
            
            try:
                # Pass this small chunk to the Gemini function we wrote earlier
                processed_chunk = self.populate_dataframe_with_gemini(gold_df, chunk_df)
                processed_chunks.append(processed_chunk)
            except Exception as e:
                print(f"Failed to process chunk {chunk_num}. Falling back to raw data. Error: {e}")
                # Preserve the raw scraped data so no problems are silently dropped
                processed_chunks.append(chunk_df)
                
        # Glue all the processed chunks back into one massive DataFrame
        print("All chunks processed. Assembling final DataFrame...")
        if not processed_chunks:
            raise ValueError("No chunks were successfully processed. Check Gemini responses above.")
        final_df = pd.concat(processed_chunks, ignore_index=True)
        
        return final_df

    def generate_curriculum(self):
        # --- Scrape ---
        examples = self.scrape_examples(self.book_url)
        exercises = self.scrape_exercises(self.book_url)

        # --- Populate raw DataFrames (problem_number and population_index carry across both calls) ---
        example_data = self.df_example_population(examples)
        exercise_data = self.df_exercise_population(exercises)

        # --- Save raw scraped output for inspection ---
        pd.set_option('display.max_colwidth', None)
        raw_combined = pd.concat([example_data, exercise_data], ignore_index=True)
        raw_combined.to_excel(f"{self.name}_raw.xlsx", index=False)
        print(f"Exported {len(examples)} examples and {len(exercises)} exercises to {self.name}_raw.xlsx")

        # --- Gemini pass: process examples and exercises separately so prompts stay focused ---
        print("\n--- Processing examples ---")
        final_examples = self.process_dataframe_in_chunks(self.gold_df, example_data, problems_per_chunk=3)

        print("\n--- Processing exercises ---")
        final_exercises = self.process_dataframe_in_chunks(self.gold_df, exercise_data, problems_per_chunk=3)

        # --- Combine and export ---
        final_df = pd.concat([final_examples, final_exercises], ignore_index=True)
        final_df.to_excel(f"{self.name}_final_ready_for_upload.xlsx", index=False)
        print(f"\nExported successfully to {self.name}_final_ready_for_upload.xlsx!")