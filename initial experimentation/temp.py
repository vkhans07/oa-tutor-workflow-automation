from google import genai
import os
from dotenv import load_dotenv

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

print(f"{'Model Name':<40} | {'Supported Actions'}")
print("-" * 70)

for m in client.models.list():
    # Show the model name and which actions it supports (like 'generateContent')
    actions = ", ".join(m.supported_actions) if m.supported_actions else "None"
    print(f"{m.name:<40} | {actions}")