import copy
import re
import requests
from bs4 import BeautifulSoup
from bs4.element import NavigableString
from dataclasses import dataclass, field
import pandas as pd


class Scraper:
    PART_PREFIX = re.compile(r'^[ⓐⓑⓒⓓⓔⓕⓖⓗ]|\([a-h]\)\s*')

    COLUMNS = [
        'Problem Name', 'Row Type', 'Title', 'Body Text', 'Answer',
        'answerType', 'HintID', 'Dependency', 'mcChoices',
        'Images (space delimited)', 'Parent', 'OER src', 'openstax KC', 'KC',
        'Taxonomy', 'License',
    ]

    # Sections to include when scraping exercises (lowercase)
    EXERCISE_SECTIONS = {'verbal', 'algebraic'}

    EXERCISE_TYPE_MAP = {
        'verbal':      'Conceptual',
        'algebraic':   'Algebraic',
        'graphical':   'Graphical',
        'numeric':     'Numeric',
        'technology':  'Technology',
        'extensions':  'Extensions',
        'real-world':  'Applications',
    }

    @dataclass
    class Question:
        title: str
        body_text: str = ''
        problems: list[str] = field(default_factory=list)
        images: list[str] = field(default_factory=list)   # image URLs found in the problem
        exercise_type: str = 'Example'                     # 'Example' or mapped exercise type

    def __init__(self, url: str, file_path: str, name: str):
        self.url = url
        self.file_path = file_path
        self.name = name
        self.problem_number = 1
        self.population_index = 0

    # ── HTML fetching ────────────────────────────────────────────────────────

    def _fetch_html(self, url: str) -> str:
        """Fetch raw chapter HTML, preferring the OpenStax archive JSON API."""
        headers = {'User-Agent': 'Mozilla/5.0'}
        print(f"Fetching: {url}")
        resp = requests.get(url, headers=headers)
        resp.encoding = 'utf-8'

        for pattern in [
            r'(https://openstax\.org/apps/archive/[^/]+/contents/[^/"\'\s]+\.json)',
            r'(/apps/archive/[^/]+/contents/[^/"\'\s]+\.json)',
        ]:
            m = re.search(pattern, resp.text)
            if m:
                archive_url = m.group(1)
                if archive_url.startswith('/'):
                    archive_url = 'https://openstax.org' + archive_url
                print(f"  Archive API: {archive_url}")
                api_resp = requests.get(archive_url, headers=headers)
                api_resp.encoding = 'utf-8'
                if api_resp.status_code == 200:
                    data = api_resp.json()
                    if 'content' in data:
                        print("  Fetched chapter content from API.")
                        return data['content']

        print("  Falling back to raw page HTML.")
        return resp.text

    # ── Math / text extraction ───────────────────────────────────────────────

    def _parse_mathml(self, node) -> str:
        """Recursively convert a MathML node to a LaTeX string."""
        if isinstance(node, NavigableString):
            return str(node).strip()
        if not hasattr(node, 'name') or not node.name:
            return ''

        name = node.name.lower()
        if name in ('annotation', 'annotation-xml'):
            return ''
        if name in ('mi', 'mn', 'mo', 'mtext'):
            return node.get_text(strip=True)

        children = [
            c for c in node.children
            if not (isinstance(c, NavigableString) and not str(c).strip())
        ]
        ch = [self._parse_mathml(c) for c in children]

        if name == 'mfrac'   and len(ch) >= 2: return f'\\frac{{{ch[0]}}}{{{ch[1]}}}'
        if name == 'msup'    and len(ch) >= 2: return f'{ch[0]}^{{{ch[1]}}}'
        if name == 'msub'    and len(ch) >= 2: return f'{ch[0]}_{{{ch[1]}}}'
        if name == 'msubsup' and len(ch) >= 3: return f'{ch[0]}_{{{ch[1]}}}^{{{ch[2]}}}'
        if name == 'msqrt':
            return f'\\sqrt{{{" ".join(ch)}}}'
        if name == 'mroot'   and len(ch) >= 2: return f'\\sqrt[{ch[1]}]{{{ch[0]}}}'
        if name == 'mfenced':
            o = node.get('open', '(')
            c_ = node.get('close', ')')
            return f'\\left{o}{"".join(ch)}\\right{c_}'
        return ''.join(ch)

    def _extract_text(self, element) -> str:
        """
        Extract clean text from a BS4 element, converting MathML to LaTeX.
        Does NOT collect images — call _extract_images separately if needed.
        """
        if not element:
            return ''
        for wrapper in element.find_all(['math', 'mjx-container']):
            math = wrapper if wrapper.name == 'math' else wrapper.find('math')
            if math:
                ann = math.find('annotation', attrs={'encoding': 'application/x-tex'})
                latex = ann.text.strip() if (ann and ann.text) else self._parse_mathml(math)
                wrapper.replace_with(f' $${latex}$$ ')
            else:
                wrapper.decompose()
        return re.sub(r'\s+', ' ', element.get_text(separator=' ', strip=True)).strip()

    def _extract_images(self, element) -> list[str]:
        """Return all image URLs found inside an element."""
        if not element:
            return []
        urls = []
        for img in element.find_all('img'):
            src = img.get('src') or img.get('data-src') or ''
            if src:
                if src.startswith('/'):
                    src = 'https://openstax.org' + src
                urls.append(src)
        return urls

    def _strip_part_prefix(self, text: str) -> str:
        return self.PART_PREFIX.sub('', text).strip()

    def _detect_exercise_type(self, section_title: str) -> str:
        lower = section_title.lower()
        for keyword, tag in self.EXERCISE_TYPE_MAP.items():
            if keyword in lower:
                return tag
        return 'General'

    # ── Scraping ─────────────────────────────────────────────────────────────

    def scrape(self, mode: str = 'examples') -> list[Question]:
        """
        Scrape questions from self.url.

        mode:
          'examples'  – worked examples only  (data-type="example")
          'exercises' – exercise sections only (filtered by EXERCISE_SECTIONS)
          'both'      – examples first, then exercises, shared counter
        """
        if mode not in ('examples', 'exercises', 'both'):
            raise ValueError(f"mode must be 'examples', 'exercises', or 'both'; got {mode!r}")

        html = self._fetch_html(self.url)
        soup = BeautifulSoup(html, 'html.parser')

        questions: list[self.Question] = []

        if mode in ('examples', 'both'):
            questions.extend(self._scrape_examples(soup))

        if mode in ('exercises', 'both'):
            questions.extend(self._scrape_exercises(soup))

        # Report any questions that carry images
        flagged = [(i + 1, q) for i, q in enumerate(questions) if q.images]
        if flagged:
            print(f"\n  {len(flagged)} question(s) contain images — upload to imgur and update the sheet:")
            for num, q in flagged:
                print(f"    Q{num} ({q.title[:60]})")
                for url in q.images:
                    print(f"      {url}")

        return questions

    def _scrape_examples(self, soup: BeautifulSoup) -> list[Question]:
        questions = []
        items = soup.find_all(attrs={"data-type": "example"}) or soup.find_all("example")

        for item in items:
            title_el = item.find(attrs={"data-type": "title"}) or item.find("title")
            title = self._extract_text(title_el) if title_el else ''

            problem_el = item.find(attrs={"data-type": "problem"}) or item.find("problem")
            if not problem_el:
                continue

            # Remove nested title so it doesn't bleed into body text
            inner_title = problem_el.find(attrs={"data-type": "title"}) or problem_el.find("title")
            if inner_title:
                inner_title.decompose()

            images = self._extract_images(problem_el)
            circled = problem_el.select("ol.circled > li, list > item")

            if len(circled) > 1:
                body_el = problem_el.find(["p", "para"])
                body = self._extract_text(body_el) if body_el else ''
                problems = [self._strip_part_prefix(self._extract_text(copy.copy(p))) for p in circled]
            else:
                body = ''
                problems = [self._extract_text(problem_el)]

            questions.append(self.Question(
                title=title,
                body_text=body,
                problems=problems,
                images=images,
                exercise_type='Example',
            ))

        return questions

    def _scrape_exercises(self, soup: BeautifulSoup) -> list[Question]:
        questions = []

        container = (
            soup.find(class_='os-section-exercises-container')
            or soup.find(class_='section-exercises')
            or soup
        )

        for section in container.find_all('section', attrs={"data-depth": "2"}):
            title_el = (
                section.find(attrs={"data-type": "title"}, recursive=False)
                or section.find(attrs={"data-type": "title"})
            )
            if not title_el:
                continue

            section_title = title_el.get_text(strip=True)
            if section_title.lower() not in self.EXERCISE_SECTIONS:
                continue

            exercise_type = self._detect_exercise_type(section_title)

            # Shared instruction line (e.g. "For the following exercises, …")
            instruction = ''
            for sibling in section.children:
                if hasattr(sibling, 'get_text') and 'following exercises' in sibling.get_text().lower():
                    instruction = self._extract_text(copy.copy(sibling)) + ' '
                    break

            for item in section.find_all(attrs={"data-type": "exercise"}):
                container_el = item.find(class_='os-problem-container')
                if not container_el:
                    continue

                clone = copy.copy(container_el)
                images = self._extract_images(clone)
                circled = clone.select("ol.circled > li, list > item")

                if len(circled) > 1:
                    for el in clone.select("ol.circled, list"):
                        el.decompose()
                    body = (instruction + self._extract_text(clone)).strip()
                    problems = [self._strip_part_prefix(self._extract_text(copy.copy(p))) for p in circled]
                else:
                    body = instruction.strip()
                    problems = [self._extract_text(clone)]

                questions.append(self.Question(
                    title=section_title,
                    body_text=body,
                    problems=problems,
                    images=images,
                    exercise_type=exercise_type,
                ))

        return questions

    # ── DataFrame population ─────────────────────────────────────────────────

    def df_population(
        self,
        questions: list[Question],
        problem_name_stem: str = '',
    ) -> pd.DataFrame:
        """
        Convert a list of Questions into the OATutor DataFrame format.
        Uses self.name as the stem if problem_name_stem is not provided.
        Appends a temporary _exercise_type column (stripped before Gemini upload).
        Continues from self.problem_number and self.population_index so multiple
        scrape() calls share the same counter.
        """
        stem = problem_name_stem or self.name
        cols = self.COLUMNS + ['_exercise_type']
        rows = []

        for q in questions:
            if not q.problems:
                self.problem_number += 1
                continue

            image_str = ' '.join(q.images)

            # Problem row
            rows.append({
                'Problem Name':          stem + str(self.problem_number),
                'Row Type':              'problem',
                'Title':                 q.title,
                'Body Text':             q.body_text,
                'OER src':               self.url,
                'Images (space delimited)': image_str,
                '_exercise_type':        q.exercise_type,
            })

            # Step rows
            for p in q.problems:
                rows.append({
                    'Problem Name': stem + str(self.problem_number),
                    'Row Type':     'step',
                    'Title':        p,
                    '_exercise_type': q.exercise_type,
                })

            self.problem_number += 1

        df = pd.DataFrame(rows, columns=cols)
        return df.fillna('')