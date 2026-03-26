import pandas as pd
import google.genai as genai
from google.genai import types
import io
import os
from dotenv import load_dotenv
 
PROMPT = """You are a deterministic CSV cleanup agent. Return only valid semicolon-delimited CSV. No markdown, no code blocks, no commentary, no extra text.
 
The output must begin with exactly this header line:
Problem Name;Row Type;Title;Body Text;Answer;answerType;HintID;Dependency;mcChoices;Images (space delimited);Parent;OER src;openstax KC;KC;Taxonomy;License
 
Every row must contain exactly 16 semicolons. Empty fields must still be delimited (write ;; not ;). Never add a leading or trailing semicolon.
 
TASK
Only modify scaffold rows where the answer is unreasonable for a student to enter reliably as free text. Convert those rows to multiple choice. Leave every other row completely unchanged.
 
CONVERT TO MC IF THE SCAFFOLD ANSWER IS ANY OF THESE
- A function, equation, or expression (e.g. f(x) = ..., y = ..., write the formula)
- Multiple values asked for at once (e.g. find A and B, state the amplitude and period)
- An ordered pair or coordinate
- A graph description or interval
- answerType = algebraic AND the answer is complex enough that format variation would cause wrong grading (e.g. a nested fraction with radicals, a trig expression with multiple terms)
 
DO NOT CONVERT
- Scaffolds already set to answerType = mc
- Scaffolds with simple single-value answers: an integer, a simple fraction, a single variable, a single trig value like pi/3
 
FOR EACH CONVERTED ROW
- Set answerType = mc
- Generate exactly 4 pipe-delimited mcChoices, one of which exactly matches the existing Answer
- The 3 distractors must be mathematically plausible: common sign errors, wrong formula, nearby values - not obviously wrong
- Leave Answer unchanged as the correct value
- Leave all other fields in the row unchanged
 
INPUT CSV:
[INPUT_CSV]
"""
 
def call_gemini(client, model_name, csv_string):
    final_prompt = PROMPT.replace("[INPUT_CSV]", csv_string)
    response = client.models.generate_content(
        model=model_name,
        contents=final_prompt,
        config=types.GenerateContentConfig(temperature=0.0)
    )
    clean = response.text.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[-1].rsplit("\n", 1)[0].strip()
    return clean
 
def additional_modifier(input_xlsx, problems_per_chunk=5, model_name="gemini-2.5-flash"):
    load_dotenv()
    key = os.getenv('GEMINI_API_KEY')
    client = genai.Client(api_key=key)
 
    df = pd.read_excel(input_xlsx, dtype=str).fillna('')
 
    unique_problems = df['Problem Name'].replace('', pd.NA).dropna().unique()
    total_chunks = (len(unique_problems) + problems_per_chunk - 1) // problems_per_chunk
 
    processed_chunks = []
 
    for i in range(0, len(unique_problems), problems_per_chunk):
        batch_names = unique_problems[i : i + problems_per_chunk]
        chunk_df = df[df['Problem Name'].isin(batch_names)]
        chunk_num = (i // problems_per_chunk) + 1
        print(f"Processing chunk {chunk_num}/{total_chunks} ({batch_names[0]}-{batch_names[-1]})...")
 
        csv_string = chunk_df.to_csv(index=False, sep=';')
 
        try:
            clean = call_gemini(client, model_name, csv_string)
            result_df = pd.read_csv(io.StringIO(clean), sep=';', dtype=str, on_bad_lines='skip').fillna('')
            processed_chunks.append(result_df)
        except Exception as e:
            print(f"  Chunk {chunk_num} failed, using original. Error: {e}")
            processed_chunks.append(chunk_df)
 
    if not processed_chunks:
        raise ValueError("No chunks processed.")
 
    final_df = pd.concat(processed_chunks, ignore_index=True)
    return final_df

def main():

    for filename in os.listdir('.'):
        if filename.endswith('final_ready_for_upload.xlsx'):
            print(f'processing {filename}...')
            try:
                modified_df = additional_modifier(filename)
                output_filename = filename.replace('final_ready_for_upload.xlsx', 'modified.xlsx')
                modified_df.to_excel(output_filename, index=False)
                print(f'Saved modified XLSX to {output_filename}')
            except Exception as e:
                print(f'Error processing {filename}: {e}')

if __name__ == "__main__":
    main()