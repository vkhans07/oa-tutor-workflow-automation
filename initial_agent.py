import re
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
df = df.iloc[:, :16]  # keep only up to 'P'
df = df.loc[:, ~df.columns.str.contains('^Unnamed')]
df = df.loc[:, df.columns != '']

# Matches circled letters (ⓐⓑⓒ...) or plain (a)(b)(c) at the start of a string
PART_PREFIX = re.compile(r'^[ⓐⓑⓒⓓⓔⓕⓖⓗ]|\([a-h]\)\s*')


@dataclass
class Question:
    title: str
    body_text: str = ''  # shared setup text for multi-part questions
    problems: list[str] = field(default_factory=list)


def strip_part_prefix(text: str) -> str:
    return PART_PREFIX.sub('', text).strip()


def extract_text_with_latex(locator: Locator) -> str:
    """
    Reconstructs text by walking child nodes, replacing os-math-in-para spans
    with raw LaTeX sourced from MathJax v3's annotation tag inside mjx-assistive-mml.
    Falls back to MathJax v2 script tag if annotation is not found.
    Skips data-type='title' nodes and mjx-container rendered output entirely.
    """
    result = locator.evaluate("""el => {
        function extractNode(node) {
            let text = '';
            node.childNodes.forEach(child => {
                if (child.nodeType === Node.TEXT_NODE) {
                    text += child.textContent;

                } else if (child.dataset && child.dataset.type === 'title') {
                    // Skip title nodes — handled separately
                    return;

                } else if (child.classList && child.classList.contains('os-math-in-para')) {
                    // MathJax v3: raw LaTeX lives in annotation tag inside mjx-assistive-mml
                    const annotation = child.querySelector('mjx-assistive-mml annotation[encoding="application/x-tex"]');
                    if (annotation) {
                        text += '$' + annotation.textContent.trim() + '$';
                        return;
                    }
                    // MathJax v2 fallback: raw LaTeX in script tag
                    const script = child.querySelector('script[type="math/tex"]');
                    if (script) {
                        text += '$' + script.textContent.trim() + '$';
                        return;
                    }
                    // Last resort: plain text (will be garbled for fractions etc.)
                    text += child.textContent;

                } else if (child.tagName === 'MJX-CONTAINER' || child.tagName === 'mjx-container') {
                    // Skip rendered MathJax output — we already got LaTeX above
                    return;

                } else {
                    text += extractNode(child);
                }
            });
            return text;
        }
        return extractNode(el).replace(/\s+/g, ' ').trim();
    }""")
    return result


def scrape_examples(url: str) -> list[Question]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")

        examples: list[Question] = []
        example_elements = page.locator("[data-type='example']").all()

        for item in example_elements:
            # Title
            title_loc = item.locator("[data-type='title']").first
            title = title_loc.inner_text().strip() if title_loc.count() > 0 else ""

            problem_loc = item.locator("[data-type='problem']").first
            if problem_loc.count() == 0:
                continue

            # Remove title node from problem DOM before extracting text
            problem_loc.evaluate("el => { const t = el.querySelector('[data-type=title]'); if (t) t.remove(); }")

            # Detect multi-part questions by OpenStax's circled ol class
            circled_items = problem_loc.locator("ol.circled > li").all()

            if len(circled_items) > 1:
                # Body text = the shared <p> setup before the list (e.g. "Given sin α = 3/5 ... find")
                body_text = extract_text_with_latex(problem_loc.locator("p").first)
                # Each circled list item becomes its own step, strip ⓐⓑ prefix
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

        # Problem header row — title + shared body text
        new_data.loc[index, 'Problem Name'] = problem_name_stem + str(number)
        new_data.loc[index, 'Row Type'] = 'problem'
        new_data.loc[index, 'Title'] = q.title
        new_data.loc[index, 'Body Text'] = q.body_text
        index += 1

        # One step row per part — problem body goes in Title column
        for p in q.problems:
            new_data.loc[index, 'Problem Name'] = problem_name_stem + str(number)
            new_data.loc[index, 'Row Type'] = 'step'
            new_data.loc[index, 'Title'] = p
            index += 1

        number += 1

    return new_data.fillna('')


# next steps:
# handle multiple choice
# handle exercises

def main():
    examples = scrape_examples(book_url)
    new_data = df_population(examples, problem_name_stem='trig')
    pd.set_option('display.max_colwidth', None)
    print(new_data.head(20))
    new_data.to_excel("trig_examples.xlsx", index=False)


if __name__ == "__main__":
    main()