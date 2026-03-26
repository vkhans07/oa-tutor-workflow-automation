from oa_tutor_agent import OATutorAgent
import pandas as pd

def main():
   
    agent_sumprod = OATutorAgent(
        name="sumprod",
        sheet_name="sumprod_sheet",
        book_url="https://openstax.org/books/precalculus-2e/pages/7-4-sum-to-product-and-product-to-sum-formulas",
        gold_df = pd.read_excel("gold.xlsx")
    )
    agent_sumprod.generate_curriculum()

    agent_trigonometric = OATutorAgent(
        name="trigonometric",
        sheet_name="trigonometric_sheet",
        book_url="https://openstax.org/books/precalculus-2e/pages/7-5-solving-trigonometric-equations",
        gold_df = pd.read_excel("gold.xlsx")
    )
    agent_trigonometric.generate_curriculum()
    
    agent_trigmodel = OATutorAgent(
        name="trigmodel",
        sheet_name="trigmodel_sheet",
        book_url="https://openstax.org/books/precalculus-2e/pages/7-6-modeling-with-trigonometric-functions",
        gold_df = pd.read_excel("gold.xlsx")
    )
    agent_trigmodel.generate_curriculum()

    agent_lawsin = OATutorAgent(
        name="lawsin",
        sheet_name="lawsin_sheet",
        book_url="https://openstax.org/books/precalculus-2e/pages/8-1-non-right-triangles-law-of-sines",
        gold_df = pd.read_excel("gold.xlsx")
    )
    agent_lawsin.generate_curriculum()

if __name__ == "__main__":
    main()