import re
from pathlib import Path

# URL for the Explain API, musst be compatible with PEV2
API_URL = "https://explain.dalibo.com"

# Path to the directory where PostgreSQL log files are stored.
LOG_DIR = Path("/var/log/postgresql")

# OPTIONAL: Define custom fields to extract from the queries
EXTRA_FIELDS = [
    {
        "name": "Customer",
        "key": "customer",
        "justify": "left",
        "regex": re.compile(r"\bcustomer_id\"?\s*=\s*(\d+)", re.IGNORECASE),
        "aggregation": "count",  # Can be one of "count", "sample" or "list"
    }
]

# Database connection string for PostgreSQL. This should be a string in the format:
PG_CONNINFO = "host=localhost " "dbname=mydb " "user=myuser " "password=mypass"

# Locale to use for formatting numbers and dates.
# This should be a string like "en_US.UTF-8" or "fr_FR.UTF-8".
# Leave empty to use the system locale
LOCALE = ""
