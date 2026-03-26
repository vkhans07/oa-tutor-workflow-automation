from oa_tutor_agent import OATutorAgent
import pandas as pd
import google.genai as genai
from google.genai import types
from google.genai.types import GenerationConfig
import io
import os
from dotenv import load_dotenv

def additional_modifier(input_xlsx):

    load_dotenv()
    key = os.getenv('GEMINI_API_KEY')

    PROMPT = """
        You are a deterministic CSV cleanup agent. Return only valid semicolon-delimited CSV. No markdown, no code blocks, no commentary, no extra text.

        The output must begin with exactly this header line:
        Problem Name;Row Type;Title;Body Text;Answer;answerType;HintID;Dependency;mcChoices;Images (space delimited);Parent;OER src;openstax KC;KC;Taxonomy;License

        Every row must contain exactly 16 semicolons. Empty fields must still be delimited (write ;; not ;). Never add a leading or trailing semicolon.

        TASK
        Only modify scaffold rows where the answer is unreasonable for a student to enter reliably as free text. Convert those rows to multiple choice. Leave every other row completely unchanged.

        CONVERT TO MC IF THE SCAFFOLD ANSWER IS ANY OF THESE

            A function, equation, or expression (e.g. f(x) = ..., y = ..., write the formula)
            Multiple values asked for at once (e.g. find A and B, state the amplitude and period)
            An ordered pair or coordinate
            A graph description or interval
            answerType = algebraic AND the answer is complex enough that format variation would cause wrong grading (e.g. a nested fraction with radicals, a trig expression with multiple terms)

        DO NOT CONVERT

            Scaffolds already set to answerType = mc
            Scaffolds with simple single-value answers: an integer, a simple fraction, a single variable, a single trig value like π/3

        FOR EACH CONVERTED ROW

            Set answerType = mc
            Generate exactly 4 pipe-delimited mcChoices, one of which exactly matches the existing Answer
            The 3 distractors must be mathematically plausible: common sign errors, wrong formula, nearby values — not obviously wrong
            Leave Answer unchanged as the correct value
            Leave all other fields in the row unchanged
        
        INPUT CSV:
        [PASTE CSV HERE]
        """
    
    csv_string = pd.read_excel(input_xlsx, dtype=str).fillna('').to_csv(index=False, sep=';')
    final_prompt = PROMPT.replace("[PASTE CSV HERE]", csv_string)
    client = genai.Client(api_key=key)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=final_prompt,
        config=types.GenerateContentConfig(temperature=0.0)
    )

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
    
def main():

    for filename in os.listdir('.'):
        if filename.endswith('upload.xlsx') and not filename.startswith('trigmodel') and not filename.startswith('angle') and not filename.startswith('lawsin'):
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