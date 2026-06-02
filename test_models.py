import os
from google import genai
from dotenv import load_dotenv

load_dotenv()

def list_models():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or api_key == "your_api_key_here":
        print("API Key not set correctly in .env")
        return

    client = genai.Client(api_key=api_key)
    print("Available models:")
    try:
        for model in client.models.list():
            print(model)
    except Exception as e:
        print(f"Error listing models: {e}")

if __name__ == "__main__":
    list_models()
