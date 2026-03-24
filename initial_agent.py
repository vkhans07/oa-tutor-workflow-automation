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

def extract_text_with_js(locator: Locator) -> str:
    """
    Uses JavaScript to extract text. Bypasses Playwright's visual visibility checks
    (which hide MathJax) and prevents duplicate math text by reading only the assistive MML.
    """
    return locator.evaluate("""el => {
        function getText(node) {
            let text = '';
            node.childNodes.forEach(child => {
                if (child.nodeType === Node.TEXT_NODE) {
                    text += child.textContent;
                } else if (child.nodeType === Node.ELEMENT_NODE) {
                    // Skip title node so it doesn't bleed into body
                    if (child.getAttribute('data-type') === 'title') return;
                    
                    // Handle MathJax: pull from assistive-mml to get clean text
                    if (child.tagName && child.tagName.toLowerCase() === 'mjx-container') {
                        const mml = child.querySelector('mjx-assistive-mml');
                        // Pad with spaces so math doesn't fuse with surrounding words
                        text += ' ' + (mml ? mml.textContent : child.textContent) + ' ';
                        return; // Stop recursing into this math node
                    }
                    
                    text += getText(child);
                }
            });
            return text;
        }
        return getText(el).replace(/\s+/g, ' ').trim();
    }""")

def scrape_examples(url: str) -> list[Question]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")

        examples: list[Question] = []
        
        # Target the problem container directly based on the DOM structure
        problem_elements = page.locator("[data-type='problem']").all()

        for problem_loc in problem_elements:
            title_loc = problem_loc.locator("[data-type='title']").first
            title = title_loc.inner_text().strip() if title_loc.count() > 0 else ""

            circled_items = problem_loc.locator("ol.circled > li").all()

            if len(circled_items) > 1:
                body_text_loc = problem_loc.locator("p").first
                body_text = extract_text_with_js(body_text_loc) if body_text_loc.count() > 0 else ""
                problems = [strip_part_prefix(extract_text_with_js(part)) for part in circled_items]
            else:
                body_text = ''
                problems = [extract_text_with_js(problem_loc)]

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
    new_data.to_excel("trig_examples.xlsx", index=False)

if __name__ == "__main__":
    main()