"""stmtflow: scalable, self-verifying extraction of transactions from digital PDF bank statements."""
from .models import PARSER_VERSION, Config
from .pipeline import extract, process_batch

__all__ = ["extract", "process_batch", "Config", "PARSER_VERSION"]
__version__ = PARSER_VERSION
