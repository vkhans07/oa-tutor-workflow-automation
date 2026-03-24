import gspread
from playwright.sync_api import sync_playwright
import pandas as pd

class OATutorAgent:
    def __init__(self, name: str, credentials_path: str='credentials.json'):
        self.name = name
        self.credentials_path = credentials_path

    def load_data(self, url: str, worksheet_name: str) -> pd.DataFrame:
        gc = gspread.service_account(filename='credentials.json')
        sheet = gc.open_by_url(url).worksheet(worksheet_name)
        list_of_lists = sheet.get_all_values()
        df = pd.DataFrame(list_of_lists[1:], columns=list_of_lists[0])
        df = df.iloc[:, :16] # keep only up to 'P'
        df = df.loc[:, ~df.columns.str.contains('^Unnamed')]
        df = df.loc[:, df.columns != '']
        return df