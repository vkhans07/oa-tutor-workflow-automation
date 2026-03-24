import re
import json
import gspread
import requests
from bs4 import BeautifulSoup
from bs4.element import NavigableString
import pandas as pd
from dataclasses import dataclass, field
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
import pandas as pd
import io
import os

# Google Sheets Setup
precalc_url = "https://docs.google.com/spreadsheets/d/10ym29sQyL_axXC-I2xJ2Fexk8eVgR-c74Rxsu3TG_RY/edit?gid=73476181#gid=73476181"
gc = gspread.service_account(filename='credentials.json')
sheet = gc.open_by_url(precalc_url).worksheet("7.2 - Sum and Difference Identities")

book_url = "https://openstax.org/books/precalculus-2e/pages/7-2-sum-and-difference-identities"
list_of_lists = sheet.get_all_values()
df = pd.DataFrame(list_of_lists[1:], columns=list_of_lists[0])
df = df.iloc[:, :16]
df = df.loc[:, ~df.columns.str.contains('^Unnamed')]
df = df.loc[:, df.columns != '']

gold_df = pd.read_excel("gold.xlsx", dtype=str).fillna('')
project_id = json.load(open('credentials.json'))['project_id']

# Initialize Vertex AI
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "credentials.json"
vertexai.init(project=project_id, location="us-central1")

# Use Gemini 1.5 Pro for complex formatting and reasoning tasks
model = GenerativeModel("gemini-2.0-flash-001")

