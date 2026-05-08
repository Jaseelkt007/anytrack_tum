"""Project-level pytest config — load .env before any test imports."""
from dotenv import load_dotenv

load_dotenv()
