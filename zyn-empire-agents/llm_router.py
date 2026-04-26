"""LLM router — Groq primary, Gemini fallback. Both free tiers, no card."""
import os
import time
from typing import List, Dict
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

class RateLimitError(Exception): pass

class LLMRouter:
    def __init__(self):
        self.groq_key = os.getenv("GROQ_API_KEY")
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        self.groq_model = "llama-3.3-70b-versatile"
        self.gemini_model = "gemini-2.0-flash-exp"
        self._init_clients()

    def _init_clients(self):
        self.groq = None
        self.gemini = None
        if self.groq_key:
            try:
                from groq import Groq
                self.groq = Groq(api_key=self.groq_key)
            except Exception as e:
                logger.warning(f"Groq init failed: {e}")
        if self.gemini_key:
            try:
                import google.generativeai as genai
                genai.configure(api_key=self.gemini_key)
                self.gemini = genai
            except Exception as e:
                logger.warning(f"Gemini init failed: {e}")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1,max=10), retry=retry_if_exception_type(RateLimitError))
    def chat(self, messages: List[Dict], temperature: float = 0.3) -> str:
        if self.groq:
            try:
                resp = self.groq.chat.completions.create(
                    model=self.groq_model,
                    messages=messages,
                    temperature=temperature,
                )
                return resp.choices[0].message.content
            except Exception as e:
                msg = str(e).lower()
                if "rate" in msg or "429" in msg or "quota" in msg:
                    logger.warning("Groq rate-limited, falling back to Gemini")
                else:
                    logger.warning(f"Groq error: {e}, trying Gemini")
        if self.gemini:
            try:
                model = self.gemini.GenerativeModel(self.gemini_model)
                prompt = "\n\n".join([f"{m.get('role','user').upper()}: {m['content']}" for m in messages])
                resp = model.generate_content(prompt, generation_config={"temperature": temperature})
                return resp.text
            except Exception as e:
                logger.error(f"Gemini also failed: {e}")
                raise
        raise RuntimeError("No LLM available — set GROQ_API_KEY or GEMINI_API_KEY in .env")

router = LLMRouter()
