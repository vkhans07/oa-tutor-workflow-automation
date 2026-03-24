import re
import json
import gspread
import requests
from bs4 import BeautifulSoup, NavigableString
import pandas as pd
from dataclasses import dataclass, field

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
        text = text.replace('π', '\\pi ').replace('θ', '\\theta ').replace('°', '^\\circ ').replace('−', '-')
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
            math_wrapper.replace_with(f" ${latex_code}$ ")
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

if __name__ == "__main__":
    main()