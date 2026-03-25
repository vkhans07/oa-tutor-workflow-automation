from oa_tutor_agent import OATutorAgent
import pandas as pd

def main():
    agent = OATutorAgent(
        name="trig",
        sheet_name="trig_sheet",
        book_url = "https://openstax.org/books/precalculus-2e/pages/7-2-sum-and-difference-identities",
        gold_df = pd.read_excel("gold.xlsx")
    )

    agent.generate_curriculum()

if __name__ == "__main__":
    main()