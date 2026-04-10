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

class Scraper:
    PART_PREFIX = re.compile(r'^[ⓐⓑⓒⓓⓔⓕⓖⓗ]|\([a-h]\)\s*')
    columns = ['Problem Name', 'Row Type', 'Title', 'Body Text', 'Answer',
       'answerType', 'HintID', 'Dependency', 'mcChoices',
       'Images (space delimited)', 'Parent', 'OER src', 'openstax KC', 'KC',
       'Taxonomy', 'License']
    EXERCISE_SECTIONS_TO_SCRAPE = {'verbal', 'algebraic'}

    def __init__(self, url, file_path, name):
        self.url = url
        self.file_path = file_path
        self.name = name
    
    @dataclass
    class Question:
        title: str
        body_text: str = ''
        problems: list[str] = field(default_factory=list)


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

    def scrape_exercises(self, url: str) -> list['Question']:
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
            new_data.loc[self.population_index, 'OER src'] = self.url
            self.population_index += 1

            for p in q.problems:
                new_data.loc[self.population_index, 'Problem Name'] = problem_name_stem + str(self.problem_number)
                new_data.loc[self.population_index, 'Row Type'] = 'step'
                new_data.loc[self.population_index, 'Title'] = p
                self.population_index += 1

            self.problem_number += 1

        return new_data.fillna('')
    
    EXERCISE_TYPE_MAP = {
        'verbal':      'Conceptual',
        'algebraic':   'Algebraic',
        'graphical':   'Graphical',
        'numeric':     'Numeric',
        'technology':  'Technology',
        'extensions':  'Extensions',
        'real-world':  'Applications',
    }
    
    def _detect_exercise_type(self, section_title: str) -> str:
        """Returns a normalised exercise type string based on the section heading."""
        lower = section_title.lower()
        for keyword, tag in self.EXERCISE_TYPE_MAP.items():
            if keyword in lower:
                return tag
        return 'General'

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
            new_data.loc[self.population_index, 'OER src'] = self.url
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