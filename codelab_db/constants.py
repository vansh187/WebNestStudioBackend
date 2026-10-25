"""Values more than one part of the database proxy must agree on."""

import re

DB_PATH = "/api/codelab/db"
ENGINES = ("postgres", "mysql", "mongodb")
SQL_ENGINES = ("postgres", "mysql")
LOGGER_NAME = "webnest.codelab_db"

_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
# A lowercase DNS name with at least two labels: no port, user info or trailing dot.
HOST_PATTERN = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")
LABEL_PATTERN = re.compile(rf"^{_LABEL}$")

MONGO_OPERATIONS = (
    "find", "find_one", "insert_one", "insert_many", "update_one", "update_many", "delete_one", "delete_many",
    "count_documents", "aggregate", "list_collection_names",
)
MONGO_WRITE_OPERATIONS = ("insert_one", "insert_many", "update_one", "update_many", "delete_one", "delete_many")
MONGO_SYSTEM_DATABASES = ("admin", "local", "config")
