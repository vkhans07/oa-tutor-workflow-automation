import re
import json
import gspread
from playwright.sync_api import sync_playwright, Locator
import pandas as pd
from dataclasses import dataclass, field

precalc_url = "https://docs.google.com/spreadsheets/d/10ym29sQyL_axXC-I2xJ2Fexk8eVgR-c74Rxsu3TG_RY/edit?gid=73476181#gid=73476181"
gc = gspread.service_account(filename='credentials.json')
sheet = gc.open_by_url(precalc_url).worksheet("7.2 - Sum and Difference Identities")

book_url = "https://openstax.org/books/precalculus-2e/pages/7-2-sum-and-difference-identities"
list_of_lists = sheet.get_all_values()
df = pd.DataFrame(list_of_lists[1:], columns=list_of_lists[0])
df = df.iloc[:, :16]
df = df.loc[:, ~df.columns.str.contains('^Unnamed')]
df = df.loc[:, df.columns != '']

PART_PREFIX = re.compile(r'^[ⓐⓑⓒⓓⓔⓕⓖⓗ]|\([a-h]\)\s*')

@dataclass
class Question:
    title: str
    body_text: str = ''
    problems: list[str] = field(default_factory=list)

def strip_part_prefix(text: str) -> str:
    return PART_PREFIX.sub('', text).strip()

def extract_text_with_latex(locator: Locator) -> str:
    """
    Extracts text from the unrendered DOM fragment. 
    MathJax is not present on this blank page, so the raw LaTeX delimiters are fully intact.
    """
    text = locator.inner_text().strip()
    
    text = re.sub(r'\\\(', '$', text)
    text = re.sub(r'\\\)', '$', text)
    text = re.sub(r'\\\[', '$$', text)
    text = re.sub(r'\\\]', '$$', text)
    
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def find_content_string(obj):
    """
    Recursively search the JSON payload to find the largest string containing raw LaTeX 
    delimiters or example tags. This bypasses brittle schema checks entirely.
    """
    candidates = []
    
    def search(o, depth=0):
        if depth > 100: return # Prevent infinite recursion on weird JSON
        if isinstance(o, str):
            # We only care about strings that contain raw LaTeX or OpenStax example tags
            if '\\(' in o or '<example' in o or 'data-type="example"' in o:
                candidates.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                search(v, depth + 1)
        elif isinstance(o, list):
            for item in o:
                search(item, depth + 1)
                
    search(obj)
    
    if not candidates:
        return ""
        
    # The longest string found will be the massive chapter payload
    return max(candidates, key=len)

def scrape_examples(url: str) -> list[Question]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")

        raw_content_html = ""
        
        # 1. Try to get JSON from Next.js State
        next_data_loc = page.locator("script#__NEXT_DATA__")
        if next_data_loc.count() > 0:
            try:
                next_text = next_data_loc.inner_text().strip()
                if next_text:
                    next_data = json.loads(next_text)
                    raw_content_html = find_content_string(next_data)
            except Exception as e:
                print("Failed to parse Next.js JSON:", e)

        # 2. Try the OpenStax API JSON fallback if Next.js data fails
        if not raw_content_html:
            html_content = page.content()
            api_url_match = re.search(r'(https?://[^"\'\s]+/apps/archive/[^"\'\s]+/contents/[^"\'\s]+\.json|/apps/archive/[^"\'\s]+/contents/[^"\'\s]+\.json)', html_content)
            
            if api_url_match:
                api_url = api_url_match.group(1)
                if api_url.startswith('/'):
                    api_url = "https://openstax.org" + api_url
                print(f"Fetching raw content via API: {api_url}")
                try:
                    api_response = page.request.get(api_url)
                    api_data = api_response.json()
                    raw_content_html = find_content_string(api_data)
                except Exception as e:
                    print("Failed to fetch from API URL:", e)

        if not raw_content_html:
            print("ERROR: Could not find any HTML containing raw LaTeX or examples in the JSON data.")
            browser.close()
            return []

        parser_page = browser.new_page()
        parser_page.set_content(raw_content_html)

        examples: list[Question] = []
        # Support both standard HTML and OpenStax CNXML tags
        example_elements = parser_page.locator("[data-type='example'], example").all()

        for item in example_elements:
            title_loc = item.locator("[data-type='title'], title").first
            title = title_loc.inner_text().strip() if title_loc.count() > 0 else ""

            problem_loc = item.locator("[data-type='problem'], problem").first
            if problem_loc.count() == 0:
                continue

            # Remove title node from problem DOM before extracting text
            problem_loc.evaluate("el => { const t = el.querySelector('[data-type=title], title'); if (t) t.remove(); }")

            # Detect multi-part questions (HTML or CNXML lists)
            circled_items = problem_loc.locator("ol.circled > li, list > item").all()

            if len(circled_items) > 1:
                body_text_loc = problem_loc.locator("p, para").first
                body_text = extract_text_with_latex(body_text_loc) if body_text_loc.count() > 0 else ""
                problems = [strip_part_prefix(extract_text_with_latex(part)) for part in circled_items]
            else:
                body_text = ''
                problems = [extract_text_with_latex(problem_loc)]

            examples.append(Question(title=title, body_text=body_text, problems=problems))

        browser.close()
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
    new_data.to_csv("trig_examples.csv", index=False)

if __name__ == "__main__":
    main()