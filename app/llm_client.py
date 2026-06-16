import os
import google.generativeai as genai
from google.generativeai.types import GenerateContentConfig

def call_llm(prompt : str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not defined.")

    genai.configure(api_key = api_key)
    model_name = os.getenv("LLM_MODEL", "gemini-2.5-flash")

    config = GenerateContentConfig( response_mime_type = "application/json", temperature = 0.1 )

    model = genai.GenerativeModel(model_name)
    response = model.generate_content( prompt, config=config )

    if not response.text:
        raise RuntimeError(f"LLM returned an empty response. Full response object: {response}")

    return response.text