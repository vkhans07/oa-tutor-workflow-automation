"""
OATutor Pipeline Agent
Orchestrates: scrape -> generate -> validate -> fix -> validate

Usage:
    python agent.py --url <openstax_url> --name <stem> --gold <gold_file>
                   [--mode examples|exercises|both]
                   [--chunk 3]
                   [--sheet <sheet_name>]
                   [--skip-generate]
                   [--skip-fix]
                   [--no-content-fix]

Examples:
    python agent.py --url https://openstax.org/books/precalculus-2e/pages/8-8-vectors \
                    --name vector --gold gold.xlsx

    python agent.py --url <url> --name trig --gold gold.xlsx --mode both --chunk 5
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import openpyxl
import pandas as pd
from dotenv import load_dotenv
import google.genai as genai

from scrape         import Scraper
from generate_hints import HintGenerator
from validator      import validate
from fixer          import fix

load_dotenv()

INPUT_DIR = Path('input_workbooks')
GOLD_DIR  = Path('gold_workbooks')
INPUT_DIR.mkdir(exist_ok=True)


# -- Helpers ------------------------------------------------------------------

def _gemini_client():
    key = os.getenv('GEMINI_API_KEY')
    if not key:
        raise EnvironmentError('GEMINI_API_KEY not set in environment or .env file')
    return genai.Client(api_key=key)


def _output_path(name):
    stamp = datetime.now().strftime('%Y%m%d_%H%M')
    return INPUT_DIR / f'{name}_{stamp}.xlsx'


def _header(text):
    width = len(text) + 4
    print(f'\n{"=" * width}')
    print(f'  {text}')
    print(f'{"=" * width}')


# -- Pipeline stages ----------------------------------------------------------

def stage_scrape(url, name, mode):
    _header(f'STAGE 1 / SCRAPE  [{mode}]')
    scraper   = Scraper(url=url, file_path='', name=name)
    questions = scraper.scrape(mode=mode)
    df        = scraper.df_population(questions)
    n_problems = scraper.problem_number - 1
    print(f'  {n_problems} problem(s) scraped, {len(df)} rows total.')
    return df


def stage_generate(raw_df, gold_path, chunk):
    _header('STAGE 2 / GENERATE HINTS & SCAFFOLDS')
    gold_df   = pd.read_excel(gold_path, dtype=str).fillna('')
    generator = HintGenerator()
    draft_df  = generator.process_dataframe_in_chunks(
        gold_df=gold_df,
        new_df=raw_df,
        problems_per_chunk=chunk,
    )
    print(f'  Generation complete — {len(draft_df)} rows.')
    return draft_df


def stage_save(df, out_path):
    df.to_excel(out_path, index=False)
    print(f'  Saved -> {out_path}')
    return out_path


def stage_validate(path, sheet_name, label=''):
    tag = f'VALIDATE [{label}]' if label else 'VALIDATE'
    _header(f'STAGE / {tag}')
    issues = validate(
        target_path=str(path),
        sheet_name=sheet_name,
        return_issues=True,
    )
    print(f'  {len(issues)} issue(s) found.')
    return issues


def stage_fix(path, issues, sheet_name, skip_content):
    _header('STAGE / FIX')
    wb = openpyxl.load_workbook(str(path))
    ws = wb[sheet_name] if sheet_name else wb.active

    client = None
    if not skip_content:
        try:
            client = _gemini_client()
        except EnvironmentError as e:
            print(f'  WARNING: {e} — content fixes skipped.')

    changelog = fix(ws, issues, client=client, skip_content=skip_content)
    wb.save(str(path))
    print(f'  {len(changelog)} fix(es) applied. Saved -> {path}')
    return changelog


# -- Main ---------------------------------------------------------------------

def run(url, name, gold_path, mode='examples', chunk=3, sheet_name=None,
        skip_generate=False, skip_fix=False, no_content_fix=False):

    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    out_path = _output_path(name)

    _header(f'OATutor Pipeline  |  {name}  |  {mode}')
    print(f'  URL:  {url}')
    print(f'  Gold: {gold_path}')
    print(f'  Out:  {out_path}')

    # Stage 1: Scrape
    raw_df = stage_scrape(url, name, mode)

    if skip_generate:
        _header('STAGE 2 / SKIPPED (--skip-generate)')
        stage_save(raw_df, out_path)
        _header('DONE')
        print(f'  Output: {out_path}')
        return

    # Stage 2: Generate
    draft_df = stage_generate(raw_df, gold_path, chunk)
    stage_save(draft_df, out_path)

    # Stage 3: Validate (pre-fix)
    issues = stage_validate(out_path, sheet_name, label='pre-fix')

    if not issues:
        _header('DONE — sheet is clean')
        print(f'  Output: {out_path}')
        return

    if skip_fix:
        _header('FIX SKIPPED (--skip-fix)')
        _header('DONE')
        print(f'  Output:  {out_path}')
        print(f'  WARNING: {len(issues)} issue(s) unresolved — see Validator Check column.')
        return

    # Stage 4: Fix
    stage_fix(out_path, issues, sheet_name, no_content_fix)

    # Stage 5: Validate (post-fix)
    remaining = stage_validate(out_path, sheet_name, label='post-fix')

    _header('DONE')
    print(f'  Output:        {out_path}')
    print(f'  Issues before: {len(issues)}')
    print(f'  Issues after:  {len(remaining)}')
    if remaining:
        print(f'  WARNING: {len(remaining)} issue(s) remain — see Validator Check column.')
    else:
        print('  All clear.')


# -- Entry point --------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='OATutor end-to-end pipeline agent',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('--url',            required=True,
                        help='OpenStax page URL to scrape')
    parser.add_argument('--name',           required=True,
                        help='Problem name stem, e.g. "vector" or "trig"')
    parser.add_argument('--gold',           required=True,
                        help='Gold standard .xlsx filename or path (checked in gold_workbooks/ if not found directly)')
    parser.add_argument('--mode',           default='examples',
                        choices=['examples', 'exercises', 'both'],
                        help='What to scrape from the page')
    parser.add_argument('--chunk',          default=3, type=int,
                        help='Problems per Gemini chunk')
    parser.add_argument('--sheet',          default=None,
                        help='Worksheet name (default: active sheet)')
    parser.add_argument('--skip-generate',  action='store_true',
                        help='Skip Gemini generation — output raw scraped xlsx only')
    parser.add_argument('--skip-fix',       action='store_true',
                        help='Skip the fix stage — validate only')
    parser.add_argument('--no-content-fix', action='store_true',
                        help='Structural fixes only — skip Gemini content fixes')
    args = parser.parse_args()

    # Resolve gold path: direct first, then relative to gold_workbooks/
    gold = Path(args.gold)
    if not gold.exists():
        gold = GOLD_DIR / args.gold
    if not gold.exists():
        parser.error(f'Gold file not found: {args.gold}  (tried gold_workbooks/{args.gold})')

    run(
        url            = args.url,
        name           = args.name,
        gold_path      = gold,
        mode           = args.mode,
        chunk          = args.chunk,
        sheet_name     = args.sheet,
        skip_generate  = args.skip_generate,
        skip_fix       = args.skip_fix,
        no_content_fix = args.no_content_fix,
    )