def populate_dataframe_with_gemini(gold_df: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
    """
    Passes a gold standard DataFrame and an incomplete DataFrame to Gemini.
    Returns a fully populated Pandas DataFrame matching the curriculum structure.
    """
    
    # 1. Convert DataFrames to CSV strings
    gold_csv = gold_df.to_csv(index=False)
    new_csv = new_df.to_csv(index=False)

    # 2. Define the exact System Prompt from ChatGPT
    SYSTEM_PROMPT = """
You are a deterministic curriculum CSV transformation agent.

Your task is to transform scraped OpenStax math textbook CSV data into a structured curriculum CSV using a Gold Standard spreadsheet as the reference for structure, pedagogy, and formatting.

You must behave like a structured data processor, not a conversational assistant.

Return only valid CSV output.
No explanations.
No markdown.
No extra text.
Only CSV.

--------------------------------------------------
INPUTS
--------------------------------------------------

You will receive:

1) GOLD STANDARD CSV
This shows the correct curriculum structure and formatting.

2) NEW INPUT CSV
This contains scraped OpenStax problems and steps.

--------------------------------------------------
GOAL
--------------------------------------------------

Transform the NEW INPUT CSV into the curriculum spreadsheet format used in the GOLD STANDARD.

Only add:

- hints
- scaffolds
- answers

Do not create new problems or new steps.

--------------------------------------------------
COLUMN STRUCTURE
--------------------------------------------------

Use the following columns exactly:

Problem Name
Row Type
Title
Body Text
Answer
answerType
HintID
Dependency
mcChoices
Images (space delimited)
Parent
OER src
openstax KC
KC
Taxonomy
License

Column order must remain identical.

Any column after Parent must be empty for step, hint, and scaffold rows.

Only problem rows may contain:

OER src
openstax KC
Validator Check
Time Last Checked
Debug Link
Problem ID
Lesson ID

All other rows leave these empty.

--------------------------------------------------
ROW STRUCTURE
--------------------------------------------------

Each problem follows this structure:

problem row
step row
hint row(s)
scaffold row(s)

If multiple steps exist:

problem
step
hint(s)
scaffold(s)
step
hint(s)
scaffold(s)

Insert a blank separator row after each problem group.

--------------------------------------------------
PROBLEM ROW RULES
--------------------------------------------------

Row Type = problem

Title = descriptive lesson-style title

Examples:

Identifying the Period of a Sine or Cosine Function
Identifying the Amplitude of a Sine or Cosine Function

Problem rows contain OpenStax metadata.

Do not change OpenStax source links.

--------------------------------------------------
STEP ROW RULES
--------------------------------------------------

Row Type = step

Title contains the actual question.

Examples:

Determine the period of the function f(x) = sin(3x)
What is the amplitude of the sinusoidal function?

LaTeX is allowed and preferred.

If a step says:

verify an identity

rewrite as:

evaluate the expression

so a concrete answer can be generated.

Do not create new steps.

Do not modify mathematical meaning.

--------------------------------------------------
HINT RULES
--------------------------------------------------

Row Type = hint

1 to 3 hints per step.

Hints must be short and concise.

Hints should resemble short instructional phrases.

Examples:

Finding period given equation
Finding amplitude given equation
Understanding stretching
Using sine formula

Hints guide thinking but do not solve the problem.

Hints must be relevant to the step.

Body Text must be empty.

Answer must be empty.

answerType must be empty.

HintID must be:

h1
h2
h3

Restart numbering for each step.

Dependencies:

h2 may depend on h1

Dependency column contains:

h1

If no dependency exists, leave empty.

--------------------------------------------------
SCAFFOLD RULES
--------------------------------------------------

Row Type = scaffold

Scaffolds push students step-by-step.

Scaffolds correspond to hints.

Scaffold titles are short instructional labels.

Examples:

Finding B
Finding A
Definition of stretching
Applying formula

HintID:

s1
s2
s3

Dependency must reference the hint.

Example:

s1 depends on h1

Dependency = h1

Scaffolds must contain answers.

--------------------------------------------------
ANSWER RULES
--------------------------------------------------

Hints:

Answer = empty

Scaffolds:

Answer must be filled.

Answer should match the step solution.

Keep answers concise.

Use LaTeX when appropriate.

--------------------------------------------------
ANSWERTYPE RULES
--------------------------------------------------

numeric

for exact values

Examples:

π/3
2
1/2
5

algebraic

for expressions or variables

Examples:

2π/B
A sin(x)
x + 1

mc

for multiple choice

mcChoices must contain:

choice1|choice2|choice3|choice4

Choices must be mathematically reasonable.

--------------------------------------------------
LATEX RULES
--------------------------------------------------

LaTeX is allowed and preferred.

It does not need to match the Gold Standard exactly.

Keep math readable.

Do not remove LaTeX.

Do not alter mathematical meaning.

--------------------------------------------------
DEPENDENCY RULES
--------------------------------------------------

Hints may depend on earlier hints.

Scaffolds must depend on hints.

Dependencies must use HintID values.

Examples:

h2 depends on h1
s1 depends on h1

--------------------------------------------------
STRICT RULES
--------------------------------------------------

Do not create new problems.
Do not create new steps.
Do not change problem names.
Do not remove rows.
Only add hints, scaffolds, and answers.

Keep CSV structure identical.

--------------------------------------------------
CSV OUTPUT RULES
--------------------------------------------------

Return only CSV.

No markdown.
No code blocks.
No commentary.
No explanation.
No headers outside CSV.
No extra whitespace.

CSV must be readable by:

pandas.read_csv(io.StringIO(output))

--------------------------------------------------
INPUT PLACEHOLDERS
--------------------------------------------------

GOLD STANDARD CSV:

[GOLD_STANDARD_CSV]

NEW INPUT CSV:

[NEW_INPUT_CSV]

--------------------------------------------------
FINAL INSTRUCTION
--------------------------------------------------

Transform the NEW INPUT CSV into the curriculum format using the GOLD STANDARD as structural and pedagogical reference, generate hints and scaffolds for every step, apply answer and dependency rules, and return only the final valid CSV.
"""

    # 3. Inject the data using .replace() to avoid f-string curly brace conflicts with LaTeX
    final_prompt = SYSTEM_PROMPT.replace("[GOLD_STANDARD_CSV]", gold_csv)
    final_prompt = final_prompt.replace("[NEW_INPUT_CSV]", new_csv)

    # 4. Use a very low temperature so it acts like a rigid data processor
    generation_config = GenerationConfig(
        temperature=0.0, 
    )

    print("Sending data to Gemini for transformation...")
    response = model.generate_content(
        final_prompt,
        generation_config=generation_config
    )

    # 5. Clean any accidental markdown blocks that LLMs sometimes hallucinate despite instructions
    clean_csv_string = response.text.strip()
    if clean_csv_string.startswith("```"):
        # Strip out ```csv and the trailing ```
        clean_csv_string = clean_csv_string.split("\n", 1)[-1].rsplit("\n", 1)[0].strip()

    # 6. Load it straight back into a pandas DataFrame!
    try:
        completed_df = pd.read_csv(io.StringIO(clean_csv_string))
        return completed_df
    except Exception as e:
        print(f"Failed to parse CSV. Raw output was:\n{clean_csv_string}")
        raise e

def process_dataframe_in_chunks(gold_df: pd.DataFrame, new_df: pd.DataFrame, problems_per_chunk: int = 3) -> pd.DataFrame:
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
            processed_chunk = populate_dataframe_with_gemini(gold_df, chunk_df)
            processed_chunks.append(processed_chunk)
        except Exception as e:
            print(f"Failed to process chunk {chunk_num}. Skipping to next. Error: {e}")
            # Depending on how strict you want to be, you might append the raw chunk_df here 
            # so you don't lose the data, or just let it skip.
            
    # Glue all the processed chunks back into one massive DataFrame
    print("All chunks processed. Assembling final DataFrame...")
    final_df = pd.concat(processed_chunks, ignore_index=True)
    
    return final_df

PART_PREFIX = re.compile(r'^[ⓐⓑⓒⓓⓔⓕⓖⓗ]|\([a-h]\)\s*')

@dataclass
class Question:
    title: str
    body_text: str = ''
    problems: list[str] = field(default_factory=list)

def strip_part_prefix(text: str) -> str:
    return PART_PREFIX.sub('', text).strip()

def parse_mathml(node) -> str:
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
    parsed_children = [parse_mathml(c) for c in children]

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

def extract_text_bs(element) -> str:
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
                latex_code = parse_mathml(actual_math)
                
            # Replace the entire HTML node (visuals + math tags) with the clean LaTeX
            math_wrapper.replace_with(f" $${latex_code}$$ ")
        else:
            math_wrapper.decompose()

    # Now extract the plain text
    text = element.get_text(separator=' ', strip=True)
    
    # Clean up excessive spacing
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def get_raw_html_via_api(url: str) -> str:
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

def scrape_examples(url: str) -> list[Question]:
    raw_html = get_raw_html_via_api(url)
    soup = BeautifulSoup(raw_html, 'html.parser')
    examples: list[Question] = []

    example_elements = soup.find_all(attrs={"data-type": "example"})
    if not example_elements:
        example_elements = soup.find_all("example")

    for item in example_elements:
        title_el = item.find(attrs={"data-type": "title"}) or item.find("title")
        title = extract_text_bs(title_el) if title_el else ""

        problem_el = item.find(attrs={"data-type": "problem"}) or item.find("problem")
        if not problem_el:
            continue

        title_in_problem = problem_el.find(attrs={"data-type": "title"}) or problem_el.find("title")
        if title_in_problem:
            title_in_problem.decompose()

        circled_items = problem_el.select("ol.circled > li, list > item")

        if len(circled_items) > 1:
            body_text_el = problem_el.find(["p", "para"])
            body_text = extract_text_bs(body_text_el) if body_text_el else ""
            problems = [strip_part_prefix(extract_text_bs(part)) for part in circled_items]
        else:
            body_text = ''
            problems = [extract_text_bs(problem_el)]

        examples.append(Question(title=title, body_text=body_text, problems=problems))

    return examples

def df_population(questions: list[Question], problem_name_stem: str = '') -> pd.DataFrame:
    new_data = pd.DataFrame(columns=df.columns, dtype=str)
    number = 1
    index = 0

    for q in questions:
        if len(q.problems) == 0:
            number += 1
            continue

        new_data.loc[index, 'Problem Name'] = problem_name_stem + str(number)
        new_data.loc[index, 'Row Type'] = 'problem'
        new_data.loc[index, 'Title'] = q.title
        new_data.loc[index, 'Body Text'] = q.body_text
        index += 1

        for p in q.problems:
            new_data.loc[index, 'Problem Name'] = problem_name_stem + str(number)
            new_data.loc[index, 'Row Type'] = 'step'
            new_data.loc[index, 'Title'] = p
            index += 1

        number += 1

    return new_data.fillna('')

def main():
    examples = scrape_examples(book_url)
    new_data = df_population(examples, problem_name_stem='trig')
    pd.set_option('display.max_colwidth', None)
    print(new_data.head(20))
    new_data.to_excel("trig_examples.xlsx", index=False)
    print(f"Successfully exported {len(examples)} examples to trig_examples.xlsx!")
    final_df = process_dataframe_in_chunks(gold_df, new_data, problems_per_chunk=3)
    final_df.to_excel("final_ready_for_upload.xlsx", index=False)
    print("Exported successfully to final_ready_for_upload.xlsx!")

if __name__ == "__main__":
    main()